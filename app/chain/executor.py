"""
Trading execution layer – swap simulation & execution using Bittensor SDK.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.config import settings
from app.logging.logger import logger
from app.utils.time import utc_iso

# RAO per TAO
RAO_PER_TAO = 1_000_000_000


@dataclass
class SwapQuote:
    origin_netuid: int
    destination_netuid: int
    amount_tao: float
    amount_rao: int
    expected_out_rao: int
    expected_out_tao: float
    fee_rao: int
    fee_tao: float
    slippage_estimate_pct: float
    timestamp: str


@dataclass
class SwapResult:
    success: bool
    tx_hash: str
    origin_netuid: int
    destination_netuid: int
    amount_tao: float
    received_tao: float
    fee_tao: float
    slippage_pct: float
    error: str
    timestamp: str
    received_alpha: float = 0.0  # actual alpha received for entries (from ExtrinsicResponse.data)


def tao_to_rao(tao: float) -> int:
    return int(tao * RAO_PER_TAO)


def rao_to_tao(rao: int) -> float:
    return rao / RAO_PER_TAO


class SwapExecutor:
    """
    Handles quoting and executing swaps between TAO (netuid=0) and
    subnet alpha tokens via the Bittensor Swap pallet.
    """

    def __init__(self) -> None:
        self._wallet = None
        self._substrate = None
        self._validator_hotkey_cache: dict[int, str] = {}  # netuid → hotkey_ss58
        # Serializes all substrate WebSocket calls — substrate is not safe for concurrent recv
        self._substrate_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """
        Initialize the Bittensor wallet and substrate connection.
        """
        if settings.EMA_DRY_RUN:
            logger.info(
                "SwapExecutor initialized (DRY_RUN - wallet and substrate skipped)",
                data={"wallet": settings.BT_WALLET_NAME, "hotkey": settings.BT_WALLET_HOTKEY},
            )
            return

        try:
            from bittensor_wallet import Wallet

            wallet = Wallet(
                name=settings.BT_WALLET_NAME,
                hotkey=settings.BT_WALLET_HOTKEY,
                path=settings.BT_WALLET_PATH,
            )
            self._wallet = wallet

            logger.info(
                "SwapExecutor initialized (LIVE wallet ready; substrate deferred)",
                data={
                    "wallet": settings.BT_WALLET_NAME,
                    "hotkey": settings.BT_WALLET_HOTKEY,
                    "rpc": settings.SUBTENSOR_NETWORK,
                },
            )
        except ImportError:
            logger.warning(
                "bittensor SDK not available – using RPC-only mode (quotes may be estimated)"
            )
        except Exception as e:
            logger.error(f"Failed to initialize bittensor wallet: {e}")

    async def _ensure_substrate(self) -> None:
        """Lazily connect to Subtensor when a live chain action requires it."""
        if self._substrate is not None:
            return
        if self._wallet is None:
            raise RuntimeError("Bittensor wallet not initialized")

        import bittensor as bt

        url = settings.SUBTENSOR_NETWORK
        try:
            sub = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: bt.Subtensor(network=u),
                ),
                timeout=30,
            )
            # Verify the connection is actually alive with a quick balance query
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: sub.get_balance(self._wallet.coldkey.ss58_address),
                ),
                timeout=15,
            )
            self._substrate = sub
            logger.info(
                "Substrate connection established",
                data={"rpc": url},
            )
        except Exception as e:
            raise RuntimeError(f"Subtensor connection failed ({url}): {e}")

    async def _reconnect_substrate(self) -> None:
        """Force a fresh Subtensor connection (e.g. after a dead-socket error)."""
        logger.warning("Forcing Subtensor reconnect")
        self._substrate = None
        self._validator_hotkey_cache.clear()
        await self._ensure_substrate()

    def _get_validator_hotkey_sync(self, netuid: int) -> str:
        """
        Find the best validator hotkey on a subnet.
        Priority order:
          1. Preferred validators (config) — tried in order; first one registered wins
          2. Highest total_stake validator (fallback)
        Result is cached in-process to avoid repeated metagraph calls.
        """
        if netuid in self._validator_hotkey_cache:
            return self._validator_hotkey_cache[netuid]
        mg = self._substrate.metagraph(netuid=netuid)
        if not mg.neurons:
            raise RuntimeError(f"SN{netuid}: no neurons in metagraph")

        registered = {n.hotkey for n in mg.neurons}

        # Try preferred validators in priority order
        for preferred_hk in settings.PREFERRED_VALIDATORS:
            if preferred_hk in registered:
                self._validator_hotkey_cache[netuid] = preferred_hk
                logger.info(f"Validator hotkey cached for SN{netuid}: {preferred_hk} (preferred)")
                return preferred_hk

        # Fall back to highest-stake validator
        best = max(mg.neurons, key=lambda n: float(getattr(n, "total_stake", 0) or 0))
        hk = best.hotkey
        self._validator_hotkey_cache[netuid] = hk
        logger.info(f"Validator hotkey cached for SN{netuid}: {hk} (top-stake fallback)")
        return hk

    async def get_validator_hotkey(self, netuid: int) -> str:
        """Async wrapper around _get_validator_hotkey_sync."""
        if netuid in self._validator_hotkey_cache:
            return self._validator_hotkey_cache[netuid]
        if self._substrate is None:
            await self._ensure_substrate()
        return await asyncio.get_running_loop().run_in_executor(
            None, self._get_validator_hotkey_sync, netuid
        )

    async def quote_swap(
        self,
        origin_netuid: int,
        destination_netuid: int,
        amount_tao: float,
    ) -> SwapQuote:
        """
        Get a swap quote: how much alpha/TAO you'd receive.
        origin_netuid=0 means TAO -> alpha (buying alpha).
        destination_netuid=0 means alpha -> TAO (selling alpha).
        """
        amount_rao = tao_to_rao(amount_tao)
        expected_out_rao = 0
        fee_rao = 0
        slippage = 0.0

        if self._substrate is not None:
            try:
                # Use SDK simulation — hold lock to prevent concurrent substrate recv
                async with self._substrate_lock:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self._sim_swap_sdk,
                        origin_netuid,
                        destination_netuid,
                        amount_rao,
                    )
                expected_out_rao = result.get("expected_out", 0)
                fee_rao = result.get("fee", 0)
            except Exception as e:
                logger.warning(f"SDK sim_swap failed, using estimate: {e}")
                expected_out_rao, fee_rao = self._estimate_swap(
                    origin_netuid, destination_netuid, amount_rao
                )
        else:
            # Estimate based on constant-product AMM model
            expected_out_rao, fee_rao = self._estimate_swap(
                origin_netuid, destination_netuid, amount_rao
            )

        expected_out_tao = rao_to_tao(expected_out_rao)
        fee_tao = rao_to_tao(fee_rao)

        # Estimate slippage as (input - output - fee) / input * 100
        if amount_rao > 0 and expected_out_rao > 0:
            ideal = amount_rao - fee_rao  # no-slippage scenario
            if ideal > 0:
                slippage = max(0.0, (1.0 - expected_out_rao / ideal) * 100.0)

        return SwapQuote(
            origin_netuid=origin_netuid,
            destination_netuid=destination_netuid,
            amount_tao=amount_tao,
            amount_rao=amount_rao,
            expected_out_rao=expected_out_rao,
            expected_out_tao=expected_out_tao,
            fee_rao=fee_rao,
            fee_tao=fee_tao,
            slippage_estimate_pct=slippage,
            timestamp=utc_iso(),
        )

    def _sim_swap_sdk(
        self,
        origin_netuid: int,
        destination_netuid: int,
        amount_rao: int,
    ) -> dict:
        """
        Run Bittensor SDK swap simulation (synchronous, run in executor).
        Uses the subtensor's sim_swap if available, otherwise falls back to
        direct pallet query.
        """
        try:
            # Try subtensor sim_swap method (bt v10: no wallet param, amount=Balance)
            if hasattr(self._substrate, "sim_swap"):
                import bittensor as _bt
                result = self._substrate.sim_swap(
                    origin_netuid=origin_netuid,
                    destination_netuid=destination_netuid,
                    amount=_bt.Balance.from_rao(amount_rao),
                )
                # Result is SimSwapResult object — extract fields
                expected = getattr(result, "expected_amount", None) or getattr(result, "amount_out", 0)
                fee = getattr(result, "fee", 0)
                if isinstance(result, dict):
                    expected = result.get("expected_amount", result.get("amount_out", 0))
                    fee = result.get("fee", 0)
                # Balance objects store rao in .rao attribute; int() may return TAO int
                def _to_rao(v) -> int:
                    if hasattr(v, "rao"):
                        return int(v.rao)
                    return int(v)
                return {
                    "expected_out": _to_rao(expected),
                    "fee": _to_rao(fee),
                }

            # Alternative: query SubtensorModule swap info
            if hasattr(self._substrate, "query_subtensor"):
                # Query pool reserves for constant-product calculation
                tao_reserve = self._substrate.query_subtensor(
                    "SubnetTAO", [destination_netuid if origin_netuid == 0 else origin_netuid]
                )
                alpha_reserve = self._substrate.query_subtensor(
                    "SubnetAlphaIn", [destination_netuid if origin_netuid == 0 else origin_netuid]
                )

                if tao_reserve and alpha_reserve:
                    tao_r = int(tao_reserve.value if hasattr(tao_reserve, "value") else tao_reserve)
                    alpha_r = int(
                        alpha_reserve.value if hasattr(alpha_reserve, "value") else alpha_reserve
                    )
                    return self._constant_product_swap(
                        origin_netuid, destination_netuid, amount_rao, tao_r, alpha_r
                    )

        except Exception as e:
            logger.warning(f"SDK sim_swap error: {e}")

        raise RuntimeError("SDK simulation not available")

    def _constant_product_swap(
        self,
        origin_netuid: int,
        destination_netuid: int,
        amount_rao: int,
        tao_reserve: int,
        alpha_reserve: int,
    ) -> dict:
        """
        Constant-product AMM calculation: x * y = k.
        Fee is 0.3% (typical subnet pool fee).
        """
        fee_bps = 30  # 0.3%
        fee_amount = (amount_rao * fee_bps) // 10000
        amount_after_fee = amount_rao - fee_amount

        if origin_netuid == 0:
            # TAO -> alpha: input is TAO, output is alpha
            new_tao = tao_reserve + amount_after_fee
            if new_tao == 0:
                return {"expected_out": 0, "fee": fee_amount}
            new_alpha = (tao_reserve * alpha_reserve) // new_tao
            amount_out = alpha_reserve - new_alpha
        else:
            # alpha -> TAO: input is alpha, output is TAO
            new_alpha = alpha_reserve + amount_after_fee
            if new_alpha == 0:
                return {"expected_out": 0, "fee": fee_amount}
            new_tao = (tao_reserve * alpha_reserve) // new_alpha
            amount_out = tao_reserve - new_tao

        return {"expected_out": max(0, amount_out), "fee": fee_amount}

    def _estimate_swap(
        self,
        origin_netuid: int,
        destination_netuid: int,
        amount_rao: int,
    ) -> tuple[int, int]:
        """
        Rough estimate when no SDK/reserves available.
        Assumes 0.3% fee and 1% slippage.
        """
        fee_rao = (amount_rao * 30) // 10000  # 0.3%
        slippage_rao = (amount_rao * 100) // 10000  # 1%
        expected_out = amount_rao - fee_rao - slippage_rao
        return max(0, expected_out), fee_rao

    async def execute_swap(
        self,
        origin_netuid: int,
        destination_netuid: int,
        amount_tao: float,
        max_slippage_pct: float | None = None,
        dry_run: bool | None = None,
        hotkey_ss58: str | None = None,
        alpha_in_pool_override: float | None = None,
    ) -> SwapResult:
        """
        Execute an actual swap on-chain.
        In dry-run mode, simulates without broadcasting.
        Pass dry_run=False to force live execution.
        """
        if max_slippage_pct is None:
            max_slippage_pct = settings.MAX_SLIPPAGE_PCT
        effective_dry_run = settings.EMA_DRY_RUN if dry_run is None else dry_run

        timestamp = utc_iso()
        amount_rao = tao_to_rao(amount_tao)

        # Get quote first
        quote = await self.quote_swap(origin_netuid, destination_netuid, amount_tao)

        # Check slippage
        if quote.slippage_estimate_pct > max_slippage_pct:
            return SwapResult(
                success=False,
                tx_hash="",
                origin_netuid=origin_netuid,
                destination_netuid=destination_netuid,
                amount_tao=amount_tao,
                received_tao=0.0,
                fee_tao=quote.fee_tao,
                slippage_pct=quote.slippage_estimate_pct,
                error=f"Slippage {quote.slippage_estimate_pct:.2f}% > max {max_slippage_pct:.2f}%",
                timestamp=timestamp,
            )

        # DRY RUN mode
        if effective_dry_run:
            logger.info(
                "DRY RUN swap executed",
                data={
                    "origin": origin_netuid,
                    "destination": destination_netuid,
                    "amount_tao": amount_tao,
                    "expected_out_tao": quote.expected_out_tao,
                    "fee_tao": quote.fee_tao,
                    "slippage_pct": quote.slippage_estimate_pct,
                },
            )
            return SwapResult(
                success=True,
                tx_hash="DRY_RUN_" + timestamp.replace(":", "").replace("-", ""),
                origin_netuid=origin_netuid,
                destination_netuid=destination_netuid,
                amount_tao=amount_tao,
                received_tao=quote.expected_out_tao,
                fee_tao=quote.fee_tao,
                slippage_pct=quote.slippage_estimate_pct,
                error="",
                timestamp=timestamp,
            )

        # LIVE execution
        try:
            tx_hash, alpha_received, tao_received = await self._submit_swap(
                origin_netuid, destination_netuid, amount_rao, hotkey_ss58,
                alpha_in_pool_override=alpha_in_pool_override,
            )
            # For exits (destination=0), use actual balance delta as received_tao.
            # For entries (origin=0), use quote estimate (tao_received is 0).
            actual_received_tao = tao_received if tao_received > 0 else quote.expected_out_tao
            logger.info(
                "LIVE swap submitted",
                data={
                    "tx_hash": tx_hash,
                    "origin": origin_netuid,
                    "destination": destination_netuid,
                    "amount_tao": amount_tao,
                    "alpha_received": alpha_received,
                    "tao_received": tao_received,
                },
            )
            return SwapResult(
                success=True,
                tx_hash=tx_hash,
                origin_netuid=origin_netuid,
                destination_netuid=destination_netuid,
                amount_tao=amount_tao,
                received_tao=actual_received_tao,
                fee_tao=quote.fee_tao,
                slippage_pct=quote.slippage_estimate_pct,
                error="",
                timestamp=timestamp,
                received_alpha=alpha_received,
            )
        except Exception as e:
            # No alpha staked on-chain — position was never funded or already exited externally.
            # Close it as a full loss (0 TAO recovered) so it's not stuck forever as OPEN.
            if "ZERO_ALPHA_ON_CHAIN" in str(e):
                logger.warning(
                    f"Exit SN{origin_netuid}: no alpha stake found on-chain, closing as full loss"
                )
                return SwapResult(
                    success=True,
                    tx_hash="NO_STAKE",
                    origin_netuid=origin_netuid,
                    destination_netuid=destination_netuid,
                    amount_tao=amount_tao,
                    received_tao=0.0,
                    fee_tao=0.0,
                    slippage_pct=0.0,
                    error="zero_alpha",
                    timestamp=timestamp,
                )
            # Retry once after reconnect for connection-level failures
            err_str = str(e).lower()
            is_conn_error = any(
                kw in err_str
                for kw in ("broken pipe", "connection", "closed", "timeout", "eof", "websocket")
            )
            if is_conn_error:
                logger.warning(f"Swap failed with connection error, retrying after reconnect: {e}")
                try:
                    await self._reconnect_substrate()
                    tx_hash, alpha_received, tao_received = await self._submit_swap(
                        origin_netuid, destination_netuid, tao_to_rao(amount_tao), hotkey_ss58,
                        alpha_in_pool_override=alpha_in_pool_override,
                    )
                    actual_received_tao = tao_received if tao_received > 0 else quote.expected_out_tao
                    return SwapResult(
                        success=True,
                        tx_hash=tx_hash,
                        origin_netuid=origin_netuid,
                        destination_netuid=destination_netuid,
                        amount_tao=amount_tao,
                        received_tao=actual_received_tao,
                        fee_tao=quote.fee_tao,
                        slippage_pct=quote.slippage_estimate_pct,
                        error="",
                        timestamp=timestamp,
                        received_alpha=alpha_received,
                    )
                except Exception as retry_e:
                    logger.error(f"Swap retry after reconnect also failed: {retry_e}")
                    e = retry_e  # report the retry error

            logger.error(f"Swap execution failed: {e}")
            return SwapResult(
                success=False,
                tx_hash="",
                origin_netuid=origin_netuid,
                destination_netuid=destination_netuid,
                amount_tao=amount_tao,
                received_tao=0.0,
                fee_tao=0.0,
                slippage_pct=0.0,
                error=str(e),
                timestamp=timestamp,
            )

    async def _submit_swap(
        self,
        origin_netuid: int,
        destination_netuid: int,
        amount_rao: int,
        hotkey_ss58: str | None = None,
        alpha_in_pool_override: float | None = None,
    ) -> tuple[str, float, float]:
        """
        Submit the swap extrinsic on-chain using Bittensor SDK.
        Returns (tx_hash, alpha_received, tao_received).
        """
        if self._substrate is None:
            await self._ensure_substrate()
        if self._substrate is None or self._wallet is None:
            raise RuntimeError("Bittensor SDK not initialized – cannot submit live swap")

        def _parse_alpha(raw) -> float:
            """Parse alpha amount from ExtrinsicResponse data field (handles strings with symbols)."""
            import re
            if isinstance(raw, (int, float)):
                return float(raw)
            return float(re.sub(r"[^\d.]", "", str(raw)) or 0)

        def _do_swap() -> tuple[str, float, float]:
            try:
                import bittensor as _bt
                amount_balance = _bt.Balance.from_rao(amount_rao)
                rate_tol = settings.MAX_SLIPPAGE_PCT / 100.0

                # Use the explicitly provided hotkey; for entries without one,
                # look up the top-stake validator on the target subnet.
                if hotkey_ss58:
                    target_hotkey = hotkey_ss58
                elif origin_netuid == 0:
                    # Entry: need a registered validator hotkey on destination subnet
                    target_hotkey = self._get_validator_hotkey_sync(destination_netuid)
                else:
                    # Exit / subnet-to-subnet: should always have hotkey_ss58 supplied
                    target_hotkey = self._wallet.hotkey.ss58_address

                alpha_received: float = 0.0
                tao_received: float = 0.0

                if origin_netuid == 0:
                    # Entry: free TAO → subnet alpha via add_stake to validator hotkey
                    # Check coldkey balance first
                    ck_bal = self._substrate.get_balance(self._wallet.coldkey.ss58_address)
                    ck_tao = float(str(ck_bal).replace("τ", "").strip())
                    if ck_tao < rao_to_tao(amount_rao) + 0.01:
                        raise RuntimeError(
                            f"Insufficient coldkey balance: {ck_tao:.4f} τ < "
                            f"{rao_to_tao(amount_rao):.4f} τ needed"
                        )
                    result = self._substrate.add_stake(
                        wallet=self._wallet,
                        netuid=destination_netuid,
                        hotkey_ss58=target_hotkey,
                        amount=amount_balance,
                        safe_staking=True,
                        allow_partial_stake=True,
                        rate_tolerance=rate_tol,
                        wait_for_inclusion=True,
                        wait_for_finalization=False,
                    )
                    # Extract actual alpha received from ExtrinsicResponse.data
                    try:
                        data = getattr(result, "data", None) or {}
                        alpha_received = (
                            _parse_alpha(data.get("stake_after", 0))
                            - _parse_alpha(data.get("stake_before", 0))
                        )
                    except Exception:
                        pass

                elif destination_netuid == 0:
                    # Exit: subnet alpha → free TAO via unstake.
                    # Query on-chain alpha stake to determine size and chunking.
                    alpha_stake = self._substrate.get_stake(
                        coldkey_ss58=self._wallet.coldkey.ss58_address,
                        hotkey_ss58=target_hotkey,
                        netuid=origin_netuid,
                    )
                    alpha_float = float(alpha_stake)
                    logger.info(
                        f"Exit SN{origin_netuid}: on-chain alpha={alpha_float:.4f}, "
                        f"hotkey={target_hotkey[:12]}..."
                    )

                    if alpha_float < 0.001:
                        raise RuntimeError("ZERO_ALPHA_ON_CHAIN")

                    # Determine number of chunks based on pool depth.
                    # Each chunk should be small enough to keep slippage per chunk < 5%.
                    # For constant-product AMM: price_impact ≈ amount / pool_reserve.
                    # With 5% max tolerance, safe chunk ≈ 5% of the alpha reserve.
                    if alpha_in_pool_override is not None and alpha_in_pool_override > 0:
                        alpha_in_pool = alpha_in_pool_override
                    else:
                        from app.data.taostats_client import TaostatsClient
                        ts_inst = getattr(TaostatsClient, '_instance', None)
                        pool_snap = ts_inst._pool_snapshot.get(origin_netuid, {}) if ts_inst else {}
                        # alpha_in_pool is in rao from Taostats; convert to token units
                        alpha_in_pool = float(pool_snap.get("alpha_in_pool", 0) or 0) / 1e9

                    if alpha_in_pool > 0 and alpha_float > 0:
                        # Safe chunk = rate_tol fraction of pool reserve
                        safe_chunk_alpha = alpha_in_pool * rate_tol
                        num_chunks = max(1, int(alpha_float / safe_chunk_alpha + 0.999))
                        num_chunks = min(num_chunks, 10)  # cap at 10 chunks max
                    else:
                        num_chunks = 1

                    bal_before = self._substrate.get_balance(
                        self._wallet.coldkey.ss58_address
                    )

                    if num_chunks <= 1:
                        # Single exit — unstake everything
                        result = self._substrate.unstake_all(
                            wallet=self._wallet,
                            netuid=origin_netuid,
                            hotkey_ss58=target_hotkey,
                            rate_tolerance=rate_tol,
                            wait_for_inclusion=True,
                            wait_for_finalization=False,
                        )
                        if hasattr(result, "success") and not result.success:
                            raise RuntimeError(
                                f"unstake_all failed: {getattr(result, 'message', result)}"
                            )
                    else:
                        # Chunked exit to reduce per-trade slippage.
                        chunk_alpha = alpha_float / num_chunks
                        logger.info(
                            f"Chunked exit SN{origin_netuid}: "
                            f"{num_chunks} chunks of ~{chunk_alpha:.2f} alpha "
                            f"(pool={alpha_in_pool:.0f} alpha)"
                        )
                        import time as _time
                        for i in range(num_chunks):
                            is_last = (i == num_chunks - 1)
                            if is_last:
                                # Last chunk: unstake everything remaining
                                chunk_result = self._substrate.unstake_all(
                                    wallet=self._wallet,
                                    netuid=origin_netuid,
                                    hotkey_ss58=target_hotkey,
                                    rate_tolerance=rate_tol,
                                    wait_for_inclusion=True,
                                    wait_for_finalization=False,
                                )
                            else:
                                chunk_bal = _bt.Balance.from_tao(chunk_alpha)
                                chunk_result = self._substrate.unstake(
                                    wallet=self._wallet,
                                    netuid=origin_netuid,
                                    hotkey_ss58=target_hotkey,
                                    amount=chunk_bal,
                                    allow_partial_stake=True,
                                    safe_unstaking=True,
                                    rate_tolerance=rate_tol,
                                    wait_for_inclusion=True,
                                    wait_for_finalization=False,
                                )
                            if hasattr(chunk_result, "success") and not chunk_result.success:
                                raise RuntimeError(
                                    f"unstake chunk {i+1}/{num_chunks} failed: "
                                    f"{getattr(chunk_result, 'message', chunk_result)}"
                                )
                            logger.info(
                                f"Unstake chunk {i+1}/{num_chunks} SN{origin_netuid} OK"
                            )
                            if not is_last:
                                _time.sleep(2)  # brief pause between chunks
                        result = chunk_result  # use last chunk result for tx hash

                    # Calculate actual TAO received from balance delta
                    bal_after = self._substrate.get_balance(
                        self._wallet.coldkey.ss58_address
                    )
                    tao_received = float(bal_after) - float(bal_before)
                    # Report on-chain alpha so callers can separate emission yield from price PnL
                    alpha_received = alpha_float
                else:
                    # Subnet-to-subnet: swap stake using the stored validator hotkey
                    result = self._substrate.swap_stake(
                        wallet=self._wallet,
                        hotkey_ss58=target_hotkey,
                        origin_netuid=origin_netuid,
                        destination_netuid=destination_netuid,
                        amount=amount_balance,
                        allow_partial_stake=True,
                        rate_tolerance=rate_tol,
                        wait_for_inclusion=True,
                        wait_for_finalization=False,
                    )

                # ExtrinsicResponse — check .success (bittensor v10)
                if hasattr(result, "success") and not result.success:
                    raise RuntimeError(f"extrinsic failed: {getattr(result, 'message', result)}")
                tx = getattr(result, "extrinsic_hash", None) or getattr(result, "tx_hash", None) or str(result)
                return str(tx), alpha_received, tao_received

            except Exception as e:
                raise RuntimeError(f"On-chain swap failed: {e}")

        # Hold lock for the entire swap — substrate WebSocket is not safe for concurrent recv
        async with self._substrate_lock:
            tx_hash, alpha_received, tao_received = await asyncio.get_event_loop().run_in_executor(None, _do_swap)
        return tx_hash, alpha_received, tao_received

    async def get_onchain_stake(self, hotkey_ss58: str, netuid: int) -> float | None:
        """Return the coldkey's alpha stake for a given hotkey+netuid.

        Returns None if the chain cannot be queried (substrate not ready,
        RPC error, etc.) so callers can distinguish "confirmed zero" from
        "unable to check".
        """
        if self._substrate is None or self._wallet is None:
            logger.warning(f"get_onchain_stake SN{netuid}: substrate not ready, returning None")
            return None

        def _query() -> float:
            bal = self._substrate.get_stake(
                coldkey_ss58=self._wallet.coldkey.ss58_address,
                hotkey_ss58=hotkey_ss58,
                netuid=netuid,
            )
            return float(bal)

        try:
            async with self._substrate_lock:
                return await asyncio.get_running_loop().run_in_executor(None, _query)
        except Exception as e:
            logger.warning(f"get_onchain_stake SN{netuid} failed: {e}")
            return None

    async def get_tao_balance(self) -> float:
        """Get the coldkey's free TAO balance (used for staking)."""
        if self._substrate is not None and self._wallet is not None:
            try:
                async with self._substrate_lock:
                    balance = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: self._substrate.get_balance(self._wallet.coldkey.ss58_address),
                    )
                return rao_to_tao(int(balance))
            except Exception as e:
                logger.warning(f"Failed to get balance via SDK (shared conn): {e}")

        # In dry-run mode, return the configured simulated balance for position sizing.
        if settings.EMA_DRY_RUN:
            return settings.EMA_DRY_RUN_STARTING_TAO

        # Last resort: fresh Subtensor connection (avoids WebSocket contention)
        if self._wallet is not None:
            try:
                import bittensor as bt
                coldkey_addr = self._wallet.coldkey.ss58_address
                balance = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: bt.Subtensor(
                            network=settings.SUBTENSOR_NETWORK
                        ).get_balance(coldkey_addr),
                    ),
                    timeout=15,
                )
                return rao_to_tao(int(balance))
            except Exception as e:
                logger.warning(f"Failed to get balance via Subtensor fallback: {e}")

        return 0.0

    async def get_onchain_alpha_price(self, netuid: int) -> float:
        """
        Query on-chain pool reserves to derive the current alpha price (TAO/alpha).
        Returns 0.0 if substrate is unavailable or query fails.
        """
        if self._substrate is None:
            return 0.0

        def _query() -> float:
            tao_r_raw = self._substrate.query_subtensor("SubnetTAO", [netuid])
            alpha_r_raw = self._substrate.query_subtensor("SubnetAlphaIn", [netuid])
            if not tao_r_raw or not alpha_r_raw:
                return 0.0
            tao_r = int(tao_r_raw.value if hasattr(tao_r_raw, "value") else tao_r_raw)
            alpha_r = int(alpha_r_raw.value if hasattr(alpha_r_raw, "value") else alpha_r_raw)
            if alpha_r <= 0:
                return 0.0
            return tao_r / alpha_r  # both in rao, so ratio = TAO-per-alpha

        try:
            async with self._substrate_lock:
                return await asyncio.get_running_loop().run_in_executor(None, _query)
        except Exception as e:
            logger.warning(f"Failed to get on-chain price for SN{netuid}: {e}")
            err_str = str(e).lower()
            if any(kw in err_str for kw in ("broken pipe", "connection", "closed", "timeout", "eof", "websocket")):
                try:
                    await self._reconnect_substrate()
                    async with self._substrate_lock:
                        return await asyncio.get_running_loop().run_in_executor(None, _query)
                except Exception:
                    pass
            return 0.0

    async def ensure_root_claim_keep(self) -> bool:
        """Set root claim type to 'Keep' so emissions accumulate as alpha.

        Returns True if already set or successfully changed, False on error.
        """
        if self._substrate is None or self._wallet is None:
            logger.warning("ensure_root_claim_keep: substrate/wallet not ready")
            return False

        def _set() -> bool:
            coldkey = self._wallet.coldkey.ss58_address
            current = self._substrate.get_root_claim_type(coldkey_ss58=coldkey)
            current_str = str(current)
            if "Keep" in current_str and "KeepSubnets" not in current_str:
                logger.info(f"Root claim type already 'Keep': {current_str}")
                return True
            logger.info(f"Setting root claim type to 'Keep' (was: {current_str})")
            result = self._substrate.set_root_claim_type(
                wallet=self._wallet,
                new_root_claim_type="Keep",
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
            if hasattr(result, "success") and not result.success:
                logger.error(f"set_root_claim_type failed: {getattr(result, 'message', result)}")
                return False
            logger.info("Root claim type set to 'Keep'")
            return True

        try:
            async with self._substrate_lock:
                return await asyncio.get_running_loop().run_in_executor(None, _set)
        except Exception as e:
            logger.error(f"ensure_root_claim_keep error: {e}")
            return False

    async def claim_root_emissions(self, netuids: list[int]) -> bool:
        """Claim accumulated root emissions for the given netuids.

        The SDK allows up to 5 netuids per call; this method batches
        automatically if more are provided.
        Returns True if all batches succeeded, False otherwise.
        """
        if self._substrate is None or self._wallet is None:
            logger.warning("claim_root_emissions: substrate/wallet not ready")
            return False
        if not netuids:
            logger.debug("claim_root_emissions: no netuids to claim")
            return True

        def _claim(batch: list[int]) -> bool:
            result = self._substrate.claim_root(
                wallet=self._wallet,
                netuids=batch,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )
            if hasattr(result, "success") and not result.success:
                logger.error(
                    f"claim_root failed for netuids {batch}: "
                    f"{getattr(result, 'message', result)}"
                )
                return False
            logger.info(f"Root emissions claimed for netuids {batch}")
            return True

        all_ok = True
        # Batch in groups of 5 (SDK limit)
        for i in range(0, len(netuids), 5):
            batch = netuids[i : i + 5]
            try:
                async with self._substrate_lock:
                    ok = await asyncio.get_running_loop().run_in_executor(None, _claim, batch)
                if not ok:
                    all_ok = False
            except Exception as e:
                logger.error(f"claim_root_emissions error for {batch}: {e}")
                all_ok = False
        return all_ok
