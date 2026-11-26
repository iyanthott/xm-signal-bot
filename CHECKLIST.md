# CHECKLIST.md — trading-signal-bot ("Poging Kita" bots)
Living record per CLAUDE.md §7. Updated the same session work ships.
Last updated: 2026-11-26 PM (Bot 8 Open Gap Fade on NAS100 + its cash-close fix; dashboard "connected" check-in
for every strategy bot; Bot 8 wired to the dashboard; HF 10+/day research rejected - see entries below; 6 live bots).
Earlier 2026-11-26: Bot 7 Fresh-Breakout Dip added on CHFJPY/GBPJPY/EURJPY; Dip bot IBS entry filter on
US500/NAS100; `research/edge_lab.py` broad research rounds 1-3.
Previous: 2026-11-21 (account-wide risk breaker; 7 new symbols across bots 3/4/5/6; W7 web reporter LIVE
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
      Opportunity scan entry below - all config-only, zero new code). **2026-11-26: IBS entry filter** on
      US500+NAS100 only (`XM_DIP_IBS_FILTER`, default 0.35: signal bar must close in the bottom 35% of its
      range). Backtest US500 PF 1.47->1.79, NAS100 1.70->2.05, max DD ~-35%; plateau across 0.20-0.40, better
      in train AND test; hurts GBPUSD/CA60 so they stay unfiltered. Commit `1bcf282`.
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
- [x] Bot 7 — Fresh-Breakout Dip (magic 570037), `[FBD]`, CHFJPY+GBPJPY+EURJPY, `scripts/run_fbd_bot.ps1`
      (2026-11-26, commit `fa43129`). Rule: 20-day high within the last 10 bars + close > SMA50 + RSI(2) < 10 ->
      buy next open; exit next open after a close above the prior day's high, or 5 days; 2xATR stop; long only.
      Backtest 2001-2026 (3-pair basket): 292 trades, 68% win, PF 1.67, +1.38R/yr, max DD 6.1R, 18/26 yrs +;
      CHFJPY positive in both halves for all 18 nearby parameter sets. Caveat: AUD/CAD/NZD-JPY fail the same
      rule (selection risk); the 3 pairs often enter together (46 shared days) - account risk cap applies.
      Added to `risk_guard.DEFAULT_STATE_FILES` (all bots restarted so every process counts it) and to
      `weekly_report.py`. 9 new unit tests incl. live-vs-research signal parity. **Web dashboard wired
      2026-11-26:** FBD added to xm-web `create-strategy-bots.ts` (xm-web `24c68c9`), workflow run 45744718380
      created Bot "FBD" + token (other 5 bots untouched), token in `.env` as `XM_FBD_WEB_TOKEN` (never committed),
      Bot 7 restarted with `web_report=True`; token verified against prod (HTTP 200; bogus token 401).
- [x] Bot 8 — Open Gap Fade (magic 570038), `[GAP]`, NAS100 (US100Cash), `scripts/run_gap_bot.ps1`
      (2026-11-26). Rule: at 16:30 server (09:30 New York) compare the 16:30 M1 open with the previous session's
      23:00 server cash close (close of the 22:30 M30 bar); if |move| >= 0.75 x 14-day ATR%, trade AGAINST it at
      market within 120s of the open; 1xATR stop; flat at 19:00 server (no swap). Backtest (`edge_lab.py
      gap_fade`, NAS100 M30 2018-2026): 179 trades, ~52% win, PF 1.35 at 1.5x spread / 1.52 at 1x, train 1.32 /
      test 1.42, max DD 3.0R; edge sits in the first 30 min and is stronger since 2022 (2019/2021 lost). Every
      fill logged vs the 16:30 open in `data/gap/fills.csv` + Telegram (slippage in bp). In `risk_guard` +
      `weekly_report.py`. **Fix same day (before its first trade):** the bot first used the D1 close as "previous
      close" (last tick of the day, incl. ~1h of after-hours drift) - on 2018-2026 data that cut PF 1.52->1.30
      and changed 61 signal days; on live data 2026-11-26 the two differed by 0.4%. Now `prev_cash_close()`
      matches the backtest, and skips the day when the previous session closed early (as the backtest does).
      13 unit tests. **Dashboard:** GAP added to xm-web `create-strategy-bots.ts` (xm-web `8fa6389`), workflow
      run 45761832255 created Bot "GAP" + token (others untouched), token in `.env` as `XM_GAP_WEB_TOKEN`
      (never committed), Bot 8 running with `web_report=True`.
- [x] **Dashboard "Never connected" fix (2026-11-26):** the site's Online/last-seen badge is driven only by
      `POST /api/bot/status`, which only the retired settings bot ever sent - Bots 3-7 were reporting account +
      trades fine (all 5 tokens verified HTTP 200) but never checked in. `WebTradeReporter` now also posts a
      status check-in each 60s cycle, echoing the site's current settings version (read from
      `/api/bot/settings`, cached 10 min; strategy bots take rules from `.env`, so there is nothing to apply).
      Bot-side only, no xm-web deploy. 3 new/updated tests. All 6 bots restarted 13:30, zero website errors.
      Adrian can archive "Adrian's bot" and "M1" on the Bots page (retired 2026-11-24).
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
- [x] Weekly Telegram report (2026-11-24, Adrian asked after being told "give the fleet 2-3 weeks and
      I'll track every closed trade"): new `scripts/weekly_report.py` - pulls REAL closed trades from MT5
      `history_deals_get` (not the local `trades.json` files, which only ever hold open positions) for the
      last 7 days, grouped by bot via magic number (Bots 1+2's magics deliberately excluded - retired), computes
      win%/PF/net P&L per bot + total, sends as a plain-text message to the same Telegram chat the bots already
      alert to. Tested both as `--dry-run` (console only) and for real (confirmed delivered to Telegram) before
      scheduling. Registered as Windows Scheduled Task `XM-WeeklyReport`, weekly Monday 08:00 (the PC's system
      clock is already Singapore Standard Time = UTC+8 = Manila, no DST either side, so this needs no extra
      conversion). Runs `Interactive` logon as Adrian's own user - same requirement as the bots themselves
      (PC on, logged in, MT5 open). Next run: 2026-11-30 08:00.
- [x] **`cpds` 2026-11-24 (end of session):** 212 tests pass (unchanged - momentum_burst_lab.py is research
      tooling, no dedicated tests, same as daily_lab.py/strategy_lab.py). Fleet is now **4 live bots, not 6**
      (Dip/Trend/VBO/CBO; Bots 1+2 retired above) - all 4 confirmed healthy, recent heartbeats, zero errors.
      No deploy step for this repo (runs on Adrian's PC, not a hosted pipeline) and no live bot source changed
      this session, so nothing needed redeploying - the running processes are already correct. Both commits
      this session (`5ac04c2` momentum burst research, `ca1f7e1` this CHECKLIST + retirement) pushed to the
      fork (`feature/settings-editor`). xm-web untouched this session - no xm-web deploy needed.
- [x] Prior-day-range fade research (2026-11-25, `research/range_fade_lab.py`, commit `1b0a814`): Adrian's
      rule, 0/12 markets profitable blind; rejection-candle variant only US30 and parameter-fragile -> rejected,
      watchlist only. (Back-filled here 2026-11-26 - this entry was missing.)
- [x] Broad research, `research/edge_lab.py` (2026-11-26, Adrian: "create your own bots, no limit"). All fixed
      rules, real spread + swap, 70/30 train/test, then parameter grids / 4 time windows / overlap with live bots.
      **Rejected:** IBS standalone, turn-of-month (GOLD = drift, not beating random entry), NR7 / inside-day
      breakouts, Monday turnaround, US-index intraday momentum, Asian-range breakout, H1 intraday dip, stock gap
      fade/go, FX carry (credits 0.1-0.6bp/night), crypto intraday VBO, session/hour-of-day drift (GOLD 01:00 &
      crypto 00:00 = stale reopen-quote artifact), pairs mean-reversion (14 pairs), fresh-breakout-dip shorts,
      big-move fade. **Crypto big-move follow** (BTC/ETH, robust 18/18) rejected as a new bot because all of its
      profit sits in trades Bots 4/5 already hold. **Watchlist:** pre-holiday US indices; Halloween effect on
      European indices (GER40 winter +5.8%/yr vs summer +0.1%). **Built:** Dip IBS filter + Bot 7 (above).
- [x] **`cpds` 2026-11-26:** 225 tests pass, ruff + mypy clean. Commits `1bcf282` (Dip IBS filter) and
      `fa43129` (Bot 7 + research) pushed to the fork. "Deploy" = local processes: all 5 bots (Dip/Trend/VBO/
      CBO/FBD) restarted onto `fa43129` at 09:52-09:53, heartbeats ok, zero errors, open trades reloaded
      (Trend 1, VBO 2). CI only runs on `main`/PRs, so no CI run for this branch. xm-web untouched.
- [x] Research round 4 + gap fade (2026-11-26, `edge_lab.py round4` / `robust_gap`): open-gap fade on US
      indices -> NAS100 only robust -> **built as Bot 8** (above).
- [x] High-frequency feasibility (2026-11-26, Adrian wants 10+ trades/day; new `research/hf_lab.py`): 6 classic
      scalping families (EMA cross, Bollinger revert, Donchian, TWAP revert, RSI2 trend, big-bar fade) on GOLD +
      NAS100, M5 + M15, settings tuned on train only. **Rejected: 0/24 positive even in train** (best train PF
      1.02); the only 10+/day candidate (GOLD M5 TWAP revert, 10.7/day) PF 0.84 train / 0.78 test. On this
      account's spreads, 10+/day is not viable; the only lever left is cheaper costs (XM raw-spread account).
- [x] **`cpds` 2026-11-26 PM:** 240 tests pass, ruff clean on src+tests; mypy: 16 errors, all pre-existing in
      `strategy.py` (retired Bot 1/2 engine, untouched) - none in new code. Bot 8 + fix, status check-in, HF and
      round-4 research committed and pushed to the fork. "Deploy" = local: all 6 bots restarted 13:30, heartbeats
      ok, zero errors, `web_report=True` on all 6.

### QA
- [x] Functional tests passed: 240 tests (unit + integration/replay), all green as of 2026-11-26 PM
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
