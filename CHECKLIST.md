# CHECKLIST.md — trading-signal-bot ("Poging Kita" bots)
Living record per CLAUDE.md §7. Updated the same session work ships.
Last updated: 2026-11-19 (4 bots running on DEMO 318967331; bot 4 just built; log timezone fixed)

### Plan & phases
- [ ] No single governing `*_PLAN.md` for this repo — built bot-by-bot by direct approval each session
      (Adrian approved "option A" 2026-11-19: research first, build only the winning strategy)
- [x] `verify` run informally each session via pytest; no formal phase gates (not a phased project)

### Development
- [x] Bot 1 — M15 strategy bot (magic 570031), legacy S1/S2 scenarios, `scripts/run_supervised.ps1`
- [x] Bot 2 — M1 bot (magic 570032), `[M1]` label, `scripts/run_m1_bot.ps1` + `.env.m1`
- [x] Bot 3 — Daily Dip (magic 570033), `[DIP]`, US500+NAS100, `scripts/run_dip_bot.ps1` (2026-11-19)
- [x] Bot 4 — Crypto Trend (magic 570034), `[TREND]`, BTC+ETH Donchian 55/20 breakout, `scripts/run_trend_bot.ps1` (2026-11-19)
- [x] `research/strategy_lab.py` + `research/daily_lab.py` — reusable backtest tooling, ~140-market universe
      research behind bots 3 and 4 (committed 2026-11-19)
- [ ] No placeholders / stub code remaining — true for bots 1-4; M1 backtest validation never finished (noted gap)

### QA
- [x] Functional tests passed: 181 unit tests (trader, dip_bot, trend_bot, strategy, settings, etc.), all green
- [x] Edge cases tested per bot: demo-account refusal, kill-file pause, daily-loss limit, one-position-per-symbol,
      time stops, exit-rule parity with backtest, late-entry skip, reversal (bot 4)
- [~] Performance / capacity — N/A in the traditional sense (no concurrent users); capacity = MT5 polling every
      30s per bot, four processes on one PC. Not measured under load; low risk at this scale.
- [ ] Mobile / cross-browser / accessibility — N/A, no UI in this repo (settings UI lives in `xm-web`)
- [x] Trade-lifecycle messages verified: Telegram SIGNAL / TRADE OPENED / TRADE CLOSED (with reason) / CLOSE FAILED
- [~] Professional build audit — informal only, done inline per bot as built; no standalone audit doc written

### Security
- [x] Demo-only guard: `DemoTrader._is_demo()` refuses to trade if the connected account isn't DEMO
      (proven fail-then-pass: real-account test in `test_dip_bot.py` / `test_trend_bot.py`)
- [x] Kill file per bot (`data/<bot>/TRADING_PAUSED`) stops new trades instantly; checked every loop
- [x] Daily loss limit per bot (env-configured %, Manila day boundary) blocks new entries once hit
- [ ] Tenant isolation — N/A, single account, no multi-tenant concept here
- [x] No secrets in repo; `.env` / `.env.m1` gitignored, `.env.example` documents every XM_* var
- [x] `TELEGRAM_CHAT_ID` is the shared group with Jay (restored 2026-11-19); no bot token printed in logs

### Documentation
- [ ] User Guide — N/A, operator (Adrian) only; runbook knowledge lives in session memory (`xm_status.md`)
- [ ] Admin Guide — not written; `scripts/run_*.ps1` headers double as minimal operating notes
- [~] Technical Guide — module docstrings in `dip_bot.py` / `trend_bot.py` / `strategy_lab.py` / `daily_lab.py`
      cover the rules and backtest numbers; no standalone doc

### Deployment & resilience
- [x] "Deploy" = run on Adrian's PC (no hosting profile applies; not a web app). MT5 terminal must be started
      first (`C:\Program Files\XM Global MT5\terminal64.exe`), then each bot via its `scripts\run_*.ps1`
- [x] Each bot runs under a supervising PowerShell loop with a 5-crashes/10-min circuit breaker
- [x] Logging: rotating file logs per bot in `logs/`, stamped in Manila time (fixed 2026-11-19 — was
      Asia/Phnom_Penh, 1h off, in `config/settings.yaml` + `settings.m1_bot.yaml` + `settings.research.yaml`)
- [ ] Backup / restore — N/A for this repo: no database; state is small JSON files under `data/`, rebuildable
      from MT5 history if lost. Not backed up separately today (low value, low risk)
- [x] External dependency failure modes identified: MT5 terminal closed (bot can't trade), Telegram API down
      (queued to `failed_messages.json`, retried), broker market closed (`SYMBOL_TRADE_MODE_FULL` check skips)
- [ ] Production monitoring/alerting beyond the heartbeat log line — none; Adrian checks manually on request
- [ ] Crypto (bot 4) trades weekends; the PC/MT5 must stay on through Sat/Sun or signals are missed (known gap,
      not yet solved — VPS move would fix it)

### Business logic & edge cases
- [x] One position per symbol per bot (own magic number keeps bots from colliding on the same symbol)
- [x] Exit rules proven to match the backtest exactly (parity tests in `test_dip_bot.py` / `test_trend_bot.py`)
- [ ] Review each bot's live performance at 20+ closed trades before any real-money decision (standing rule)

### Final
- [ ] Final approval — N/A, ongoing demo research project; no "launch" milestone, Adrian decides go-live per bot
