"""Demo-account auto-trading for the signal bot (OFF by default, DEMO ACCOUNTS ONLY).

Safety rules (all enforced in code, covered by tests/unit/test_trader.py):
- Refuses to place any order unless the connected MT5 account is a DEMO account.
- Disabled unless XM_TRADE_ENABLED=1; also pausable instantly by creating the kill file
  (default data/TRADING_PAUSED) without restarting the bot.
- Only symbols listed in XM_TRADE_SYMBOLS trade; every other symbol stays alert-only.
- Only the proven (backtested) scenarios trade; chain / M1-only signals stay alert-only.
- Risk per trade = XM_TRADE_RISK_PCT of balance; lot size is derived from the stop distance and
  rounded DOWN. If even the minimum lot would exceed the risk budget by >2x the trade is skipped.
- One open position per symbol; daily loss limit stops new trades for the (Manila) day.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_signal_bot.models import Direction, Scenario, Signal

MAGIC = 570031
COMMENT = "XMBOT"
MANILA = ZoneInfo("Asia/Manila")
LEGACY_SCENARIOS = frozenset({Scenario.BUY_S1, Scenario.BUY_S2, Scenario.SELL_S1, Scenario.SELL_S2})


@dataclass(frozen=True)
class TradeConfig:
    enabled: bool = False
    symbols: tuple[str, ...] = ("XAUUSD", "NAS100")
    risk_pct: float = 1.0
    rr: float = 1.5
    daily_loss_pct: float = 3.0
    max_per_symbol: int = 1
    kill_file: Path = Path("data/TRADING_PAUSED")
    state_file: Path = Path("data/trades.json")
    magic: int = MAGIC  # separate per bot instance so each only sees its own trades
    scenarios: frozenset[Scenario] = LEGACY_SCENARIOS
    label: str = ""  # prefix for Telegram messages, e.g. "[M1]"


def load_trade_config(env: Mapping[str, str | None]) -> TradeConfig:
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

    symbols_raw = env.get("XM_TRADE_SYMBOLS") or "XAUUSD,NAS100"
    symbols = tuple(s.strip() for s in symbols_raw.split(",") if s.strip())
    return TradeConfig(
        enabled=str(env.get("XM_TRADE_ENABLED") or "").strip().lower()
        in {"1", "true", "yes", "on"},
        symbols=symbols,
        risk_pct=num("XM_TRADE_RISK_PCT", 1.0, 0.05, 2.0),
        rr=num("XM_TRADE_RR", 1.5, 0.5, 5.0),
        daily_loss_pct=num("XM_TRADE_DAILY_LOSS_PCT", 3.0, 0.5, 10.0),
        max_per_symbol=int(num("XM_TRADE_MAX_PER_SYMBOL", 1, 1, 5)),
        kill_file=Path(env.get("XM_TRADE_KILL_FILE") or "data/TRADING_PAUSED"),
        state_file=Path(env.get("XM_TRADE_STATE_FILE") or "data/trades.json"),
        magic=int(num("XM_TRADE_MAGIC", MAGIC, 1, 2_000_000_000)),
        scenarios=_parse_scenarios(env.get("XM_TRADE_SCENARIOS")),
        label=(env.get("XM_BOT_LABEL") or "").strip(),
    )


def _parse_scenarios(raw: str | None) -> frozenset[Scenario]:
    if not raw or not raw.strip():
        return LEGACY_SCENARIOS
    try:
        return frozenset(Scenario(x.strip()) for x in raw.split(",") if x.strip())
    except ValueError as exc:
        raise ValueError(f"XM_TRADE_SCENARIOS has an unknown scenario: {exc}") from exc


@dataclass
class _Tracked:
    ticket: int
    symbol: str  # alias
    direction: str
    volume: float
    entry: float
    sl: float
    tp: float
    risk_money: float
    opened_utc: str
    scenario: str
    exit_reason: str = ""  # set when the bot itself closes the position (exit rule)


@dataclass
class DemoTrader:
    config: TradeConfig
    mt5: Any  # the MetaTrader5 module (or a fake)
    resolve_symbol: Callable[[str], str]
    notify: Callable[[str], bool]
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    _tracked: dict[int, _Tracked] = field(default_factory=dict)
    _day_stop_notified: str = ""
    _demo_ok: bool | None = None
    _logger: logging.Logger = field(default_factory=lambda: logging.getLogger("DemoTrader"))

    def __post_init__(self) -> None:
        raw_notify = self.notify
        label = self.config.label
        if label:
            self.notify = lambda text: raw_notify(f"{label} {text}")
        self._load_state()

    # ------------------------------------------------------------------ public

    def on_signal(self, signal: Signal) -> None:
        """Called for every signal that is about to be sent. Never raises."""
        try:
            self._on_signal(signal)
        except Exception:
            self._logger.exception("trade handling failed for %s", signal.symbol)

    def poll_closed(self) -> None:
        """Report positions that have closed since the last call. Never raises."""
        try:
            self._poll_closed()
        except Exception:
            self._logger.exception("trade close polling failed")

    def open_trades(self) -> list[_Tracked]:
        """Positions this bot opened and has not yet seen close."""
        return list(self._tracked.values())

    def close_position(self, ticket: int, reason: str) -> bool:
        """Close one of this bot's positions at market (exit rule). The close is reported by
        poll_closed with `reason`. Returns True if the broker accepted the close. Never raises."""
        try:
            return self._close_position(ticket, reason)
        except Exception:
            self._logger.exception("closing position %s failed", ticket)
            return False

    # ---------------------------------------------------------------- internals

    def _eligible(self, signal: Signal) -> str | None:
        """Returns None if eligible, else a short reason (logged, not sent)."""
        cfg = self.config
        if not cfg.enabled:
            return "trading disabled"
        if cfg.kill_file.exists():
            return "kill file present (trading paused)"
        if signal.symbol not in cfg.symbols:
            return "symbol is alert-only"
        scenarios = set(signal.matched_scenarios or []) | {signal.scenario}
        if not (scenarios & cfg.scenarios):
            return "scenario is alert-only (not backtest-proven)"
        if signal.risk_stop_distance is None or signal.risk_stop_distance <= 0:
            return "signal has no stop distance"
        return None

    def _is_demo(self) -> bool:
        info = self.mt5.account_info()
        demo_const = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        ok = info is not None and getattr(info, "trade_mode", None) == demo_const
        if self._demo_ok is None or self._demo_ok != ok:
            self._demo_ok = ok
            if not ok:
                self._logger.critical("account is NOT a demo account - trading refused")
        return ok

    def _day_bounds_utc(self) -> tuple[datetime, datetime]:
        now_m = self.now_fn().astimezone(MANILA)
        start_m = now_m.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_m.astimezone(timezone.utc), (start_m + timedelta(days=1)).astimezone(
            timezone.utc
        )

    def _day_pnl(self) -> float:
        start, end = self._day_bounds_utc()
        deals = self.mt5.history_deals_get(start, end) or []
        closed = sum(
            float(d.profit)
            + float(getattr(d, "commission", 0) or 0)
            + float(getattr(d, "swap", 0) or 0)
            for d in deals
            if getattr(d, "magic", None) == self.config.magic
            and getattr(d, "entry", None) == getattr(self.mt5, "DEAL_ENTRY_OUT", 1)
        )
        floating = sum(
            float(p.profit)
            for p in (self.mt5.positions_get() or [])
            if getattr(p, "magic", None) == self.config.magic
        )
        return closed + floating

    def _open_positions(self, broker_symbol: str) -> list[Any]:
        return [
            p
            for p in (self.mt5.positions_get(symbol=broker_symbol) or [])
            if getattr(p, "magic", None) == self.config.magic
        ]

    def _on_signal(self, signal: Signal) -> None:
        reason = self._eligible(signal)
        if reason is not None:
            self._logger.info(
                "no trade for %s %s: %s", signal.symbol, signal.direction.value, reason
            )
            return
        if not self._is_demo():
            self.notify("TRADE REFUSED: the connected account is not a DEMO account.")
            return

        broker = self.resolve_symbol(signal.symbol)
        account = self.mt5.account_info()
        balance = float(account.balance)

        day_pnl = self._day_pnl()
        limit = -balance * self.config.daily_loss_pct / 100.0
        if day_pnl <= limit:
            day_key = self._day_bounds_utc()[0].isoformat()
            if self._day_stop_notified != day_key:
                self._day_stop_notified = day_key
                self.notify(
                    f"DAILY LOSS LIMIT hit ({day_pnl:.2f}, limit {limit:.2f}). "
                    "No new trades today (Manila time). Alerts continue."
                )
            self._logger.warning("daily loss limit reached: %.2f <= %.2f", day_pnl, limit)
            return

        if len(self._open_positions(broker)) >= self.config.max_per_symbol:
            self._logger.info("no trade for %s: position already open", signal.symbol)
            return

        self.mt5.symbol_select(broker, True)
        info = self.mt5.symbol_info(broker)
        tick = self.mt5.symbol_info_tick(broker)
        if info is None or tick is None:
            self.notify(f"TRADE SKIPPED {signal.symbol}: no symbol/price data from MT5.")
            return
        if getattr(info, "trade_mode", 0) != getattr(self.mt5, "SYMBOL_TRADE_MODE_FULL", 4):
            self.notify(f"TRADE SKIPPED {signal.symbol}: market not open for trading.")
            return

        is_buy = signal.direction is Direction.BUY
        entry = float(tick.ask if is_buy else tick.bid)
        dist = float(signal.risk_stop_distance)  # type: ignore[arg-type]
        point = float(info.point)
        min_dist = float(getattr(info, "trade_stops_level", 0)) * point
        if dist <= min_dist:
            self.notify(f"TRADE SKIPPED {signal.symbol}: stop distance too small for broker rules.")
            return
        digits = int(info.digits)
        sl = round(entry - dist if is_buy else entry + dist, digits)
        if self.config.rr > 0:
            tp = round(
                entry + dist * self.config.rr if is_buy else entry - dist * self.config.rr, digits
            )
        else:
            tp = 0.0  # no take-profit: the bot exits by rule (see close_position)

        tick_size = float(info.trade_tick_size)
        tick_value = float(info.trade_tick_value)
        if tick_size <= 0 or tick_value <= 0:
            self.notify(f"TRADE SKIPPED {signal.symbol}: invalid tick size/value.")
            return
        risk_budget = balance * self.config.risk_pct / 100.0
        loss_per_lot = dist / tick_size * tick_value
        step = float(info.volume_step)
        vmin = float(info.volume_min)
        vmax = float(info.volume_max)
        volume = math.floor(risk_budget / loss_per_lot / step + 1e-9) * step
        volume = min(volume, vmax)
        if volume < vmin:
            if vmin * loss_per_lot > risk_budget * 2:
                self.notify(
                    f"TRADE SKIPPED {signal.symbol}: minimum lot {vmin} would risk "
                    f"{vmin * loss_per_lot:.2f} (budget {risk_budget:.2f})."
                )
                return
            volume = vmin
        volume = round(volume, 8)
        risk_money = volume * loss_per_lot

        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": broker,
            "volume": volume,
            "type": self.mt5.ORDER_TYPE_BUY if is_buy else self.mt5.ORDER_TYPE_SELL,
            "price": entry,
            "sl": sl,
            "tp": tp,
            "deviation": 30,
            "magic": self.config.magic,
            "comment": COMMENT + (self.config.label.strip("[]") if self.config.label else ""),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(info),
        }
        result = self.mt5.order_send(request)
        done = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
        if result is None or getattr(result, "retcode", None) != done:
            code = getattr(result, "retcode", None)
            msg = (
                getattr(result, "comment", "") if result is not None else str(self.mt5.last_error())
            )
            self._logger.error("order rejected %s: %s %s", signal.symbol, code, msg)
            self.notify(f"TRADE FAILED {signal.symbol} {signal.direction.value}: {code} {msg}")
            return

        ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0))
        position_ticket = self._position_ticket(broker, ticket)
        fill = float(getattr(result, "price", entry) or entry)
        self._tracked[position_ticket] = _Tracked(
            ticket=position_ticket,
            symbol=signal.symbol,
            direction=signal.direction.value,
            volume=volume,
            entry=fill,
            sl=sl,
            tp=tp,
            risk_money=risk_money,
            opened_utc=self.now_fn().isoformat(),
            scenario=signal.scenario.value,
        )
        self._save_state()
        tp_text = f"TP {tp} (1:{self.config.rr})" if tp else "no TP (exits by rule)"
        self.notify(
            f"TRADE OPENED (DEMO) {signal.symbol} {signal.direction.value}\n"
            f"Ticket {position_ticket} | lots {volume}\n"
            f"Entry {fill} | SL {sl} | {tp_text}\n"
            f"Risk about {risk_money:.2f} ({self.config.risk_pct}% of {balance:.2f})"
        )

    def _close_position(self, ticket: int, reason: str) -> bool:
        t = self._tracked.get(ticket)
        if t is None:
            self._logger.warning("close requested for untracked position %s", ticket)
            return False
        pos = [
            p for p in (self.mt5.positions_get() or []) if int(getattr(p, "ticket", -1)) == ticket
        ]
        if not pos:
            return False  # already closed (e.g. stop loss); poll_closed reports it
        p = pos[0]
        broker = str(p.symbol)
        tick = self.mt5.symbol_info_tick(broker)
        info = self.mt5.symbol_info(broker)
        if tick is None or info is None:
            self._logger.error("cannot close %s: no price data", ticket)
            return False
        is_buy = t.direction == Direction.BUY.value
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": broker,
            "volume": float(p.volume),
            "type": self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY,
            "position": ticket,
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": 30,
            "magic": self.config.magic,
            "comment": COMMENT + "exit",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(info),
        }
        result = self.mt5.order_send(request)
        if result is None or getattr(result, "retcode", None) != getattr(
            self.mt5, "TRADE_RETCODE_DONE", 10009
        ):
            code = getattr(result, "retcode", None)
            msg = (
                getattr(result, "comment", "") if result is not None else str(self.mt5.last_error())
            )
            self._logger.error("close rejected %s: %s %s", ticket, code, msg)
            self.notify(f"CLOSE FAILED {t.symbol} ticket {ticket}: {code} {msg}. Will retry.")
            return False
        t.exit_reason = reason
        self._save_state()
        return True

    def _filling(self, info: Any) -> int:
        mode = int(getattr(info, "filling_mode", 0) or 0)
        if mode & 1:
            return int(getattr(self.mt5, "ORDER_FILLING_FOK", 0))
        if mode & 2:
            return int(getattr(self.mt5, "ORDER_FILLING_IOC", 1))
        return int(getattr(self.mt5, "ORDER_FILLING_RETURN", 2))

    def _position_ticket(self, broker: str, order_ticket: int) -> int:
        for p in self._open_positions(broker):
            if (
                getattr(p, "ticket", None) == order_ticket
                or getattr(p, "identifier", None) == order_ticket
            ):
                return int(p.ticket)
        positions = self._open_positions(broker)
        return int(positions[-1].ticket) if positions else order_ticket

    def _poll_closed(self) -> None:
        if not self._tracked:
            return
        open_tickets = {int(p.ticket) for p in (self.mt5.positions_get() or [])}
        changed = False
        for ticket in list(self._tracked):
            if ticket in open_tickets:
                continue
            deals = self.mt5.history_deals_get(position=ticket) or []
            out = [
                d
                for d in deals
                if getattr(d, "entry", None) == getattr(self.mt5, "DEAL_ENTRY_OUT", 1)
            ]
            if not out:
                continue  # history not visible yet; try again next poll
            t = self._tracked.pop(ticket)
            changed = True
            profit = sum(
                float(d.profit)
                + float(getattr(d, "commission", 0) or 0)
                + float(getattr(d, "swap", 0) or 0)
                for d in deals
            )
            exit_price = float(out[-1].price)
            reason = self._close_reason(t, exit_price)
            r_mult = profit / t.risk_money if t.risk_money else 0.0
            verdict = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BREAKEVEN")
            self.notify(
                f"TRADE CLOSED (DEMO) {t.symbol} {t.direction} - {verdict} ({reason})\n"
                f"Ticket {ticket} | entry {t.entry} -> exit {exit_price}\n"
                f"Result {profit:+.2f} ({r_mult:+.2f}R)"
            )
        if changed:
            self._save_state()

    @staticmethod
    def _close_reason(t: _Tracked, exit_price: float) -> str:
        if t.exit_reason:
            return t.exit_reason
        tol = max(abs(t.entry - t.sl) * 0.1, 1e-9)
        if t.tp and abs(exit_price - t.tp) <= max(abs(t.tp - t.entry) * 0.1, 1e-9):
            return "take profit"
        if abs(exit_price - t.sl) <= tol:
            return "stop loss"
        return "closed manually/other"

    def _load_state(self) -> None:
        try:
            raw = json.loads(self.config.state_file.read_text(encoding="utf-8"))
            self._tracked = {int(k): _Tracked(**v) for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            self._tracked = {}

    def _save_state(self) -> None:
        try:
            self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {str(k): v.__dict__ for k, v in self._tracked.items()}
            self.config.state_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            self._logger.exception("could not save trade state")
