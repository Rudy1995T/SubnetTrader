"""
Core backtest engine — replays historical candle data through strategy logic.

Reuses signal functions from app.strategy.ema_signals and app.strategy.indicators.
Mirrors entry/exit logic from EmaManager but in a stateless simulation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.strategy.ema_signals import (
    Candle,
    build_candles_from_history,
    bullish_ema_bounce,
    candle_close_prices,
    compute_ema,
    compute_mtf_signal,
    dual_ema_signal,
    ema_signal,
)
from app.strategy.indicators import (
    compute_bollinger_bands,
    compute_macd,
    compute_rsi,
)

from .slippage import (
    apply_entry_slippage,
    apply_exit_slippage,
    estimate_entry_slippage,
    estimate_exit_slippage,
)
from .strategies import BacktestStrategyConfig


@dataclass
class TradeRecord:
    netuid: int
    entry_bar: int
    exit_bar: int
    entry_price: float
    exit_price: float
    entry_ts: str
    exit_ts: str
    amount_tao: float
    pnl_pct: float
    pnl_tao: float
    hold_bars: int
    hold_hours: float
    exit_reason: str
    peak_price: float
    entry_slippage_pct: float = 0.0
    exit_slippage_pct: float = 0.0


@dataclass
class BacktestResult:
    strategy_id: str
    window_days: int
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_hold_hours: float = 0.0
    max_concurrent: int = 0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    subnets_traded: list[int] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)


@dataclass
class _SimPosition:
    """In-flight simulated position."""
    netuid: int
    entry_bar: int
    entry_price: float  # effective (post-slippage)
    spot_price: float   # market price at entry
    amount_tao: float
    peak_price: float
    entry_ts: str
    entry_slippage_pct: float = 0.0


def _dynamic_trail_pct(
    pnl_pct: float,
    base_trail: float,
    dynamic: bool,
) -> float:
    """Mirror EmaManager._dynamic_trail_pct."""
    if not dynamic:
        return base_trail
    if pnl_pct >= 90:
        return 6.0
    if pnl_pct >= 75:
        return 8.0
    if pnl_pct >= 60:
        return 10.0
    return base_trail


def _check_exit(
    pos: _SimPosition,
    cur_price: float,
    prices_so_far: list[float],
    bar_index: int,
    entry_bar: int,
    candle_hours: int,
    cfg: BacktestStrategyConfig,
) -> str | None:
    """Check all exit conditions. Returns exit reason or None."""
    pnl_pct = (cur_price - pos.entry_price) / pos.entry_price * 100.0

    # Take-profit
    if pnl_pct >= cfg.take_profit_pct:
        return "TAKE_PROFIT"

    # Stop-loss (with RSI suppression if enabled)
    if pnl_pct <= -cfg.stop_loss_pct:
        if cfg.rsi_filter_enabled and prices_so_far:
            hard_floor = -cfg.stop_loss_pct * 1.5
            if pnl_pct > hard_floor:
                rsi = compute_rsi(prices_so_far, period=cfg.rsi_period)
                if rsi[-1] < cfg.rsi_oversold:
                    pass  # defer stop-loss
                else:
                    return "STOP_LOSS"
            else:
                return "STOP_LOSS"
        else:
            return "STOP_LOSS"

    # Time stop
    bars_held = bar_index - entry_bar
    hours_held = bars_held * candle_hours
    if hours_held >= cfg.max_holding_hours:
        return "TIME_STOP"

    # Breakeven stop
    peak_pnl = (pos.peak_price - pos.entry_price) / pos.entry_price * 100.0
    if peak_pnl >= cfg.breakeven_trigger_pct and pnl_pct <= 0:
        return "BREAKEVEN_STOP"

    # Trailing stop
    if pnl_pct > 0 and pos.peak_price > pos.entry_price:
        drawdown = (pos.peak_price - cur_price) / pos.peak_price * 100.0
        trail = _dynamic_trail_pct(pnl_pct, cfg.trailing_stop_pct, cfg.trailing_stop_dynamic)
        if drawdown >= trail:
            return "TRAILING_STOP"

    # EMA cross exit
    if len(prices_so_far) >= cfg.confirm_bars:
        sig = ema_signal(prices_so_far, cfg.slow_period, cfg.confirm_bars)
        if sig == "SELL":
            return "EMA_CROSS"

    return None


def _check_entry_filters(
    prices: list[float],
    candles: list[Candle],
    candles_lower: list[Candle] | None,
    cfg: BacktestStrategyConfig,
) -> bool:
    """Apply entry filters. Returns True if entry is allowed."""
    if len(prices) < max(cfg.fast_period, cfg.slow_period, cfg.confirm_bars):
        return False

    # Dual EMA signal
    sig = dual_ema_signal(prices, cfg.fast_period, cfg.slow_period, cfg.confirm_bars)
    if sig != "BUY":
        return False

    # Max entry price filter
    if prices[-1] > cfg.max_entry_price_tao:
        return False

    # Parabolic guard: reject if price extended too far above slow EMA
    slow_ema = compute_ema(prices, cfg.slow_period)
    if slow_ema and slow_ema[-1] > 0:
        extension = prices[-1] / slow_ema[-1]
        if extension > cfg.parabolic_guard_mult:
            return False

    # Momentum pre-filters
    if cfg.momentum_filters_enabled and len(prices) >= 42:
        # ~7 days at 4h (scaled by timeframe)
        day_bars = max(1, 24 // cfg.candle_timeframe_hours)
        week_bars = day_bars * 7
        if len(prices) >= day_bars + 1:
            day_change = (prices[-1] - prices[-day_bars - 1]) / prices[-day_bars - 1] * 100
            if day_change < -cfg.reject_day_and_week_negative_pct:
                return False
        if len(prices) >= week_bars + 1:
            week_change = (prices[-1] - prices[-week_bars - 1]) / prices[-week_bars - 1] * 100
            if week_change < -cfg.reject_day_and_week_negative_pct:
                return False
            # Structural decline: sustained drop over the week
            if week_change < -cfg.reject_structural_decline_pct:
                return False

    # MTF confirmation
    if cfg.mtf_enabled and candles_lower:
        mtf = compute_mtf_signal(
            candles_lower, cfg.fast_period, cfg.slow_period, cfg.mtf_confirm_bars
        )
        if not mtf["lower_tf_bullish"]:
            return False

    # Bounce confirmation (secondary entry path — if enabled, accept even
    # without fresh crossover if there's a bullish EMA bounce)
    # Note: bounce is checked as an additional entry condition, not a gate
    # The dual EMA BUY above already passed, so bounce is bonus confirmation

    # RSI filter
    if cfg.rsi_filter_enabled and len(prices) >= cfg.rsi_period:
        rsi = compute_rsi(prices, period=cfg.rsi_period)
        if rsi[-1] > cfg.rsi_overbought:
            return False

    # MACD filter
    if cfg.macd_filter_enabled and len(prices) >= cfg.macd_slow:
        macd_line, signal_line, histogram = compute_macd(
            prices, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
        )
        if histogram[-1] < 0:
            return False

    # Bollinger Band filter
    if cfg.bb_filter_enabled and len(prices) >= cfg.bb_period:
        upper, middle, lower = compute_bollinger_bands(prices, period=cfg.bb_period)
        if middle[-1] > 0:
            bb_position = (prices[-1] - lower[-1]) / (upper[-1] - lower[-1]) if upper[-1] != lower[-1] else 0.5
            if bb_position > cfg.bb_upper_reject:
                return False

    return True


def _detect_data_resolution_hours(history: list[dict]) -> int:
    """Detect the actual data interval in hours from history timestamps."""
    if len(history) < 3:
        return 24
    # Sample a few intervals
    intervals = []
    for i in range(min(5, len(history) - 1)):
        try:
            t1 = _parse_ts(str(history[i].get("timestamp", "")))
            t2 = _parse_ts(str(history[i + 1].get("timestamp", "")))
            gap_h = abs((t1 - t2).total_seconds()) / 3600
            if gap_h > 0:
                intervals.append(gap_h)
        except Exception:
            continue
    if not intervals:
        return 24
    median = sorted(intervals)[len(intervals) // 2]
    # Round to nearest standard interval
    for std in [1, 2, 4, 8, 12, 24]:
        if median <= std * 1.5:
            return std
    return 24


def backtest_subnet(
    history: list[dict],
    netuid: int,
    cfg: BacktestStrategyConfig,
    pool_tao: float = 0.0,
    window_days: int | None = None,
) -> list[TradeRecord]:
    """
    Run backtest on a single subnet's historical data.

    Returns list of completed trades.
    """
    # Detect actual data resolution and use it as minimum candle size
    data_resolution = _detect_data_resolution_hours(history)
    tf = max(cfg.candle_timeframe_hours, data_resolution)

    # Build candles at the effective timeframe
    candles = build_candles_from_history(history, candle_hours=tf)
    if not candles:
        return []

    # Trim to window
    if window_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        candles = [c for c in candles if _parse_ts(c.end_ts) >= cutoff]

    if len(candles) < max(cfg.fast_period, cfg.slow_period) + cfg.confirm_bars:
        return []

    # Build lower-TF candles for MTF (if enabled and different from main TF)
    candles_lower: list[Candle] | None = None
    lower_tf = max(cfg.mtf_lower_tf_hours, data_resolution)
    if cfg.mtf_enabled and lower_tf < tf:
        candles_lower_all = build_candles_from_history(
            history, candle_hours=lower_tf
        )
        if window_days is not None and candles_lower_all:
            cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
            candles_lower_all = [c for c in candles_lower_all if _parse_ts(c.end_ts) >= cutoff]
        candles_lower = candles_lower_all if candles_lower_all else None

    prices = candle_close_prices(candles)
    trades: list[TradeRecord] = []
    position: _SimPosition | None = None
    cooldown_until: int = -1  # bar index until which we're in cooldown
    available_tao = cfg.pot_tao

    for i in range(max(cfg.fast_period, cfg.slow_period), len(candles)):
        cur_price = prices[i]
        prices_so_far = prices[: i + 1]
        candles_so_far = candles[: i + 1]

        # ── Exit check ────────────────────────────────────────────
        if position is not None:
            # Update peak
            if cur_price > position.peak_price:
                position.peak_price = cur_price

            reason = _check_exit(
                position, cur_price, prices_so_far,
                i, position.entry_bar, tf, cfg,
            )
            if reason:
                # Apply exit slippage
                exit_slip_pct = estimate_exit_slippage(
                    position.amount_tao * (cur_price / position.entry_price),
                    pool_tao,
                ) if pool_tao > 0 else 0.0
                effective_exit = apply_exit_slippage(cur_price, exit_slip_pct)

                pnl_pct = (effective_exit - position.entry_price) / position.entry_price * 100.0
                pnl_tao = position.amount_tao * pnl_pct / 100.0
                bars_held = i - position.entry_bar
                hours_held = bars_held * tf

                trade = TradeRecord(
                    netuid=netuid,
                    entry_bar=position.entry_bar,
                    exit_bar=i,
                    entry_price=position.entry_price,
                    exit_price=effective_exit,
                    entry_ts=position.entry_ts,
                    exit_ts=candles[i].end_ts,
                    amount_tao=position.amount_tao,
                    pnl_pct=pnl_pct,
                    pnl_tao=pnl_tao,
                    hold_bars=bars_held,
                    hold_hours=hours_held,
                    exit_reason=reason,
                    peak_price=position.peak_price,
                    entry_slippage_pct=position.entry_slippage_pct,
                    exit_slippage_pct=exit_slip_pct,
                )
                trades.append(trade)
                available_tao += position.amount_tao + pnl_tao

                # Set cooldown
                cooldown_bars = int(cfg.cooldown_hours / tf)
                cooldown_until = i + cooldown_bars
                position = None

        # ── Entry check ───────────────────────────────────────────
        if position is None and i > cooldown_until:
            # Slice lower-TF candles up to the current bar's time
            lower_slice = None
            if candles_lower and cfg.mtf_enabled:
                bar_end = _parse_ts(candles[i].end_ts)
                lower_slice = [
                    c for c in candles_lower if _parse_ts(c.end_ts) <= bar_end
                ]

            if _check_entry_filters(prices_so_far, candles_so_far, lower_slice, cfg):
                # Bounce check as additional confirmation
                if cfg.bounce_enabled:
                    has_bounce = bullish_ema_bounce(
                        candles_so_far,
                        period=cfg.slow_period,
                        touch_tolerance_pct=1.0,
                        require_green=True,
                    )
                    # Bounce is bonus — don't gate on it, but the dual EMA BUY
                    # already confirmed trend. Bounce just adds conviction.

                size_tao = cfg.pot_tao * cfg.position_size_pct
                if size_tao > available_tao:
                    size_tao = available_tao
                if size_tao < 0.1:
                    continue

                # Apply entry slippage
                entry_slip_pct = estimate_entry_slippage(
                    size_tao, pool_tao
                ) if pool_tao > 0 else 0.0
                effective_entry = apply_entry_slippage(cur_price, entry_slip_pct)

                position = _SimPosition(
                    netuid=netuid,
                    entry_bar=i,
                    entry_price=effective_entry,
                    spot_price=cur_price,
                    amount_tao=size_tao,
                    peak_price=effective_entry,
                    entry_ts=candles[i].end_ts,
                    entry_slippage_pct=entry_slip_pct,
                )
                available_tao -= size_tao

    # Close any open position at end of data (mark-to-market)
    if position is not None:
        cur_price = prices[-1]
        pnl_pct = (cur_price - position.entry_price) / position.entry_price * 100.0
        pnl_tao = position.amount_tao * pnl_pct / 100.0
        bars_held = len(candles) - 1 - position.entry_bar
        trades.append(TradeRecord(
            netuid=netuid,
            entry_bar=position.entry_bar,
            exit_bar=len(candles) - 1,
            entry_price=position.entry_price,
            exit_price=cur_price,
            entry_ts=position.entry_ts,
            exit_ts=candles[-1].end_ts,
            amount_tao=position.amount_tao,
            pnl_pct=pnl_pct,
            pnl_tao=pnl_tao,
            hold_bars=bars_held,
            hold_hours=bars_held * tf,
            exit_reason="END_OF_DATA",
            peak_price=position.peak_price,
            entry_slippage_pct=position.entry_slippage_pct,
        ))

    return trades


def backtest_strategy(
    all_history: dict[int, list[dict]],
    cfg: BacktestStrategyConfig,
    pool_snapshots: dict[int, dict],
    window_days: int,
) -> BacktestResult:
    """
    Run backtest across all subnets for a single strategy + window.
    """
    all_trades: list[TradeRecord] = []
    subnets_traded: set[int] = set()

    for netuid, history in all_history.items():
        # Get pool depth for slippage model
        pool_tao = 0.0
        snap = pool_snapshots.get(netuid, {})
        raw_tao = snap.get("total_tao") or snap.get("tao_in_pool") or 0
        try:
            pool_tao = float(raw_tao) / 1e9
        except (ValueError, TypeError):
            pass

        if pool_tao < cfg.min_pool_depth_tao:
            continue

        trades = backtest_subnet(history, netuid, cfg, pool_tao, window_days)
        if trades:
            all_trades.extend(trades)
            subnets_traded.add(netuid)

    return _compute_result(cfg.strategy_id, window_days, all_trades, sorted(subnets_traded))


def _compute_result(
    strategy_id: str,
    window_days: int,
    trades: list[TradeRecord],
    subnets: list[int],
) -> BacktestResult:
    """Compute aggregate metrics from trade list."""
    result = BacktestResult(
        strategy_id=strategy_id,
        window_days=window_days,
        subnets_traded=subnets,
        trades=trades,
    )

    if not trades:
        return result

    result.total_trades = len(trades)
    winners = [t for t in trades if t.pnl_pct > 0]
    losers = [t for t in trades if t.pnl_pct <= 0]
    result.winning_trades = len(winners)
    result.losing_trades = len(losers)
    result.win_rate = len(winners) / len(trades) * 100.0 if trades else 0.0

    result.avg_win_pct = (
        sum(t.pnl_pct for t in winners) / len(winners) if winners else 0.0
    )
    result.avg_loss_pct = (
        sum(t.pnl_pct for t in losers) / len(losers) if losers else 0.0
    )

    # Expectancy: (win_rate * avg_win) - ((1-win_rate) * abs(avg_loss))
    wr = result.win_rate / 100.0
    result.expectancy = (wr * result.avg_win_pct) - ((1 - wr) * abs(result.avg_loss_pct))

    # Profit factor
    gross_wins = sum(t.pnl_tao for t in winners)
    gross_losses = abs(sum(t.pnl_tao for t in losers))
    result.profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Total PnL
    total_pnl_tao = sum(t.pnl_tao for t in trades)
    pot = trades[0].amount_tao / 0.20 if trades else 10.0  # approximate pot
    result.total_pnl_pct = total_pnl_tao / pot * 100.0 if pot > 0 else 0.0

    # Max drawdown (equity curve)
    equity = 0.0
    peak_equity = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        equity += t.pnl_tao
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown_pct = (max_dd / pot * 100.0) if pot > 0 else 0.0

    # Sharpe ratio (annualized)
    returns = [t.pnl_pct for t in trades]
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 1.0
        # Annualize: assume avg holding period, scale by sqrt(trades/year)
        avg_hold_h = sum(t.hold_hours for t in trades) / len(trades)
        trades_per_year = 8760 / avg_hold_h if avg_hold_h > 0 else 1
        result.sharpe_ratio = (mean_r / std_r) * math.sqrt(trades_per_year)
    else:
        result.sharpe_ratio = 0.0

    # Average hold duration
    result.avg_hold_hours = sum(t.hold_hours for t in trades) / len(trades)

    # Max concurrent (approximate — sort by entry, scan for overlaps)
    events: list[tuple[str, int]] = []
    for t in trades:
        events.append((t.entry_ts, 1))
        events.append((t.exit_ts, -1))
    events.sort()
    concurrent = 0
    max_conc = 0
    for _, delta in events:
        concurrent += delta
        max_conc = max(max_conc, concurrent)
    result.max_concurrent = max_conc

    # Exit reasons
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    result.exit_reasons = reasons

    return result


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO timestamp to tz-aware UTC datetime."""
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
