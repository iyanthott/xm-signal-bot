"""Bot 3 - Daily Dip: buy sharp short-term dips in US stock indices that are in a long-term uptrend.

Rules (exactly what research/strategy_lab.py `dip` backtested, 2011-2026, net of spread and swap):
  entry : daily close above its 200-day average AND RSI(2) below 15  -> buy at the next day's open
  stop  : 2 x ATR(14) of the daily bars below entry (broker-side stop loss)
  exit  : first daily close above its 5-day average -> sell at the next open;
          or after 10 daily bars in the trade (time stop)
  long only, one position per market

Backtest (US500 + NAS100, 1% risk/trade): 401 trades, 73% win, PF 1.59, about +3%/year, worst drawdown 9%.
Past results do not guarantee future results; this runs on the DEMO account only (DemoTrader enforces it).

Runs as its own process next to the other two bots:
    poetry run python -m trading_signal_bot.dip_bot            (reads .env; see .env.example XM_DIP_*)
Pause trading instantly: create data/dip/TRADING_PAUSED
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

from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.trader import DemoTrader, TradeConfig

DIP_MAGIC = 570033
LOG = logging.getLogger("DipBot")


# ---------------------------------------------------------------- config
@dataclass(frozen=True)
class DipConfig:
    markets: dict[str, str] = field(
        default_factory=lambda: {"US500": "US500Cash", "NAS100": "US100Cash"}
    )
    rsi2_below: float = 15.0
    stop_atr: float = 2.0
    max_days: int = 10
    entry_window_hours: float = (
        4.0  # only enter if the new day's bar is younger than this (enter near the open)
    )
    state_file: Path = Path("data/dip/dip_state.json")


def load_dip_config(env: Mapping[str, str | None]) -> tuple[DipConfig, TradeConfig]:
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
    for part in (env.get("XM_DIP_MARKETS") or "US500=US500Cash,NAS100=US100Cash").split(","):
        label, _, broker = part.strip().partition("=")
        if label.strip():
            markets[label.strip()] = (broker or label).strip()
    dip = DipConfig(
        markets=markets,
        rsi2_below=num("XM_DIP_RSI2_BELOW", 15.0, 1.0, 50.0),
        stop_atr=num("XM_DIP_STOP_ATR", 2.0, 0.5, 6.0),
        max_days=int(num("XM_DIP_MAX_DAYS", 10, 1, 30)),
    )
    trade = TradeConfig(
        enabled=str(env.get("XM_DIP_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"},
        symbols=tuple(markets),
        risk_pct=num("XM_DIP_RISK_PCT", 0.5, 0.05, 2.0),
        rr=0.0,  # no take-profit; exits by rule
        daily_loss_pct=num("XM_DIP_DAILY_LOSS_PCT", 2.0, 0.5, 10.0),
        kill_file=Path(env.get("XM_DIP_KILL_FILE") or "data/dip/TRADING_PAUSED"),
        state_file=Path(env.get("XM_DIP_STATE_FILE") or "data/dip/trades.json"),
        magic=int(num("XM_DIP_MAGIC", DIP_MAGIC, 1, 2_000_000_000)),
        scenarios=frozenset({Scenario.BUY_DIP}),
        label="[DIP]",
        risk_cap_pct=num("XM_RISK_CAP_PCT", 5.0, 0.0, 100.0),
    )
    return dip, trade


# ---------------------------------------------------------------- indicators (same formulas as the backtest)
def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(
        axis=1
    )
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def dip_signal(daily: pd.DataFrame, rsi2_below: float) -> pd.Series:
    """True on each closed daily bar where the entry condition holds."""
    c = daily.close
    return (c > c.rolling(200).mean()) & (_rsi(c, 2) < rsi2_below)


def exit_on_strength(daily: pd.DataFrame) -> bool:
    """True when the latest closed bar closed above its 5-day average."""
    c = daily.close
    return bool(len(c) >= 5 and c.iloc[-1] > c.iloc[-5:].mean())


# ---------------------------------------------------------------- bot
@dataclass
class DipBot:
    config: DipConfig
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
        self._state.setdefault(
            "last_bar", {}
        )  # label -> time of the last closed daily bar processed
        self._state.setdefault(
            "entry_bar", {}
        )  # ticket -> time of the daily bar the trade was entered on

    # one pass of the loop; safe to call every few seconds
    def step(self) -> None:
        self.trader.poll_closed()
        for label, broker in self.config.markets.items():
            try:
                self._step_market(label, broker)
            except Exception:
                LOG.exception("dip step failed for %s", label)

    def _daily(self, broker: str) -> pd.DataFrame | None:
        self.mt5.symbol_select(broker, True)
        rates = self.mt5.copy_rates_from_pos(broker, self.mt5.TIMEFRAME_D1, 0, 320)
        if rates is None or len(rates) < 210:
            return None
        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close"]]
        df["time"] = pd.to_datetime(df["time"], unit="s")  # server time
        weekdays: pd.DataFrame = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
        return weekdays

    def _step_market(self, label: str, broker: str) -> None:
        df = self._daily(broker)
        tick = self.mt5.symbol_info_tick(broker)
        if df is None or tick is None:
            return
        forming, closed = df.iloc[-1], df.iloc[:-1].reset_index(drop=True)
        server_now = pd.to_datetime(int(tick.time), unit="s")
        # Act only while the market is live: the quote must have moved since the previous pass
        # (an open index ticks many times per 30s; a closed one does not). Offset-free, unlike clock checks.
        prev_tick = self._last_tick.get(label)
        self._last_tick[label] = int(tick.time)
        if prev_tick is None or int(tick.time) <= prev_tick or server_now < forming.time:
            return
        last_closed = str(closed.time.iloc[-1])
        if self._state["last_bar"].get(label) == last_closed:
            return

        # --- exits first (exit rule is evaluated on the bar that just closed; we are at the next open)
        for t in [t for t in self.trader.open_trades() if t.symbol == label]:
            entry_bar = self._state["entry_bar"].get(str(t.ticket))
            bars_in_trade = int((closed.time >= pd.Timestamp(entry_bar)).sum()) if entry_bar else 0
            reason = None
            if exit_on_strength(closed):
                reason = "exit rule: closed above the 5-day average"
            elif bars_in_trade >= self.config.max_days:
                reason = f"time stop: {bars_in_trade} days in the trade"
            if reason and not self.trader.close_position(t.ticket, reason):
                return  # retry next pass; do not mark the bar as processed

        # --- entry
        bar_age_h = (server_now - forming.time).total_seconds() / 3600
        sig = dip_signal(closed, self.config.rsi2_below)
        if bool(sig.iloc[-1]) and not any(t.symbol == label for t in self.trader.open_trades()):
            if bar_age_h <= self.config.entry_window_hours:
                self._enter(label, closed, float(tick.ask), forming.time)
            else:
                LOG.info(
                    "dip signal on %s but the day started %.1fh ago; skipping late entry",
                    label,
                    bar_age_h,
                )

        self._state["last_bar"][label] = last_closed
        self._save()

    def _enter(
        self, label: str, closed: pd.DataFrame, ask: float, entry_bar_time: pd.Timestamp
    ) -> None:
        atr = float(_atr(closed).iloc[-1])
        rsi2 = float(_rsi(closed.close, 2).iloc[-1])
        sma200 = float(closed.close.rolling(200).mean().iloc[-1])
        dist = self.config.stop_atr * atr
        now = datetime.now(timezone.utc)
        self.notify(
            f"[DIP] SIGNAL {label} BUY (daily dip in an uptrend)\n"
            f"Close {closed.close.iloc[-1]:.2f} above 200-day avg {sma200:.2f}; RSI(2) {rsi2:.1f} < {self.config.rsi2_below:g}\n"
            f"Plan: stop {self.config.stop_atr:g}x daily ATR ({dist:.2f}); exit on first close above the 5-day avg "
            f"or after {self.config.max_days} days"
        )
        before = {t.ticket for t in self.trader.open_trades()}
        self.trader.on_signal(
            Signal(
                id=Signal.new_id(),
                symbol=label,
                direction=Direction.BUY,
                scenario=Scenario.BUY_DIP,
                price=ask,
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
            LOG.exception("could not save dip state")


# ---------------------------------------------------------------- process entry point
def main() -> None:
    from trading_signal_bot.settings import load_secrets
    from trading_signal_bot.telegram_notifier import TelegramNotifier
    from trading_signal_bot.utils import setup_logging, single_instance_lock

    ap = argparse.ArgumentParser(description="Bot 3 - Daily Dip (demo only)")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--lock-file", type=Path, default=Path("data/dip/bot.lock"))
    ap.add_argument("--poll-seconds", type=float, default=30.0)
    args = ap.parse_args()

    secrets = load_secrets(args.env)
    setup_logging("INFO", Path("logs/dip_bot.log"), 5_000_000, 5, timezone="Asia/Manila")
    dip_cfg, trade_cfg = load_dip_config(os.environ)

    from trading_signal_bot.mt5_client import _load_mt5_module

    mt5 = _load_mt5_module()  # Windows-only dependency; loaded here so tests do not need it
    if mt5 is None:
        raise SystemExit("MetaTrader5 package is unavailable in this environment")
    ok = (
        mt5.initialize(path=secrets.mt5_terminal_path)
        if secrets.mt5_terminal_path
        else mt5.initialize()
    )
    if not ok:
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    acct = mt5.account_info()
    if acct is None or int(acct.login) != secrets.mt5_login:
        raise SystemExit("MT5 terminal is not logged into the account in .env; refusing to start")

    tg = TelegramNotifier(
        token=secrets.telegram_bot_token,
        chat_id=secrets.telegram_chat_id,
        failed_queue_file=Path("data/dip/failed_messages.json"),
    )
    from trading_signal_bot.web_report import WebTradeReporter, load_web_report_config

    report_cfg = load_web_report_config(os.environ, "XM_DIP_WEB_TOKEN")
    reporter = WebTradeReporter(report_cfg) if report_cfg else None
    trader = DemoTrader(
        trade_cfg, mt5, lambda s: dip_cfg.markets.get(s, s), tg.send_text,
        on_closed=reporter.on_closed if reporter else None,
    )
    bot = DipBot(dip_cfg, trader, mt5, tg.send_text)

    stop = {"flag": False}
    os_signal.signal(os_signal.SIGINT, lambda *_: stop.update(flag=True))
    with single_instance_lock(args.lock_file):
        LOG.warning(
            "DIP BOT started trading=%s markets=%s risk=%s%% rsi2<%s stop=%sxATR max_days=%s",
            trade_cfg.enabled,
            dip_cfg.markets,
            trade_cfg.risk_pct,
            dip_cfg.rsi2_below,
            dip_cfg.stop_atr,
            dip_cfg.max_days,
        )
        tg.send_text(
            f"[DIP] Daily Dip bot started on {', '.join(dip_cfg.markets)} "
            f"(demo trading {'ON' if trade_cfg.enabled else 'OFF'}, risk {trade_cfg.risk_pct}% per trade). "
            "It checks once per day at the market open; expect about 2-3 trades a month."
        )
        last_beat = 0.0
        last_report = 0.0
        while not stop["flag"]:
            if mt5.terminal_info() is None:
                LOG.error("MT5 connection lost; reconnecting")
                (
                    mt5.initialize(path=secrets.mt5_terminal_path)
                    if secrets.mt5_terminal_path
                    else mt5.initialize()
                )
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
