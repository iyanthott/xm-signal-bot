from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.unit.test_dip_bot import FakeMT5, run_pass
from trading_signal_bot.fbd_bot import FBD_MAGIC, FbdBot, FbdConfig, fbd_signal, load_fbd_config
from trading_signal_bot.risk_guard import DEFAULT_STATE_FILES
from trading_signal_bot.trader import DemoTrader

ROOT = Path(__file__).resolve().parents[2]


def fresh_high_then_dip(n: int = 260) -> list[float]:
    """Uptrend making new highs, then three sharp down days -> fresh 20-day high, RSI(2) near 0, above SMA50."""
    up = [150 + 0.2 * i for i in range(n - 3)]
    return [*up, up[-1] - 0.6, up[-1] - 1.2, up[-1] - 1.8]


def make_bot(tmp_path: Path, mt5: FakeMT5, **kw) -> tuple[FbdBot, DemoTrader, list[str]]:
    msgs: list[str] = []
    cfg, trade_cfg = load_fbd_config(
        {
            "XM_FBD_ENABLED": "1",
            "XM_FBD_MARKETS": "CHFJPY=CHFJPY",
            "XM_FBD_KILL_FILE": str(tmp_path / "PAUSE"),
            "XM_FBD_STATE_FILE": str(tmp_path / "trades.json"),
        }
    )
    cfg = FbdConfig(**{**cfg.__dict__, "state_file": tmp_path / "fbd_state.json", **kw})
    trader = DemoTrader(trade_cfg, mt5, lambda s: cfg.markets.get(s, s), lambda m: msgs.append(m) or True)
    return FbdBot(cfg, trader, mt5, lambda m: msgs.append(m) or True), trader, msgs


def _signal_market(closes: list[float]) -> FakeMT5:
    mt5 = FakeMT5([*closes, closes[-1]])  # last value = forming bar
    return mt5


def test_live_signal_matches_research_signal():
    sys.path.insert(0, str(ROOT / "research"))
    import edge_lab

    rng = np.random.default_rng(5)
    close = 150 + np.cumsum(rng.normal(0.02, 0.6, 2000))
    high = close + rng.uniform(0.05, 0.8, 2000)
    low = close - rng.uniform(0.05, 0.8, 2000)
    df = pd.DataFrame({"time": pd.date_range("2015-01-01", periods=2000, freq="B"), "open": close,
                       "high": high, "low": low, "close": close})
    live = fbd_signal(df, 20, 10, 10).to_numpy()
    meta = {"spread_frac": 0.0, "swap_long_frac": 0.0, "swap_short_frac": 0.0}
    res = edge_lab.fbd_param(df, meta, 20, 10, 10)
    assert live.sum() > 20
    assert set(res.t - 1) <= set(np.flatnonzero(live))  # every research entry is a live signal day


def test_buys_the_dip_with_atr_stop_and_no_take_profit(tmp_path):
    mt5 = _signal_market(fresh_high_then_dip())
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
    req = mt5.sent[0]
    assert req["symbol"] == "CHFJPY" and req["type"] == mt5.ORDER_TYPE_BUY and req["magic"] == FBD_MAGIC
    assert req["tp"] == 0.0 and req["sl"] < req["price"]
    assert any("[FBD] SIGNAL CHFJPY BUY" in m for m in msgs)


def stale_high_then_dip() -> list[float]:
    """Peak 18 bars ago, sideways below it, then a sharp dip that stays above the 50-day average: every
    entry condition holds except 'a 20-day high within the last 10 bars'."""
    up = [4000 + 5 * i for i in range(240)]
    peak = up[-1]
    side = [peak - 20 if i % 2 else peak - 25 for i in range(15)]
    return [*up, *side, peak - 35, peak - 45, peak - 55]


def test_stale_high_case_fails_only_the_fresh_high_condition():
    from trading_signal_bot.dip_bot import _rsi

    mt5 = FakeMT5([*stale_high_then_dip(), 0])
    df = pd.DataFrame(mt5.rates[:-1])
    c = df.close
    assert c.iloc[-1] > c.rolling(50).mean().iloc[-1] and _rsi(c, 2).iloc[-1] < 10
    assert not fbd_signal(df).iloc[-1]


def test_no_entry_without_a_fresh_high(tmp_path):
    mt5 = _signal_market(stale_high_then_dip())
    bot, _, _ = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_exits_on_close_above_previous_high(tmp_path):
    closes = fresh_high_then_dip()
    mt5 = _signal_market(closes)
    bot, trader, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert len(trader.open_trades()) == 1
    mt5.set_bars([*closes, closes[-1] + 15, closes[-1] + 15])  # yesterday closed far above the prior high
    run_pass(bot, mt5)
    close_req = mt5.sent[-1]
    assert close_req.get("position") and close_req["type"] == mt5.ORDER_TYPE_SELL
    bot.step()
    assert any("previous day's high" in m for m in msgs if "TRADE CLOSED" in m)


def test_time_stop_after_max_days(tmp_path):
    closes = fresh_high_then_dip()
    mt5 = _signal_market(closes)
    bot, trader, msgs = make_bot(tmp_path, mt5, max_days=3)
    run_pass(bot, mt5)
    weak = closes[:]
    for day in range(1, 4):
        weak = [*weak, weak[-1] - 0.01]
        mt5.set_bars([*weak, weak[-1]])
        run_pass(bot, mt5)
        if day < 3:
            assert trader.open_trades(), f"closed too early on day {day}"
    bot.step()
    assert any("time stop" in m for m in msgs if "TRADE CLOSED" in m)


def test_refuses_real_account_and_honours_kill_file(tmp_path):
    mt5 = FakeMT5([*fresh_high_then_dip(), 0], mode=2)
    mt5.rates["close"][-1] = mt5.rates["close"][-2]
    bot, _, msgs = make_bot(tmp_path, mt5)
    run_pass(bot, mt5)
    assert mt5.sent == [] and any("not a DEMO" in m for m in msgs)

    mt5b = _signal_market(fresh_high_then_dip())
    (tmp_path / "PAUSE").write_text("x")
    bot_b, _, _ = make_bot(tmp_path, mt5b)
    run_pass(bot_b, mt5b)
    assert mt5b.sent == []


def test_config_defaults_and_validation():
    cfg, trade = load_fbd_config({})
    assert trade.enabled is False and trade.risk_pct == 0.5 and trade.magic == FBD_MAGIC and trade.rr == 0.0
    assert cfg.markets == {"CHFJPY": "CHFJPY", "GBPJPY": "GBPJPY", "EURJPY": "EURJPY"}
    assert (cfg.high_lookback, cfg.recent_bars, cfg.rsi2_below, cfg.max_days) == (20, 10, 10.0, 5)
    with pytest.raises(ValueError):
        load_fbd_config({"XM_FBD_RISK_PCT": "5"})


def test_fbd_trades_count_toward_account_risk_cap():
    assert Path("data/fbd/trades.json") in DEFAULT_STATE_FILES
