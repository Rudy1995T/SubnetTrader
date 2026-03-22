# SubnetTrader Desktop Widget — Specification

## Overview

A standalone Python desktop widget (`widget.py`) that displays live EMA trading positions,
portfolio stats, and price sparklines sourced from the existing FastAPI backend at
`http://localhost:8081`. Built with Tkinter + Matplotlib — no browser, no Node.js.

**Target RAM usage:** ~80–150 MB (vs ~600 MB for Chromium)

---

## Same Project vs Separate VS Code Solution

**It lives in this repo — no separate solution needed.**

- `widget.py` sits at the project root alongside `start.sh`
- Uses the same `.venv` (Python 3.13 + matplotlib already installable there)
- Launched independently of the bot; the bot just needs to be running on port 8081
- No shared imports from `app/` — it communicates only over HTTP

---

## File Layout

```
SubnetTrader/
├── widget.py           ← new file (this spec)
├── spec/
│   └── widget.md       ← this file
├── start.sh            ← optionally extended to launch widget
└── app/                ← unchanged
```

---

## Dependencies

Add to `requirements.txt`:

```
matplotlib>=3.9
requests>=2.31
```

`tkinter` is part of Python's standard library. On Raspberry Pi OS it may need:

```bash
sudo apt install python3-tk
```

---

## Window Design

### Style
- Dark theme (`#0d1117` background, white/grey text) — matches the frontend aesthetic
- Borderless (`overrideredirect=True`) with a slim custom title bar for dragging
- Always-on-top toggle via right-click context menu
- Fixed size: **480 × 640 px** (fits on screen beside other windows)
- Positioned at top-right corner on launch (configurable via `WIDGET_X`, `WIDGET_Y` env vars)

### Layout (top to bottom)

```
┌─────────────────────────────────────────┐
│  [●] SubnetTrader  [live/dry] [⟳] [✕]  │  ← drag bar (title bar)
├─────────────────────────────────────────┤
│  Wallet  12.34 τ   |  TAO/USD  $450.12  │  ← summary row
│  Pot     10.00 τ   |  Deployed  6.00 τ  │
├─────────────────────────────────────────┤
│  OPEN POSITIONS                         │  ← section header
│  ┌───────────────────────────────────┐  │
│  │ SN18 Cortex   +4.2%  ████░░  0.6τ │  │  ← position row (×5 max)
│  │ SN64 Chutes   -1.1%  ██░░░░  0.4τ │  │
│  │ ...                               │  │
│  └───────────────────────────────────┘  │
├─────────────────────────────────────────┤
│  [sparkline chart — 7d prices]          │  ← matplotlib canvas
│  shows price + EMA line for selected    │
│  position (click row to select)         │
├─────────────────────────────────────────┤
│  SIGNALS  (top movers)                  │  ← section header
│  SN42 ▲ BUY   SN11 ▼ SELL  SN7 — HOLD │  ← top 3 signals inline
├─────────────────────────────────────────┤
│  Last update: 14:23:05  [BOT RUNNING]   │  ← status bar
└─────────────────────────────────────────┘
```

---

## Data Sources

All data fetched over HTTP from the local FastAPI. No direct DB or chain access.

| Section           | Endpoint                     | Key fields used |
|-------------------|------------------------------|-----------------|
| Portfolio summary | `GET /api/ema/portfolio`     | `wallet_balance`, `pot_tao`, `deployed_tao`, `dry_run`, `open_positions[]` |
| Position rows     | `GET /api/ema/positions`     | `netuid`, `name`, `status`, `entry_price`, `pnl_pct`, `tao_deployed`, `entry_time` |
| Sparkline prices  | `GET /api/ema/signals`       | `seven_day_prices[]`, `ema`, `fast_ema`, `signal` per netuid |
| TAO/USD price     | `GET /api/price/tao-usd`     | `price_usd` |
| Health check      | `GET /health`                | used to show BOT RUNNING / OFFLINE status |

---

## Refresh Strategy

- **Background thread** polls all endpoints every **30 seconds** (non-blocking)
- Tkinter `after()` used to push data to UI thread safely — no direct cross-thread widget calls
- On fetch failure: show stale data with a grey "OFFLINE" badge and timestamp of last success
- Manual refresh button (⟳ in title bar) triggers an immediate poll

---

## Position Row Details

Each open position row shows:
- Subnet name (e.g. `SN18 Cortex`)
- PnL % coloured: green `≥ 0`, red `< 0`, yellow within 1% of stop-loss
- A mini horizontal bar representing PnL progress toward take-profit (20%) — drawn with a
  Tkinter `Canvas` rectangle, no matplotlib needed for this element
- TAO deployed (e.g. `2.0τ`)
- Clicking a row selects it and redraws the sparkline chart below

---

## Sparkline Chart

- Rendered in an embedded `matplotlib.backends.backend_tkagg.FigureCanvasTkAgg` canvas
- Size: 460 × 160 px, transparent/dark background
- Plots: 7-day price history (white line) + slow EMA (sky blue line) + fast EMA (orange line)
- Current price marked with a horizontal dashed line
- Entry price marked with a horizontal dotted green/red line
- No axes labels to save space; y-axis tick values only (small font)
- Title: subnet name + current price

---

## Title Bar Controls

| Control | Behaviour |
|---------|-----------|
| `●` dot | Green = bot running, red = offline |
| `[live]` / `[dry]` badge | Reflects `dry_run` from portfolio endpoint |
| `⟳` button | Force-refresh now |
| `✕` button | Close widget |
| Right-click anywhere on title bar | Context menu: Toggle always-on-top, Set opacity (70/85/100%) |
| Click-drag on title bar | Move window |

---

## Configuration

Widget reads optional env vars (falls back to defaults):

| Var | Default | Purpose |
|-----|---------|---------|
| `WIDGET_API` | `http://localhost:8081` | FastAPI base URL |
| `WIDGET_REFRESH_SEC` | `30` | Poll interval |
| `WIDGET_X` | right-aligned | Initial X position |
| `WIDGET_Y` | `20` | Initial Y position |
| `WIDGET_OPACITY` | `0.92` | Window opacity (0.0–1.0) |

These can be set in `.env` — `widget.py` loads `.env` via `python-dotenv` (already a
transitive dependency in the project).

---

## Running

```bash
# Activate venv and launch (bot must already be running)
source .venv/bin/activate
python widget.py

# Or in background (logs to data/widget.log)
nohup python widget.py >> data/widget.log 2>&1 &
```

Optionally add to `start.sh` after the bot launch line:

```bash
sleep 5 && nohup python widget.py >> data/widget.log 2>&1 &
```

---

## Out of Scope

- No write actions (no manual close button in widget — use the frontend for that)
- No authentication (localhost only)
- No historical trade log view (use the frontend `/api/export/trades.csv`)
- No main strategy positions (EMA strategy only, as that is the live strategy)
