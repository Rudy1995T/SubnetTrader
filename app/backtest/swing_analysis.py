"""
Swing analysis: measure how often subnet prices swing >=5%, 10%, 15%, 20%+
from any local trough to peak (and peak to trough).

Covers subnets ranked #5 to #45 by current price.

Usage:
    python -m app.backtest.swing_analysis
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

HISTORY_DIR = Path("data/backtest/history")
THRESHOLDS = [5, 10, 15, 20, 25, 30]


@dataclass
class Swing:
    netuid: int
    direction: str  # "up" or "down"
    pct: float
    start_idx: int
    end_idx: int
    start_price: float
    end_price: float
    hours: int


@dataclass
class SubnetStats:
    netuid: int
    price: float
    num_points: int
    swings: list[Swing] = field(default_factory=list)


def load_pool_snapshots() -> dict[int, dict]:
    path = HISTORY_DIR / "pool_snapshots.json"
    with open(path) as f:
        blob = json.load(f)
    return {int(k): v for k, v in blob.get("pools", {}).items()}


def load_history(netuid: int) -> list[float]:
    """Load hourly price series for a subnet, return as list of floats."""
    path = HISTORY_DIR / f"sn{netuid}.json"
    if not path.exists():
        return []
    with open(path) as f:
        blob = json.load(f)
    data = blob.get("data", [])
    # Sort by timestamp ascending
    data.sort(key=lambda x: x.get("timestamp", ""))
    prices = []
    for entry in data:
        try:
            p = float(entry["price"])
            if p > 0:
                prices.append(p)
        except (KeyError, ValueError, TypeError):
            continue
    return prices


def find_swings(prices: list[float], netuid: int, min_pct: float = 5.0) -> list[Swing]:
    """
    Detect all swings >= min_pct using a zigzag algorithm.

    Tracks running highs and lows from the last confirmed turning point.
    When the price reverses by >= min_pct from the extreme, that's a swing.
    """
    if len(prices) < 2:
        return []

    swings: list[Swing] = []
    # Start by looking for the first significant move
    last_low = prices[0]
    last_high = prices[0]
    low_idx = 0
    high_idx = 0
    # direction: None (undecided), "up" (tracking a rise), "down" (tracking a fall)
    direction: str | None = None

    for i in range(1, len(prices)):
        p = prices[i]

        if direction is None:
            # Determine initial direction
            if p <= last_low:
                last_low = p
                low_idx = i
            if p >= last_high:
                last_high = p
                high_idx = i
            # Check if we've moved enough from the start
            if last_low > 0 and (last_high - last_low) / last_low * 100 >= min_pct:
                if high_idx > low_idx:
                    direction = "up"
                    pct = (last_high - last_low) / last_low * 100
                    swings.append(Swing(
                        netuid=netuid, direction="up", pct=pct,
                        start_idx=low_idx, end_idx=high_idx,
                        start_price=last_low, end_price=last_high,
                        hours=high_idx - low_idx,
                    ))
                    last_low = last_high
                    low_idx = high_idx
                else:
                    direction = "down"
                    pct = (last_high - last_low) / last_high * 100
                    swings.append(Swing(
                        netuid=netuid, direction="down", pct=pct,
                        start_idx=high_idx, end_idx=low_idx,
                        start_price=last_high, end_price=last_low,
                        hours=low_idx - high_idx,
                    ))
                    last_high = last_low
                    high_idx = low_idx

        elif direction == "up":
            # Tracking upward — update high
            if p >= last_high:
                last_high = p
                high_idx = i
            # Check for reversal down
            if last_high > 0:
                drop_pct = (last_high - p) / last_high * 100
                if drop_pct >= min_pct:
                    # Confirm downswing from high to here
                    swings.append(Swing(
                        netuid=netuid, direction="down", pct=drop_pct,
                        start_idx=high_idx, end_idx=i,
                        start_price=last_high, end_price=p,
                        hours=i - high_idx,
                    ))
                    direction = "down"
                    last_low = p
                    low_idx = i

        elif direction == "down":
            # Tracking downward — update low
            if p <= last_low:
                last_low = p
                low_idx = i
            # Check for reversal up
            if last_low > 0:
                rise_pct = (p - last_low) / last_low * 100
                if rise_pct >= min_pct:
                    swings.append(Swing(
                        netuid=netuid, direction="up", pct=rise_pct,
                        start_idx=low_idx, end_idx=i,
                        start_price=last_low, end_price=p,
                        hours=i - low_idx,
                    ))
                    direction = "up"
                    last_high = p
                    high_idx = i

    return swings


def get_target_netuids() -> list[tuple[int, float]]:
    """Return netuids ranked #5 to #45 by price (descending)."""
    pools = load_pool_snapshots()
    ranked = []
    for netuid, entry in pools.items():
        if netuid == 0:
            continue
        price = float(entry.get("price", 0))
        if price > 0:
            ranked.append((netuid, price))
    ranked.sort(key=lambda x: x[1], reverse=True)
    # Rank 5 through 45 (0-indexed: 4 through 44)
    return ranked[4:45]


def run() -> None:
    targets = get_target_netuids()
    print(f"Analyzing {len(targets)} subnets (price rank #5 to #45)\n")
    print(f"{'':>4} {'Price range':>28}  |  ", end="")
    for t in THRESHOLDS:
        print(f" >={t:>2}%", end="")
    print("  | Avg duration (h)")
    print("-" * 110)

    all_swings: list[Swing] = []

    for rank_offset, (netuid, price) in enumerate(targets):
        rank = rank_offset + 5
        prices = load_history(netuid)
        if len(prices) < 10:
            print(f"  #{rank:>2} SN{netuid:<4} — insufficient data ({len(prices)} points)")
            continue

        swings = find_swings(prices, netuid)
        all_swings.extend(swings)

        price_min = min(prices)
        price_max = max(prices)

        print(f"  #{rank:>2} SN{netuid:<4} {price_min:.6f}–{price_max:.6f} TAO", end="  |  ")
        for t in THRESHOLDS:
            count = sum(1 for s in swings if s.pct >= t)
            print(f"  {count:>4}", end="")

        if swings:
            avg_h = sum(s.hours for s in swings) / len(swings)
            print(f"  | {avg_h:>6.1f}h", end="")
        print()

    # ── Summary ──
    print("\n" + "=" * 110)
    print("AGGREGATE SUMMARY (all subnets combined)")
    print("=" * 110)

    up_swings = [s for s in all_swings if s.direction == "up"]
    down_swings = [s for s in all_swings if s.direction == "down"]

    print(f"\nTotal swings detected: {len(all_swings)}  (↑ {len(up_swings)}  ↓ {len(down_swings)})")
    print(f"\n{'Threshold':>12} | {'Total':>6} | {'Up ↑':>6} | {'Down ↓':>6} | {'Avg hrs':>8} | {'Med hrs':>8} | {'Avg %':>7}")
    print("-" * 75)

    for t in THRESHOLDS:
        matching = [s for s in all_swings if s.pct >= t]
        up_match = [s for s in up_swings if s.pct >= t]
        dn_match = [s for s in down_swings if s.pct >= t]
        if matching:
            hours = sorted(s.hours for s in matching)
            avg_h = sum(hours) / len(hours)
            med_h = hours[len(hours) // 2]
            avg_pct = sum(s.pct for s in matching) / len(matching)
            print(f"  >= {t:>2}%     | {len(matching):>6} | {len(up_match):>6} | {len(dn_match):>6} | {avg_h:>7.1f}h | {med_h:>7}h | {avg_pct:>6.1f}%")
        else:
            print(f"  >= {t:>2}%     |      0 |      0 |      0 |       — |       — |      —")

    # ── Per-subnet swing frequency (swings per week) ──
    print(f"\n{'':>4} {'Subnet':>8} | {'Swings':>7} | {'Per week':>9} | {'Avg %':>7} | {'Max %':>7} | {'Bias':>6}")
    print("-" * 70)

    for rank_offset, (netuid, price) in enumerate(targets):
        rank = rank_offset + 5
        prices = load_history(netuid)
        if len(prices) < 10:
            continue
        sn_swings = [s for s in all_swings if s.netuid == netuid]
        if not sn_swings:
            continue
        hours_span = len(prices)
        weeks = hours_span / (24 * 7) if hours_span > 0 else 1
        per_week = len(sn_swings) / weeks
        avg_pct = sum(s.pct for s in sn_swings) / len(sn_swings)
        max_pct = max(s.pct for s in sn_swings)
        up_count = sum(1 for s in sn_swings if s.direction == "up")
        dn_count = sum(1 for s in sn_swings if s.direction == "down")
        bias = "↑" if up_count > dn_count else "↓" if dn_count > up_count else "="
        print(f"  #{rank:>2} SN{netuid:<4} | {len(sn_swings):>7} | {per_week:>8.1f} | {avg_pct:>6.1f}% | {max_pct:>6.1f}% | {bias:>4} ({up_count}↑/{dn_count}↓)")

    # ── Distribution of swing magnitudes ──
    print("\nSwing magnitude distribution:")
    buckets = [(5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 50), (50, 100), (100, 999)]
    for lo, hi in buckets:
        count = sum(1 for s in all_swings if lo <= s.pct < hi)
        bar = "█" * (count // 2)
        label = f"{lo}–{hi}%" if hi < 999 else f"{lo}%+"
        print(f"  {label:>8}: {count:>4}  {bar}")


if __name__ == "__main__":
    run()
