from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_dip_bot import FakeMT5

from trading_signal_bot.models import Direction
from trading_signal_bot.trader import DemoTrader
from trading_signal_bot.trend_bot import (
    TREND_MAGIC,
    TrendBot,
    TrendConfig,
    breakout_signal,
    exit_signal,
    load_trend_config,
)

ROOT = Path(__file__).resolve().parents[2]


def uptrend(n: int = 120) -> list[float]:
    return [1000 + 10 * i for i in range(n)]


def downtrend(n: int = 120) -> list[float]:
    return [3000 - 10 * i for i in range(n)]


def with_forming(closes: list[float]) -> list[float]:
    return [*closes, closes[-1]]


def make_bot(tmp_path: Path, mt5: FakeMT5, **kw) -> tuple[TrendBot, DemoTrader, list[str]]:
    msgs: list[str] = []
    cfg, trade_cfg = load_trend_config(
        {
            "XM_TREND_ENABLED": "1",
            "XM_TREND_MARKETS": "BTC=BTCUSD",
            "XM_TREND_KILL_FILE": str(tmp_path / "PAUSE"),
            "XM_TREND_STATE_FILE": str(tmp_path / "trades.json"),
        }
    )
    cfg = TrendConfig(**{**cfg.__dict__, "state_file": tmp_path / "trend_state.json", **kw})
    trader = DemoTrader(
        trade_cfg, mt5, lambda s: cfg.markets.get(s, s), lambda m: msgs.append(m) or True
    )
    return TrendBot(cfg, trader, mt5, lambda m: msgs.append(m) or True), trader, msgs


def run_pass(bot: TrendBot, mt5: FakeMT5) -> None:
    bot.step()
    mt5.advance_tick()
    bot.step()


def test_live_signal_matches_backtested_signal():
    sys.path.insert(0, str(ROOT / "research"))
    import daily_lab as lab

    rng = np.random.default_rng(11)
    close = 20000 + np.cumsum(rng.normal(5, 400, 2000))
    df = pd.DataFrame(
        {
            "close": close,
            "open": close,
            "high": close + rng.uniform(50, 300, 2000),
            "low": close - rng.uniform(50, 300, 2000),
        }
    )
    live = breakout_signal(df, 55, 20)
    tested, exit_l, exit_s = lab.signals(df, "breakout")
    assert (live != 0).sum() > 50 and (live == tested).all()
    for i in range(100, 2000, 37):  # exit rule parity on the latest closed bar
        sub = df.iloc[: i + 1]
        assert exit_signal(sub, Direction.BUY, 55, 20) == bool(exit_l[i])
        assert exit_signal(sub, Direction.SELL, 55, 20) == bool(exit_s[i])


def test_buys_upside_breakout_with_atr_stop_and_no_take_profit(tmp_path):
    mt5 = FakeMT5(with_forming(uptrend()))
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
    req = mt5.sent[0]
    assert req["symbol"] == "BTCUSD" and req["type"] == mt5.ORDER_TYPE_BUY
    assert req["magic"] == TREND_MAGIC and req["tp"] == 0.0 and req["sl"] < req["price"]
    assert any("[TREND] SIGNAL BTC BUY" in m for m in msgs)


def test_sells_downside_breakout_with_stop_above(tmp_path):
    mt5 = FakeMT5(with_forming(downtrend()))
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    req = mt5.sent[0]
    assert req["type"] == mt5.ORDER_TYPE_SELL and req["sl"] > req["price"]
    assert any("SIGNAL BTC SELL" in m for m in msgs)


def test_long_only_ignores_downside_breakout(tmp_path):
    mt5 = FakeMT5(with_forming(downtrend()))
    bot, _, _ = make_bot(tmp_path, mt5, long_only=True)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_no_signal_inside_the_channel(tmp_path):
    flat = [2000 + (5 if i % 2 else -5) for i in range(120)]
    mt5 = FakeMT5(with_forming(flat))
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_waits_for_live_quote_and_skips_late_entry(tmp_path):
    mt5 = FakeMT5(with_forming(uptrend()))
    bot, _, _ = make_bot(tmp_path, mt5)
    bot.step()
    bot.step()  # quote has not moved -> no action
    assert mt5.sent == []

    late = FakeMT5(with_forming(uptrend()))
    late.tick_time += 6 * 3600  # 7h into the day, past the 4h entry window
    bot_l, _, _ = make_bot(tmp_path / "late", late)
    run_pass(bot_l, late)
    assert late.sent == []


def test_exits_long_on_close_below_20_day_low(tmp_path):
    closes = uptrend()
    mt5 = FakeMT5(with_forming(closes))
    bot, trader, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(trader.open_trades()) == 1
    mt5.set_bars(
        with_forming([*closes, closes[-1] - 400])
    )  # below the 20-day low, above the 55-day low
    run_pass(bot, mt5)
    close_req = mt5.sent[-1]
    assert close_req.get("position") and close_req["type"] == mt5.ORDER_TYPE_SELL
    closed = [m for m in msgs if "TRADE CLOSED" in m]
    assert len(closed) == 1 and "20-day low" in closed[0] and closed[0].startswith("[TREND]")
    assert trader.open_trades() == [] and len(mt5.sent) == 2  # no new entry: not a 55-day low


def test_reverses_on_a_close_below_the_55_day_low(tmp_path):
    closes = uptrend()
    mt5 = FakeMT5(with_forming(closes))
    bot, trader, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    mt5.set_bars(with_forming([*closes, 500.0]))  # crash below every low in the window
    run_pass(bot, mt5)
    assert mt5.sent[1].get("position")  # long closed
    assert mt5.sent[2]["type"] == mt5.ORDER_TYPE_SELL and "position" not in mt5.sent[2]
    assert [t.direction for t in trader.open_trades()] == [Direction.SELL.value]


def test_time_stop_after_max_days(tmp_path):
    closes = uptrend()
    mt5 = FakeMT5(with_forming(closes))
    bot, trader, msgs = make_bot(tmp_path, mt5, max_days=3)
    run_pass(bot, mt5)
    for day in range(1, 4):
        closes = [*closes, closes[-1] + 1]
        mt5.set_bars(with_forming(closes))
        run_pass(bot, mt5)
        if day < 3:
            assert trader.open_trades(), f"closed too early on day {day}"
    bot.step()
    assert any("time stop" in m for m in msgs if "TRADE CLOSED" in m)


def test_refuses_real_account_and_honours_kill_file(tmp_path):
    mt5 = FakeMT5(with_forming(uptrend()), mode=2)
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == [] and any("not a DEMO" in m for m in msgs)

    mt5b = FakeMT5(with_forming(uptrend()))
    (tmp_path / "PAUSE").write_text("x")
    bot_b, _, _ = make_bot(tmp_path, mt5b)
    run_pass(bot_b, mt5b)
    assert mt5b.sent == []


def test_config_defaults_and_validation():
    cfg, trade = load_trend_config({})
    assert trade.enabled is False and trade.risk_pct == 0.5 and trade.magic == TREND_MAGIC
    assert trade.rr == 0.0 and trade.label == "[TREND]"
    assert cfg.markets == {"BTC": "BTCUSD", "ETH": "ETHUSD"}
    assert (cfg.entry_days, cfg.exit_days, cfg.max_days, cfg.long_only) == (55, 20, 120, False)
    with pytest.raises(ValueError):
        load_trend_config({"XM_TREND_RISK_PCT": "5"})
    with pytest.raises(ValueError):
        load_trend_config({"XM_TREND_ENTRY_DAYS": "20", "XM_TREND_EXIT_DAYS": "30"})
