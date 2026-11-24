# CHECKLIST.md — trading-signal-bot ("Poging Kita" bots)
Living record per CLAUDE.md §7. Updated the same session work ships.
Last updated: 2026-11-21 (account-wide risk breaker; 7 new symbols across bots 3/4/5/6; W7 web reporter LIVE
with real prod tokens (Dip/Trend/VBO/CBO/M1 each got their own Bot + token via `create-strategy-bots.yml`,
main bot reuses its existing one); then a security/code review found and fixed 3 bugs (atomic trades.json
write, CBO's unreachable time-stop, VBO's ambiguous dual-level entry) - see entries below. All of this is
pushed to the fork (`iyanthott/xm-signal-bot` `feature/settings-editor`, now at `d7a3605`) and all 6 live bot
processes on Adrian's PC have been restarted twice this session, most recently onto `d7a3605`)

### Plan & phases
- [ ] No single governing `*_PLAN.md` for this repo — built bot-by-bot by direct approval each session
      (Adrian approved "option A" 2026-11-19: research first, build only the winning strategy)
- [x] `verify` run informally each session via pytest; no formal phase gates (not a phased project)

### Development
- [x] **RETIRED 2026-11-24** — Bot 1 — M15 strategy bot (magic 570031), legacy S1/S2 scenarios,
      `scripts/run_supervised.ps1`. Never run through the `daily_lab.py`/`strategy_lab.py` validation
      framework (predates it — inherited as-is from the Mineaworld fork). Live result this week: 4 closed
      trades, 0 wins, -$393.12, all FX pairs. Matches prior research exactly ("intraday trend-pullback H1,
      opening-range breakout M15 and mean-reversion M15 LOSE after spread... FX worst", 2026-11-19) — Bot 1's
      LWMA-cross + Stochastic-zone logic is that same category, just never re-tested until live money proved
      it. Process tree stopped (was already flat, 0 open positions, so no disruption); `data/TRADING_PAUSED`
      kill file left in place as a second guard. Not deleted — code stays for reference/possible reuse.
- [x] **RETIRED 2026-11-24** — Bot 2 — M1 bot (magic 570032), `[M1]` label, `scripts/run_m1_bot.ps1` +
      `.env.m1`. Same unvalidated legacy engine as Bot 1, smaller sample but same direction: 3 closed, 1W/2L,
      -$29.21. Process tree stopped (was flat); `data/m1/TRADING_PAUSED` left in place.
- [x] Bot 3 — Daily Dip (magic 570033), `[DIP]`, US500+NAS100+GBPUSD+CA60+SA40, `scripts/run_dip_bot.ps1`
      (2026-11-19; GBPUSD added 2026-11-20, test_PF 1.82/80 trades; CA60Cash+SA40Cash added 2026-11-21, see
      Opportunity scan entry below - all config-only, zero new code)
- [x] Bot 4 — Crypto Trend (magic 570034), `[TREND]`, BTC+ETH+JP225Cash+Netflix Donchian 55/20 breakout,
      `scripts/run_trend_bot.ps1` (2026-11-19; JP225Cash+Netflix added 2026-11-21, see Opportunity scan entry
      below - the engine is symbol-agnostic breakout, not crypto-specific, despite the bot's name)
- [x] Bot 5 — Volatility Breakout (magic 570035), `[VBO]`, ETH+BRENT+OIL intraday stop-order breakout (open
      +/- 0.5x prior range), 5-day time stop, `scripts/run_vbo_bot.ps1` (2026-11-20; BRENTCash+OILCash added
      2026-11-21). Built from the "learn from performing traders" research (Williams volatility breakout),
      stress-tested for robustness across k and time before building. 7 new unit tests, all green (192 total).
      No BTC — BTCUSD did not pass the backtest filter.
- [x] Bot 6 — Candle Breakout (magic 570036), `[CBO]`, GOLD+Nvidia 55-day breakout gated on a strong-bodied
      candle, `scripts/run_cbo_bot.ps1` (2026-11-20; Nvidia added 2026-11-21). Reuses Bot 4's exit rule directly
      (imported from trend_bot.py, not duplicated). Backtest: GOLD test_PF 2.55, 22 held-out trades. 8 new unit
      tests incl. live-vs-backtest formula parity, all green (200 total).
- [x] `research/strategy_lab.py` + `research/daily_lab.py` — reusable backtest tooling, ~140-market universe
      research behind bots 3 and 4 (committed 2026-11-19)
- [ ] No placeholders / stub code remaining — true for bots 1-4; M1 backtest validation never finished (noted gap)
- [~] Candlestick-pattern research (2026-11-20): added `engulf_pullback` and `breakout_candle` strategies to
      `daily_lab.py`, ran full universe. Standalone engulfing pattern + trend/RSI context = no edge (fails pass
      filter everywhere). Strong-body candle as a *filter* on the existing breakout entry improves held-out test
      PF/winrate/drawdown on BTCUSD and ETHUSD specifically (Bot 4's own markets), but test_n is only 13/7 trades —
      below the 15-trade bar `daily_lab.py` itself requires to call a result real. Not wired into the live Bot 4;
      revisit once more data exists. No live code changed.
- [~] "Learn from performing traders" research (2026-11-20): added `holy_grail` (Raschke/Connors ADX-pullback)
      and `vol_breakout` (Williams-style open +/- 0.5x prior-range stop) to `daily_lab.py`, ran full universe.
      `holy_grail`: no edge, negative pooled expectancy in every asset class, zero markets pass - matches the
      research caveat that a high ADX reading describes strength already spent, not what comes next.
      `vol_breakout`: real edge, NOT universal - passes on BRENTCash, OILCash, and ETHUSD with solid sample
      sizes (test_n 115-200, well above the 15-trade bar), but pooled expectancy is flat-to-negative on fx/
      index/metal and value-destroying on stocks (avg_expR -0.207). ETHUSD is the actionable one - a market
      we already trade (Bot 4), but a different cadence (5-day max hold, ~48 trades/yr vs Bot 4's ~4/yr), so
      it would be a new/separate bot, not a Bot 4 tweak. Not built yet - awaiting go-ahead. No live code changed.
      Stress-tested 2026-11-20: ETHUSD holds up across k=0.3-1.0 (PF 1.18-1.41 throughout, never flips) and
      across all 4 sequential time-windows since 2017 (never negative, PF 1.06-1.56) - genuinely robust, not a
      lucky parameter pick. BRENTCash/OILCash downgraded on the same test: both flip to losing (PF<1) at
      higher k (Brent k>=0.8, Oil k>=0.6) and Brent had one clearly losing multi-year window (2020-07..2022-08,
      PF 0.68) - fragile, and moot anyway since no live bot trades energy. ETHUSD is the only candidate worth
      building on.
- [x] Opportunity scan (2026-11-21, Adrian's go-ahead): re-filtered `data/lab/d1_results.csv` (770 symbol x
      strategy rows, full XM universe, strict pass bar) - only 14 rows pass. 2 were config-only additions to
      already-running bots, approved and shipped same session, **zero new code**:
      - Bot 3 (Dip): `CA60Cash`, `SA40Cash` added (dip_long test_PF 2.74-2.84; caveat: only 6.3-6.6yr history,
        thinner than the original 3 markets)
      - Bot 5 (VBO): `BRENTCash`, `OILCash` added (vol_breakout test_PF 1.25-1.50, test_n 115-200 - the same
        candidates flagged 2026-11-20 above, now actually wired in)
      - Bot 4 (Trend): `JP225Cash`, `Netflix` added (plain `breakout` strategy, same engine already running
        BTC/ETH; JP225Cash is the weakest pass in the whole set - test win% only 31%, worth re-checking after
        it accumulates trades)
      - Bot 6 (CBO): `Nvidia` added (breakout_candle, test_PF 1.68). Deliberately NOT also added to Trend's
        breakout (Nvidia passed both, but breakout_candle is a subset of the same signal - running both would
        double-size the same trade on the days they coincide)
      - All 4 symbol lists are just `.env` market-list edits (`XM_DIP_MARKETS` / `XM_VBO_MARKETS` /
        `XM_TREND_MARKETS` / `XM_CBO_MARKETS`); confirmed every new symbol is tradable on this MT5 account first
- [x] Account-wide risk breaker (2026-11-21, `src/trading_signal_bot/risk_guard.py`): all 6 `DemoTrader`
      instances share one MT5 account but previously had no shared risk check - theoretical worst case (every
      symbol across every bot firing at once) was already ~13% of balance before today's symbol additions, well
      past the 5% ceiling Adrian approved. Rather than cut every bot's already-running per-trade risk% (a real
      behavior change with no incident behind it - max ever seen open at once is 2 positions), added a
      breaker: `breaker_tripped()` sums `risk_money` for every still-open trade across all 6 bots'
      `data/*/trades.json` files; if that total hits `XM_RISK_CAP_PCT` (5, one shared value in `.env`) of
      balance, **every** bot skips new entries (existing positions untouched) until it drops. Wired into
      `trader.py::_on_signal` (one insertion point - all 6 bots share `DemoTrader`), not into each bot
      individually. 12 new unit tests (`test_risk_guard.py` + 2 in `test_trader.py`), 189 total, all green.
- [x] W7 web reporter (2026-11-21, `src/trading_signal_bot/web_report.py`, for the xm-web dashboard): `DemoTrader`
      gained an `on_closed` callback (the only point where a closed trade's exit price/P&L/reason exist -
      `trades.json` only ever holds OPEN positions, a closed one is removed, not archived). `WebTradeReporter`
      pushes the account snapshot + every open position every 60s (self-healing - a failed push just resends
      current state next cycle) and buffers closed trades, retrying until the website confirms them (capped at
      100 so a long outage can't grow memory unbounded). OFF by default per bot process; enabled by setting
      that bot's own token (`XM_DIP_WEB_TOKEN` / `XM_TREND_WEB_TOKEN` / `XM_VBO_WEB_TOKEN` / `XM_CBO_WEB_TOKEN`;
      main bot reuses `XM_SETTINGS_TOKEN`, M1 the same var in its own `.env.m1`). Wired into all 6 bot
      processes' `main()`. 7 new unit tests (`test_web_report.py` + 1 in `test_trader.py`), 210 total, all green,
      ruff + mypy clean. **LIVE as of 2026-11-21:** Adrian ran `create-strategy-bots.yml` against prod (5 new
      Bot rows + tokens for M1/Dip/Trend/VBO/CBO); tokens pasted into `.env`/`.env.m1`; all 6 bots restarted
      and confirmed reporting with zero errors across a full 60s cycle. The dashboard at
      pogingkita.stayatdiez.com/dashboard is receiving real account/trade data from all 6 bots.
- [x] **Code review (2026-11-21, `code-review` skill at `high` + manual pass) found and fixed 3 real bugs,
      all pushed + live** (`d7a3605`):
      1. `trader.py::_save_state()` now uses `atomic_write_json()` instead of a plain truncate-then-write.
         `risk_guard.py` reads this same file from OTHER bot processes concurrently; a half-written file was
         silently treated as zero risk (`except (OSError, ValueError): continue`), which could let the
         account-wide breaker fail to trip exactly when a write collided with a check.
      2. `cbo_bot.py::_daily()`'s raw MT5 fetch was sized only for the signal lookback (`entry_days+40`), not
         for the 120-day time stop; after the weekday filter removed weekends the window shrank to ~110 bars,
         so `bars_in_trade` could never reach `max_days=120` - the documented time-stop could never fire. Now
         sized off `max(entry_days+40, max_days+20)` converted to calendar days. Regression test reproduces
         this with a real 400-day calendar feed (weekends included, unlike the usual weekday-only test fixture).
      3. `vbo_bot.py`'s entry check used if/elif, so a quote where ask was past the upper breakout level AND
         bid was past the lower level at once (e.g. a gap) always resolved to BUY - contradicting the
         backtest's own ambiguous-bar skip rule (`research/daily_lab.py::simulate_vbo`). Now explicitly
         skipped and logged when both are touched at once; a later unambiguous touch still trades normally.
      Also found but NOT in scope of this fix (pre-existing, unrelated to this session's new code): an M1-only
      ATR/risk-context mismatch in `strategy.py`, a settings-candidate-file race in `remote_settings.py`, a
      missing `poll_closed()` call in `dip_bot.py`'s same-bar reversal path, and a latent `_position_ticket`
      fallback bug in `trader.py` (unreachable under every currently-shipped config). Not fixed - flagged only.
      212 tests pass (3 new), ruff + mypy clean. All 6 live bots restarted onto this commit.
- [x] Momentum Burst / Episodic Pivot research (2026-11-24, Adrian asked after seeing a trading-education
      reel naming Pradeep Bonde/"StockBee"): new `research/momentum_burst_lab.py`, same train/test discipline,
      tested against the real 40-stock universe (volume = MT5 tick_volume, a proxy, not real share volume -
      flagged as a caveat, not a confirmation). Momentum Burst (4%+ day on 1.5x+ volume, trend-filtered):
      **rejected** - 879 trades pooled, avg expR -0.265, 0/40 markets pass; single-stock CFD spread (48-246bp
      round trip, measured directly this run) eats the edge. Episodic Pivot (10%+ gap on 2x+ volume after a
      quiet base): **inconclusive, not validated** - the setup is too rare to generate a statistically
      meaningful sample (0.1-0.3 qualifying events/year/stock). No bot built. Pushed to the fork (`5ac04c2`).
- [x] **Bots 1+2 formally retired 2026-11-24** (see entries above) - full process trees stopped, not just
      paused; `TRADING_PAUSED` kill files left in place as a second guard.
- [x] Full-universe rescan (2026-11-24, re-ran `daily_lab.py run` against the existing cached fetch to check
      for any stock/symbol passing the strict bar that isn't already wired into a live bot): **none found** -
      every symbol on the PASSED list (BTCUSD/JP225Cash/Nvidia/Netflix on `breakout`, GOLD/Nvidia on
      `breakout_candle`, CA60Cash/SA40Cash/US100Cash/GBPUSD/US500Cash on `dip_long`, BRENTCash/ETHUSD/OILCash
      on `vol_breakout`) is already running in Bots 3-6. This rescan also corrects the 2026-11-21 entry above:
      Netflix was never actually the weak one - its `breakout` numbers (test_PF 1.61, test_n 17) are solid,
      second only to BTC; JP225Cash remains the thin pass (train_PF only 1.15, test_n 16) and is the one
      actually worth re-checking as it accumulates live trades.
- [x] Checked Anthropic (`Anthropic`, Nasdaq ANT.OQ) after Adrian asked to watch it - **confirmed newly
      tradable on this MT5 account** (IPO'd ~2026-11-19), but has only days of price history. Every strategy
      here needs 200+ daily bars just for the trend filter, so it **cannot be backtested or traded yet** -
      not a rejection, just too early. Revisit once it has a few months of history.

### QA
- [x] Functional tests passed: 212 unit tests + integration/replay, all green as of 2026-11-21
- [x] Edge cases tested per bot: demo-account refusal, kill-file pause, daily-loss limit, one-position-per-symbol,
      time stops, exit-rule parity with backtest, late-entry skip, reversal (bot 4), account-wide risk breaker
      (tripped-blocks-entry and cap-disabled-at-zero cases, isolated from the real on-disk `data/*/trades.json`)
- [~] Performance / capacity — N/A in the traditional sense (no concurrent users); capacity = MT5 polling every
      15-30s per bot, six processes on one PC. Not measured under load; low risk at this scale.
- [ ] Mobile / cross-browser / accessibility — N/A, no UI in this repo (settings UI lives in `xm-web`)
- [x] Trade-lifecycle messages verified: Telegram SIGNAL / TRADE OPENED / TRADE CLOSED (with reason) / CLOSE FAILED
- [~] Professional build audit — informal only, done inline per bot as built; no standalone audit doc written

### Security
- [x] Demo-only guard: `DemoTrader._is_demo()` refuses to trade if the connected account isn't DEMO
      (proven fail-then-pass: real-account test in `test_dip_bot.py` / `test_trend_bot.py`)
- [x] Kill file per bot (`data/<bot>/TRADING_PAUSED`) stops new trades instantly; checked every loop
- [x] Daily loss limit per bot (env-configured %, Manila day boundary) blocks new entries once hit
- [x] Account-wide risk breaker (`risk_guard.py`, 2026-11-21): total open risk across all 6 bots capped at
      `XM_RISK_CAP_PCT` (5%) of balance; new entries pause account-wide (not per-bot) above the cap, existing
      positions never touched. A "realistic cap" by design, not a literal worst-case cap - see Opportunity scan
      entry above for why (today's per-trade risk %s were left as-is; the breaker is the safety net, not a
      per-trade cut)
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
- [ ] Crypto (BTC/ETH on bots 4 and 5) trades weekends; the PC/MT5 must stay on through Sat/Sun or signals are
      missed (known gap, not yet solved — VPS move would fix it). Bot 4's other new markets (JP225Cash,
      Netflix) are weekday-only like bots 3/6, so this gap doesn't widen with today's additions

### Business logic & edge cases
- [x] One position per symbol per bot (own magic number keeps bots from colliding on the same symbol)
- [x] Exit rules proven to match the backtest exactly (parity tests in `test_dip_bot.py` / `test_trend_bot.py`)
- [ ] Review each bot's live performance at 20+ closed trades before any real-money decision (standing rule)

### Final
- [ ] Final approval — N/A, ongoing demo research project; no "launch" milestone, Adrian decides go-live per bot
