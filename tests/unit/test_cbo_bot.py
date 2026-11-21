from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS  # noqa: N814

import numpy as np
import pandas as pd
import pytest
from test_dip_bot import FakeMT5

from trading_signal_bot.cbo_bot import (
    CBO_MAGIC,
    CboBot,
    CboConfig,
    breakout_candle_signal,
    load_cbo_config,
)
from trading_signal_bot.trader import DemoTrader, _Tracked

ROOT = Path(__file__).resolve().parents[2]


def flat(n: int = 150, level: float = 2000.0) -> list[float]:
    # Must cover need_weekdays = max(entry_days+40, max_days+20) = max(95, 140) = 140 for the default
    # CboConfig (entry_days=55, max_days=120) - see cbo_bot.py::_daily.
    return [level] * n


def uptrend(n: int = 150) -> list[float]:
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
    flat_wobble = [2000 + (5 if i % 2 else -5) for i in range(150)]
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


def real_calendar_bars(mt5: FakeMT5, n: int, level: float = 2000.0, end: int = 1_700_000_000) -> None:
    """Overwrite mt5.rates with `n` consecutive REAL calendar days (weekends included, unlike
    FakeMT5.set_bars which deliberately skips them) ending at a forming bar at `end`. Reproduces what MT5
    actually returns before the bot's own weekday filter runs."""
    day = 86400
    end = end - (end % day)
    times = [end - (n - 1 - i) * day for i in range(n)]
    c = np.array([level] * n, dtype=float)
    mt5.rates = np.zeros(n, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")])
    mt5.rates["time"], mt5.rates["open"], mt5.rates["close"] = times, c, c
    mt5.rates["high"], mt5.rates["low"] = c + 5, c - 5
    mt5.tick_time = int(times[-1]) + 3600


def test_time_stop_fires_even_though_the_feed_includes_real_weekends(tmp_path):
    """Regression test: entry_days=55 + max_days=120 (both defaults) used to fetch too few RAW calendar
    bars for the post-weekend-filter window to ever reach 120 weekday bars, so bars_in_trade plateaued
    below max_days and the time stop could never fire. See _daily()'s need_weekdays/need_raw comment."""
    mt5 = FakeMT5(with_forming(flat(5)))
    real_calendar_bars(mt5, 400)  # far more raw calendar days than entry_days+40 or max_days alone suggest
    bot, trader, msgs = make_bot(tmp_path, mt5)  # default max_days=120
    closed = bot._daily("GOLD").iloc[:-1]
    assert len(closed) >= 120, "the weekday-filtered window must hold at least max_days bars"

    ticket = 9001
    trader._tracked[ticket] = _Tracked(
        ticket=ticket, symbol="GOLD", direction="BUY", volume=0.1, entry=2000.0, sl=1990.0, tp=0.0,
        risk_money=50.0, scenario="BREAKOUT_CANDLE", opened_utc="2000-01-01T00:00:00+00:00",
    )
    mt5.positions.append(NS(ticket=ticket, identifier=ticket, symbol="GOLD", magic=CBO_MAGIC, volume=0.1, profit=0.0))
    # An entry bar older than every bar in the window makes bars_in_trade == len(closed) - the worst case
    # for the OLD formula (window capped around ~110) and a case the NEW formula comfortably covers (>=120).
    bot._state["entry_bar"][str(ticket)] = str(closed.time.iloc[0] - pd.Timedelta(days=1))

    run_pass(bot, mt5)
    assert trader.open_trades() == [], "the time stop must fire once bars_in_trade reaches max_days"
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
