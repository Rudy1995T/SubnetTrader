"""Volatility regime classifier (meta-strategy).

Classifies the subnet market as TRENDING / DISPERSED / CHOPPY / DEAD from
realized volatility, directional strength, and cross-sectional dispersion
computed off the ``pool_snapshots`` table. Exposes an O(1) ``entry_allowed``
check for each strategy manager; exits are never gated.

See: specs/strategy-volatility-regime-filter.md
     specs/NewSpecs/implement-regime-classifier.md
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Iterable

from app.logging.logger import logger
from app.notifications.telegram import send_alert
from app.utils.time import parse_iso, utc_iso, utc_now


TRENDING = "TRENDING"
DISPERSED = "DISPERSED"
CHOPPY = "CHOPPY"
DEAD = "DEAD"

_ALL_REGIMES = (TRENDING, DISPERSED, CHOPPY, DEAD)


def _parse_gate(raw: str) -> frozenset[str]:
    """Parse ``REGIME_GATE_*`` env value into a set of uppercase regime names.

    ``"all"`` expands to every regime. Unknown tokens are silently ignored.
    """
    tokens = [t.strip().lower() for t in (raw or "").split(",") if t.strip()]
    if not tokens:
        return frozenset()
    if "all" in tokens:
        return frozenset(_ALL_REGIMES)
    mapped: set[str] = set()
    for tok in tokens:
        up = tok.upper()
        if up in _ALL_REGIMES:
            mapped.add(up)
    return frozenset(mapped)


def _bucket_snapshots(
    snaps: list[dict], bucket_hours: int, window_hours: int
) -> list[float]:
    """Downsample a per-subnet snapshot series into fixed-size time buckets.

    Edges are anchored at the **most recent observation** and step backwards
    by ``bucket_hours`` for up to ``window_hours // bucket_hours`` buckets.
    Anchoring at the end (rather than the first observation + bucket_sec)
    makes the count independent of sampling jitter on the earliest point —
    a snapshot that lands 5 min after ``since`` should not cost a whole
    bucket. Edges that would fall before the first observation are dropped
    to avoid projecting prices into a period with no data.

    For each surviving edge, take the last snapshot at or before that edge
    (last-observation-carried-forward). Requires snapshots to carry ``ts``
    (ISO8601) and ``price``.
    """
    if not snaps or bucket_hours <= 0 or window_hours <= 0:
        return []
    parsed: list[tuple[float, float]] = []
    for row in snaps:
        ts_raw = row.get("ts")
        price = row.get("price")
        if not ts_raw or price is None:
            continue
        try:
            ts = parse_iso(ts_raw).timestamp()
            p = float(price)
        except (ValueError, TypeError):
            continue
        if p <= 0:
            continue
        parsed.append((ts, p))
    if not parsed:
        return []
    parsed.sort(key=lambda x: x[0])

    first_ts = parsed[0][0]
    last_ts = parsed[-1][0]
    bucket_sec = bucket_hours * 3600
    n_buckets = max(1, window_hours // bucket_hours)

    edges: list[float] = []
    for i in range(n_buckets - 1, -1, -1):
        edge = last_ts - i * bucket_sec
        if edge >= first_ts - 1e-6:
            edges.append(edge)
    if not edges:
        return []

    prices: list[float] = []
    idx = 0
    last_price = parsed[0][1]
    for edge_ts in edges:
        while idx < len(parsed) and parsed[idx][0] <= edge_ts:
            last_price = parsed[idx][1]
            idx += 1
        prices.append(last_price)
    return prices


def _log_returns(prices: Iterable[float]) -> list[float]:
    out: list[float] = []
    prev: float | None = None
    for p in prices:
        if prev is not None and prev > 0 and p > 0:
            try:
                out.append(math.log(p / prev))
            except ValueError:
                pass
        prev = p
    return out


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def classify_regime(
    vol: float,
    dir_strength: float,
    dispersion: float,
    settings,
) -> str:
    """Pure state-table mapping from aggregate metrics → regime label.

    Extracted so the backtest labeller runs identical math to live without
    going through the async ``RegimeFilter`` class.
    """
    if vol < settings.REGIME_VOL_DEAD_THRESHOLD:
        return DEAD
    if (
        vol >= settings.REGIME_VOL_TREND_THRESHOLD
        and dir_strength >= settings.REGIME_DIR_THRESHOLD
    ):
        return TRENDING
    if dispersion >= settings.REGIME_DISP_THRESHOLD:
        return DISPERSED
    if (
        vol >= settings.REGIME_VOL_CHOP_FLOOR
        and dir_strength < settings.REGIME_DIR_THRESHOLD
    ):
        return CHOPPY
    return DEAD


def compute_regime_metrics(
    per_netuid_snapshots: dict[int, list[dict]],
    settings,
) -> dict:
    """Compute aggregate regime metrics from per-subnet snapshot slices.

    Inputs mirror what ``RegimeFilter._compute_metrics`` fetches from the
    live DB but are passed in-memory so the backtest labeller can walk the
    historical timeline without hitting SQLite. Each snapshot row must
    carry ``ts`` (ISO8601) and ``price``.
    """
    bucket_hours = max(1, int(settings.REGIME_BUCKET_HOURS))
    window_hours = max(1, int(settings.REGIME_VOL_WINDOW_HOURS))
    min_buckets = max(2, int(settings.REGIME_MIN_BUCKETS))
    min_subnets = max(1, int(settings.REGIME_MIN_SUBNETS))

    per_subnet_vol: list[float] = []
    per_subnet_abs_ret_mean: list[float] = []
    per_subnet_window_ret: list[float] = []

    for _netuid, snaps in per_netuid_snapshots.items():
        if not snaps:
            continue
        prices = _bucket_snapshots(snaps, bucket_hours, window_hours)
        if len(prices) < min_buckets:
            continue
        rets = _log_returns(prices)
        if len(rets) < 2:
            continue
        bars_per_day = max(1, 24 // bucket_hours)
        ann_factor = math.sqrt(bars_per_day * 365)
        vol = _std(rets) * ann_factor
        per_subnet_vol.append(vol)
        per_subnet_abs_ret_mean.append(
            sum(abs(r) for r in rets) / len(rets)
        )
        per_subnet_window_ret.append(math.log(prices[-1] / prices[0]))

    n_subnets = len(per_subnet_vol)
    thin = n_subnets < min_subnets

    if n_subnets == 0 or thin:
        return {
            "vol_24h": 0.0,
            "directional_strength": 0.0,
            "dispersion": 0.0,
            "raw_regime": DEAD,
            "n_subnets": n_subnets,
            "thin_universe": True,
        }

    vol_agg = sum(per_subnet_vol) / n_subnets
    dir_strength = sum(per_subnet_abs_ret_mean) / n_subnets
    dispersion = _std(per_subnet_window_ret)

    return {
        "vol_24h": round(vol_agg, 4),
        "directional_strength": round(dir_strength, 5),
        "dispersion": round(dispersion, 5),
        "raw_regime": classify_regime(vol_agg, dir_strength, dispersion, settings),
        "n_subnets": n_subnets,
        "thin_universe": False,
    }


class RegimeFilter:
    """Aggregate market regime classifier.

    Driven off ``pool_snapshots`` bucketed into ``REGIME_BUCKET_HOURS``
    windows. ``refresh()`` is throttled to at most one DB pass per
    ``REGIME_REFRESH_SECONDS``; all other accessors read cached state.
    """

    def __init__(self, db, settings) -> None:
        self._db = db
        self._settings = settings
        self._last_refresh_ts: float = 0.0
        self._current_regime: str = DEAD
        self._pending_regime: str | None = None
        self._pending_count: int = 0
        self._regime_since: str = utc_iso()
        self._last_metrics: dict = {
            "vol_24h": None,
            "directional_strength": None,
            "dispersion": None,
            "raw_regime": DEAD,
            "n_subnets": 0,
            "thin_universe": True,
            "updated_at": None,
        }

    # ── Public API ──────────────────────────────────────────────

    async def refresh(self, force: bool = False) -> None:
        """Recompute metrics and (possibly) flip the debounced regime.

        Called once per cycle or per watcher tick; internally throttled so
        high-frequency callers don't hammer the DB.
        """
        now_ts = utc_now().timestamp()
        interval = max(0, int(self._settings.REGIME_REFRESH_SECONDS))
        if not force and interval > 0 and (now_ts - self._last_refresh_ts) < interval:
            return
        self._last_refresh_ts = now_ts

        try:
            metrics = await self._compute_metrics()
        except Exception as exc:
            logger.error(f"RegimeFilter refresh failed: {exc}")
            return

        raw = metrics["raw_regime"]
        self._last_metrics = metrics

        # Debounce: require N consecutive raw classifications before flipping
        debounce = max(1, int(self._settings.REGIME_DEBOUNCE_CYCLES))
        if raw == self._current_regime:
            self._pending_regime = None
            self._pending_count = 0
        elif raw == self._pending_regime:
            self._pending_count += 1
            if self._pending_count >= debounce:
                prev = self._current_regime
                self._current_regime = raw
                self._regime_since = utc_iso()
                self._pending_regime = None
                self._pending_count = 0
                logger.warning(
                    f"REGIME CHANGE: {prev} → {raw} "
                    f"(vol={metrics['vol_24h']} dir={metrics['directional_strength']} "
                    f"disp={metrics['dispersion']})"
                )
                try:
                    await send_alert(
                        f"🎚️ <b>Regime change</b>: {prev} → {raw}\n"
                        f"vol={metrics['vol_24h']:.3f} "
                        f"dir={metrics['directional_strength']:.4f} "
                        f"disp={metrics['dispersion']:.4f}"
                    )
                except Exception:
                    pass
        else:
            self._pending_regime = raw
            self._pending_count = 1

        # One-line per-cycle log for observability
        gates = [name for name in ("ema", "flow", "mr", "yield") if self.entry_allowed(name)]
        logger.info(
            f"REGIME: {self._current_regime} "
            f"(vol={metrics['vol_24h']} dir={metrics['directional_strength']} "
            f"disp={metrics['dispersion']}) gates={','.join(gates) or 'none'}"
        )

    def classify(self) -> str:
        """Return the most recent raw (un-debounced) classification."""
        return self._last_metrics.get("raw_regime", DEAD)

    @property
    def current_regime(self) -> str:
        """Debounced regime the bot is currently acting on."""
        return self._current_regime

    @property
    def regime_since(self) -> str:
        return self._regime_since

    @property
    def metrics(self) -> dict:
        return dict(self._last_metrics)

    @property
    def enabled(self) -> bool:
        return bool(self._settings.REGIME_ENABLED)

    def entry_allowed(self, strategy: str) -> bool:
        """True if ``strategy`` is permitted to open new entries this cycle.

        Kill-switch short-circuits to ``True`` — exits always run, the filter
        never blocks anything when disabled. ``strategy`` is the gate name
        from the env config: ``"ema"``, ``"flow"``, ``"mr"``, or ``"yield"``.
        """
        if not self.enabled:
            return True
        gate = self._gate_for(strategy)
        return self._current_regime in gate

    def gates_map(self) -> dict[str, bool]:
        """Per-strategy allow map for the /api/regime response."""
        return {name: self.entry_allowed(name) for name in ("ema", "flow", "mr", "yield")}

    # ── Internals ──────────────────────────────────────────────

    def _gate_for(self, strategy: str) -> frozenset[str]:
        key = (strategy or "").strip().lower()
        attr = {
            "ema": "REGIME_GATE_EMA",
            "flow": "REGIME_GATE_FLOW",
            "mr": "REGIME_GATE_MR",
            "meanrev": "REGIME_GATE_MR",
            "yield": "REGIME_GATE_YIELD",
        }.get(key)
        if not attr:
            # Unknown strategy → conservative: allow in all regimes (match
            # Yield default) so an unmapped manager doesn't deadlock itself
            # silently.
            return frozenset(_ALL_REGIMES)
        return _parse_gate(getattr(self._settings, attr, ""))

    async def _compute_metrics(self) -> dict:
        window_hours = max(1, int(self._settings.REGIME_VOL_WINDOW_HOURS))

        since_dt = utc_now() - timedelta(hours=window_hours)
        since_iso = since_dt.isoformat()

        # Pull distinct netuids present in the window
        netuid_rows = await self._db.fetchall(
            "SELECT DISTINCT netuid FROM pool_snapshots WHERE ts >= ? AND netuid != 0",
            (since_iso,),
        )
        netuids = [int(r["netuid"]) for r in netuid_rows]

        per_netuid: dict[int, list[dict]] = {}
        for netuid in netuids:
            snaps = await self._db.get_pool_snapshots(netuid=netuid, since_ts=since_iso)
            if snaps:
                per_netuid[netuid] = snaps

        metrics = compute_regime_metrics(per_netuid, self._settings)
        metrics["updated_at"] = utc_iso()
        return metrics

    def _classify_raw(self, vol: float, dir_strength: float, dispersion: float) -> str:
        """Backwards-compatible wrapper around :func:`classify_regime`."""
        return classify_regime(vol, dir_strength, dispersion, self._settings)
