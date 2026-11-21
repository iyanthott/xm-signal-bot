from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS  # noqa: N814

import pytest
from test_dip_bot import FakeMT5

from trading_signal_bot.trader import DemoTrader
from trading_signal_bot.vbo_bot import VBO_MAGIC, VboBot, VboConfig, load_vbo_config


class FakeMT5Vbo(FakeMT5):
    """Decouples the live tick price from the forming bar's open, so tests can move price
    intraday without retroactively changing today's already-fixed open (which the bot used
    to compute today's breakout levels)."""

    def __init__(self, closes: list[float], mode: int = 0) -> None:
        super().__init__(closes, mode)
        self.price: float | None = None

    def set_price(self, price: float) -> None:
        self.price = price

    def symbol_info_tick(self, _s):
        price = self.price if self.price is not None else float(self.rates["close"][-1])
        return NS(ask=price + 0.5, bid=price, time=self.tick_time)


def flat(n: int = 40, level: float = 2000.0) -> list[float]:
    return [level] * n


def with_forming(closes: list[float]) -> list[float]:
    return [*closes, closes[-1]]


def make_bot(tmp_path: Path, mt5: FakeMT5Vbo, **kw) -> tuple[VboBot, DemoTrader, list[str]]:
    msgs: list[str] = []
    cfg, trade_cfg = load_vbo_config(
        {
            "XM_VBO_ENABLED": "1",
            "XM_VBO_MARKETS": "ETH=ETHUSD",
            "XM_VBO_KILL_FILE": str(tmp_path / "PAUSE"),
            "XM_VBO_STATE_FILE": str(tmp_path / "trades.json"),
        }
    )
    cfg = VboConfig(**{**cfg.__dict__, "state_file": tmp_path / "vbo_state.json", **kw})
    trader = DemoTrader(
        trade_cfg, mt5, lambda s: cfg.markets.get(s, s), lambda m: msgs.append(m) or True
    )
    return VboBot(cfg, trader, mt5, lambda m: msgs.append(m) or True), trader, msgs


def run_pass(bot: VboBot, mt5: FakeMT5Vbo) -> None:
    bot.step()
    mt5.advance_tick()
    bot.step()


def test_buys_when_price_crosses_todays_upper_level(tmp_path):
    closes = flat()
    mt5 = FakeMT5Vbo(with_forming(closes))
    bot, _, msgs = make_bot(tmp_path, mt5)
    mt5.set_price(closes[-1] + 6)  # open(2000) + 0.5 x range(10) = 2005; 2006 crosses it
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
    req = mt5.sent[0]
    assert req["symbol"] == "ETHUSD" and req["type"] == mt5.ORDER_TYPE_BUY
    assert req["magic"] == VBO_MAGIC and req["tp"] == 0.0 and req["sl"] < req["price"]
    assert any("[VBO] SIGNAL ETH BUY" in m for m in msgs)


def test_sells_when_price_crosses_todays_lower_level(tmp_path):
    closes = flat()
    mt5 = FakeMT5Vbo(with_forming(closes))
    bot, _, msgs = make_bot(tmp_path, mt5)
    mt5.set_price(closes[-1] - 6)  # open(2000) - 0.5 x range(10) = 1995; 1994 crosses it
    run_pass(bot, mt5)
    req = mt5.sent[0]
    assert req["type"] == mt5.ORDER_TYPE_SELL and req["sl"] > req["price"]
    assert any("SIGNAL ETH SELL" in m for m in msgs)


def test_no_entry_while_price_stays_inside_todays_levels(tmp_path):
    closes = flat()
    mt5 = FakeMT5Vbo(with_forming(closes))
    bot, _, _ = make_bot(tmp_path, mt5)
    mt5.set_price(closes[-1] + 2)  # inside +/- 5
    run_pass(bot, mt5)
    assert mt5.sent == []


def test_skips_entry_when_both_levels_are_touched_in_the_same_check(tmp_path):
    """Regression test: level_up/level_dn used to be checked with if/elif, so a quote straddling both
    (ask past level_up AND bid past level_dn at once - e.g. a wide-spread quote during a gap) always
    resolved to BUY, contradicting the ambiguous-bar skip rule the backtest itself uses
    (research/daily_lab.py::simulate_vbo)."""
    closes = flat()
    mt5 = FakeMT5Vbo(with_forming(closes))
    bot, _, msgs = make_bot(tmp_path, mt5)
    # level_up = 2005, level_dn = 1995 (open 2000 +/- 0.5 x range 10); force a quote whose ask is past
    # level_up AND whose bid is past level_dn at the same time, unlike FakeMT5Vbo.set_price's fixed 0.5 spread.
    mt5.symbol_info_tick = lambda _s: NS(ask=2010.0, bid=1990.0, time=mt5.tick_time)
    run_pass(bot, mt5)
    assert mt5.sent == [], "an ambiguous double-touch must not pick a direction"

    # the day is not marked "entered" by the skip, so a later, unambiguous touch still trades normally
    mt5.symbol_info_tick = lambda _s: NS(ask=2010.0, bid=2009.5, time=mt5.tick_time)
    mt5.advance_tick()
    bot.step()
    assert len(mt5.sent) == 1 and mt5.sent[0]["type"] == mt5.ORDER_TYPE_BUY
    assert any("[VBO] SIGNAL ETH BUY" in m for m in msgs)


def test_only_one_entry_attempt_per_day(tmp_path):
    closes = flat()
    mt5 = FakeMT5Vbo(with_forming(closes))
    bot, trader, _ = make_bot(tmp_path, mt5)
    mt5.set_price(closes[-1] + 6)
    run_pass(bot, mt5)
    assert len(mt5.sent) == 1
    trader.close_position(trader.open_trades()[0].ticket, "manual close for test")
    mt5.set_price(closes[-1] + 7)  # still the same day; should not re-enter
    mt5.advance_tick()
    bot.step()
    assert len(mt5.sent) == 2  # the manual close only; no second entry order


def test_time_stop_after_max_days(tmp_path):
    closes = flat()
    mt5 = FakeMT5Vbo(with_forming(closes))
    bot, trader, msgs = make_bot(tmp_path, mt5, max_days=3)
    mt5.set_price(closes[-1] + 6)
    run_pass(bot, mt5)
    assert trader.open_trades()
    for day in range(1, 4):
        closes = [*closes, closes[-1]]
        mt5.set_bars(with_forming(closes))
        mt5.set_price(closes[-1])  # stay inside the new day's levels; no second entry
        run_pass(bot, mt5)
        if day < 3:
            assert trader.open_trades(), f"closed too early on day {day}"
    assert trader.open_trades() == []
    assert any("time stop" in m for m in msgs if "TRADE CLOSED" in m)


def test_refuses_real_account_and_honours_kill_file(tmp_path):
    closes = flat()
    mt5 = FakeMT5Vbo(with_forming(closes), mode=2)
    bot, _, msgs = make_bot(tmp_path, mt5)
    mt5.set_price(closes[-1] + 6)
    run_pass(bot, mt5)
    assert mt5.sent == [] and any("not a DEMO" in m for m in msgs)

    mt5b = FakeMT5Vbo(with_forming(closes))
    (tmp_path / "PAUSE").write_text("x")
    bot_b, _, _ = make_bot(tmp_path, mt5b)
    mt5b.set_price(closes[-1] + 6)
    run_pass(bot_b, mt5b)
    assert mt5b.sent == []


def test_config_defaults_and_validation():
    cfg, trade = load_vbo_config({})
    assert trade.enabled is False and trade.risk_pct == 0.5 and trade.magic == VBO_MAGIC
    assert trade.rr == 0.0 and trade.label == "[VBO]"
    assert cfg.markets == {"ETH": "ETHUSD"}
    assert (cfg.k, cfg.stop_atr, cfg.max_days) == (0.5, 2.0, 5)
    with pytest.raises(ValueError):
        load_vbo_config({"XM_VBO_RISK_PCT": "5"})
    with pytest.raises(ValueError):
        load_vbo_config({"XM_VBO_K": "5"})
