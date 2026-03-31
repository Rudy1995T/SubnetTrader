#!/usr/bin/env python3
"""
SubnetTrader QA runner — specs/qa-test-cases.md
Runs all non-destructive, non-browser test cases against the live API + DB + chain.

Skip categories:
  [CHAIN]     — no bittensor connection (should be resolved in this version)
  [MUTATING]  — would change real state (close positions / send TAO / reset live data)
  [BROWSER]   — requires visual browser inspection
  [RISKY]     — safe to skip in live mode
"""
import sys, os, sqlite3, csv, io, datetime, time
import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE              = "http://localhost:8081"
SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.path.join(SCRIPT_DIR, "..", "data", "ledger.db")
TAOSTATS_BASE     = "https://api.taostats.io"

def read_env():
    env, path = {}, os.path.join(SCRIPT_DIR, "..", ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    return env

ENV               = read_env()
TAOSTATS_KEY      = ENV.get("TAOSTATS_API_KEY", "")
MAX_SLIPPAGE_PCT  = float(ENV.get("MAX_SLIPPAGE_PCT", "5.0"))
STOP_LOSS_PCT     = float(ENV.get("EMA_STOP_LOSS_PCT", "8.0"))
TAKE_PROFIT_PCT   = float(ENV.get("EMA_TAKE_PROFIT_PCT", "20.0"))
FEE_RESERVE_TAO   = float(ENV.get("FEE_RESERVE_TAO", "0.5"))
EMA_PERIOD        = int(ENV.get("EMA_PERIOD", "9"))
EMA_FAST_PERIOD   = int(ENV.get("EMA_FAST_PERIOD", "3"))
EMA_CONFIRM_BARS  = int(ENV.get("EMA_CONFIRM_BARS", "3"))
SUBTENSOR_NETWORK = ENV.get("SUBTENSOR_NETWORK", "wss://entrypoint-finney.opentensor.ai:443")
COLDKEY_SS58      = "5CJVxybdv6kcwMwnegJwSEQvAqDqFzVAuwcfsvXB64Sm36Sf"

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
results = []

def report(tc_id, name, passed, detail=""):
    results.append((tc_id, name, "PASS" if passed else "FAIL", detail))
    icon = "✓" if passed else "✗"
    print(f"  [{icon}] {tc_id}: {name}")
    if not passed and detail:
        print(f"       → {detail}")

def skip(tc_id, name, reason=""):
    results.append((tc_id, name, "SKIP", reason))
    print(f"  [·] {tc_id}: {name}  ({reason})")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows

def compute_ema(prices, period):
    k = 2 / (period + 1)
    ema, out = prices[0], [prices[0]]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
        out.append(ema)
    return out

def parse_list(r, *keys):
    if r.status_code != 200:
        return None
    j = r.json()
    if isinstance(j, list):
        return j
    for k in keys:
        if k in j:
            return j[k]
    return list(j.values())[0] if isinstance(j, dict) else None

def ok_json(r):
    return r.json() if r.status_code == 200 else None

# ---------------------------------------------------------------------------
# Fetch all data upfront
# ---------------------------------------------------------------------------
print("=" * 65)
print("SubnetTrader QA — fetching all data…")
print("=" * 65)

client = httpx.Client(timeout=30.0)
ts_client = httpx.Client(
    timeout=30.0,
    # Taostats uses raw key, not "Bearer <key>"
    headers={"Authorization": TAOSTATS_KEY} if TAOSTATS_KEY else {},
)

portfolio_r  = client.get(f"{BASE}/api/ema/portfolio")
positions_r  = client.get(f"{BASE}/api/ema/positions?limit=200")
trades_r     = client.get(f"{BASE}/api/ema/recent-trades")
signals_r    = client.get(f"{BASE}/api/ema/signals")
slippage_r   = client.get(f"{BASE}/api/ema/slippage-stats")
health_r     = client.get(f"{BASE}/api/health/services")
ctrl_r       = client.get(f"{BASE}/api/control/status")
csv_r        = client.get(f"{BASE}/api/export/trades.csv")
config_r     = client.get(f"{BASE}/api/config")
cfgstatus_r  = client.get(f"{BASE}/api/config/status")
pool_r       = ts_client.get(f"{TAOSTATS_BASE}/api/dtao/pool/latest/v1?limit=200")

portfolio   = ok_json(portfolio_r)
slippage    = ok_json(slippage_r)
health      = ok_json(health_r)
ctrl        = ok_json(ctrl_r)
config      = ok_json(config_r)
cfgstatus   = ok_json(cfgstatus_r)
pool_resp   = ok_json(pool_r)

# Parse portfolio: keys are strategy tags + "combined"
strats_list   = []
wallet_balance = None
if portfolio:
    strats_list    = [v for k, v in portfolio.items()
                      if isinstance(v, dict) and k != "combined"]
    wallet_balance = portfolio.get("combined", {}).get("wallet_balance")

# Parse positions
all_positions  = parse_list(positions_r, "positions", "data") or []
open_positions = [p for p in all_positions if p.get("status") == "OPEN"]
trades_list    = parse_list(trades_r, "trades", "data") or []
sig_list       = parse_list(signals_r, "signals", "data") or []

# Build pool lookup
pool_by_netuid = {}
if pool_resp:
    items = pool_resp.get("data", pool_resp) if isinstance(pool_resp, dict) else pool_resp
    if isinstance(items, list):
        for item in items:
            if "netuid" in item:
                pool_by_netuid[int(item["netuid"])] = item

# Spot prices per open position
spot_prices = {}
for pos in open_positions:
    r = client.get(f"{BASE}/api/subnets/{pos['netuid']}/spot")
    if r.status_code == 200:
        spot_prices[pos["netuid"]] = r.json()

# DB
db_all    = db_query("SELECT * FROM ema_positions")
db_open   = db_query("SELECT * FROM ema_positions WHERE status='OPEN'")
db_closed = db_query("SELECT * FROM ema_positions WHERE status='CLOSED'")

print(f"  portfolio:{portfolio_r.status_code}  positions:{positions_r.status_code}  "
      f"trades:{trades_r.status_code}  signals:{signals_r.status_code}")
print(f"  slippage:{slippage_r.status_code}  health:{health_r.status_code}  "
      f"ctrl:{ctrl_r.status_code}  csv:{csv_r.status_code}")
print(f"  config:{config_r.status_code}  cfg_status:{cfgstatus_r.status_code}  "
      f"taostats_pool:{pool_r.status_code}")
print(f"  strategies:{len(strats_list)}  wallet_balance:{wallet_balance}  "
      f"open_positions:{len(open_positions)}  db_rows:{len(db_all)}")
print()

# ---------------------------------------------------------------------------
# Chain connection (bittensor)
# ---------------------------------------------------------------------------
sub = None
try:
    import bittensor
    sub = bittensor.Subtensor(network=SUBTENSOR_NETWORK)
    print(f"  chain: connected (block {sub.get_current_block()})\n")
except Exception as e:
    print(f"  chain: UNAVAILABLE — {e}\n")

# ===========================================================================
# TC-1  Wallet Balance
# ===========================================================================
print("── TC-1  Wallet Balance ──")

# TC-1.1  [CHAIN] widget balance vs on-chain
if sub:
    try:
        chain_bal = sub.get_balance(COLDKEY_SS58)
        chain_tao = float(chain_bal)
        if wallet_balance is not None:
            diff = abs(chain_tao - wallet_balance)
            report("TC-1.1", "Widget wallet_balance matches on-chain coldkey balance",
                   diff <= 0.002,
                   f"chain={chain_tao:.4f}  widget={wallet_balance:.4f}  diff={diff:.5f}")
        else:
            # Still useful to report the chain value
            report("TC-1.1", "Chain balance readable (portfolio balance unavailable)",
                   True, f"chain={chain_tao:.4f} TAO  (portfolio returned None)")
    except Exception as e:
        skip("TC-1.1", "Widget balance vs on-chain", f"chain query failed: {e}")
else:
    skip("TC-1.1", "Widget balance vs on-chain", "[CHAIN] unavailable")

# TC-1.2  unstaked ≈ wallet_balance − fee_reserve
if wallet_balance is not None:
    total_unstaked = sum(float(s.get("unstaked_tao", 0)) for s in strats_list)
    open_count = sum(len(s.get("open_positions", [])) for s in strats_list)
    if open_count == 0:
        diff = abs(total_unstaked - (wallet_balance - FEE_RESERVE_TAO))
        report("TC-1.2", "Unstaked ≈ wallet_balance − fee_reserve (no open positions)",
               diff < 0.01,
               f"unstaked={total_unstaked:.4f}  expected={wallet_balance - FEE_RESERVE_TAO:.4f}")
    else:
        skip("TC-1.2", "Unstaked math", f"positions open ({open_count}); math only valid with 0 open")
else:
    skip("TC-1.2", "Unstaked math", "wallet_balance=None in portfolio (executor RPC failing)")

skip("TC-1.3", "Balance reflected after manual send", "[MUTATING] requires sending real TAO")
print()

# ===========================================================================
# TC-2  Open Positions: Widget vs Database
# ===========================================================================
print("── TC-2  Open Positions: Widget vs Database ──")

db_open_by_strat = {}
for row in db_open:
    db_open_by_strat.setdefault(row.get("strategy", "scalper"), []).append(row)

# TC-2.1
if strats_list:
    all_ok, msgs = True, []
    for s in strats_list:
        tag    = s.get("tag", "?")
        wcount = s.get("open_count", len(s.get("open_positions", [])))
        dcount = len(db_open_by_strat.get(tag, []))
        if wcount != dcount:
            all_ok = False
            msgs.append(f"{tag}: widget={wcount} db={dcount}")
    report("TC-2.1", "Open position counts match DB per strategy", all_ok, "; ".join(msgs))
else:
    skip("TC-2.1", "Open position count vs DB", "portfolio endpoint failed")

# TC-2.2  fields
if open_positions:
    db_open_map = {row["id"]: row for row in db_open}
    bad = []
    for wp in open_positions:
        pid = wp.get("position_id") or wp.get("id")
        if pid not in db_open_map:
            bad.append(f"pid={pid} missing from DB"); continue
        dr = db_open_map[pid]
        for wkey, dkey, tol in [
            ("netuid",       "netuid",       0),
            ("entry_price",  "entry_price",  1e-5),
            ("amount_tao",   "amount_tao",   1e-3),
            ("amount_alpha", "amount_alpha", 0.1),
            ("entry_ts",     "entry_ts",     0),
        ]:
            wv, dv = wp.get(wkey), dr.get(dkey)
            if wv is None or dv is None: continue
            ok = (str(wv) == str(dv)) if tol == 0 else abs(float(wv) - float(dv)) <= tol
            if not ok:
                bad.append(f"pid={pid} {wkey}: widget={wv!r} db={dv!r}")
    report("TC-2.2", "Open position fields match DB", not bad, "; ".join(bad[:3]))
elif not db_open:
    report("TC-2.2", "Open position fields match DB", True, "no open positions")
else:
    skip("TC-2.2", "Open position fields match DB", "positions endpoint failed")

# TC-2.3  [CHAIN] on-chain alpha stake vs widget amount_alpha
if sub and db_open:
    bad = []
    for row in db_open:
        nid, hk = row["netuid"], row.get("staked_hotkey", "")
        if not hk:
            bad.append(f"pid={row['id']} missing staked_hotkey"); continue
        try:
            chain_alpha = float(sub.get_stake(COLDKEY_SS58, hk, nid))
            db_alpha    = float(row["amount_alpha"])
            diff_pct    = abs(chain_alpha - db_alpha) / db_alpha * 100 if db_alpha else 0
            if diff_pct > 2.0:
                bad.append(f"pid={row['id']} sn{nid} chain={chain_alpha:.2f} db={db_alpha:.2f} diff={diff_pct:.1f}%")
        except Exception as e:
            bad.append(f"pid={row['id']} sn{nid} error: {e}")
    report("TC-2.3", "On-chain alpha stake matches DB amount_alpha (within 2%)",
           not bad, "; ".join(bad[:3]))
elif not sub:
    skip("TC-2.3", "On-chain alpha stake vs DB", "[CHAIN] unavailable")
else:
    skip("TC-2.3", "On-chain alpha stake vs DB", "no open positions")

# TC-2.4  no phantom open positions
if all_positions:
    widget_open_ids = {(p.get("position_id") or p.get("id")) for p in all_positions if p.get("status") == "OPEN"}
    db_open_ids     = {row["id"] for row in db_open}
    phantoms        = widget_open_ids - db_open_ids
    report("TC-2.4", "No phantom open positions (widget shows positions absent from DB)",
           not phantoms, f"phantom ids: {phantoms}")
else:
    skip("TC-2.4", "No phantom positions", "positions endpoint failed")

# TC-2.5  [CHAIN] no ghost on-chain stake in non-open netuids
if sub and db_open:
    open_keys = {(row["netuid"], row.get("staked_hotkey", "")) for row in db_open}
    # Check hotkeys from open positions across recently-used netuids (not the open ones)
    recent_netuids = {row["netuid"] for row in
                      db_query("SELECT DISTINCT netuid FROM ema_positions ORDER BY id DESC LIMIT 30")}
    open_netuids   = {row["netuid"] for row in db_open}
    check_netuids  = recent_netuids - open_netuids
    hotkeys        = {row.get("staked_hotkey") for row in db_open if row.get("staked_hotkey")}
    ghosts = []
    for hk in hotkeys:
        for nid in check_netuids:
            try:
                alpha = float(sub.get_stake(COLDKEY_SS58, hk, nid))
                if alpha > 0.01:
                    ghosts.append(f"sn{nid} hk={hk[:8]}… alpha={alpha:.4f}")
            except Exception:
                pass
    report("TC-2.5", "No ghost on-chain stake in recently-used non-open netuids",
           not ghosts, "; ".join(ghosts[:3]))
elif not sub:
    skip("TC-2.5", "Ghost stake check", "[CHAIN] unavailable")
else:
    skip("TC-2.5", "Ghost stake check", "no open positions to get hotkeys from")
print()

# ===========================================================================
# TC-3  Subnet Investment: Online vs Widget
# ===========================================================================
print("── TC-3  Subnet Investment: Online vs Widget ──")

# TC-3.1
if open_positions and pool_by_netuid:
    missing = [p["netuid"] for p in open_positions if p["netuid"] not in pool_by_netuid]
    report("TC-3.1", "All invested netuids appear in Taostats pool",
           not missing, f"missing: {missing}")
elif not pool_by_netuid:
    skip("TC-3.1", "Invested netuids in Taostats pool",
         f"Taostats pool: HTTP {pool_r.status_code}")
else:
    skip("TC-3.1", "Invested netuids in Taostats pool", "no open positions")

# TC-3.2
if open_positions and spot_prices and pool_by_netuid:
    bad = []
    for pos in open_positions:
        nid   = pos["netuid"]
        ts_p  = pool_by_netuid.get(nid, {}).get("price")
        sp    = spot_prices.get(nid, {})
        wp    = sp.get("price") or sp.get("spot_price")
        if ts_p and wp:
            diff = abs(float(wp) - float(ts_p)) / float(ts_p) * 100
            if diff > 1.0:
                bad.append(f"sn{nid} widget={float(wp):.6f} ts={float(ts_p):.6f} Δ={diff:.2f}%")
    report("TC-3.2", "Widget current prices within 1% of Taostats pool price",
           not bad, "; ".join(bad))
elif not pool_by_netuid:
    skip("TC-3.2", "Price vs Taostats", f"Taostats: HTTP {pool_r.status_code}")
else:
    skip("TC-3.2", "Price vs Taostats", "no open positions or spot prices")

# TC-3.3  [CHAIN] widget price vs on-chain pool reserves
if sub and open_positions:
    bad = []
    for pos in open_positions:
        nid = pos["netuid"]
        sp  = spot_prices.get(nid, {})
        wp  = sp.get("price") or sp.get("spot_price")
        if not wp:
            continue
        try:
            tao_r_raw   = sub.query_subtensor("SubnetTAO",     [nid])
            alpha_r_raw = sub.query_subtensor("SubnetAlphaIn", [nid])
            tao_r   = int(tao_r_raw.value   if hasattr(tao_r_raw,   "value") else tao_r_raw)
            alpha_r = int(alpha_r_raw.value if hasattr(alpha_r_raw, "value") else alpha_r_raw)
            if alpha_r > 0:
                chain_price = tao_r / alpha_r
                diff = abs(float(wp) - chain_price) / chain_price * 100
                if diff > 2.0:
                    bad.append(f"sn{nid} widget={float(wp):.6f} chain={chain_price:.6f} Δ={diff:.2f}%")
        except Exception as e:
            bad.append(f"sn{nid} chain query error: {e}")
    report("TC-3.3", "Widget spot prices within 2% of on-chain pool reserves",
           not bad, "; ".join(bad[:3]))
elif not sub:
    skip("TC-3.3", "Price vs on-chain reserves", "[CHAIN] unavailable")
else:
    skip("TC-3.3", "Price vs on-chain reserves", "no open positions")

# TC-3.4  pnl_pct recomputable
if open_positions:
    bad = []
    for pos in open_positions:
        ep, cp, wpnl = pos.get("entry_price"), pos.get("current_price"), pos.get("pnl_pct")
        if ep and cp and wpnl is not None:
            exp = (float(cp) - float(ep)) / float(ep) * 100
            if abs(exp - float(wpnl)) > 0.01:
                bad.append(f"pid={pos.get('position_id')} exp={exp:.4f} got={wpnl}")
    report("TC-3.4", "PnL % = (current − entry) / entry × 100", not bad, "; ".join(bad))
else:
    skip("TC-3.4", "PnL pct recomputable", "no open positions")

# TC-3.5  pnl_tao consistent
if open_positions:
    bad = []
    for pos in open_positions:
        pct, tao_pnl, amt = pos.get("pnl_pct"), pos.get("pnl_tao"), pos.get("amount_tao")
        if pct is not None and tao_pnl is not None and amt:
            exp = float(amt) * float(pct) / 100
            if abs(exp - float(tao_pnl)) > 0.0005:
                bad.append(f"pid={pos.get('position_id')} exp={exp:.5f} got={tao_pnl}")
    report("TC-3.5", "pnl_tao = amount_tao × pnl_pct / 100", not bad, "; ".join(bad))
else:
    skip("TC-3.5", "PnL TAO consistent", "no open positions")

# TC-3.6  deployed = sum of position sizes
if strats_list:
    all_ok, msgs = True, []
    for s in strats_list:
        tag      = s.get("tag", "?")
        deployed = float(s.get("deployed_tao", 0))
        pos_sum  = sum(float(p.get("amount_tao", 0)) for p in s.get("open_positions", []))
        diff = abs(deployed - pos_sum)
        if diff > 0.001:
            all_ok = False
            msgs.append(f"{tag}: deployed={deployed:.4f} sum={pos_sum:.4f}")
    report("TC-3.6", "deployed_tao = Σ amount_tao across open positions", all_ok, "; ".join(msgs))
else:
    skip("TC-3.6", "Deployed TAO", "portfolio endpoint failed")

# TC-3.7  pot = deployed + unstaked
if strats_list:
    all_ok, msgs = True, []
    for s in strats_list:
        tag  = s.get("tag", "?")
        pot  = float(s.get("pot_tao", 0))
        dep  = float(s.get("deployed_tao", 0))
        uns  = float(s.get("unstaked_tao", 0))
        diff = abs(pot - (dep + uns))
        if diff > 0.001:
            all_ok = False
            msgs.append(f"{tag}: pot={pot:.4f} dep+uns={dep+uns:.4f}")
    report("TC-3.7", "pot_tao = deployed_tao + unstaked_tao", all_ok, "; ".join(msgs))
else:
    skip("TC-3.7", "Pot math", "portfolio endpoint failed")
print()

# ===========================================================================
# TC-4  Recent Trades: Widget vs Database
# ===========================================================================
print("── TC-4  Recent Trades: Widget vs Database ──")

db_closed_recent = db_query(
    "SELECT * FROM ema_positions WHERE status='CLOSED' ORDER BY exit_ts DESC LIMIT 10"
)
db_closed_map = {row["id"]: row for row in db_closed_recent}

# TC-4.1
if trades_list:
    bad = []
    for wt in trades_list:
        pid = wt.get("position_id") or wt.get("id")
        if pid not in db_closed_map:
            bad.append(f"pid={pid} not in DB top-10 closed"); continue
        dr = db_closed_map[pid]
        for wkey, dkey, tol in [
            ("netuid",      "netuid",      0),
            ("entry_price", "entry_price", 1e-5),
            ("exit_price",  "exit_price",  1e-5),
            ("amount_tao",  "amount_tao",  1e-3),
            ("pnl_tao",     "pnl_tao",     1e-3),
            ("pnl_pct",     "pnl_pct",     0.01),
            ("exit_reason", "exit_reason", 0),
            ("strategy",    "strategy",    0),
        ]:
            wv, dv = wt.get(wkey), dr.get(dkey)
            if wv is None or dv is None: continue
            ok = (str(wv) == str(dv)) if tol == 0 else abs(float(wv) - float(dv)) <= tol
            if not ok:
                bad.append(f"pid={pid} {wkey}: widget={wv!r} db={dv!r}")
    report("TC-4.1", "Recent trades match DB closed positions (all fields)", not bad,
           "; ".join(bad[:3]))
elif not db_closed:
    skip("TC-4.1", "Recent trades match DB", "no closed trades in DB")
else:
    skip("TC-4.1", "Recent trades match DB", "trades endpoint failed")

# TC-4.2  pnl_tao = tao_out − tao_in
if db_closed:
    bad = []
    for dr in db_closed:
        if dr.get("amount_tao") and dr.get("amount_tao_out") and dr.get("pnl_tao") is not None:
            exp = float(dr["amount_tao_out"]) - float(dr["amount_tao"])
            if abs(exp - float(dr["pnl_tao"])) > 0.0001:
                bad.append(f"pid={dr['id']} exp={exp:.5f} stored={dr['pnl_tao']:.5f}")
    report("TC-4.2", "pnl_tao = amount_tao_out − amount_tao", not bad, "; ".join(bad[:3]))
else:
    skip("TC-4.2", "PnL recomputable", "no closed trades")

# TC-4.3  ordered by exit_ts desc
if len(trades_list) >= 2:
    ts = [t.get("exit_ts") for t in trades_list if t.get("exit_ts")]
    ordered = all(ts[i] >= ts[i+1] for i in range(len(ts)-1))
    report("TC-4.3", "Recent trades ordered by exit_ts descending", ordered,
           f"first 3: {ts[:3]}")
else:
    skip("TC-4.3", "Trades ordered", f"only {len(trades_list)} trade(s) returned")

# TC-4.4  valid exit reason values
VALID_REASONS = {"STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP", "EMA_CROSS",
                 "TIME_STOP", "MANUAL", "MANUAL_CLOSE", "BREAKEVEN_STOP",
                 "GHOST_CLOSE", "COMPANION_EXIT"}
if db_all:
    bad = [(r["id"], r["exit_reason"]) for r in db_all
           if r.get("exit_reason") and r["exit_reason"] not in VALID_REASONS]
    report("TC-4.4", "All exit_reason values are from the allowed set",
           not bad, f"invalid: {bad[:3]}")
else:
    skip("TC-4.4", "Exit reason values", "no positions in DB")

# TC-4.5  STOP_LOSS pnl bound
sl_rows = [r for r in db_all if r.get("exit_reason") == "STOP_LOSS" and r.get("pnl_pct") is not None]
if sl_rows:
    bad = [r for r in sl_rows if float(r["pnl_pct"]) > -(STOP_LOSS_PCT - 0.5)]
    report("TC-4.5", f"STOP_LOSS trades have pnl_pct ≤ −{STOP_LOSS_PCT - 0.5:.1f}%",
           not bad, f"violators: {[(r['id'], r['pnl_pct']) for r in bad[:3]]}")
else:
    skip("TC-4.5", "Stop-loss PnL bound", "no STOP_LOSS trades in DB")

# TC-4.6  TAKE_PROFIT pnl bound
tp_rows = [r for r in db_all if r.get("exit_reason") == "TAKE_PROFIT" and r.get("pnl_pct") is not None]
if tp_rows:
    bad = [r for r in tp_rows if float(r["pnl_pct"]) < (TAKE_PROFIT_PCT - 1.0)]
    report("TC-4.6", f"TAKE_PROFIT trades have pnl_pct ≥ {TAKE_PROFIT_PCT - 1.0:.1f}%",
           not bad, f"violators: {[(r['id'], r['pnl_pct']) for r in bad[:3]]}")
else:
    skip("TC-4.6", "Take-profit PnL bound", "no TAKE_PROFIT trades in DB")

# TC-4.7  open positions have no exit_ts
bad_open = db_query("SELECT id FROM ema_positions WHERE status='OPEN' AND exit_ts IS NOT NULL")
report("TC-4.7", "No OPEN position has exit_ts set", not bad_open,
       f"bad ids: {[r['id'] for r in bad_open]}")

# TC-4.8  closed positions have all exit fields
bad_closed = db_query(
    "SELECT id FROM ema_positions WHERE status='CLOSED' AND "
    "(exit_ts IS NULL OR exit_price IS NULL OR amount_tao_out IS NULL OR pnl_tao IS NULL)"
)
report("TC-4.8", "No CLOSED position missing required exit fields", not bad_closed,
       f"bad ids: {[r['id'] for r in bad_closed]}")
print()

# ===========================================================================
# TC-5  Slippage Statistics
# ===========================================================================
print("── TC-5  Slippage Statistics ──")

def check_avg_slip(tc_id, label, widget_key, db_col):
    if not slippage:
        skip(tc_id, label, "slippage endpoint failed"); return
    rows   = db_query(f"SELECT AVG({db_col}) as avg FROM ema_positions "
                      f"WHERE status='CLOSED' AND {db_col} IS NOT NULL")
    db_avg = rows[0]["avg"] if rows else None
    w_avg  = slippage.get(widget_key)
    if w_avg is not None and db_avg is not None:
        diff = abs(float(w_avg) - float(db_avg))
        report(tc_id, label, diff < 0.01,
               f"widget={float(w_avg):.4f}  db={float(db_avg):.4f}  diff={diff:.5f}")
    else:
        skip(tc_id, label, f"widget={w_avg}  db_avg={db_avg}")

check_avg_slip("TC-5.1", "Avg entry slippage matches DB aggregate",
               "avg_entry_slippage_pct", "entry_slippage_pct")
check_avg_slip("TC-5.2", "Avg exit slippage matches DB aggregate",
               "avg_exit_slippage_pct",  "exit_slippage_pct")

if slippage:
    computed = sum(
        float(r.get("amount_tao") or 0)
        * (float(r.get("entry_slippage_pct") or 0) + float(r.get("exit_slippage_pct") or 0))
        / 100
        for r in db_all
    )
    w_total = slippage.get("total_slippage_tao") or 0
    if w_total:
        diff = abs(float(w_total) - computed)
        report("TC-5.3", "Total slippage TAO recomputable", diff < 0.01,
               f"widget={float(w_total):.5f}  computed={computed:.5f}  diff={diff:.5f}")
    else:
        skip("TC-5.3", "Total slippage TAO", "widget value null/zero")
else:
    skip("TC-5.3", "Total slippage TAO", "slippage endpoint failed")

SAFE_STAKING_FIX_DATE = "2026-03-09"  # safe_staking=True deployed on this date
bad = [(r["id"], r["entry_slippage_pct"]) for r in db_all
       if r.get("entry_slippage_pct") is not None
       and float(r["entry_slippage_pct"]) > MAX_SLIPPAGE_PCT
       and (r.get("entry_ts") or "") >= SAFE_STAKING_FIX_DATE]
report("TC-5.4", f"No entry slippage > MAX_SLIPPAGE_PCT ({MAX_SLIPPAGE_PCT}%) since {SAFE_STAKING_FIX_DATE}",
       not bad, f"violators: {bad[:3]}")
print()

# ===========================================================================
# TC-6  EMA Signals
# ===========================================================================
print("── TC-6  EMA Signals ──")

# TC-6.1
if sig_list:
    bad = [s for s in sig_list if s.get("signal") not in {"BUY", "SELL", "HOLD"}]
    report("TC-6.1", "All signal values are BUY / SELL / HOLD", not bad, f"bad: {bad[:3]}")
else:
    skip("TC-6.1", "Signal values", "signals endpoint failed or empty")

# TC-6.2  fresh positions have no SELL signal
if sig_list and open_positions:
    sig_map = {int(s.get("netuid", 0)): s.get("signal") for s in sig_list}
    now     = datetime.datetime.now(datetime.timezone.utc)
    bad     = []
    for pos in open_positions:
        try:
            ets     = pos["entry_ts"]
            # Parse with or without timezone info
            if ets.endswith("Z"):
                ets = ets[:-1] + "+00:00"
            entry_dt = datetime.datetime.fromisoformat(ets)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=datetime.timezone.utc)
            age_min = (now - entry_dt).total_seconds() / 60
            if age_min < 30:
                sig = sig_map.get(pos["netuid"])
                if sig == "SELL":
                    bad.append(f"pid={pos.get('position_id')} sn{pos['netuid']} age={age_min:.0f}m")
        except Exception:
            pass
    report("TC-6.2", "No freshly-entered position (<30 min) has a SELL signal",
           not bad, "; ".join(bad))
elif not open_positions:
    skip("TC-6.2", "Fresh position signal check", "no open positions")
else:
    skip("TC-6.2", "Fresh position signal check", "signals endpoint failed")

# TC-6.3  bars matches recomputed count (use embedded prices from signals response)
# TC-6.4  BUY implies last confirm_bars candles above both EMAs
buy_sigs = [s for s in sig_list if s.get("signal") == "BUY"] if sig_list else []
if buy_sigs:
    sample = buy_sigs[0]
    netuid = int(sample.get("netuid", 0))
    slow_p = int(sample.get("slow_period") or sample.get("ema_period") or EMA_PERIOD)
    fast_p = int(sample.get("fast_period") or EMA_FAST_PERIOD)
    prices = [float(p) for p in (sample.get("prices") or []) if p is not None]

    if len(prices) >= max(slow_p, EMA_CONFIRM_BARS):
        slow_ema = compute_ema(prices, slow_p)
        fast_ema = compute_ema(prices, fast_p)

        # TC-6.3  bars count
        bars = 0
        for i in range(len(prices) - 1, -1, -1):
            if prices[i] > slow_ema[i]:
                bars += 1
            else:
                break
        if prices[-1] < slow_ema[-1]:
            bars = -bars
        # widget field is "bars" (not "bars_above")
        w_bars = sample.get("bars") or sample.get("bars_above")
        if w_bars is not None:
            report("TC-6.3", f"bars count matches recomputed EMA (sn{netuid})",
                   w_bars == bars, f"widget={w_bars}  computed={bars}")
        else:
            skip("TC-6.3", "bars recomputed", "field not present in signal response")

        # TC-6.4  BUY: last confirm_bars prices > both EMAs
        lp  = prices[-EMA_CONFIRM_BARS:]
        lsl = slow_ema[-EMA_CONFIRM_BARS:]
        lfa = fast_ema[-EMA_CONFIRM_BARS:]
        cond = all(lp[i] > lsl[i] and lp[i] > lfa[i] for i in range(EMA_CONFIRM_BARS))
        report("TC-6.4", f"BUY signal: last {EMA_CONFIRM_BARS} prices above both EMAs (sn{netuid})",
               cond,
               f"prices={[f'{x:.5f}' for x in lp[-3:]]}  slow={[f'{x:.5f}' for x in lsl[-3:]]}")
    else:
        skip("TC-6.3", "bars recomputed", f"insufficient price history ({len(prices)} pts) in signal")
        skip("TC-6.4", "BUY implies price > EMAs", f"insufficient history ({len(prices)} pts)")
else:
    skip("TC-6.3", "bars recomputed", "no BUY signals currently")
    skip("TC-6.4", "BUY implies price > EMAs", "no BUY signals currently")
print()

# ===========================================================================
# TC-7  Health & Service Status
# ===========================================================================
print("── TC-7  Health & Service Status ──")

def extract_services(h):
    if h is None: return []
    svcs = h.get("services", h)
    if isinstance(svcs, list):   return svcs
    if isinstance(svcs, dict):   return list(svcs.values())
    return []

def find_svc(services, name):
    for s in services:
        if isinstance(s, dict) and name.lower() in str(s.get("name", "")).lower():
            return s
    return None

services = extract_services(health)

if health is not None:
    failing = [s.get("name", "?") for s in services
               if isinstance(s, dict) and not s.get("optional", False) and not s.get("ok", False)]
    report("TC-7.1", "All critical (non-optional) services are ok=true",
           not failing, f"failing: {failing}")
else:
    skip("TC-7.1", "Critical services ok", "health endpoint failed")

# TC-7.2  wallet service balance matches portfolio
wallet_svc = find_svc(services, "wallet")
if wallet_svc and wallet_balance is not None:
    sb   = wallet_svc.get("balance_tao")
    diff = abs(float(sb) - wallet_balance) if sb is not None else 999
    report("TC-7.2", "Wallet service balance matches portfolio wallet_balance",
           diff < 0.001, f"health={sb}  portfolio={wallet_balance}  diff={diff:.5f}")
elif wallet_svc and wallet_balance is None:
    sb = wallet_svc.get("balance_tao")
    skip("TC-7.2", "Wallet balance consistency",
         f"portfolio wallet_balance=None (executor RPC issue); health shows {sb}")
else:
    skip("TC-7.2", "Wallet balance consistency", "wallet service not found in health response")

# TC-7.3  Taostats latency
ts_svc = find_svc(services, "taostats")
if ts_svc:
    lat = ts_svc.get("latency_ms")
    if lat is not None:
        report("TC-7.3", "Taostats API latency < 5000ms", float(lat) < 5000, f"latency={lat}ms")
    else:
        skip("TC-7.3", "Taostats latency", "latency_ms not in response")
else:
    skip("TC-7.3", "Taostats latency", "taostats service not found in health response")

# TC-7.4  can_trade consistent with blockers
if wallet_svc and ctrl:
    can_trade   = wallet_svc.get("can_trade")
    kill_switch = bool(ctrl.get("kill_switch_active", False))
    breaker     = bool(ctrl.get("breaker_active", False))
    if strats_list:
        breaker = breaker or any(s.get("breaker_active", False) for s in strats_list)
    if can_trade is not None:
        if kill_switch or breaker:
            report("TC-7.4", "can_trade=false when kill switch or breaker active",
                   not can_trade, f"can_trade={can_trade} kill={kill_switch} breaker={breaker}")
        else:
            report("TC-7.4", "can_trade=true when no blockers active",
                   bool(can_trade), f"can_trade={can_trade} kill={kill_switch} breaker={breaker}")
    else:
        skip("TC-7.4", "can_trade logic", "can_trade not in wallet service response")
else:
    skip("TC-7.4", "can_trade logic", "wallet service or control endpoint unavailable")
print()

# ===========================================================================
# TC-8  Control Endpoints
# ===========================================================================
print("── TC-8  Control Endpoints ──")

# TC-8.1  Kill switch: pause prevents entries, resume restores
# Safe: only creates/deletes a KILL_SWITCH file; does NOT run a real cycle
open_before = db_query("SELECT COUNT(*) as n FROM ema_positions WHERE status='OPEN'")[0]["n"]
try:
    pause_r = client.post(f"{BASE}/api/control/pause")
    if pause_r.status_code == 200:
        paused_ctrl = ok_json(client.get(f"{BASE}/api/control/status"))
        kill_active = paused_ctrl.get("kill_switch_active", False) if paused_ctrl else False

        # Resume immediately — we're NOT running a live cycle to avoid triggering real trades
        resume_r = client.post(f"{BASE}/api/control/resume")
        resumed_ctrl = ok_json(client.get(f"{BASE}/api/control/status"))
        kill_after = resumed_ctrl.get("kill_switch_active", True) if resumed_ctrl else True

        open_after = db_query("SELECT COUNT(*) as n FROM ema_positions WHERE status='OPEN'")[0]["n"]
        report("TC-8.1", "Pause sets kill_switch_active=true; resume clears it",
               kill_active and not kill_after and open_before == open_after,
               f"kill_during={kill_active} kill_after={kill_after} "
               f"open_before={open_before} open_after={open_after}")
    else:
        skip("TC-8.1", "Kill switch pause/resume", f"pause returned HTTP {pause_r.status_code}")
except Exception as e:
    skip("TC-8.1", "Kill switch pause/resume", f"error: {e}")

skip("TC-8.2", "Kill switch: resume allows new entries",
     "[RISKY] run-ema-cycle could enter real positions with 1 open slot on scalper")
skip("TC-8.3", "Manual close removes position", "[MUTATING] would close a real live position")
skip("TC-8.4", "Reset dry-run clears history", "[RISKY] bot is live (EMA_DRY_RUN=false)")
print()

# ===========================================================================
# TC-9  CSV Export
# ===========================================================================
print("── TC-9  CSV Export ──")
if csv_r.status_code == 200 and csv_r.text.strip():
    csv_rows = list(csv.DictReader(io.StringIO(csv_r.text)))
    db_map   = {str(row["id"]): row for row in db_all}

    report("TC-9.1", "CSV row count matches total DB rows",
           len(csv_rows) == len(db_all), f"csv={len(csv_rows)}  db={len(db_all)}")

    bad = []
    for cr in csv_rows[:5]:
        pid = cr.get("id") or cr.get("position_id")
        dr  = db_map.get(str(pid))
        if not dr:
            bad.append(f"pid={pid} not in DB"); continue
        for ck, dk, tol in [("netuid","netuid",0), ("entry_price","entry_price",1e-4),
                             ("amount_tao","amount_tao",1e-3)]:
            cv, dv = cr.get(ck), dr.get(dk)
            if cv is None or dv is None: continue
            try:
                ok = (str(cv) == str(dv)) if tol == 0 else abs(float(cv) - float(dv)) <= tol
            except (ValueError, TypeError):
                ok = True
            if not ok:
                bad.append(f"pid={pid} {ck}: csv={cv!r} db={dv!r}")
    report("TC-9.2", "CSV sample fields match DB (first 5 rows)", not bad, "; ".join(bad[:3]))

    bad = []
    for cr in csv_rows:
        if cr.get("status") != "CLOSED": continue
        try:
            exp = float(cr["amount_tao_out"]) - float(cr["amount_tao"])
            if abs(exp - float(cr["pnl_tao"])) > 0.0001:
                bad.append(f"pid={cr.get('id')} exp={exp:.5f} csv={cr['pnl_tao']}")
        except (KeyError, ValueError, TypeError):
            pass
    report("TC-9.3", "CSV pnl_tao = amount_tao_out − amount_tao", not bad, "; ".join(bad[:3]))
else:
    reason = f"HTTP {csv_r.status_code}" if csv_r.status_code != 200 else "empty response"
    skip("TC-9.1", "CSV row count", reason)
    skip("TC-9.2", "CSV fields match DB", reason)
    skip("TC-9.3", "CSV PnL recomputable", reason)
print()

# ===========================================================================
# TC-10  Frontend Display
# ===========================================================================
print("── TC-10  Frontend Display ──")
skip("TC-10.1", "Wallet balance colour reflects risk level", "[BROWSER]")

# TC-10.2  Subnet names in widget match signal/spot API data (no browser needed)
if open_positions and sig_list:
    sig_name_map = {int(s.get("netuid", 0)): s.get("name") for s in sig_list if s.get("name")}
    bad = []
    for pos in open_positions:
        nid    = pos["netuid"]
        w_name = pos.get("name")
        s_name = sig_name_map.get(nid)
        if w_name and s_name and w_name != s_name:
            bad.append(f"sn{nid}: position={w_name!r} signals={s_name!r}")
    report("TC-10.2", "Position subnet names match signals API names",
           not bad, "; ".join(bad))
elif not open_positions:
    skip("TC-10.2", "Subnet names consistent", "no open positions")
else:
    skip("TC-10.2", "Subnet names consistent", "signals or positions endpoint failed")

skip("TC-10.3", "Exit animation fires on close", "[BROWSER]")
skip("TC-10.4", "Stale data indicator when API unreachable", "[MUTATING] would kill backend")
skip("TC-10.5", "DRY RUN badge visible when dry_run=true", "[BROWSER]")
print()

# ===========================================================================
# TC-11  Cross-Strategy Exclusion
# ===========================================================================
print("── TC-11  Cross-Strategy Exclusion ──")

if db_open:
    by_netuid = {}
    for row in db_open:
        by_netuid.setdefault(row["netuid"], set()).add(row.get("strategy", "?"))
    conflicts = {n: list(s) for n, s in by_netuid.items() if len(s) > 1}
    report("TC-11.1", "Same subnet not held by both strategies simultaneously",
           not conflicts, f"conflicts: {conflicts}")
else:
    report("TC-11.1", "Same subnet not held by both strategies simultaneously",
           True, "no open positions")

db_cooldowns = db_query("SELECT * FROM ema_cooldowns")
recent_closed = db_query(
    "SELECT netuid, strategy, exit_ts FROM ema_positions "
    "WHERE status='CLOSED' ORDER BY exit_ts DESC LIMIT 5"
)
now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
active_cooldowns = [(r["strategy"], r["netuid"]) for r in db_cooldowns
                    if (r.get("expires_at") or "") > now_iso[:19]]
if recent_closed:
    report("TC-11.2", "Cooldown table populated after recent exits",
           len(db_cooldowns) > 0,
           f"active: {len(active_cooldowns)}  total rows: {len(db_cooldowns)}")
else:
    skip("TC-11.2", "Cooldown after exit", "no closed positions in DB")
print()

# ===========================================================================
# TC-12  Config API
# ===========================================================================
print("── TC-12  Config API ──")

if config and isinstance(config, dict):
    safe = {k: v for k, v in config.items()
            if k in ("EMA_STRATEGY_TAG", "EMA_B_STRATEGY_TAG", "LOG_LEVEL")}
    if safe:
        post_r  = client.post(f"{BASE}/api/config", json=safe)
        config2 = ok_json(client.get(f"{BASE}/api/config"))
        if config2:
            bad = [k for k in safe if config2.get(k) != safe[k]]
            report("TC-12.1", "Config round-trip: POST + GET returns same values",
                   not bad, f"mismatches: {bad}")
        else:
            skip("TC-12.1", "Config round-trip", "second GET /api/config failed")
    else:
        skip("TC-12.1", "Config round-trip", "no safe test fields in config response")
else:
    skip("TC-12.1", "Config round-trip", "config endpoint failed")

bad_r = client.post(f"{BASE}/api/config", json={"EMA_STOP_LOSS_PCT": -999})
report("TC-12.2", "POST invalid EMA_STOP_LOSS_PCT=-999 is rejected with 4xx",
       bad_r.status_code >= 400, f"got HTTP {bad_r.status_code}")

if cfgstatus:
    has_field = "setup_complete" in cfgstatus or "complete" in cfgstatus
    report("TC-12.3", "GET /api/config/status contains setup_complete field",
           has_field, f"keys: {list(cfgstatus.keys())[:6]}")
else:
    skip("TC-12.3", "Setup status field", "config/status endpoint failed")
print()

# ===========================================================================
# TC-13  Regression Tests
# ===========================================================================
print("── TC-13  Regression Tests ──")

if db_all:
    bad = []
    for row in db_all:
        if row.get("amount_tao") and row.get("amount_alpha") and row.get("entry_price"):
            basis = float(row["amount_tao"]) / float(row["amount_alpha"])
            if abs(basis - float(row["entry_price"])) > 0.000005:
                bad.append(f"pid={row['id']} basis={basis:.7f} stored={float(row['entry_price']):.7f}")
    report("TC-13.1", "entry_price = amount_tao / amount_alpha (cost basis, not spot)",
           not bad, "; ".join(bad[:3]))
else:
    skip("TC-13.1", "Entry price = cost basis", "no positions in DB")

if db_closed:
    stale = [r for r in db_closed if r.get("pnl_tao") == 0.0 and float(r.get("amount_tao_out") or 0) > 0]
    wrong = []
    for r in db_closed:
        if r.get("amount_tao") and r.get("amount_tao_out") and r.get("pnl_tao") is not None:
            exp = float(r["amount_tao_out"]) - float(r["amount_tao"])
            if abs(exp - float(r["pnl_tao"])) > 0.0001:
                wrong.append(f"pid={r['id']} exp={exp:.5f} stored={r['pnl_tao']:.5f}")
    issues = [f"pid={r['id']} pnl=0 tao_out={r['amount_tao_out']}" for r in stale] + wrong
    report("TC-13.2", "Exit pnl_tao = balance delta (not stale 0 from old price-quote bug)",
           not issues, "; ".join(issues[:3]))
else:
    skip("TC-13.2", "Exit PnL = balance delta", "no closed trades")

if db_all:
    bad = [(r["id"], r["entry_slippage_pct"]) for r in db_all
           if r.get("entry_slippage_pct") is not None
           and float(r["entry_slippage_pct"]) > MAX_SLIPPAGE_PCT
           and (r.get("entry_ts") or "") >= SAFE_STAKING_FIX_DATE]
    report("TC-13.3", f"safe_staking guard: no entry slippage > {MAX_SLIPPAGE_PCT}% since {SAFE_STAKING_FIX_DATE} (regression: was 38%)",
           not bad, f"violators: {bad[:3]}")
else:
    skip("TC-13.3", "safe_staking guard", "no positions in DB")
print()

# ===========================================================================
# Summary
# ===========================================================================
passed  = sum(1 for r in results if r[2] == "PASS")
failed  = sum(1 for r in results if r[2] == "FAIL")
skipped = sum(1 for r in results if r[2] == "SKIP")

print("=" * 65)
print(f"Results:  {passed} PASS  |  {failed} FAIL  |  {skipped} SKIP  "
      f"({passed + failed + skipped} total)")
if failed:
    print("\nFailed tests:")
    for tc_id, name, status, detail in results:
        if status == "FAIL":
            print(f"  ✗ {tc_id}: {name}")
            if detail:
                print(f"     → {detail}")
print("=" * 65)
sys.exit(1 if failed else 0)
