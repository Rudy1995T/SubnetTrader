# Feature: Wallet-Split Pot Sizing for EMA Strategies

## Goal

Replace the current **fixed `EMA_POT_TAO` / `EMA_B_POT_TAO`** values with an
optional **wallet-split mode**, where each strategy's pot is computed from the
live wallet balance minus a fee reserve, divided between strategies by a
configurable weight.

When the user deposits or withdraws TAO, both strategies should automatically
re-scale their pot on the next cycle — no `.env` edits, no restart.

## Motivation

Today, `EMA_POT_TAO=20` and `EMA_B_POT_TAO=10` are hard-coded numbers. If the
user sends 14 TAO to the wallet, that capital sits idle until they manually
edit `.env`. With two strategies running, this also means manually rebalancing
both pots whenever the wallet changes.

## Design

### New settings (in `app/config.py`)

```python
# Pot sizing mode
EMA_POT_MODE: str = "fixed"           # "fixed" | "wallet_split"
EMA_FEE_RESERVE_TAO: float = 1.0      # held back from the wallet for tx fees
EMA_POT_WEIGHT: float = 0.5           # Strategy A's share of (wallet - reserve)
# Strategy B implicitly receives (1 - EMA_POT_WEIGHT)
```

- `EMA_POT_MODE=fixed` → existing behavior (read `EMA_POT_TAO` / `EMA_B_POT_TAO`
  literally). Default, fully backwards compatible.
- `EMA_POT_MODE=wallet_split` → compute pots from wallet balance.

### Pot computation (wallet_split mode)

```
spendable      = max(0, wallet_balance_tao - EMA_FEE_RESERVE_TAO)
pot_A          = spendable * EMA_POT_WEIGHT
pot_B          = spendable * (1 - EMA_POT_WEIGHT)
```

Notes:
- `wallet_balance_tao` is the **free balance** of the coldkey (already fetched
  for the EMA frontend — see `app/main.py` `/api/ema/portfolio` and the wallet
  balance lookup it uses). Reuse the same source.
- If `EMA_B_ENABLED=false`, give Strategy A the full spendable amount.
- Round each pot to 6 decimals.
- If `spendable <= 0`, both pots are 0 and managers should skip entries.

### Where the pot is consumed

`pot_tao` lives on `StrategyConfig` (`app/config.py:20`) and is read by
`EmaManager` in many places:

- [ema_manager.py:247](app/portfolio/ema_manager.py#L247) `current_pot = self._cfg.pot_tao + self._realized_pnl`
- [ema_manager.py:513](app/portfolio/ema_manager.py#L513) unstaked-balance display
- [ema_manager.py:1003](app/portfolio/ema_manager.py#L1003), [1013](app/portfolio/ema_manager.py#L1013), [1015](app/portfolio/ema_manager.py#L1015) — entry sizing (`pot_tao * position_size_pct`)
- [ema_manager.py:1452](app/portfolio/ema_manager.py#L1452), [1484](app/portfolio/ema_manager.py#L1484) — summary/portfolio reporting

The simplest implementation is therefore: **mutate `self._cfg.pot_tao` on each
cycle** before any of these reads happen, rather than threading a new argument
everywhere.

### Implementation steps

1. **`app/config.py`**
   - Add the three new settings above.
   - Leave `EMA_POT_TAO` / `EMA_B_POT_TAO` defaults untouched (used as the
     "fixed" mode value).

2. **New helper `app/portfolio/pot_sizer.py`** (small, ~30 lines)
   ```python
   def compute_pots(wallet_balance_tao: float, settings) -> tuple[float, float]:
       """Return (pot_a, pot_b) based on EMA_POT_MODE."""
   ```
   - In `fixed` mode: return `(settings.EMA_POT_TAO, settings.EMA_B_POT_TAO)`.
   - In `wallet_split` mode: apply the formula above, honoring
     `EMA_B_ENABLED`.

3. **`app/main.py` — EMA cycle loop**
   - Find the function that runs the EMA tick (where both managers'
     `run_cycle` / `tick` are invoked — search for `EmaManager` instances and
     where `settings.EMA_POT_TAO` is read; line ~699 is one site).
   - Before invoking the managers, fetch the wallet balance (already done for
     the portfolio endpoint — extract or reuse that helper) and call
     `compute_pots(...)`.
   - Assign `mgr_a._cfg.pot_tao = pot_a` and `mgr_b._cfg.pot_tao = pot_b`.
   - Also update the `pot_tao` value used in the combined dashboard summary at
     [main.py:699](app/main.py#L699) and [main.py:834](app/main.py#L834).

4. **`/api/ema/portfolio` response**
   - Already returns `pot_tao` per strategy from `EmaManager.summary()`. Once
     `_cfg.pot_tao` is mutated, the existing field will Just Work.
   - Add two new top-level fields for the frontend so the user can see the
     mode in effect:
     - `pot_mode`: `"fixed" | "wallet_split"`
     - `fee_reserve_tao`: float
     - `wallet_balance_tao`: float (already present? confirm)

5. **`app/config_api.py`**
   - Add `EMA_POT_MODE`, `EMA_FEE_RESERVE_TAO`, `EMA_POT_WEIGHT` to
     `ALLOWED_KEYS` with appropriate validators
     (`enum:fixed,wallet_split`, `float_pos`, `float_pct_0_1`).
   - Add them to the strategy-config GET response groupings near
     [config_api.py:130](app/config_api.py#L130) and [152](app/config_api.py#L152).

6. **Frontend** (`frontend/src/app/ema/page.tsx`)
   - When `pot_mode === "wallet_split"`, show a small badge: *"Auto: 50/50 split,
     1 τ fee reserve"* near the pot display.
   - Optional: add the three settings to the EMA config form. Out of scope if
     time-pressed — the user can edit `.env` directly.

### Edge cases

- **Wallet balance read fails / returns None.** Fall back to the previous
  cycle's pot (don't crash, don't zero out mid-trade).
- **Open positions worth more than the new pot.** Don't force-close. The
  pot only governs *new* entries; `current_pot = pot_tao + realized_pnl` will
  go negative briefly and `select_entries` already guards against
  insufficient unstaked balance.
- **User flips `EMA_POT_MODE` at runtime via the config API.** Next cycle
  picks up the new value automatically — no special handling needed.
- **`EMA_POT_WEIGHT` outside [0, 1].** Validator should reject; clamp
  defensively in `compute_pots` as a safety net.

### Backwards compatibility

- Default `EMA_POT_MODE=fixed` → zero behavioral change for existing users.
- No DB migrations.
- No changes to position sizing formulas inside `EmaManager`.

## Testing

1. **Unit test** `compute_pots` in both modes, including:
   - `EMA_B_ENABLED=False` → all spendable goes to A.
   - `wallet_balance < fee_reserve` → both pots = 0.
   - Weight 0.6 → 60/40 split.

2. **Integration**: with `EMA_POT_MODE=wallet_split`, `EMA_FEE_RESERVE_TAO=1`,
   wallet at 31 TAO, expect pot_A = 15.0 and pot_B = 15.0 in
   `/api/ema/portfolio`.

3. **Manual**: deposit 5 TAO into the wallet, wait one EMA cycle, confirm both
   pots increased by ~2.5 TAO each without restarting the bot.

## Out of scope

- Rebalancing *open positions* when the pot changes.
- Per-position fixed TAO sizing (still % of pot).
- Dynamic weight based on recent strategy PnL.
