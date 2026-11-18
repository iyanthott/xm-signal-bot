from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.trader import MAGIC, DemoTrader, TradeConfig, load_trade_config

UTC = timezone.utc
NOW = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    SYMBOL_TRADE_MODE_FULL = 4
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_OUT = 1

    def __init__(self, mode: int = 0, balance: float = 10000.0) -> None:
        self.mode = mode
        self.balance = balance
        self.sent: list[dict] = []
        self.positions: list[NS] = []
        self.deals: list[NS] = []
        self.retcode = 10009
        self.info = NS(
            trade_mode=4, point=0.01, digits=2, trade_stops_level=10, trade_tick_size=0.01,
            trade_tick_value=1.0, volume_step=0.01, volume_min=0.01, volume_max=50.0, filling_mode=2,
        )

    def account_info(self):
        return NS(trade_mode=self.mode, balance=self.balance)

    def symbol_select(self, *_a):
        return True

    def symbol_info(self, _s):
        return self.info

    def symbol_info_tick(self, _s):
        return NS(ask=2000.10, bid=2000.00)

    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def history_deals_get(self, *a, position=None):
        if position is not None:
            return [d for d in self.deals if d.position_id == position]
        return list(self.deals)

    def order_send(self, req):
        self.sent.append(req)
        if self.retcode == self.TRADE_RETCODE_DONE:
            self.positions.append(NS(ticket=555, identifier=555, symbol=req["symbol"], magic=MAGIC, profit=0.0))
        return NS(retcode=self.retcode, comment="x", order=555, deal=777, price=req["price"])

    def last_error(self):
        return (1, "err")


def make_signal(symbol="XAUUSD", direction=Direction.BUY, scenario=Scenario.BUY_S1, dist=5.0) -> Signal:
    return Signal(
        id="s1", symbol=symbol, direction=direction, scenario=scenario, price=2000.0,
        created_at_utc=NOW, m1_bar_time_utc=NOW, m15_bar_time_utc=NOW, risk_stop_distance=dist,
    )


def make_trader(tmp_path: Path, mt5: FakeMT5, **kw) -> tuple[DemoTrader, list[str]]:
    msgs: list[str] = []
    cfg = TradeConfig(
        enabled=kw.pop("enabled", True), kill_file=tmp_path / "PAUSE", state_file=tmp_path / "t.json", **kw
    )
    t = DemoTrader(cfg, mt5, lambda s: {"XAUUSD": "GOLD", "NAS100": "US100Cash"}.get(s, s),
                   lambda m: msgs.append(m) or True, now_fn=lambda: NOW)
    return t, msgs


def test_opens_buy_with_1pct_risk_and_sl_tp(tmp_path):
    mt5 = FakeMT5()
    t, msgs = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    req = mt5.sent[0]
    assert req["symbol"] == "GOLD" and req["type"] == mt5.ORDER_TYPE_BUY
    assert req["sl"] == 1995.10 and req["tp"] == 2007.60  # entry 2000.10, dist 5, rr 1.5
    # risk 100 / (5/0.01*1.0 = 500 per lot) = 0.2 lots
    assert req["volume"] == pytest.approx(0.2)
    assert "TRADE OPENED" in msgs[0]


def test_refuses_real_account(tmp_path):
    mt5 = FakeMT5(mode=2)
    t, msgs = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    assert mt5.sent == []
    assert "not a DEMO" in msgs[0]


def test_disabled_and_kill_file(tmp_path):
    mt5 = FakeMT5()
    t, _ = make_trader(tmp_path, mt5, enabled=False)
    t.on_signal(make_signal())
    assert mt5.sent == []
    t2, _ = make_trader(tmp_path, mt5)
    (tmp_path / "PAUSE").write_text("x")
    t2.on_signal(make_signal())
    assert mt5.sent == []


def test_alert_only_symbol_and_scenario(tmp_path):
    mt5 = FakeMT5()
    t, _ = make_trader(tmp_path, mt5)
    t.on_signal(make_signal(symbol="EURUSD"))
    t.on_signal(make_signal(scenario=Scenario.BUY_CHAIN))
    assert mt5.sent == []


def test_one_position_per_symbol(tmp_path):
    mt5 = FakeMT5()
    t, _ = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    t.on_signal(make_signal())
    assert len(mt5.sent) == 1


def test_daily_loss_limit_blocks_new_trades(tmp_path):
    mt5 = FakeMT5()
    mt5.deals = [NS(magic=MAGIC, entry=1, profit=-350.0, commission=0, swap=0, position_id=1, price=1.0)]
    t, msgs = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    assert mt5.sent == []
    assert "DAILY LOSS LIMIT" in msgs[0]


def test_skips_when_min_lot_exceeds_double_risk(tmp_path):
    mt5 = FakeMT5(balance=100.0)  # budget 1.0; min lot risks 5.0
    t, msgs = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    assert mt5.sent == []
    assert "SKIPPED" in msgs[0]


def test_rejected_order_reported(tmp_path):
    mt5 = FakeMT5()
    mt5.retcode = 10019
    t, msgs = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    assert "TRADE FAILED" in msgs[0]


def test_close_reported_once_with_result(tmp_path):
    mt5 = FakeMT5()
    t, msgs = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    mt5.positions.clear()
    mt5.deals = [NS(position_id=555, entry=1, profit=150.0, commission=0, swap=0, price=2007.6, magic=MAGIC)]
    t.poll_closed()
    t.poll_closed()
    closed = [m for m in msgs if "TRADE CLOSED" in m]
    assert len(closed) == 1 and "WIN" in closed[0] and "take profit" in closed[0] and "+1.50R" in closed[0]


def test_tracked_trades_survive_restart(tmp_path):
    mt5 = FakeMT5()
    t, _ = make_trader(tmp_path, mt5)
    t.on_signal(make_signal())
    t2, msgs2 = make_trader(tmp_path, mt5)
    mt5.positions.clear()
    mt5.deals = [NS(position_id=555, entry=1, profit=-100.0, commission=0, swap=0, price=1995.1, magic=MAGIC)]
    t2.poll_closed()
    assert "LOSS" in msgs2[0] and "stop loss" in msgs2[0]


def test_config_defaults_off_and_validation():
    assert load_trade_config({}).enabled is False
    cfg = load_trade_config({"XM_TRADE_ENABLED": "1", "XM_TRADE_SYMBOLS": "XAUUSD, NAS100,EURUSD"})
    assert cfg.symbols == ("XAUUSD", "NAS100", "EURUSD")
    with pytest.raises(ValueError):
        load_trade_config({"XM_TRADE_RISK_PCT": "50"})


def test_m1_bot_scenarios_magic_and_label(tmp_path):
    from trading_signal_bot.trader import _parse_scenarios

    mt5 = FakeMT5()
    msgs: list[str] = []
    cfg = TradeConfig(
        enabled=True, kill_file=tmp_path / "P", state_file=tmp_path / "m1.json", magic=570032,
        scenarios=_parse_scenarios("BUY_M1,SELL_M1"), label="[M1]",
    )
    t = DemoTrader(cfg, mt5, lambda s: s, lambda m: msgs.append(m) or True, now_fn=lambda: NOW)
    t.on_signal(make_signal(scenario=Scenario.BUY_S1))  # legacy is not this bot's job
    assert mt5.sent == []
    t.on_signal(make_signal(scenario=Scenario.BUY_M1, dist=1.7))
    assert mt5.sent[0]["magic"] == 570032
    assert msgs[0].startswith("[M1] TRADE OPENED")


def test_other_bots_positions_are_ignored(tmp_path):
    mt5 = FakeMT5()
    mt5.positions = [NS(ticket=9, identifier=9, symbol="GOLD", magic=570031, profit=0.0)]
    msgs: list[str] = []
    cfg = TradeConfig(enabled=True, kill_file=tmp_path / "P", state_file=tmp_path / "m1.json", magic=570032,
                      scenarios=frozenset({Scenario.BUY_M1}))
    t = DemoTrader(cfg, mt5, lambda s: s, lambda m: msgs.append(m) or True, now_fn=lambda: NOW)
    t.on_signal(make_signal(scenario=Scenario.BUY_M1, dist=1.7))
    assert len(mt5.sent) == 1  # the M15 bot's open GOLD trade does not block the M1 bot
