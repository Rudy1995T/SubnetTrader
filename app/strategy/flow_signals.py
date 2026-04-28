"""Pool Flow Momentum signal helpers.

Windows are expressed in **snapshots**, not wall-clock hours, so that the math
adapts automatically if ``SCAN_INTERVAL_MIN`` changes. A snapshot is one row
persisted per subnet per scan cycle (default cadence 5 minutes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# ── Raw flow deltas ────────────────────────────────────────────────

def compute_flow_delta(
    snapshots: list[dict],
    window_snaps: int,
    field: str = "tao_in_pool",
) -> float | None:
    """Return the absolute change in ``field`` between the last snapshot and
    the snapshot ``window_snaps`` samples ago.

    Returns None if we don't have enough history. ``snapshots`` must be sorted
    oldest → newest.
    """
    if len(snapshots) <= window_snaps or window_snaps <= 0:
        return None
    current = snapshots[-1].get(field)
    prior = snapshots[-window_snaps - 1].get(field)
    if current is None or prior is None:
        return None
    return float(current) - float(prior)


def compute_flow_delta_pct(
    snapshots: list[dict],
    window_snaps: int,
    field: str = "tao_in_pool",
) -> float | None:
    """Return percentage change of ``field`` between now and window_snaps ago."""
    if len(snapshots) <= window_snaps or window_snaps <= 0:
        return None
    current = snapshots[-1].get(field)
    prior = snapshots[-window_snaps - 1].get(field)
    if current is None or prior is None:
        return None
    prior_f = float(prior)
    if prior_f <= 0:
        return None
    return (float(current) - prior_f) / prior_f * 100.0


# ── Emission adjustment ────────────────────────────────────────────

def emission_adjusted_flow(
    snapshots: list[dict],
    window_snaps: int,
    sold_fraction: float = 0.60,
) -> float | None:
    """Return TAO flow over window_snaps with expected emission contribution
    subtracted.

    The alpha_emission_rate is in alpha per block. Miners/validators tend to
    unstake a fraction and dump the TAO back into the pool, which inflates
    ``tao_in_pool`` without any real buying. We estimate and subtract that
    contribution.

    Falls back to the raw delta if no block number / emission rate data is
    available.
    """
    raw = compute_flow_delta(snapshots, window_snaps, field="tao_in_pool")
    if raw is None:
        return None

    start = snapshots[-window_snaps - 1]
    end = snapshots[-1]

    start_block = start.get("block_number")
    end_block = end.get("block_number")
    emission_rate = start.get("alpha_emission_rate") or end.get("alpha_emission_rate")

    if start_block is None or end_block is None or not emission_rate:
        return raw

    blocks_elapsed = max(0, int(end_block) - int(start_block))
    if blocks_elapsed == 0:
        return raw

    avg_price = (float(start.get("price", 0) or 0) + float(end.get("price", 0) or 0)) / 2.0
    if avg_price <= 0:
        return raw

    expected_alpha = float(emission_rate) * blocks_elapsed
    expected_tao_in = expected_alpha * sold_fraction * avg_price
    return raw - expected_tao_in


# ── EWMA / EWSTD ──────────────────────────────────────────────────

def _ewma_ewstd(values: list[float], halflife_samples: int) -> tuple[float, float]:
    """Exponentially-weighted mean and std for a series.

    ``halflife_samples`` is the number of samples after which a weight decays
    to half. For 5-min cadence and a 24h halflife, that's 288 samples.
    Returns (0, 0) on empty input.
    """
    if not values:
        return 0.0, 0.0
    if halflife_samples <= 0:
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1)
        return mean, math.sqrt(var)

    alpha = 1.0 - math.exp(math.log(0.5) / halflife_samples)
    mean = values[0]
    var = 0.0
    for v in values[1:]:
        diff = v - mean
        mean += alpha * diff
        var = (1.0 - alpha) * (var + alpha * diff * diff)
    return mean, math.sqrt(var)


def compute_flow_zscore(
    snapshots: list[dict],
    window_snaps: int,
    baseline_snaps: int,
    sold_fraction: float = 0.60,
) -> tuple[float | None, float, float, float] | None:
    """Return (z, flow_pct_now, ewma_flow_pct, ewstd_flow_pct) for the most
    recent window.

    Compares the current windowed adj_flow as a % of pool depth to its
    rolling baseline of per-sample values. EWMA halflife is half the
    baseline window (tracks recent regime without over-reacting to the
    latest spike).
    """
    if len(snapshots) < baseline_snaps + window_snaps + 1:
        return None

    flow_pct_series: list[float] = []
    for i in range(baseline_snaps):
        end_idx = len(snapshots) - baseline_snaps + i + 1
        sub = snapshots[:end_idx]
        if len(sub) <= window_snaps:
            continue
        adj = emission_adjusted_flow(sub, window_snaps, sold_fraction)
        tao_prev = float(sub[-window_snaps - 1].get("tao_in_pool", 0) or 0)
        if adj is None or tao_prev <= 0:
            continue
        flow_pct_series.append(adj / tao_prev * 100.0)

    if not flow_pct_series:
        return None

    flow_pct_now = flow_pct_series[-1]
    halflife = max(1, baseline_snaps // 2)
    mean, std = _ewma_ewstd(flow_pct_series, halflife)
    if std <= 1e-9:
        return (None, flow_pct_now, mean, std)
    return ((flow_pct_now - mean) / std, flow_pct_now, mean, std)


# ── Regime filter ─────────────────────────────────────────────────

def regime_index(
    per_netuid_snapshots: dict[int, list[dict]],
    lookback_snaps: int,
    top_n: int = 50,
) -> float | None:
    """Market-wide DTAO index: median ratio price[now] / price[lookback_snaps].

    ``per_netuid_snapshots`` maps netuid → list of snapshots (oldest→newest).
    Only the top_n subnets by current ``tao_in_pool`` are considered.
    Returns None if fewer than 3 subnets have usable data.
    """
    by_depth: list[tuple[float, int, list[dict]]] = []
    for netuid, snaps in per_netuid_snapshots.items():
        if netuid == 0 or len(snaps) <= lookback_snaps:
            continue
        cur = float(snaps[-1].get("tao_in_pool", 0) or 0)
        if cur > 0:
            by_depth.append((cur, netuid, snaps))
    if len(by_depth) < 3:
        return None

    by_depth.sort(reverse=True)
    ratios: list[float] = []
    for _, _, snaps in by_depth[:top_n]:
        p_now = float(snaps[-1].get("price", 0) or 0)
        p_then = float(snaps[-lookback_snaps - 1].get("price", 0) or 0)
        if p_now > 0 and p_then > 0:
            ratios.append(p_now / p_then)
    if len(ratios) < 3:
        return None

    ratios.sort()
    mid = len(ratios) // 2
    if len(ratios) % 2 == 1:
        return ratios[mid]
    return (ratios[mid - 1] + ratios[mid]) / 2.0


# ── Gap detection ─────────────────────────────────────────────────

def has_gap(
    snapshots: list[dict],
    max_gap_minutes: float,
    scan_interval_min: float,
) -> bool:
    """True if any consecutive snapshot pair is farther apart than
    ``max_gap_minutes`` (invalidates the rolling baseline).

    Relies on ISO-8601 ts strings; silently treats malformed rows as no-gap
    so that a single bad ts doesn't poison baselines.
    """
    if len(snapshots) < 2 or max_gap_minutes <= 0:
        return False
    from datetime import datetime
    prev: datetime | None = None
    for snap in snapshots:
        ts = snap.get("ts")
        if not ts:
            prev = None
            continue
        try:
            cur = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            prev = None
            continue
        if prev is not None:
            gap_min = (cur - prev).total_seconds() / 60.0
            if gap_min > max_gap_minutes:
                return True
        prev = cur
    return False


# ── Entry / exit signals ──────────────────────────────────────────

@dataclass
class FlowSignalConfig:
    z_entry: float = 2.0
    z_exit: float = -1.5
    min_tao_pct: float = 2.0
    exit_pct: float = 0.5
    magnitude_cap: float = 10.0
    window_1h_snaps: int = 12
    window_4h_snaps: int = 48
    baseline_snaps: int = 576
    cold_start_snaps: int = 624
    emission_adjust: bool = True
    sold_fraction: float = 0.60


@dataclass
class FlowEvaluation:
    signal: str           # "BUY" | "HOLD" | "BLOCKED-<reason>"
    reason: str
    z_score: float | None
    tao_delta_pct_1h: float | None
    tao_delta_pct_4h: float | None
    tao_delta_pct_12h: float | None
    alpha_delta_pct_4h: float | None
    adj_flow_4h: float | None
    ewma_flow_pct: float
    ewstd_flow_pct: float
    magnitude_capped: bool
    snapshots_collected: int


def flow_entry_signal(
    snapshots: list[dict],
    cfg: FlowSignalConfig,
    ema_fast_value: float | None = None,
    ema_slow_value: float | None = None,
    ema_confirm: bool = True,
    regime_ok: bool = True,
) -> FlowEvaluation:
    """Evaluate a BUY signal against the v2 rule set.

    ``snapshots`` is oldest→newest. ``ema_fast_value`` / ``ema_slow_value`` are
    computed externally (on whatever TF the caller uses) — if ``ema_confirm``
    is True we require fast > slow.
    """
    sold = cfg.sold_fraction if cfg.emission_adjust else 0.0
    count = len(snapshots)

    # Cold start — need baseline + window
    if count < cfg.cold_start_snaps:
        return FlowEvaluation(
            signal="BLOCKED-cold_start",
            reason=f"need {cfg.cold_start_snaps} snaps, have {count}",
            z_score=None,
            tao_delta_pct_1h=None,
            tao_delta_pct_4h=None,
            tao_delta_pct_12h=None,
            alpha_delta_pct_4h=None,
            adj_flow_4h=None,
            ewma_flow_pct=0.0,
            ewstd_flow_pct=0.0,
            magnitude_capped=False,
            snapshots_collected=count,
        )

    # Windowed percentage deltas (tao + alpha)
    tao_pct_1h = compute_flow_delta_pct(snapshots, cfg.window_1h_snaps)
    tao_pct_4h = compute_flow_delta_pct(snapshots, cfg.window_4h_snaps)
    tao_pct_12h = compute_flow_delta_pct(snapshots, cfg.window_4h_snaps * 3)
    alpha_pct_4h = compute_flow_delta_pct(
        snapshots, cfg.window_4h_snaps, field="alpha_in_pool"
    )

    # Magnitude cap: compare single-snapshot delta to prevent whale spikes
    single = compute_flow_delta_pct(snapshots, 1)
    magnitude_capped = single is not None and abs(single) > cfg.magnitude_cap

    # Adj flow + z
    zinfo = compute_flow_zscore(
        snapshots, cfg.window_4h_snaps, cfg.baseline_snaps, sold_fraction=sold
    )
    adj_flow = emission_adjusted_flow(snapshots, cfg.window_4h_snaps, sold)

    if zinfo is None:
        return FlowEvaluation(
            signal="BLOCKED-insufficient_baseline",
            reason="z-score undefined",
            z_score=None,
            tao_delta_pct_1h=tao_pct_1h,
            tao_delta_pct_4h=tao_pct_4h,
            tao_delta_pct_12h=tao_pct_12h,
            alpha_delta_pct_4h=alpha_pct_4h,
            adj_flow_4h=adj_flow,
            ewma_flow_pct=0.0,
            ewstd_flow_pct=0.0,
            magnitude_capped=magnitude_capped,
            snapshots_collected=count,
        )

    z, _flow_pct_now, ewma, ewstd = zinfo

    base = FlowEvaluation(
        signal="HOLD",
        reason="",
        z_score=z,
        tao_delta_pct_1h=tao_pct_1h,
        tao_delta_pct_4h=tao_pct_4h,
        tao_delta_pct_12h=tao_pct_12h,
        alpha_delta_pct_4h=alpha_pct_4h,
        adj_flow_4h=adj_flow,
        ewma_flow_pct=ewma,
        ewstd_flow_pct=ewstd,
        magnitude_capped=magnitude_capped,
        snapshots_collected=count,
    )

    if magnitude_capped:
        base.signal = "BLOCKED-magnitude_cap"
        base.reason = f"single-snap delta {single:.1f}% > {cfg.magnitude_cap}%"
        return base

    if not regime_ok:
        base.signal = "BLOCKED-regime"
        base.reason = "market-wide drawdown"
        return base

    checks = [
        (z is not None and z >= cfg.z_entry, f"z {z}"),
        (tao_pct_4h is not None and tao_pct_4h >= cfg.min_tao_pct,
         f"tao_pct_4h {tao_pct_4h}"),
        (alpha_pct_4h is not None and alpha_pct_4h <= -cfg.min_tao_pct / 2.0,
         f"alpha_pct_4h {alpha_pct_4h}"),
        (tao_pct_1h is not None and tao_pct_1h > 0,
         f"tao_pct_1h {tao_pct_1h}"),
    ]
    for passed, label in checks:
        if not passed:
            base.signal = "HOLD"
            base.reason = f"fail: {label}"
            return base

    if ema_confirm and ema_fast_value is not None and ema_slow_value is not None:
        if not (ema_fast_value > ema_slow_value):
            base.signal = "HOLD"
            base.reason = "ema trend"
            return base

    base.signal = "BUY"
    base.reason = "all gates passed"
    return base


def flow_exit_signal(
    snapshots: list[dict],
    cfg: FlowSignalConfig,
    consecutive_outflow_cycles: int,
    regime_ok: bool = True,
) -> str | None:
    """Return an exit reason, or None to hold.

    ``consecutive_outflow_cycles`` is tracked by the caller — this function
    only reports whether *this* cycle saw outflow below -exit_pct over 1h.
    """
    if len(snapshots) <= cfg.window_4h_snaps:
        return None

    sold = cfg.sold_fraction if cfg.emission_adjust else 0.0
    zinfo = compute_flow_zscore(
        snapshots, cfg.window_4h_snaps, cfg.baseline_snaps, sold_fraction=sold
    )
    tao_pct_1h = compute_flow_delta_pct(snapshots, cfg.window_1h_snaps)

    if zinfo is not None:
        z_eff = cfg.z_exit if regime_ok else max(cfg.z_exit, -1.0)
        z, _, _, _ = zinfo
        if z is not None and z <= z_eff:
            return "FLOW_Z_EXIT"

    if (
        tao_pct_1h is not None
        and tao_pct_1h < -cfg.exit_pct
        and consecutive_outflow_cycles >= 2
    ):
        return "FLOW_REVERSAL"

    if not regime_ok:
        return "REGIME_EXIT"

    return None


# ── Legacy ring-buffer flow delta (for EMA strategy compatibility) ──

def compute_ring_flow_delta(
    cur_snap: dict,
    prev_snap: dict,
    field: str = "total_tao",
    scale: float = 1e9,
) -> float | None:
    """Compute percentage flow between two raw Taostats snapshots.

    Kept as a drop-in replacement for ``EmaManager._compute_flow_delta`` so the
    EMA flow-reversal exit continues to work unchanged.
    """
    cur = float(cur_snap.get(field, 0) or 0) / scale
    prev = float(prev_snap.get(field, 0) or 0) / scale
    if prev <= 0 or cur <= 0:
        return None
    return (cur - prev) / prev * 100.0
