"""
EMA trend signal helpers for the live EMA strategy.

Uses partial warmup (seed = first price) since we only have ~45 bars of 4h data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.utils.time import parse_iso


@dataclass(frozen=True)
class Candle:
    start_ts: str
    end_ts: str
    open: float
    high: float
    low: float
    close: float
    sample_count: int = 1


def compute_ema(prices: list[float], period: int) -> list[float]:
    """Compute EMA with partial warmup (seed = first available price)."""
    if not prices:
        return []
    k = 2.0 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1.0 - k))
    return ema


def ema_signal(prices: list[float], period: int = 18, confirm: int = 3) -> str:
    """
    Returns 'BUY', 'SELL', or 'HOLD'.

    BUY:  last `confirm` closes are all above EMA
    SELL: last `confirm` closes are all below EMA
    """
    if len(prices) < confirm:
        return "HOLD"
    ema = compute_ema(prices, period)
    if len(ema) < confirm:
        return "HOLD"
    recent_p = prices[-confirm:]
    recent_e = ema[-confirm:]
    if all(p > e for p, e in zip(recent_p, recent_e)):
        return "BUY"
    if all(p < e for p, e in zip(recent_p, recent_e)):
        return "SELL"
    return "HOLD"


def dual_ema_signal(
    prices: list[float],
    fast_period: int,
    slow_period: int,
    confirm: int = 3,
) -> str:
    """Require both fast and slow EMA to agree for BUY.

    BUY:  both fast and slow EMA signals are BUY
    SELL: either fast or slow EMA signals SELL
    HOLD: otherwise
    """
    fast = ema_signal(prices, fast_period, confirm)
    slow = ema_signal(prices, slow_period, confirm)
    if fast == "BUY" and slow == "BUY":
        return "BUY"
    if fast == "SELL" or slow == "SELL":
        return "SELL"
    return "HOLD"


def bars_above_below_ema(prices: list[float], period: int = 18) -> int:
    """
    Returns positive int = bars consecutively above EMA,
    negative int = bars consecutively below EMA,
    0 = mixed / insufficient data.
    """
    if not prices:
        return 0
    ema = compute_ema(prices, period)
    count = 0
    for p, e in zip(reversed(prices), reversed(ema)):
        if p > e:
            if count < 0:
                break
            count += 1
        elif p < e:
            if count > 0:
                break
            count -= 1
        else:
            break
    return count


def build_sampled_candles(
    points: list[dict],
    timeframe_hours: int = 4,
    close_tolerance_minutes: int = 20,
) -> list[Candle]:
    """
    Build approximate OHLC candles from irregular timestamped price samples.

    The Taostats stream mixes completed 4h closes with newer intra-bar samples.
    This groups samples into the intended 4h candle, preserves the current
    candle's developing range, and drops the last candle if its close has not
    been observed yet.
    """
    parsed: list[tuple[datetime, float]] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        raw_ts = point.get("timestamp") or point.get("t")
        raw_price = point.get("price", point.get("close", point.get("alpha_price")))
        if raw_ts in (None, "") or raw_price in (None, ""):
            continue
        try:
            ts = _parse_point_ts(str(raw_ts))
            price = float(raw_price)
        except Exception:
            continue
        parsed.append((ts, price))

    if not parsed:
        return []

    parsed.sort(key=lambda item: item[0])
    timeframe_sec = timeframe_hours * 3600
    offset_sec = _infer_close_offset_seconds(parsed, timeframe_sec)

    buckets: dict[int, list[tuple[datetime, float]]] = {}
    for ts, price in parsed:
        close_epoch = _bucket_close_epoch(ts, timeframe_sec, offset_sec)
        buckets.setdefault(close_epoch, []).append((ts, price))

    candles: list[Candle] = []
    prior_close: float | None = None
    for close_epoch in sorted(buckets):
        samples = sorted(buckets[close_epoch], key=lambda item: item[0])
        sample_prices = [price for _, price in samples]
        open_price = prior_close if prior_close is not None else sample_prices[0]
        close_price = sample_prices[-1]
        high_price = max([open_price, *sample_prices])
        low_price = min([open_price, *sample_prices])
        close_dt = datetime.fromtimestamp(close_epoch, tz=timezone.utc)
        start_dt = close_dt - timedelta(hours=timeframe_hours)
        candles.append(
            Candle(
                start_ts=start_dt.isoformat(),
                end_ts=close_dt.isoformat(),
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                sample_count=len(sample_prices),
            )
        )
        prior_close = close_price

    if not candles:
        return []

    latest_ts = parsed[-1][0]
    last_close = _parse_point_ts(candles[-1].end_ts)
    tolerance = timedelta(minutes=close_tolerance_minutes)
    if latest_ts + tolerance < last_close:
        candles.pop()
    return candles


def bullish_ema_bounce(
    candles: list[Candle],
    period: int = 72,
    touch_tolerance_pct: float = 1.0,
    require_green: bool = True,
) -> bool:
    """
    Require a bullish reclaim of the slow EMA on the latest completed candle.

    Standard pullback logic:
      - slow EMA slope is rising
      - candle low touches / slightly pierces the EMA zone
      - candle closes back above the EMA
      - optionally require a green body
    """
    if len(candles) < 2:
        return False
    closes = [c.close for c in candles]
    slow_ema = compute_ema(closes, period)
    if len(slow_ema) < 2:
        return False

    last = candles[-1]
    ema_now = slow_ema[-1]
    ema_prev = slow_ema[-2]
    touch_limit = ema_now * (1.0 + touch_tolerance_pct / 100.0)

    if ema_now <= ema_prev:
        return False
    if last.low > touch_limit:
        return False
    if last.close <= ema_now:
        return False
    if require_green and last.close <= last.open:
        return False
    return True


def candle_close_prices(candles: list[Candle]) -> list[float]:
    """Extract closes from candle data."""
    return [c.close for c in candles]


def _parse_point_ts(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return parse_iso(value).astimezone(timezone.utc)


def _infer_close_offset_seconds(
    parsed: list[tuple[datetime, float]],
    timeframe_sec: int,
) -> int:
    offsets: list[int] = []
    for (prev_ts, _), (cur_ts, _) in zip(parsed, parsed[1:]):
        gap = (cur_ts - prev_ts).total_seconds()
        if gap >= timeframe_sec * 0.75:
            offsets.append(cur_ts.minute * 60 + cur_ts.second)
    if not offsets:
        offsets = [ts.minute * 60 + ts.second for ts, _ in parsed]
    offsets.sort()
    return offsets[len(offsets) // 2]


def _bucket_close_epoch(ts: datetime, timeframe_sec: int, offset_sec: int) -> int:
    adjusted = int(ts.timestamp()) - offset_sec
    bucket = adjusted // timeframe_sec
    if adjusted % timeframe_sec != 0:
        bucket += 1
    return bucket * timeframe_sec + offset_sec
