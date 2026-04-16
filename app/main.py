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
from app.config import settings, strategy_a_config, strategy_b_config
from app.data.taostats_client import TaostatsClient
from app.logging.logger import logger
from app.notifications.telegram import (
    TelegramBot,
    TelegramCommandHandlers,
    TelegramDocument,
    send_alert,
)
from app.portfolio.ema_manager import EmaManager
from app.portfolio.pot_sizer import compute_pots
from app.storage.db import Database
from app.utils.time import utc_iso

db: Database | None = None
executor: SwapExecutor | None = None
taostats: TaostatsClient | None = None
ema_scalper: EmaManager | None = None
ema_trend: EmaManager | None = None
scheduler: AsyncIOScheduler | None = None
telegram_bot: TelegramBot | None = None
_shutdown_event: asyncio.Event | None = None
_scalper_exit_watch_status: dict[str, object | None] = {
    "last_run": None, "last_error": None, "last_exit_count": 0,
}
_trend_exit_watch_status: dict[str, object | None] = {
    "last_run": None, "last_error": None, "last_exit_count": 0,
}
_scalper_entry_watch_status: dict[str, object | None] = {
    "last_run": None, "last_error": None, "last_crossover_count": 0,
}
_trend_entry_watch_status: dict[str, object | None] = {
    "last_run": None, "last_error": None, "last_crossover_count": 0,
}
_ema_cycle_running: bool = False


async def _get_open_netuids(mgr: EmaManager) -> set[int]:
    """Return netuids currently held by a strategy manager."""
    return {p.netuid for p in await mgr._open_positions_snapshot()}
_last_wallet_balance_tao: float | None = None


async def _apply_pot_sizing() -> tuple[float, float, float | None]:
    """Refresh wallet balance and mutate each manager's ``pot_tao`` per
    ``EMA_POT_MODE``. Returns ``(pot_a, pot_b, wallet_balance)``.

    On wallet read failure in ``wallet_split`` mode, falls back to the
    previous cycle's pot rather than zeroing mid-trade.
    """
    global _last_wallet_balance_tao
    wallet_balance: float | None = None
    if executor is not None:
        try:
            raw = float(await executor.get_tao_balance())
            # get_tao_balance returns 0.0 when both SDK and Subtensor
            # fallback fail (instead of raising).  Treat zero as a failed
            # read so we don't poison pot_tao and false-trip the breaker.
            if raw > 0:
                wallet_balance = raw
                _last_wallet_balance_tao = wallet_balance
            else:
                logger.warning("Wallet balance returned 0 — treating as failed read")
                wallet_balance = _last_wallet_balance_tao
        except Exception as exc:
            logger.warning(f"Wallet balance read failed: {exc}")
            wallet_balance = _last_wallet_balance_tao

    pot_a, pot_b = compute_pots(wallet_balance, settings)

    # Sentinel: wallet_split with no balance available — keep existing pots.
    if pot_a < 0 and pot_b < 0:
        pot_a = ema_scalper._cfg.pot_tao if ema_scalper else 0.0
        pot_b = ema_trend._cfg.pot_tao if ema_trend else 0.0

    if ema_scalper is not None:
        ema_scalper._cfg.pot_tao = pot_a
    if ema_trend is not None:
        ema_trend._cfg.pot_tao = pot_b

    return pot_a, pot_b, wallet_balance
_tao_usd_cache: dict[str, float | None] = {"price": None, "fetched_at": 0.0}


async def init_services() -> None:
    """Initialize the EMA runtime and its shared infrastructure."""
    global db, executor, taostats, ema_scalper, ema_trend

    active = []
    if settings.EMA_ENABLED:
        active.append("A")
    if settings.EMA_B_ENABLED:
        active.append("B")
    logger.info(
        f"Initializing EMA services — active strategies: {active or ['NONE']}"
    )
    if not active:
        logger.warning("Both EMA_ENABLED and EMA_B_ENABLED are false — no strategies will run")

    prune_old_logs()

    db = Database()
    await db.connect()

    executor = SwapExecutor()
    await executor.initialize()

    taostats = TaostatsClient()

    if settings.EMA_ENABLED:
        ema_scalper = EmaManager(db, executor, taostats, strategy_a_config())
        await ema_scalper.initialize()

    if settings.EMA_B_ENABLED:
        ema_trend = EmaManager(db, executor, taostats, strategy_b_config())
        await ema_trend.initialize()

    # Wire companion exit callbacks so whichever strategy exits a dual-held
    # subnet can immediately close the other strategy's ghost position.
    if ema_scalper and ema_trend:
        ema_scalper._companion_exit_cb = ema_trend.on_companion_exit
        ema_trend._companion_exit_cb = ema_scalper.on_companion_exit
        # Wire companion netuid callbacks for entry watcher cross-exclusion
        ema_scalper._companion_netuids_cb = (
            lambda: _get_open_netuids(ema_trend)
        )
        ema_trend._companion_netuids_cb = (
            lambda: _get_open_netuids(ema_scalper)
        )

    # Set root claim type to "Keep" so emissions accumulate as alpha
    if not settings.EMA_DRY_RUN:
        await executor.ensure_root_claim_keep()

    logger.info(
        "EMA services initialized",
        data={
            "scalper_enabled": settings.EMA_ENABLED,
            "scalper_dry_run": settings.EMA_DRY_RUN,
            "trend_enabled": settings.EMA_B_ENABLED,
            "trend_dry_run": settings.EMA_B_DRY_RUN,
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
    if db:
        await db.close()

    logger.info("Services shut down cleanly")


async def run_ema_cycle() -> None:
    """Execute one EMA scan and trade cycle for both strategies."""
    global _ema_cycle_running
    if ema_scalper is None and ema_trend is None:
        logger.warning("No EMA managers initialized")
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        logger.warning("KILL_SWITCH active; skipping EMA cycle")
        return
    if _ema_cycle_running:
        logger.warning("EMA cycle already in progress; skipping")
        return

    _ema_cycle_running = True
    try:
        logger.info(f"Starting EMA dual cycle at {utc_iso()}")
        await _apply_pot_sizing()
        # Collect occupied netuids from both strategies for cross-exclusion
        scalper_netuids: set[int] = set()
        trend_netuids: set[int] = set()
        if ema_scalper:
            scalper_netuids = {p.netuid for p in await ema_scalper._open_positions_snapshot()}
        if ema_trend:
            trend_netuids = {p.netuid for p in await ema_trend._open_positions_snapshot()}

        if ema_scalper:
            summary = await ema_scalper.run_cycle(globally_occupied=trend_netuids)
            logger.info("Scalper cycle complete", data=summary)
            # Re-snapshot after scalper may have entered new positions
            scalper_netuids = {p.netuid for p in await ema_scalper._open_positions_snapshot()}
        if ema_trend:
            summary = await ema_trend.run_cycle(globally_occupied=scalper_netuids)
            logger.info("Trend cycle complete", data=summary)
    except Exception as exc:
        logger.error(f"EMA cycle failed: {exc}", data={"error": str(exc)})
    finally:
        _ema_cycle_running = False


async def _detect_dual_held_netuids() -> set[int]:
    """Return netuids held by both the scalper and trend strategies."""
    if not ema_scalper or not ema_trend:
        return set()
    scalper = {p.netuid for p in await ema_scalper._open_positions_snapshot()}
    trend = {p.netuid for p in await ema_trend._open_positions_snapshot()}
    overlap = scalper & trend
    if overlap:
        logger.warning(f"Dual-held subnets detected: {overlap}")
    return overlap


async def run_scalper_exit_watch() -> None:
    """Execute the lightweight exit watcher for the Scalper strategy."""
    global _scalper_exit_watch_status
    if ema_scalper is None or not settings.EMA_EXIT_WATCHER_ENABLED:
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return
    try:
        dual = await _detect_dual_held_netuids()
        summary = await ema_scalper.run_price_exit_watch(dual_held_netuids=dual)
        _scalper_exit_watch_status = {
            "last_run": utc_iso(), "last_error": None,
            "last_exit_count": len(summary.get("exits", [])),
        }
        if summary.get("exits") or summary.get("deferred"):
            logger.info("Scalper exit watcher complete", data=summary)
    except Exception as exc:
        _scalper_exit_watch_status = {
            "last_run": utc_iso(), "last_error": str(exc), "last_exit_count": 0,
        }
        logger.error(f"Scalper exit watcher failed: {exc}", data={"error": str(exc)})


async def run_trend_exit_watch() -> None:
    """Execute the lightweight exit watcher for the Trend strategy."""
    global _trend_exit_watch_status
    if ema_trend is None or not settings.EMA_EXIT_WATCHER_ENABLED:
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return
    try:
        dual = await _detect_dual_held_netuids()
        summary = await ema_trend.run_price_exit_watch(dual_held_netuids=dual)
        _trend_exit_watch_status = {
            "last_run": utc_iso(), "last_error": None,
            "last_exit_count": len(summary.get("exits", [])),
        }
        if summary.get("exits") or summary.get("deferred"):
            logger.info("Trend exit watcher complete", data=summary)
    except Exception as exc:
        _trend_exit_watch_status = {
            "last_run": utc_iso(), "last_error": str(exc), "last_exit_count": 0,
        }
        logger.error(f"Trend exit watcher failed: {exc}", data={"error": str(exc)})


async def run_scalper_entry_watch() -> None:
    """Lightweight entry crossover poll for the Scalper strategy."""
    global _scalper_entry_watch_status
    if ema_scalper is None or not settings.EMA_ENTRY_WATCHER_ENABLED:
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return
    if _ema_cycle_running:
        return
    try:
        summary = await ema_scalper.run_entry_watch()
        _scalper_entry_watch_status = {
            "last_run": utc_iso(), "last_error": None,
            "last_crossover_count": len(summary.get("new_crossovers", [])),
        }
        if summary.get("new_crossovers"):
            logger.info("Scalper entry watcher: crossovers triggered cycle", data=summary)
    except Exception as exc:
        _scalper_entry_watch_status = {
            "last_run": utc_iso(), "last_error": str(exc), "last_crossover_count": 0,
        }
        logger.error(f"Scalper entry watcher failed: {exc}", data={"error": str(exc)})


async def run_trend_entry_watch() -> None:
    """Lightweight entry crossover poll for the Trend strategy."""
    global _trend_entry_watch_status
    if ema_trend is None or not settings.EMA_ENTRY_WATCHER_ENABLED:
        return
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return
    if _ema_cycle_running:
        return
    try:
        summary = await ema_trend.run_entry_watch()
        _trend_entry_watch_status = {
            "last_run": utc_iso(), "last_error": None,
            "last_crossover_count": len(summary.get("new_crossovers", [])),
        }
        if summary.get("new_crossovers"):
            logger.info("Trend entry watcher: crossovers triggered cycle", data=summary)
    except Exception as exc:
        _trend_entry_watch_status = {
            "last_run": utc_iso(), "last_error": str(exc), "last_crossover_count": 0,
        }
        logger.error(f"Trend entry watcher failed: {exc}", data={"error": str(exc)})


async def run_root_claim() -> None:
    """Claim accumulated root emissions for all netuids with open positions."""
    if executor is None:
        return
    if settings.EMA_DRY_RUN and settings.EMA_B_DRY_RUN:
        return

    netuids: set[int] = set()
    if ema_scalper:
        netuids |= {p.netuid for p in await ema_scalper._open_positions_snapshot()}
    if ema_trend:
        netuids |= {p.netuid for p in await ema_trend._open_positions_snapshot()}

    if not netuids:
        logger.debug("Root claim: no open positions, skipping")
        return

    sorted_netuids = sorted(netuids)
    logger.info(f"Claiming root emissions for netuids {sorted_netuids}")
    ok = await executor.claim_root_emissions(sorted_netuids)
    if ok:
        logger.info(f"Root emissions claimed successfully for {sorted_netuids}")
    else:
        logger.warning(f"Root emission claim had failures for {sorted_netuids}")


def prune_old_logs() -> None:
    """Delete JSONL files in ``settings.JSONL_DIR`` older than
    ``LOG_RETENTION_DAYS``. Matches both ``YYYY-MM-DD.jsonl`` and rotated
    ``YYYY-MM-DD.jsonl.N`` files. Best-effort: errors are logged, never raised.
    """
    import re
    from datetime import datetime, timedelta, timezone

    directory = settings.JSONL_DIR
    days = max(0, int(settings.LOG_RETENTION_DAYS))
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(?:\.\d+)?$")
    removed = 0
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for name in entries:
        m = pattern.match(name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError as exc:
                logger.warning(f"Failed to prune log {name}: {exc}")
    if removed:
        logger.info(
            f"Pruned {removed} old log file(s) older than {days}d",
            data={"removed": removed, "retention_days": days},
        )


def setup_scheduler() -> AsyncIOScheduler:
    """Configure the EMA scheduler."""
    sched = AsyncIOScheduler()
    if settings.EMA_ENABLED or settings.EMA_B_ENABLED:
        sched.add_job(
            run_ema_cycle,
            trigger="interval",
            minutes=settings.SCAN_INTERVAL_MIN,
            id="ema_cycle",
            name="EMA Dual Scanner",
            max_instances=1,
            misfire_grace_time=60,
        )
    if settings.EMA_ENABLED and settings.EMA_EXIT_WATCHER_ENABLED:
        sched.add_job(
            run_scalper_exit_watch,
            trigger="interval",
            seconds=settings.EMA_EXIT_WATCHER_SEC,
            id="scalper_exit_watch",
            name="Scalper Exit Watcher",
            max_instances=1,
            misfire_grace_time=max(settings.EMA_EXIT_WATCHER_SEC, 5),
        )
    if settings.EMA_B_ENABLED and settings.EMA_EXIT_WATCHER_ENABLED:
        sched.add_job(
            run_trend_exit_watch,
            trigger="interval",
            seconds=settings.EMA_EXIT_WATCHER_SEC,
            id="trend_exit_watch",
            name="Trend Exit Watcher",
            max_instances=1,
            misfire_grace_time=max(settings.EMA_EXIT_WATCHER_SEC, 5),
        )
    if settings.EMA_ENABLED and settings.EMA_ENTRY_WATCHER_ENABLED:
        sched.add_job(
            run_scalper_entry_watch,
            trigger="interval",
            seconds=settings.EMA_ENTRY_WATCHER_SEC,
            id="scalper_entry_watch",
            name="Scalper Entry Watcher",
            max_instances=1,
            misfire_grace_time=max(settings.EMA_ENTRY_WATCHER_SEC, 5),
        )
    if settings.EMA_B_ENABLED and settings.EMA_ENTRY_WATCHER_ENABLED:
        sched.add_job(
            run_trend_entry_watch,
            trigger="interval",
            seconds=settings.EMA_ENTRY_WATCHER_SEC,
            id="trend_entry_watch",
            name="Trend Entry Watcher",
            max_instances=1,
            misfire_grace_time=max(settings.EMA_ENTRY_WATCHER_SEC, 5),
        )
    # Prune old JSONL logs daily (and once at startup, see init_services)
    sched.add_job(
        prune_old_logs,
        trigger="interval",
        hours=24,
        id="log_retention",
        name="Log Retention",
        max_instances=1,
        misfire_grace_time=3600,
    )
    # Claim root emissions every 6 hours for open positions
    if not (settings.EMA_DRY_RUN and settings.EMA_B_DRY_RUN):
        sched.add_job(
            run_root_claim,
            trigger="interval",
            hours=6,
            id="root_claim",
            name="Root Emission Claimer",
            max_instances=1,
            misfire_grace_time=300,
        )
    return sched


def _telegram_help_text() -> str:
    return (
        "🤖 <b>EMA Telegram Commands</b>\n\n"
        "<code>/status</code> — current EMA runtime status\n"
        "<code>/positions [limit]</code> — open EMA positions\n"
        "<code>/close &lt;netuid&gt;</code> — close a position (e.g. /close 32)\n"
        "<code>/history [limit]</code> — recent closed trades\n"
        "<code>/pause</code> — enable kill switch\n"
        "<code>/resume</code> — clear kill switch\n"
        "<code>/run</code> — trigger one EMA cycle\n"
        "<code>/export</code> — send the EMA trades CSV"
    )


async def _telegram_status_text() -> str:
    if ema_scalper is None and ema_trend is None:
        return "EMA runtime is still initializing."

    alpha_prices = await taostats.get_alpha_prices() if taostats else {}
    next_cycle = None
    if scheduler:
        job = scheduler.get_job("ema_cycle")
        if job and job.next_run_time:
            next_cycle = job.next_run_time.isoformat()

    lines = [
        "🤖 <b>EMA Dual Status</b>",
        f"Trading: {'PAUSED' if os.path.exists(settings.KILL_SWITCH_PATH) else 'RUNNING'}",
        f"Next cycle: {next_cycle or 'n/a'}",
    ]

    for label, mgr in [("Scalper", ema_scalper), ("Trend", ema_trend)]:
        if mgr is None:
            lines.append(f"\n<b>{label}</b>: disabled")
            continue
        summary = mgr.get_portfolio_summary(alpha_prices)
        mode = "LIVE" if not mgr._cfg.dry_run else "DRY"
        unrealized = sum(
            (p["current_price"] - p["entry_price"]) / p["entry_price"] * p["amount_tao"]
            if p["entry_price"] else 0.0
            for p in summary["open_positions"]
        )
        total_pnl = (summary["pot_tao"] - mgr._cfg.pot_tao) + unrealized
        lines.append(
            f"\n<b>{label} {mgr._cfg.fast_period}/{mgr._cfg.slow_period}</b> ({mode})"
        )
        lines.append(f"  Positions: {summary['open_count']}/{summary['max_positions']}")
        lines.append(f"  Pot: {summary['pot_tao']:.4f} τ | Deployed: {summary['deployed_tao']:.4f} τ")
        lines.append(f"  PnL: {total_pnl:+.4f} τ | Breaker: {'ACTIVE' if summary['breaker_active'] else 'off'}")

    if executor is not None:
        try:
            wallet_balance = await executor.get_tao_balance()
            lines.append(f"\nWallet: {wallet_balance:.4f} τ")
        except Exception:
            pass

    return "\n".join(lines)


async def _telegram_positions_text(limit: int) -> str:
    if ema_scalper is None and ema_trend is None:
        return "EMA runtime is still initializing."

    alpha_prices = await taostats.get_alpha_prices() if taostats else {}
    snapshot = taostats._pool_snapshot if taostats else {}
    limit = max(1, min(limit, 20))
    lines = ["📂 <b>Open EMA Positions</b>"]
    total = 0

    for label, mgr in [("SCL", ema_scalper), ("TRD", ema_trend)]:
        if mgr is None:
            continue
        summary = mgr.get_portfolio_summary(alpha_prices)
        positions = summary["open_positions"]
        total += len(positions)
        for pos in positions[:limit]:
            name = snapshot.get(pos["netuid"], {}).get("name", "") or f"SN{pos['netuid']}"
            lines.append(
                f"[{label}] #{pos['position_id']} {html.escape(name)} (SN{pos['netuid']}) | "
                f"{pos['pnl_pct']:+.2f}% | {pos['amount_tao']:.4f} τ | {pos['hours_held']:.1f}h"
            )

    if total == 0:
        lines.append("No open EMA positions.")
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
    if ema_scalper is None and ema_trend is None:
        return "EMA runtime is still initializing."
    if os.path.exists(settings.KILL_SWITCH_PATH):
        return "Kill switch is active. Use <code>/resume</code> before triggering a cycle."
    if _ema_cycle_running:
        return "⏳ <b>EMA cycle already running</b>\nWait for the current cycle to finish."

    asyncio.create_task(run_ema_cycle())
    return "🟢 <b>EMA cycle triggered</b>\nA manual scan has been queued."


async def _telegram_close_text(target: str) -> str:
    if ema_scalper is None and ema_trend is None:
        return "EMA runtime is still initializing."

    alpha_prices = await taostats.get_alpha_prices() if taostats else {}

    try:
        netuid = int(target.strip())
    except ValueError:
        return "Usage: <code>/close 32</code> (subnet number)"

    # Search both strategies for the position
    for mgr in [ema_scalper, ema_trend]:
        if mgr is None:
            continue
        summary = mgr.get_portfolio_summary(alpha_prices)
        selected = next((p for p in summary["open_positions"] if p["netuid"] == netuid), None)
        if selected:
            result = await mgr.manual_close(selected["position_id"])
            if result is None:
                return "That EMA position is already closing or no longer available."
            snapshot = taostats._pool_snapshot if taostats else {}
            name = snapshot.get(result["netuid"], {}).get("name", "") or f"SN{result['netuid']}"
            return (
                f"📉 <b>[{mgr._cfg.tag.upper()}] manual close</b>\n"
                f"{html.escape(name)} (SN{result['netuid']})\n"
                f"Reason: {result['reason']}\n"
                f"PnL: {result['pnl_pct']:+.2f}% ({result['pnl_tao']:+.4f} τ)"
            )

    return f"No open EMA position for SN{netuid}."


async def _telegram_history_text(limit: int) -> str:
    if db is None:
        return "Trade history is unavailable while the database is still initializing."

    rows = await db.get_closed_ema_positions(limit=limit)
    if not rows:
        return "📋 <b>Recent Closed Trades</b>\nNo closed EMA trades yet."

    snapshot = taostats._pool_snapshot if taostats else {}
    lines = ["📋 <b>Recent Closed Trades</b>"]
    for row in rows:
        name = snapshot.get(row["netuid"], {}).get("name", "") or f"SN{row['netuid']}"
        pnl_tao = row.get("pnl_tao") or 0.0
        pnl_pct = row.get("pnl_pct") or 0.0
        reason = row.get("exit_reason") or "?"
        exit_ts = (row.get("exit_ts") or "")[:16]
        lines.append(
            f"#{row['id']} {html.escape(name)} (SN{row['netuid']}) | "
            f"{pnl_pct:+.2f}% ({pnl_tao:+.4f} τ) | {reason} | {exit_ts}"
        )
    return "\n".join(lines)


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
            history=_telegram_history_text,
        )
    )


def create_health_app():
    """Create the FastAPI app for EMA health, control, and trading routes."""
    try:
        import time as time_module

        import httpx
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from starlette.responses import Response

        from app.config_api import router as config_router

        app = FastAPI(title="SubnetTrader EMA", docs_url=None, redoc_url=None)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        app.include_router(config_router)

        @app.get("/health")
        async def health():
            data: dict[str, object] = {
                "status": "ok" if (ema_scalper or ema_trend) else "initializing",
                "timestamp": utc_iso(),
                "scalper_enabled": settings.EMA_ENABLED,
                "trend_enabled": settings.EMA_B_ENABLED,
                "kill_switch_active": os.path.exists(settings.KILL_SWITCH_PATH),
            }
            if taostats:
                alpha_prices = await taostats.get_alpha_prices()
                if ema_scalper:
                    data["scalper"] = ema_scalper.get_portfolio_summary(alpha_prices)
                if ema_trend:
                    data["trend"] = ema_trend.get_portfolio_summary(alpha_prices)
            return JSONResponse(content=data)

        @app.get("/api/health/services")
        async def api_health_services():
            results = {}
            ts = utc_iso()

            # 1. Taostats
            try:
                taostats_key = settings.TAOSTATS_API_KEY or ""
                headers = {"Authorization": taostats_key} if taostats_key else {}
                t0 = time_module.monotonic()
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://api.taostats.io/api/dtao/pool/latest/v1?limit=1",
                        headers=headers,
                    )
                latency = int((time_module.monotonic() - t0) * 1000)
                results["taostats"] = {
                    "ok": r.status_code == 200,
                    "name": "Taostats API",
                    "detail": "200 OK" if r.status_code == 200
                        else f"HTTP {r.status_code}",
                    "last_check": ts,
                    "latency_ms": latency,
                }
            except Exception as exc:
                results["taostats"] = {
                    "ok": False,
                    "name": "Taostats API",
                    "detail": str(exc),
                    "last_check": ts,
                }

            # 3. Telegram
            tg_token = settings.TELEGRAM_BOT_TOKEN
            tg_chat = settings.TELEGRAM_CHAT_ID
            if tg_token and tg_chat:
                try:
                    t0 = time_module.monotonic()
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.get(f"https://api.telegram.org/bot{tg_token}/getMe")
                    latency = int((time_module.monotonic() - t0) * 1000)
                    if r.status_code == 200:
                        bot_info = r.json().get("result", {})
                        bot_name = bot_info.get("username", "unknown")
                        results["telegram"] = {
                            "ok": True,
                            "name": "Telegram Bot",
                            "detail": f"Bot @{bot_name} responding",
                            "last_check": ts,
                            "latency_ms": latency,
                        }
                    else:
                        results["telegram"] = {
                            "ok": False,
                            "name": "Telegram Bot",
                            "detail": f"HTTP {r.status_code} — invalid token",
                            "last_check": ts,
                        }
                except Exception as exc:
                    results["telegram"] = {
                        "ok": False,
                        "name": "Telegram Bot",
                        "detail": str(exc),
                        "last_check": ts,
                    }
            else:
                results["telegram"] = {
                    "ok": False,
                    "name": "Telegram Bot",
                    "detail": "Not configured"
                        + (" (missing token)" if not tg_token else "")
                        + (" (missing chat ID)" if not tg_chat else ""),
                    "last_check": ts,
                }

            # 4. Database
            try:
                if db:
                    count = await db.fetchone("SELECT COUNT(*) as n FROM ema_positions")
                    trade_count = count["n"] if count else 0
                    results["database"] = {
                        "ok": True,
                        "name": "SQLite Database",
                        "detail": f"WAL mode, {trade_count} trades",
                        "last_check": ts,
                    }
                else:
                    results["database"] = {
                        "ok": False,
                        "name": "SQLite Database",
                        "detail": "Not initialized",
                        "last_check": ts,
                    }
            except Exception as exc:
                results["database"] = {
                    "ok": False,
                    "name": "SQLite Database",
                    "detail": str(exc),
                    "last_check": ts,
                }

            # 5. Wallet
            try:
                if executor:
                    balance = await executor.get_tao_balance()
                    pot_a_live, pot_b_live = compute_pots(balance, settings)
                    if pot_a_live < 0:
                        pot_a_live = ema_scalper._cfg.pot_tao if ema_scalper else 0.0
                        pot_b_live = ema_trend._cfg.pot_tao if ema_trend else 0.0
                    pot = pot_a_live + pot_b_live
                    kill_switch = os.path.exists(settings.KILL_SWITCH_PATH)
                    scalper_breaker = ema_scalper.is_breaker_active if ema_scalper else False
                    trend_breaker = ema_trend.is_breaker_active if ema_trend else False
                    can_trade = (
                        not kill_switch
                        and not (scalper_breaker or trend_breaker)
                        and (not settings.EMA_DRY_RUN or not settings.EMA_B_DRY_RUN)
                    )
                    results["wallet"] = {
                        "ok": True,
                        "name": "Wallet",
                        "detail": f"{settings.BT_WALLET_NAME} — {balance:.4f} TAO",
                        "can_trade": can_trade,
                        "balance_tao": round(balance, 4),
                        "pot_tao": pot,
                        "last_check": ts,
                    }
                else:
                    results["wallet"] = {
                        "ok": False,
                        "name": "Wallet",
                        "detail": "Executor not initialized",
                        "last_check": ts,
                    }
            except Exception as exc:
                results["wallet"] = {
                    "ok": False,
                    "name": "Wallet",
                    "detail": f"Balance check failed: {exc}",
                    "last_check": ts,
                }

            return JSONResponse(content={"timestamp": ts, "services": results})

        @app.get("/api/control/status")
        async def api_control_status():
            next_run = None
            if scheduler:
                job = scheduler.get_job("ema_cycle")
                if job and job.next_run_time:
                    next_run = job.next_run_time.isoformat()

            alpha_prices = await taostats.get_alpha_prices() if taostats else {}
            scalper_summary = ema_scalper.get_portfolio_summary(alpha_prices) if ema_scalper else None
            trend_summary = ema_trend.get_portfolio_summary(alpha_prices) if ema_trend else None

            return JSONResponse(
                content={
                    "kill_switch_active": os.path.exists(settings.KILL_SWITCH_PATH),
                    "scheduler_running": scheduler.running if scheduler else False,
                    "next_cycle": next_run,
                    "scalper_enabled": settings.EMA_ENABLED,
                    "scalper_dry_run": settings.EMA_DRY_RUN,
                    "trend_enabled": settings.EMA_B_ENABLED,
                    "trend_dry_run": settings.EMA_B_DRY_RUN,
                    "exit_watcher_enabled": settings.EMA_EXIT_WATCHER_ENABLED,
                    "scalper_breaker_active": scalper_summary["breaker_active"] if scalper_summary else False,
                    "trend_breaker_active": trend_summary["breaker_active"] if trend_summary else False,
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
            if ema_scalper is None and ema_trend is None:
                raise HTTPException(status_code=503, detail="EMA not initialized")
            asyncio.create_task(run_ema_cycle())
            return JSONResponse(content={"triggered": True})

        @app.post("/api/control/reset-dry-run")
        async def api_reset_dry_run():
            if not settings.EMA_DRY_RUN and not settings.EMA_B_DRY_RUN:
                raise HTTPException(status_code=403, detail="Reset only allowed in DRY_RUN mode")
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            await db.clear_ema_history()
            if ema_scalper:
                await ema_scalper.initialize()
            if ema_trend:
                await ema_trend.initialize()

            logger.info("EMA dry-run data reset via control panel")
            return JSONResponse(content={"reset": True})

        @app.get("/api/ema/portfolio")
        async def api_ema_portfolio():
            alpha_prices = await taostats.get_alpha_prices() if taostats else {}
            snapshot = taostats._pool_snapshot if taostats else {}
            # Refresh pot sizing so summary reflects current mode/wallet state.
            try:
                await _apply_pot_sizing()
            except Exception as exc:
                logger.debug(f"Pot sizing refresh skipped: {exc}")
            result: dict = {}

            for key, mgr, exit_status in [
                ("scalper", ema_scalper, _scalper_exit_watch_status),
                ("trend", ema_trend, _trend_exit_watch_status),
            ]:
                if mgr is None:
                    result[key] = {"enabled": False}
                    continue
                summary = mgr.get_portfolio_summary(alpha_prices)
                for pos in summary["open_positions"]:
                    pos["name"] = snapshot.get(pos["netuid"], {}).get("name", "") or f"SN{pos['netuid']}"
                summary["enabled"] = True
                summary["dry_run"] = mgr._cfg.dry_run
                summary["confirm_bars"] = mgr._cfg.confirm_bars
                summary["signal_timeframe_hours"] = mgr._cfg.candle_timeframe_hours
                summary["stop_loss_pct"] = mgr._cfg.stop_loss_pct
                summary["take_profit_pct"] = mgr._cfg.take_profit_pct
                summary["trailing_stop_pct"] = mgr._cfg.trailing_stop_pct
                summary["exit_watcher"] = {
                    "enabled": settings.EMA_EXIT_WATCHER_ENABLED,
                    "interval_sec": settings.EMA_EXIT_WATCHER_SEC,
                    **exit_status,
                }
                result[key] = summary

            try:
                wallet_balance = round(await executor.get_tao_balance(), 6) if executor else None
            except Exception:
                wallet_balance = None

            s = result.get("scalper", {})
            t = result.get("trend", {})
            result["combined"] = {
                "total_pot": (s.get("pot_tao") or 0) + (t.get("pot_tao") or 0),
                "total_deployed": (s.get("deployed_tao") or 0) + (t.get("deployed_tao") or 0),
                "total_open": (s.get("open_count") or 0) + (t.get("open_count") or 0),
                "wallet_balance": wallet_balance,
                "pot_mode": settings.EMA_POT_MODE,
                "fee_reserve_tao": settings.EMA_FEE_RESERVE_TAO,
                "pot_weight": settings.EMA_POT_WEIGHT,
            }

            return JSONResponse(content=result)

        @app.get("/api/ema/positions")
        async def api_ema_positions(limit: int = 200, strategy: str | None = None):
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            rows = await db.get_ema_positions(limit=limit, strategy=strategy)
            snapshot = taostats._pool_snapshot if taostats else {}
            for row in rows:
                row["name"] = snapshot.get(row["netuid"], {}).get("name", "") or f"SN{row['netuid']}"
            return JSONResponse(content={"positions": rows})

        @app.get("/api/ema/recent-trades")
        async def api_ema_recent_trades(limit: int = 5, strategy: str | None = None):
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            rows = await db.get_closed_ema_positions(limit=limit, strategy=strategy)
            snapshot = taostats._pool_snapshot if taostats else {}
            for row in rows:
                row["name"] = snapshot.get(row["netuid"], {}).get("name", "") or f"SN{row['netuid']}"
            return JSONResponse(content={"trades": rows})

        @app.get("/api/ema/signals")
        async def api_ema_signals():
            if taostats is None:
                return JSONResponse(content={"signals": []})

            alpha_prices = await taostats.get_alpha_prices()
            snapshot = taostats._pool_snapshot

            from app.strategy.ema_signals import (
                bars_above_below_ema,
                build_sampled_candles,
                compute_ema,
                compute_mtf_signal,
                dual_ema_signal,
            )
            from app.strategy.indicators import (
                compute_bollinger_bands,
                compute_macd,
                compute_rsi,
            )
            from app.utils.math import rolling_volatility

            # Use scalper params as the primary signal display (fastest EMA)
            primary = ema_scalper or ema_trend
            fast_p = primary._cfg.fast_period if primary else 3
            slow_p = primary._cfg.slow_period if primary else 9
            confirm = primary._cfg.confirm_bars if primary else 3
            mtf_enabled = primary._cfg.mtf_enabled if primary else False
            mtf_lower_tf = primary._cfg.mtf_lower_tf_hours if primary else 1
            mtf_confirm = primary._cfg.mtf_confirm_bars if primary else 3

            # Collect open position netuids so they are always included
            open_netuids: set[int] = set()
            for mgr in [ema_scalper, ema_trend]:
                if mgr is not None:
                    for p in mgr.get_portfolio_summary(alpha_prices).get("open_positions", []):
                        open_netuids.add(p["netuid"])

            def _build_signal(netuid: int, snap_data: dict) -> dict | None:
                cur = alpha_prices.get(netuid, 0.0)
                if cur <= 0:
                    return None
                seven_day = snap_data.get("seven_day_prices", [])
                prices = [float(entry["price"]) for entry in seven_day if entry.get("price")]
                if not prices:
                    return None
                ema_vals = compute_ema(prices, slow_p)
                fast_ema_vals = compute_ema(prices, fast_p)
                sig_data: dict = {
                    "netuid": netuid,
                    "name": snap_data.get("name", "") or f"SN{netuid}",
                    "price": cur,
                    "ema": round(ema_vals[-1], 8) if ema_vals else 0.0,
                    "fast_ema": round(fast_ema_vals[-1], 8) if fast_ema_vals else 0.0,
                    "signal": dual_ema_signal(prices, fast_p, slow_p, confirm),
                    "bars": bars_above_below_ema(prices, slow_p),
                    "prices": [round(p, 8) for p in prices],
                    "ema_values": [round(v, 8) for v in ema_vals],
                    "fast_ema_values": [round(v, 8) for v in fast_ema_vals],
                }
                # Add per-strategy signals if both strategies are active
                if ema_scalper and ema_trend:
                    sig_data["signal_scalper"] = dual_ema_signal(
                        prices, ema_scalper._cfg.fast_period,
                        ema_scalper._cfg.slow_period, ema_scalper._cfg.confirm_bars,
                    )
                    sig_data["signal_trend"] = dual_ema_signal(
                        prices, ema_trend._cfg.fast_period,
                        ema_trend._cfg.slow_period, ema_trend._cfg.confirm_bars,
                    )
                # Multi-timeframe confirmation
                if mtf_enabled:
                    lower_candles = build_sampled_candles(seven_day, timeframe_hours=mtf_lower_tf)
                    mtf = compute_mtf_signal(lower_candles, fast_p, slow_p, mtf_confirm)
                    sig_data["mtf_lower_tf_hours"] = mtf_lower_tf
                    sig_data["lower_tf_ema_fast"] = mtf["lower_tf_ema_fast"]
                    sig_data["lower_tf_ema_slow"] = mtf["lower_tf_ema_slow"]
                    sig_data["lower_tf_bars_above"] = mtf["lower_tf_bars_above"]
                    sig_data["mtf_confirmed"] = mtf["lower_tf_bullish"]
                    # Per-strategy MTF if both active
                    if ema_scalper and ema_trend:
                        for mgr, key in [(ema_scalper, "mtf_scalper"), (ema_trend, "mtf_trend")]:
                            if mgr._cfg.mtf_enabled:
                                lc = build_sampled_candles(seven_day, timeframe_hours=mgr._cfg.mtf_lower_tf_hours)
                                m = compute_mtf_signal(lc, mgr._cfg.fast_period, mgr._cfg.slow_period, mgr._cfg.mtf_confirm_bars)
                                sig_data[key] = m["lower_tf_bullish"]
                # Volatility-based sizing info
                vol_cfg = primary._cfg if primary else None
                if vol_cfg and vol_cfg.vol_sizing_enabled:
                    vol = rolling_volatility(prices, window=vol_cfg.vol_window)
                    sig_data["ann_volatility"] = round(vol, 4) if vol is not None else None
                    if vol is not None:
                        vol_clamped = max(vol_cfg.vol_floor, min(vol, vol_cfg.vol_cap))
                        vol_pct = vol_cfg.vol_target_risk / vol_clamped
                        vol_pct = max(vol_cfg.vol_min_size_pct, min(vol_pct, vol_cfg.vol_max_size_pct))
                        sig_data["vol_adjusted_size_pct"] = round(vol_pct, 4)
                        # Pool-depth size for comparison
                        tao_in_pool = float(snap_data.get("total_tao", 0) or 0) / 1e9
                        if tao_in_pool > 0:
                            depth_pct = round((tao_in_pool * 0.025) / vol_cfg.pot_tao, 4)
                        else:
                            depth_pct = round(vol_cfg.position_size_pct, 4)
                        sig_data["pool_depth_size_pct"] = depth_pct
                        sig_data["final_size_pct"] = round(min(vol_pct, depth_pct), 4)
                    else:
                        sig_data["vol_adjusted_size_pct"] = None
                        sig_data["pool_depth_size_pct"] = None
                        sig_data["final_size_pct"] = None
                # Technical indicators (RSI, MACD, Bollinger Bands)
                if len(prices) >= 2:
                    rsi = compute_rsi(prices)
                    sig_data["rsi"] = round(rsi[-1], 1)
                    _, _, histogram = compute_macd(prices)
                    sig_data["macd_histogram"] = round(histogram[-1], 6)
                    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(prices)
                    bb_range = bb_upper[-1] - bb_lower[-1]
                    sig_data["bb_position"] = round(
                        (prices[-1] - bb_lower[-1]) / bb_range if bb_range > 0 else 0.5, 2
                    )
                else:
                    sig_data["rsi"] = None
                    sig_data["macd_histogram"] = None
                    sig_data["bb_position"] = None
                return sig_data

            results = []
            included_netuids: set[int] = set()
            ranked_snapshot = sorted(
                snapshot.items(),
                key=lambda item: float(item[1].get("tao_in_pool", 0) or 0),
                reverse=True,
            )
            for netuid, snap_data in ranked_snapshot:
                if netuid == 0:
                    continue
                sig = _build_signal(netuid, snap_data)
                if sig is not None:
                    results.append(sig)
                    included_netuids.add(netuid)

            # Ensure open positions are always included
            for netuid in open_netuids - included_netuids:
                snap_data = snapshot.get(netuid, {})
                sig = _build_signal(netuid, snap_data)
                if sig is not None:
                    results.append(sig)

            strategies = []
            if ema_scalper:
                strategies.append({"tag": "scalper", "fast": ema_scalper._cfg.fast_period, "slow": ema_scalper._cfg.slow_period})
            if ema_trend:
                strategies.append({"tag": "trend", "fast": ema_trend._cfg.fast_period, "slow": ema_trend._cfg.slow_period})

            return JSONResponse(
                content={
                    "signals": results[:120],
                    "ema_period": slow_p,
                    "fast_ema_period": fast_p,
                    "strategies": strategies,
                    "mtf_enabled": mtf_enabled,
                    "mtf_lower_tf_hours": mtf_lower_tf,
                }
            )

        @app.post("/api/ema/positions/{position_id}/close")
        async def api_ema_close(position_id: int):
            # Determine which manager owns this position
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")
            row = await db.fetchone(
                "SELECT strategy FROM ema_positions WHERE id = ? AND status = 'OPEN'",
                (position_id,),
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Position not found")
            mgr = {"scalper": ema_scalper, "trend": ema_trend}.get(row["strategy"])
            if mgr is None:
                raise HTTPException(status_code=503, detail="Strategy manager not initialized")
            try:
                result = await mgr.manual_close(position_id)
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            return JSONResponse(content={"success": True, "result": result})

        @app.get("/api/ema/stuck-positions")
        async def api_ema_stuck_positions():
            stuck: list[dict] = []
            for mgr in [ema_scalper, ema_trend]:
                if mgr is not None:
                    for info in mgr._stuck_positions.values():
                        stuck.append({**info, "strategy": mgr._cfg.tag})
            return JSONResponse(content={"stuck": stuck, "count": len(stuck)})

        @app.get("/api/ema/slippage-stats")
        async def api_ema_slippage_stats():
            if db is None:
                raise HTTPException(status_code=503, detail="DB not initialized")

            # Averages computed from CLOSED positions only (matches QA DB aggregate)
            closed_rows = await db.fetchall(
                """
                SELECT netuid, entry_slippage_pct, exit_slippage_pct, amount_tao
                FROM ema_positions
                WHERE status = 'CLOSED'
                ORDER BY exit_ts DESC
                """
            )
            # Total slippage computed from ALL positions (matches QA test which iterates db_all)
            all_rows = await db.fetchall(
                """
                SELECT netuid, entry_slippage_pct, exit_slippage_pct, amount_tao
                FROM ema_positions
                """
            )

            if not closed_rows and not all_rows:
                return JSONResponse(content={"trade_count": 0})

            entry_slips = [row["entry_slippage_pct"] for row in closed_rows if row["entry_slippage_pct"] is not None]
            exit_slips = [row["exit_slippage_pct"] for row in closed_rows if row["exit_slippage_pct"] is not None]
            avg_entry = sum(entry_slips) / len(entry_slips) if entry_slips else 0.0
            avg_exit = sum(exit_slips) / len(exit_slips) if exit_slips else 0.0

            combined = []
            total_slip_tao = 0.0
            for row in closed_rows:
                entry_slip = row["entry_slippage_pct"] or 0.0
                exit_slip = row["exit_slippage_pct"] or 0.0
                combined_slip = entry_slip + exit_slip
                combined.append((combined_slip, row["netuid"]))
            for row in all_rows:
                entry_slip = row["entry_slippage_pct"] or 0.0
                exit_slip = row["exit_slippage_pct"] or 0.0
                total_slip_tao += (row["amount_tao"] or 0.0) * (entry_slip + exit_slip) / 100.0

            combined.sort()
            return JSONResponse(
                content={
                    "trade_count": len(closed_rows),
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

        # ── SSE live price feed ─────────────────────────────────────
        import json as _json

        _sse_connections = 0

        @app.get("/api/prices")
        async def price_stream(request: Request):
            nonlocal _sse_connections

            if _sse_connections >= settings.PRICE_FEED_MAX_CONNECTIONS:
                return JSONResponse(
                    {"error": "too many price stream connections"}, status_code=503
                )

            async def event_generator():
                nonlocal _sse_connections
                _sse_connections += 1
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        snapshot = taostats._pool_snapshot if taostats else {}
                        payload = {
                            int(netuid): {
                                "price": float(snap.get("price", 0) or 0),
                                "tao_in_pool": float(snap.get("total_tao", 0) or 0) / 1e9,
                                "alpha_in_pool": float(snap.get("alpha_in_pool", 0) or 0) / 1e9,
                            }
                            for netuid, snap in snapshot.items()
                        }
                        data = _json.dumps({"ts": utc_iso(), "prices": payload})
                        yield f"data: {data}\n\n"

                        # Wait for snapshot refresh or timeout
                        if taostats:
                            try:
                                await asyncio.wait_for(
                                    taostats._price_updated.wait(),
                                    timeout=settings.PRICE_FEED_INTERVAL_SEC,
                                )
                                taostats._price_updated.clear()
                            except asyncio.TimeoutError:
                                pass
                        else:
                            await asyncio.sleep(settings.PRICE_FEED_INTERVAL_SEC)
                finally:
                    _sse_connections -= 1

            from starlette.responses import StreamingResponse

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
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
            mode_a = "LIVE" if not settings.EMA_DRY_RUN else "DRY"
            mode_b = "LIVE" if not settings.EMA_B_DRY_RUN else "DRY"
            await send_alert(
                f"🤖 <b>EMA bot online</b>\n"
                f"Scalper {settings.EMA_FAST_PERIOD}/{settings.EMA_PERIOD}: {mode_a}\n"
                f"Trend {settings.EMA_B_FAST_PERIOD}/{settings.EMA_B_PERIOD}: {mode_b}\n"
                f"Scan: {settings.SCAN_INTERVAL_MIN}m\n"
                f"Commands: <code>/help</code>"
            )

        # R3: Detect dual-held subnets at startup and warn
        if ema_scalper and ema_trend:
            dual = await _detect_dual_held_netuids()
            if dual:
                msg = (
                    f"⚠️ <b>Dual-held subnets at startup</b>\n"
                    f"Both Scalper and Trend hold: {sorted(dual)}\n"
                    f"Trend exit watcher will defer exits on these to avoid double-dumping.\n"
                    f"Use <code>/close</code> to manually close the worse position."
                )
                logger.warning(msg)
                await send_alert(msg)

        if settings.EMA_ENABLED or settings.EMA_B_ENABLED:
            await run_ema_cycle()
            if settings.EMA_EXIT_WATCHER_ENABLED:
                if ema_scalper:
                    await run_scalper_exit_watch()
                if ema_trend:
                    await run_trend_exit_watch()

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
