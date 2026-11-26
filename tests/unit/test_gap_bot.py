from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS  # noqa: N814

import numpy as np
import pandas as pd
import pytest

from tests.unit.test_dip_bot import FakeMT5
from trading_signal_bot.gap_bot import (
    GAP_MAGIC,
    GapBot,
    GapConfig,
    daily_atr_pct,
    gap_decision,
    load_gap_config,
    prev_cash_close,
)
from trading_signal_bot.risk_guard import DEFAULT_STATE_FILES
from trading_signal_bot.trader import DemoTrader

TODAY = pd.Timestamp("2026-11-25")  # a Wednesday
PREV_CLOSE = 20000.0  # the 23:00 cash close; daily bars have (high - low) / close = 1% -> ATR% 1%, threshold 0.75%
D1_CLOSE = 20150.0  # after-hours drift: the D1 close is NOT the gap reference (20200 vs it = +0.25%, no trade)


def ts(hm: str, day: pd.Timestamp = TODAY) -> int:
    return int(pd.Timestamp(f"{day.date()} {hm}").value // 10**9)


def m30_bars(rows: list[tuple[str, float]]) -> np.ndarray:
    a = np.zeros(len(rows), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")])
    for i, (when, c) in enumerate(rows):
        a[i] = (int(pd.Timestamp(when).value // 10**9), c, c, c, c)
    return a


class FakeGapMT5(FakeMT5):
    TIMEFRAME_M1 = 1
    TIMEFRAME_M30 = 30

    def __init__(self, open_1630: float, now: str = "16:30:03", mode: int = 0, day: pd.Timestamp = TODAY) -> None:
        super().__init__([PREV_CLOSE] * 5, mode=mode)
        days = pd.bdate_range(end=day - pd.Timedelta(days=1), periods=30)
        last = days[-1].date()
        self.m30 = m30_bars([(f"{last} 22:00", 19990.0), (f"{last} 22:30", PREV_CLOSE), (f"{last} 23:00", D1_CLOSE),
                             (f"{day.date()} 16:00", open_1630)])
        c = np.full(len(days), D1_CLOSE)
        self.d1 = np.zeros(len(days) + 1, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
                                                 ("close", "f8")])
        self.d1["time"][:-1] = [int(d.value // 10**9) for d in days]
        self.d1["open"][:-1], self.d1["close"][:-1] = c, c
        self.d1["high"][:-1], self.d1["low"][:-1] = c * 1.005, c * 0.995
        self.d1[-1] = (int(day.value // 10**9), open_1630, open_1630, open_1630, open_1630)  # forming day
        self.m1 = np.zeros(1, dtype=self.d1.dtype)
        self.m1[0] = (ts("16:30", day), open_1630, open_1630 + 5, open_1630 - 5, open_1630)
        self.price = open_1630
        self.tick_time = ts(now, day)

    def copy_rates_from_pos(self, _s, tf, _start, count):
        return {self.TIMEFRAME_M1: self.m1, self.TIMEFRAME_M30: self.m30}.get(tf, self.d1)[-count:]

    def symbol_info_tick(self, _s):
        return NS(ask=self.price + 1.0, bid=self.price, time=self.tick_time)


def make_bot(tmp_path: Path, mt5: FakeGapMT5) -> tuple[GapBot, DemoTrader, list[str]]:
    msgs: list[str] = []
    cfg, trade_cfg = load_gap_config({
        "XM_GAP_ENABLED": "1",
        "XM_GAP_KILL_FILE": str(tmp_path / "PAUSE"),
        "XM_GAP_STATE_FILE": str(tmp_path / "trades.json"),
    })
    cfg = GapConfig(**{**cfg.__dict__, "state_file": tmp_path / "gap_state.json",
                       "fills_file": tmp_path / "fills.csv"})
    trader = DemoTrader(trade_cfg, mt5, lambda s: cfg.markets.get(s, s), lambda m: msgs.append(m) or True)
    return GapBot(cfg, trader, mt5, lambda m: msgs.append(m) or True), trader, msgs


def run_pass(bot: GapBot, mt5: FakeGapMT5) -> None:
    bot.step()
    mt5.tick_time += 2
    bot.step()


def test_gap_decision():
    assert gap_decision(20200, 20000, 0.01, 0.75) == -1  # +1.0% gap up -> fade with a SELL
    assert gap_decision(19800, 20000, 0.01, 0.75) == 1  # -1.0% gap down -> BUY
    assert gap_decision(20100, 20000, 0.01, 0.75) == 0  # +0.5% < 0.75% threshold
    assert gap_decision(20200, 20000, float("nan"), 0.75) == 0


def test_prev_cash_close_is_the_2230_bar_of_the_previous_session():
    mon = pd.Timestamp("2026-11-23")
    m30 = pd.DataFrame(m30_bars([("2026-11-20 22:00", 1.0), ("2026-11-20 22:30", 2.0), ("2026-11-20 23:30", 3.0),
                                 ("2026-11-23 10:00", 4.0)]))
    m30["time"] = pd.to_datetime(m30.time, unit="s")
    assert prev_cash_close(m30, mon) == 2.0  # Monday -> Friday's 23:00 cash close, not its last bar
    early = m30[m30.time.dt.date != pd.Timestamp("2026-11-20").date()].copy()
    early = pd.concat([pd.DataFrame({"time": pd.to_datetime(["2026-11-19 22:30", "2026-11-20 19:30"]),
                                     "open": 0.0, "high": 0.0, "low": 0.0, "close": [5.0, 6.0]}), early])
    assert prev_cash_close(early, mon) is None  # previous session closed early -> skip, like the backtest


def test_gap_reference_is_cash_close_not_d1_close(tmp_path):
    mt5 = FakeGapMT5(open_1630=20200.0)  # +1.0% vs cash close (trade), only +0.25% vs D1 close
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1


def test_daily_atr_matches_research_definition():
    rng = np.random.default_rng(1)
    c = 20000 + np.cumsum(rng.normal(0, 50, 60))
    d = pd.DataFrame({"high": c + rng.uniform(20, 200, 60), "low": c - rng.uniform(20, 200, 60), "close": c})
    research = ((d.high - d.low) / d.close).rolling(14).mean().shift().iloc[-1]  # edge_lab.gap_fade's atrp
    assert daily_atr_pct(d.iloc[:-1], 14) == pytest.approx(research)


def test_gap_up_at_the_open_sells_with_stop_above_and_logs_the_fill(tmp_path):
    mt5 = FakeGapMT5(open_1630=20200.0)
    bot, trader, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
    req = mt5.sent[0]
    assert req["symbol"] == "US100Cash" and req["type"] == mt5.ORDER_TYPE_SELL and req["magic"] == GAP_MAGIC
    assert req["tp"] == 0.0 and req["sl"] > req["price"]
    assert req["sl"] - req["price"] == pytest.approx(0.01 * 20200, abs=1)  # 1 x ATR% of the open
    assert any("[GAP] SIGNAL NAS100 SELL" in m for m in msgs) and any("slippage" in m for m in msgs)
    rows = (tmp_path / "fills.csv").read_text().strip().splitlines()
    assert rows[0].startswith("date,market,side") and ",NAS100,SELL,20200.00," in rows[1]


def test_gap_down_buys(tmp_path):
    mt5 = FakeGapMT5(open_1630=19800.0)
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1 and mt5.sent[0]["type"] == mt5.ORDER_TYPE_BUY and mt5.sent[0]["sl"] < mt5.sent[0]["price"]


def test_small_gap_no_trade_and_decides_once(tmp_path):
    mt5 = FakeGapMT5(open_1630=20100.0)
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    mt5.price = 20300.0  # price runs later: the decision was already made for today
    mt5.m1[0]["open"] = 20300.0
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_no_entry_before_the_open_or_after_the_window(tmp_path):
    mt5 = FakeGapMT5(open_1630=20200.0, now="16:29:50")
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []
    late = FakeGapMT5(open_1630=20200.0, now="16:33:00")
    bot_l, _, _ = make_bot(tmp_path / "late", late)
    run_pass(bot_l, late)
    assert late.sent == []


def test_no_trade_at_weekends(tmp_path):
    sat = TODAY + pd.Timedelta(days=3)
    mt5 = FakeGapMT5(open_1630=20200.0, day=sat)
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_closes_at_19_server(tmp_path):
    mt5 = FakeGapMT5(open_1630=20200.0)
    bot, trader, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(trader.open_trades()) == 1
    mt5.tick_time = ts("18:59:58")
    bot.step()
    assert len(mt5.sent) == 1  # still open just before 19:00
    mt5.tick_time = ts("19:00:01")
    bot.step()
    close_req = mt5.sent[-1]
    assert close_req.get("position") and close_req["type"] == mt5.ORDER_TYPE_BUY
    bot.step()
    assert any("19:00" in m for m in msgs if "TRADE CLOSED" in m)


def test_refuses_real_account(tmp_path):
    mt5 = FakeGapMT5(open_1630=20200.0, mode=2)
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == [] and any("not a DEMO" in m for m in msgs)


def test_config_defaults_and_validation():
    cfg, trade = load_gap_config({})
    assert trade.enabled is False and trade.magic == GAP_MAGIC and trade.rr == 0.0 and trade.risk_pct == 0.5
    assert cfg.markets == {"NAS100": "US100Cash"}
    assert (cfg.gap_atr, cfg.stop_atr, cfg.open_hm, cfg.exit_hm, cfg.entry_window_sec) == (0.75, 1.0, (16, 30),
                                                                                            (19, 0), 120)
    with pytest.raises(ValueError):
        load_gap_config({"XM_GAP_RISK_PCT": "5"})


def test_gap_trades_count_toward_account_risk_cap():
    assert Path("data/gap/trades.json") in DEFAULT_STATE_FILES
