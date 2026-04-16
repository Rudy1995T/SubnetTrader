"""Compute per-strategy pot sizes from settings and live wallet balance."""
from __future__ import annotations


def compute_pots(wallet_balance_tao: float | None, settings) -> tuple[float, float]:
    """Return ``(pot_a, pot_b)`` based on ``EMA_POT_MODE``.

    - ``fixed``: returns the literal ``EMA_POT_TAO`` / ``EMA_B_POT_TAO`` settings.
    - ``wallet_split``: derives both pots from
      ``max(0, wallet_balance - EMA_FEE_RESERVE_TAO)`` split by ``EMA_POT_WEIGHT``.
      If Strategy B is disabled, all spendable goes to Strategy A.
    """
    mode = (getattr(settings, "EMA_POT_MODE", "fixed") or "fixed").lower()

    if mode != "wallet_split":
        return (
            float(settings.EMA_POT_TAO),
            float(settings.EMA_B_POT_TAO) if settings.EMA_B_ENABLED else 0.0,
        )

    if wallet_balance_tao is None:
        # Caller should fall back to previous pot — return -1 sentinel pair.
        return (-1.0, -1.0)

    reserve = float(getattr(settings, "EMA_FEE_RESERVE_TAO", 1.0))
    spendable = max(0.0, float(wallet_balance_tao) - reserve)

    if not settings.EMA_B_ENABLED:
        return (round(spendable, 6), 0.0)

    weight = float(getattr(settings, "EMA_POT_WEIGHT", 0.5))
    weight = max(0.0, min(1.0, weight))

    pot_a = round(spendable * weight, 6)
    pot_b = round(spendable * (1.0 - weight), 6)
    return (pot_a, pot_b)
