"""
Regime labeller for historical backtests.

Walks the cached flow history (data/backtest/history/flow/) through a coarse
time grid, computes the aggregate regime metrics at each grid point using
the same helpers the live RegimeFilter uses, applies the same debounce
rule, and emits a sorted list of ``(ts, regime)`` labels plus an O(log n)
lookup for arbitrary timestamps.

Designed so the backtest labelled series matches what live would have seen
at each historical timestamp.
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.strategy.regime import (
    DEAD,
    compute_regime_metrics,
)

from .flow_data_loader import load_cached_flow_history

TIMELINE_PATH = Path("data/backtest/regime_timeline.json")
UNKNOWN = "UNKNOWN"


def _parse_iso(raw: str) -> datetime:
    s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class RegimeTimeline:
    """Sorted list of (ts_epoch, regime) plus binary-search lookup.

    ``ts`` is seconds since epoch so ``bisect`` works on a scalar key.
    """
    epochs: list[float]
    regimes: list[str]

    @classmethod
    def empty(cls) -> "RegimeTimeline":
        return cls(epochs=[], regimes=[])

    def regime_at(self, ts_iso: str) -> str:
        """Return the regime in force at ``ts_iso``.

        Returns the most recent debounced label with a timestamp ``<= ts_iso``.
        If ``ts_iso`` is before the first label (pre-warmup), returns
        ``UNKNOWN`` so the caller can exclude the trade from the matrix.
        """
        if not self.epochs:
            return UNKNOWN
        try:
            target = _parse_iso(ts_iso).timestamp()
        except (ValueError, TypeError):
            return UNKNOWN
        idx = bisect.bisect_right(self.epochs, target) - 1
        if idx < 0:
            return UNKNOWN
        return self.regimes[idx]

    def to_json(self, resolved: "_LabellerConfig | None" = None) -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "settings": {
                "REGIME_BUCKET_HOURS": settings.REGIME_BUCKET_HOURS,
                "REGIME_VOL_WINDOW_HOURS": settings.REGIME_VOL_WINDOW_HOURS,
                "REGIME_DEBOUNCE_CYCLES": settings.REGIME_DEBOUNCE_CYCLES,
                "REGIME_VOL_DEAD_THRESHOLD": settings.REGIME_VOL_DEAD_THRESHOLD,
                "REGIME_VOL_CHOP_FLOOR": settings.REGIME_VOL_CHOP_FLOOR,
                "REGIME_VOL_TREND_THRESHOLD": settings.REGIME_VOL_TREND_THRESHOLD,
                "REGIME_DIR_THRESHOLD": settings.REGIME_DIR_THRESHOLD,
                "REGIME_DISP_THRESHOLD": settings.REGIME_DISP_THRESHOLD,
                "REGIME_MIN_SUBNETS": settings.REGIME_MIN_SUBNETS,
                "REGIME_MIN_BUCKETS": settings.REGIME_MIN_BUCKETS,
            },
            "resolved": None if resolved is None else {
                "bucket_hours": resolved.bucket_hours,
                "window_hours": resolved.window_hours,
                "source": resolved.source,
            },
            "labels": [
                {"ts": datetime.fromtimestamp(e, tz=timezone.utc).isoformat(), "regime": r}
                for e, r in zip(self.epochs, self.regimes)
            ],
        }

    @classmethod
    def from_json(cls, blob: dict) -> "RegimeTimeline":
        labels = blob.get("labels", []) or []
        epochs: list[float] = []
        regimes: list[str] = []
        for row in labels:
            try:
                epochs.append(_parse_iso(row["ts"]).timestamp())
                regimes.append(str(row["regime"]))
            except (KeyError, ValueError, TypeError):
                continue
        return cls(epochs=epochs, regimes=regimes)


def _slice_snapshots_up_to(
    snaps: list[dict],
    cutoff_epoch: float,
    lookback_seconds: float,
) -> list[dict]:
    """Return snapshots in [cutoff-lookback, cutoff], assuming ``snaps``
    is pre-sorted by ``ts`` ascending.
    """
    lo = cutoff_epoch - lookback_seconds
    out: list[dict] = []
    for s in snaps:
        ts_raw = s.get("ts")
        if not ts_raw:
            continue
        try:
            t = _parse_iso(str(ts_raw)).timestamp()
        except (ValueError, TypeError):
            continue
        if t > cutoff_epoch:
            break
        if t >= lo:
            out.append(s)
    return out


def _detect_cadence_hours(history: dict[int, list[dict]]) -> float:
    """Median inter-sample gap in hours across all subnets, rounded to a sane
    bin. Defaults to 24 if the data is too sparse to infer.
    """
    gaps: list[float] = []
    for snaps in history.values():
        if len(snaps) < 2:
            continue
        prev_t: float | None = None
        for s in snaps[:20]:  # sample first few per subnet
            try:
                t = _parse_iso(str(s.get("ts"))).timestamp()
            except (ValueError, TypeError):
                continue
            if prev_t is not None:
                gap_h = (t - prev_t) / 3600.0
                if gap_h > 0:
                    gaps.append(gap_h)
            prev_t = t
    if not gaps:
        return 24.0
    gaps.sort()
    median = gaps[len(gaps) // 2]
    for std in (1.0, 4.0, 12.0, 24.0):
        if median <= std * 1.5:
            return std
    return 24.0


class _LabellerConfig:
    """Resolved window/bucket settings the labeller actually ran with.

    When the cached history is sparser than the live 5-min cadence, we
    scale up the vol window and bucket so `REGIME_MIN_BUCKETS` is
    achievable — otherwise every slice would classify as DEAD.
    """
    def __init__(self, bucket_hours: int, window_hours: int, source: str):
        self.bucket_hours = bucket_hours
        self.window_hours = window_hours
        self.source = source


def _resolve_labeller_config(
    history: dict[int, list[dict]],
    grid_hours: int | None,
    window_hours: int | None,
) -> _LabellerConfig:
    cadence_h = _detect_cadence_hours(history)
    live_bucket = max(1, int(settings.REGIME_BUCKET_HOURS))
    live_window = max(1, int(settings.REGIME_VOL_WINDOW_HOURS))
    min_buckets = max(2, int(settings.REGIME_MIN_BUCKETS))

    if grid_hours is not None and window_hours is not None:
        return _LabellerConfig(
            bucket_hours=int(grid_hours),
            window_hours=int(window_hours),
            source="explicit-override",
        )

    # If cadence >= bucket, the live config can't fill buckets. Auto-scale:
    # bucket = cadence (one sample per bucket), window = bucket × min_buckets × 2
    if cadence_h >= live_bucket:
        new_bucket = int(max(live_bucket, cadence_h))
        new_window = int(max(live_window, new_bucket * min_buckets * 2))
        return _LabellerConfig(
            bucket_hours=new_bucket,
            window_hours=new_window,
            source=f"auto-scale(cadence={cadence_h:g}h)",
        )

    return _LabellerConfig(
        bucket_hours=live_bucket,
        window_hours=live_window,
        source="live-defaults",
    )


def build_regime_timeline(
    flow_history: dict[int, list[dict]] | None = None,
    grid_hours: int | None = None,
    window_hours: int | None = None,
) -> tuple[RegimeTimeline, _LabellerConfig]:
    """Walk the flow-history timeline on a coarse grid and classify each slot.

    Returns ``(timeline, resolved_config)`` so the caller can log/persist
    which window/bucket were used.
    """
    if flow_history is None:
        flow_history = load_cached_flow_history()
    if not flow_history:
        return RegimeTimeline.empty(), _LabellerConfig(0, 0, "empty-history")

    # Pre-sort each subnet's snapshots by ts ascending (they already are,
    # but the flow cache is not guaranteed to be).
    sorted_history: dict[int, list[dict]] = {}
    all_epochs: list[float] = []
    for netuid, snaps in flow_history.items():
        clean: list[tuple[float, dict]] = []
        for s in snaps:
            try:
                t = _parse_iso(str(s.get("ts"))).timestamp()
            except (ValueError, TypeError):
                continue
            clean.append((t, s))
        clean.sort(key=lambda x: x[0])
        if clean:
            sorted_history[netuid] = [row for _, row in clean]
            all_epochs.extend(t for t, _ in clean)

    if not all_epochs:
        return RegimeTimeline.empty(), _LabellerConfig(0, 0, "empty-history")

    first_epoch = min(all_epochs)
    last_epoch = max(all_epochs)

    resolved = _resolve_labeller_config(sorted_history, grid_hours, window_hours)
    debounce = max(1, int(settings.REGIME_DEBOUNCE_CYCLES))
    grid_seconds = resolved.bucket_hours * 3600
    lookback_seconds = resolved.window_hours * 3600

    # Align first grid tick to the next bucket boundary after we have at
    # least one window's worth of data.
    warmup_epoch = first_epoch + lookback_seconds
    tick = (int(warmup_epoch // grid_seconds) + 1) * grid_seconds

    current_regime = DEAD
    pending_regime: str | None = None
    pending_count = 0

    epochs: list[float] = []
    regimes: list[str] = []
    last_emitted: str | None = None

    # Shim settings for compute_regime_metrics so our resolved bucket wins
    # without mutating the real settings object.
    class _S:
        pass
    shim = _S()
    for attr in (
        "REGIME_VOL_DEAD_THRESHOLD", "REGIME_VOL_CHOP_FLOOR",
        "REGIME_VOL_TREND_THRESHOLD", "REGIME_DIR_THRESHOLD",
        "REGIME_DISP_THRESHOLD", "REGIME_MIN_SUBNETS", "REGIME_MIN_BUCKETS",
    ):
        setattr(shim, attr, getattr(settings, attr))
    shim.REGIME_BUCKET_HOURS = resolved.bucket_hours
    shim.REGIME_VOL_WINDOW_HOURS = resolved.window_hours

    while tick <= last_epoch:
        sliced: dict[int, list[dict]] = {}
        for netuid, snaps in sorted_history.items():
            sl = _slice_snapshots_up_to(snaps, tick, lookback_seconds)
            if sl:
                sliced[netuid] = sl

        metrics = compute_regime_metrics(sliced, shim)
        raw = metrics["raw_regime"]

        if raw == current_regime:
            pending_regime = None
            pending_count = 0
        elif raw == pending_regime:
            pending_count += 1
            if pending_count >= debounce:
                current_regime = raw
                pending_regime = None
                pending_count = 0
        else:
            pending_regime = raw
            pending_count = 1

        # Only emit on change to keep the file small; regime_at() returns the
        # most recent entry <= target anyway.
        if current_regime != last_emitted:
            epochs.append(tick)
            regimes.append(current_regime)
            last_emitted = current_regime

        tick += grid_seconds

    return RegimeTimeline(epochs=epochs, regimes=regimes), resolved


def save_timeline(
    timeline: RegimeTimeline,
    path: Path = TIMELINE_PATH,
    resolved: "_LabellerConfig | None" = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(timeline.to_json(resolved), f, indent=2)
    return path


def load_timeline(path: Path = TIMELINE_PATH) -> RegimeTimeline | None:
    if not path.exists():
        return None
    try:
        with open(path) as f:
            blob = json.load(f)
        return RegimeTimeline.from_json(blob)
    except Exception:
        return None


def regime_distribution(timeline: RegimeTimeline) -> dict[str, float]:
    """Fraction of wall-clock time spent in each regime (for reporting).

    Each segment is weighted by its duration, ending at the next label or
    at the final tick for the terminal segment.
    """
    if not timeline.epochs:
        return {}
    total = 0.0
    per: dict[str, float] = {}
    for i, reg in enumerate(timeline.regimes):
        start = timeline.epochs[i]
        end = timeline.epochs[i + 1] if i + 1 < len(timeline.epochs) else start
        dur = max(0.0, end - start)
        per[reg] = per.get(reg, 0.0) + dur
        total += dur
    if total <= 0:
        return {k: 0.0 for k in per}
    return {k: v / total * 100.0 for k, v in per.items()}
