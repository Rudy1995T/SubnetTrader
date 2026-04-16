"""Shared math utilities."""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone


def pearson_r(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient between two equal-length price series.

    Returns 0.0 if series are too short or have zero variance.
    """
    n = min(len(xs), len(ys))
    if n < 5:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    da = math.sqrt(sum((x - mx) ** 2 for x in xs))
    db = math.sqrt(sum((y - my) ** 2 for y in ys))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def rolling_volatility(prices: list[float], window: int = 24) -> float | None:
    """Annualized volatility from a price series.

    Args:
        prices: chronological prices (e.g. 4h candles over 7 days = ~42 points)
        window: lookback window in data points (default 24 = ~4 days at 4h)

    Returns:
        Annualized volatility as a decimal (e.g. 0.45 = 45%), or None if
        insufficient data.
    """
    if len(prices) < window + 1:
        return None
    recent = prices[-window - 1:]
    log_returns = [
        math.log(recent[i] / recent[i - 1])
        for i in range(1, len(recent))
        if recent[i - 1] > 0
    ]
    if len(log_returns) < 5:
        return None
    std_dev = statistics.stdev(log_returns)
    # Annualize: assume 4h candles → 6 per day → 2190 per year
    return std_dev * math.sqrt(2190)


def gini_coefficient(values: list[float]) -> float:
    """Gini coefficient (0 = equal, 1 = one holder dominates).

    Expects a list of positive stake/balance values.
    """
    vals = sorted(v for v in values if v > 0)
    n = len(vals)
    if n == 0:
        return 0.0
    total = sum(vals)
    if total == 0:
        return 0.0
    cum = 0.0
    gini_sum = 0.0
    for i, v in enumerate(vals):
        cum += v
        gini_sum += (2 * (i + 1) - n - 1) * v
    return gini_sum / (n * total)


def compute_price_changes(seven_day_prices: list[dict], now_price: float) -> dict:
    """Compute day/week price changes from seven_day_prices list.

    Returns dict with keys: day_change_pct, week_change_pct (float or None
    if insufficient data within tolerance).
    """
    if not seven_day_prices or now_price <= 0:
        return {"day_change_pct": None, "week_change_pct": None}

    now_ts = datetime.now(timezone.utc)
    tolerance_sec = 6 * 3600  # 6 hours

    targets = {
        "day_change_pct": 24 * 3600,
        "week_change_pct": 7 * 24 * 3600,
    }

    # Parse timestamps once
    parsed: list[tuple[float, float]] = []
    for point in seven_day_prices:
        ts_raw = point.get("timestamp")
        price = point.get("price")
        if ts_raw is None or price is None:
            continue
        try:
            if isinstance(ts_raw, (int, float)):
                epoch = float(ts_raw)
            else:
                ts_str = str(ts_raw)
                # Handle ISO format with or without Z
                ts_str = ts_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                epoch = dt.timestamp()
            parsed.append((epoch, float(price)))
        except (ValueError, TypeError):
            continue

    if not parsed:
        return {"day_change_pct": None, "week_change_pct": None}

    now_epoch = now_ts.timestamp()
    result: dict[str, float | None] = {}

    for key, offset_sec in targets.items():
        target_epoch = now_epoch - offset_sec
        best_point = None
        best_diff = float("inf")
        for epoch, price in parsed:
            diff = abs(epoch - target_epoch)
            if diff < best_diff:
                best_diff = diff
                best_point = price
        if best_point is not None and best_diff <= tolerance_sec and best_point > 0:
            result[key] = (now_price - best_point) / best_point * 100.0
        else:
            result[key] = None

    return result
