"""
Bittensor Subnet Alpha Trading Bot – entrypoint.

Runs the scheduler (every 15 min), health endpoint, and orchestrates
the scan → score → trade cycle.
"""
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
from app.notifications.telegram import send_alert
from app.portfolio.manager import PortfolioManager
from app.portfolio.fast_manager import FastPortfolioManager
from app.storage.db import Database
from app.utils.time import utc_iso

# ── Globals ────────────────────────────────────────────────────────
db: Database | None = None
rpc: FlameWireRPC | None = None
executor: SwapExecutor | None = None
taostats: TaostatsClient | None = None
portfolio: PortfolioManager | None = None
fast_portfolio: FastPortfolioManager | None = None
scheduler: AsyncIOScheduler | None = None
_shutdown_event: asyncio.Event | None = None


async def init_services() -> None:
    """Initialize all service components."""
    global db, rpc, executor, taostats, portfolio, fast_portfolio

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

    # Fast (scalp) trading manager
    if settings.FAST_TRADING_ENABLED:
        fast_portfolio = FastPortfolioManager(db, executor, taostats)
        await fast_portfolio.initialize()

    logger.info(
        "All services initialized",
        data={
            "dry_run": settings.DRY_RUN,
            "scan_interval_min": settings.SCAN_INTERVAL_MIN,
            "num_slots": settings.NUM_SLOTS,
            "fast_trading": settings.FAST_TRADING_ENABLED,
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
    """Execute one main scan-decide-trade cycle."""
    if portfolio is None:
        logger.error("Portfolio manager not initialized")
        return

    # Check kill switch
    if os.path.exists(settings.KILL_SWITCH_PATH):
        logger.critical("KILL_SWITCH detected – stopping scheduler")
        await send_alert("⚠️ <b>KILL_SWITCH</b> activated — trading halted")
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
        if _shutdown_event:
            _shutdown_event.set()
        return

    try:
        logger.info(f"═══ Starting scan cycle at {utc_iso()} ═══")
        summary = await portfolio.run_cycle()
        logger.info("═══ Cycle complete ═══", data=summary)
    except Exception as e:
        logger.error(f"Cycle failed: {e}", data={"error": str(e)})


async def run_fast_cycle() -> None:
    """Execute one fast (scalp) trading cycle."""
    if fast_portfolio is None:
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return
    try:
        logger.info(f"⚡ Starting fast cycle at {utc_iso()}")
        summary = await fast_portfolio.run_cycle()
        logger.info("⚡ Fast cycle complete", data=summary)
    except Exception as e:
        logger.error(f"Fast cycle failed: {e}", data={"error": str(e)})


def setup_scheduler() -> AsyncIOScheduler:
    """Configure APScheduler for main and fast scan cycles."""
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
    if settings.FAST_TRADING_ENABLED:
        sched.add_job(
            run_fast_cycle,
            trigger="interval",
            minutes=settings.FAST_TRADING_SCAN_MIN,
            id="fast_scan_cycle",
            name="Fast Scalp Scanner",
            max_instances=1,
            misfire_grace_time=30,
        )
    return sched


# ── FastAPI health + dashboard API ─────────────────────────────────

def create_health_app():
    """Create a FastAPI app with /health and dashboard API endpoints."""
    try:
        import json as _json
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel

        class BacktestRequest(BaseModel):
            days: int = 7
            nav: float = settings.DRY_RUN_STARTING_TAO
            top_n: int = 20

        app = FastAPI(title="SubnetTrader", docs_url="/docs", redoc_url=None)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:3000",
                "http://raspberrypi.local:3000",
                "http://0.0.0.0:3000",
            ],
            allow_methods=["*"],
            allow_headers=["*"],
        )

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

        @app.get("/api/portfolio")
        async def api_portfolio():
            if portfolio is None:
                raise HTTPException(status_code=503, detail="Portfolio not initialized")
            status = portfolio.status()
            nav = 0.0
            if db is not None and executor is not None:
                try:
                    from app.utils.time import today_midnight_utc
                    today = today_midnight_utc().strftime("%Y-%m-%d")
                    nav_row = await db.get_daily_nav(today)
                    if nav_row:
                        nav = nav_row["nav_tao"]
                    else:
                        tao_balance = await executor.get_tao_balance()
                        pos_value = sum(
                            s["amount_tao"] for s in status["slots"] if s["status"] == "ALPHA"
                        )
                        nav = tao_balance + pos_value
                except Exception:
                    pass
            return JSONResponse(content={
                "portfolio": status,
                "nav_tao": nav,
                "timestamp": utc_iso(),
            })

        @app.get("/api/positions")
        async def api_positions():
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")
            rows = await db.fetchall(
                "SELECT * FROM positions ORDER BY entry_ts DESC LIMIT 200"
            )
            return JSONResponse(content={"positions": rows})

        @app.get("/api/nav")
        async def api_nav():
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")
            rows = await db.fetchall(
                "SELECT * FROM daily_nav ORDER BY date ASC"
            )
            return JSONResponse(content={"nav": rows})

        @app.get("/api/signals/latest")
        async def api_signals_latest():
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")
            latest = await db.fetchone(
                "SELECT scan_ts FROM signals ORDER BY id DESC LIMIT 1"
            )
            if not latest:
                return JSONResponse(content={"signals": [], "scan_ts": None})
            scan_ts = latest["scan_ts"]
            rows = await db.fetchall(
                "SELECT * FROM signals WHERE scan_ts = ? ORDER BY rank ASC",
                (scan_ts,),
            )
            return JSONResponse(content={"signals": rows, "scan_ts": scan_ts})

        @app.post("/api/backtest")
        async def api_backtest(req: BacktestRequest):
            out_path = "/tmp/bt_result.json"
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "tools/backtest.py",
                "--days", str(req.days),
                "--nav", str(req.nav),
                "--top-n", str(req.top_n),
                "--output", out_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Backtest failed: {stderr.decode()[:500]}",
                )
            try:
                with open(out_path) as f:
                    result = _json.load(f)
                return JSONResponse(content=result)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Could not read result: {e}")

        @app.get("/api/subnets")
        async def api_subnets():
            if taostats is None:
                raise HTTPException(status_code=503, detail="Taostats not initialized")
            items = await taostats.get_subnets()
            result = []
            for s in items:
                try:
                    netuid = int(s.get("netuid", -1))
                    if netuid < 0:
                        continue
                    price = float(s.get("price", 0) or 0)
                    seven_day = s.get("seven_day_prices", [])
                    history = []
                    for entry in seven_day:
                        if isinstance(entry, dict):
                            p = entry.get("price")
                            ts = entry.get("timestamp", "")
                            if p is not None:
                                history.append({"t": ts, "p": float(p)})
                    result.append({
                        "netuid": netuid,
                        "name": s.get("name", ""),
                        "symbol": s.get("symbol", ""),
                        "price": price,
                        "change_1h": float(s.get("price_change_1_hour", 0) or 0),
                        "change_24h": float(s.get("price_change_1_day", 0) or 0),
                        "change_7d": float(s.get("price_change_1_week", 0) or 0),
                        "volume_24h": float(s.get("tao_volume_24_hr", 0) or 0),
                        "market_cap": float(s.get("market_cap", 0) or 0),
                        "total_tao": float(s.get("total_tao", 0) or 0),
                        "buys_24h": int(s.get("buys_24_hr", 0) or 0),
                        "sells_24h": int(s.get("sells_24_hr", 0) or 0),
                        "history": history,
                    })
                except (ValueError, TypeError):
                    continue
            result.sort(key=lambda x: x["total_tao"], reverse=True)
            return JSONResponse(content={"subnets": result, "count": len(result)})

        @app.post("/api/positions/{position_id}/close")
        async def api_close_position(position_id: int):
            result = None
            if portfolio:
                result = await portfolio.manual_close(position_id)
            if result is None and fast_portfolio:
                result = await fast_portfolio.manual_close(position_id)
            if result is None:
                raise HTTPException(status_code=404, detail="Position not found in any active slot")
            return JSONResponse(content={"success": True, "result": result})

        @app.get("/api/fast-portfolio")
        async def api_fast_portfolio():
            if fast_portfolio is None:
                return JSONResponse(content={"enabled": False})
            return JSONResponse(content={
                "fast_portfolio": fast_portfolio.status(),
                "timestamp": utc_iso(),
            })

        @app.get("/api/subnets/{netuid}/history")
        async def api_subnet_history(netuid: int):
            if taostats is None:
                raise HTTPException(status_code=503, detail="Taostats not initialized")
            # Try pool_snapshot first (already fetched this cycle)
            snapshot = taostats._pool_snapshot.get(netuid, {})
            seven_day = snapshot.get("seven_day_prices", [])
            history = []
            for entry in seven_day:
                if isinstance(entry, dict):
                    p = entry.get("price")
                    ts = entry.get("timestamp", "")
                    if p is not None:
                        history.append({"t": ts, "p": float(p)})
            if not history:
                # Fallback to history endpoint
                raw = await taostats.get_price_history(netuid, limit=200)
                for entry in raw:
                    if isinstance(entry, dict):
                        p = entry.get("price")
                        ts = entry.get("timestamp", "")
                        if p is not None:
                            history.append({"t": ts, "p": float(p)})
            price = float(snapshot.get("price", 0) or 0)
            name = snapshot.get("name", f"Subnet {netuid}")
            return JSONResponse(content={
                "netuid": netuid,
                "name": name,
                "price": price,
                "history": history,
            })

        # ── Logs endpoint ───────────────────────────────────────────

        @app.get("/api/logs")
        async def api_logs(lines: int = 150):
            import glob, json as _j
            log_dir = settings.JSONL_DIR
            files = sorted(glob.glob(f"{log_dir}/*.jsonl"))
            entries: list[dict] = []
            for fpath in reversed(files[-2:]):  # today + yesterday
                try:
                    with open(fpath) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    entries.append(_j.loads(line))
                                except Exception:
                                    entries.append({"level": "RAW", "message": line})
                except OSError:
                    pass
            return JSONResponse(content={"logs": entries[-lines:]})

        # ── Anomalies endpoint ──────────────────────────────────────

        @app.get("/api/anomalies")
        async def api_anomalies():
            if taostats is None:
                return JSONResponse(content={"anomalies": []})
            snapshot = taostats._pool_snapshot
            anomalies = []
            for netuid, s in snapshot.items():
                seven_day = s.get("seven_day_prices", [])
                prices = [float(e["price"]) for e in seven_day if e.get("price")]
                if len(prices) < 7:
                    continue
                cur = prices[-1]
                p4h = prices[-2]
                p24h = prices[-7]
                chg_4h = (cur - p4h) / p4h * 100 if p4h else 0.0
                chg_24h = (cur - p24h) / p24h * 100 if p24h else 0.0
                if abs(chg_4h) >= 15 or abs(chg_24h) >= 25:
                    anomalies.append({
                        "netuid": netuid,
                        "name": s.get("name", ""),
                        "price": cur,
                        "chg_4h": round(chg_4h, 2),
                        "chg_24h": round(chg_24h, 2),
                        "magnitude": max(abs(chg_4h), abs(chg_24h)),
                    })
            anomalies.sort(key=lambda x: x["magnitude"], reverse=True)
            return JSONResponse(content={"anomalies": anomalies[:20]})

        # ── Correlations endpoint ───────────────────────────────────

        @app.get("/api/correlations")
        async def api_correlations():
            import math as _math
            if taostats is None:
                return JSONResponse(content={"netuids": [], "names": {}, "matrix": {}})
            snapshot = taostats._pool_snapshot
            subnet_list = sorted(
                snapshot.values(),
                key=lambda s: float(s.get("total_tao", 0) or 0),
                reverse=True,
            )[:50]
            subnet_data_c = []
            for s in subnet_list:
                netuid_c = s.get("netuid")
                if netuid_c is None:
                    continue
                prices_c = [
                    float(e["price"])
                    for e in s.get("seven_day_prices", [])
                    if e.get("price")
                ]
                if len(prices_c) >= 10:
                    subnet_data_c.append({
                        "netuid": int(netuid_c),
                        "name": s.get("name", ""),
                        "prices": prices_c,
                    })
            series_c = {d["netuid"]: d["prices"] for d in subnet_data_c}
            netuids_c = [d["netuid"] for d in subnet_data_c]
            names_c = {str(d["netuid"]): d["name"] for d in subnet_data_c}
            matrix_c: dict[str, float] = {}
            for i, a in enumerate(netuids_c):
                for b in netuids_c[i + 1:]:
                    xs, ys = series_c[a], series_c[b]
                    n = min(len(xs), len(ys))
                    xs, ys = xs[-n:], ys[-n:]
                    mx, my = sum(xs) / n, sum(ys) / n
                    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                    da = _math.sqrt(sum((x - mx) ** 2 for x in xs))
                    db = _math.sqrt(sum((y - my) ** 2 for y in ys))
                    if da > 0 and db > 0:
                        matrix_c[f"{a},{b}"] = round(num / (da * db), 3)
            return JSONResponse(content={
                "netuids": netuids_c,
                "names": names_c,
                "matrix": matrix_c,
                "threshold": settings.CORRELATION_THRESHOLD,
            })

        # ── Signal weights endpoints ────────────────────────────────

        @app.get("/api/settings/weights")
        async def api_get_weights():
            return JSONResponse(content={
                "w_trend": settings.W_TREND,
                "w_support_resistance": settings.W_SUPPORT_RESISTANCE,
                "w_fibonacci": settings.W_FIBONACCI,
                "w_volatility": settings.W_VOLATILITY,
                "w_mean_reversion": settings.W_MEAN_REVERSION,
                "w_value_band": settings.W_VALUE_BAND,
                "w_dereg": settings.W_DEREG,
            })

        @app.post("/api/settings/weights")
        async def api_set_weights(request: Request):
            body = await request.json()
            settings.W_TREND = float(body.get("w_trend", settings.W_TREND))
            settings.W_SUPPORT_RESISTANCE = float(body.get("w_support_resistance", settings.W_SUPPORT_RESISTANCE))
            settings.W_FIBONACCI = float(body.get("w_fibonacci", settings.W_FIBONACCI))
            settings.W_VOLATILITY = float(body.get("w_volatility", settings.W_VOLATILITY))
            settings.W_MEAN_REVERSION = float(body.get("w_mean_reversion", settings.W_MEAN_REVERSION))
            settings.W_VALUE_BAND = float(body.get("w_value_band", settings.W_VALUE_BAND))
            settings.W_DEREG = float(body.get("w_dereg", settings.W_DEREG))
            return JSONResponse(content={"applied": True})

        @app.get("/api/settings/weights/preview")
        async def api_preview_weights(
            w_trend: float = 0.20,
            w_support_resistance: float = 0.15,
            w_fibonacci: float = 0.10,
            w_volatility: float = 0.20,
            w_mean_reversion: float = 0.15,
            w_value_band: float = 0.10,
            w_dereg: float = 0.10,
        ):
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")
            latest = await db.fetchone(
                "SELECT scan_ts FROM signals ORDER BY id DESC LIMIT 1"
            )
            if not latest:
                return JSONResponse(content={"signals": []})
            rows = await db.fetchall(
                "SELECT * FROM signals WHERE scan_ts = ? ORDER BY rank ASC",
                (latest["scan_ts"],),
            )
            weights = [
                w_trend, w_support_resistance, w_fibonacci,
                w_volatility, w_mean_reversion, w_value_band, w_dereg,
            ]
            total_w = sum(weights) or 1.0
            results = []
            for r in rows:
                raw = [
                    r["trend"], r["support_resist"], r["fibonacci"],
                    r["volatility"], r["mean_reversion"], r["value_band"],
                    r.get("dereg", 0.0),
                ]
                composite = sum(w * v for w, v in zip(weights, raw)) / total_w
                results.append({
                    "netuid": r["netuid"],
                    "composite": round(composite, 4),
                    "old_rank": r["rank"],
                })
            results.sort(key=lambda x: x["composite"], reverse=True)
            for i, row in enumerate(results):
                row["new_rank"] = i + 1
                row["rank_delta"] = row["old_rank"] - row["new_rank"]
            return JSONResponse(content={"signals": results, "scan_ts": latest["scan_ts"]})

        # ── Control endpoints ───────────────────────────────────────

        @app.get("/api/control/status")
        async def api_control_status():
            ks = os.path.exists(settings.KILL_SWITCH_PATH)
            next_run = None
            if scheduler:
                job = scheduler.get_job("scan_cycle")
                if job and job.next_run_time:
                    next_run = job.next_run_time.isoformat()
            return JSONResponse(content={
                "kill_switch_active": ks,
                "fast_trading_enabled": settings.FAST_TRADING_ENABLED,
                "scheduler_running": scheduler.running if scheduler else False,
                "next_cycle": next_run,
                "dry_run": settings.DRY_RUN,
            })

        @app.post("/api/control/pause")
        async def api_control_pause():
            with open(settings.KILL_SWITCH_PATH, "w") as f:
                f.write(utc_iso())
            return JSONResponse(content={"paused": True})

        @app.post("/api/control/resume")
        async def api_control_resume():
            if os.path.exists(settings.KILL_SWITCH_PATH):
                os.remove(settings.KILL_SWITCH_PATH)
            return JSONResponse(content={"paused": False})

        @app.post("/api/control/run-cycle")
        async def api_run_cycle():
            if portfolio is None:
                raise HTTPException(status_code=503, detail="Portfolio not initialized")
            asyncio.create_task(run_cycle())
            return JSONResponse(content={"triggered": True})

        @app.post("/api/control/run-fast-cycle")
        async def api_run_fast_cycle():
            if fast_portfolio is None:
                raise HTTPException(status_code=503, detail="Fast portfolio not initialized")
            asyncio.create_task(run_fast_cycle())
            return JSONResponse(content={"triggered": True})

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
        if settings.FAST_TRADING_ENABLED:
            await run_fast_cycle()

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
