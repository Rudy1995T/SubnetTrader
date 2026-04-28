# Spec: Wire Mean-Reversion Strategy Through API, Frontend, and Widget

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-17
**Status:** Draft
**Depends on:** `replace-scalper-with-mean-reversion.md` (Phases 1–2 complete)

## Motivation

The mean-reversion signal layer (`app/strategy/mean_reversion.py`), config
(`meanrev_config()` / `MR_*`), and `EmaManager` strategy-type dispatch are
complete. The backtest engine also dispatches on `strategy_type`. However,
**none of the runtime surfaces have been wired up**:

- `app/main.py` still instantiates `EmaManager(…, strategy_a_config())` and
  names its global `ema_scalper`. The FastAPI endpoints return portfolio data
  keyed under `"scalper"` with `scalper_enabled` / `scalper_dry_run` flags.
- The frontend ([frontend/src/app/ema/page.tsx](frontend/src/app/ema/page.tsx))
  hard-codes the string `"scalper"` in ~40 places: the `DualData` type, the
  strategy-card key, color classes, filter badges, closed-trade split, and
  two-column grid.
- [widget.py](widget.py) hard-codes `"scalper"` in ~10 places: `_STRAT_COLOUR`,
  `_STRAT_TAG`, portfolio summation loops, and position packing.

As a result, the bot cannot launch against the new strategy — the API contract,
UI labels, and widget tags all point to a dead strategy identifier.

## Goal

Rename the live strategy slot from `"scalper"` to `"meanrev"` across the API
layer, frontend, and widget, and wire `meanrev_config()` into the manager
startup. After this spec, `/api/ema/portfolio` returns `{ meanrev, trend,
combined }`, the EMA page renders a "Mean-Reversion" card, and the widget
groups mean-reversion positions under the `"M"` tag.

## Non-goals

- Reworking the strategy-selection UI to be data-driven (we simply rename the
  slot; a future refactor can lift the tag out of string literals).
- Migrating historical `strategy = 'scalper'` rows in `ema_positions`. They
  remain as read-only history; new rows use `'meanrev'`.
- Any change to the Trend strategy, backtest framework, or signal layer.

## Design

### Naming convention

| Layer          | Before (scalper)            | After (meanrev)              |
|----------------|-----------------------------|------------------------------|
| API key        | `"scalper"`                 | `"meanrev"`                  |
| Flag names     | `scalper_enabled`, `scalper_dry_run`, `scalper_breaker_active` | `meanrev_enabled`, `meanrev_dry_run`, `meanrev_breaker_active` |
| Python global  | `ema_scalper`               | `ema_meanrev`                |
| Watch task IDs | `scalper_exit_watch`, `scalper_entry_watch` | `meanrev_exit_watch`, `meanrev_entry_watch` |
| Widget tag     | `"S"` (sky)                 | `"M"` (sky)                  |
| UI label       | "Scalper"                   | "Mean-Reversion"             |
| Signal fields  | `signal_scalper`, `mtf_scalper` | `signal_meanrev`, `mtf_meanrev` |

Color palette stays the same (sky/violet for slot A, cyan for slot B) — only
the labels and keys change. This keeps the visual identity consistent for
users watching the UI.

### API contract changes

`/api/ema/portfolio` response shape:

```jsonc
{
  "meanrev": { /* EmaManager.get_portfolio_summary() */ },
  "trend":   { /* ... */ },
  "combined": { /* ... */ },
  "meanrev_enabled": true,
  "meanrev_dry_run": false,
  "meanrev_breaker_active": false,
  "trend_enabled":   true,
  "trend_dry_run":   true,
  "trend_breaker_active": false
}
```

`/api/ema/signals` response items:

```jsonc
{
  "netuid": 42,
  "signal": "HOLD",
  "signal_meanrev": "BUY",
  "mtf_meanrev": false,
  "signal_trend": "HOLD",
  "mtf_trend": true,
  /* ... */
}
```

No breaking change for UI calls during deploy — the frontend rolls out at the
same time. External consumers (Telegram, logs) use the strategy tag from the
DB row, which is already `'meanrev'` for new positions.

### Database

No migration. The `ema_positions.strategy` column already stores whatever tag
the manager writes; `EmaManager` instantiated with `meanrev_config()` writes
`'meanrev'`. The historical default of `'scalper'` in [app/storage/db.py:48](app/storage/db.py#L48)
stays in place for backwards-compat of old rows. The default in the `insert`
helper at [app/storage/db.py:95](app/storage/db.py#L95) should be dropped
(callers always pass an explicit strategy tag), so the default value doesn't
lie.

## Implementation plan

### Phase 1: Backend API (`app/main.py`)

1. **Imports:** Replace `strategy_a_config` with `meanrev_config`.
2. **Global rename:** `ema_scalper` → `ema_meanrev` everywhere (71 hits). Do
   this with a targeted find-replace, then visually diff the cycle/exit/entry
   watcher logic to confirm no accidental collisions.
3. **Setting references:** Replace `settings.EMA_ENABLED`, `settings.EMA_DRY_RUN`,
   `settings.EMA_EXIT_WATCHER_ENABLED`, `settings.EMA_ENTRY_WATCHER_ENABLED`
   with the corresponding `settings.MR_*` names. (Verify which shared
   watcher settings — `EMA_EXIT_WATCHER_SEC`, `EMA_ENTRY_WATCHER_SEC` — remain
   unchanged per the existing spec.)
4. **Manager instantiation:** `EmaManager(db, executor, taostats, strategy_a_config())`
   → `EmaManager(db, executor, taostats, meanrev_config())`.
5. **Watch-task IDs:** Scheduler IDs `scalper_exit_watch` / `scalper_entry_watch`
   → `meanrev_exit_watch` / `meanrev_entry_watch`. Function names
   `run_scalper_exit_watch` / `run_scalper_entry_watch` → `run_meanrev_exit_watch`
   / `run_meanrev_entry_watch`.
6. **Dashboard/Telegram labels:** The terminal dashboard row labels (`"Scalper"`,
   `"SCL"`) → (`"MeanRev"`, `"MR"`). Telegram status output: `"Scalper"` →
   `"Mean-Reversion"`.
7. **API payload keys:** All three endpoints (`/api/ema/portfolio`,
   `/api/ema/positions`, `/api/ema/signals`) emit `meanrev*` keys.
8. **Signal computation:** The `signal_scalper` / `mtf_scalper` fields are
   computed by calling `dual_ema_signal()` with the manager's fast/slow/confirm
   bars. For mean-reversion this is misleading. **Replace** with:
   ```python
   sig_data["signal_meanrev"] = (
       "BUY" if meanrev_entry_signal(prices, …) else "HOLD"
   )
   ```
   Drop `mtf_meanrev` (MTF doesn't apply). Keep `signal_trend` / `mtf_trend`
   unchanged.

### Phase 2: Widget (`widget.py`)

1. `_STRAT_COLOUR = {"meanrev": SKY, "trend": PURPLE}` (keep sky for slot A).
2. `_STRAT_TAG = {"meanrev": "M", "trend": "T"}`.
3. Portfolio summation loops: change the iteration keys from `("scalper",
   "trend")` to `("meanrev", "trend")`.
4. Position packing: `pos.get("_strategy", "meanrev")` default; variable names
   `scalper_indices` → `meanrev_indices`.
5. Section label: `"Scalper"` header → `"Mean-Reversion"`.

### Phase 3: Frontend ([frontend/src/app/ema/page.tsx](frontend/src/app/ema/page.tsx))

1. **Type changes:**
   ```typescript
   type DualData = {
     meanrev: PortfolioData;
     trend:   PortfolioData;
     /* ... */
   };
   type SignalData = {
     signal_meanrev?: "BUY" | "SELL" | "HOLD";
     mtf_meanrev?: boolean;
     /* ... */
   };
   ```
2. Replace the `(["scalper", "trend"] as const)` tuple with
   `(["meanrev", "trend"] as const)`. All derived `stratKey` / `strat` lookups
   follow automatically.
3. `const meanrev = dualData?.meanrev;` (rename `scalper` → `meanrev` variable
   and destructure). Same for `meanrevClosed` etc.
4. Closed-position filter: `(p as any).strategy === "meanrev"` (with the
   `|| !(p as any).strategy` backstop so rows imported from `"scalper"` legacy
   history still render — optional; if we prefer a clean break, drop the
   backstop and old rows render under a new "Legacy" bucket or are hidden).
5. **Color map:** `_STRAT_COLOUR["meanrev"] = "bg-sky-900/60 text-sky-300
   border-sky-700"` (keep sky for slot A).
6. **Badges/labels:** header text `"Scalper"` → `"Mean-Reversion"`; short badge
   `"S"` → `"M"`.
7. **Default tag fallback:** `port.tag ?? "meanrev"` (was `"scalper"`).
8. Grep for any remaining `"scalper"` / `scalper` string literals and
   eliminate. The page has ~15 such sites — all should be rewritten.

### Phase 4: Incidental cleanup

1. **`app/storage/db.py`:** Drop the `strategy: str = "scalper"` default at
   [app/storage/db.py:95](app/storage/db.py#L95) — every caller passes an
   explicit strategy tag today. The migration default at
   [app/storage/db.py:48](app/storage/db.py#L48) stays (it's a one-time
   backfill for rows written before the column existed).
2. **`specs/run_qa.py`:** Update the one `"scalper"` reference to `"meanrev"`
   (or the QA equivalent, if it's iterating both strategies).
3. **`app/backtest/strategies.py`:** The `A1 = BacktestStrategyConfig(…,
   tag="scalper", …)` entry is historical (for backtesting the old strategy
   against historical data). Leave it — it's the backtest baseline the new
   strategy is evaluated against.

## Testing

1. **Backend smoke:**
   ```bash
   source .venv/bin/activate && python -c "from app.main import app; print([r.path for r in app.routes])"
   ```
   No import errors; endpoints enumerate.
2. **API contract:** `curl -s localhost:8081/api/ema/portfolio | jq` shows
   `meanrev` / `trend` keys with non-null values.
3. **Frontend build:** `cd frontend && npm run build` passes TypeScript
   strict-mode without `any` casts pointing at the old key.
4. **Live render:** start the stack (`bash start.sh`), open `localhost:3000/ema`,
   confirm the left card reads "Mean-Reversion" and shows the correct pot/size.
5. **Widget render:** launch the widget and confirm tag `"M"` appears for
   mean-reversion positions, `"T"` for trend.
6. **Signal field:** in `/api/ema/signals`, `signal_meanrev` fires `"BUY"`
   when a subnet has RSI<30 and a lower-BB bounce; otherwise `"HOLD"`.

## Risk mitigation

- **Big-bang rename risk:** ~100 string occurrences across 4 files. Mitigation:
  one commit per phase (backend / widget / frontend / cleanup). Each phase is
  independently testable — revertable with `git revert` if a phase breaks.
- **Historical data display:** users may have open positions tagged `"scalper"`
  in the DB when the rename lands. The frontend closed-filter's `|| !(p as
  any).strategy` backstop protects rendering; positions can be closed normally
  and the tag is cosmetic at that point. Verify the close-position button
  works on a legacy `"scalper"` row before deploy.
- **Telegram command compatibility:** if users have existing commands
  referencing `"scalper"`, they'll break. Search [app/services/](app/services/)
  for any command-dispatch literals and update.

## What NOT to change

- Color palette: sky for slot A, violet/cyan for slot B. Muscle memory matters.
- Database schema / migrations.
- Backtest config `A1 tag="scalper"` — that's historical-data identity for
  comparison studies.
- Any of the `EMA_B_*` (Trend) plumbing — strictly out of scope.

## Success criteria

- `/api/ema/portfolio` returns `{ meanrev, trend, combined, meanrev_enabled,
  meanrev_dry_run, … }` with the mean-reversion strategy live.
- The EMA page shows a "Mean-Reversion" card on the left with the correct
  pot (5 TAO default), max positions (4), and any active trades tagged
  `"meanrev"`.
- The widget shows `"M"` tags for mean-reversion positions and groups them
  under the "Mean-Reversion" header.
- `grep -rIn '"scalper"' app/ frontend/src widget.py` returns at most the
  intentional backstop in the frontend closed-trade filter and the historical
  backtest config A1.
