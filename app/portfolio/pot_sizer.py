"""Compute per-strategy pot sizes from settings and live wallet balance."""
from __future__ import annotations


_SENTINEL = {"meanrev": -1.0, "trend": -1.0, "flow": -1.0}


def compute_pots(wallet_balance_tao: float | None, settings) -> dict[str, float]:
    """Return ``{"meanrev": τ, "trend": τ, "flow": τ}`` based on ``EMA_POT_MODE``.

    - ``fixed``: per-strategy ``*_POT_TAO`` settings (0 for disabled strategies).
    - ``wallet_split``: splits ``max(0, wallet - EMA_FEE_RESERVE_TAO)`` across
      enabled strategies using ``EMA_{TREND,MEANREV,FLOW}_WEIGHT``. Weights of
      disabled strategies drop out and the remainder is renormalized.
      Returns a sentinel ({strategy: -1.0}) if the wallet read failed, so the
      caller can keep the previous cycle's pots.
    """
    enabled = {
        "meanrev": bool(settings.MR_ENABLED),
        "trend": bool(settings.EMA_B_ENABLED),
        "flow": bool(settings.FLOW_ENABLED),
    }
    mode = (getattr(settings, "EMA_POT_MODE", "fixed") or "fixed").lower()

    if mode != "wallet_split":
        return {
            "meanrev": float(settings.MR_POT_TAO) if enabled["meanrev"] else 0.0,
            "trend": float(settings.EMA_B_POT_TAO) if enabled["trend"] else 0.0,
            "flow": float(settings.FLOW_POT_TAO) if enabled["flow"] else 0.0,
        }

    if wallet_balance_tao is None:
        return dict(_SENTINEL)

    reserve = float(getattr(settings, "EMA_FEE_RESERVE_TAO", 1.0))
    spendable = max(0.0, float(wallet_balance_tao) - reserve)

    raw_weights = {
        "meanrev": max(0.0, float(getattr(settings, "EMA_MEANREV_WEIGHT", 0.25))),
        "trend": max(0.0, float(getattr(settings, "EMA_TREND_WEIGHT", 0.50))),
        "flow": max(0.0, float(getattr(settings, "EMA_FLOW_WEIGHT", 0.25))),
    }
    active = {k: w for k, w in raw_weights.items() if enabled[k] and w > 0}
    total = sum(active.values())
    if total <= 0:
        return {"meanrev": 0.0, "trend": 0.0, "flow": 0.0}

    return {
        k: round(spendable * (active.get(k, 0.0) / total), 6)
        for k in ("meanrev", "trend", "flow")
    }
