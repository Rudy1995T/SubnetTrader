"""
EMA-only application entrypoint.

Runs the EMA trading scheduler, lightweight control API, and the health server.
"""
from __future__ import annotations

import asyncio
import html
import io
import os
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.chain.executor import SwapExecutor
from app.chain.flamewire_rpc import FlameWireRPC
from app.config import settings
from app.data.taostats_client import TaostatsClient
from app.logging.logger import logger
from app.notifications.telegram import (
    TelegramBot,
    TelegramCommandHandlers,
    TelegramDocument,
    send_alert,
)
from app.portfolio.ema_manager import EmaManager
from app.storage.db import Database
from app.utils.time import utc_iso

db: Database | None = None
rpc: FlameWireRPC | None = None
executor: SwapExecutor | None = None
taostats: TaostatsClient | None = None
ema_portfolio: EmaManager | None = None
scheduler: AsyncIOScheduler | None = None
telegram_bot: TelegramBot | None = None
_shutdown_event: asyncio.Event | None = None
_ema_exit_watch_status: dict[str, object | None] = {
    "last_run": None,
    "last_error": None,
    "last_exit_count": 0,
}
_tao_usd_cache: dict[str, float | None] = {"price": None, "fetched_at": 0.0}


async def init_services() -> None:
    """Initialize the EMA runtime and its shared infrastructure."""
    global db, rpc, executor, taostats, ema_portfolio

    logger.info("Initializing EMA services")

    db = Database()
    await db.connect()

    rpc = FlameWireRPC()
    if await rpc.health_check():
        logger.info("FlameWire RPC health check passed")
    else:
        logger.warning("FlameWire RPC health check failed; continuing in degraded mode")

    executor = SwapExecutor(rpc)
    await executor.initialize()

    taostats = TaostatsClient()

    if settings.EMA_ENABLED:
        ema_portfolio = EmaManager(db, executor, taostats)
        await ema_portfolio.initialize()

    logger.info(
        "EMA services initialized",
        data={
            "ema_enabled": settings.EMA_ENABLED,
            "ema_dry_run": settings.EMA_DRY_RUN,
            "scan_interval_min": settings.SCAN_INTERVAL_MIN,
            "exit_watcher_enabled": settings.EMA_EXIT_WATCHER_ENABLED,
        },
    )


async def shutdown_services() -> None:
    """Gracefully shut down services."""
    logger.info("Shutting down EMA services")

    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    if taostats:
        await taostats.close()
    if rpc:
        await rpc.close()
    if db:
        await db.close()

    logger.info("Services shut down cleanly")


async def run_ema_cycle() -> None:
    """Execute one EMA scan and trade cycle."""
    if ema_portfolio is None:
        logger.warning("EMA manager not initialized")
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        logger.warning("KILL_SWITCH active; skipping EMA cycle")
        return

    try:
        logger.info(f"Starting EMA cycle at {utc_iso()}")
        summary = await ema_portfolio.run_cycle()
        logger.info("EMA cycle complete", data=summary)
    except Exception as exc:
        logger.error(f"EMA cycle failed: {exc}", data={"error": str(exc)})


async def run_ema_exit_watch() -> None:
    """Execute the lightweight EMA exit watcher."""
    global _ema_exit_watch_status

    if ema_portfolio is None or not settings.EMA_EXIT_WATCHER_ENABLED:
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return

    try:
        summary = await ema_portfolio.run_price_exit_watch()
        _ema_exit_watch_status = {
            "last_run": utc_iso(),
            "last_error": None,
            "last_exit_count": len(summary.get("exits", [])),
        }
        if summary.get("exits"):
            logger.info("EMA exit watcher complete", data=summary)
    except Exception as exc:
        _ema_exit_watch_status = {
            "last_run": utc_iso(),
            "last_error": str(exc),
            "last_exit_count": 0,
        }
        logger.error(f"EMA exit watcher failed: {exc}", data={"error": str(exc)})


def setup_scheduler() -> AsyncIOScheduler:
    """Configure the EMA scheduler."""
    sched = AsyncIOScheduler()
    if settings.EMA_ENABLED:
        sched.add_job(
            run_ema_cycle,
            trigger="interval",
            minutes=settings.SCAN_INTERVAL_MIN,
            id="ema_cycle",
            name="EMA Trend Scanner",
            max_instances=1,
            misfire_grace_time=60,
        )
        if settings.EMA_EXIT_WATCHER_ENABLED:
            sched.add_job(
                run_ema_exit_watch,
                trigger="interval",
                seconds=settings.EMA_EXIT_WATCHER_SEC,
                id="ema_exit_watch",
                name="EMA Exit Watcher",
                max_instances=1,
                misfire_grace_time=max(settings.EMA_EXIT_WATCHER_SEC, 5),
            )
    return sched


def _telegram_help_text() -> str:
    return (
        "🤖 <b>EMA Telegram Commands</b>\n"
        "<code>/status</code> current EMA runtime status\n"
        "<code>/positions [limit]</code> open EMA positions\n"
        "<code>/close &lt;position_id|sn42&gt;</code> close one position\n"
        "<code>/pause</code> enable kill switch\n"
        "<code>/resume</code> clear kill switch\n"
        "<code>/run</code> trigger one EMA cycle\n"
        "<code>/export</code> send the EMA trades CSV"
    )


async def _telegram_status_text() -> str:
    if ema_portfolio is None:
        return "EMA runtime is still initializing."

    alpha_prices = await taostats.get_alpha_prices() if taostats else {}
    summary = ema_portfolio.get_portfolio_summary(alpha_prices)
    next_cycle = None
    if scheduler:
        job = scheduler.get_job("ema_cycle")
        if job and job.next_run_time:
            next_cycle = job.next_run_time.isoformat()

    total_pnl_tao = summary["pot_tao"] - settings.EMA_POT_TAO
    total_pnl_pct = (
        total_pnl_tao / settings.EMA_POT_TAO * 100.0 if settings.EMA_POT_TAO > 0 else 0.0
    )
    lines = [
        "🤖 <b>EMA Status</b>",
        f"Mode: {'LIVE' if not settings.EMA_DRY_RUN else 'DRY RUN'}",
        f"Trading: {'PAUSED' if os.path.exists(settings.KILL_SWITCH_PATH) else 'RUNNING'}",
        f"Next cycle: {next_cycle or 'n/a'}",
        f"Open positions: {summary['open_count']}/{summary['max_positions']}",
        f"Pot: {summary['pot_tao']:.4f} τ",
        f"Deployed: {summary['deployed_tao']:.4f} τ",
        f"Unstaked: {summary['unstaked_tao']:.4f} τ",
        f"PnL: {total_pnl_tao:+.4f} τ ({total_pnl_pct:+.2f}%)",
        f"Circuit breaker: {'ACTIVE' if summary['breaker_active'] else 'off'}",
        (
            f"Exit watcher: on ({settings.EMA_EXIT_WATCHER_SEC}s)"
            if settings.EMA_EXIT_WATCHER_ENABLED
            else "Exit watcher: off"
        ),
    ]

    if executor is not None:
        try:
            wallet_balance = await executor.get_tao_balance()
            lines.append(f"Wallet balance: {wallet_balance:.4f} τ")
        except Exception:
            pass

    return "\n".join(lines)


async def _telegram_positions_text(limit: int) -> str:
    if ema_portfolio is None:
        return "EMA runtime is still initializing."

    alpha_prices = await taostats.get_alpha_prices() if taostats else {}
    summary = ema_portfolio.get_portfolio_summary(alpha_prices)
    positions = summary["open_positions"]
    if not positions:
        return "📂 <b>Open EMA Positions</b>\nNo open EMA positions."

    snapshot = taostats._pool_snapshot if taostats else {}
    limit = max(1, min(limit, 20))
    lines = ["📂 <b>Open EMA Positions</b>"]
    for pos in positions[:limit]:
        name = snapshot.get(pos["netuid"], {}).get("name", "") or f"SN{pos['netuid']}"
        lines.append(
            f"#{pos['position_id']} {html.escape(name)} (SN{pos['netuid']}) | "
            f"{pos['pnl_pct']:+.2f}% | {pos['amount_tao']:.4f} τ | {pos['hours_held']:.1f}h"
        )

    if len(positions) > limit:
        lines.append(f"... {len(positions) - limit} more open position(s)")
    return "\n".join(lines)


async def _telegram_pause_text() -> str:
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return "⏸️ <b>EMA paused</b>\nKill switch is already active."

    with open(settings.KILL_SWITCH_PATH, "w") as handle:
        handle.write(utc_iso())
    return "⏸️ <b>EMA paused</b>\nKill switch enabled."


async def _telegram_resume_text() -> str:
    if os.path.exists(settings.KILL_SWITCH_PATH):
        os.remove(settings.KILL_SWITCH_PATH)
        return "▶️ <b>EMA resumed</b>\nKill switch cleared."
    return "▶️ <b>EMA resumed</b>\nKill switch was already clear."


async def _telegram_run_cycle_text() -> str:
    if ema_portfolio is None:
        return "EMA runtime is still initializing."
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return "Kill switch is active. Use <code>/resume</code> before triggering a cycle."

    asyncio.create_task(run_ema_cycle())
    return "🟢 <b>EMA cycle triggered</b>\nA manual scan has been queued."


async def _telegram_close_text(target: str) -> str:
    if ema_portfolio is None:
        return "EMA runtime is still initializing."

    alpha_prices = await taostats.get_alpha_prices() if taostats else {}
    summary = ema_portfolio.get_portfolio_summary(alpha_prices)
    positions = summary["open_positions"]
    if not positions:
        return "No open EMA positions to close."

    normalized = target.strip().lower()
    if normalized.startswith("#"):
        normalized = normalized[1:]

    selected = None
    if normalized.startswith("sn"):
        try:
            netuid = int(normalized[2:])
        except ValueError:
            return "Usage: <code>/close &lt;position_id|sn42&gt;</code>"
        selected = next((pos for pos in positions if pos["netuid"] == netuid), None)
    else:
        try:
            numeric = int(normalized)
        except ValueError:
            return "Usage: <code>/close &lt;position_id|sn42&gt;</code>"
        selected = next((pos for pos in positions if pos["position_id"] == numeric), None)
        if selected is None:
            selected = next((pos for pos in positions if pos["netuid"] == numeric), None)

    if selected is None:
        return f"No open EMA position matches <code>{html.escape(target)}</code>."

    result = await ema_portfolio.manual_close(selected["position_id"])
    if result is None:
        return "That EMA position is already closing or no longer available."

    snapshot = taostats._pool_snapshot if taostats else {}
    name = snapshot.get(result["netuid"], {}).get("name", "") or f"SN{result['netuid']}"
    return (
        f"📉 <b>EMA manual close</b>\n"
        f"{html.escape(name)} (SN{result['netuid']})\n"
        f"Reason: {result['reason']}\n"
        f"PnL: {result['pnl_pct']:+.2f}% ({result['pnl_tao']:+.4f} τ)"
    )


async def _telegram_export_result() -> str | TelegramDocument:
    if db is None:
        return "Trade export is unavailable while the database is still initializing."

    rows = await db.get_ema_positions(limit=1)
    if not rows:
        return "No EMA trades are available to export yet."

    path = await db.export_ema_positions_csv("data/exports/ema_trades.csv")
    return TelegramDocument(
        path=path,
        caption="🧾 <b>EMA trades export</b>\nAttached: <code>ema_trades.csv</code>",
    )


def _build_telegram_bot() -> TelegramBot:
    return TelegramBot(
        TelegramCommandHandlers(
            help_text=_telegram_help_text(),
            status=_telegram_status_text,
            positions=_telegram_positions_text,
            close=_telegram_close_text,
            pause=_telegram_pause_text,
            resume=_telegram_resume_text,
            run_cycle=_telegram_run_cycle_text,
            export_csv=_telegram_export_result,
        )
    )


def create_health_app():
    """Create the FastAPI app for EMA health, control, and trading routes."""
    try:
        import time as time_module

        import httpx
        from fastapi import FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from starlette.responses import Response

        app = FastAPI(title="SubnetTrader EMA", docs_url=None, redoc_url=None)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/health")
        async def health():
            data: dict[str, object] = {
                "status": "ok" if ema_portfolio else "initializing",
                "timestamp": utc_iso(),
                "ema_enabled": settings.EMA_ENABLED,
                "ema_dry_run": settings.EMA_DRY_RUN,
                "kill_switch_active": os.path.exists(settings.KILL_SWITCH_PATH),
            }
            if ema_portfolio and taostats:
                alpha_prices = await taostats.get_alpha_prices()
                data["portfolio"] = ema_portfolio.get_portfolio_summary(alpha_prices)
            return JSONResponse(content=data)

        @app.get("/api/control/status")
        async def api_control_status():
            next_run = None
            if scheduler:
                job = scheduler.get_job("ema_cycle")
                if job and job.next_run_time:
                    next_run = job.next_run_time.isoformat()

            summary = None
            if ema_portfolio and taostats:
                alpha_prices = await taostats.get_alpha_prices()
                summary = ema_portfolio.get_portfolio_summary(alpha_prices)

            return JSONResponse(
                content={
                    "kill_switch_active": os.path.exists(settings.KILL_SWITCH_PATH),
                    "scheduler_running": scheduler.running if scheduler else False,
                    "next_cycle": next_run,
                    "ema_enabled": settings.EMA_ENABLED,
                    "ema_dry_run": settings.EMA_DRY_RUN,
                    "exit_watcher_enabled": settings.EMA_EXIT_WATCHER_ENABLED,
                    "breaker_active": summary["breaker_active"] if summary else False,
                }
            )

        @app.post("/api/control/pause")
        async def api_control_pause():
            with open(settings.KILL_SWITCH_PATH, "w") as handle:
                handle.write(utc_iso())
            return JSONResponse(content={"paused": True})

        @app.post("/api/control/resume")
        async def api_control_resume():
            if os.path.exists(settings.KILL_SWITCH_PATH):
                os.remove(settings.KILL_SWITCH_PATH)
            return JSONResponse(content={"paused": False})

        @app.post("/api/control/run-ema-cycle")
        async def api_run_ema_cycle():
            if ema_portfolio is None:
                raise HTTPException(status_code=503, detail="EMA not initialized")
            asyncio.create_task(run_ema_cycle())
            return JSONResponse(content={"triggered": True})

        @app.post("/api/control/reset-dry-run")
        async def api_reset_dry_run():
            if not settings.EMA_DRY_RUN:
                raise HTTPException(status_code=403, detail="Reset only allowed in EMA_DRY_RUN mode")
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            await db.clear_ema_history()
            if ema_portfolio:
                await ema_portfolio.initialize()

            logger.info("EMA dry-run data reset via control panel")
            return JSONResponse(content={"reset": True})

        @app.get("/api/ema/portfolio")
        async def api_ema_portfolio():
            if ema_portfolio is None:
                return JSONResponse(content={"enabled": False})

            alpha_prices = await taostats.get_alpha_prices() if taostats else {}
            summary = ema_portfolio.get_portfolio_summary(alpha_prices)
            snapshot = taostats._pool_snapshot if taostats else {}

            for pos in summary["open_positions"]:
                pos["name"] = snapshot.get(pos["netuid"], {}).get("name", "") or f"SN{pos['netuid']}"

            summary["enabled"] = True
            summary["ema_period"] = settings.EMA_PERIOD
            summary["confirm_bars"] = settings.EMA_CONFIRM_BARS
            summary["dry_run"] = settings.EMA_DRY_RUN
            summary["signal_timeframe_hours"] = settings.EMA_CANDLE_TIMEFRAME_HOURS
            summary["stop_loss_pct"] = settings.EMA_STOP_LOSS_PCT
            summary["take_profit_pct"] = settings.EMA_TAKE_PROFIT_PCT
            summary["trailing_stop_pct"] = 10.0
            summary["exit_watcher"] = {
                "enabled": settings.EMA_EXIT_WATCHER_ENABLED,
                "interval_sec": settings.EMA_EXIT_WATCHER_SEC,
                "last_run": _ema_exit_watch_status["last_run"],
                "last_error": _ema_exit_watch_status["last_error"],
                "last_exit_count": _ema_exit_watch_status["last_exit_count"],
            }

            try:
                summary["wallet_balance"] = round(await executor.get_tao_balance(), 6) if executor else None
            except Exception:
                summary["wallet_balance"] = None

            return JSONResponse(content=summary)

        @app.get("/api/ema/positions")
        async def api_ema_positions(limit: int = 200):
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            rows = await db.get_ema_positions(limit=limit)
            snapshot = taostats._pool_snapshot if taostats else {}
            for row in rows:
                row["name"] = snapshot.get(row["netuid"], {}).get("name", "") or f"SN{row['netuid']}"
            return JSONResponse(content={"positions": rows})

        @app.get("/api/ema/signals")
        async def api_ema_signals():
            if taostats is None:
                return JSONResponse(content={"signals": []})

            alpha_prices = await taostats.get_alpha_prices()
            snapshot = taostats._pool_snapshot

            from app.strategy.ema_signals import (
                bars_above_below_ema,
                compute_ema,
                dual_ema_signal,
            )

            results = []
            ranked_snapshot = sorted(
                snapshot.items(),
                key=lambda item: float(item[1].get("tao_in_pool", 0) or 0),
                reverse=True,
            )
            for netuid, snap_data in ranked_snapshot:
                if netuid == 0:
                    continue
                cur = alpha_prices.get(netuid, 0.0)
                if cur <= 0:
                    continue

                seven_day = snap_data.get("seven_day_prices", [])
                prices = [float(entry["price"]) for entry in seven_day if entry.get("price")]
                if not prices:
                    continue

                ema_vals = compute_ema(prices, settings.EMA_PERIOD)
                fast_ema_vals = compute_ema(prices, settings.EMA_FAST_PERIOD)
                results.append(
                    {
                        "netuid": netuid,
                        "name": snap_data.get("name", "") or f"SN{netuid}",
                        "price": cur,
                        "ema": round(ema_vals[-1], 8) if ema_vals else 0.0,
                        "fast_ema": round(fast_ema_vals[-1], 8) if fast_ema_vals else 0.0,
                        "signal": dual_ema_signal(
                            prices,
                            settings.EMA_FAST_PERIOD,
                            settings.EMA_PERIOD,
                            settings.EMA_CONFIRM_BARS,
                        ),
                        "bars": bars_above_below_ema(prices, settings.EMA_PERIOD),
                    }
                )

            return JSONResponse(
                content={
                    "signals": results[:60],
                    "ema_period": settings.EMA_PERIOD,
                    "fast_ema_period": settings.EMA_FAST_PERIOD,
                }
            )

        @app.post("/api/ema/positions/{position_id}/close")
        async def api_ema_close(position_id: int):
            if ema_portfolio is None:
                raise HTTPException(status_code=503, detail="EMA not initialized")

            result = await ema_portfolio.manual_close(position_id)
            if result is None:
                raise HTTPException(status_code=404, detail="EMA position not found")
            return JSONResponse(content={"success": True, "result": result})

        @app.get("/api/ema/slippage-stats")
        async def api_ema_slippage_stats():
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            rows = await db.fetchall(
                """
                SELECT netuid, entry_slippage_pct, exit_slippage_pct, amount_tao
                FROM ema_positions
                WHERE status = 'CLOSED' AND entry_slippage_pct IS NOT NULL
                ORDER BY exit_ts DESC
                LIMIT 100
                """
            )
            if not rows:
                return JSONResponse(content={"trade_count": 0})

            entry_slips = [row["entry_slippage_pct"] for row in rows if row["entry_slippage_pct"] is not None]
            exit_slips = [row["exit_slippage_pct"] for row in rows if row["exit_slippage_pct"] is not None]
            avg_entry = sum(entry_slips) / len(entry_slips) if entry_slips else 0.0
            avg_exit = sum(exit_slips) / len(exit_slips) if exit_slips else 0.0

            combined = []
            total_slip_tao = 0.0
            for row in rows:
                entry_slip = row["entry_slippage_pct"] or 0.0
                exit_slip = row["exit_slippage_pct"] or 0.0
                combined_slip = entry_slip + exit_slip
                combined.append((combined_slip, row["netuid"]))
                total_slip_tao += row["amount_tao"] * combined_slip / 100.0

            combined.sort()
            return JSONResponse(
                content={
                    "trade_count": len(rows),
                    "avg_entry_slippage_pct": round(avg_entry, 3),
                    "avg_exit_slippage_pct": round(avg_exit, 3),
                    "avg_round_trip_pct": round(avg_entry + avg_exit, 3),
                    "total_slippage_tao": round(total_slip_tao, 4),
                    "best_trade": {
                        "slippage_pct": round(combined[0][0], 2),
                        "netuid": combined[0][1],
                    }
                    if combined
                    else None,
                    "worst_trade": {
                        "slippage_pct": round(combined[-1][0], 2),
                        "netuid": combined[-1][1],
                    }
                    if combined
                    else None,
                }
            )

        @app.get("/api/subnets/{netuid}/history")
        async def api_subnet_history(netuid: int):
            if taostats is None:
                raise HTTPException(status_code=503, detail="Taostats not initialized")

            snapshot = taostats._pool_snapshot.get(netuid, {})
            history = []
            for entry in snapshot.get("seven_day_prices", []):
                if isinstance(entry, dict) and entry.get("price") is not None:
                    history.append({"t": entry.get("timestamp", ""), "p": float(entry["price"])})

            if not history:
                raw = await taostats.get_price_history(netuid, limit=200)
                for entry in raw:
                    if isinstance(entry, dict) and entry.get("price") is not None:
                        history.append({"t": entry.get("timestamp", ""), "p": float(entry["price"])})

            return JSONResponse(
                content={
                    "netuid": netuid,
                    "name": snapshot.get("name", "") or f"Subnet {netuid}",
                    "price": float(snapshot.get("price", 0) or 0),
                    "history": history,
                }
            )

        @app.get("/api/subnets/{netuid}/spot")
        async def api_subnet_spot(netuid: int):
            price = 0.0
            source = "unavailable"

            if executor is not None:
                price = await executor.get_onchain_alpha_price(netuid)
                if price > 0:
                    source = "onchain"

            if price <= 0 and taostats is not None:
                snapshot = taostats._pool_snapshot.get(netuid, {})
                price = float(snapshot.get("price", 0) or 0)
                if price > 0:
                    source = "taostats"

            return JSONResponse(
                content={
                    "netuid": netuid,
                    "price": price if price > 0 else None,
                    "available": price > 0,
                    "source": source,
                    "timestamp": utc_iso(),
                }
            )

        @app.get("/api/price/tao-usd")
        async def api_tao_usd_price():
            now = time_module.time()
            if _tao_usd_cache["price"] and now - (_tao_usd_cache["fetched_at"] or 0) < 120:
                return JSONResponse(content={"usd": _tao_usd_cache["price"], "cached": True})

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(
                        "https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": "bittensor", "vs_currencies": "usd"},
                    )
                    response.raise_for_status()
                    usd = float(response.json()["bittensor"]["usd"])
                    _tao_usd_cache["price"] = usd
                    _tao_usd_cache["fetched_at"] = now
                    return JSONResponse(content={"usd": usd, "cached": False})
            except Exception as exc:
                if _tao_usd_cache["price"]:
                    return JSONResponse(
                        content={"usd": _tao_usd_cache["price"], "cached": True, "stale": True}
                    )
                raise HTTPException(status_code=503, detail=f"Price feed unavailable: {exc}")

        @app.get("/api/export/trades.csv")
        async def api_export_trades_csv():
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            rows = await db.get_ema_positions(limit=10000)
            if not rows:
                raise HTTPException(status_code=404, detail="No EMA trades to export")

            buffer = io.StringIO()
            fieldnames = list(rows[0].keys())
            import csv

            writer = csv.DictWriter(buffer, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

            return Response(
                content=buffer.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=ema_trades.csv"},
            )

        return app
    except ImportError:
        return None


async def run_health_server() -> None:
    """Run the FastAPI server in the background."""
    app = create_health_app()
    if app is None:
        logger.info("FastAPI not available; health endpoint disabled")
        return

    try:
        import uvicorn

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=settings.HEALTH_PORT,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        await server.serve()
    except ImportError:
        logger.info("uvicorn not available; health endpoint disabled")
    except Exception as exc:
        logger.error(f"Health server error: {exc}")


async def export_csvs() -> None:
    """Export EMA trades to a CSV file for reporting workflows."""
    database = Database()
    await database.connect()
    path = await database.export_ema_positions_csv("data/exports/ema_trades.csv")
    print(f"Exported EMA trades to: {path}")
    await database.close()


async def main() -> None:
    """Main async entrypoint."""
    global scheduler, telegram_bot, _shutdown_event

    if len(sys.argv) > 1 and sys.argv[1].lower() == "export":
        await export_csvs()
        return

    _shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        _shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    health_task: asyncio.Task[None] | None = None
    telegram_task: asyncio.Task[None] | None = None
    try:
        await init_services()

        scheduler = setup_scheduler()
        scheduler.start()
        logger.info(f"EMA scheduler started: scanning every {settings.SCAN_INTERVAL_MIN} minutes")

        health_task = asyncio.create_task(run_health_server())

        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
            telegram_bot = _build_telegram_bot()
            telegram_task = asyncio.create_task(telegram_bot.run(_shutdown_event))
            await send_alert(
                f"🤖 <b>EMA bot online</b>\n"
                f"Mode: {'LIVE' if not settings.EMA_DRY_RUN else 'DRY RUN'}\n"
                f"Scan interval: {settings.SCAN_INTERVAL_MIN}m\n"
                f"Commands: <code>/help</code>"
            )

        if settings.EMA_ENABLED:
            await run_ema_cycle()
            if settings.EMA_EXIT_WATCHER_ENABLED:
                await run_ema_exit_watch()

        await _shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as exc:
        logger.critical(f"Fatal error: {exc}")
    finally:
        if telegram_task:
            telegram_task.cancel()
        if health_task:
            health_task.cancel()
        await shutdown_services()


def entrypoint() -> None:
    """Sync entrypoint for python -m app.main."""
    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()
