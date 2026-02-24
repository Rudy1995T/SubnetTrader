"""
Trading execution layer – swap simulation & execution using Bittensor SDK
with FlameWire as the RPC backend.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.chain.flamewire_rpc import FlameWireRPC
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


def tao_to_rao(tao: float) -> int:
    return int(tao * RAO_PER_TAO)


def rao_to_tao(rao: int) -> float:
    return rao / RAO_PER_TAO


class SwapExecutor:
    """
    Handles quoting and executing swaps between TAO (netuid=0) and
    subnet alpha tokens via the Bittensor Swap pallet.
    """

    def __init__(self, rpc: FlameWireRPC) -> None:
        self._rpc = rpc
        self._wallet = None
        self._substrate = None

    async def initialize(self) -> None:
        """
        Initialize the Bittensor wallet and substrate connection.
        Uses FlameWire as the endpoint.
        """
        try:
            import bittensor as bt

            wallet = bt.wallet(
                name=settings.BT_WALLET_NAME,
                hotkey=settings.BT_WALLET_HOTKEY,
                path=settings.BT_WALLET_PATH,
            )
            self._wallet = wallet

            # Connect substrate to FlameWire
            self._substrate = bt.subtensor(
                network=settings.flamewire_http_url,
            )

            logger.info(
                "SwapExecutor initialized",
                data={
                    "wallet": settings.BT_WALLET_NAME,
                    "hotkey": settings.BT_WALLET_HOTKEY,
                    "rpc": settings.flamewire_http_url,
                },
            )
        except ImportError:
            logger.warning(
                "bittensor SDK not available – using RPC-only mode (quotes may be estimated)"
            )
        except Exception as e:
            logger.error(f"Failed to initialize bittensor wallet: {e}")

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
                # Use SDK simulation
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
            # Try subtensor sim_swap method
            if hasattr(self._substrate, "sim_swap"):
                result = self._substrate.sim_swap(
                    origin_netuid=origin_netuid,
                    destination_netuid=destination_netuid,
                    amount=amount_rao,
                    wallet=self._wallet,
                )
                return {
                    "expected_out": int(result.get("expected_amount", result.get("amount_out", 0))),
                    "fee": int(result.get("fee", 0)),
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
    ) -> SwapResult:
        """
        Execute an actual swap on-chain.
        In DRY_RUN mode, simulates without broadcasting.
        """
        if max_slippage_pct is None:
            max_slippage_pct = settings.MAX_SLIPPAGE_PCT

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
        if settings.DRY_RUN:
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
            tx_hash = await self._submit_swap(
                origin_netuid, destination_netuid, amount_rao, quote.expected_out_rao
            )
            logger.info(
                "LIVE swap submitted",
                data={
                    "tx_hash": tx_hash,
                    "origin": origin_netuid,
                    "destination": destination_netuid,
                    "amount_tao": amount_tao,
                },
            )
            return SwapResult(
                success=True,
                tx_hash=tx_hash,
                origin_netuid=origin_netuid,
                destination_netuid=destination_netuid,
                amount_tao=amount_tao,
                received_tao=quote.expected_out_tao,
                fee_tao=quote.fee_tao,
                slippage_pct=quote.slippage_estimate_pct,
                error="",
                timestamp=timestamp,
            )
        except Exception as e:
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
        min_expected_rao: int,
    ) -> str:
        """
        Submit the swap extrinsic on-chain using Bittensor SDK.
        """
        if self._substrate is None or self._wallet is None:
            raise RuntimeError("Bittensor SDK not initialized – cannot submit live swap")

        # Apply slippage tolerance to min expected
        slippage_factor = 1.0 - (settings.MAX_SLIPPAGE_PCT / 100.0)
        min_out = int(min_expected_rao * slippage_factor)

        def _do_swap() -> str:
            try:
                # Try the primary swap method
                if hasattr(self._substrate, "swap"):
                    result = self._substrate.swap(
                        wallet=self._wallet,
                        origin_netuid=origin_netuid,
                        destination_netuid=destination_netuid,
                        amount=amount_rao,
                        min_expected=min_out,
                        wait_for_inclusion=True,
                        wait_for_finalization=False,
                    )
                    if isinstance(result, dict):
                        return result.get("tx_hash", result.get("extrinsic_hash", str(result)))
                    return str(result)

                # Fallback: compose extrinsic manually
                call = self._substrate.substrate.compose_call(
                    call_module="SubtensorModule",
                    call_function="swap",
                    call_params={
                        "origin_netuid": origin_netuid,
                        "destination_netuid": destination_netuid,
                        "amount": amount_rao,
                        "min_expected": min_out,
                    },
                )
                extrinsic = self._substrate.substrate.create_signed_extrinsic(
                    call=call,
                    keypair=self._wallet.hotkey,
                )
                receipt = self._substrate.substrate.submit_extrinsic(
                    extrinsic,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )
                return str(receipt.extrinsic_hash)

            except Exception as e:
                raise RuntimeError(f"On-chain swap failed: {e}")

        tx_hash = await asyncio.get_event_loop().run_in_executor(None, _do_swap)
        return tx_hash

    async def get_tao_balance(self) -> float:
        """Get the hotkey's TAO balance."""
        if self._substrate is not None and self._wallet is not None:
            try:
                balance = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._substrate.get_balance(self._wallet.hotkey.ss58_address),
                )
                return rao_to_tao(int(balance))
            except Exception as e:
                logger.warning(f"Failed to get balance via SDK: {e}")

        # Fallback via RPC
        try:
            if self._wallet is not None:
                addr = self._wallet.hotkey.ss58_address
            else:
                addr = ""
            if addr:
                balance = await self._rpc.get_balance(addr)
                return rao_to_tao(balance)
        except Exception as e:
            logger.warning(f"Failed to get balance via RPC: {e}")

        return 0.0

    async def get_alpha_balance(self, netuid: int) -> float:
        """Get alpha token balance for a specific subnet."""
        if self._substrate is not None and self._wallet is not None:
            try:
                balance = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._substrate.get_stake(
                        self._wallet.hotkey.ss58_address,
                        netuid,
                    ),
                )
                return rao_to_tao(int(balance))
            except Exception:
                pass
        return 0.0
