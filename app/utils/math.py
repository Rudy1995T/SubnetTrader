"""Shared math utilities."""
from __future__ import annotations

import math


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
