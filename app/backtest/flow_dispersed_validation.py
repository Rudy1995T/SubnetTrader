"""One-off: validate the per-regime suggestion `REGIME_GATE_FLOW=dispersed`.

Runs the best config from data/backtest/results/flow_sweep_20260421_131154.csv
across three regime-gate variants so we can see whether restricting flow
entries to DISPERSED windows actually beats the unfiltered baseline.

Run:
    python -m app.backtest.flow_dispersed_validation
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

from .flow_data_loader import load_cached_flow_history
from .flow_engine import (
    FlowBacktestConfig,
    build_signal_config,
    run_flow_backtest,
)
from .probe_flow_history import INTERVAL_SECONDS, load_probe
from .regime_labeler import (
    RegimeTimeline,
    TIMELINE_PATH,
    build_regime_timeline,
    load_timeline,
    regime_distribution,
    save_timeline,
)


# Best config from yesterday's sweep (top of expectancy ranking)
BEST = {
    "z_entry": 1.5,
    "min_tao_pct": 1.0,
    "stop_loss_pct": 4.0,
    "take_profit_pct": 8.0,
}

VARIANTS = [
    ("baseline (no label gate)", None),
    ("DISPERSED-only", frozenset({"DISPERSED"})),
    ("DISPERSED+TRENDING", frozenset({"DISPERSED", "TRENDING"})),
]


def _make_run_cfg(
    interval: str,
    interval_seconds: int,
    label_gate: frozenset[str] | None,
    window_days: int,
) -> FlowBacktestConfig:
    return FlowBacktestConfig(
        interval=interval,
        interval_seconds=interval_seconds,
        window_days=window_days,
        pot_tao=settings.FLOW_POT_TAO,
        slots=settings.FLOW_SLOTS,
        position_size_pct=settings.FLOW_POSITION_SIZE_PCT,
        min_pool_depth_tao=settings.FLOW_MIN_POOL_DEPTH_TAO,
        stop_loss_pct=BEST["stop_loss_pct"],
        take_profit_pct=BEST["take_profit_pct"],
        trailing_pct=settings.FLOW_TRAILING_PCT,
        trailing_trigger_pct=settings.FLOW_TRAILING_TRIGGER_PCT,
        time_soft_hours=settings.FLOW_TIME_SOFT_HOURS,
        time_hard_hours=settings.FLOW_TIME_HARD_HOURS,
        cooldown_hours=max(
            settings.FLOW_COOLDOWN_TIME_HOURS, interval_seconds / 3600.0
        ),
        regime_filter_enabled=False,  # Yesterday's sweep showed this hurts
        regime_threshold=settings.FLOW_REGIME_INDEX_THRESHOLD,
        regime_label_gate=label_gate,
        ema_fast_period=settings.FLOW_EMA_FAST_PERIOD,
        ema_slow_period=settings.FLOW_EMA_SLOW_PERIOD,
        ema_confirm=settings.FLOW_REQUIRE_EMA_CONFIRM,
        cadence_acknowledged=True,  # 1d cadence — known degraded signal
    )


def main() -> None:
    probe = load_probe() or {}
    interval = probe.get("finest_interval") or "1d"
    interval_seconds = INTERVAL_SECONDS.get(interval, 86400)
    window_days = 120

    print("Loading cached flow history...")
    history = load_cached_flow_history(interval_seconds=interval_seconds)
    if not history:
        print("ERROR: no flow history cached. Run "
              "`python -m app.backtest flow --fetch-only` first.")
        return
    print(f"  loaded {len(history)} subnets, "
          f"{sum(len(h) for h in history.values())} snapshots total")

    timeline = load_timeline(TIMELINE_PATH)
    if timeline is None or not timeline.epochs:
        print("\nNo cached regime timeline — building from history...")
        timeline, resolved = build_regime_timeline(history)
        save_timeline(timeline, TIMELINE_PATH, resolved=resolved)
        print(f"  built {len(timeline.epochs)} labels "
              f"(bucket={resolved.bucket_hours}h, "
              f"window={resolved.window_hours}h, source={resolved.source})")
    else:
        print(f"\nUsing cached regime timeline ({len(timeline.epochs)} labels)")

    dist = regime_distribution(timeline)
    if dist:
        share = ", ".join(f"{k}={v:.1f}%" for k, v in sorted(dist.items()))
        print(f"  regime time-share: {share}")

    sig_cfg = build_signal_config(
        interval_seconds=interval_seconds,
        z_entry=BEST["z_entry"],
        min_tao_pct=BEST["min_tao_pct"],
        emission_adjust=False,
    )

    print(f"\nBest config under test: z_entry={BEST['z_entry']} "
          f"min_tao_pct={BEST['min_tao_pct']} "
          f"stop={BEST['stop_loss_pct']}% tp={BEST['take_profit_pct']}%")
    print(f"  cadence={interval} window={window_days}d "
          f"pot={settings.FLOW_POT_TAO}τ slots={settings.FLOW_SLOTS}")

    rows: list[dict] = []
    print("\n" + "─" * 110)
    header = (f"{'variant':<28} {'trades':>7} {'WR%':>6} {'E(R)':>7} "
              f"{'PF':>5} {'maxDD%':>7} {'potΔτ':>8} {'pot%':>7} "
              f"{'avgHold(h)':>10} {'lblBlk%':>8}")
    print(header)
    print("─" * 110)

    for label, gate in VARIANTS:
        run_cfg = _make_run_cfg(interval, interval_seconds, gate, window_days)
        result = run_flow_backtest(
            history,
            sig_cfg,
            run_cfg,
            regime_timeline=timeline if gate else None,
        )
        line = (f"{label:<28} {result.total_trades:>7d} "
                f"{result.win_rate:>6.2f} {result.expectancy:>7.3f} "
                f"{result.profit_factor:>5.2f} {result.max_drawdown_pct:>7.2f} "
                f"{result.pot_growth_tao:>+8.3f} {result.pot_growth_pct:>+6.2f}% "
                f"{result.avg_hold_hours:>10.1f} "
                f"{result.pct_blocked_by_regime_label:>7.1f}%")
        print(line)
        rows.append({
            "variant": label,
            "regime_label_gate": sorted(gate) if gate else None,
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate": result.win_rate,
            "expectancy": result.expectancy,
            "profit_factor": result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": result.sharpe_ratio,
            "avg_win_pct": result.avg_win_pct,
            "avg_loss_pct": result.avg_loss_pct,
            "avg_hold_hours": result.avg_hold_hours,
            "pot_growth_tao": result.pot_growth_tao,
            "pot_growth_pct": result.pot_growth_pct,
            "pct_blocked_by_regime_label": result.pct_blocked_by_regime_label,
            "pct_blocked_by_cold_start": result.pct_blocked_by_cold_start,
            "exit_reasons": result.exit_reasons,
            "subnets_traded": result.subnets_traded,
        })

    print("─" * 110)

    out_dir = Path("data/backtest/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"flow_dispersed_validation_{ts}.json"
    with open(out_path, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "interval": interval,
            "window_days": window_days,
            "best_config": BEST,
            "regime_distribution_pct": dist,
            "results": rows,
        }, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
