# Trading Signal Bot

Lightweight Python bot for MetaTrader 5 that watches market conditions and sends trading signals to Telegram.

## What It Does

- Reads live market data from MT5
- Evaluates strategy rules on closed candles
- Sends BUY/SELL signal alerts to Telegram
- Supports:
  - M15 + M1 strategy flow
  - Optional M1-only mode
  - Chained signal mode (M15 trigger -> M1 confirmations)
  - Signal deduplication and retry queue

## Current Status

- Core bot flow is implemented
- Chained BUY/SELL strategy is implemented
- Summary signals for multiple matches are implemented
- Active validation focus is real-time signal behavior (missed/expected signals)

## Requirements

- Windows machine (MT5 Python integration)
- Python 3.10+
- MetaTrader 5 terminal installed and logged in
- Telegram bot token and target chat ID

## Setup

1. Install Poetry and dependencies:

```powershell
python -m pip install poetry
python -m poetry install
```

2. Create `.env` from template:

```powershell
Copy-Item .env.example .env
```

3. Fill `.env` values:
- `MT5_LOGIN`
- `MT5_PASSWORD`
- `MT5_SERVER`
- `MT5_TERMINAL_PATH` (optional)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

4. Check config:
- `config/settings.yaml` (default)
- `config/settings.m1_only.yaml` (M1-only enabled)

## Run

Dry run:

```powershell
python -m poetry run trading-signal-bot --dry-run
```

Live:

```powershell
python -m poetry run trading-signal-bot
```

M1-only config:

```powershell
python -m poetry run trading-signal-bot --config config/settings.m1_only.yaml
```

## Website Settings (XM Signals)

Optional. Lets you edit the bot's settings from the XM Signals website (pogingkita.stayatdiez.com) instead of editing
`config/settings.yaml`. Every save on the website is a numbered version; the bot applies new versions automatically.

1. On the website, Super Admin: **Bot connection -> Create token**. Copy it (shown once).
2. In `.env` set `XM_SETTINGS_TOKEN=<token>` (and `XM_SETTINGS_URL` if different from the default).
3. Check the connection (read-only, changes nothing):

```powershell
python -m poetry run trading-signal-bot --remote-check
```

4. Run the bot through the supervisor so it can restart itself when new settings arrive:

```powershell
.\scripts\run_supervised.ps1
```

How it behaves:
- Polls the website about once a minute over outbound HTTPS. Nothing on this PC is exposed.
- Each new version is merged onto `config/settings.yaml`, validated with the bot's own loader, and written to
  `config/settings.effective.yaml`. A version the loader rejects is **never applied**: the bot keeps its last good
  settings and reports the problem to the website.
- It restarts at a quiet moment (no M15 -> M1 setup in flight, waiting at most 30 minutes) with exit code 75.
- If the website is unreachable, the bot keeps running on its last good settings (cached in `data/remote_settings_state.json`).
- Only editable fields come from the website. Paths, logging, Telegram queue and all credentials stay local in `.env`.
- Turn it off for one run with `--no-remote`, or remove `XM_SETTINGS_TOKEN`. `--dry-run` also ignores the website.

## One-Click Local Launch

If included in your branch/workspace:

```powershell
run_bot_live.bat
```

This runs MT5 preflight checks and then starts the bot with your configured settings.

## Troubleshooting

- If `poetry` is not recognized, use `python -m poetry ...`
- If MT5 initialize fails (`IPC timeout`):
  - keep MT5 open and connected
  - confirm `.env` login/server matches active MT5 account
  - enable MT5 API in `Tools -> Options -> Expert Advisors`
- If only startup message appears in Telegram:
  - bot is running, but strategy conditions are not met yet

## Project Layout

```text
src/trading_signal_bot/
  main.py
  strategy.py
  mt5_client.py
  telegram_notifier.py
  settings.py
  models.py
  indicators/
  repositories/
config/
tests/
docs/
```

## Documentation

- `docs/Planning.md` - implementation roadmap and branch status
- `docs/TECHNICAL_SPEC.md` - technical behavior and config contracts

