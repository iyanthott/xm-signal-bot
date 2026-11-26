from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS  # noqa: N814

import numpy as np
import pandas as pd
import pytest

from trading_signal_bot.dip_bot import DIP_MAGIC, DipBot, DipConfig, dip_signal, load_dip_config
from trading_signal_bot.trader import DemoTrader

ROOT = Path(__file__).resolve().parents[2]
DAY = 86400


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
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
    TIMEFRAME_D1 = 16408

    def __init__(self, closes: list[float], mode: int = 0) -> None:
        self.mode = mode
        self.sent: list[dict] = []
        self.positions: list[NS] = []
        self.deals: list[NS] = []
        self.next_ticket = 1000
        self.tick_time = 0
        self.set_bars(closes)
        self.info = NS(
            trade_mode=4,
            point=0.01,
            digits=2,
            trade_stops_level=0,
            trade_tick_size=0.01,
            trade_tick_value=0.01,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
            filling_mode=2,
        )

    def set_bars(
        self, closes: list[float], start: int = 1_600_000_000 - (1_600_000_000 % DAY)
    ) -> None:
        """Closed bars + one forming bar (the last close); weekdays only so the bot's weekend filter keeps them all."""
        times, t = [], start
        for _ in closes:
            while datetime.fromtimestamp(t, timezone.utc).weekday() >= 5:
                t += DAY
            times.append(t)
            t += DAY
        c = np.array(closes, dtype=float)
        self.rates = np.zeros(
            len(c),
            dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")],
        )
        self.rates["time"], self.rates["open"], self.rates["close"] = times, c, c
        self.rates["high"], self.rates["low"] = c + 5, c - 5
        down = np.r_[False, c[1:] < c[:-1]]  # down days close near their low (IBS 0.1), as real dips do
        self.rates["high"][down], self.rates["low"][down] = c[down] + 9, c[down] - 1
        self.tick_time = int(times[-1]) + 3600  # one hour into the forming day

    def advance_tick(self, seconds: int = 30) -> None:
        self.tick_time += seconds

    def account_info(self):
        return NS(trade_mode=self.mode, balance=10000.0, login=1)

    def symbol_select(self, *_a):
        return True

    def symbol_info(self, _s):
        return self.info

    def symbol_info_tick(self, _s):
        price = float(self.rates["close"][-1])
        return NS(ask=price + 0.5, bid=price, time=self.tick_time)

    def copy_rates_from_pos(self, _s, _tf, _start, count):
        return self.rates[-count:]

    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def history_deals_get(self, *a, position=None):
        if position is not None:
            return [d for d in self.deals if d.position_id == position]
        return list(self.deals)

    def order_send(self, req):
        self.sent.append(req)
        if "position" in req:  # closing deal
            pos = next(p for p in self.positions if p.ticket == req["position"])
            self.positions.remove(pos)
            self.deals.append(
                NS(
                    position_id=pos.ticket,
                    entry=1,
                    profit=25.0,
                    commission=0,
                    swap=-1.0,
                    price=req["price"],
                    magic=req["magic"],
                )
            )
        else:
            self.next_ticket += 1
            self.positions.append(
                NS(
                    ticket=self.next_ticket,
                    identifier=self.next_ticket,
                    symbol=req["symbol"],
                    magic=req["magic"],
                    profit=0.0,
                    volume=req["volume"],
                )
            )
        return NS(retcode=10009, comment="done", order=self.next_ticket, deal=1, price=req["price"])

    def last_error(self):
        return (1, "err")


def uptrend_then_dip(n: int = 260) -> list[float]:
    """Steady uptrend (far above the 200-day average) ending in three sharp down days -> RSI(2) near 0."""
    up = [4000 + 5 * i for i in range(n - 3)]
    return [*up, up[-1] - 60, up[-1] - 120, up[-1] - 180]


def make_bot(tmp_path: Path, mt5: FakeMT5, **dip_kw) -> tuple[DipBot, DemoTrader, list[str]]:
    msgs: list[str] = []
    dip_cfg, trade_cfg = load_dip_config(
        {
            "XM_DIP_ENABLED": "1",
            "XM_DIP_MARKETS": "US500=US500Cash",
            "XM_DIP_KILL_FILE": str(tmp_path / "PAUSE"),
            "XM_DIP_STATE_FILE": str(tmp_path / "trades.json"),
        }
    )
    dip_cfg = DipConfig(**{**dip_cfg.__dict__, "state_file": tmp_path / "dip_state.json", **dip_kw})
    trader = DemoTrader(
        trade_cfg, mt5, lambda s: dip_cfg.markets.get(s, s), lambda m: msgs.append(m) or True
    )
    return DipBot(dip_cfg, trader, mt5, lambda m: msgs.append(m) or True), trader, msgs


def run_pass(bot: DipBot, mt5: FakeMT5) -> None:
    """Two loop passes with a moving quote (the bot waits until it has seen the price move)."""
    bot.step()
    mt5.advance_tick()
    bot.step()


def test_live_signal_matches_backtested_signal():
    sys.path.insert(0, str(ROOT / "research"))
    import strategy_lab as lab

    rng = np.random.default_rng(7)
    close = 5000 + np.cumsum(rng.normal(0.4, 25, 1500))
    df = pd.DataFrame({"close": close, "open": close, "high": close + 3, "low": close - 3})
    live = dip_signal(df, 15).to_numpy()
    tested = lab.sig_dip_buy(df, {"rsi2": 15}) == 1
    assert live.sum() > 20 and (live == tested).all()


def test_buys_the_dip_with_atr_stop_and_no_take_profit(tmp_path):
    mt5 = FakeMT5([*uptrend_then_dip(), 0])  # last value = forming bar
    mt5.rates["close"][-1] = mt5.rates["close"][-2]
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
    req = mt5.sent[0]
    assert (
        req["symbol"] == "US500Cash"
        and req["type"] == mt5.ORDER_TYPE_BUY
        and req["magic"] == DIP_MAGIC
    )
    assert req["tp"] == 0.0 and req["sl"] < req["price"]
    assert any("SIGNAL US500 BUY" in m for m in msgs) and any(
        "no TP (exits by rule)" in m for m in msgs
    )


def test_no_signal_without_uptrend(tmp_path):
    down = [6000 - 5 * i for i in range(260)]
    mt5 = FakeMT5([*down, down[-1]])
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_waits_for_a_live_quote_and_processes_each_day_once(tmp_path):
    mt5 = FakeMT5([*uptrend_then_dip(), 0])
    mt5.rates["close"][-1] = mt5.rates["close"][-2]
    bot, _, _ = make_bot(tmp_path, mt5)
    bot.step()
    bot.step()  # quote has not moved: market may be closed -> no action
    assert mt5.sent == []
    mt5.advance_tick()
    bot.step()
    mt5.advance_tick()
    bot.step()
    assert len(mt5.sent) == 1


def test_late_in_the_day_does_not_enter(tmp_path):
    mt5 = FakeMT5([*uptrend_then_dip(), 0])
    mt5.rates["close"][-1] = mt5.rates["close"][-2]
    mt5.tick_time += 6 * 3600  # 7h into the day, past the 4h entry window
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_exits_on_first_close_above_5_day_average(tmp_path):
    closes = uptrend_then_dip()
    mt5 = FakeMT5([*closes, closes[-1]])
    bot, trader, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(trader.open_trades()) == 1
    # next day: yesterday's bar closed strongly (above the 5-day average) -> sell at this open
    mt5.set_bars([*closes, closes[-1] + 400, closes[-1] + 400])
    run_pass(bot, mt5)
    close_req = mt5.sent[-1]
    assert close_req.get("position") and close_req["type"] == mt5.ORDER_TYPE_SELL
    bot.step()
    closed = [m for m in msgs if "TRADE CLOSED" in m]
    assert (
        len(closed) == 1
        and "closed above the 5-day average" in closed[0]
        and closed[0].startswith("[DIP]")
    )
    assert trader.open_trades() == []


def test_time_stop_after_max_days(tmp_path):
    closes = uptrend_then_dip()
    mt5 = FakeMT5([*closes, closes[-1]])
    bot, trader, msgs = make_bot(tmp_path, mt5, max_days=3)
    run_pass(bot, mt5)
    weak = closes[:]
    for day in range(1, 4):  # keep closing below the 5-day average for 3 more days
        weak = [*weak, weak[-1] - 1]
        mt5.set_bars([*weak, weak[-1]])
        run_pass(bot, mt5)
        if day < 3:
            assert trader.open_trades(), f"closed too early on day {day}"
    bot.step()
    assert any("time stop" in m for m in msgs if "TRADE CLOSED" in m)


def test_refuses_real_account_and_honours_kill_file(tmp_path):
    mt5 = FakeMT5([*uptrend_then_dip(), 0], mode=2)
    mt5.rates["close"][-1] = mt5.rates["close"][-2]
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == [] and any("not a DEMO" in m for m in msgs)

    mt5b = FakeMT5([*uptrend_then_dip(), 0])
    mt5b.rates["close"][-1] = mt5b.rates["close"][-2]
    (tmp_path / "PAUSE").write_text("x")
    bot_b, _, _ = make_bot(tmp_path, mt5b)
    run_pass(bot_b, mt5b)
    assert mt5b.sent == []


def test_config_defaults_and_validation():
    dip, trade = load_dip_config({})
    assert (
        trade.enabled is False
        and trade.risk_pct == 0.5
        and trade.magic == DIP_MAGIC
        and trade.rr == 0.0
    )
    assert dip.markets == {"US500": "US500Cash", "NAS100": "US100Cash"}
    assert dip.ibs_below == {"US500": 0.35, "NAS100": 0.35}
    with pytest.raises(ValueError):
        load_dip_config({"XM_DIP_RISK_PCT": "5"})


def test_ibs_filter_config():
    assert load_dip_config({"XM_DIP_IBS_FILTER": "off"})[0].ibs_below == {}
    assert load_dip_config({"XM_DIP_IBS_FILTER": "US500=0.3"})[0].ibs_below == {"US500": 0.3}
    for bad in ("US500=abc", "US500=1.5", "US500=0"):
        with pytest.raises(ValueError):
            load_dip_config({"XM_DIP_IBS_FILTER": bad})


def test_live_ibs_signal_matches_research():
    sys.path.insert(0, str(ROOT / "research"))
    import strategy_lab as lab

    rng = np.random.default_rng(3)
    close = 5000 + np.cumsum(rng.normal(0.4, 25, 1500))
    high = close + rng.uniform(1, 30, 1500)
    low = close - rng.uniform(1, 30, 1500)
    df = pd.DataFrame({"close": close, "open": close, "high": high, "low": low})
    live = dip_signal(df, 15, 0.35).to_numpy()
    ibs = (close - low) / (high - low)
    tested = (lab.sig_dip_buy(df, {"rsi2": 15}) == 1) & (ibs < 0.35)
    assert live.sum() > 5 and (live == tested).all()


def _dip_closing_mid_range() -> FakeMT5:
    mt5 = FakeMT5([*uptrend_then_dip(), 0])
    mt5.rates["close"][-1] = mt5.rates["close"][-2]
    c = mt5.rates["close"][-2]
    mt5.rates["high"][-2], mt5.rates["low"][-2] = c + 5, c - 5  # signal bar closes mid-range: IBS 0.5
    return mt5


def test_ibs_filter_blocks_mid_range_close_on_filtered_market(tmp_path):
    mt5 = _dip_closing_mid_range()
    bot, _, _ = make_bot(tmp_path, mt5)  # US500 is filtered by default
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_unfiltered_market_still_enters_on_mid_range_close(tmp_path):
    mt5 = _dip_closing_mid_range()
    bot, _, _ = make_bot(tmp_path, mt5, ibs_below={})
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
