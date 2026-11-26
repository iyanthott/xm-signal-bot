"""Bot 8 - Open Gap Fade: trade against a large overnight move at the US cash open, flat by 19:00 server.

Rules (exactly what research/edge_lab.py `gap_fade` backtested on NAS100 M30 2018-2026, net of spread):
  signal : at 16:30 server (09:30 New York, the cash open) take the 16:30 one-minute bar's open and the previous
           cash close. If |open / prev close - 1| >= 0.75 x ATR%(14 daily (high-low)/close), trade AGAINST the
           move (gap up -> SELL, gap down -> BUY), immediately at market. "Previous close" = close of the
           previous weekday's 22:30 M30 bar (the 23:00 server cash close), exactly as the backtest - see
           prev_cash_close.
  stop   : 1 x ATR% of the entry price (broker-side stop loss)
  exit   : 19:00 server time at market (no overnight hold -> no swap)
  one trade per market per day; entries only within ENTRY_WINDOW_SEC of 16:30 (the edge is in the first minutes)

Backtest (NAS100, M30 2018-2026, 1.5x spread): 179 trades, 51% win, PF 1.35, +1.3R/year, worst drawdown 3.0R;
positive in train and test across 0.75-1.0 thresholds, 0.5-1.0 ATR stops and up to 2.5x spread. Much stronger
since 2022 (2019 and 2021 lost). Every fill is logged against the 16:30 open to measure real slippage.
Past results do not guarantee future results; this runs on the DEMO account only (DemoTrader enforces it).

    poetry run python -m trading_signal_bot.gap_bot            (reads .env; see .env.example XM_GAP_*)
Pause trading instantly: create data/gap/TRADING_PAUSED
"""

from __future__ import annotations

import argparse
import csv
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

from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.trader import DemoTrader, TradeConfig

GAP_MAGIC = 570038
LOG = logging.getLogger("GapBot")


@dataclass(frozen=True)
class GapConfig:
    markets: dict[str, str] = field(default_factory=lambda: {"NAS100": "US100Cash"})
    gap_atr: float = 0.75
    stop_atr: float = 1.0
    atr_days: int = 14
    open_hm: tuple[int, int] = (16, 30)  # server time = 09:30 New York (XM server follows US DST)
    exit_hm: tuple[int, int] = (19, 0)
    entry_window_sec: int = 120
    state_file: Path = Path("data/gap/gap_state.json")
    fills_file: Path = Path("data/gap/fills.csv")


def load_gap_config(env: Mapping[str, str | None]) -> tuple[GapConfig, TradeConfig]:
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
    for part in (env.get("XM_GAP_MARKETS") or "NAS100=US100Cash").split(","):
        label, _, broker = part.strip().partition("=")
        if label.strip():
            markets[label.strip()] = (broker or label).strip()
    cfg = GapConfig(
        markets=markets,
        gap_atr=num("XM_GAP_ATR_MULT", 0.75, 0.1, 5.0),
        stop_atr=num("XM_GAP_STOP_ATR", 1.0, 0.1, 5.0),
        entry_window_sec=int(num("XM_GAP_ENTRY_WINDOW_SEC", 120, 10, 900)),
    )
    trade = TradeConfig(
        enabled=str(env.get("XM_GAP_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"},
        symbols=tuple(markets),
        risk_pct=num("XM_GAP_RISK_PCT", 0.5, 0.05, 2.0),
        rr=0.0,  # no take-profit; time exit at 19:00
        daily_loss_pct=num("XM_GAP_DAILY_LOSS_PCT", 2.0, 0.5, 10.0),
        kill_file=Path(env.get("XM_GAP_KILL_FILE") or "data/gap/TRADING_PAUSED"),
        state_file=Path(env.get("XM_GAP_STATE_FILE") or "data/gap/trades.json"),
        magic=int(num("XM_GAP_MAGIC", GAP_MAGIC, 1, 2_000_000_000)),
        scenarios=frozenset({Scenario.GAP_FADE}),
        label="[GAP]",
        risk_cap_pct=num("XM_RISK_CAP_PCT", 5.0, 0.0, 100.0),
    )
    return cfg, trade


def gap_decision(open_px: float, prev_close: float, atr_pct: float, gap_atr: float) -> int:
    """+1 buy (fade a gap down), -1 sell (fade a gap up), 0 no trade."""
    if prev_close <= 0 or not atr_pct > 0:
        return 0
    move = open_px / prev_close - 1
    if abs(move) < gap_atr * atr_pct:
        return 0
    return -1 if move > 0 else 1


def prev_cash_close(m30: pd.DataFrame, today: pd.Timestamp, bar_hm: tuple[int, int] = (22, 30)) -> float | None:
    """Close of the last weekday 22:30 server M30 bar before `today` = the 23:00 server (16:00 New York) cash
    close - the backtest's 'previous close'. NOT the D1 close: that is the last tick of the day and includes
    ~an hour of after-hours drift (on 2018-2026 data the D1 close cut PF from 1.52 to 1.30 and moved 61 signal days)."""
    before = m30[(m30.time < today) & (m30.time.dt.dayofweek < 5)]
    if before.empty:
        return None
    last_day = before.time.iloc[-1].normalize()  # previous trading session; an early close there -> skip (as backtest)
    hit = before[before.time == last_day + pd.Timedelta(hours=bar_hm[0], minutes=bar_hm[1])]
    return float(hit.close.iloc[0]) if len(hit) else None


def daily_atr_pct(daily: pd.DataFrame, n: int) -> float:
    """Mean of (high - low) / close over the last n completed daily bars (same as the backtest)."""
    d = daily.tail(n)
    return float(((d.high - d.low) / d.close).mean()) if len(d) >= n else float("nan")


@dataclass
class GapBot:
    config: GapConfig
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
        self._state.setdefault("decided_day", {})  # label -> server date already evaluated
        self._state.setdefault("entry_day", {})  # ticket -> server date it was opened

    def step(self) -> None:
        self.trader.poll_closed()
        for label, broker in self.config.markets.items():
            try:
                self._step_market(label, broker)
            except Exception:
                LOG.exception("gap step failed for %s", label)

    def _rates(self, broker: str, tf: Any, count: int) -> pd.DataFrame | None:
        rates = self.mt5.copy_rates_from_pos(broker, tf, 0, count)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close"]]
        df["time"] = pd.to_datetime(df["time"], unit="s")  # server time
        return df

    def _step_market(self, label: str, broker: str) -> None:
        self.mt5.symbol_select(broker, True)
        tick = self.mt5.symbol_info_tick(broker)
        if tick is None:
            return
        now = pd.to_datetime(int(tick.time), unit="s")  # server time
        prev = self._last_tick.get(label)
        self._last_tick[label] = int(tick.time)
        if prev is None or int(tick.time) <= prev:
            return  # quote not moving: market closed or feed stalled

        day = str(now.date())
        exit_at = now.normalize() + pd.Timedelta(hours=self.config.exit_hm[0], minutes=self.config.exit_hm[1])
        for t in [t for t in self.trader.open_trades() if t.symbol == label]:
            opened_day = self._state["entry_day"].get(str(t.ticket), day)
            if now >= exit_at or opened_day != day:
                self.trader.close_position(t.ticket, "time exit: 19:00 server")

        if now.dayofweek >= 5 or self._state["decided_day"].get(label) == day:
            return
        open_at = now.normalize() + pd.Timedelta(hours=self.config.open_hm[0], minutes=self.config.open_hm[1])
        if now < open_at:
            return
        if (now - open_at).total_seconds() > self.config.entry_window_sec:
            LOG.info("%s: missed the %s entry window (now %s); no trade today", label, open_at.time(), now.time())
            self._decide(label, day)
            return

        m1 = self._rates(broker, self.mt5.TIMEFRAME_M1, 5)
        bar = m1[m1.time == open_at] if m1 is not None else None
        if bar is None or bar.empty:
            return  # the 16:30 bar has not printed yet; try again next pass
        daily = self._rates(broker, self.mt5.TIMEFRAME_D1, 40)
        if daily is None:
            return
        done = daily[(daily.time < now.normalize()) & (daily.time.dt.dayofweek < 5)]
        if len(done) < self.config.atr_days:
            return
        m30 = self._rates(broker, self.mt5.TIMEFRAME_M30, 300)  # ~6 trading days: covers weekends + a holiday
        cash_close = prev_cash_close(m30, now.normalize()) if m30 is not None else None
        if cash_close is None:
            LOG.warning("%s: no previous 23:00 cash-close bar found; no trade today", label)
            self._decide(label, day)
            return
        open_px, prev_close = float(bar.open.iloc[0]), cash_close
        atr_pct = daily_atr_pct(done, self.config.atr_days)
        d = gap_decision(open_px, prev_close, atr_pct, self.config.gap_atr)
        move_pct = 100 * (open_px / prev_close - 1)
        LOG.info("%s 16:30 open %.2f vs prev close %.2f = %+.2f%% (threshold %.2f%%) -> %s", label, open_px,
                 prev_close, move_pct, 100 * self.config.gap_atr * atr_pct, {1: "BUY", -1: "SELL", 0: "no trade"}[d])
        if d != 0 and not any(t.symbol == label for t in self.trader.open_trades()):
            self._enter(label, d, open_px, prev_close, atr_pct, tick, day)
        self._decide(label, day)

    def _enter(self, label: str, d: int, open_px: float, prev_close: float, atr_pct: float, tick: Any,
               day: str) -> None:
        dist = self.config.stop_atr * atr_pct * open_px
        side = "BUY" if d == 1 else "SELL"
        move_pct = 100 * (open_px / prev_close - 1)
        self.notify(
            f"[GAP] SIGNAL {label} {side} (fade the overnight move at the cash open)\n"
            f"Open {open_px:.2f} vs previous close {prev_close:.2f} = {move_pct:+.2f}% "
            f"(>= {self.config.gap_atr:g} x ATR {100 * atr_pct:.2f}%)\n"
            f"Plan: stop {self.config.stop_atr:g}x ATR ({dist:.2f}); close at 19:00 server"
        )
        before = {t.ticket for t in self.trader.open_trades()}
        now = datetime.now(timezone.utc)
        self.trader.on_signal(
            Signal(
                id=Signal.new_id(),
                symbol=label,
                direction=Direction.BUY if d == 1 else Direction.SELL,
                scenario=Scenario.GAP_FADE,
                price=float(tick.ask if d == 1 else tick.bid),
                created_at_utc=now,
                m1_bar_time_utc=now,
                risk_stop_distance=dist,
            )
        )
        for t in self.trader.open_trades():
            if t.ticket not in before:
                self._state["entry_day"][str(t.ticket)] = day
                slip_bp = d * (t.entry / open_px - 1) * 1e4  # positive = filled worse than the 16:30 open
                self._log_fill(day, label, side, open_px, t.entry, slip_bp)
                self.notify(f"[GAP] fill {t.entry:.2f} vs 16:30 open {open_px:.2f}: slippage {slip_bp:+.1f} bp")

    def _log_fill(self, day: str, label: str, side: str, open_px: float, fill: float, slip_bp: float) -> None:
        path = self.config.fills_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["date", "market", "side", "open_1630", "fill", "slippage_bp"])
                w.writerow([day, label, side, f"{open_px:.2f}", f"{fill:.2f}", f"{slip_bp:.2f}"])
        except OSError:
            LOG.exception("could not log fill")

    def _decide(self, label: str, day: str) -> None:
        self._state["decided_day"][label] = day
        live = {str(t.ticket) for t in self.trader.open_trades()}
        self._state["entry_day"] = {k: v for k, v in self._state["entry_day"].items() if k in live}
        try:
            self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.config.state_file.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        except OSError:
            LOG.exception("could not save gap state")


def main() -> None:
    from trading_signal_bot.settings import load_secrets
    from trading_signal_bot.telegram_notifier import TelegramNotifier
    from trading_signal_bot.utils import setup_logging, single_instance_lock

    ap = argparse.ArgumentParser(description="Bot 8 - Open Gap Fade (demo only)")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--lock-file", type=Path, default=Path("data/gap/bot.lock"))
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    args = ap.parse_args()

    secrets = load_secrets(args.env)
    setup_logging("INFO", Path("logs/gap_bot.log"), 5_000_000, 5, timezone="Asia/Manila")
    gap_cfg, trade_cfg = load_gap_config(os.environ)

    from trading_signal_bot.mt5_client import _load_mt5_module

    mt5 = _load_mt5_module()
    if mt5 is None:
        raise SystemExit("MetaTrader5 package is unavailable in this environment")
    ok = mt5.initialize(path=secrets.mt5_terminal_path) if secrets.mt5_terminal_path else mt5.initialize()
    if not ok:
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    acct = mt5.account_info()
    if acct is None or int(acct.login) != secrets.mt5_login:
        raise SystemExit("MT5 terminal is not logged into the account in .env; refusing to start")

    tg = TelegramNotifier(
        token=secrets.telegram_bot_token,
        chat_id=secrets.telegram_chat_id,
        failed_queue_file=Path("data/gap/failed_messages.json"),
    )
    from trading_signal_bot.web_report import WebTradeReporter, load_web_report_config

    report_cfg = load_web_report_config(os.environ, "XM_GAP_WEB_TOKEN")
    reporter = WebTradeReporter(report_cfg) if report_cfg else None
    trader = DemoTrader(
        trade_cfg, mt5, lambda s: gap_cfg.markets.get(s, s), tg.send_text,
        on_closed=reporter.on_closed if reporter else None,
    )
    bot = GapBot(gap_cfg, trader, mt5, tg.send_text)

    stop = {"flag": False}
    os_signal.signal(os_signal.SIGINT, lambda *_: stop.update(flag=True))
    with single_instance_lock(args.lock_file):
        LOG.warning(
            "GAP BOT started trading=%s markets=%s risk=%s%% gap>=%sxATR stop=%sxATR window=%ss web_report=%s",
            trade_cfg.enabled, gap_cfg.markets, trade_cfg.risk_pct, gap_cfg.gap_atr, gap_cfg.stop_atr,
            gap_cfg.entry_window_sec, reporter is not None,
        )
        tg.send_text(
            f"[GAP] Open Gap Fade bot started on {', '.join(gap_cfg.markets)} "
            f"(demo trading {'ON' if trade_cfg.enabled else 'OFF'}, risk {trade_cfg.risk_pct}% per trade). "
            "It checks once per day at the 16:30 server cash open; expect about 1-2 trades a month."
        )
        last_beat = 0.0
        last_report = 0.0
        while not stop["flag"]:
            if mt5.terminal_info() is None:
                LOG.error("MT5 connection lost; reconnecting")
                mt5.initialize(path=secrets.mt5_terminal_path) if secrets.mt5_terminal_path else mt5.initialize()
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
