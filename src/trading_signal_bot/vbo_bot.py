"""Bot 5 - Volatility Breakout: Larry Williams-style short-term breakout on Ethereum.

Rules (exactly what research/daily_lab.py `vol_breakout` backtested on ETHUSD, net of spread and swap):
  entry : buy stop at today's open + 0.5 x yesterday's daily range
          sell stop at today's open - 0.5 x yesterday's daily range
          filled intraday the moment price touches the level (not at the next day's open); if both
          levels are touched before either order is seen filled, skip the day (ambiguous)
  stop  : 2 x ATR(14) of the daily bars (broker-side stop loss)
  exit  : time stop after 5 daily bars in the trade (no rule-based exit; the backtest only used the
          stop-loss and the time stop)
  one position per market; crypto trades 7 days a week; one attempt per calendar day per market

Backtest (ETHUSD, 2016-2026): 484 trades, 48.6% win, PF 1.25, +0.072R/trade, about 48 trades a year.
Stress-tested 2026-11-20: edge holds across breakout multiples 0.3-1.0x (PF 1.18-1.41 throughout) and
across all 4 sequential multi-year windows since 2017 (never negative). BTCUSD, by contrast, did not
pass the backtest filter and is not traded by this bot.
Past results do not guarantee future results; this runs on the DEMO account only (DemoTrader enforces it).

    poetry run python -m trading_signal_bot.vbo_bot             (reads .env; see .env.example XM_VBO_*)
Pause trading instantly: create data/vbo/TRADING_PAUSED
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

import pandas as pd

from trading_signal_bot.dip_bot import _atr
from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.trader import DemoTrader, TradeConfig

VBO_MAGIC = 570035
LOG = logging.getLogger("VboBot")


# ---------------------------------------------------------------- config
@dataclass(frozen=True)
class VboConfig:
    markets: dict[str, str] = field(default_factory=lambda: {"ETH": "ETHUSD"})
    k: float = 0.5
    stop_atr: float = 2.0
    max_days: int = 5
    state_file: Path = Path("data/vbo/vbo_state.json")


def _flag(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def load_vbo_config(env: Mapping[str, str | None]) -> tuple[VboConfig, TradeConfig]:
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
    for part in (env.get("XM_VBO_MARKETS") or "ETH=ETHUSD").split(","):
        label, _, broker = part.strip().partition("=")
        if label.strip():
            markets[label.strip()] = (broker or label).strip()
    vbo = VboConfig(
        markets=markets,
        k=num("XM_VBO_K", 0.5, 0.1, 2.0),
        stop_atr=num("XM_VBO_STOP_ATR", 2.0, 0.5, 6.0),
        max_days=int(num("XM_VBO_MAX_DAYS", 5, 1, 30)),
    )
    trade = TradeConfig(
        enabled=_flag(env.get("XM_VBO_ENABLED")),
        symbols=tuple(markets),
        risk_pct=num("XM_VBO_RISK_PCT", 0.5, 0.05, 2.0),
        rr=0.0,  # no take-profit; exits by time stop / broker-side stop-loss only
        daily_loss_pct=num("XM_VBO_DAILY_LOSS_PCT", 2.0, 0.5, 10.0),
        kill_file=Path(env.get("XM_VBO_KILL_FILE") or "data/vbo/TRADING_PAUSED"),
        state_file=Path(env.get("XM_VBO_STATE_FILE") or "data/vbo/trades.json"),
        magic=int(num("XM_VBO_MAGIC", VBO_MAGIC, 1, 2_000_000_000)),
        scenarios=frozenset({Scenario.VOL_BREAKOUT}),
        label="[VBO]",
    )
    return vbo, trade


# ---------------------------------------------------------------- bot
@dataclass
class VboBot:
    config: VboConfig
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
        self._state.setdefault("day", {})  # label -> {date, level_up, level_dn, entered}
        self._state.setdefault("entry_bar", {})  # ticket -> daily bar the trade was entered on

    def step(self) -> None:
        self.trader.poll_closed()
        for label, broker in self.config.markets.items():
            try:
                self._step_market(label, broker)
            except Exception:
                LOG.exception("vbo step failed for %s", label)

    def _daily(self, broker: str) -> pd.DataFrame | None:
        self.mt5.symbol_select(broker, True)
        rates = self.mt5.copy_rates_from_pos(broker, self.mt5.TIMEFRAME_D1, 0, 60)
        if rates is None or len(rates) < 30:
            return None
        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close"]]
        df["time"] = pd.to_datetime(df["time"], unit="s")  # server time; crypto trades every day
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
        cfg = self.config

        day_state: dict[str, Any] = self._state["day"].setdefault(label, {})
        today_key = str(forming.time)
        if day_state.get("date") != today_key:
            prior_range = float(closed.high.iloc[-1] - closed.low.iloc[-1]) if len(closed) else 0.0
            day_state["date"] = today_key
            day_state["entered"] = False
            if prior_range > 0:
                day_state["level_up"] = float(forming.open) + cfg.k * prior_range
                day_state["level_dn"] = float(forming.open) - cfg.k * prior_range
            else:
                day_state["level_up"] = None
                day_state["level_dn"] = None

        # --- exits: time stop only (the price stop is a broker-side SL set at entry)
        closed_any = False
        for t in [t for t in self.trader.open_trades() if t.symbol == label]:
            entry_bar = self._state["entry_bar"].get(str(t.ticket))
            bars_in_trade = int((closed.time >= pd.Timestamp(entry_bar)).sum()) if entry_bar else 0
            if bars_in_trade >= cfg.max_days:
                if not self.trader.close_position(t.ticket, f"time stop: {bars_in_trade} days in the trade"):
                    return  # retry next pass
                closed_any = True
        if closed_any:
            self.trader.poll_closed()

        # --- entry: has today's stop level been touched yet?
        level_up, level_dn = day_state.get("level_up"), day_state.get("level_dn")
        already_open = any(t.symbol == label for t in self.trader.open_trades())
        if not day_state.get("entered") and not already_open and level_up is not None:
            if float(tick.ask) >= level_up:
                self._enter(label, closed, tick, 1, forming.time, level_up)
                day_state["entered"] = True
            elif float(tick.bid) <= level_dn:
                self._enter(label, closed, tick, -1, forming.time, level_dn)
                day_state["entered"] = True

        self._save()

    def _enter(
        self, label: str, closed: pd.DataFrame, tick: Any, sig: int, entry_bar_time: pd.Timestamp, level: float
    ) -> None:
        cfg = self.config
        direction = Direction.BUY if sig > 0 else Direction.SELL
        dist = cfg.stop_atr * float(_atr(closed).iloc[-1])
        now = datetime.now(timezone.utc)
        self.notify(
            f"[VBO] SIGNAL {label} {direction.value} (volatility breakout)\n"
            f"Price crossed {level:.2f} (today's open {'+' if sig > 0 else '-'} {cfg.k:g}x yesterday's range)\n"
            f"Plan: stop {cfg.stop_atr:g}x daily ATR ({dist:.2f}); time exit after {cfg.max_days} days. "
            "Short-term, expect many small losses and a few decent wins."
        )
        before = {t.ticket for t in self.trader.open_trades()}
        self.trader.on_signal(
            Signal(
                id=Signal.new_id(),
                symbol=label,
                direction=direction,
                scenario=Scenario.VOL_BREAKOUT,
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
            LOG.exception("could not save vbo state")


# ---------------------------------------------------------------- process entry point
def main() -> None:
    from trading_signal_bot.mt5_client import _load_mt5_module
    from trading_signal_bot.settings import load_secrets
    from trading_signal_bot.telegram_notifier import TelegramNotifier
    from trading_signal_bot.utils import setup_logging, single_instance_lock

    ap = argparse.ArgumentParser(description="Bot 5 - Volatility Breakout (demo only)")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--lock-file", type=Path, default=Path("data/vbo/bot.lock"))
    ap.add_argument("--poll-seconds", type=float, default=15.0)
    args = ap.parse_args()

    secrets = load_secrets(args.env)
    setup_logging("INFO", Path("logs/vbo_bot.log"), 5_000_000, 5, timezone="Asia/Manila")
    vbo_cfg, trade_cfg = load_vbo_config(os.environ)

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
        failed_queue_file=Path("data/vbo/failed_messages.json"),
    )
    trader = DemoTrader(trade_cfg, mt5, lambda s: vbo_cfg.markets.get(s, s), tg.send_text)
    bot = VboBot(vbo_cfg, trader, mt5, tg.send_text)

    stop = {"flag": False}
    os_signal.signal(os_signal.SIGINT, lambda *_: stop.update(flag=True))
    with single_instance_lock(args.lock_file):
        LOG.warning(
            "VBO BOT started trading=%s markets=%s risk=%s%% k=%s stop=%sxATR max_days=%s",
            trade_cfg.enabled,
            vbo_cfg.markets,
            trade_cfg.risk_pct,
            vbo_cfg.k,
            vbo_cfg.stop_atr,
            vbo_cfg.max_days,
        )
        tg.send_text(
            f"[VBO] Volatility Breakout bot started on {', '.join(vbo_cfg.markets)} "
            f"(demo trading {'ON' if trade_cfg.enabled else 'OFF'}, risk {trade_cfg.risk_pct}% per trade). "
            "Watches intraday for a stop-order breakout of today's range; expect about 1 trade a week per market."
        )
        last_beat = 0.0
        while not stop["flag"]:
            if mt5.terminal_info() is None:
                LOG.error("MT5 connection lost; reconnecting")
                connect()
            bot.step()
            if time.time() - last_beat > 3600:
                LOG.info("heartbeat ok open_trades=%d", len(trader.open_trades()))
                last_beat = time.time()
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
