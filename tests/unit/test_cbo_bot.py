from __future__ import annotations

from pathlib import Path

import pytest
from test_dip_bot import FakeMT5

from trading_signal_bot.cbo_bot import CBO_MAGIC, CboBot, CboConfig, breakout_candle_signal, load_cbo_config
from trading_signal_bot.trader import DemoTrader

ROOT = Path(__file__).resolve().parents[2]


def flat(n: int = 120, level: float = 2000.0) -> list[float]:
    return [level] * n


def uptrend(n: int = 120) -> list[float]:
    return [1000 + 10 * i for i in range(n)]


def with_forming(closes: list[float]) -> list[float]:
    return [*closes, closes[-1]]


def make_candle(mt5: FakeMT5, direction: int) -> None:
    """Overwrite the last CLOSED bar (index -2; -1 is the still-forming bar) with a strong-bodied
    candle that breaks the flat 55-day channel, in the given direction (+1 bull, -1 bear)."""
    if direction > 0:
        o, h, lo, c = 2000.0, 2012.0, 1998.0, 2010.0  # body 10/range 14=0.71; close at 12/14=0.86 of range
    else:
        o, h, lo, c = 2000.0, 2002.0, 1988.0, 1990.0  # body 10/range 14=0.71; close at 12/14=0.86 of range
    mt5.rates["open"][-2] = o
    mt5.rates["high"][-2] = h
    mt5.rates["low"][-2] = lo
    mt5.rates["close"][-2] = c


def make_bot(tmp_path: Path, mt5: FakeMT5, **kw) -> tuple[CboBot, DemoTrader, list[str]]:
    msgs: list[str] = []
    cfg, trade_cfg = load_cbo_config(
        {
            "XM_CBO_ENABLED": "1",
            "XM_CBO_MARKETS": "GOLD=GOLD",
            "XM_CBO_KILL_FILE": str(tmp_path / "PAUSE"),
            "XM_CBO_STATE_FILE": str(tmp_path / "trades.json"),
        }
    )
    cfg = CboConfig(**{**cfg.__dict__, "state_file": tmp_path / "cbo_state.json", **kw})
    trader = DemoTrader(
        trade_cfg, mt5, lambda s: cfg.markets.get(s, s), lambda m: msgs.append(m) or True
    )
    return CboBot(cfg, trader, mt5, lambda m: msgs.append(m) or True), trader, msgs


def run_pass(bot: CboBot, mt5: FakeMT5) -> None:
    bot.step()
    mt5.advance_tick()
    bot.step()


def test_buys_on_candle_confirmed_upside_breakout(tmp_path):
    mt5 = FakeMT5(with_forming(flat()))
    make_candle(mt5, 1)
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
    req = mt5.sent[0]
    assert req["symbol"] == "GOLD" and req["type"] == mt5.ORDER_TYPE_BUY
    assert req["magic"] == CBO_MAGIC and req["tp"] == 0.0 and req["sl"] < req["price"]
    assert any("[CBO] SIGNAL GOLD BUY" in m for m in msgs)


def test_sells_on_candle_confirmed_downside_breakout(tmp_path):
    mt5 = FakeMT5(with_forming(flat()))
    make_candle(mt5, -1)
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    req = mt5.sent[0]
    assert req["type"] == mt5.ORDER_TYPE_SELL and req["sl"] > req["price"]
    assert any("SIGNAL GOLD SELL" in m for m in msgs)


def test_plain_breakout_without_strong_candle_is_ignored(tmp_path):
    # a smooth uptrend closes above its own 55-day high every day, but every bar here has open==close
    # (body == 0), so the candle filter must block it even though the plain breakout condition holds.
    mt5 = FakeMT5(with_forming(uptrend()))
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_no_signal_inside_the_channel(tmp_path):
    flat_wobble = [2000 + (5 if i % 2 else -5) for i in range(120)]
    mt5 = FakeMT5(with_forming(flat_wobble))
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_time_stop_after_max_days(tmp_path):
    mt5 = FakeMT5(with_forming(flat()))
    make_candle(mt5, 1)
    bot, trader, msgs = make_bot(tmp_path, mt5, max_days=3)
    run_pass(bot, mt5)
    assert trader.open_trades()
    closes = flat()
    for day in range(1, 4):
        closes = [*closes, 2010.0 + day]  # stay inside the 20-day low; just advance the clock
        mt5.set_bars(with_forming(closes))
        run_pass(bot, mt5)
        if day < 3:
            assert trader.open_trades(), f"closed too early on day {day}"
    assert trader.open_trades() == []
    assert any("time stop" in m for m in msgs if "TRADE CLOSED" in m)


def test_refuses_real_account_and_honours_kill_file(tmp_path):
    mt5 = FakeMT5(with_forming(flat()), mode=2)
    make_candle(mt5, 1)
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == [] and any("not a DEMO" in m for m in msgs)

    mt5b = FakeMT5(with_forming(flat()))
    make_candle(mt5b, 1)
    (tmp_path / "PAUSE").write_text("x")
    bot_b, _, _ = make_bot(tmp_path, mt5b)
    run_pass(bot_b, mt5b)
    assert mt5b.sent == []


def test_config_defaults_and_validation():
    cfg, trade = load_cbo_config({})
    assert trade.enabled is False and trade.risk_pct == 0.5 and trade.magic == CBO_MAGIC
    assert trade.rr == 0.0 and trade.label == "[CBO]"
    assert cfg.markets == {"GOLD": "GOLD"}
    assert (cfg.entry_days, cfg.exit_days, cfg.max_days) == (55, 20, 120)
    with pytest.raises(ValueError):
        load_cbo_config({"XM_CBO_RISK_PCT": "5"})
    with pytest.raises(ValueError):
        load_cbo_config({"XM_CBO_ENTRY_DAYS": "20", "XM_CBO_EXIT_DAYS": "30"})


def test_signal_formula_matches_backtest():
    import sys

    sys.path.insert(0, str(ROOT / "research"))
    import daily_lab as lab

    mt5 = FakeMT5(with_forming(flat()))
    make_candle(mt5, 1)
    import pandas as pd

    df = pd.DataFrame(mt5.rates)[:-1]  # closed bars only, matching the bot's own slicing
    live = breakout_candle_signal(df, 55, 20)
    tested, _, _ = lab.signals(df, "breakout_candle")
    assert (live == tested).all() and live[-1] == 1
