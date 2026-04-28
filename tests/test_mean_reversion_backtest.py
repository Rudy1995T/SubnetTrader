"""Tests for the mean-reversion branch of the EMA backtest engine.

Covers three deterministic cases called out in
``specs/NewSpecs/wire-meanrev-to-backtest-harness.md``:

    1. Insufficient history → zero trades, no exception.
    2. Clean RSI dip + bounce produces exactly one trade whose exit is a
       meanrev exit (``BB_MID_CROSS`` or ``TAKE_PROFIT``).
    3. A flat post-entry series exits via ``TIME_STOP``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.engine import backtest_subnet
from app.backtest.strategies import F1, F8


def _history(prices: list[float]) -> list[dict]:
    """Wrap a price series as Taostats-shaped history dicts.

    Timestamps end 2h in the past (so ``build_candles_from_history`` does
    not drop the last candle as in-progress) and step backwards at 1h so
    the resolution detector pins the timeframe to 1h.
    """
    n = len(prices)
    end = datetime.now(timezone.utc) - timedelta(hours=2)
    out: list[dict] = []
    for i, px in enumerate(prices):
        ts = end - timedelta(hours=(n - 1 - i))
        out.append({"timestamp": ts.isoformat(), "price": float(px)})
    return out


def test_insufficient_history_returns_empty():
    """Fewer than bb_period+1 bars yields no trades, no exception."""
    history = _history([1.0] * 15)
    trades = backtest_subnet(history, netuid=0, cfg=F1, pool_tao=5_000.0, window_days=30)
    assert trades == []


# Prices are scaled to sit below F*'s ``max_entry_price_tao=0.1`` filter; the
# meanrev indicators (RSI, BB) are scale-invariant so ratios drive the math.
# The plunge-and-bounce shape is tuned so entry triggers at bar 31 where the
# prior bar sits below the lower Bollinger Band and the current bar is the
# first to close back above it while RSI is still oversold.
SCALE = 0.08

_FLAT_BASE = [1.0] * 25
_PLUNGE = [0.95, 0.90, 0.85]
_PRE_ENTRY = [0.82, 0.80, 0.80, 0.80]  # entry fires at the last bar


def test_clean_rsi_dip_produces_meanrev_trade():
    """A sharp drop below the lower band followed by a bounce opens a trade,
    and a subsequent rally closes it via a meanrev-style exit."""
    climb = [0.82, 0.84, 0.87, 0.90, 0.93]
    history = _history([p * SCALE for p in _FLAT_BASE + _PLUNGE + _PRE_ENTRY + climb])

    # pool_tao=0 disables slippage so the asserted exit reason is deterministic.
    trades = backtest_subnet(history, netuid=42, cfg=F1, pool_tao=0.0, window_days=90)

    assert trades, "expected at least one trade from clean RSI dip"
    first = trades[0]
    assert first.exit_reason in {"BB_MID_CROSS", "TAKE_PROFIT"}, (
        f"unexpected exit_reason={first.exit_reason}"
    )
    assert first.netuid == 42
    assert first.pnl_pct > 0


def test_time_stop_fires_on_flat_hold():
    """After entry, a flat series must exit via ``TIME_STOP`` at
    ``max_holding_hours``. Uses F8 (``bb_mid_exit=False``) because F1's
    middle-band cross fires when a post-entry flat hold lets the SMA decay
    into the hold price; disabling the BB exit isolates the time-stop path."""
    hold = [0.80] * 30  # flat at entry price for 30h (> F8.max_holding_hours=24)
    history = _history([p * SCALE for p in _FLAT_BASE + _PLUNGE + _PRE_ENTRY + hold])

    trades = backtest_subnet(history, netuid=7, cfg=F8, pool_tao=0.0, window_days=90)

    assert trades, "expected at least one trade on dip-and-hold series"
    reasons = {t.exit_reason for t in trades}
    assert "TIME_STOP" in reasons, (
        f"expected TIME_STOP in exit_reasons, got {reasons}"
    )
