"""
Taostats API client – fetches subnet metadata, prices, and historical data.
Implements caching + rate-limiting.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.config import settings
from app.logging.logger import logger


@dataclass
class CacheEntry:
    data: Any
    fetched_at: float  # epoch seconds

    def is_stale(self, ttl: float) -> bool:
        return (time.time() - self.fetched_at) > ttl


class TaostatsClient:
    """Async Taostats API wrapper with cache and rate limiter."""

    def __init__(self) -> None:
        self._base = settings.TAOSTATS_BASE_URL.rstrip("/")
        self._api_key = settings.TAOSTATS_API_KEY
        self._ttl = settings.TAOSTATS_CACHE_TTL_SEC
        self._rate_limit = settings.TAOSTATS_RATE_LIMIT_PER_MIN
        self._cache: dict[str, CacheEntry] = {}
        self._call_times: list[float] = []
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        # Per-cycle pool snapshot keyed by netuid, populated by get_subnets/get_alpha_prices
        self._pool_snapshot: dict[int, dict] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers: dict[str, str] = {
                "Accept": "application/json",
            }
            if self._api_key:
                headers["Authorization"] = self._api_key
            self._client = httpx.AsyncClient(
                base_url=self._base,
                headers=headers,
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Rate limiting ──────────────────────────────────────────────
    async def _wait_for_slot(self) -> None:
        async with self._lock:
            now = time.time()
            window_start = now - 60.0
            self._call_times = [t for t in self._call_times if t > window_start]
            if len(self._call_times) >= self._rate_limit:
                sleep_for = 60.0 - (now - self._call_times[0]) + 0.1
                logger.debug(f"Taostats rate-limit: sleeping {sleep_for:.1f}s")
                await asyncio.sleep(sleep_for)
            self._call_times.append(time.time())

    # ── Generic GET with cache ─────────────────────────────────────
    async def _get(self, path: str, params: dict | None = None) -> Any:
        cache_key = f"{path}|{params}"
        cached = self._cache.get(cache_key)
        if cached and not cached.is_stale(self._ttl):
            return cached.data

        await self._wait_for_slot()
        client = await self._get_client()

        for attempt in range(3):
            try:
                resp = await client.get(path, params=params)
                resp.raise_for_status()
                data = resp.json()
                self._cache[cache_key] = CacheEntry(data=data, fetched_at=time.time())
                return data
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Taostats 429, backing off {wait}s")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Taostats HTTP error: {e}")
                raise
            except httpx.RequestError as e:
                logger.error(f"Taostats request error: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                raise

        raise RuntimeError(f"Taostats: failed after retries for {path}")

    # ── Public API methods ─────────────────────────────────────────

    async def get_subnets(self) -> list[dict]:
        """Fetch all subnets metadata with current pool data."""
        data = await self._get("/api/dtao/pool/latest/v1", params={"limit": 200})
        items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        # Populate pool snapshot for use by get_price_history
        self._pool_snapshot = {int(s["netuid"]): s for s in items if "netuid" in s}
        return items

    async def get_subnet_info(self, netuid: int) -> dict:
        """Fetch pool info for a single subnet."""
        if netuid in self._pool_snapshot:
            return self._pool_snapshot[netuid]
        data = await self._get("/api/dtao/pool/latest/v1", params={"netuid": netuid, "limit": 1})
        if isinstance(data, dict):
            items = data.get("data", [])
            if items:
                return items[0]
        return {}

    async def get_alpha_prices(self) -> dict[int, float]:
        """
        Fetch alpha token prices for all subnets in one call.
        Populates the pool snapshot so get_price_history needs no extra API calls.
        Returns {netuid: price_in_tao}.
        """
        data = await self._get("/api/dtao/pool/latest/v1", params={"limit": 200})
        items = data.get("data", []) if isinstance(data, dict) else []

        # Cache full records for get_price_history to reuse
        self._pool_snapshot = {int(s["netuid"]): s for s in items if "netuid" in s}

        prices: dict[int, float] = {}
        for subnet in items:
            try:
                netuid = int(subnet.get("netuid", -1))
                price = float(subnet.get("price", 0) or 0)
                if netuid >= 1 and price > 0:
                    prices[netuid] = price
            except (ValueError, TypeError):
                continue
        return prices

    async def get_price_history(
        self, netuid: int, limit: int = 200
    ) -> list[dict]:
        """
        Return price history for a subnet.
        Uses seven_day_prices embedded in the pool snapshot (no extra API call).
        Falls back to the pool/history endpoint only if snapshot is missing.
        """
        if netuid in self._pool_snapshot:
            seven_day = self._pool_snapshot[netuid].get("seven_day_prices", [])
            if seven_day:
                return seven_day[-limit:]

        # Fallback: fetch from history endpoint
        try:
            data = await self._get(
                "/api/dtao/pool/history/v1",
                params={"netuid": netuid, "limit": limit},
            )
            if isinstance(data, dict):
                data = data.get("data", [])
            return data if isinstance(data, list) else []
        except Exception:
            logger.warning(f"No price history available for netuid {netuid}")
            return []

    async def get_subnet_metrics(self, netuid: int) -> dict:
        """Fetch subnet pool metrics."""
        try:
            data = await self._get(
                "/api/dtao/pool/latest/v1",
                params={"netuid": netuid, "limit": 1},
            )
            if isinstance(data, dict):
                items = data.get("data", [])
                return items[0] if items else {}
            return {}
        except Exception:
            return {}

    def invalidate_cache(self) -> None:
        """Clear all cached data."""
        self._cache.clear()
