"""Power of 2 (Set 2) - EXPERIMENTAL FORWARD-TEST bot, NOT a production bot.

Adrian explicitly asked (2026-11-28) to run Power of 2's research live on the demo account
rather than trust the backtest alone, after pushing back on the all-negative research verdict.
Of the 14 day-trading archetypes tested in research/power_of_2_lab.py (277 strategy x market x
timeframe combos across a widened FX/crypto/index universe, M15 and M5), these 3 are the only
ones with any positive signal at all - and two of them only marginally cleared this project's
normal train+test bar:

  bb_breakout                NAS100 M15  train PF 1.01 / test PF 1.07 / ~0.37 trades/day (passed, barely)
  session_overlap_momentum   GOLD   M15  train PF 1.01 / test PF 1.02 / ~0.89 trades/day (passed, barely)
  donchian_scalp              NAS100 M15  train PF 0.95 / test PF 1.03 / ~2.88 trades/day (did NOT clear
                                                                           train - included for live
                                                                           signal only, highest frequency)

Entry/stop/target logic is ported 1:1 from research/power_of_2_lab.py (sig_bb_breakout,
sig_session_overlap_momentum, sig_donchian_scalp, plus the same ATR(14)-stop / RR-target model
strategy_lab.simulate() used) - zero logic drift from what was actually backtested. This file
does not reimplement or retune any of it.

Risk is tiny (default 0.1% of balance per trade, see XM_P2_RISK_PCT) and this bot has its OWN
risk breaker (XM_P2_RISK_CAP_PCT, default 3%), completely separate from Set 1 "Long Waiting"'s
shared 5% cap (risk_guard.DEFAULT_STATE_FILES) - per Adrian's explicit scope decision earlier
this session. Alerts go to Adrian's PERSONAL Telegram chat (XM_P2_CHAT_ID), never the shared
group Long Waiting uses.

Past results do not guarantee future results - two of three strategies here did not clear this
project's normal robustness bar before being deployed; this is a live validation experiment,
not a vote of confidence. This runs on the DEMO account only (DemoTrader enforces it).

    poetry run python -m trading_signal_bot.p2_forwardtest_bot   (reads .env; see .env.example XM_P2_*)
Pause trading instantly: create data/p2_forward_test/TRADING_PAUSED
"""

from __future__ import annotations

import argparse
import logging
import os
import signal as os_signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.trader import DemoTrader, TradeConfig

_RESEARCH_DIR = Path(__file__).resolve().parents[2] / "research"
sys.path.insert(0, str(_RESEARCH_DIR))
from power_of_2_lab import (  # noqa: E402
    sig_bb_breakout,
    sig_donchian_scalp,
    sig_session_overlap_momentum,
)
from strategy_lab import atr  # noqa: E402

LOG = logging.getLogger("P2ForwardTestBot")

STATE_DIR = Path("data/p2_forward_test")
RISK_STATE_FILES = (
    STATE_DIR / "bb_breakout_trades.json",
    STATE_DIR / "session_overlap_trades.json",
    STATE_DIR / "donchian_scalp_trades.json",
)
MIN_WARMUP_BARS = 220
FETCH_BARS = 300


@dataclass(frozen=True)
class P2Strategy:
    name: str
    scenario: Scenario
    market_label: str
    broker_symbol: str
    sig_fn: Callable[[pd.DataFrame, dict[str, Any]], Any]
    params: dict[str, Any]
    sl_atr: float
    rr: float
    max_hold_bars: int
    magic: int
    telegram_label: str
    expected_per_day: float


# Params/sl_atr/rr/max_hold are the exact winning combo research/power_of_2_lab.py found for
# each (strategy, market) pair - see data/lab/power_of_2_results_M15.json.
STRATEGIES: tuple[P2Strategy, ...] = (
    P2Strategy(
        name="bb_breakout",
        scenario=Scenario.P2_BB_BREAKOUT,
        market_label="NAS100",
        broker_symbol="US100Cash",
        sig_fn=sig_bb_breakout,
        params={"k": 2.0, "lookback": 100},
        sl_atr=1.5,
        rr=2.0,
        max_hold_bars=24,
        magic=570040,
        telegram_label="[P2-BB]",
        expected_per_day=0.37,
    ),
    P2Strategy(
        name="session_overlap_momentum",
        scenario=Scenario.P2_SESSION_OVERLAP,
        market_label="GOLD",
        broker_symbol="GOLD",
        sig_fn=sig_session_overlap_momentum,
        params={},
        sl_atr=2.0,
        rr=2.0,
        max_hold_bars=12,
        magic=570041,
        telegram_label="[P2-SOM]",
        expected_per_day=0.89,
    ),
    P2Strategy(
        name="donchian_scalp",
        scenario=Scenario.P2_DONCHIAN_SCALP,
        market_label="NAS100",
        broker_symbol="US100Cash",
        sig_fn=sig_donchian_scalp,
        params={"n": 20},
        sl_atr=2.0,
        rr=2.0,
        max_hold_bars=20,
        magic=570042,
        telegram_label="[P2-DONCH]",
        expected_per_day=2.88,
    ),
)


def _num(env: Any, name: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    value = float(str(raw))
    if not (lo <= value <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def load_p2_trade_config(env: Any, strat: P2Strategy) -> TradeConfig:
    enabled = str(env.get("XM_P2_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    return TradeConfig(
        enabled=enabled,
        symbols=(strat.market_label,),
        risk_pct=_num(env, "XM_P2_RISK_PCT", 0.1, 0.02, 1.0),
        rr=strat.rr,
        daily_loss_pct=_num(env, "XM_P2_DAILY_LOSS_PCT", 2.0, 0.5, 10.0),
        max_per_symbol=1,
        kill_file=Path(env.get("XM_P2_KILL_FILE") or str(STATE_DIR / "TRADING_PAUSED")),
        state_file=STATE_DIR / f"{strat.name}_trades.json",
        magic=strat.magic,
        scenarios=frozenset({strat.scenario}),
        label=strat.telegram_label,
        risk_cap_pct=_num(env, "XM_P2_RISK_CAP_PCT", 3.0, 0.0, 100.0),
        risk_state_files=RISK_STATE_FILES,
    )


@dataclass
class P2ForwardTestBot:
    traders: dict[str, DemoTrader]
    mt5: Any
    _last_bar: dict[str, pd.Timestamp] = field(default_factory=dict)

    def step(self) -> None:
        bars_by_market: dict[str, pd.DataFrame | None] = {}
        for strat in STRATEGIES:
            trader = self.traders[strat.name]
            trader.poll_closed()
            self._time_exit(strat, trader)

            if strat.market_label not in bars_by_market:
                bars_by_market[strat.market_label] = self._closed_bars(strat.broker_symbol)
            df = bars_by_market[strat.market_label]
            if df is None or len(df) < MIN_WARMUP_BARS:
                continue

            last_time = df.time.iloc[-1]
            if self._last_bar.get(strat.name) == last_time:
                continue  # already evaluated this closed bar for this strategy
            self._last_bar[strat.name] = last_time

            sig = strat.sig_fn(df, strat.params)
            direction = int(sig[-1])
            if direction == 0:
                continue
            if trader.open_trades():
                LOG.info("%s: signal fired but a position is already open, skipping", strat.name)
                continue
            self._enter(strat, trader, df, direction)

    def _closed_bars(self, broker: str) -> pd.DataFrame | None:
        rates = self.mt5.copy_rates_from_pos(broker, self.mt5.TIMEFRAME_M15, 0, FETCH_BARS)
        if rates is None or len(rates) < 2:
            return None
        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close"]]
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.iloc[:-1].reset_index(drop=True)  # drop the still-forming bar

    def _time_exit(self, strat: P2Strategy, trader: DemoTrader) -> None:
        now = datetime.now(timezone.utc)
        max_age = timedelta(minutes=15 * strat.max_hold_bars)
        for t in trader.open_trades():
            opened = datetime.fromisoformat(t.opened_utc)
            if now - opened >= max_age:
                trader.close_position(t.ticket, f"time exit: {strat.max_hold_bars} bars")

    def _enter(self, strat: P2Strategy, trader: DemoTrader, df: pd.DataFrame, direction: int) -> None:
        atr_val = float(atr(df, 14).iloc[-1])
        if not (atr_val > 0):
            LOG.warning("%s: ATR unavailable, skipping signal", strat.name)
            return
        dist = strat.sl_atr * atr_val
        tick = self.mt5.symbol_info_tick(strat.broker_symbol)
        if tick is None:
            return
        now = datetime.now(timezone.utc)
        price = float(tick.ask if direction == 1 else tick.bid)
        LOG.info(
            "%s signal %s %s price=%.2f atr=%.4f dist=%.4f",
            strat.name, strat.market_label, "BUY" if direction == 1 else "SELL", price, atr_val, dist,
        )
        trader.on_signal(
            Signal(
                id=Signal.new_id(),
                symbol=strat.market_label,
                direction=Direction.BUY if direction == 1 else Direction.SELL,
                scenario=strat.scenario,
                price=price,
                created_at_utc=now,
                m1_bar_time_utc=now,
                risk_stop_distance=dist,
            )
        )


def main() -> None:
    from trading_signal_bot.mt5_client import _load_mt5_module
    from trading_signal_bot.settings import load_secrets
    from trading_signal_bot.telegram_notifier import TelegramNotifier
    from trading_signal_bot.utils import setup_logging, single_instance_lock

    ap = argparse.ArgumentParser(description="Power of 2 forward-test bot (experimental, demo only)")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--lock-file", type=Path, default=STATE_DIR / "bot.lock")
    ap.add_argument("--poll-seconds", type=float, default=20.0)
    args = ap.parse_args()

    secrets = load_secrets(args.env)
    setup_logging("INFO", Path("logs/p2_forwardtest_bot.log"), 5_000_000, 5, timezone="Asia/Manila")

    chat_id = os.environ.get("XM_P2_CHAT_ID", "").strip()
    if not chat_id:
        raise SystemExit("XM_P2_CHAT_ID is not set - refusing to start without a Telegram destination")

    mt5 = _load_mt5_module()
    if mt5 is None:
        raise SystemExit("MetaTrader5 package is unavailable in this environment")
    ok = mt5.initialize(path=secrets.mt5_terminal_path) if secrets.mt5_terminal_path else mt5.initialize()
    if not ok:
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    acct = mt5.account_info()
    if acct is None or int(acct.login) != secrets.mt5_login:
        raise SystemExit("MT5 terminal is not logged into the account in .env; refusing to start")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tg = TelegramNotifier(
        token=secrets.telegram_bot_token,
        chat_id=chat_id,
        failed_queue_file=STATE_DIR / "failed_messages.json",
    )

    traders: dict[str, DemoTrader] = {}
    for strat in STRATEGIES:
        trade_cfg = load_p2_trade_config(os.environ, strat)
        traders[strat.name] = DemoTrader(
            config=trade_cfg,
            mt5=mt5,
            resolve_symbol=lambda _s, strat=strat: strat.broker_symbol,
            notify=tg.send_text,
        )

    bot = P2ForwardTestBot(traders=traders, mt5=mt5)

    stop = {"flag": False}
    os_signal.signal(os_signal.SIGINT, lambda *_: stop.update(flag=True))
    with single_instance_lock(args.lock_file):
        any_cfg = next(iter(traders.values())).config
        LOG.warning(
            "P2 FORWARDTEST BOT started trading=%s strategies=%s risk=%s%% cap=%s%%",
            any_cfg.enabled, [s.name for s in STRATEGIES], any_cfg.risk_pct, any_cfg.risk_cap_pct,
        )
        tg.send_text(
            "[P2-FORWARDTEST] EXPERIMENTAL VALIDATION RUN started "
            f"(demo trading {'ON' if any_cfg.enabled else 'OFF'}, risk {any_cfg.risk_pct}% per trade, "
            f"own risk cap {any_cfg.risk_cap_pct}%, separate from Long Waiting).\n"
            "3 strategies from the Power of 2 research - NONE deployed with confidence. "
            "bb_breakout & session_overlap_momentum only marginally passed train+test (PF ~1.0-1.07). "
            "donchian_scalp did NOT pass train at all; included for live signal only, highest frequency.\n"
            "Expected frequency: "
            + ", ".join(f"{s.telegram_label} ~{s.expected_per_day}/day" for s in STRATEGIES)
            + ".\nThis validates research against live data - it is not a vote of confidence."
        )
        last_beat = 0.0
        while not stop["flag"]:
            if mt5.terminal_info() is None:
                LOG.error("MT5 connection lost; reconnecting")
                mt5.initialize(path=secrets.mt5_terminal_path) if secrets.mt5_terminal_path else mt5.initialize()
            bot.step()
            if time.time() - last_beat > 3600:
                open_counts = {name: len(t.open_trades()) for name, t in traders.items()}
                LOG.info("heartbeat ok open_trades=%s", open_counts)
                last_beat = time.time()
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
