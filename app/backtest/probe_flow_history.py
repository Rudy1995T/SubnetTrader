"""
Data-availability probe for Pool Flow Momentum backtest.

Answers three questions against Taostats before any fetch plan:
  1. Which `interval` values does /api/dtao/pool/history/v1 accept?
  2. How far back does history go at the finest supported interval?
  3. Is `alpha_emission_rate` present in historical rows?

**2026-04-21 finding:** Taostats silently collapses every requested
``interval`` to ``1d``. Direct curls for ``5m``/``15m``/``1h``/``4h`` return
the same timestamps as ``1d``. This matches the root cause documented in
``specs/fix-deep-history-resolution-mismatch.md`` and is treated as the base
case, not the tail. Each interval's first few raw timestamps are now recorded
under ``raw_samples`` so future operators can re-verify with a single file
read.

Writes the findings to ``data/backtest/history/flow_probe.json``. Downstream
scripts (flow_data_loader, flow_engine) read that file to pick the interval
and window length rather than hard-coding them.

Usage:
    python -m app.backtest.probe_flow_history
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings

from .data_loader import _rate_limited_get

HISTORY_DIR = Path("data/backtest/history")
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
PROBE_PATH = HISTORY_DIR / "flow_probe.json"

CANDIDATE_INTERVALS = ["5m", "15m", "1h", "4h", "1d"]
# Order from finest → coarsest so the first supported interval we see is the
# finest. Must match _interval_seconds rank.
INTERVAL_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

PROBE_NETUID = 1  # SN1 is always populated
MAX_BACK_PAGES = 30  # ceiling for back-pagination probe (≈6000 rows)


def _parse_ts(raw: str | int | float) -> datetime | None:
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


def _extract_rows(resp: dict | list | None) -> list[dict]:
    if not resp:
        return []
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        rows = resp.get("data", [])
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def _row_ts(row: dict) -> datetime | None:
    for key in ("timestamp", "ts", "time", "created_at", "block_timestamp"):
        if key in row:
            dt = _parse_ts(row[key])
            if dt is not None:
                return dt
    return None


def _observed_interval_seconds(rows: list[dict]) -> float | None:
    """Median gap between consecutive timestamps in seconds."""
    stamps = [ts for ts in (_row_ts(r) for r in rows) if ts is not None]
    stamps.sort()
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(stamps, stamps[1:])
        if (b - a).total_seconds() > 0
    ]
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


async def probe_interval(
    client: httpx.AsyncClient,
    interval: str,
    call_times: list[float],
    rate_limit: int,
) -> dict:
    """Issue one request for the given interval; report observed cadence."""
    resp = await _rate_limited_get(
        client,
        "/api/dtao/pool/history/v1",
        {"netuid": PROBE_NETUID, "interval": interval, "limit": 10},
        call_times,
        rate_limit,
    )
    rows = _extract_rows(resp)
    observed = _observed_interval_seconds(rows) if rows else None
    requested = INTERVAL_SECONDS[interval]
    # Treat as supported when we get ≥ 2 rows AND the observed cadence is
    # within 25% of the requested one (upstream sometimes silently downgrades
    # fine-grained intervals to daily).
    supported = False
    if len(rows) >= 2 and observed is not None:
        ratio = observed / requested
        supported = 0.75 <= ratio <= 1.25
    # Keep the first 5 timestamps raw so cross-interval comparisons are
    # auditable without re-running the probe.
    raw_samples = []
    for r in rows[:5]:
        raw_samples.append(
            {
                "timestamp": r.get("timestamp") or r.get("ts"),
                "block_number": r.get("block_number"),
                "price": r.get("price"),
            }
        )
    return {
        "interval": interval,
        "rows_returned": len(rows),
        "observed_seconds": observed,
        "requested_seconds": requested,
        "supported": supported,
        "sample": rows[0] if rows else None,
        "raw_samples": raw_samples,
    }


async def probe_history_depth(
    client: httpx.AsyncClient,
    interval: str,
    call_times: list[float],
    rate_limit: int,
) -> tuple[datetime | None, datetime | None, int]:
    """Page backwards until the endpoint stops returning rows.

    Returns (oldest_ts, newest_ts, total_rows_seen).
    """
    oldest: datetime | None = None
    newest: datetime | None = None
    total = 0
    ts_cursor: str | None = None

    for _ in range(MAX_BACK_PAGES):
        params: dict = {
            "netuid": PROBE_NETUID,
            "interval": interval,
            "limit": 200,
        }
        if ts_cursor:
            params["timestamp_end"] = ts_cursor
        resp = await _rate_limited_get(
            client,
            "/api/dtao/pool/history/v1",
            params,
            call_times,
            rate_limit,
        )
        rows = _extract_rows(resp)
        if not rows:
            break

        page_stamps = [ts for ts in (_row_ts(r) for r in rows) if ts is not None]
        if not page_stamps:
            break
        page_stamps.sort()
        page_oldest = page_stamps[0]
        page_newest = page_stamps[-1]

        total += len(rows)
        if newest is None or page_newest > newest:
            newest = page_newest
        if oldest is None or page_oldest < oldest:
            oldest = page_oldest

        # Advance cursor to just before the oldest row on this page.
        next_cursor = page_oldest.isoformat()
        if next_cursor == ts_cursor:
            break
        ts_cursor = next_cursor

        if len(rows) < 200:
            break

    return oldest, newest, total


async def run_probe() -> dict:
    rate_limit = settings.TAOSTATS_RATE_LIMIT_PER_MIN
    call_times: list[float] = []

    headers: dict[str, str] = {"Accept": "application/json"}
    if settings.TAOSTATS_API_KEY:
        headers["Authorization"] = settings.TAOSTATS_API_KEY

    async with httpx.AsyncClient(
        base_url=settings.TAOSTATS_BASE_URL.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(30.0),
    ) as client:
        per_interval: list[dict] = []
        for interval in CANDIDATE_INTERVALS:
            print(f"  probing interval={interval} ...", end="", flush=True)
            info = await probe_interval(client, interval, call_times, rate_limit)
            print(
                f" rows={info['rows_returned']} "
                f"observed={info['observed_seconds']}s "
                f"supported={info['supported']}"
            )
            per_interval.append(info)

        supported = [i for i in per_interval if i["supported"]]
        supported.sort(key=lambda d: d["requested_seconds"])
        intervals_supported = [i["interval"] for i in supported]
        finest = intervals_supported[0] if intervals_supported else None

        sample_record: dict | None = None
        emission_rate_present = False
        max_history_days: float | None = None

        if finest:
            print(f"  paging back at finest={finest} ...")
            oldest, newest, total = await probe_history_depth(
                client, finest, call_times, rate_limit
            )
            if oldest and newest:
                max_history_days = (newest - oldest).total_seconds() / 86400.0
            print(
                f"    oldest={oldest} newest={newest} "
                f"rows_seen={total} span_days={max_history_days}"
            )

            # Use the last-page oldest row to inspect fields (historical schema
            # may differ from latest schema).
            resp = await _rate_limited_get(
                client,
                "/api/dtao/pool/history/v1",
                {
                    "netuid": PROBE_NETUID,
                    "interval": finest,
                    "limit": 1,
                    **(
                        {"timestamp_end": oldest.isoformat()}
                        if oldest
                        else {}
                    ),
                },
                call_times,
                rate_limit,
            )
            rows = _extract_rows(resp)
            if rows:
                sample_record = rows[0]
                rate_val = sample_record.get("alpha_emission_rate")
                emission_rate_present = rate_val is not None and float(
                    rate_val or 0
                ) > 0

    # Cross-interval cadence check. The *first* timestamp drifts by seconds
    # between API calls ("now"), so we compare the *second* timestamp — the
    # first historical bar. If every interval pins row 2 to the same
    # daily-boundary timestamp, Taostats is ignoring the parameter entirely.
    second_ts_by_interval = {
        i["interval"]: (i["raw_samples"][1]["timestamp"] if len(i.get("raw_samples") or []) > 1 else None)
        for i in per_interval
    }
    non_daily = [
        v for k, v in second_ts_by_interval.items() if k != "1d" and v is not None
    ]
    cadence_collapsed_to_1d = (
        len(non_daily) >= 2 and len(set(non_daily)) == 1
    )

    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "probe_netuid": PROBE_NETUID,
        "per_interval": per_interval,
        "intervals_supported": intervals_supported,
        "finest_interval": finest,
        "max_history_days_per_netuid": (
            round(max_history_days, 1) if max_history_days is not None else None
        ),
        "emission_rate_present": emission_rate_present,
        "sample_record": sample_record,
        "second_ts_by_interval": second_ts_by_interval,
        "cadence_collapsed_to_1d": cadence_collapsed_to_1d,
        "note": (
            "Taostats /api/dtao/pool/history/v1 ignores the `interval` "
            "parameter and returns daily rows. Only `1d` is honest. See "
            "specs/fix-deep-history-resolution-mismatch.md."
            if cadence_collapsed_to_1d
            else None
        ),
    }


def load_probe() -> dict | None:
    if not PROBE_PATH.exists():
        return None
    try:
        with open(PROBE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def main() -> None:
    print("=== Flow History Probe ===")
    t0 = time.time()
    result = asyncio.run(run_probe())
    elapsed = time.time() - t0

    with open(PROBE_PATH, "w") as f:
        json.dump(result, f, indent=2, default=str)

    print(f"\n  finest interval:       {result['finest_interval']}")
    print(f"  intervals supported:   {result['intervals_supported']}")
    print(f"  max history days:      {result['max_history_days_per_netuid']}")
    print(f"  emission_rate present: {result['emission_rate_present']}")
    if result.get("cadence_collapsed_to_1d"):
        print(
            "  [!] cadence collapse detected — Taostats ignores `interval`. "
            "Treat flow metrics as 1d-cadence lower bounds."
        )
    print(f"  elapsed:               {elapsed:.1f}s")
    print(f"  wrote {PROBE_PATH}")


if __name__ == "__main__":
    main()
