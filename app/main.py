"""
Bittensor Subnet Alpha Trading Bot – entrypoint.

Runs the scheduler (every 15 min), health endpoint, and orchestrates
the scan → score → trade cycle.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.chain.flamewire_rpc import FlameWireRPC
from app.chain.executor import SwapExecutor
from app.data.taostats_client import TaostatsClient
from app.logging.logger import logger
from app.portfolio.manager import PortfolioManager
from app.storage.db import Database
from app.utils.time import utc_iso

# ── Globals ────────────────────────────────────────────────────────
db: Database | None = None
rpc: FlameWireRPC | None = None
executor: SwapExecutor | None = None
taostats: TaostatsClient | None = None
portfolio: PortfolioManager | None = None
scheduler: AsyncIOScheduler | None = None
_shutdown_event: asyncio.Event | None = None


async def init_services() -> None:
    """Initialize all service components."""
    global db, rpc, executor, taostats, portfolio

    logger.info("Initializing services…")

    # Database
    db = Database()
    await db.connect()

    # FlameWire RPC
    rpc = FlameWireRPC()
    health_ok = await rpc.health_check()
    if health_ok:
        logger.info("FlameWire RPC health check passed")
    else:
        logger.warning("FlameWire RPC health check failed – continuing in degraded mode")

    # Swap executor
    executor = SwapExecutor(rpc)
    await executor.initialize()

    # Taostats client
    taostats = TaostatsClient()

    # Portfolio manager
    portfolio = PortfolioManager(db, executor, taostats)
    await portfolio.initialize()

    logger.info(
        "All services initialized",
        data={
            "dry_run": settings.DRY_RUN,
            "scan_interval_min": settings.SCAN_INTERVAL_MIN,
            "num_slots": settings.NUM_SLOTS,
        },
    )


async def shutdown_services() -> None:
    """Gracefully shut down all services."""
    logger.info("Shutting down services…")

    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    if taostats:
        await taostats.close()
    if rpc:
        await rpc.close()
    if db:
        await db.close()

    logger.info("Services shut down cleanly.")


async def run_cycle() -> None:
    """Execute one scan-decide-trade cycle."""
    if portfolio is None:
        logger.error("Portfolio manager not initialized")
        return

    # Check kill switch
    if os.path.exists(settings.KILL_SWITCH_PATH):
        logger.critical("KILL_SWITCH detected – stopping scheduler")
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
        if _shutdown_event:
            _shutdown_event.set()
        return

    try:
        logger.info(f"═══ Starting scan cycle at {utc_iso()} ═══")
        summary = await portfolio.run_cycle()
        logger.info(
            f"═══ Cycle complete ═══",
            data=summary,
        )
    except Exception as e:
        logger.error(f"Cycle failed: {e}", data={"error": str(e)})


def setup_scheduler() -> AsyncIOScheduler:
    """Configure APScheduler for periodic scan cycles."""
    sched = AsyncIOScheduler()
    sched.add_job(
        run_cycle,
        trigger="interval",
        minutes=settings.SCAN_INTERVAL_MIN,
        id="scan_cycle",
        name="Subnet Alpha Scanner",
        max_instances=1,
        misfire_grace_time=60,
    )
    return sched


# ── FastAPI health endpoint (optional) ─────────────────────────────

def create_health_app():
    """Create a minimal FastAPI app with /health endpoint."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        app = FastAPI(title="SubnetTrader", docs_url=None, redoc_url=None)

        @app.get("/health")
        async def health():
            status = "ok" if portfolio else "initializing"
            data = {
                "status": status,
                "dry_run": settings.DRY_RUN,
                "timestamp": utc_iso(),
            }
            if portfolio:
                data["portfolio"] = portfolio.status()
            return JSONResponse(content=data)

        return app
    except ImportError:
        return None


async def run_health_server() -> None:
    """Run the FastAPI health endpoint in the background."""
    app = create_health_app()
    if app is None:
        logger.info("FastAPI not available – health endpoint disabled")
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
        logger.info("uvicorn not available – health endpoint disabled")
    except Exception as e:
        logger.error(f"Health server error: {e}")


# ── CSV export command ─────────────────────────────────────────────

async def export_csvs() -> None:
    """Export fills and positions to CSV files."""
    database = Database()
    await database.connect()

    fills_path = await database.export_fills_csv("data/exports/fills.csv")
    positions_path = await database.export_positions_csv("data/exports/positions.csv")

    print(f"Exported fills to: {fills_path}")
    print(f"Exported positions to: {positions_path}")

    await database.close()


# ── Main ───────────────────────────────────────────────────────────

async def main() -> None:
    """Main async entrypoint."""
    global scheduler, _shutdown_event

    # Handle CLI commands
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "export":
            await export_csvs()
            return
        elif cmd == "health":
            # Just run health check
            rpc_client = FlameWireRPC()
            ok = await rpc_client.health_check()
            print(f"RPC Health: {'OK' if ok else 'FAILED'}")
            await rpc_client.close()
            return

    _shutdown_event = asyncio.Event()

    # Signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        _shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await init_services()

        # Set up scheduler
        scheduler = setup_scheduler()
        scheduler.start()
        logger.info(
            f"Scheduler started: scanning every {settings.SCAN_INTERVAL_MIN} minutes"
        )

        # Run initial cycle immediately
        await run_cycle()

        # Start health server in background
        health_task = asyncio.create_task(run_health_server())

        # Wait until shutdown
        await _shutdown_event.wait()

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
    finally:
        await shutdown_services()


def entrypoint() -> None:
    """Sync entrypoint for __main__."""
    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()
