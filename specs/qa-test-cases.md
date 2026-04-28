# SubnetTrader QA Test Cases

Validates that what the dashboard displays matches ground truth — on-chain state, the
database, and the Taostats API.  Tests are grouped by concern and ordered from
"cheapest to run" (API-only) to "most expensive" (requires a live chain connection).

---

## Scope

| Area | Ground Truth | Widget Source |
|---|---|---|
| Wallet balance | `subtensor.get_tao_balance(coldkey)` | `/api/ema/portfolio` → `wallet_balance` |
| Open positions | `ema_positions WHERE status='OPEN'` (DB) | `/api/ema/portfolio` → `open_positions` |
| Staked alpha per position | `subtensor.get_stake(coldkey, hotkey, netuid)` | `/api/ema/positions` → `amount_alpha` |
| Deployed TAO | Sum of `amount_tao` for open positions (DB) | `deployed_tao` in portfolio |
| Recent trades | `ema_positions WHERE status='CLOSED'` (DB) | `/api/ema/recent-trades` |
| Current prices | Taostats `/api/dtao/pool/latest/v1` | `/api/subnets/{netuid}/spot` |
| PnL calculations | Derived from DB fields | `pnl_pct`, `pnl_tao` in open positions |
| Cooldowns | `ema_cooldowns` table (DB) | Implicitly: no position opened on cooldown subnet |
| Slippage stats | Aggregate of DB `entry_slippage_pct`, `exit_slippage_pct` | `/api/ema/slippage-stats` |
| Signal state | EMA computed from Taostats history | `/api/ema/signals` |

---

## TC-1 — Wallet Balance

### TC-1.1  Widget balance matches on-chain coldkey balance

**Steps**
1. Query `GET /api/ema/portfolio`.  Record `wallet_balance`.
2. Call `subtensor.get_tao_balance(coldkey_ss58)` directly.
3. Compare.

**Pass criteria**
`abs(widget_balance - chain_balance) <= 0.001 TAO`
(allow for the in-flight cycle that may have spent from the pot)

**Fail hints**
— widget shows `null`: wallet RPC is failing → check `/api/health/services` `.wallet.ok`
— large delta: a swap executed between steps; re-run and check for concurrent activity

---

### TC-1.2  Unstaked TAO = wallet balance − fee reserve

**Steps**
1. From `/api/ema/portfolio` for each strategy: `unstaked_tao`.
2. Combined `unstaked_tao` (scalper + trend) should equal `wallet_balance − FEE_RESERVE_TAO`
   when no position is currently open.

**Pass criteria**
`sum(unstaked_tao) ≈ wallet_balance − fee_reserve`  (±0.005 TAO)

---

### TC-1.3  Balance reflected after manual send

**Steps**
1. Record `wallet_balance` from widget.
2. Send 1 TAO out of the coldkey wallet from another terminal.
3. Wait for next EMA cycle (up to `SCAN_INTERVAL_MIN` minutes) or call
   `POST /api/control/run-ema-cycle`.
4. Re-read `/api/ema/portfolio`.

**Pass criteria**
New `wallet_balance ≈ old_balance − 1.0 TAO` (within 0.002 TAO).

---

## TC-2 — Open Positions: Widget vs Database

### TC-2.1  Open position count matches DB

**Steps**
1. `GET /api/ema/portfolio`.  For each strategy count `len(open_positions)`.
2. Query DB: `SELECT COUNT(*) FROM ema_positions WHERE status='OPEN' AND strategy='<tag>'`.

**Pass criteria**
Counts equal for both scalper and trend.

---

### TC-2.2  Open position list matches DB rows

**Steps**
1. `GET /api/ema/positions?limit=200`.
2. Query DB: `SELECT * FROM ema_positions WHERE status='OPEN'`.
3. For each DB row, find the matching widget item by `position_id`.

**Pass criteria** — for every open position:

| Widget field | DB column | Tolerance |
|---|---|---|
| `position_id` | `id` | exact |
| `netuid` | `netuid` | exact |
| `entry_price` | `entry_price` | ±0.000001 |
| `amount_tao` | `amount_tao` | ±0.0001 |
| `amount_alpha` | `amount_alpha` | ±0.01 |
| `entry_ts` | `entry_ts` | exact string |

---

### TC-2.3  Open position netuid list matches on-chain stake

**Steps**
1. Collect all `(netuid, staked_hotkey)` pairs for open positions from
   `GET /api/ema/positions`.
2. For each pair call `subtensor.get_stake(coldkey, staked_hotkey, netuid)`.
3. Compare returned alpha balance to `amount_alpha` in widget.

**Pass criteria**
`abs(chain_alpha − widget_alpha) / widget_alpha < 0.02`  (within 2%)

**Note** — if the position is mid-exit, chain stake may briefly be 0 while DB is still OPEN;
tolerate only if `exit_ts` is being set concurrently.

---

### TC-2.4  No phantom positions (widget has no position not in DB)

**Steps**
1. Collect `position_id` from `GET /api/ema/positions`.
2. Query DB for those IDs.

**Pass criteria**
Every widget position_id exists in DB with `status='OPEN'`.

---

### TC-2.5  No ghost stake (on-chain stake exists but widget shows no position)

**Steps**
1. For every `netuid` listed in the Taostats pool snapshot, query
   `subtensor.get_stake(coldkey, hotkey, netuid)` for known hotkeys.
2. If chain returns alpha > 0.01, check that a matching OPEN position exists in widget.

**Pass criteria**
No subnet has non-trivial on-chain stake without a corresponding open widget position.

**Note** — false positives possible for manually staked positions unrelated to the bot;
exclude netuids not tracked by the bot's `staked_hotkey` registry.

---

## TC-3 — Subnet Investment: Online vs Widget

### TC-3.1  Invested subnet list matches active pool stake

**Steps**
1. From `/api/ema/positions`, collect all `netuid` values.
2. From Taostats `GET /api/dtao/pool/latest/v1`, find each netuid's current
   `alpha_in_pool` and `price`.
3. Verify the subnet is still listed (not deprecated / removed from pool).

**Pass criteria**
All widget netuids appear in the Taostats pool list.

---

### TC-3.2  Current price in widget matches Taostats spot

**Steps**
1. For each open position, read `current_price` from `/api/ema/portfolio`.
2. Fetch `GET /api/subnets/{netuid}/spot` immediately after.
3. Compare to Taostats direct call `GET /api/dtao/pool/latest/v1` for that netuid.

**Pass criteria**
`abs(widget_price − taostats_price) / taostats_price < 0.01`  (within 1%)

**Note** — widget may be using a 5-minute cached value; timestamps should be within
the cache TTL window.

---

### TC-3.3  Current price in widget matches on-chain spot

**Steps**
1. For each open position, read `current_price` from widget.
2. Call `executor.get_onchain_alpha_price(netuid)` directly (or via a test utility).

**Pass criteria**
Both prices within 2% of each other.

**Note** — Taostats and chain may diverge during low-liquidity events; flag if > 5%.

---

### TC-3.4  PnL percentage matches recomputed value

**Steps**
1. For each open position collect `entry_price`, `current_price`, `pnl_pct` from widget.
2. Recompute: `expected_pnl_pct = (current_price − entry_price) / entry_price * 100`.

**Pass criteria**
`abs(widget_pnl_pct − expected_pnl_pct) < 0.01`

---

### TC-3.5  PnL in TAO consistent with PnL percent and amount_tao

**Steps**
1. Collect `pnl_pct`, `pnl_tao`, `amount_tao` for each open position.
2. Recompute: `expected_pnl_tao = amount_tao * pnl_pct / 100`.

**Pass criteria**
`abs(widget_pnl_tao − expected_pnl_tao) < 0.0005 TAO`

---

### TC-3.6  Deployed TAO equals sum of open position sizes

**Steps**
1. From `/api/ema/portfolio`, read `deployed_tao` for each strategy.
2. Sum `amount_tao` across all open positions for that strategy.

**Pass criteria**
`abs(deployed_tao − sum_amount_tao) < 0.0001`

---

### TC-3.7  Pot TAO = deployed + unstaked (within fee reserve)

**Steps**
1. From `/api/ema/portfolio` read `pot_tao`, `deployed_tao`, `unstaked_tao`.
2. Check: `pot_tao ≈ deployed_tao + unstaked_tao`.

**Pass criteria**
`abs(pot_tao − (deployed_tao + unstaked_tao)) < 0.001`

---

## TC-4 — Recent Trades: Widget vs Database

### TC-4.1  Recent trades list matches DB closed positions

**Steps**
1. `GET /api/ema/recent-trades`.  Record returned list.
2. Query DB: `SELECT * FROM ema_positions WHERE status='CLOSED' ORDER BY exit_ts DESC LIMIT 10`.

**Pass criteria** — for each returned trade row:

| Widget field | DB column | Tolerance |
|---|---|---|
| `position_id` | `id` | exact |
| `netuid` | `netuid` | exact |
| `entry_price` | `entry_price` | ±0.000001 |
| `exit_price` | `exit_price` | ±0.000001 |
| `amount_tao` | `amount_tao` | ±0.0001 |
| `amount_tao_out` | `amount_tao_out` | ±0.0001 |
| `pnl_tao` | `pnl_tao` | ±0.0001 |
| `pnl_pct` | `pnl_pct` | ±0.01 |
| `exit_reason` | `exit_reason` | exact string |
| `entry_ts` | `entry_ts` | exact |
| `exit_ts` | `exit_ts` | exact |
| `strategy` | `strategy` | exact |

---

### TC-4.2  Recent trades PnL recomputable from raw fields

**Steps**
1. For each closed trade in widget, read `amount_tao`, `amount_tao_out`, `pnl_tao`, `pnl_pct`.
2. Recompute:
   - `expected_pnl_tao = amount_tao_out − amount_tao`
   - `expected_pnl_pct = (amount_tao_out − amount_tao) / amount_tao * 100`

**Pass criteria**
Both within 0.0001 TAO / 0.01% of stored values.

---

### TC-4.3  Trades are ordered by exit_ts descending

**Steps**
1. Collect `exit_ts` list from `/api/ema/recent-trades`.
2. Verify sorted descending (most recent first).

**Pass criteria**
`exit_ts[i] >= exit_ts[i+1]` for all adjacent pairs.

---

### TC-4.4  Exit reason is one of the allowed values

**Steps**
1. For all closed trades from DB: check `exit_reason`.

**Pass criteria**
Every non-null `exit_reason` is one of:
`STOP_LOSS`, `TAKE_PROFIT`, `TRAILING_STOP`, `EMA_CROSS`, `TIME_STOP`,
`MANUAL`, `BREAKEVEN_STOP`

---

### TC-4.5  Stop-loss exits confirm PnL <= -stop_loss_pct

**Steps**
1. Filter trades where `exit_reason = 'STOP_LOSS'`.
2. Check `pnl_pct <= -(stop_loss_pct + small_slippage_buffer)`.

**Pass criteria**
`pnl_pct <= -(stop_loss_pct − 0.5)` for all stop-loss trades
(allow 0.5% tolerance for slippage at exit).

---

### TC-4.6  Take-profit exits confirm PnL >= take_profit_pct

**Steps**
1. Filter trades where `exit_reason = 'TAKE_PROFIT'`.
2. Check `pnl_pct >= (take_profit_pct − 1.0)` (allow 1% slippage at exit).

**Pass criteria**
All take-profit trades have positive PnL above or near the target.

---

### TC-4.7  No open position has exit_ts set

**Steps**
1. Query DB: `SELECT COUNT(*) FROM ema_positions WHERE status='OPEN' AND exit_ts IS NOT NULL`.

**Pass criteria**
Count = 0.

---

### TC-4.8  No closed position is missing exit fields

**Steps**
1. Query DB: `SELECT COUNT(*) FROM ema_positions WHERE status='CLOSED' AND (exit_ts IS NULL OR exit_price IS NULL OR amount_tao_out IS NULL OR pnl_tao IS NULL)`.

**Pass criteria**
Count = 0.

---

## TC-5 — Slippage Statistics

### TC-5.1  Average entry slippage matches recomputed value

**Steps**
1. `GET /api/ema/slippage-stats`.  Record `avg_entry_slippage_pct`, `trade_count`.
2. Query DB: `SELECT AVG(entry_slippage_pct) FROM ema_positions WHERE status='CLOSED' AND entry_slippage_pct IS NOT NULL`.

**Pass criteria**
`abs(widget_avg − db_avg) < 0.01`

---

### TC-5.2  Average exit slippage matches recomputed value

Same approach as TC-5.1 using `exit_slippage_pct`.

---

### TC-5.3  Total slippage TAO recomputable

**Steps**
1. Read `total_slippage_tao` from widget.
2. Recompute: for each closed trade, `slippage_cost = amount_tao * (entry_slippage_pct + exit_slippage_pct) / 100`.  Sum all.

**Pass criteria**
Within 0.005 TAO of widget value.

---

### TC-5.4  Slippage is within configured maximum

**Steps**
1. Query DB for all `entry_slippage_pct` and `exit_slippage_pct`.

**Pass criteria**
No individual entry slippage > `MAX_SLIPPAGE_PCT` (configured 5%).
No individual exit slippage > `MAX_SLIPPAGE_PCT * 2` (exit tolerance is looser).

---

## TC-6 — EMA Signals

### TC-6.1  Signal state is one of the allowed values

**Steps**
1. `GET /api/ema/signals`.  Check every entry's `signal` field.

**Pass criteria**
Every signal is `BUY`, `SELL`, or `HOLD`.

---

### TC-6.2  Held positions have non-SELL signal at entry

**Steps**
1. For each open position, find its netuid's current signal from `/api/ema/signals`.
2. If position was entered within the last `SCAN_INTERVAL_MIN * 2` minutes, signal should
   still be `BUY` or `HOLD`.

**Pass criteria**
No freshly-entered position (< 30 min old) has a `SELL` signal at time of entry check.

**Note** — signals evolve; this test is only meaningful immediately after an entry.

---

### TC-6.3  Signal freshness: bars_above must match price vs EMA trend

**Steps**
1. From `/api/ema/signals` read `bars_above` for a subnet.
2. Fetch 7-day price history from Taostats for that subnet.
3. Recompute EMA with the strategy's `slow_period` and count consecutive bars above/below.

**Pass criteria**
Widget `bars_above` matches recomputed value (exact integer).

---

### TC-6.4  BUY signal implies price > both EMA lines

**Steps**
1. Identify any subnet with `signal = 'BUY'`.
2. Fetch its price history, compute fast EMA and slow EMA.
3. Verify last `confirm_bars` candles all have close > slow EMA and close > fast EMA.

**Pass criteria**
For every BUY signal, EMA condition holds on the last N candles (N = `EMA_CONFIRM_BARS`).

---

## TC-7 — Health & Service Status

### TC-7.1  All critical services report ok=true

**Steps**
1. `GET /api/health/services`.
2. Check all non-optional services.

**Pass criteria**
Every service where `optional=false` has `ok=true`.

---

### TC-7.2  Wallet service balance matches /api/ema/portfolio balance

**Steps**
1. Read `balance_tao` from `/api/health/services` → wallet entry.
2. Read `wallet_balance` from `/api/ema/portfolio`.

**Pass criteria**
Values equal to within 0.001 TAO.

---

### TC-7.3  Taostats latency within acceptable range

**Steps**
1. Read `latency_ms` from `/api/health/services` → taostats entry.

**Pass criteria**
`latency_ms < 5000` (5 seconds).

---

### TC-7.4  can_trade flag matches actual trading conditions

**Steps**
1. From `/api/health/services` read wallet `can_trade`.
2. From `/api/control/status` read `kill_switch_active` and `breaker_active`.
3. If `kill_switch_active` or `breaker_active`, `can_trade` should be `false`.

**Pass criteria**
`can_trade = not (kill_switch_active or breaker_active or wallet.ok == false)`

---

## TC-8 — Control Endpoints

### TC-8.1  Kill switch: pause stops new entries

**Steps**
1. `POST /api/control/pause`.
2. `POST /api/control/run-ema-cycle`.  Wait for cycle to finish.
3. Record open position count before and after.
4. `POST /api/control/resume`.

**Pass criteria**
No new positions opened during the paused cycle.
Position count unchanged.

---

### TC-8.2  Kill switch: resume allows new entries

**Steps**
1. Ensure `kill_switch_active = false` via `/api/control/resume`.
2. Trigger `POST /api/control/run-ema-cycle`.
3. Check that the scheduler proceeded (check logs for cycle start/end).

**Pass criteria**
Log shows "EMA cycle complete" or equivalent without "kill switch active" skip.

---

### TC-8.3  Manual close removes position from open list

**Steps**
1. Pick an open position's `position_id` from `/api/ema/positions`.
2. `POST /api/ema/positions/{position_id}/close`.
3. Re-fetch `/api/ema/positions`.
4. Query DB.

**Pass criteria**
- Position no longer appears in open list.
- DB row has `status='CLOSED'`, `exit_reason='MANUAL'`, non-null `exit_ts`.
- On-chain stake for `(netuid, staked_hotkey)` drops to ~0 within 60 seconds.

---

### TC-8.4  Reset dry-run clears history in dry-run mode only

**Steps** (DRY_RUN must be enabled)
1. Confirm `dry_run = true` from `/api/ema/portfolio`.
2. `POST /api/control/reset-dry-run`.
3. Fetch `/api/ema/portfolio` and `/api/ema/recent-trades`.

**Pass criteria**
Open positions = 0, recent trades = empty.

**Steps** (LIVE mode)
1. Confirm `dry_run = false`.
2. `POST /api/control/reset-dry-run`.

**Pass criteria**
Response returns 400 or similar error; no data deleted.

---

## TC-9 — CSV Export

### TC-9.1  Export contains all DB trades

**Steps**
1. `GET /api/export/trades.csv`.  Parse CSV.  Count rows (excluding header).
2. Query DB: `SELECT COUNT(*) FROM ema_positions`.

**Pass criteria**
CSV row count = DB row count.

---

### TC-9.2  CSV fields match DB columns

**Steps**
1. Verify CSV column headers map to DB columns.
2. For a random sample of 5 rows, compare each field to the matching DB row.

**Pass criteria**
All fields match within numeric tolerance (0.0001).

---

### TC-9.3  CSV PnL values are recomputable

For all closed rows in the CSV, recompute:
- `pnl_tao = amount_tao_out - amount_tao`
- `pnl_pct = pnl_tao / amount_tao * 100`

**Pass criteria**
Matches CSV columns within 0.0001.

---

## TC-10 — Frontend Display Consistency

### TC-10.1  Wallet balance colour reflects risk level

**Steps**
1. Check the displayed TAO balance on the `/ema` page.
2. If `wallet_balance < pot_tao * 0.5`, the balance element should have a warning colour (amber/red).
3. If `wallet_balance >= pot_tao`, it should be nominal colour (sky blue per design).

**Pass criteria**
Correct CSS class present per colour rules (inspect element or screenshot comparison).

---

### TC-10.2  Open position cards show correct subnet name

**Steps**
1. For each open position in the widget, note the displayed `name`.
2. Cross-reference with Taostats pool latest, matching on `netuid`.

**Pass criteria**
Widget `name` matches `name` field from Taostats for the same `netuid`.

---

### TC-10.3  Exit animation fires on position close

**Steps** (requires live or simulated environment)
1. Observe widget with one open position.
2. Trigger `POST /api/ema/positions/{id}/close` (or wait for natural exit).
3. Observe UI within 15 seconds.

**Pass criteria**
Exit animation is displayed.  Closed position disappears from open list.

---

### TC-10.4  Stale data indicator appears if API unreachable

**Steps**
1. Kill the backend process (`lsof -ti:8081 | xargs kill -9`).
2. Observe the frontend dashboard.

**Pass criteria**
Dashboard shows an error state or stale data indicator rather than silently displaying
the last known (now incorrect) values.

---

### TC-10.5  DRY RUN badge is visible when dry_run = true

**Steps**
1. Set `EMA_DRY_RUN=true` and restart the bot.
2. Open `/ema` page.

**Pass criteria**
"DRY RUN" badge or similar indicator is prominently visible for the affected strategy.

---

## TC-11 — Cross-Strategy Exclusion

### TC-11.1  Same subnet not held by both strategies simultaneously

**Steps**
1. `GET /api/ema/positions`.
2. Collect `(netuid, strategy)` pairs for all open positions.

**Pass criteria**
No `netuid` appears with both `strategy='scalper'` and `strategy='trend'` simultaneously.

---

### TC-11.2  Cooldown prevents re-entry after exit

**Steps**
1. Observe a position close (or manually close one).  Note `netuid` and `exit_ts`.
2. Query DB: `SELECT expires_at FROM ema_cooldowns WHERE netuid=<N> AND strategy=<S>`.
3. Trigger `POST /api/control/run-ema-cycle`.
4. Verify no new position is opened for that netuid within the cooldown window.

**Pass criteria**
`expires_at > now()` after close.
Widget shows no new position for that netuid until cooldown expires.

---

## TC-12 — Config API

### TC-12.1  Config read round-trips correctly

**Steps**
1. `GET /api/config`.  Record all field values.
2. `POST /api/config` with the same values unchanged.
3. `GET /api/config` again.

**Pass criteria**
All values identical before and after the no-op write.

---

### TC-12.2  Invalid config value rejected

**Steps**
1. `POST /api/config` with `EMA_STOP_LOSS_PCT = -1` (invalid: must be positive).

**Pass criteria**
Response is 4xx with a descriptive error message.  Config file is unchanged.

---

### TC-12.3  Setup status reflects required fields

**Steps**
1. `GET /api/config/status`.

**Pass criteria**
`setup_complete = true` only when all required fields are present and non-empty in `.env`.

---

## TC-13 — Regression Tests (Known Past Bugs)

### TC-13.1  Entry price is cost basis, not spot price

**Steps**
1. Enter a position (or inspect a recent trade in DB).
2. Compute: `expected_entry_price = amount_tao / amount_alpha`.
3. Compare to DB `entry_price`.

**Pass criteria**
`abs(db_entry_price − expected_entry_price) < 0.000005`
If `entry_spot_price` is also stored, `entry_price != entry_spot_price` (they differ due to slippage).

---

### TC-13.2  Exit PnL uses balance delta, not price quote

**Steps**
1. For any closed trade in DB, check: `pnl_tao ≈ amount_tao_out − amount_tao`.
2. Verify `amount_tao_out > 0` (i.e., was actually recorded, not a stale 0).

**Pass criteria**
`abs(pnl_tao − (amount_tao_out − amount_tao)) < 0.0001` for every closed trade.

---

### TC-13.3  Slippage guard active: no entry slippage > MAX_SLIPPAGE_PCT

**Steps**
1. Query all entries: `SELECT entry_slippage_pct FROM ema_positions`.
2. Verify none exceed `MAX_SLIPPAGE_PCT`.

**Pass criteria**
All `entry_slippage_pct <= MAX_SLIPPAGE_PCT` (5.0%).

---

## Execution Notes

**Environment required:**
- Bot running with `EMA_DRY_RUN=true` for safe execution of destructive test cases (TC-8, TC-10.4)
- A live or testnet Subtensor connection for on-chain validation (TC-1.1, TC-2.3, TC-2.5, TC-3.3)
- At least 3 closed trades in DB for meaningful trade-validation tests (TC-4, TC-5)

**Tooling suggestions:**
- API tests: `pytest` with `httpx.AsyncClient` pointed at `http://localhost:8081`
- DB access: `sqlite3 data/ledger.db` or Python `aiosqlite`
- On-chain queries: `bittensor.Subtensor` + `bittensor.Wallet` in a test fixture
- Taostats comparison: direct `httpx` calls using `TAOSTATS_API_KEY` from `.env`

**Ordering for a CI pipeline:**
1. TC-7 (health) — fast, no chain needed
2. TC-6 (signals) — fast, Taostats only
3. TC-1.2, TC-3.6, TC-3.7 — math checks on live API data
4. TC-2.1, TC-2.2, TC-4.1–TC-4.8 — DB consistency
5. TC-5 (slippage) — DB aggregate checks
6. TC-9 (CSV) — file I/O
7. TC-12 (config) — write caution: use a test .env copy
8. TC-8 (control) — mutates state; run in dry-run mode only
9. TC-1.1, TC-2.3, TC-3.3 (on-chain) — requires chain connection, run last
