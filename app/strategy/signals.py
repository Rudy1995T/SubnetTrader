"""
Signal generators – each produces a normalized 0..1 score per subnet.
All functions are pure: take data in, return a float.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from app.config import settings


def _safe_array(data: Sequence[float]) -> np.ndarray:
    """Convert to numpy array, replacing NaN/None with 0."""
    arr = np.array(data, dtype=np.float64)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return arr


def _ema(data: np.ndarray, span: int) -> np.ndarray:
    """Exponential moving average."""
    if len(data) < 2:
        return data.copy()
    alpha = 2.0 / (span + 1)
    result = np.empty_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def _sma(data: np.ndarray, window: int) -> np.ndarray:
    """Simple moving average with edge padding."""
    if len(data) < window:
        return np.full_like(data, np.mean(data))
    cumsum = np.cumsum(np.insert(data, 0, 0))
    sma = (cumsum[window:] - cumsum[:-window]) / float(window)
    # Pad leading values
    pad = np.full(window - 1, sma[0] if len(sma) > 0 else 0.0)
    return np.concatenate([pad, sma])


def _rsi(prices: np.ndarray, period: int = 14) -> float:
    """Relative Strength Index for the most recent point."""
    if len(prices) < period + 1:
        return 50.0  # neutral
    deltas = np.diff(prices[-(period + 1) :])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger_bands(
    prices: np.ndarray, window: int = 20, num_std: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (upper, middle, lower) bands."""
    middle = _sma(prices, window)
    if len(prices) < window:
        std = np.std(prices) if len(prices) > 0 else 0.0
        upper = middle + num_std * std
        lower = middle - num_std * std
        return upper, middle, lower

    rolling_std = np.empty_like(prices)
    for i in range(len(prices)):
        start = max(0, i - window + 1)
        rolling_std[i] = np.std(prices[start : i + 1])

    upper = middle + num_std * rolling_std
    lower = middle - num_std * rolling_std
    return upper, middle, lower


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ═══════════════════════════════════════════════════════════════════
# SIGNAL 1: Trend / Momentum (EMA slope + EMA cross)
# ═══════════════════════════════════════════════════════════════════

def trend_momentum_signal(prices: Sequence[float]) -> float:
    """
    Combines:
      - EMA-12 vs EMA-26 cross direction (MACD-like)
      - Recent EMA slope (positive = bullish)
    Returns 0..1 (0 = strong bearish, 1 = strong bullish).
    """
    arr = _safe_array(prices)
    if len(arr) < 5:
        return 0.5  # neutral

    ema12 = _ema(arr, 12)
    ema26 = _ema(arr, 26)

    # Cross signal: how far EMA12 is above/below EMA26
    if ema26[-1] == 0:
        cross_ratio = 0.0
    else:
        cross_ratio = (ema12[-1] - ema26[-1]) / ema26[-1]

    # Normalize cross ratio: ±5% maps to 0..1
    cross_signal = _clamp(0.5 + cross_ratio / 0.10)

    # Slope signal: look at last 5 bars of EMA12
    if len(ema12) >= 5 and ema12[-5] != 0:
        slope = (ema12[-1] - ema12[-5]) / ema12[-5]
    else:
        slope = 0.0
    slope_signal = _clamp(0.5 + slope / 0.06)

    # Blend: 60% cross, 40% slope
    return _clamp(0.6 * cross_signal + 0.4 * slope_signal)


# ═══════════════════════════════════════════════════════════════════
# SIGNAL 2: Support / Resistance proximity (pivot-based)
# ═══════════════════════════════════════════════════════════════════

def support_resistance_signal(prices: Sequence[float]) -> float:
    """
    Identifies pivot-based support/resistance.
    Bullish when price is near support and holding.
    Returns 0..1 (higher = closer to support / bouncing off support).
    """
    arr = _safe_array(prices)
    if len(arr) < 10:
        return 0.5

    # Simple pivot: use recent high, low, close
    recent = arr[-20:] if len(arr) >= 20 else arr
    high = np.max(recent)
    low = np.min(recent)
    close = arr[-1]

    if high == low:
        return 0.5

    # Pivot point
    pivot = (high + low + close) / 3.0
    s1 = 2 * pivot - high  # Support 1
    r1 = 2 * pivot - low  # Resistance 1
    s2 = pivot - (high - low)  # Support 2

    spread = high - low

    # Score based on proximity to support levels
    dist_to_s1 = (close - s1) / spread if spread > 0 else 0
    dist_to_s2 = (close - s2) / spread if spread > 0 else 0

    # Near support and holding (close > support) is bullish
    if close >= s1:
        # Price above S1 - check if bouncing
        # Closer to S1 = potentially more opportunity
        support_score = _clamp(1.0 - dist_to_s1 * 0.5)
    elif close >= s2:
        # Between S2 and S1
        support_score = _clamp(0.6 + (close - s2) / (s1 - s2 + 1e-12) * 0.3)
    else:
        # Below S2 - bearish
        support_score = 0.2

    # Check if price is breaking above resistance
    if r1 > 0:
        resistance_break = _clamp((close - r1) / (spread * 0.5 + 1e-12) + 0.5)
    else:
        resistance_break = 0.5

    return _clamp(0.6 * support_score + 0.4 * resistance_break)


# ═══════════════════════════════════════════════════════════════════
# SIGNAL 3: Fibonacci retracement zone (0.5 - 0.618 pullback)
# ═══════════════════════════════════════════════════════════════════

def fibonacci_signal(prices: Sequence[float]) -> float:
    """
    Detects if price has pulled back into the 0.5 - 0.618 Fibonacci zone
    within a recent trend and is showing signs of support.
    Returns 0..1 (higher = price is reacting well at Fib level).
    """
    arr = _safe_array(prices)
    if len(arr) < 15:
        return 0.5

    # Find recent swing high and swing low (last 50 bars)
    lookback = min(50, len(arr))
    segment = arr[-lookback:]

    swing_high_idx = np.argmax(segment)
    swing_low_idx = np.argmin(segment)
    swing_high = segment[swing_high_idx]
    swing_low = segment[swing_low_idx]

    if swing_high == swing_low:
        return 0.5

    current = arr[-1]
    total_range = swing_high - swing_low

    # Determine if trend is up (low before high) or down (high before low)
    if swing_low_idx < swing_high_idx:
        # Uptrend: measure pullback from high
        retrace_level = (swing_high - current) / total_range
    else:
        # Downtrend: measure pullback from low (rally from low)
        retrace_level = (current - swing_low) / total_range

    # Fibonacci zones of interest
    fib_50 = 0.500
    fib_618 = 0.618

    # Score highest when in the golden zone (0.5 - 0.618)
    if fib_50 <= retrace_level <= fib_618:
        # Perfect zone - high score
        zone_center = (fib_50 + fib_618) / 2
        dist = abs(retrace_level - zone_center) / (fib_618 - fib_50)
        zone_score = 0.85 + 0.15 * (1.0 - dist)
    elif 0.382 <= retrace_level < fib_50:
        # Near Fib 38.2-50% - decent
        zone_score = 0.6
    elif fib_618 < retrace_level <= 0.786:
        # Extended pullback - less ideal
        zone_score = 0.4
    else:
        # Outside Fib zones
        zone_score = 0.2

    # Check for bullish reaction (last few bars bouncing)
    if len(arr) >= 3:
        recent_3 = arr[-3:]
        if recent_3[-1] > recent_3[-2] and recent_3[-2] >= recent_3[-3] * 0.998:
            # Bouncing up
            zone_score = min(1.0, zone_score + 0.1)

    return _clamp(zone_score)


# ═══════════════════════════════════════════════════════════════════
# SIGNAL 4: Volatility expansion / breakout (squeeze → expansion)
# ═══════════════════════════════════════════════════════════════════

def volatility_breakout_signal(prices: Sequence[float]) -> float:
    """
    Detects Bollinger Band squeeze followed by expansion.
    A squeeze resolving upward is bullish.
    Returns 0..1.
    """
    arr = _safe_array(prices)
    if len(arr) < 25:
        return 0.5

    upper, middle, lower = _bollinger_bands(arr, window=20, num_std=2.0)

    # Band width
    current_width = upper[-1] - lower[-1]
    if middle[-1] == 0:
        return 0.5
    current_bw_pct = current_width / middle[-1]

    # Historical band width (average)
    lookback = min(40, len(arr))
    widths = upper[-lookback:] - lower[-lookback:]
    avg_bw = np.mean(widths / (middle[-lookback:] + 1e-12))

    # Squeeze ratio: < 1 means bands are tighter than average
    squeeze_ratio = current_bw_pct / (avg_bw + 1e-12)

    # Recent expansion check: compare current BW to BW 5 bars ago
    if len(upper) >= 6:
        prev_width = upper[-6] - lower[-6]
        prev_bw = prev_width / (middle[-6] + 1e-12)
        expansion = current_bw_pct / (prev_bw + 1e-12)
    else:
        expansion = 1.0

    # Direction: is price above or below middle band?
    direction = 1.0 if arr[-1] > middle[-1] else 0.0

    # Scoring:
    # Best case: was squeezed (ratio < 0.8) and now expanding (>1.2) upward
    score = 0.5

    if squeeze_ratio < 0.8 and expansion > 1.2:
        # Squeeze resolving
        score = 0.75 + 0.25 * direction
    elif squeeze_ratio < 0.9 and expansion > 1.1:
        # Mild squeeze resolving
        score = 0.65 + 0.15 * direction
    elif expansion > 1.5:
        # Strong expansion (may already be in breakout)
        score = 0.55 + 0.25 * direction
    elif squeeze_ratio < 0.7:
        # Tight squeeze, not yet resolved - anticipation
        score = 0.60

    return _clamp(score)


# ═══════════════════════════════════════════════════════════════════
# SIGNAL 5: Range mean-reversion (Bollinger + RSI)
# ═══════════════════════════════════════════════════════════════════

def mean_reversion_signal(prices: Sequence[float]) -> float:
    """
    Mean-reversion within a range:
      - Oversold (RSI < 30 + near lower BB) → bullish bounce expected
      - Overbought (RSI > 70 + near upper BB) → bearish reversal expected
    Returns 0..1 (higher = bullish mean-reversion setup).
    """
    arr = _safe_array(prices)
    if len(arr) < 15:
        return 0.5

    rsi = _rsi(arr, period=14)
    upper, middle, lower = _bollinger_bands(arr, window=20, num_std=2.0)

    current = arr[-1]
    bb_width = upper[-1] - lower[-1]

    if bb_width == 0:
        return 0.5

    # Position within Bollinger bands: 0 = at lower, 1 = at upper
    bb_position = (current - lower[-1]) / bb_width

    # RSI component: normalize to 0..1 where 0 = overbought, 1 = oversold (bullish)
    rsi_signal = _clamp(1.0 - rsi / 100.0)

    # Combine: oversold + near lower band = strong buy signal
    if rsi < 30 and bb_position < 0.2:
        # Strong mean-reversion buy
        score = 0.85 + 0.15 * (1.0 - bb_position)
    elif rsi < 40 and bb_position < 0.35:
        # Moderate buy zone
        score = 0.65 + 0.15 * rsi_signal
    elif rsi > 70 and bb_position > 0.8:
        # Overbought near upper band - bearish for entry
        score = 0.15
    elif rsi > 60 and bb_position > 0.7:
        # Extended but not extreme
        score = 0.30
    else:
        # Neutral zone - blend signals
        score = 0.5 * rsi_signal + 0.5 * (1.0 - bb_position) * 0.8

    return _clamp(score)


# ═══════════════════════════════════════════════════════════════════
# SIGNAL 6: Value band boost (custom heuristic)
# ═══════════════════════════════════════════════════════════════════

def value_band_boost(
    alpha_price: float,
    band_low: float | None = None,
    band_high: float | None = None,
    decay: float | None = None,
) -> float:
    """
    Scoring boost for subnets with alpha price in the sweet-spot band.
    Inside [band_low, band_high]: returns 1.0.
    Outside: Gaussian decay.
    Returns 0..1.
    """
    if band_low is None:
        band_low = settings.VALUE_BAND_LOW
    if band_high is None:
        band_high = settings.VALUE_BAND_HIGH
    if decay is None:
        decay = settings.VALUE_BAND_DECAY

    if alpha_price <= 0:
        return 0.0

    if band_low <= alpha_price <= band_high:
        return 1.0

    # Distance from nearest band edge
    if alpha_price < band_low:
        distance = band_low - alpha_price
    else:
        distance = alpha_price - band_high

    # Gaussian decay: exp(-0.5 * (distance / decay)^2)
    if decay <= 0:
        return 0.0

    boost = math.exp(-0.5 * (distance / decay) ** 2)
    return _clamp(boost)
