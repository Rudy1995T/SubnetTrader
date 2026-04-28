"""Pure signal functions for the mean-reversion strategy.

Entry: RSI oversold + price at/below lower Bollinger Band + bounce confirmation
       (close back above the lower band).
Exit:  BB_MID_CROSS (price above SMA20), RSI_OVERBOUGHT (while in profit),
       TAKE_PROFIT (hard cap).

Stop-loss and time-stop are handled by the caller.
"""
from __future__ import annotations

from app.strategy.indicators import compute_bollinger_bands, compute_rsi


def meanrev_entry_signal(
    prices: list[float],
    rsi_threshold: float = 30.0,
    rsi_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> bool:
    """Return True when all entry conditions hold on the latest bar:

    1. RSI(rsi_period) < rsi_threshold (oversold)
    2. Previous bar's low/close was at or below the lower Bollinger Band
    3. Current bar closes above the lower Bollinger Band (bounce confirmation)

    Needs at least bb_period + 1 prices.
    """
    if len(prices) < bb_period + 1:
        return False

    rsi = compute_rsi(prices, period=rsi_period)
    if not rsi or rsi[-1] >= rsi_threshold:
        return False

    _, _, lower = compute_bollinger_bands(prices, period=bb_period, num_std=bb_std)
    if len(lower) < 2:
        return False

    prev_price = prices[-2]
    cur_price = prices[-1]
    prev_lower = lower[-2]
    cur_lower = lower[-1]

    # The prior bar must have been at or below the lower band
    if prev_price > prev_lower:
        return False
    # The current bar must close back above the lower band (bounce)
    if cur_price <= cur_lower:
        return False

    return True


def meanrev_exit_signal(
    prices: list[float],
    entry_price: float,
    take_profit_pct: float = 8.0,
    rsi_exit: float = 65.0,
    rsi_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
    bb_mid_exit: bool = True,
) -> str | None:
    """Return exit reason or None.

    Checked in priority order:
      TAKE_PROFIT     — PnL% >= take_profit_pct
      BB_MID_CROSS    — price crosses up through SMA(bb_period) (mean reverted)
      RSI_OVERBOUGHT  — RSI > rsi_exit while in profit
    """
    if not prices or entry_price <= 0:
        return None

    cur_price = prices[-1]
    pnl_pct = (cur_price - entry_price) / entry_price * 100.0

    if pnl_pct >= take_profit_pct:
        return "TAKE_PROFIT"

    if bb_mid_exit and len(prices) >= bb_period + 1:
        _, middle, _ = compute_bollinger_bands(prices, period=bb_period, num_std=bb_std)
        if len(middle) >= 2:
            prev_mid = middle[-2]
            cur_mid = middle[-1]
            prev_price = prices[-2]
            # Cross from below to at-or-above the middle band
            if prev_price < prev_mid and cur_price >= cur_mid:
                return "BB_MID_CROSS"

    if pnl_pct > 0 and len(prices) >= rsi_period + 1:
        rsi = compute_rsi(prices, period=rsi_period)
        if rsi and rsi[-1] > rsi_exit:
            return "RSI_OVERBOUGHT"

    return None


def detect_new_meanrev_signal(
    prices: list[float],
    prev_state: str,
    rsi_threshold: float = 30.0,
    rsi_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> tuple[bool, str]:
    """For the entry watcher: detect a fresh mean-reversion setup.

    Returns (is_new_setup, new_state). `new_state` is "armed" when the entry
    signal fires this bar and the previous state was not "armed"; otherwise
    "idle". Caller persists new_state per-subnet.
    """
    armed = meanrev_entry_signal(
        prices,
        rsi_threshold=rsi_threshold,
        rsi_period=rsi_period,
        bb_period=bb_period,
        bb_std=bb_std,
    )
    new_state = "armed" if armed else "idle"
    is_new = armed and prev_state != "armed"
    return is_new, new_state
