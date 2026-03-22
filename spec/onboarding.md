# SubnetTrader Onboarding UI — Specification

## Overview

A first-run setup wizard (`/setup`) and persistent settings page (`/settings`) that guide users
through configuring the bot — wallet credentials, API keys, Telegram alerts, and trading
parameters. Backed by new FastAPI endpoints that validate, write, and read `.env` config.

**Goal:** eliminate the need to manually edit `.env` — new users complete a guided wizard;
returning users tweak settings from the browser.

---

## File Layout

```
SubnetTrader/
├── app/
│   ├── main.py                  ← add /api/config routes + restart helper
│   └── config.py                ← add config validation helpers
├── frontend/src/
│   ├── app/
│   │   ├── setup/
│   │   │   └── page.tsx         ← new: setup wizard (multi-step form)
│   │   ├── settings/
│   │   │   └── page.tsx         ← new: settings editor (inline form)
│   │   ├── layout.tsx           ← update: conditional NavBar (hide during setup)
│   │   └── page.tsx             ← update: redirect logic (→ /setup if incomplete, else → /ema)
│   └── components/
│       ├── NavBar.tsx           ← update: add /settings link
│       └── SetupWizard.tsx      ← new: shared multi-step form component
├── spec/
│   └── onboarding.md            ← this file
└── .env.example                 ← unchanged (reference for defaults)
```

---

## Backend: Config API

### Endpoints

#### `GET /api/config/status`

Returns which fields are present, which are missing, and whether the bot is configured
enough to run. Used by the frontend to decide whether to redirect to `/setup`.

```json
{
  "setup_complete": false,
  "missing_required": ["BT_WALLET_NAME", "BT_WALLET_HOTKEY"],
  "missing_optional": ["FLAMEWIRE_API_KEY", "TELEGRAM_BOT_TOKEN"],
  "has_env_file": true
}
```

**Required fields** (setup is incomplete without these):
- `BT_WALLET_NAME`
- `BT_WALLET_HOTKEY`
- `BT_WALLET_PATH`

All other fields have usable defaults and are optional. `setup_complete` is `true` when
all required fields are present and non-empty in `.env`.

If `.env` does not exist at all, `has_env_file` is `false` and `setup_complete` is `false`.

---

#### `GET /api/config`

Returns current config values. **Secrets are masked.**

```json
{
  "BT_WALLET_NAME": "trader_wallet",
  "BT_WALLET_HOTKEY": "trader_hotkey",
  "BT_WALLET_PATH": "~/.bittensor/wallets",
  "BT_WALLET_PASSWORD": "••••••••",
  "FLAMEWIRE_API_KEY": "••••••••",
  "TAOSTATS_API_KEY": "",
  "TELEGRAM_BOT_TOKEN": "••••••••",
  "TELEGRAM_CHAT_ID": "12345678",
  "EMA_DRY_RUN": true,
  "EMA_POT_TAO": 10.0,
  "EMA_MAX_POSITIONS": 5,
  "EMA_POSITION_SIZE_PCT": 0.20,
  "MAX_SLIPPAGE_PCT": 5.0,
  "EMA_STOP_LOSS_PCT": 8.0,
  "EMA_TAKE_PROFIT_PCT": 20.0,
  "EMA_TRAILING_STOP_PCT": 5.0,
  "EMA_MAX_HOLDING_HOURS": 168,
  "EMA_COOLDOWN_HOURS": 4.0,
  "EMA_PERIOD": 18,
  "EMA_FAST_PERIOD": 6,
  "EMA_CONFIRM_BARS": 3,
  "EMA_CANDLE_TIMEFRAME_HOURS": 4,
  "EMA_DRAWDOWN_BREAKER_PCT": 15.0,
  "EMA_DRAWDOWN_PAUSE_HOURS": 6.0,
  "SCAN_INTERVAL_MIN": 15,
  "LOG_LEVEL": "INFO"
}
```

**Masking rules:**
- Fields containing `PASSWORD`, `TOKEN`, or `API_KEY` in their name: if non-empty, return
  `"••••••••"`. If empty, return `""`.
- All other fields: return actual values.
- Boolean fields: return as JSON booleans.
- Numeric fields: return as JSON numbers.

---

#### `POST /api/config`

Accepts a partial or full config update. Validates, merges with existing `.env`, writes
the file, and optionally triggers a graceful restart.

**Request body:**

```json
{
  "values": {
    "BT_WALLET_NAME": "trader_wallet",
    "BT_WALLET_HOTKEY": "trader_hotkey",
    "EMA_DRY_RUN": false,
    "EMA_POT_TAO": 10.0
  },
  "restart": true
}
```

**Response (success):**

```json
{
  "success": true,
  "written_fields": ["BT_WALLET_NAME", "BT_WALLET_HOTKEY", "EMA_DRY_RUN", "EMA_POT_TAO"],
  "restart_triggered": true
}
```

**Response (validation error):**

```json
{
  "success": false,
  "errors": {
    "EMA_POT_TAO": "Must be a positive number",
    "MAX_SLIPPAGE_PCT": "Must be between 0.1 and 50.0"
  }
}
```

**Behaviour:**
1. Validate each field against its type and constraints (see Validation Rules below).
2. If any field fails validation, return 422 with error map — write nothing.
3. Read existing `.env` (or start fresh if missing).
4. Merge: submitted fields overwrite existing; unsubmitted fields are preserved.
5. Write `.env` atomically (write to `.env.tmp`, then `os.replace` to `.env`).
6. If `restart` is `true`, trigger graceful restart (see Restart Strategy below).

**Validation rules:**

| Field | Type | Constraint |
|-------|------|------------|
| `BT_WALLET_NAME` | str | Non-empty, alphanumeric + underscore, max 64 chars |
| `BT_WALLET_HOTKEY` | str | Non-empty, alphanumeric + underscore, max 64 chars |
| `BT_WALLET_PATH` | str | Non-empty, valid path syntax |
| `BT_WALLET_PASSWORD` | str | Any string (may be empty) |
| `FLAMEWIRE_API_KEY` | str | Any string (may be empty) |
| `TAOSTATS_API_KEY` | str | Any string (may be empty) |
| `TELEGRAM_BOT_TOKEN` | str | Empty or matches `^\d+:[A-Za-z0-9_-]+$` |
| `TELEGRAM_CHAT_ID` | str | Empty or numeric string (negative allowed for groups) |
| `EMA_DRY_RUN` | bool | `true` or `false` |
| `EMA_POT_TAO` | float | > 0 |
| `EMA_MAX_POSITIONS` | int | 1–20 |
| `EMA_POSITION_SIZE_PCT` | float | 0.01–1.0 |
| `MAX_SLIPPAGE_PCT` | float | 0.1–50.0 |
| `EMA_STOP_LOSS_PCT` | float | 1.0–50.0 |
| `EMA_TAKE_PROFIT_PCT` | float | 1.0–100.0 |
| `EMA_TRAILING_STOP_PCT` | float | 1.0–50.0 |
| `EMA_MAX_HOLDING_HOURS` | int | 1–720 |
| `EMA_COOLDOWN_HOURS` | float | 0–48.0 |
| `EMA_PERIOD` | int | 2–100 |
| `EMA_FAST_PERIOD` | int | 2–100, must be < `EMA_PERIOD` |
| `EMA_CONFIRM_BARS` | int | 1–10 |
| `EMA_CANDLE_TIMEFRAME_HOURS` | int | 1, 2, 4, 6, 8, 12, or 24 |
| `EMA_DRAWDOWN_BREAKER_PCT` | float | 1.0–50.0 |
| `EMA_DRAWDOWN_PAUSE_HOURS` | float | 0.5–48.0 |
| `SCAN_INTERVAL_MIN` | int | 1–60 |
| `LOG_LEVEL` | str | One of: DEBUG, INFO, WARNING, ERROR, CRITICAL |

Fields not in this list are rejected (return 422 with `"Unknown field"`).

---

#### `POST /api/config/test-telegram`

Sends a test message to verify Telegram credentials.

**Request body:**

```json
{
  "bot_token": "123456:ABC-DEF...",
  "chat_id": "12345678"
}
```

**Response:**

```json
{
  "success": true,
  "message": "Test message sent successfully"
}
```

Or on failure:

```json
{
  "success": false,
  "error": "Unauthorized: invalid bot token"
}
```

**Behaviour:**
- Calls Telegram `sendMessage` API directly with: `"SubnetTrader test — configuration verified."`
- Timeout: 10 seconds.
- Does NOT require the values to be saved to `.env` first (uses provided values directly).

---

#### `POST /api/config/test-taostats`

Tests the Taostats API key by making a lightweight API call.

**Request body:**

```json
{
  "api_key": "tskey_..."
}
```

**Response:**

```json
{
  "success": true,
  "message": "Taostats API key is valid"
}
```

**Behaviour:**
- Calls `GET https://api.taostats.io/api/dtao/pool/latest/v1?limit=1` with header
  `Authorization: <api_key>`.
- If 200: success. If 401/403: invalid key. If empty key: test public access (no auth header)
  and report whether public access works.
- Timeout: 10 seconds.

---

#### `POST /api/config/test-wallet`

Verifies wallet credentials can load and checks the TAO balance.

**Request body:**

```json
{
  "wallet_name": "trader_wallet",
  "hotkey": "trader_hotkey",
  "wallet_path": "~/.bittensor/wallets",
  "password": "..."
}
```

**Response (success):**

```json
{
  "success": true,
  "coldkey_ss58": "5CJVx...",
  "balance_tao": 12.34
}
```

**Response (failure):**

```json
{
  "success": false,
  "error": "Wallet not found at ~/.bittensor/wallets/trader_wallet"
}
```

**Behaviour:**
- Instantiates a Bittensor `Wallet` with the provided params.
- Attempts to load the coldkey (using password if provided).
- If the wallet loads, queries TAO balance via subtensor.
- Timeout: 15 seconds (chain queries can be slow).
- This is a read-only check — no transactions.

---

### Restart Strategy

When `POST /api/config` is called with `restart: true`:

1. Reload the `Settings` object from the newly written `.env` so the running process
   picks up changes immediately: `importlib.reload` the config module, or re-instantiate
   `Settings()` and replace the module-level `settings` reference.
2. If the scheduler is running, reschedule jobs with potentially new intervals
   (`SCAN_INTERVAL_MIN`, `EMA_EXIT_WATCHER_SEC`).
3. Re-initialize services that depend on changed config (e.g., if `FLAMEWIRE_API_KEY`
   changed, recreate the RPC client; if Telegram token changed, restart the bot).
4. Return the response immediately — do NOT kill the process. This is a hot-reload, not
   a process restart.

If hot-reload is insufficient for a particular change (e.g., wallet credentials), the
response should include `"full_restart_required": true` and the frontend should show a
message: *"Restart the bot for wallet changes to take effect"* with a button that calls
`POST /api/control/restart` (which can use `os.execv` to replace the process, or signal
`start.sh` via a sentinel file).

---

### .env Write Format

When writing `.env`, preserve the section comments from `.env.example` for readability.
Use this template order:

```
# FlameWire RPC
FLAMEWIRE_API_KEY=...
...

# Subtensor fallback
SUBTENSOR_FALLBACK_NETWORK=...

# Taostats
TAOSTATS_API_KEY=...
...

# Wallet
BT_WALLET_NAME=...
...

# Scheduler
SCAN_INTERVAL_MIN=...

# Execution
MAX_ENTRY_PRICE_TAO=...
MAX_SLIPPAGE_PCT=...

# EMA strategy
EMA_ENABLED=true
EMA_DRY_RUN=...
...

# Telegram alerts + commands (optional)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Observability
LOG_LEVEL=...
...
```

Fields not explicitly set by the user should be written with their default values (from
`config.py` or `.env.example`), so the file is always complete.

---

## Frontend: Setup Wizard (`/setup`)

### First-Run Detection

In `frontend/src/app/page.tsx` (root page), on mount:

1. Fetch `GET /api/config/status`.
2. If `setup_complete` is `false`, redirect to `/setup`.
3. If `setup_complete` is `true`, redirect to `/ema` (current behaviour).

During the setup wizard, the NavBar is hidden (the wizard has its own minimal header).

---

### Wizard Component (`SetupWizard.tsx`)

A reusable multi-step form component used by both `/setup` and embedded in `/settings`.

**Props:**
```tsx
interface SetupWizardProps {
  mode: "wizard" | "settings";   // wizard = full flow; settings = inline tabs
  initialValues?: ConfigValues;   // pre-fill from GET /api/config
  onComplete?: () => void;        // called after successful save
}
```

---

### Step 1 — Wallet

**Fields:**

| Field | Label | Type | Required | Notes |
|-------|-------|------|----------|-------|
| `BT_WALLET_NAME` | Wallet Name | text | yes | Default: `default` |
| `BT_WALLET_HOTKEY` | Hotkey Name | text | yes | Default: `default` |
| `BT_WALLET_PATH` | Wallet Path | text | yes | Default: `~/.bittensor/wallets` |
| `BT_WALLET_PASSWORD` | Coldkey Password | password | no | Masked input |

**Layout:**
- Title: "Connect Your Wallet"
- Subtitle: "Enter the Bittensor wallet credentials stored on this machine."
- Each field is a labelled input with placeholder text showing the default.
- Password field has a show/hide toggle (eye icon).
- **"Verify Wallet"** button at the bottom — calls `POST /api/config/test-wallet` with
  the current field values.
  - On success: show green check + coldkey address + balance in TAO.
  - On failure: show red error message below the button.
  - The button is not required to proceed — users can skip verification.

**Validation (client-side):**
- Wallet name and hotkey: non-empty.
- Path: non-empty (no filesystem check — the backend verify button does that).

---

### Step 2 — API Keys

**Fields:**

| Field | Label | Type | Required | Notes |
|-------|-------|------|----------|-------|
| `FLAMEWIRE_API_KEY` | FlameWire API Key | password | no | |
| `TAOSTATS_API_KEY` | Taostats API Key | password | no | |

**Layout:**
- Title: "API Keys (Optional)"
- Each key has:
  - A description paragraph explaining what the service does and why you might want a key.
  - For FlameWire: "Provides fast RPC access to the Bittensor chain. Without a key, the
    bot falls back to the public subtensor endpoint, which may be slower."
  - For Taostats: "Provides subnet price data and pool metrics. Without a key, the free
    tier is used (30 requests/min)."
  - A show/hide toggle on the input.
  - A **"Test Connection"** button (Taostats only) — calls `POST /api/config/test-taostats`.

**Validation (client-side):**
- None required — both fields are optional.

---

### Step 3 — Telegram (Optional)

**Fields:**

| Field | Label | Type | Required | Notes |
|-------|-------|------|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Bot Token | password | no | |
| `TELEGRAM_CHAT_ID` | Chat ID | text | no | |

**Layout:**
- Title: "Telegram Alerts (Optional)"
- Description: "Receive trade alerts, position updates, and daily summaries via Telegram.
  You can set this up later from the Settings page."
- Brief inline instructions: "1. Message @BotFather on Telegram to create a bot. 2. Copy
  the bot token. 3. Send a message to your bot, then use @userinfobot to get your chat ID."
- **"Send Test Message"** button — calls `POST /api/config/test-telegram` with the
  current field values.
  - Disabled if either field is empty.
  - On success: green "Message sent! Check your Telegram."
  - On failure: red error message.
- **"Skip"** link below the test button — proceeds without configuring Telegram.

---

### Step 4 — Trading Mode

**Presets:**

Three preset buttons at the top, styled as selectable cards:

| Preset | DRY_RUN | POT_TAO | MAX_POSITIONS | SLIPPAGE | STOP_LOSS | TAKE_PROFIT | TRAILING |
|--------|---------|---------|---------------|----------|-----------|-------------|----------|
| **Conservative** | true | 5.0 | 3 | 3.0% | 5.0% | 15.0% | 3.0% |
| **Moderate** | false | 10.0 | 5 | 5.0% | 8.0% | 20.0% | 5.0% |
| **Aggressive** | false | 20.0 | 8 | 8.0% | 12.0% | 30.0% | 8.0% |

Selecting a preset fills in the fields below. Users can then modify individual values.

**Fields:**

| Field | Label | Type | Notes |
|-------|-------|------|-------|
| `EMA_DRY_RUN` | Paper Trading Mode | toggle | ON = dry run (no real trades) |
| `EMA_POT_TAO` | Trading Pot (TAO) | number | The total TAO allocated for trading |
| `EMA_MAX_POSITIONS` | Max Open Positions | number (stepper) | 1–20 |
| `MAX_SLIPPAGE_PCT` | Max Slippage % | number | 0.1–50.0 |
| `EMA_STOP_LOSS_PCT` | Stop Loss % | number | |
| `EMA_TAKE_PROFIT_PCT` | Take Profit % | number | |
| `EMA_TRAILING_STOP_PCT` | Trailing Stop % | number | |
| `EMA_MAX_HOLDING_HOURS` | Max Holding Time (hours) | number | |
| `EMA_COOLDOWN_HOURS` | Re-entry Cooldown (hours) | number | |

**Layout:**
- Title: "Trading Configuration"
- Preset cards in a horizontal row (3 columns).
- Below presets: DRY_RUN toggle, prominent with a warning box when OFF:
  "**Live trading is enabled.** The bot will execute real trades with your TAO."
  Background: red-900/20 border, red text.
- Remaining fields in a 2-column grid on desktop, single column on mobile.
- Each number input shows the unit (TAO, %, hours) as a suffix label.
- Position size is calculated and shown as a read-only helper:
  `"Each position: {POT_TAO * POSITION_SIZE_PCT} TAO ({POSITION_SIZE_PCT * 100}% of pot)"`
  — but `EMA_POSITION_SIZE_PCT` is derived from `1 / MAX_POSITIONS` for simplicity in the
  wizard. Advanced users can set it precisely in `/settings`.

**Validation (client-side):**
- All numeric fields: must be within the ranges specified in the Validation Rules table above.
- `EMA_POT_TAO`: warn (not block) if > wallet balance (if known from Step 1 verification).

---

### Step 5 — Review & Save

**Layout:**
- Title: "Review Your Configuration"
- A summary card showing all values grouped by section:
  - **Wallet**: name, hotkey, path (password hidden)
  - **API Keys**: FlameWire (set/not set), Taostats (set/not set)
  - **Telegram**: configured/not configured
  - **Trading**: dry run/live, pot size, max positions, stop-loss, take-profit, slippage
- Each section has a pencil icon that jumps back to that step for editing.
- **"Save & Start"** button at the bottom:
  1. Calls `POST /api/config` with all values and `restart: true`.
  2. Shows a spinner while saving.
  3. On success: shows green "Configuration saved!" and a progress indicator while the
     bot restarts, then redirects to `/ema` after 3 seconds.
  4. On failure: shows error messages inline and does not redirect.

---

### Wizard Navigation

- Step indicator at the top: numbered circles (1–5) connected by lines. Current step is
  highlighted (indigo-500), completed steps have a checkmark, future steps are grey.
- **"Back"** and **"Next"** buttons at the bottom of each step.
- "Next" is disabled until required fields on the current step are filled (Step 1 only has
  required fields; Steps 2–4 can always proceed).
- Steps are accessible by clicking the step indicator circles (can jump to any completed
  step or the current step).
- State is held in React `useState` — no persistence between page refreshes (acceptable;
  the wizard is short).

---

### Wizard Styling

Match the existing dark theme:
- Background: `bg-gray-950`
- Card backgrounds: `bg-gray-900 border border-gray-800 rounded-xl`
- Input fields: `bg-gray-800 border-gray-700 text-gray-100 placeholder-gray-500`
- Focus rings: `focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500`
- Primary buttons: `bg-indigo-600 hover:bg-indigo-500 text-white`
- Secondary buttons: `bg-gray-800 hover:bg-gray-700 text-gray-300`
- Danger/warning: `bg-red-900/20 border-red-800 text-red-400`
- Success: `text-emerald-400`
- Step indicator: `bg-indigo-500` (active), `bg-emerald-500` (complete), `bg-gray-700` (future)

Wizard is centered on the page with `max-w-2xl mx-auto` and generous vertical padding.
No NavBar during the wizard — just a small "SubnetTrader" logo/text at the top left.

---

## Frontend: Settings Page (`/settings`)

### Overview

The settings page reuses the same form fields as the wizard but presents them as inline,
editable sections within the standard page layout (with NavBar).

### Layout

```
┌────────────────────────────────────────────────────────┐
│  [NavBar: EMA | Control | Settings]                    │
├────────────────────────────────────────────────────────┤
│                                                        │
│  Settings                                              │
│                                                        │
│  ┌─ Wallet ──────────────────────────────────────────┐ │
│  │  Wallet Name    [trader_wallet    ]               │ │
│  │  Hotkey Name    [trader_hotkey    ]               │ │
│  │  Wallet Path    [~/.bittensor/... ]               │ │
│  │  Password       [••••••••         ] 👁            │ │
│  │  [Verify Wallet]    ✓ 5CJVx... | 12.34 TAO      │ │
│  └───────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ API Keys ────────────────────────────────────────┐ │
│  │  FlameWire Key  [••••••••         ] 👁            │ │
│  │  Taostats Key   [••••••••         ] 👁  [Test]   │ │
│  └───────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ Telegram ────────────────────────────────────────┐ │
│  │  Bot Token      [••••••••         ] 👁            │ │
│  │  Chat ID        [12345678         ]    [Test]     │ │
│  └───────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ Trading ─────────────────────────────────────────┐ │
│  │  Paper Trading  [  ON  ]                          │ │
│  │  ⚠ Live trading warning (when OFF)                │ │
│  │  Pot Size       [10.0 ] TAO                       │ │
│  │  Max Positions  [5    ]                           │ │
│  │  Slippage       [5.0  ] %                         │ │
│  │  Stop Loss      [8.0  ] %                         │ │
│  │  Take Profit    [20.0 ] %                         │ │
│  │  Trailing Stop  [5.0  ] %                         │ │
│  │  Max Hold Time  [168  ] hours                     │ │
│  │  Cooldown       [4.0  ] hours                     │ │
│  └───────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ Advanced EMA ────────────────────────────────────┐ │
│  │  (collapsed by default — expand to reveal)        │ │
│  │  EMA Period, Fast Period, Confirm Bars,           │ │
│  │  Candle Timeframe, Drawdown Breaker, etc.         │ │
│  └───────────────────────────────────────────────────┘ │
│                                                        │
│         [Save Changes]   [Discard]                     │
│                                                        │
│  Last saved: 2026-03-20 14:23 UTC                      │
└────────────────────────────────────────────────────────┘
```

### Behaviour

1. On mount: fetch `GET /api/config` to populate all fields.
2. Track dirty state: compare current form values against initial values. Show a floating
   "Unsaved changes" bar at the bottom when dirty.
3. **"Save Changes"** button:
   - Calls `POST /api/config` with only the changed fields + `restart: true`.
   - On success: toast "Settings saved", refresh form from `GET /api/config`.
   - On validation error: highlight the failing fields with red borders and error messages.
4. **"Discard"** button: resets all fields to the values from the last `GET /api/config`.
5. **"Test Connection" buttons**: same behaviour as in the wizard steps.
6. **"Verify Wallet" button**: same as wizard Step 1.
7. **Unsaved changes guard**: if the user tries to navigate away with dirty state, show a
   browser `beforeunload` confirmation dialog.

### Advanced EMA Section

The "Advanced EMA" section is collapsed by default (a `<details>` or state-toggled div).
It contains the fields that most users won't need to change:

| Field | Label | Default |
|-------|-------|---------|
| `EMA_PERIOD` | Slow EMA Period | 18 |
| `EMA_FAST_PERIOD` | Fast EMA Period | 6 |
| `EMA_CONFIRM_BARS` | Confirmation Bars | 3 |
| `EMA_CANDLE_TIMEFRAME_HOURS` | Candle Timeframe (hours) | 4 |
| `EMA_DRAWDOWN_BREAKER_PCT` | Drawdown Breaker % | 15.0 |
| `EMA_DRAWDOWN_PAUSE_HOURS` | Drawdown Pause (hours) | 6.0 |
| `EMA_POSITION_SIZE_PCT` | Position Size % | 0.20 |
| `SCAN_INTERVAL_MIN` | Scan Interval (minutes) | 15 |
| `LOG_LEVEL` | Log Level | INFO (dropdown) |

---

## NavBar Update

Add a "Settings" link to the NavBar, after "Control":

```
EMA | Control | Settings
```

Uses the same active-link styling pattern (indigo bottom border + text colour when on
`/settings`). The gear icon (`⚙`) can optionally prefix the label.

---

## Root Page Redirect Logic

Update `frontend/src/app/page.tsx`:

```tsx
"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function Home() {
  const router = useRouter();
  const API = process.env.NEXT_PUBLIC_API_URL || `http://${typeof window !== "undefined" ? window.location.hostname : "localhost"}:8081`;

  useEffect(() => {
    fetch(`${API}/api/config/status`)
      .then(r => r.json())
      .then(data => {
        router.replace(data.setup_complete ? "/ema" : "/setup");
      })
      .catch(() => {
        // Backend unreachable — go to EMA page (will show error state)
        router.replace("/ema");
      });
  }, []);

  return null; // or a loading spinner
}
```

---

## Layout Update

In `layout.tsx`, conditionally hide the NavBar when on `/setup`:

```tsx
// Pseudocode — use usePathname() from next/navigation
const pathname = usePathname();
const showNav = !pathname.startsWith("/setup");

return (
  <html>
    <body>
      {showNav && <NavBar />}
      <main>{children}</main>
    </body>
  </html>
);
```

This requires making the layout a client component or extracting the conditional nav into
a wrapper client component (since `layout.tsx` is currently a server component importing
client NavBar). The cleanest approach: create a `<ConditionalNav />` client component.

---

## Security Considerations

- **No authentication** — the bot runs on localhost (Raspberry Pi). The config endpoints
  are accessible only on the local network. This matches the existing security model where
  all endpoints (including `/api/control/pause`, manual close, etc.) are unauthenticated.
- **Secret masking** — `GET /api/config` never returns plaintext secrets. The settings page
  sends secrets only when the user has actively modified the field (dirty check).
- **Atomic writes** — `.env` is written via a temp file + `os.replace()` to avoid partial
  writes on crash.
- **No shell injection** — all values are written as plain `KEY=value` lines. Values
  containing special characters are not quoted (pydantic-settings handles this). If a value
  contains `=` or newlines, it should be rejected in validation.

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| Backend unreachable during wizard | Show "Cannot connect to backend" with retry button. Do not allow proceeding. |
| Validation error on save | Highlight failing fields, show error messages, do not close wizard. |
| `.env` write fails (permissions) | Return 500 with `"error": "Failed to write .env: Permission denied"`. Frontend shows the error. |
| Restart fails | Return success for the write, `"restart_triggered": false, "restart_error": "..."`. Frontend shows warning. |
| Test connection timeout | Show "Connection timed out — check your network" after 10–15s. |

---

## Out of Scope

- User authentication / multi-user support — single operator, localhost only.
- Editing `.env` fields not listed in the Validation Rules table (e.g., `FLAMEWIRE_CHAIN`,
  `SUBTENSOR_FALLBACK_NETWORK`, `DB_PATH`) — advanced users edit `.env` directly.
- Import/export config files.
- Config version history / undo.
- Changing `HEALTH_PORT` from the UI (would break the connection).
