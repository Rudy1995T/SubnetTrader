# Spec: Surface Pool Flow Momentum (v2) in Localhost UI and Desktop Widget

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-21
**Status:** Draft
**Depends on:** [strategy-pool-flow-momentum.md](strategy-pool-flow-momentum.md) (runtime implemented), [wire-meanrev-to-api-and-ui.md](wire-meanrev-to-api-and-ui.md) (precedent for cross-surface rename)

## Motivation

Pool Flow Momentum (v2) runs live in the backend. The API is wired
([app/main.py:1386-1512](app/main.py#L1386-L1512)) — `/api/flow/portfolio`,
`/api/flow/positions`, `/api/flow/signals`, `/api/flow/positions/{id}/close`,
`/api/flow/snapshots` all return correct data — and a standalone page exists at
[frontend/src/app/flow/page.tsx](frontend/src/app/flow/page.tsx). But the Flow
strategy is **invisible to the operator**:

- [frontend/src/components/NavBar.tsx:14-18](frontend/src/components/NavBar.tsx#L14-L18)
  only exposes `EMA`, `Control`, `Settings`. There is no link to `/flow`, so
  users have to manually type the URL.
- [widget.py:148-165](widget.py#L148-L165) only knows about `meanrev` and
  `trend`. Flow positions never appear in the desktop widget — not in summary,
  not in open positions, not in closed-trade flash notifications.
- [widget.py:87-139](widget.py#L87-L139) polls only `/api/ema/*` endpoints.
  Flow portfolio state is never fetched.
- [frontend/src/app/layout.tsx:6-7](frontend/src/app/layout.tsx#L6-L7) metadata
  still reads `"SubnetTrader EMA Live"` — misleading once a third strategy is
  user-visible.

Without these surfaces, operators cannot observe live Flow trades, cannot close
them from the widget, and cannot see the "snapshot warmup" progress for flow
signals. All of that lives in the backend and goes unread.

## Goal

Make Pool Flow Momentum (v2) first-class in both the localhost web UI and the
desktop widget:

- A `/flow` tab in the NavBar with a live indicator matching EMA's pattern.
- A Flow summary row, Flow-tagged open-position group, and Flow close button
  in the widget.
- Snapshot-warmup visibility (how close each subnet is to 52 h of data).
- A closed-trades list that includes Flow exits with the correct tag.

After this spec, an operator opening `localhost:3000` sees three strategy tabs
(`EMA`, `Flow`, `Control`, `Settings`); an operator launching `widget.py` sees
three summary rows and positions grouped under `M / T / F` tags.

## Non-goals

- Changing any Flow signal math, thresholds, or exit logic (covered by the
  strategy spec).
- Unifying `/ema` and `/flow` into a single multi-strategy page. A future
  refactor can lift the per-strategy page into a parametrised `StrategyPage`
  component — out of scope here.
- Adding a new mobile layout. Existing `overflow-x-auto` on the nav is enough
  for the fourth link on narrow screens.
- Adding Flow rows to the Control page's strategy switches. The Control page
  already reads `flow_enabled` / `flow_dry_run` (see
  [app/main.py:195-196](app/main.py#L195-L196)); if the dry-run/enable toggles
  are not yet surfaced there, that is a separate ticket.

## Design

### Naming and colour

| Surface              | Key       | Tag | Colour                    | Label                  |
|----------------------|-----------|-----|---------------------------|------------------------|
| API portfolio key    | `flow`    | —   | —                         | —                      |
| Widget `_STRAT_TAG`  | `flow`    | `F` | `ORANGE` (`#d18616`)      | `"Flow"`               |
| Widget position row  | `flow`    | `F` | `ORANGE`                  | `"FLOW"` header        |
| Frontend NavBar      | `/flow`   | —   | orange dot (same pulse)   | `"Flow"`               |
| Frontend page header | —         | —   | —                         | `"Pool Flow Momentum"` |

Orange was already imported in [widget.py:48](widget.py#L48) but unused — pick
that so the three-strategy palette stays `SKY / PURPLE / ORANGE` without
reshuffling the existing sky-for-MeanRev and purple-for-Trend muscle memory.

### Live indicator rule

The NavBar already shows a green ping dot next to `EMA` when
`control/status.ema_dry_run === false`. Mirror that for Flow:

- Add `flow_dry_run: boolean` to the `ControlStatus` type.
- `flowIsLive = status ? !status.flow_dry_run : false` — render pulse when
  flow is enabled **and** live.
- Gate the whole tab behind `control/status.flow_enabled` — if the strategy is
  disabled in `.env`, hide the tab entirely rather than show a dead link that
  renders the "FLOW_ENABLED=false" placeholder page.

`/api/control/status` already emits `flow_enabled` and `flow_dry_run`
([app/main.py:195-196](app/main.py#L195-L196)); no backend change needed.

### Widget data flow

Add `/api/flow/portfolio` and `/api/flow/signals` polls to the existing
`_fetch` loop (`widget.py:87`). Store under `DataStore.flow_portfolio` and
merge into `_strat_positions()` so the open-position list becomes a flat list
across three strategies, each row tagged via `_strategy`.

The existing `/api/ema/recent-trades` query reads `ema_positions` (which
already contains flow rows, tagged `strategy='flow'`). The widget's closed-
trade flash uses `_STRAT_TAG.get(trade_strat, "?")` — once we add `"flow"` to
that map, flow exits flash with an `F` badge automatically. No new endpoint.

### Widget summary row height

Adding a third summary row pushes the widget from ~760 px to ~790 px — inside
the variance tolerance of the existing Tk window. No geometry change needed.
If the user enables all three strategies at once, the widget stays within a
single ~800 px frame.

### Snapshot warmup visibility

The existing Flow page shows `snapshots_collected` per signal but no aggregate
progress. Add a small header banner on `/flow`:

```
Warming up: 18 / 50 subnets have ≥52 h of snapshots (target 624 snaps @ 5 min)
```

Reads from `signalsData.signals` — count rows where `snapshots >= 624`. Zero
new API surface.

For the widget, a single-line status under the Flow summary row is enough:

```
FLOW  [live]  Pot 10.0τ  Dep 4.2τ   2/3
             warmup: 18/50 subnets ready
```

## Implementation plan

### Phase 1 — NavBar link

1. [frontend/src/components/NavBar.tsx](frontend/src/components/NavBar.tsx):
   - Add `flow_dry_run: boolean` and `flow_enabled: boolean` to `ControlStatus`.
   - Compute `flowIsLive = status ? !status.flow_dry_run : false` and
     `flowEnabled = status?.flow_enabled ?? false`.
   - Conditionally insert `{ href: "/flow", label: "Flow" }` into `links`
     when `flowEnabled`. Render the same pulsing-dot pattern as EMA, gated on
     `flowIsLive`.
   - Swap the hard-coded `const isEma = l.href === "/ema"` branch into a more
     general `const liveHref = l.href === "/ema" ? emaIsLive : l.href === "/flow" ? flowIsLive : false;`
     — minimal change, avoids duplicating the span JSX.

2. [frontend/src/app/layout.tsx](frontend/src/app/layout.tsx):
   - Update `metadata.title` to `"SubnetTrader Live"` (drop the `EMA` qualifier
     now that Flow is a peer).
   - Update `metadata.description` to `"Multi-strategy trading control
     surface"`.

### Phase 2 — Flow page polish

1. [frontend/src/app/flow/page.tsx](frontend/src/app/flow/page.tsx):
   - Add a warmup banner above the stat grid:
     ```tsx
     const ready = signals.filter((s) => s.snapshots >= COLD_START).length;
     const total = signals.length;
     ```
     Render when `ready < total`:
     ```
     <div className="text-amber-300 text-xs">
       Warming up: {ready}/{total} subnets have ≥52 h of snapshots
     </div>
     ```
   - Read `cold_start_snaps` from `signalsData` (already returned at
     [app/main.py:1484](app/main.py#L1484)) rather than hard-coding 624.
   - Add a "Last snapshot" header stat from `portfolio.snapshot_status.last_run`
     (moved up from the footer — operators check warmup first, other status
     second).

2. The existing placeholder "Flow strategy is disabled" screen
   ([frontend/src/app/flow/page.tsx:153-164](frontend/src/app/flow/page.tsx#L153-L164))
   stays as a fallback for direct URL hits when `flow_enabled=false`. With
   Phase 1 hiding the tab, normal users will not reach it, but direct links
   (bookmarks, shared URLs) still need a graceful render.

### Phase 3 — Widget

1. [widget.py](widget.py) — data fetch (~line 87):
   - Add `self.flow_portfolio: dict | None = None` to `DataStore.__init__`.
   - In `_fetch()`, add:
     ```python
     try:
         r = requests.get(f"{API_BASE}/api/flow/portfolio", timeout=5)
         store.put(flow_portfolio=r.json())
     except Exception:
         pass
     ```
   - `DataStore.snap()` includes `flow_portfolio`.

2. [widget.py:148-149](widget.py#L148-L149) — strategy maps:
   ```python
   _STRAT_COLOUR = {"meanrev": SKY, "trend": PURPLE, "flow": ORANGE}
   _STRAT_TAG    = {"meanrev": "M", "trend": "T", "flow": "F"}
   ```

3. [widget.py:152-165](widget.py#L152-L165) — `_strat_positions`:
   - Extend the iteration to merge three sources: `portfolio` (meanrev +
     trend live under the same response) plus `flow_portfolio` (separate
     response).
   - Signature change: `def _strat_positions(portfolio, flow_portfolio)`.
   - Caller in `_tick` passes both. When `flow_portfolio.enabled`, iterate
     `flow_portfolio["open_positions"]` and tag each with `"flow"`.

4. [widget.py:292](widget.py#L292) — summary rows:
   - Add a third entry: `("flow", ORANGE, "Flow")`.
   - Same loop body; the existing `_strat_rows[key]` dict keys off the strategy
     name, so only the iteration tuple grows.

5. [widget.py:334](widget.py#L334) — position-group headers:
   - Extend to `("meanrev", SKY), ("trend", PURPLE), ("flow", ORANGE)`.

6. [widget.py:820-859](widget.py#L820-L859) — per-strategy summary update
   loop: read from `s.get("flow_portfolio")` when `key == "flow"`, else from
   `p.get(key, {})` as today. Extract a small helper:
   ```python
   def _strat_data(s, key):
       if key == "flow":
           return s.get("flow_portfolio") or {}
       return (s.get("portfolio") or {}).get(key, {})
   ```
   Use it in both the summary row and position-group update loops.

7. [widget.py:886-905](widget.py#L886-L905) — position grouping:
   - Add `flow_indices = [i for i, pos in enumerate(positions) if pos.get("_strategy") == "flow"]`.
   - Extend the group loop to 3 entries.

8. [widget.py:441](widget.py#L441) — close button:
   - Detect strategy key from the row's `pos` object and POST to the right
     endpoint: `/api/flow/positions/{pid}/close` when `_strategy == "flow"`,
     else `/api/ema/positions/{pid}/close` (existing behaviour). Do this in
     `_close_position` by reading the cached position row rather than
     hard-coding the EMA path.

9. [widget.py:715](widget.py#L715) — default strategy fallback:
   - Change `pos.get("_strategy", "meanrev")` to `pos.get("_strategy") or "meanrev"`
     with a log-once warning if the key is missing. The missing-key path is a
     data bug and we should hear about it, not silently default.

10. [widget.py:937-939](widget.py#L937-L939) — closed-trades flash:
    - Works automatically once `_STRAT_TAG["flow"] = "F"` is added, since the
      trade row's `strategy` field is read straight from `ema_positions`.
    - Verify: close a flow position, confirm the widget flashes the correct
      `F` tag and orange colour.

### Phase 4 — Cross-surface verification

1. `curl -s localhost:8081/api/control/status | jq` shows `flow_enabled` and
   `flow_dry_run` fields (existing behaviour, just asserting before frontend
   relies on them).
2. `cd frontend && npm run build` — no TypeScript errors after NavBar change.
3. Open `localhost:3000` with `FLOW_ENABLED=false`: no Flow tab.
4. Set `FLOW_ENABLED=true`, `FLOW_DRY_RUN=true`, restart bot: Flow tab appears,
   no pulse dot.
5. Set `FLOW_DRY_RUN=false`, restart: pulse dot appears.
6. `python widget.py` with flow live and an open flow position: summary row
   renders `FLOW [live] Pot 10.0τ Dep X.Xτ`; position list groups the flow
   position under `F` tag.
7. Click close on the flow position in the widget — confirm it hits
   `/api/flow/positions/{id}/close`, not `/api/ema/…`.

## Database / API impact

**None.** `ema_positions.strategy` already records `'flow'` for flow rows via
[app/main.py:150](app/main.py#L150) instantiating `EmaManager(..., flow_config())`.
`/api/control/status` already emits the Flow flags. `/api/ema/recent-trades`
already returns flow rows unless filtered by strategy. No schema change, no
new endpoints, no migration.

## Testing

| Scenario | Expected |
|---|---|
| `FLOW_ENABLED=false`, frontend | No `/flow` tab in NavBar; direct nav to `/flow` shows the existing "disabled" placeholder |
| `FLOW_ENABLED=true`, dry-run, frontend | Tab visible, no pulse, page renders `DRY RUN` banner |
| `FLOW_ENABLED=true`, live, frontend | Tab visible with orange pulse, page shows `LIVE` banner |
| Warmup period (snapshots < 624) | Flow page shows `Warming up: N/50` banner; signals table shows `snapshots` column with count |
| Widget, flow disabled | No Flow summary row; no Flow position group |
| Widget, flow enabled with open position | Flow row visible; position grouped under `F` badge; close button closes via flow endpoint |
| Closed flow trade | Recent trades flash shows `F` tag, orange accent; widget `daily_trades` stat includes the flow exit |

## Risk mitigation

- **Widget vertical overflow.** Three summary rows + position headers + 6
  position rows + daily stats + sparkline should stay under 800 px. If the
  widget clips on smaller desktops, add `MAX_POS_ROWS = 5` for multi-strategy
  mode rather than growing the window.
- **Close-button routing bug.** The existing `_close_position` hard-codes
  `/api/ema/positions/{id}/close`. If a flow position is closed through that
  path, the EMA manager returns 404 (position not in its table slice). Covered
  explicitly in Phase 3 step 8.
- **NavBar link churn on narrow screens.** Four links fit comfortably at ≥640
  px. Below that, the existing `overflow-x-auto` handles scroll.
- **Stale strategy key.** Phase 3 step 9 turns the silent default into a
  logged warning; catches the case where a future strategy ships before its
  widget plumbing.

## What NOT to change

- Flow signal thresholds, magnitude cap, regime filter — all owned by the
  strategy spec.
- Any EMA / MeanRev / Trend rendering paths except for the three-way extension
  of `_strat_positions` and the summary-row tuple.
- The Flow page's core sections (stats grid, open positions, signals table,
  closed trades) — only the warmup banner is new.
- API contract for `/api/flow/*` — surfaces consume what exists.

## Success criteria

1. `localhost:3000` NavBar shows a `Flow` tab when `FLOW_ENABLED=true`, with a
   pulse dot when `FLOW_DRY_RUN=false`.
2. `/flow` page displays a warmup banner until all scanned subnets reach
   `snapshots >= cold_start_snaps`.
3. `widget.py` shows three summary rows (Mean-Reversion / Trend / Flow) with
   correct per-strategy pot/deployed/slots data.
4. Open flow positions appear in the widget under an orange `F` tag group;
   close button closes through `/api/flow/positions/{id}/close`.
5. Closed flow trades flash in the widget's recent-trades list with the `F`
   tag.
6. `grep -rn '"flow"' widget.py` returns ≥5 hits across `_STRAT_COLOUR`,
   `_STRAT_TAG`, `_strat_positions`, summary tuple, header tuple, and close
   routing.
