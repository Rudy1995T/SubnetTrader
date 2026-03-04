"""
Standalone backtester for SubnetTrader strategy.

Uses existing compute_signals / rank_subnets logic unchanged.
Data sources:
  --days 7  : seven_day_prices embedded in pool/latest (1 API call, ~45 bars per subnet)
  --days 14 : pool/history endpoint, top-N subnets by volatility (~20 calls)
  --days 30 : same, higher limit

Usage:
  python tools/backtest.py --days 7 --nav 2.0
  python tools/backtest.py --days 30 --nav 2.0 --top-n 20 --output /tmp/result.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any

# Make sure the project root is in path so we can import app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.config import settings
from app.strategy.scoring import compute_signals, rank_subnets, select_entries


# ── Configuration ────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    days: int = 7
    nav_tao: float = 2.0
    num_slots: int = 4
    enter_threshold: float = settings.ENTER_THRESHOLD
    high_conviction_threshold: float = settings.HIGH_CONVICTION_THRESHOLD
    stop_loss_pct: float = settings.STOP_LOSS_PCT
    take_profit_pct: float = settings.TAKE_PROFIT_PCT
    trailing_stop_pct: float = settings.TRAILING_STOP_PCT
    max_holding_bars: int = 0        # set from days in main()
    warm_up_bars: int = 30           # bars before first entry
    fee_pct: float = 0.3             # 0.3% swap fee
    slippage_pct: float = 1.0        # estimated slippage


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class BacktestPosition:
    netuid: int
    entry_bar: int
    entry_price: float
    entry_tao: float          # TAO spent
    peak_price: float
    slot_id: int


@dataclass
class BacktestTrade:
    netuid: int
    entry_bar: int
    exit_bar: int
    entry_price: float
    exit_price: float
    tao_in: float
    tao_out: float
    pnl_tao: float
    pnl_pct: float
    exit_reason: str
    hold_bars: int


@dataclass
class BacktestResult:
    config: dict
    total_return_pct: float
    final_nav_tao: float
    start_nav_tao: float
    num_trades: int
    win_rate_pct: float
    max_drawdown_pct: float
    avg_hold_bars: float
    avg_pnl_pct: float
    equity_curve: list[float]          # NAV at each bar
    trades: list[dict]
    stats_by_subnet: list[dict]


# ── API fetching ─────────────────────────────────────────────────────

async def _throttle(call_times: list[float], rate_limit: int = 30) -> None:
    """Respect Taostats rate limit (30 req/min)."""
    now = time.time()
    window_start = now - 60.0
    call_times[:] = [t for t in call_times if t > window_start]
    if len(call_times) >= rate_limit:
        sleep_for = 60.0 - (now - call_times[0]) + 0.2
        print(f"  [rate-limit] sleeping {sleep_for:.1f}s …", flush=True)
        await asyncio.sleep(sleep_for)
    call_times.append(time.time())


async def fetch_7d_prices(
    api_key: str, base_url: str
) -> dict[int, list[float]]:
    """
    Single call to pool/latest — extract seven_day_prices per subnet.
    Returns {netuid: [price, ...]} oldest first.
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        resp = await client.get(
            f"{base_url}/api/dtao/pool/latest/v1",
            params={"limit": 200},
        )
        resp.raise_for_status()
        data = resp.json()

    items = data.get("data", []) if isinstance(data, dict) else []
    result: dict[int, list[float]] = {}

    for subnet in items:
        try:
            netuid = int(subnet.get("netuid", -1))
            if netuid < 1:
                continue
            seven_day = subnet.get("seven_day_prices", [])
            prices = []
            for entry in seven_day:
                p = entry.get("price") if isinstance(entry, dict) else entry
                if p is not None:
                    try:
                        prices.append(float(p))
                    except (ValueError, TypeError):
                        continue
            if prices:
                result[netuid] = prices
        except (ValueError, TypeError):
            continue

    return result


async def fetch_history_prices(
    api_key: str, base_url: str, netuids: list[int], limit: int
) -> dict[int, list[float]]:
    """
    Fetch pool history for each netuid from /api/dtao/pool/history/v1.
    Returns {netuid: [price, ...]} oldest first.
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key

    result: dict[int, list[float]] = {}
    call_times: list[float] = []

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        for i, netuid in enumerate(netuids):
            await _throttle(call_times)
            print(f"  Fetching history netuid={netuid} ({i+1}/{len(netuids)}) …", flush=True)
            try:
                resp = await client.get(
                    f"{base_url}/api/dtao/pool/history/v1",
                    params={"netuid": netuid, "limit": limit},
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data", data) if isinstance(data, dict) else data
                prices = []
                for entry in (items if isinstance(items, list) else []):
                    p = entry.get("price") if isinstance(entry, dict) else None
                    if p is not None:
                        try:
                            prices.append(float(p))
                        except (ValueError, TypeError):
                            continue
                if prices:
                    result[netuid] = prices
            except Exception as e:
                print(f"  Warning: failed netuid={netuid}: {e}", flush=True)

    return result


def select_top_n_by_volatility(
    prices_map: dict[int, list[float]], n: int
) -> list[int]:
    """Return top-N netuids by price std dev (most volatile subnets)."""
    scored = []
    for netuid, prices in prices_map.items():
        if len(prices) < 5:
            continue
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        std = variance ** 0.5
        scored.append((netuid, std))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [netuid for netuid, _ in scored[:n]]


# ── Simulation ───────────────────────────────────────────────────────

def check_exit(
    pos: BacktestPosition,
    current_price: float,
    current_bar: int,
    cfg: BacktestConfig,
) -> str | None:
    """Return exit reason or None."""
    pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100.0

    if pnl_pct <= -cfg.stop_loss_pct:
        return "STOP_LOSS"

    if cfg.max_holding_bars > 0 and (current_bar - pos.entry_bar) >= cfg.max_holding_bars:
        return "TIME_STOP"

    if pnl_pct > 0 and pos.peak_price > pos.entry_price:
        drawdown_from_peak = (pos.peak_price - current_price) / pos.peak_price * 100.0
        if drawdown_from_peak >= cfg.trailing_stop_pct:
            return "TRAILING_STOP"

    if pnl_pct >= cfg.take_profit_pct:
        return "TAKE_PROFIT"

    return None


def simulate_backtest(
    prices_map: dict[int, list[float]],
    cfg: BacktestConfig,
) -> BacktestResult:
    """Walk-forward simulation bar by bar."""
    netuids = list(prices_map.keys())

    # Find the maximum length to determine total bars
    max_len = max(len(v) for v in prices_map.values()) if prices_map else 0
    if max_len < cfg.warm_up_bars + 2:
        return BacktestResult(
            config=asdict(cfg),
            total_return_pct=0.0,
            final_nav_tao=cfg.nav_tao,
            start_nav_tao=cfg.nav_tao,
            num_trades=0,
            win_rate_pct=0.0,
            max_drawdown_pct=0.0,
            avg_hold_bars=0.0,
            avg_pnl_pct=0.0,
            equity_curve=[cfg.nav_tao],
            trades=[],
            stats_by_subnet=[],
        )

    cash = cfg.nav_tao
    open_positions: list[BacktestPosition] = []
    closed_trades: list[BacktestTrade] = []
    equity_curve: list[float] = []
    peak_nav = cfg.nav_tao
    max_drawdown = 0.0
    next_slot_id = 0

    fee_factor = 1.0 - (cfg.fee_pct / 100.0)
    slippage_factor = 1.0 - (cfg.slippage_pct / 100.0)
    cost_factor = fee_factor * slippage_factor  # combined cost on exit

    for bar in range(max_len):
        # Build current price snapshot
        current_prices: dict[int, float] = {}
        for netuid, prices in prices_map.items():
            if bar < len(prices):
                current_prices[netuid] = prices[bar]

        if not current_prices:
            continue

        # 1. Update peaks and check exits
        exits_this_bar: list[BacktestPosition] = []
        for pos in list(open_positions):
            price = current_prices.get(pos.netuid)
            if price is None:
                continue
            if price > pos.peak_price:
                pos.peak_price = price

            reason = check_exit(pos, price, bar, cfg)
            if reason:
                exits_this_bar.append(pos)
                # Calculate exit: alpha -> TAO (apply cost)
                pnl_ratio = price / pos.entry_price
                tao_out = pos.entry_tao * pnl_ratio * cost_factor
                pnl_tao = tao_out - pos.entry_tao
                pnl_pct = (tao_out - pos.entry_tao) / pos.entry_tao * 100.0
                closed_trades.append(BacktestTrade(
                    netuid=pos.netuid,
                    entry_bar=pos.entry_bar,
                    exit_bar=bar,
                    entry_price=pos.entry_price,
                    exit_price=price,
                    tao_in=pos.entry_tao,
                    tao_out=tao_out,
                    pnl_tao=pnl_tao,
                    pnl_pct=pnl_pct,
                    exit_reason=reason,
                    hold_bars=bar - pos.entry_bar,
                ))
                cash += tao_out

        for pos in exits_this_bar:
            open_positions.remove(pos)

        # 2. Entries (only after warm-up)
        if bar >= cfg.warm_up_bars:
            free_slots = cfg.num_slots - len(open_positions)
            if free_slots > 0:
                # Build subnet data for scoring
                subnet_data = []
                occupied = {p.netuid for p in open_positions}
                for netuid, prices in prices_map.items():
                    if bar >= len(prices):
                        continue
                    history = prices[max(0, bar - 60):bar + 1]
                    alpha_price = prices[bar]
                    subnet_data.append({
                        "netuid": netuid,
                        "prices": history,
                        "alpha_price": alpha_price,
                    })

                ranked = rank_subnets(subnet_data, cfg.enter_threshold, cfg.high_conviction_threshold)
                to_enter = select_entries(
                    ranked=ranked,
                    available_slots=free_slots,
                    current_positions=occupied,
                    cooldown_netuids=set(),
                )

                for scored in to_enter:
                    if cash <= 0:
                        break
                    slot_alloc = cfg.nav_tao / cfg.num_slots
                    # Don't allocate more than 95% of cash
                    tao_in = min(slot_alloc, cash * 0.95)
                    if tao_in <= 0:
                        break
                    entry_price = current_prices.get(scored.netuid, 0.0)
                    if entry_price <= 0:
                        continue
                    # Apply fee on entry (TAO -> alpha)
                    tao_in_after_fee = tao_in * fee_factor
                    cash -= tao_in
                    open_positions.append(BacktestPosition(
                        netuid=scored.netuid,
                        entry_bar=bar,
                        entry_price=entry_price,
                        entry_tao=tao_in_after_fee,
                        peak_price=entry_price,
                        slot_id=next_slot_id % cfg.num_slots,
                    ))
                    next_slot_id += 1

        # 3. Calculate current NAV
        positions_value = 0.0
        for pos in open_positions:
            price = current_prices.get(pos.netuid, pos.entry_price)
            positions_value += pos.entry_tao * (price / pos.entry_price)
        nav = cash + positions_value
        equity_curve.append(round(nav, 6))

        # Track max drawdown
        if nav > peak_nav:
            peak_nav = nav
        if peak_nav > 0:
            dd = (peak_nav - nav) / peak_nav * 100.0
            if dd > max_drawdown:
                max_drawdown = dd

    # Force-close any remaining open positions at last price
    for pos in open_positions:
        prices = prices_map.get(pos.netuid, [])
        if prices:
            price = prices[-1]
            pnl_ratio = price / pos.entry_price
            tao_out = pos.entry_tao * pnl_ratio * cost_factor
            pnl_tao = tao_out - pos.entry_tao
            pnl_pct = pnl_tao / pos.entry_tao * 100.0
            closed_trades.append(BacktestTrade(
                netuid=pos.netuid,
                entry_bar=pos.entry_bar,
                exit_bar=max_len - 1,
                entry_price=pos.entry_price,
                exit_price=price,
                tao_in=pos.entry_tao,
                tao_out=tao_out,
                pnl_tao=pnl_tao,
                pnl_pct=pnl_pct,
                exit_reason="PERIOD_END",
                hold_bars=max_len - 1 - pos.entry_bar,
            ))
            cash += tao_out

    final_nav = equity_curve[-1] if equity_curve else cfg.nav_tao
    total_return_pct = (final_nav - cfg.nav_tao) / cfg.nav_tao * 100.0

    wins = [t for t in closed_trades if t.pnl_tao > 0]
    win_rate = (len(wins) / len(closed_trades) * 100.0) if closed_trades else 0.0
    avg_hold = (sum(t.hold_bars for t in closed_trades) / len(closed_trades)) if closed_trades else 0.0
    avg_pnl = (sum(t.pnl_pct for t in closed_trades) / len(closed_trades)) if closed_trades else 0.0

    # Per-subnet stats
    subnet_trades: dict[int, list[BacktestTrade]] = {}
    for t in closed_trades:
        subnet_trades.setdefault(t.netuid, []).append(t)

    stats_by_subnet = []
    for netuid, trades in sorted(subnet_trades.items(), key=lambda x: -sum(t.pnl_tao for t in x[1])):
        total_pnl = sum(t.pnl_tao for t in trades)
        sn_wins = [t for t in trades if t.pnl_tao > 0]
        stats_by_subnet.append({
            "netuid": netuid,
            "trades": len(trades),
            "win_rate_pct": round(len(sn_wins) / len(trades) * 100, 1),
            "total_pnl_tao": round(total_pnl, 6),
            "avg_pnl_pct": round(sum(t.pnl_pct for t in trades) / len(trades), 2),
            "avg_hold_bars": round(sum(t.hold_bars for t in trades) / len(trades), 1),
            "best_pnl_pct": round(max(t.pnl_pct for t in trades), 2),
            "worst_pnl_pct": round(min(t.pnl_pct for t in trades), 2),
        })

    return BacktestResult(
        config=asdict(cfg),
        total_return_pct=round(total_return_pct, 4),
        final_nav_tao=round(final_nav, 6),
        start_nav_tao=cfg.nav_tao,
        num_trades=len(closed_trades),
        win_rate_pct=round(win_rate, 2),
        max_drawdown_pct=round(max_drawdown, 4),
        avg_hold_bars=round(avg_hold, 2),
        avg_pnl_pct=round(avg_pnl, 4),
        equity_curve=equity_curve,
        trades=[asdict(t) for t in closed_trades],
        stats_by_subnet=stats_by_subnet,
    )


# ── Output formatting ─────────────────────────────────────────────────

def print_results(result: BacktestResult) -> None:
    cfg = result.config
    bars_per_day = 6  # ~4h bars

    print("\n" + "═" * 60)
    print(f"  BACKTEST RESULTS — {cfg['days']}d | NAV {cfg['nav_tao']} TAO | {cfg['num_slots']} slots")
    print("═" * 60)
    print(f"  Start NAV  : {result.start_nav_tao:.4f} TAO")
    print(f"  Final NAV  : {result.final_nav_tao:.4f} TAO")
    print(f"  Return     : {result.total_return_pct:+.2f}%")
    print(f"  Max DD     : {result.max_drawdown_pct:.2f}%")
    print(f"  Trades     : {result.num_trades}")
    print(f"  Win rate   : {result.win_rate_pct:.1f}%")
    print(f"  Avg PnL    : {result.avg_pnl_pct:+.2f}%  per trade")
    hold_h = result.avg_hold_bars / bars_per_day * 24.0
    print(f"  Avg hold   : {hold_h:.1f}h  ({result.avg_hold_bars:.0f} bars)")
    print("─" * 60)

    if result.trades:
        print("\n  TOP TRADES (by PnL):")
        sorted_trades = sorted(result.trades, key=lambda t: t["pnl_tao"], reverse=True)
        for t in sorted_trades[:10]:
            hold_h2 = t["hold_bars"] / bars_per_day * 24.0
            print(
                f"  netuid={t['netuid']:>4}  {t['exit_reason']:<14}  "
                f"PnL={t['pnl_pct']:+.2f}%  hold={hold_h2:.0f}h"
            )

    if result.stats_by_subnet:
        print("\n  BY SUBNET (top 10):")
        print(f"  {'netuid':>6}  {'trades':>6}  {'win%':>5}  {'pnl_tao':>8}  {'avg_pnl%':>9}")
        print("  " + "-" * 44)
        for s in result.stats_by_subnet[:10]:
            print(
                f"  {s['netuid']:>6}  {s['trades']:>6}  {s['win_rate_pct']:>5.1f}  "
                f"{s['total_pnl_tao']:>8.5f}  {s['avg_pnl_pct']:>+9.2f}%"
            )

    print("═" * 60)


# ── Main ─────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="SubnetTrader backtester")
    parser.add_argument("--days", type=int, default=7, choices=[7, 14, 30],
                        help="Backtest window in days (default: 7)")
    parser.add_argument("--nav", type=float, default=settings.DRY_RUN_STARTING_TAO,
                        help="Starting NAV in TAO (default: DRY_RUN_STARTING_TAO)")
    parser.add_argument("--top-n", type=int, default=20,
                        help="Subnets to test for 14d/30d (default: 20)")
    parser.add_argument("--output", type=str, default="",
                        help="Write JSON result to this path")
    args = parser.parse_args()

    # Bars: ~4h intervals
    bars_per_day = 6
    total_bars = args.days * bars_per_day

    # Max holding bars: 72h = 18 bars
    max_hold_bars = 18  # 72h / 4h

    cfg = BacktestConfig(
        days=args.days,
        nav_tao=args.nav,
        num_slots=settings.NUM_SLOTS,
        enter_threshold=settings.ENTER_THRESHOLD,
        high_conviction_threshold=settings.HIGH_CONVICTION_THRESHOLD,
        stop_loss_pct=settings.STOP_LOSS_PCT,
        take_profit_pct=settings.TAKE_PROFIT_PCT,
        trailing_stop_pct=settings.TRAILING_STOP_PCT,
        max_holding_bars=max_hold_bars,
        warm_up_bars=min(30, total_bars // 4),
    )

    api_key = settings.TAOSTATS_API_KEY
    base_url = settings.TAOSTATS_BASE_URL.rstrip("/")

    print(f"\nBacktest: {args.days}d window, {args.nav} TAO NAV, {settings.NUM_SLOTS} slots")

    if args.days == 7:
        print("Fetching 7d data (single API call) …", flush=True)
        prices_map = await fetch_7d_prices(api_key, base_url)
        print(f"  Got data for {len(prices_map)} subnets", flush=True)
    else:
        limit = total_bars + 10
        print(f"Step 1/2: Fetching 7d snapshot to pick top-{args.top_n} by volatility …", flush=True)
        snapshot = await fetch_7d_prices(api_key, base_url)
        top_netuids = select_top_n_by_volatility(snapshot, args.top_n)
        print(f"  Selected: {top_netuids}", flush=True)
        print(f"Step 2/2: Fetching {args.days}d history for {len(top_netuids)} subnets …", flush=True)
        prices_map = await fetch_history_prices(api_key, base_url, top_netuids, limit)
        print(f"  Got data for {len(prices_map)} subnets", flush=True)

    if not prices_map:
        print("No price data available. Check your TAOSTATS_API_KEY.")
        sys.exit(1)

    # Filter to subnets with enough bars
    min_bars = cfg.warm_up_bars + 5
    prices_map = {k: v for k, v in prices_map.items() if len(v) >= min_bars}
    print(f"  Subnets with >= {min_bars} bars: {len(prices_map)}", flush=True)

    if not prices_map:
        print("Not enough historical data for simulation.")
        sys.exit(1)

    print("Running simulation …", flush=True)
    result = simulate_backtest(prices_map, cfg)
    print_results(result)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(asdict(result), f, default=str)
        print(f"\nJSON saved to: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
