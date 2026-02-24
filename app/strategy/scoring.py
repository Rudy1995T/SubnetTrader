"""
Ensemble scoring – combines all signals into a single composite score per subnet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.config import settings
from app.strategy.signals import (
    trend_momentum_signal,
    support_resistance_signal,
    fibonacci_signal,
    volatility_breakout_signal,
    mean_reversion_signal,
    value_band_boost,
)


@dataclass
class SubnetSignals:
    """All computed signals for a single subnet."""
    netuid: int
    trend: float = 0.0
    support_resistance: float = 0.0
    fibonacci: float = 0.0
    volatility: float = 0.0
    mean_reversion: float = 0.0
    value_band: float = 0.0
    composite: float = 0.0

    def to_dict(self) -> dict:
        return {
            "netuid": self.netuid,
            "trend": round(self.trend, 4),
            "support_resistance": round(self.support_resistance, 4),
            "fibonacci": round(self.fibonacci, 4),
            "volatility": round(self.volatility, 4),
            "mean_reversion": round(self.mean_reversion, 4),
            "value_band": round(self.value_band, 4),
            "composite": round(self.composite, 4),
        }


@dataclass
class ScoredSubnet:
    """A subnet with its composite score and classification."""
    netuid: int
    signals: SubnetSignals
    score: float
    eligible_entry: bool = False
    high_conviction: bool = False
    rank: int = 0

    def to_dict(self) -> dict:
        return {
            "netuid": self.netuid,
            "score": round(self.score, 4),
            "eligible_entry": self.eligible_entry,
            "high_conviction": self.high_conviction,
            "rank": self.rank,
            "signals": self.signals.to_dict(),
        }


def normalize_signal(value: float) -> float:
    """Clamp signal to [0, 1]."""
    return max(0.0, min(1.0, value))


def compute_signals(
    netuid: int,
    prices: Sequence[float],
    alpha_price: float,
) -> SubnetSignals:
    """
    Compute all six signals for a given subnet.
    *prices* is the historical price series (oldest → newest).
    *alpha_price* is the current alpha token price in TAO.
    """
    signals = SubnetSignals(netuid=netuid)

    signals.trend = normalize_signal(trend_momentum_signal(prices))
    signals.support_resistance = normalize_signal(support_resistance_signal(prices))
    signals.fibonacci = normalize_signal(fibonacci_signal(prices))
    signals.volatility = normalize_signal(volatility_breakout_signal(prices))
    signals.mean_reversion = normalize_signal(mean_reversion_signal(prices))
    signals.value_band = normalize_signal(value_band_boost(alpha_price))

    # Weighted composite
    signals.composite = (
        settings.W_TREND * signals.trend
        + settings.W_SUPPORT_RESISTANCE * signals.support_resistance
        + settings.W_FIBONACCI * signals.fibonacci
        + settings.W_VOLATILITY * signals.volatility
        + settings.W_MEAN_REVERSION * signals.mean_reversion
        + settings.W_VALUE_BAND * signals.value_band
    )
    signals.composite = normalize_signal(signals.composite)

    return signals


def rank_subnets(
    subnet_data: list[dict],
    enter_threshold: float | None = None,
    high_conviction_threshold: float | None = None,
) -> list[ScoredSubnet]:
    """
    Score and rank all subnets.

    *subnet_data* is a list of dicts, each with:
      - netuid: int
      - prices: list[float]  (historical price series)
      - alpha_price: float   (current alpha price in TAO)

    Returns a list of ScoredSubnet sorted by composite score descending.
    """
    if enter_threshold is None:
        enter_threshold = settings.ENTER_THRESHOLD
    if high_conviction_threshold is None:
        high_conviction_threshold = settings.HIGH_CONVICTION_THRESHOLD

    scored: list[ScoredSubnet] = []

    for sd in subnet_data:
        netuid = sd["netuid"]
        prices = sd.get("prices", [])
        alpha_price = sd.get("alpha_price", 0.0)

        signals = compute_signals(netuid, prices, alpha_price)

        entry_eligible = signals.composite >= enter_threshold
        high_conv = signals.composite >= high_conviction_threshold

        scored.append(
            ScoredSubnet(
                netuid=netuid,
                signals=signals,
                score=signals.composite,
                eligible_entry=entry_eligible,
                high_conviction=high_conv,
            )
        )

    # Sort descending by score
    scored.sort(key=lambda s: s.score, reverse=True)

    # Assign ranks
    for i, s in enumerate(scored):
        s.rank = i + 1

    return scored


def select_entries(
    ranked: list[ScoredSubnet],
    available_slots: int,
    allow_double: bool | None = None,
    current_positions: set[int] | None = None,
    cooldown_netuids: set[int] | None = None,
) -> list[ScoredSubnet]:
    """
    Select which subnets to enter, respecting:
      - available slot count
      - double-slot for high conviction (if enabled)
      - already-held positions
      - cooldown netuids
    """
    if allow_double is None:
        allow_double = settings.ALLOW_DOUBLE_SLOT
    if current_positions is None:
        current_positions = set()
    if cooldown_netuids is None:
        cooldown_netuids = set()

    entries: list[ScoredSubnet] = []
    slots_used = 0

    for subnet in ranked:
        if slots_used >= available_slots:
            break

        if not subnet.eligible_entry:
            continue

        if subnet.netuid in current_positions:
            continue

        if subnet.netuid in cooldown_netuids:
            continue

        # Determine how many slots this entry uses
        slots_for_this = 1
        if allow_double and subnet.high_conviction and (available_slots - slots_used) >= 2:
            slots_for_this = 2

        entries.append(subnet)
        slots_used += slots_for_this

    return entries
