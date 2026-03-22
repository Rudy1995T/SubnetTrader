# SubnetTrader Documentation — Specification

## Overview

Provide comprehensive documentation so that a new user can clone the repo, install, configure,
and operate the bot without external help. Documentation lives in two places: the README (for
developers and terminal users) and in-app help (for browser-based users).

**Goal:** a user who has never seen the project can go from `git clone` to a running bot using
only the README and the setup wizard's inline help — no Slack, no Discord, no asking the author.

---

## File Layout

```
SubnetTrader/
├── README.md                              ← rewritten: quick start, architecture, config, FAQ
├── frontend/src/
│   ├── components/
│   │   └── SetupWizard.tsx                ← updated: HelpTip tooltips + external links
│   └── app/
│       └── settings/
│           └── page.tsx                   ← updated: HelpTip tooltips on all fields
└── spec/
    └── documentation.md                   ← this file
```

---

## Part 1 — README Rewrite

### 1.1 Quick Start

Three commands to get running:

```bash
git clone <repo-url> SubnetTrader && cd SubnetTrader
bash install.sh
bash start.sh
```

The README leads with this. No preamble, no prerequisites list before the commands — just
clone → install → start. Prerequisites are checked by `install.sh` and errors are
self-explanatory.

### 1.2 Architecture Overview

A text-based diagram showing the full stack:

- **Browser layer**: Next.js pages (EMA, Control, Settings, Setup)
- **API layer**: FastAPI on port 8081 (EMA Manager, Executor, Config API)
- **External services**: Taostats, FlameWire RPC, Telegram
- **Storage**: SQLite with WAL mode

Plus a **Key Files** table mapping every important file to its purpose.

### 1.3 Configuration Reference Table

Every `.env` variable documented in a table with:

| Column | Purpose |
|--------|---------|
| Variable | The exact env var name |
| Default | Default value (or "empty") |
| Description | What it does, in plain English |

Grouped by category: Wallet, RPC & Data, EMA Strategy, Execution, Telegram, Observability.

### 1.4 Troubleshooting FAQ

Covers the most common failure modes:

| Problem | Section |
|---------|---------|
| Bot won't start | Port conflicts, venv issues, log inspection |
| Frontend can't reach backend | Backend must be up first, health check |
| Wallet verification fails | Path issues, password, chain connectivity |
| Trades not executing | DRY_RUN flag, balance, slippage, kill switch |
| High slippage | Pool depth, safe_staking, tuning slippage % |
| Taostats API errors | 401 (bad key), rate limits, cache TTL |
| Telegram not sending | Token/chat ID, first-message requirement |
| Raspberry Pi issues | Slow npm, compilation time, memory |

Each section includes the exact commands to diagnose and fix.

---

## Part 2 — In-App Help

### 2.1 HelpTip Component

A small `(?)` button next to field labels that shows a tooltip on hover/click.

**Behaviour:**
- Desktop: tooltip appears on `mouseEnter`, disappears on `mouseLeave`
- Mobile: tooltip toggles on `click`
- Tooltip positioned above the `(?)` button, centered, with a small arrow
- Max width: 256px (`w-64`)
- Background: `bg-gray-800 border border-gray-700`
- Text: `text-xs text-gray-300`
- Z-index: 40 (below modals at z-50)

**Implementation:** inline component in both `SetupWizard.tsx` and `settings/page.tsx`
(not extracted to a shared file — each page has its own copy to avoid coupling).

### 2.2 ExternalLink Component

A styled `<a>` tag for linking to external services:

```tsx
<ExternalLink href="https://flamewire.io">Get a key at flamewire.io</ExternalLink>
```

- Opens in new tab (`target="_blank"`)
- Styled: `text-indigo-400 hover:text-indigo-300 hover:underline`
- Includes `rel="noopener noreferrer"`

### 2.3 FieldLabel Enhancement

`FieldLabel` gains an optional `tip` prop:

```tsx
<FieldLabel htmlFor="pot-tao" tip="Total TAO allocated for trading...">
  Trading Pot
</FieldLabel>
```

When `tip` is provided, a `HelpTip` is rendered inline after the label text.

### 2.4 Tooltip Content by Field

#### Setup Wizard — Step 1 (Wallet)

| Field | Tooltip |
|-------|---------|
| Wallet Name | The directory name of your wallet under the wallet path. Created with 'btcli wallet create' or via the wizard. |
| Hotkey Name | The hotkey used for signing transactions. Each wallet can have multiple hotkeys. 'default' works for most setups. |
| Wallet Path | Directory where Bittensor wallets are stored. The default (~/.bittensor/wallets) is standard for btcli. |
| Coldkey Password | If your coldkey is encrypted, enter the password here. The bot needs it to sign transactions. Leave blank for unencrypted keys. |

#### Setup Wizard — Step 2 (API Keys)

| Field | Tooltip | External Link |
|-------|---------|---------------|
| FlameWire API Key | FlameWire provides premium RPC access with lower latency. Optional — the bot works without it. | flamewire.io |
| Taostats API Key | Taostats provides subnet pool data and price history. Free tier (no key) allows 30 req/min. | taostats.io |

#### Setup Wizard — Step 3 (Telegram)

| Field | Tooltip | External Link |
|-------|---------|---------------|
| Bot Token | The token @BotFather gives you after creating a bot. | t.me/BotFather |
| Chat ID | Your numeric Telegram user ID. Send any message to @userinfobot to find it. | t.me/userinfobot |

Step instructions also link to @BotFather and @userinfobot.

#### Setup Wizard — Step 4 (Trading)

| Field | Tooltip |
|-------|---------|
| Trading Pot | Total TAO allocated for trading. Divided equally among max positions. Independent of wallet balance. |
| Max Open Positions | Maximum number of subnet positions open at once. Each gets an equal share of the pot. |
| Max Slippage | Maximum price slippage allowed on entry. If actual price differs by more than this %, trade is rejected. |
| Stop Loss | Exit if a position drops this % below entry price. Protects against large losses. |
| Take Profit | Exit when a position gains this % above entry price. Locks in profit at target. |
| Trailing Stop | Once profitable, exit if position drops this % from peak. Lets winners run while protecting gains. |
| Max Holding Time | Auto-exit after this many hours regardless of P&L. Prevents capital stuck in stale trades. |
| Re-entry Cooldown | Wait this long before re-entering same subnet after exit. Prevents re-entering failing positions. |

#### Settings Page — Advanced EMA

| Field | Tooltip |
|-------|---------|
| Slow EMA Period | Number of candles for the slow EMA line. Larger = smoother, slower to react. |
| Fast EMA Period | Number of candles for the fast EMA line. Smaller = more responsive. Must be < slow period. |
| Confirmation Bars | Consecutive candles fast EMA must stay above slow before confirming a buy signal. |
| Candle Timeframe | Each candle represents this many hours of price data. 4h is a good balance. |
| Drawdown Breaker | If portfolio P&L drops this % from peak, pause all new entries. Circuit breaker for bad markets. |
| Drawdown Pause | How long to pause new entries after drawdown breaker trips. |
| Position Size | Fraction of pot per position (0.20 = 20%). Usually auto-set as 1/max_positions. |
| Scan Interval | Minutes between strategy scans. Lower = more responsive but more API calls. |
| Log Level | Controls log verbosity. INFO recommended. DEBUG is noisy. |

---

## What Your Friend Needs to Do

After you package and share the repo:

1. `git clone <url> && cd SubnetTrader`
2. `bash install.sh`
3. `bash start.sh`
4. Open `http://localhost:3000/setup`
5. Create or point to a Bittensor wallet
6. Enter API keys (or skip for free tier)
7. Choose dry-run or live + strategy preset
8. Hit "Save & Start"

The setup wizard handles everything. The README is there for troubleshooting and reference.

---

## Validation Checklist

- [ ] README quick start works on a fresh clone (3 commands)
- [ ] Architecture diagram renders correctly in GitHub markdown
- [ ] Every `.env` variable is documented in the config reference table
- [ ] Troubleshooting sections cover the top 8 failure modes
- [ ] Common commands section has correct, working examples
- [ ] HelpTip tooltips appear on hover for every field in SetupWizard
- [ ] HelpTip tooltips appear on hover for every field in Settings page
- [ ] External links open in new tab and point to correct URLs
- [ ] Telegram step links to @BotFather and @userinfobot
- [ ] API Keys step links to flamewire.io and taostats.io
- [ ] Tooltips are readable on mobile (click to toggle)
- [ ] Tooltip z-index does not conflict with modals (z-40 vs z-50)

---

## Out of Scope

- Video tutorials or screencasts.
- Translations / i18n for tooltips.
- Versioned documentation (changelog is in git history).
- External documentation site (README + in-app help is sufficient).
- API reference docs for the backend endpoints (developers can read the code).
