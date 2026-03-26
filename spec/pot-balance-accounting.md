# Spec: Pot Balance Accounting — Reconcile Pots with Wallet Balance

## Problem

The Scalper and Trend trading pots are virtual accounting values (`EMA_POT_TAO + realized_pnl`)
that do not reconcile with the actual coldkey wallet balance. Example from live data:

- Scalper pot: 13.76 τ (started at 10 τ, +3.76 τ realized P&L)
- Trend pot: 10.19 τ (started at 10 τ, +0.19 τ realized P&L)
- **Combined pots: 23.95 τ**
- **Actual wallet balance: 8.31 τ**

The pots claim 23.95 τ of capital exists, but the wallet only has 8.31 τ free (plus ~9.9 τ
deployed in stakes = ~18.2 τ total on-chain). The ~5.7 τ gap is unexplained.

This makes the dashboard misleading — users see pot values that exceed their actual holdings.

## Root Cause

1. Pot values are calculated as `config.pot_tao + cumulative_realized_pnl` (in-memory).
2. `realized_pnl` accumulates from closed-trade P&L but is never reconciled against on-chain
   balance. Rounding errors, fees, slippage variance, and emission rewards all create drift.
3. The frontend shows pot values as if they represent real capital, but they're bookkeeping
   estimates that diverge over time.
4. There is no "total account value" figure that sums `wallet_balance + total_staked_alpha_value`.

## Requirements

### R1 — On-chain NAV calculation
Add a `wallet_nav` field to the `/api/ema/portfolio` response:
```json
"wallet_nav": {
  "free_balance": 8.31,
  "staked_value_tao": 9.90,
  "total_nav": 18.21,
  "pot_total": 23.95,
  "drift_tao": -5.74,
  "drift_pct": -23.97
}
```
- `free_balance`: actual coldkey TAO balance (already fetched)
- `staked_value_tao`: sum of all open positions' current TAO value (alpha × current price)
- `total_nav`: free + staked
- `pot_total`: sum of both pots' virtual values
- `drift_tao / drift_pct`: difference between pot accounting and on-chain reality

### R2 — Dashboard display
On the frontend EMA page and widget, show:
- **Wallet NAV** (on-chain truth) prominently
- Pot values as secondary/breakdown info
- A drift indicator if |drift| > 2% (amber warning)

### R3 — Pot auto-correction (optional, discuss first)
Consider periodically re-syncing pot values to on-chain reality:
- On bot startup, recalculate `realized_pnl` from DB closed trades instead of accumulating in-memory
- Or: derive pot value as `(wallet_balance + staked_value) / 2` when both strategies are enabled

## Files to Modify

| File | Change |
|------|--------|
| `app/main.py` | Add `wallet_nav` to `/api/ema/portfolio` response |
| `app/portfolio/ema_manager.py` | Add method to compute staked TAO value from open positions |
| `frontend/src/app/ema/page.tsx` | Display wallet NAV, show drift warning |
| `widget.py` | Display wallet NAV in header section |

## Out of Scope
- Changing how pots are configured (EMA_POT_TAO stays as-is)
- Multi-wallet support
