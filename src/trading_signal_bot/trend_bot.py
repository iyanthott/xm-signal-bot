"""Bot 4 - Crypto Trend: ride big daily trends in Bitcoin and Ethereum (Donchian channel breakout).

Rules (exactly what research/daily_lab.py `breakout` backtested on BTCUSD + ETHUSD, net of spread and swap):
  entry : daily close above the previous 55-day high -> buy at the next day's open
          daily close below the previous 55-day low   -> sell at the next day's open
  stop  : 2 x ATR(14) of the daily bars (broker-side stop loss)
  exit  : long  - first daily close below the previous 20-day low  -> close at the next open
          short - first daily close above the previous 20-day high -> close at the next open
          or after 120 daily bars in the trade (time stop)
  one position per market; crypto trades 7 days a week, so weekend bars count

Backtest since 2022 (BTC + ETH): 51 trades, 39% win, PF 1.75, +0.39R per trade, about 10 trades a year.
Expect more losing trades than winners; the profit comes from a few large trends.
Past results do not guarantee future results; this runs on the DEMO account only (DemoTrader enforces it).

    poetry run python -m trading_signal_bot.trend_bot          (reads .env; see .env.example XM_TREND_*)
Pause trading instantly: create data/trend/TRADING_PAUSED
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal as os_signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trading_signal_bot.dip_bot import _atr
from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.trader import DemoTrader, TradeConfig

TREND_MAGIC = 570034
LOG = logging.getLogger("TrendBot")


# ---------------------------------------------------------------- config
@dataclass(frozen=True)
class TrendConfig:
    markets: dict[str, str] = field(default_factory=lambda: {"BTC": "BTCUSD", "ETH": "ETHUSD"})
    entry_days: int = 55
    exit_days: int = 20
    stop_atr: float = 2.0
    max_days: int = 120
    long_only: bool = False
    entry_window_hours: float = 4.0  # only enter if the new day's bar is younger than this
    state_file: Path = Path("data/trend/trend_state.json")


def _flag(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def load_trend_config(env: Mapping[str, str | None]) -> tuple[TrendConfig, TradeConfig]:
    def num(name: str, default: float, lo: float, hi: float) -> float:
        raw = env.get(name)
        if raw in (None, ""):
            return default
        try:
            value = float(str(raw))
        except ValueError as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not (lo <= value <= hi):
            raise ValueError(f"{name} must be between {lo} and {hi}")
        return value

    markets: dict[str, str] = {}
    for part in (env.get("XM_TREND_MARKETS") or "BTC=BTCUSD,ETH=ETHUSD").split(","):
        label, _, broker = part.strip().partition("=")
        if label.strip():
            markets[label.strip()] = (broker or label).strip()
    entry_days = int(num("XM_TREND_ENTRY_DAYS", 55, 10, 200))
    exit_days = int(num("XM_TREND_EXIT_DAYS", 20, 5, 100))
    if exit_days >= entry_days:
        raise ValueError("XM_TREND_EXIT_DAYS must be shorter than XM_TREND_ENTRY_DAYS")
    trend = TrendConfig(
        markets=markets,
        entry_days=entry_days,
        exit_days=exit_days,
        stop_atr=num("XM_TREND_STOP_ATR", 2.0, 0.5, 6.0),
        max_days=int(num("XM_TREND_MAX_DAYS", 120, 5, 365)),
        long_only=_flag(env.get("XM_TREND_LONG_ONLY")),
    )
    trade = TradeConfig(
        enabled=_flag(env.get("XM_TREND_ENABLED")),
        symbols=tuple(markets),
        risk_pct=num("XM_TREND_RISK_PCT", 0.5, 0.05, 2.0),
        rr=0.0,  # no take-profit; exits by rule
        daily_loss_pct=num("XM_TREND_DAILY_LOSS_PCT", 2.0, 0.5, 10.0),
        kill_file=Path(env.get("XM_TREND_KILL_FILE") or "data/trend/TRADING_PAUSED"),
        state_file=Path(env.get("XM_TREND_STATE_FILE") or "data/trend/trades.json"),
        magic=int(num("XM_TREND_MAGIC", TREND_MAGIC, 1, 2_000_000_000)),
        scenarios=frozenset({Scenario.TREND_BREAKOUT}),
        label="[TREND]",
        risk_cap_pct=num("XM_RISK_CAP_PCT", 5.0, 0.0, 100.0),
    )
    return trend, trade


# ---------------------------------------------------------------- rules (same formulas as the backtest)
def _channels(daily: pd.DataFrame, entry_days: int, exit_days: int) -> dict[str, pd.Series]:
    """Channel levels of the bars BEFORE each bar (shifted), as in research/daily_lab.py."""
    return {
        "hi_entry": daily.high.rolling(entry_days).max().shift(),
        "lo_entry": daily.low.rolling(entry_days).min().shift(),
        "hi_exit": daily.high.rolling(exit_days).max().shift(),
        "lo_exit": daily.low.rolling(exit_days).min().shift(),
    }


def breakout_signal(daily: pd.DataFrame, entry_days: int = 55, exit_days: int = 20) -> np.ndarray:
    """+1 (buy) / -1 (sell) / 0 on each closed daily bar."""
    ch = _channels(daily, entry_days, exit_days)
    c = daily.close
    return np.where(c > ch["hi_entry"], 1, np.where(c < ch["lo_entry"], -1, 0))


def exit_signal(daily: pd.DataFrame, direction: Direction, entry_days: int, exit_days: int) -> bool:
    """True when the latest closed bar closed through the opposite exit channel."""
    ch = _channels(daily, entry_days, exit_days)
    c = float(daily.close.iloc[-1])
    if direction is Direction.BUY:
        return bool(c < float(ch["lo_exit"].iloc[-1]))
    return bool(c > float(ch["hi_exit"].iloc[-1]))


# ---------------------------------------------------------------- bot
@dataclass
class TrendBot:
    config: TrendConfig
    trader: DemoTrader
    mt5: Any
    notify: Callable[[str], bool]
    _state: dict[str, Any] = field(default_factory=dict)
    _last_tick: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            self._state = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._state = {}
        self._state.setdefault("last_bar", {})  # label -> last closed daily bar processed
        self._state.setdefault("entry_bar", {})  # ticket -> daily bar the trade was entered on

    def step(self) -> None:
        self.trader.poll_closed()
        for label, broker in self.config.markets.items():
            try:
                self._step_market(label, broker)
            except Exception:
                LOG.exception("trend step failed for %s", label)

    def _daily(self, broker: str) -> pd.DataFrame | None:
        self.mt5.symbol_select(broker, True)
        need = self.config.entry_days + 40
        rates = self.mt5.copy_rates_from_pos(broker, self.mt5.TIMEFRAME_D1, 0, need + 60)
        if rates is None or len(rates) < need:
            return None
        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close"]]
        df["time"] = pd.to_datetime(df["time"], unit="s")  # server time; all 7 days kept
        return df

    def _step_market(self, label: str, broker: str) -> None:
        df = self._daily(broker)
        tick = self.mt5.symbol_info_tick(broker)
        if df is None or tick is None:
            return
        forming, closed = df.iloc[-1], df.iloc[:-1].reset_index(drop=True)
        server_now = pd.to_datetime(int(tick.time), unit="s")
        prev_tick = self._last_tick.get(label)
        self._last_tick[label] = int(tick.time)
        if prev_tick is None or int(tick.time) <= prev_tick or server_now < forming.time:
            return  # act only on a live, moving quote
        last_closed = str(closed.time.iloc[-1])
        if self._state["last_bar"].get(label) == last_closed:
            return
        cfg = self.config

        # --- exits first (rule evaluated on the bar that just closed; we are at the next open)
        closed_any = False
        for t in [t for t in self.trader.open_trades() if t.symbol == label]:
            entry_bar = self._state["entry_bar"].get(str(t.ticket))
            bars_in_trade = int((closed.time >= pd.Timestamp(entry_bar)).sum()) if entry_bar else 0
            reason = None
            if exit_signal(closed, Direction(t.direction), cfg.entry_days, cfg.exit_days):
                side = "low" if t.direction == Direction.BUY.value else "high"
                reason = f"exit rule: closed through the {cfg.exit_days}-day {side}"
            elif bars_in_trade >= cfg.max_days:
                reason = f"time stop: {bars_in_trade} days in the trade"
            if reason:
                if not self.trader.close_position(t.ticket, reason):
                    return  # retry next pass; do not mark the bar as processed
                closed_any = True
        if closed_any:
            self.trader.poll_closed()  # report the close now so a reversal can enter this bar

        # --- entry
        sig = int(breakout_signal(closed, cfg.entry_days, cfg.exit_days)[-1])
        if cfg.long_only and sig < 0:
            sig = 0
        if sig and not any(t.symbol == label for t in self.trader.open_trades()):
            bar_age_h = (server_now - forming.time).total_seconds() / 3600
            if bar_age_h <= cfg.entry_window_hours:
                self._enter(label, closed, tick, sig, forming.time)
            else:
                LOG.info(
                    "trend signal on %s but the day started %.1fh ago; skipping late entry",
                    label,
                    bar_age_h,
                )

        self._state["last_bar"][label] = last_closed
        self._save()

    def _enter(
        self, label: str, closed: pd.DataFrame, tick: Any, sig: int, entry_bar_time: pd.Timestamp
    ) -> None:
        cfg = self.config
        direction = Direction.BUY if sig > 0 else Direction.SELL
        ch = _channels(closed, cfg.entry_days, cfg.exit_days)
        dist = cfg.stop_atr * float(_atr(closed).iloc[-1])
        close = float(closed.close.iloc[-1])
        if direction is Direction.BUY:
            why = f"close {close:.2f} above the {cfg.entry_days}-day high {float(ch['hi_entry'].iloc[-1]):.2f}"
            exit_txt = f"first close below the {cfg.exit_days}-day low"
        else:
            why = f"close {close:.2f} below the {cfg.entry_days}-day low {float(ch['lo_entry'].iloc[-1]):.2f}"
            exit_txt = f"first close above the {cfg.exit_days}-day high"
        now = datetime.now(timezone.utc)
        self.notify(
            f"[TREND] SIGNAL {label} {direction.value} (daily trend breakout)\n"
            f"{why}\n"
            f"Plan: stop {cfg.stop_atr:g}x daily ATR ({dist:.2f}); exit on {exit_txt} "
            f"or after {cfg.max_days} days. Expect many small losses and a few big wins."
        )
        before = {t.ticket for t in self.trader.open_trades()}
        self.trader.on_signal(
            Signal(
                id=Signal.new_id(),
                symbol=label,
                direction=direction,
                scenario=Scenario.TREND_BREAKOUT,
                price=float(tick.ask if direction is Direction.BUY else tick.bid),
                created_at_utc=now,
                m1_bar_time_utc=now,
                risk_stop_distance=dist,
            )
        )
        for t in self.trader.open_trades():
            if t.ticket not in before:
                self._state["entry_bar"][str(t.ticket)] = str(entry_bar_time)

    def _save(self) -> None:
        live = {str(t.ticket) for t in self.trader.open_trades()}
        self._state["entry_bar"] = {k: v for k, v in self._state["entry_bar"].items() if k in live}
        try:
            self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.config.state_file.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        except OSError:
            LOG.exception("could not save trend state")


# ---------------------------------------------------------------- process entry point
def main() -> None:
    from trading_signal_bot.mt5_client import _load_mt5_module
    from trading_signal_bot.settings import load_secrets
    from trading_signal_bot.telegram_notifier import TelegramNotifier
    from trading_signal_bot.utils import setup_logging, single_instance_lock

    ap = argparse.ArgumentParser(description="Bot 4 - Crypto Trend (demo only)")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--lock-file", type=Path, default=Path("data/trend/bot.lock"))
    ap.add_argument("--poll-seconds", type=float, default=30.0)
    args = ap.parse_args()

    secrets = load_secrets(args.env)
    setup_logging("INFO", Path("logs/trend_bot.log"), 5_000_000, 5, timezone="Asia/Manila")
    trend_cfg, trade_cfg = load_trend_config(os.environ)

    mt5 = _load_mt5_module()
    if mt5 is None:
        raise SystemExit("MetaTrader5 package is unavailable in this environment")

    def connect() -> bool:
        return bool(
            mt5.initialize(path=secrets.mt5_terminal_path)
            if secrets.mt5_terminal_path
            else mt5.initialize()
        )

    if not connect():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    acct = mt5.account_info()
    if acct is None or int(acct.login) != secrets.mt5_login:
        raise SystemExit("MT5 terminal is not logged into the account in .env; refusing to start")

    tg = TelegramNotifier(
        token=secrets.telegram_bot_token,
        chat_id=secrets.telegram_chat_id,
        failed_queue_file=Path("data/trend/failed_messages.json"),
    )
    from trading_signal_bot.web_report import WebTradeReporter, load_web_report_config

    report_cfg = load_web_report_config(os.environ, "XM_TREND_WEB_TOKEN")
    reporter = WebTradeReporter(report_cfg) if report_cfg else None
    trader = DemoTrader(
        trade_cfg, mt5, lambda s: trend_cfg.markets.get(s, s), tg.send_text,
        on_closed=reporter.on_closed if reporter else None,
    )
    bot = TrendBot(trend_cfg, trader, mt5, tg.send_text)

    stop = {"flag": False}
    os_signal.signal(os_signal.SIGINT, lambda *_: stop.update(flag=True))
    with single_instance_lock(args.lock_file):
        LOG.warning(
            "TREND BOT started trading=%s markets=%s risk=%s%% channel=%s/%s stop=%sxATR "
            "max_days=%s long_only=%s",
            trade_cfg.enabled,
            trend_cfg.markets,
            trade_cfg.risk_pct,
            trend_cfg.entry_days,
            trend_cfg.exit_days,
            trend_cfg.stop_atr,
            trend_cfg.max_days,
            trend_cfg.long_only,
        )
        tg.send_text(
            f"[TREND] Crypto Trend bot started on {', '.join(trend_cfg.markets)} "
            f"(demo trading {'ON' if trade_cfg.enabled else 'OFF'}, risk {trade_cfg.risk_pct}% per trade). "
            "It checks once per day after the daily close; expect about 10 trades a year, "
            "mostly small losses with a few large wins."
        )
        last_beat = 0.0
        last_report = 0.0
        while not stop["flag"]:
            if mt5.terminal_info() is None:
                LOG.error("MT5 connection lost; reconnecting")
                connect()
            bot.step()
            if reporter is not None and time.time() - last_report > 60:
                reporter.report(trader, mt5)
                last_report = time.time()
            if time.time() - last_beat > 3600:
                LOG.info("heartbeat ok open_trades=%d", len(trader.open_trades()))
                last_beat = time.time()
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
