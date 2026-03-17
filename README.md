# SubnetTrader EMA

This repo contains only the EMA trading runtime and the two frontend surfaces that operate it:

- `EMA` tab for portfolio state, signals, charts, and manual closes
- `Control` tab for pause/resume, manual EMA cycle runs, CSV export, and dry-run resets

## Runtime

- Backend: FastAPI + APScheduler in [`app/main.py`](/home/pi/Desktop/SN_Bot/SubnetTrader/app/main.py)
- EMA strategy: [`app/portfolio/ema_manager.py`](/home/pi/Desktop/SN_Bot/SubnetTrader/app/portfolio/ema_manager.py)
- Frontend tabs: [`frontend/src/app/ema/page.tsx`](/home/pi/Desktop/SN_Bot/SubnetTrader/frontend/src/app/ema/page.tsx) and [`frontend/src/app/control/page.tsx`](/home/pi/Desktop/SN_Bot/SubnetTrader/frontend/src/app/control/page.tsx)
- Optional Telegram alerts and commands: [`app/notifications/telegram.py`](/home/pi/Desktop/SN_Bot/SubnetTrader/app/notifications/telegram.py)

## Start

```bash
source .venv/bin/activate
python -m app.main
```

In another shell:

```bash
cd frontend
npm run dev
```

The frontend root now redirects to `/ema`.

## Reporting

Use the `Download CSV` button in the Control tab, or export directly from the backend:

```bash
source .venv/bin/activate
python -m app.main export
```

This writes `data/exports/ema_trades.csv`, which is suitable for tax/reporting workflows.

## Telegram

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, the bot now sends alerts and accepts EMA-only control commands in that chat:

- `/status`
- `/positions [limit]`
- `/close <position_id|sn42>`
- `/pause`
- `/resume`
- `/run`
- `/export`

The polling bot ignores stale pending updates on startup, so a restart does not replay old commands.

## Config

Copy `.env.example` to `.env` and set the wallet, FlameWire, Taostats, and optional Telegram credentials. The remaining strategy settings are EMA-only.
