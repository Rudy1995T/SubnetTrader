"""
Taostats API client – fetches subnet metadata, prices, and historical data.
Implements caching + rate-limiting.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

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

    _HISTORY_CACHE_MAX = 50  # LRU eviction threshold

    def __init__(self) -> None:
        self._base = settings.TAOSTATS_BASE_URL.rstrip("/")
        self._api_key = settings.TAOSTATS_API_KEY
        self._ttl = settings.TAOSTATS_CACHE_TTL_SEC
        self._rate_limit = settings.TAOSTATS_RATE_LIMIT_PER_MIN
        self._cache: dict[str, CacheEntry] = {}
        self._call_times: list[float] = []
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        # Per-cycle pool snapshot keyed by netuid, populated by get_alpha_prices.
        self._pool_snapshot: dict[int, dict] = {}
        # Previous cycle's snapshot for pool delta detection (Gini force-refresh).
        self._prev_pool_snapshot: dict[int, dict] = {}
        # Per-subnet history cache: netuid → (fetched_at, data)
        self._history_cache: dict[int, tuple[float, list[dict]]] = {}
        self._history_ttl = settings.SUBNET_HISTORY_CACHE_TTL_SEC
        # Asyncio event signalled when pool snapshot refreshes (for SSE price feed).
        self._price_updated: asyncio.Event = asyncio.Event()
        # Store singleton reference so executor can access pool depth.
        TaostatsClient._instance = self

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

    async def get_alpha_prices(
        self, include_raw: bool = False
    ) -> "dict[int, float] | tuple[dict[int, float], dict[int, list]]":
        """
        Fetch alpha token prices for all subnets in one call.
        Populates the pool snapshot so get_price_history needs no extra API calls.

        Args:
            include_raw: If True, also return {netuid: seven_day_prices} as second element.

        Returns:
            {netuid: price_in_tao}, or ({netuid: price_in_tao}, {netuid: seven_day_prices})
            if include_raw=True.
        """
        data = await self._get("/api/dtao/pool/latest/v1", params={"limit": 200})
        items = data.get("data", []) if isinstance(data, dict) else []

        # Rotate snapshot: save previous for pool delta detection
        self._prev_pool_snapshot = self._pool_snapshot.copy()
        # Cache full records for get_price_history to reuse
        self._pool_snapshot = {int(s["netuid"]): s for s in items if "netuid" in s}

        # Signal SSE price feed that snapshot has been refreshed.
        self._price_updated.set()

        prices: dict[int, float] = {}
        for subnet in items:
            try:
                netuid = int(subnet.get("netuid", -1))
                price = float(subnet.get("price", 0) or 0)
                if netuid >= 1 and price > 0:
                    prices[netuid] = price
            except (ValueError, TypeError):
                continue

        if include_raw:
            raw_prices: dict[int, list] = {
                int(s["netuid"]): s.get("seven_day_prices", [])
                for s in items
                if "netuid" in s
            }
            return prices, raw_prices

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

    async def get_subnet_history(
        self,
        netuid: int,
        interval: str = "1h",
        limit: int = 336,
    ) -> list[dict]:
        """Fetch per-subnet price history from /api/dtao/pool/history/v1.

        Returns list of dicts with at least {timestamp, price}.
        Results are cached per netuid with independent TTL.
        Falls back to seven_day_prices if the history endpoint fails.
        """
        now = time.time()
        cached = self._history_cache.get(netuid)
        if cached is not None:
            ts, data = cached
            if (now - ts) < self._history_ttl:
                return data

        try:
            resp = await self._get(
                "/api/dtao/pool/history/v1",
                params={"netuid": netuid, "interval": interval, "limit": limit},
            )
            # Normalise: API may wrap in {"data": [...]}
            if isinstance(resp, dict):
                resp = resp.get("data", [])
            if not isinstance(resp, list):
                resp = []

            # Validate each entry has at least a price
            validated: list[dict] = []
            for entry in resp:
                if isinstance(entry, dict) and entry.get("price") is not None:
                    validated.append(entry)

            if validated:
                self._history_cache[netuid] = (now, validated)
                self._evict_history_cache()
                return validated

            logger.warning(
                f"Subnet history empty/invalid for SN{netuid}, falling back to seven_day_prices"
            )
        except Exception as exc:
            logger.warning(f"Subnet history fetch failed for SN{netuid}: {exc}")

        # Graceful fallback to seven_day_prices from pool snapshot
        if netuid in self._pool_snapshot:
            seven_day = self._pool_snapshot[netuid].get("seven_day_prices", [])
            if seven_day:
                return seven_day[-limit:]
        return []

    async def get_fresh_pool(self, netuid: int) -> dict | None:
        """Fetch the latest pool reserves for a single subnet.

        Bypasses the bulk snapshot cache to give a just-in-time read.
        Returns the same dict shape as _pool_snapshot[netuid] or None on failure.
        """
        try:
            # Try single-subnet filter first; fall back to bulk if API ignores it.
            await self._wait_for_slot()
            client = await self._get_client()
            resp = await client.get(
                "/api/dtao/pool/latest/v1",
                params={"netuid": netuid, "limit": 1},
            )
            if resp.status_code != 200:
                logger.warning(f"get_fresh_pool SN{netuid}: HTTP {resp.status_code}")
                return None
            data = resp.json()
            records = data.get("data") or data.get("subnets") or []

            # Search through all returned records for the target subnet.
            # API may return just the one subnet or all 200 — handle both.
            for rec in records:
                if int(rec.get("netuid", -1)) == netuid:
                    return rec

            logger.debug(f"get_fresh_pool SN{netuid}: subnet not found in {len(records)} records")
            return None
        except Exception as exc:
            logger.warning(f"get_fresh_pool SN{netuid} failed: {exc}")
            return None

    # ── Pool reserve helpers (Pool Flow Momentum) ───────────────

    def pool_reserves(self, netuid: int) -> dict | None:
        """Return a thin slice of the current pool snapshot for a subnet
        (TAO/alpha reserves, price, block, emission rate). Returns None if no
        snapshot has been captured yet.
        """
        snap = self._pool_snapshot.get(netuid)
        if not snap:
            return None

        def _rao(field: str) -> float:
            try:
                return float(snap.get(field, 0) or 0) / 1e9
            except (TypeError, ValueError):
                return 0.0

        block = snap.get("block_number") or snap.get("block")
        try:
            block_int = int(block) if block is not None else None
        except (TypeError, ValueError):
            block_int = None

        # Taostats may expose emission_rate at a few paths; try common ones.
        emission = (
            snap.get("alpha_emission_rate")
            or snap.get("emission")
            or snap.get("emission_rate")
        )
        try:
            emission_f = float(emission) if emission is not None else None
        except (TypeError, ValueError):
            emission_f = None

        return {
            "netuid": int(netuid),
            "block_number": block_int,
            "tao_in_pool": _rao("total_tao"),
            "alpha_in_pool": _rao("alpha_in_pool"),
            "price": float(snap.get("price", 0) or 0),
            "alpha_emission_rate": emission_f,
        }

    def all_pool_reserves(self) -> list[dict]:
        """Return ``pool_reserves`` for every subnet currently in the
        cached snapshot. Useful for bulk persistence.
        """
        out: list[dict] = []
        for netuid in self._pool_snapshot:
            reserves = self.pool_reserves(netuid)
            if reserves is not None and reserves["tao_in_pool"] > 0:
                out.append(reserves)
        return out

    # ── Gini support methods ─────────────────────────────────────

    def pool_concentration_alert(
        self, netuid: int, current_snap: dict, threshold: float = 0.15
    ) -> bool:
        """Return True if pool reserves shifted enough to warrant a Gini refresh."""
        prev = self._prev_pool_snapshot.get(netuid)
        if not prev:
            return False
        prev_alpha = float(prev.get("alpha_in_pool", 0))
        curr_alpha = float(current_snap.get("alpha_in_pool", 0))
        if prev_alpha == 0:
            return False
        delta_pct = abs(curr_alpha - prev_alpha) / prev_alpha
        return delta_pct > threshold

    async def get_stake_distribution(self, netuid: int) -> list[float] | None:
        """Fetch top staker balances from Taostats for Gini computation.

        Returns a list of positive stake values, or None if the endpoint
        is unavailable or returns no data.
        """
        try:
            data = await self._get(
                "/api/dtao/stake/latest/v1",
                params={"netuid": netuid, "limit": 100},
            )
            items = data.get("data", []) if isinstance(data, dict) else []
            stakes = [
                float(r.get("stake", 0))
                for r in items
                if float(r.get("stake", 0)) > 0
            ]
            return stakes if stakes else None
        except Exception as e:
            logger.debug(f"Taostats stake distribution unavailable for SN{netuid}: {e}")
            return None

    def _evict_history_cache(self) -> None:
        """Evict oldest entries when cache exceeds max size."""
        if len(self._history_cache) <= self._HISTORY_CACHE_MAX:
            return
        # Sort by fetched_at ascending, drop oldest
        by_age = sorted(self._history_cache.items(), key=lambda kv: kv[1][0])
        to_drop = len(self._history_cache) - self._HISTORY_CACHE_MAX
        for netuid, _ in by_age[:to_drop]:
            del self._history_cache[netuid]
