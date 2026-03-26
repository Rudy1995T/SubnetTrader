# Spec: Fix Cross-Strategy Subnet Exclusion Race Condition

## Problem

Both the Scalper and Trend strategies entered SN69 simultaneously:
- Scalper position #136: entered at 13:15:02
- Trend position #137: entered at 13:15:27

This happened despite the cross-exclusion logic in `run_ema_cycle()`. Both strategies
should never hold the same subnet — if one sells, it dumps on the other's position,
causing unnecessary slippage and correlated losses.

## Root Cause

In `app/main.py` lines 126-138, the exclusion snapshots are taken **before** either
cycle runs:

```python
# Both snapshots taken BEFORE any entries happen
scalper_netuids = {p.netuid for p in await ema_scalper._open_positions_snapshot()}
trend_netuids = {p.netuid for p in await ema_trend._open_positions_snapshot()}

# Scalper runs — may enter SN69
await ema_scalper.run_cycle(globally_occupied=trend_netuids)
# Trend runs — still has stale snapshot, doesn't see Scalper's new SN69 position
await ema_trend.run_cycle(globally_occupied=scalper_netuids)
```

Since the scalper cycle runs first and may enter new subnets, the trend cycle's
`globally_occupied` set is stale — it was captured before the scalper entered anything.

## Requirements

### R1 — Re-snapshot after first cycle
After the scalper cycle completes, re-snapshot its positions before passing to the trend:

```python
scalper_netuids = set()
trend_netuids = set()

if ema_scalper:
    scalper_netuids = {p.netuid for p in await ema_scalper._open_positions_snapshot()}
if ema_trend:
    trend_netuids = {p.netuid for p in await ema_trend._open_positions_snapshot()}

if ema_scalper:
    await ema_scalper.run_cycle(globally_occupied=trend_netuids)
    # RE-SNAPSHOT after scalper may have entered new positions
    scalper_netuids = {p.netuid for p in await ema_scalper._open_positions_snapshot()}

if ema_trend:
    await ema_trend.run_cycle(globally_occupied=scalper_netuids)
```

### R2 — Exit coordination
When both strategies happen to hold the same subnet (legacy positions or edge cases):
- The exit watcher should detect dual-holdings and log a warning
- Exits should be staggered: if both want to exit the same subnet in the same cycle,
  only one exits per cycle to avoid double-dumping liquidity
- Add a check in `run_scalper_exit_watch` and `run_trend_exit_watch`

### R3 — Immediate fix for existing dual position
Add a one-time cleanup: if on startup both strategies hold the same netuid, log a
warning and let the position with worse P&L be closed first (or let the user decide
via Telegram `/close`).

## Files to Modify

| File | Change |
|------|--------|
| `app/main.py` | Fix `run_ema_cycle()` to re-snapshot after first strategy runs |
| `app/main.py` | Add dual-holding detection in exit watchers |
| `app/portfolio/ema_manager.py` | No changes needed (exclusion logic is correct, input is wrong) |

## Risk
- Low risk: the fix is a 2-line change (re-snapshot after first cycle)
- The exit coordination (R2) is defensive and can be done as a follow-up

## Testing
- Verify with both strategies enabled that entering the same subnet is blocked
- Check logs for "globally_occupied" debug output showing updated sets
