"""
Fetch and cache historical price data from Taostats for backtesting.

Usage:
    python -m app.backtest.data_loader          # fetch all qualifying subnets
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings
from app.strategy.ema_signals import Candle, build_candles_from_history

HISTORY_DIR = Path("data/backtest/history")
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# 150 days of hourly data
MAX_LIMIT = 3600
MIN_POOL_DEPTH_TAO = 3000.0
CACHE_MAX_AGE_SEC = 24 * 3600  # reuse if < 24h old


def _cache_path(netuid: int) -> Path:
    return HISTORY_DIR / f"sn{netuid}.json"


def _load_cache(netuid: int) -> tuple[list[dict], float] | None:
    """Load cached history. Returns (data, fetched_at) or None."""
    path = _cache_path(netuid)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            blob = json.load(f)
        fetched_at = blob.get("fetched_at", 0)
        data = blob.get("data", [])
        return data, fetched_at
    except Exception:
        return None


def _save_cache(netuid: int, data: list[dict]) -> None:
    blob = {"fetched_at": time.time(), "netuid": netuid, "data": data}
    with open(_cache_path(netuid), "w") as f:
        json.dump(blob, f)


async def _rate_limited_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict,
    call_times: list[float],
    rate_limit: int,
) -> dict | list | None:
    """GET with sliding-window rate limiting and retries."""
    now = time.time()
    window_start = now - 60.0
    call_times[:] = [t for t in call_times if t > window_start]
    if len(call_times) >= rate_limit:
        sleep_for = 60.0 - (now - call_times[0]) + 0.2
        await asyncio.sleep(sleep_for)
    call_times.append(time.time())

    for attempt in range(3):
        try:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"  [rate-limit] 429 from Taostats, backing off {wait}s")
                await asyncio.sleep(wait)
                continue
            print(f"  [error] HTTP {e.response.status_code} for {path}")
            return None
        except httpx.RequestError as e:
            print(f"  [error] Request failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
                continue
            return None
    return None


async def fetch_qualifying_netuids(
    client: httpx.AsyncClient,
    call_times: list[float],
    rate_limit: int,
    min_depth: float = MIN_POOL_DEPTH_TAO,
) -> list[tuple[int, dict]]:
    """Fetch pool snapshots and return netuids with sufficient depth."""
    data = await _rate_limited_get(
        client, "/api/dtao/pool/latest/v1", {"limit": 200}, call_times, rate_limit
    )
    if not data:
        return []
    if isinstance(data, dict):
        data = data.get("data", data.get("pools", []))
    if not isinstance(data, list):
        return []

    qualifying = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        netuid = entry.get("netuid")
        total_tao_raw = entry.get("total_tao") or entry.get("tao_in_pool") or 0
        try:
            total_tao = float(total_tao_raw) / 1e9  # rao → TAO
        except (ValueError, TypeError):
            continue
        if netuid is not None and total_tao >= min_depth:
            qualifying.append((int(netuid), entry))
    qualifying.sort(key=lambda x: x[0])
    return qualifying


async def fetch_subnet_history(
    client: httpx.AsyncClient,
    netuid: int,
    call_times: list[float],
    rate_limit: int,
    limit: int = MAX_LIMIT,
    interval: str = "1h",
) -> list[dict]:
    """Fetch hourly history for a single subnet."""
    data = await _rate_limited_get(
        client,
        "/api/dtao/pool/history/v1",
        {"netuid": netuid, "interval": interval, "limit": limit},
        call_times,
        rate_limit,
    )
    if not data:
        return []
    if isinstance(data, dict):
        data = data.get("data", [])
    return data if isinstance(data, list) else []


async def fetch_all(
    min_depth: float = MIN_POOL_DEPTH_TAO,
    force_refresh: bool = False,
) -> dict[int, list[dict]]:
    """
    Fetch 150-day hourly history for all qualifying subnets.

    Returns {netuid: [history_points]}.
    """
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
        print("Fetching pool snapshots...")
        qualifying = await fetch_qualifying_netuids(
            client, call_times, rate_limit, min_depth
        )
        print(f"Found {len(qualifying)} subnets with pool depth >= {min_depth} TAO")

        # Save pool snapshots for slippage modeling
        pool_snapshots = {}
        for netuid, entry in qualifying:
            pool_snapshots[netuid] = entry

        pool_path = HISTORY_DIR / "pool_snapshots.json"
        with open(pool_path, "w") as f:
            json.dump(
                {"fetched_at": time.time(), "pools": pool_snapshots},
                f,
                default=str,
            )

        all_history: dict[int, list[dict]] = {}

        for i, (netuid, _) in enumerate(qualifying):
            # Check cache
            if not force_refresh:
                cached = _load_cache(netuid)
                if cached:
                    data, fetched_at = cached
                    age = time.time() - fetched_at
                    if age < CACHE_MAX_AGE_SEC and len(data) > 100:
                        print(
                            f"  [{i+1}/{len(qualifying)}] SN{netuid}: "
                            f"cached ({len(data)} points, {age/3600:.1f}h old)"
                        )
                        all_history[netuid] = data
                        continue

            print(
                f"  [{i+1}/{len(qualifying)}] SN{netuid}: fetching...",
                end="",
                flush=True,
            )
            data = await fetch_subnet_history(
                client, netuid, call_times, rate_limit
            )
            if data:
                _save_cache(netuid, data)
                all_history[netuid] = data
                print(f" {len(data)} points")
            else:
                print(" no data")

            # 1s sleep between requests to stay under ceiling
            await asyncio.sleep(1.0)

    return all_history


def load_cached_history() -> dict[int, list[dict]]:
    """Load all cached history from disk (no API calls)."""
    result = {}
    for path in sorted(HISTORY_DIR.glob("sn*.json")):
        try:
            with open(path) as f:
                blob = json.load(f)
            netuid = blob.get("netuid")
            data = blob.get("data", [])
            if netuid is not None and data:
                result[int(netuid)] = data
        except Exception:
            continue
    return result


def load_pool_snapshots() -> dict[int, dict]:
    """Load saved pool snapshots for slippage modeling."""
    path = HISTORY_DIR / "pool_snapshots.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            blob = json.load(f)
        pools = blob.get("pools", {})
        return {int(k): v for k, v in pools.items()}
    except Exception:
        return {}


def build_candles_multi_tf(
    history: list[dict],
    timeframes: list[int] | None = None,
) -> dict[int, list[Candle]]:
    """Build candles at multiple timeframes from hourly history.

    Returns {timeframe_hours: [candles]}.
    """
    if timeframes is None:
        timeframes = [1, 2, 4, 8, 24]
    result = {}
    for tf in timeframes:
        candles = build_candles_from_history(history, candle_hours=tf)
        if candles:
            result[tf] = candles
    return result


if __name__ == "__main__":
    print("=== Backtest Data Fetcher ===")
    history = asyncio.run(fetch_all())
    total_points = sum(len(v) for v in history.values())
    print(f"\nDone: {len(history)} subnets, {total_points:,} total data points")
