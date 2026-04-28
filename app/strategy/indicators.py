"""Technical indicators computed from close price series."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.strategy.ema_signals import Candle


def compute_atr(candles: "list[Candle]", period: int = 14) -> list[float]:
    """Compute Average True Range from OHLC candles.

    True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
    ATR = EMA of True Range over `period`.

    Returns list same length as input. First value uses high-low only
    (no previous close available).
    """
    if not candles:
        return []

    true_ranges: list[float] = []
    for i, c in enumerate(candles):
        hl = c.high - c.low
        if i == 0:
            true_ranges.append(hl)
        else:
            prev_close = candles[i - 1].close
            true_ranges.append(max(hl, abs(c.high - prev_close), abs(c.low - prev_close)))

    # EMA smoothing of true range
    if not true_ranges:
        return []

    alpha = 2.0 / (period + 1)
    atr_values: list[float] = [true_ranges[0]]
    for i in range(1, len(true_ranges)):
        atr_values.append(alpha * true_ranges[i] + (1 - alpha) * atr_values[-1])

    return atr_values


def compute_rsi(prices: list[float], period: int = 14) -> list[float]:
    """Compute RSI using exponential smoothing (Wilder's method).

    Returns list same length as input. First `period` values are approximate
    (seeded with simple average). Values range 0-100.
    """
    if len(prices) < 2:
        return [50.0] * len(prices)

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    # Seed with simple average
    avg_gain = sum(gains[:period]) / period if len(gains) >= period else sum(gains) / max(len(gains), 1)
    avg_loss = sum(losses[:period]) / period if len(losses) >= period else sum(losses) / max(len(losses), 1)

    rsi_values = [50.0]  # placeholder for first price (no delta)
    for i in range(len(deltas)):
        if i < period:
            # Use simple average for warmup
            g = sum(gains[: i + 1]) / (i + 1)
            l = sum(losses[: i + 1]) / (i + 1)
        else:
            g = (avg_gain * (period - 1) + gains[i]) / period
            l = (avg_loss * (period - 1) + losses[i]) / period
            avg_gain, avg_loss = g, l
        rs = g / l if l > 0 else 100.0
        rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    return rsi_values


def compute_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[list[float], list[float], list[float]]:
    """Compute MACD line, signal line, and histogram.

    Returns (macd_line, signal_line, histogram), each same length as input.
    """
    from app.strategy.ema_signals import compute_ema

    ema_fast = compute_ema(prices, fast)
    ema_slow = compute_ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = compute_ema(macd_line, signal_period)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def compute_bollinger_bands(
    prices: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[float], list[float], list[float]]:
    """Compute Bollinger Bands (upper, middle SMA, lower).

    Returns (upper, middle, lower), each same length as input.
    Early values use expanding window.
    """
    upper, middle, lower = [], [], []
    for i in range(len(prices)):
        window = prices[max(0, i - period + 1) : i + 1]
        mean = sum(window) / len(window)
        variance = sum((p - mean) ** 2 for p in window) / len(window)
        std = variance**0.5
        middle.append(mean)
        upper.append(mean + num_std * std)
        lower.append(mean - num_std * std)
    return upper, middle, lower


def bollinger_bandwidth(
    upper: list[float], lower: list[float], middle: list[float]
) -> list[float]:
    """Bandwidth = (upper - lower) / middle. Measures volatility squeeze."""
    return [
        (u - l) / m if m > 0 else 0.0
        for u, l, m in zip(upper, lower, middle)
    ]
