"""
Fetch and cache historical pool snapshots for the Flow Momentum backtest.

The EMA loader caches candle-shaped rows and keeps only the *latest* pool
depth. Flow needs the raw snapshot per timestep so z-scores and per-timestep
slippage reconstruct exactly. Different schema → separate cache directory.

Usage:
    python -m app.backtest.flow_data_loader                # fetch qualifying subnets
    python -m app.backtest.flow_data_loader --interval 4h  # override probe
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings

from .data_loader import _rate_limited_get, fetch_qualifying_netuids
from .probe_flow_history import load_probe

FLOW_HISTORY_DIR = Path("data/backtest/history/flow")
FLOW_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

CACHE_MAX_AGE_SEC = 24 * 3600
PAGE_LIMIT = 200
# Subnet-level data-quality gate: if > this fraction of expected snapshot slots
# are missing, the series is too sparse for flow baselines and the subnet is
# dropped from the backtest input.
MAX_GAP_FRACTION = 0.10


def _cache_path(netuid: int) -> Path:
    return FLOW_HISTORY_DIR / f"sn{netuid}.json"


def _load_cache(netuid: int) -> tuple[list[dict], float] | None:
    path = _cache_path(netuid)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            blob = json.load(f)
        return blob.get("snapshots", []), blob.get("fetched_at", 0.0)
    except Exception:
        return None


def _save_cache(
    netuid: int,
    snapshots: list[dict],
    interval: str,
    gap_fraction: float | None = None,
) -> None:
    blob = {
        "fetched_at": time.time(),
        "netuid": netuid,
        "interval": interval,
        "gap_fraction": gap_fraction,
        "snapshots": snapshots,
    }
    with open(_cache_path(netuid), "w") as f:
        json.dump(blob, f, default=str)


def compute_gap_fraction(
    snapshots: list[dict],
    interval_seconds: int,
) -> float:
    """Fraction of the expected per-cadence slots that are missing.

    Expected slot count = span_seconds / interval_seconds. Missing fraction is
    ``1 - actual/expected``, clamped to [0, 1]. Returns 0.0 for series that
    are too short to judge.
    """
    if len(snapshots) < 2 or interval_seconds <= 0:
        return 0.0
    first = _parse_ts(snapshots[0].get("ts"))
    last = _parse_ts(snapshots[-1].get("ts"))
    if first is None or last is None:
        return 0.0
    span = (last - first).total_seconds()
    if span <= 0:
        return 0.0
    expected = span / interval_seconds + 1
    if expected <= 1:
        return 0.0
    missing = max(0.0, 1.0 - len(snapshots) / expected)
    return min(1.0, missing)


def _parse_ts(raw: str | int | float | None) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, ValueError):
            return None
    s = str(raw)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def normalize_snapshot(row: dict) -> dict | None:
    """Convert a raw Taostats pool-history row into the snapshot schema that
    ``app.strategy.flow_signals`` consumes.

    Returns None on rows with unusable numeric fields (so the caller can
    silently drop them). Amounts from Taostats are in RAO; we divide by 1e9
    to get TAO tokens — flow_signals only cares about *ratios*, but keeping
    the same scale as live `pool_snapshots` rows makes per-timestep slippage
    math use the same units as the EMA engine.
    """
    ts_raw = row.get("timestamp") or row.get("ts") or row.get("time")
    dt = _parse_ts(ts_raw)
    if dt is None:
        return None

    def _num(key: str) -> float | None:
        v = row.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    tao_raw = _num("tao_in_pool")
    if tao_raw is None:
        tao_raw = _num("total_tao")
    alpha_raw = _num("alpha_in_pool")
    if tao_raw is None or alpha_raw is None:
        return None

    tao_tokens = tao_raw / 1e9
    alpha_tokens = alpha_raw / 1e9
    price = _num("price")
    if price is None and alpha_tokens > 0:
        price = tao_tokens / alpha_tokens

    return {
        "ts": dt.isoformat(),
        "tao_in_pool": tao_tokens,
        "alpha_in_pool": alpha_tokens,
        "price": price if price is not None else 0.0,
        "block_number": int(row["block_number"]) if row.get("block_number") else None,
        "alpha_emission_rate": row.get("alpha_emission_rate"),
    }


async def fetch_subnet_history(
    client: httpx.AsyncClient,
    netuid: int,
    interval: str,
    window_days: int,
    call_times: list[float],
    rate_limit: int,
) -> list[dict]:
    """Paginate /api/dtao/pool/history/v1 backwards until the window is
    covered. Returns snapshots oldest→newest with duplicates removed.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - window_days * 86400
    seen_ts: set[str] = set()
    collected: list[dict] = []
    cursor: str | None = None

    while True:
        params: dict = {
            "netuid": netuid,
            "interval": interval,
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["timestamp_end"] = cursor

        resp = await _rate_limited_get(
            client,
            "/api/dtao/pool/history/v1",
            params,
            call_times,
            rate_limit,
        )
        if not resp:
            break

        rows = resp.get("data", []) if isinstance(resp, dict) else resp
        if not isinstance(rows, list) or not rows:
            break

        page_snaps: list[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            norm = normalize_snapshot(r)
            if norm is None:
                continue
            if norm["ts"] in seen_ts:
                continue
            seen_ts.add(norm["ts"])
            page_snaps.append(norm)

        if not page_snaps:
            break

        page_snaps.sort(key=lambda s: s["ts"])
        collected.extend(page_snaps)

        page_oldest_ts = page_snaps[0]["ts"]
        page_oldest_dt = _parse_ts(page_oldest_ts)

        # Stop if we've paged past the window, or endpoint returned a short page.
        if page_oldest_dt and page_oldest_dt.timestamp() < cutoff:
            break
        if len(rows) < PAGE_LIMIT:
            break
        if cursor == page_oldest_ts:
            break
        cursor = page_oldest_ts

    collected.sort(key=lambda s: s["ts"])
    # Trim to the requested window.
    keep: list[dict] = []
    for s in collected:
        dt = _parse_ts(s["ts"])
        if dt is None or dt.timestamp() >= cutoff:
            keep.append(s)
    return keep


INTERVAL_SECONDS_MAP = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


async def fetch_all_flow_history(
    window_days: int,
    interval: str | None = None,
    min_depth: float | None = None,
    force_refresh: bool = False,
    max_gap_fraction: float = MAX_GAP_FRACTION,
) -> dict[int, list[dict]]:
    probe = load_probe() or {}
    interval = interval or probe.get("finest_interval") or "1h"
    interval_seconds = INTERVAL_SECONDS_MAP.get(interval, 3600)
    if min_depth is None:
        min_depth = settings.FLOW_MIN_POOL_DEPTH_TAO

    rate_limit = settings.TAOSTATS_RATE_LIMIT_PER_MIN
    call_times: list[float] = []

    headers: dict[str, str] = {"Accept": "application/json"}
    if settings.TAOSTATS_API_KEY:
        headers["Authorization"] = settings.TAOSTATS_API_KEY

    all_history: dict[int, list[dict]] = {}

    async with httpx.AsyncClient(
        base_url=settings.TAOSTATS_BASE_URL.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(30.0),
    ) as client:
        print(f"Fetching qualifying subnets (min depth {min_depth} TAO)...")
        qualifying = await fetch_qualifying_netuids(
            client, call_times, rate_limit, min_depth
        )
        print(f"  {len(qualifying)} subnets")

        for i, (netuid, _) in enumerate(qualifying):
            if not force_refresh:
                cached = _load_cache(netuid)
                if cached:
                    snaps, fetched_at = cached
                    age = time.time() - fetched_at
                    if age < CACHE_MAX_AGE_SEC and snaps:
                        gap = compute_gap_fraction(snaps, interval_seconds)
                        if gap > max_gap_fraction:
                            print(
                                f"  [{i+1}/{len(qualifying)}] SN{netuid}: "
                                f"cached but gap={gap:.1%} > "
                                f"{max_gap_fraction:.0%} (excluded)"
                            )
                            continue
                        print(
                            f"  [{i+1}/{len(qualifying)}] SN{netuid}: "
                            f"cached ({len(snaps)} snaps, "
                            f"{age/3600:.1f}h old, gap={gap:.1%})"
                        )
                        all_history[netuid] = snaps
                        continue

            print(
                f"  [{i+1}/{len(qualifying)}] SN{netuid}: fetching @ {interval}...",
                end="",
                flush=True,
            )
            snaps = await fetch_subnet_history(
                client, netuid, interval, window_days, call_times, rate_limit
            )
            if snaps:
                gap = compute_gap_fraction(snaps, interval_seconds)
                _save_cache(netuid, snaps, interval, gap_fraction=gap)
                if gap > max_gap_fraction:
                    print(
                        f" {len(snaps)} snaps (gap={gap:.1%} > "
                        f"{max_gap_fraction:.0%}, excluded)"
                    )
                else:
                    all_history[netuid] = snaps
                    print(f" {len(snaps)} snaps (gap={gap:.1%})")
            else:
                print(" no data")

    return all_history


def load_cached_flow_history(
    max_gap_fraction: float = MAX_GAP_FRACTION,
    interval_seconds: int | None = None,
) -> dict[int, list[dict]]:
    """Load all cached flow snapshots from disk (no API calls).

    Subnets whose cached gap_fraction (or recomputed gap, when the cache was
    written by an older version that didn't persist it) exceeds
    ``max_gap_fraction`` are silently dropped so stale/sparse series never
    enter the backtest.
    """
    result: dict[int, list[dict]] = {}
    for path in sorted(FLOW_HISTORY_DIR.glob("sn*.json")):
        try:
            with open(path) as f:
                blob = json.load(f)
        except Exception:
            continue
        netuid = blob.get("netuid")
        snaps = blob.get("snapshots", [])
        if netuid is None or not snaps:
            continue
        gap = blob.get("gap_fraction")
        if gap is None and interval_seconds:
            gap = compute_gap_fraction(snaps, interval_seconds)
        if gap is not None and gap > max_gap_fraction:
            continue
        result[int(netuid)] = snaps
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.backtest.flow_data_loader")
    p.add_argument("--window-days", type=int, default=120)
    p.add_argument("--interval", type=str, default=None)
    p.add_argument("--min-depth", type=float, default=None)
    p.add_argument("--force-refresh", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print("=== Flow History Loader ===")
    history = asyncio.run(
        fetch_all_flow_history(
            window_days=args.window_days,
            interval=args.interval,
            min_depth=args.min_depth,
            force_refresh=args.force_refresh,
        )
    )
    total = sum(len(v) for v in history.values())
    print(f"\nDone: {len(history)} subnets, {total:,} snapshots")


if __name__ == "__main__":
    main()
