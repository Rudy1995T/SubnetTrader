"""
FlameWire RPC client – HTTP JSON-RPC + WebSocket subscription support.
Health checks, timeouts, retries, reconnect logic.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Optional

import httpx

try:
    import websockets
    import websockets.client as ws_client

    HAS_WS = True
except ImportError:
    HAS_WS = False

from app.config import settings
from app.logging.logger import logger


class FlameWireRPC:
    """
    JSON-RPC client for the FlameWire Bittensor gateway.
    Supports HTTP calls + optional WebSocket subscriptions.
    """

    def __init__(self) -> None:
        self._http_url = settings.flamewire_http_url
        self._ws_url = settings.flamewire_ws_url
        self._timeout = settings.FLAMEWIRE_TIMEOUT
        self._retries = settings.FLAMEWIRE_RETRIES
        self._retry_delay = settings.FLAMEWIRE_RETRY_DELAY
        self._ws_ping_interval = settings.FLAMEWIRE_WS_PING_INTERVAL
        self._ws_reconnect_delay = settings.FLAMEWIRE_WS_RECONNECT_DELAY
        self._request_id = 0
        self._http_client: httpx.AsyncClient | None = None
        self._ws_connection: Any = None
        self._ws_running = False
        self._healthy = False
        self._last_health_check: float = 0

    # ── HTTP Client management ─────────────────────────────────────

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
            )
        return self._http_client

    async def close(self) -> None:
        """Shutdown all connections."""
        self._ws_running = False
        if self._ws_connection is not None:
            try:
                await self._ws_connection.close()
            except Exception:
                pass
            self._ws_connection = None
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    # ── JSON-RPC call ──────────────────────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def call(self, method: str, params: list | dict | None = None) -> Any:
        """
        Make a JSON-RPC 2.0 call over HTTP with retries.
        Returns the 'result' field or raises on error.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or [],
        }

        client = await self._get_http_client()
        last_error: Exception | None = None

        for attempt in range(1, self._retries + 1):
            try:
                resp = await client.post(
                    self._http_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                body = resp.json()

                if "error" in body and body["error"]:
                    err = body["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"RPC error: {msg}")

                return body.get("result")

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(
                    f"FlameWire HTTP {e.response.status_code} (attempt {attempt}/{self._retries})"
                )
                if attempt < self._retries:
                    await asyncio.sleep(self._retry_delay * attempt)
            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    f"FlameWire request error: {e} (attempt {attempt}/{self._retries})"
                )
                if attempt < self._retries:
                    await asyncio.sleep(self._retry_delay * attempt)
            except RuntimeError:
                raise
            except Exception as e:
                last_error = e
                logger.error(f"FlameWire unexpected error: {e}")
                if attempt < self._retries:
                    await asyncio.sleep(self._retry_delay * attempt)

        raise RuntimeError(
            f"FlameWire: all {self._retries} attempts failed – {last_error}"
        )

    # ── Health check ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Ping the node via system_health RPC call."""
        try:
            result = await self.call("system_health")
            self._healthy = result is not None
            self._last_health_check = time.time()
            logger.debug("FlameWire health check OK", data={"result": result})
            return self._healthy
        except Exception as e:
            self._healthy = False
            logger.error(f"FlameWire health check failed: {e}")
            return False

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    # ── Chain queries ──────────────────────────────────────────────

    async def get_block_hash(self, block_number: int | None = None) -> str:
        """Get block hash. None = latest."""
        params = [block_number] if block_number is not None else []
        return await self.call("chain_getBlockHash", params)

    async def get_finalized_head(self) -> str:
        return await self.call("chain_getFinalizedHead")

    async def get_runtime_version(self) -> dict:
        return await self.call("state_getRuntimeVersion")

    async def get_storage(self, key: str, block_hash: str | None = None) -> Any:
        params = [key]
        if block_hash:
            params.append(block_hash)
        return await self.call("state_getStorage", params)

    async def submit_extrinsic(self, extrinsic_hex: str) -> str:
        """Submit a signed extrinsic and return the tx hash."""
        return await self.call("author_submitExtrinsic", [extrinsic_hex])

    async def get_nonce(self, address: str) -> int:
        """Get account nonce for transaction signing."""
        result = await self.call("system_accountNextIndex", [address])
        return int(result)

    async def get_balance(self, address: str) -> int:
        """Get free balance in RAO (1 TAO = 1e9 RAO)."""
        result = await self.call(
            "state_call",
            ["AccountNonceApi_account_nonce", address],
        )
        # Fallback: query system account
        try:
            account = await self.call("system_account", [address])
            if isinstance(account, dict):
                data = account.get("data", {})
                return int(data.get("free", 0))
        except Exception:
            pass
        return 0

    # ── WebSocket subscription ─────────────────────────────────────

    async def subscribe_new_heads(
        self, callback: Callable[[dict], Any]
    ) -> None:
        """
        Subscribe to new block headers via WebSocket.
        Reconnects automatically on failure.
        """
        if not HAS_WS:
            logger.warning("websockets not installed; WS subscriptions disabled")
            return

        self._ws_running = True

        while self._ws_running:
            try:
                logger.info(f"Connecting WebSocket to {self._ws_url}")
                async with ws_client.connect(
                    self._ws_url,
                    ping_interval=self._ws_ping_interval,
                    ping_timeout=self._timeout,
                    close_timeout=10,
                ) as ws:
                    self._ws_connection = ws
                    # Send subscription request
                    sub_payload = {
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": "chain_subscribeNewHeads",
                        "params": [],
                    }
                    await ws.send(json.dumps(sub_payload))

                    async for raw_msg in ws:
                        if not self._ws_running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            if "params" in msg and "result" in msg["params"]:
                                header = msg["params"]["result"]
                                await callback(header)
                        except json.JSONDecodeError:
                            logger.warning("WS: invalid JSON received")
                        except Exception as e:
                            logger.error(f"WS callback error: {e}")

            except Exception as e:
                if self._ws_running:
                    logger.warning(
                        f"WS disconnected: {e}. Reconnecting in {self._ws_reconnect_delay}s"
                    )
                    await asyncio.sleep(self._ws_reconnect_delay)
                else:
                    break

        self._ws_connection = None
        logger.info("WS subscription stopped")

    def stop_ws(self) -> None:
        """Signal the WebSocket loop to stop."""
        self._ws_running = False
