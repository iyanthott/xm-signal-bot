from __future__ import annotations

from types import SimpleNamespace as NS  # noqa: N814
from typing import Any

import pytest
import requests

from trading_signal_bot.trader import ClosedTrade
from trading_signal_bot.web_report import WebReportConfig, WebTradeReporter, load_web_report_config

CFG = WebReportConfig(base_url="https://example.test", token="xmbot_abc")


class FakeResp:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = ""


class FakeSession:
    def __init__(self, responses: list[Any], settings: Any = None) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.settings: Any = {"version": 3} if settings is None else settings

    def post(self, url: str, **kw: Any) -> FakeResp:
        self.calls.append({"url": url, **kw})
        r = self.responses.pop(0) if self.responses else FakeResp(200)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url: str, **kw: Any) -> Any:
        self.gets.append(url)
        if isinstance(self.settings, Exception):
            raise self.settings
        return NS(status_code=200, json=lambda: self.settings)


def posts_to(s: FakeSession, path: str) -> list[dict[str, Any]]:
    return [c for c in s.calls if c["url"].endswith(path)]


def make_closed(ticket: int = 1, **over: Any) -> ClosedTrade:
    base: dict[str, Any] = dict(
        ticket=ticket, symbol="GOLD", direction="BUY", volume=0.1, entry=2000.0, sl=1995.0, tp=2007.5,
        risk_money=50.0, scenario="BUY_S1", opened_utc="2026-11-20T00:00:00+00:00",
        closed_utc="2026-11-21T00:00:00+00:00", exit_price=2010.0, pnl=100.0, exit_reason="take profit",
    )
    base.update(over)
    return ClosedTrade(**base)


class FakeTrader:
    def __init__(self, open_trades: list[Any]) -> None:
        self._open = open_trades

    def open_trades(self) -> list[Any]:
        return self._open


def make_open(ticket: int = 1) -> Any:
    return NS(ticket=ticket, symbol="GOLD", direction="BUY", volume=0.1, entry=2000.0, sl=1995.0, tp=2007.5,
              risk_money=50.0, scenario="BUY_S1", opened_utc="2026-11-20T00:00:00+00:00")


class FakeMt5:
    def __init__(self, info: Any) -> None:
        self._info = info

    def account_info(self) -> Any:
        return self._info


ACCT = NS(balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0, currency="USD")


# ----------------------------------------------------------------------------- config

def test_disabled_without_a_token_and_rejects_plain_http() -> None:
    assert load_web_report_config({}, "XM_DIP_WEB_TOKEN") is None
    cfg = load_web_report_config({"XM_DIP_WEB_TOKEN": "xmbot_x", "XM_SETTINGS_URL": "https://example.test"}, "XM_DIP_WEB_TOKEN")
    assert cfg is not None and cfg.token == "xmbot_x" and cfg.base_url == "https://example.test"
    with pytest.raises(ValueError):
        load_web_report_config({"XM_DIP_WEB_TOKEN": "xmbot_x", "XM_SETTINGS_URL": "http://not-localhost"}, "XM_DIP_WEB_TOKEN")
    ok = load_web_report_config({"XM_DIP_WEB_TOKEN": "xmbot_x", "XM_SETTINGS_URL": "http://localhost:3000"}, "XM_DIP_WEB_TOKEN")
    assert ok is not None


# ----------------------------------------------------------------------------- reporting

def test_reports_account_and_open_trades() -> None:
    s = FakeSession([FakeResp(200), FakeResp(200)])
    r = WebTradeReporter(CFG, session=s)
    trader = FakeTrader([make_open(1)])
    r.report(trader, FakeMt5(ACCT))
    assert s.calls[0]["url"] == "https://example.test/api/bot/account"
    assert s.calls[0]["headers"]["Authorization"] == "Bearer xmbot_abc"
    assert s.calls[0]["json"]["balance"] == 10000.0 and s.calls[0]["json"]["currency"] == "USD"
    assert s.calls[1]["url"] == "https://example.test/api/bot/trades"
    assert s.calls[1]["json"] == [{
        "ticket": "1", "symbol": "GOLD", "direction": "BUY", "volume": 0.1, "entryPrice": 2000.0,
        "stopLoss": 1995.0, "takeProfit": 2007.5, "riskMoney": 50.0, "scenario": "BUY_S1",
        "openedAt": "2026-11-20T00:00:00+00:00", "closedAt": None, "exitPrice": None, "pnl": None, "exitReason": None,
    }]


def test_no_account_info_or_no_trades_sends_only_the_check_in() -> None:
    s = FakeSession([])
    r = WebTradeReporter(CFG, session=s)
    r.report(FakeTrader([]), FakeMt5(None))
    assert [c["url"] for c in s.calls] == ["https://example.test/api/bot/status"]


def test_status_check_in_echoes_the_site_settings_version_and_caches_it() -> None:
    s = FakeSession([], settings={"version": 3})
    r = WebTradeReporter(CFG, session=s)
    r.report(FakeTrader([]), FakeMt5(ACCT))
    r.report(FakeTrader([]), FakeMt5(ACCT))
    status = posts_to(s, "/api/bot/status")
    assert [c["json"] for c in status] == [{"runningVersion": 3}, {"runningVersion": 3}]
    assert status[0]["headers"]["Authorization"] == "Bearer xmbot_abc"
    assert s.gets == ["https://example.test/api/bot/settings"], "version is cached, not fetched every minute"


def test_status_check_in_still_sent_when_settings_unreadable() -> None:
    s = FakeSession([], settings=requests.ConnectionError("down"))
    r = WebTradeReporter(CFG, session=s)
    r.report(FakeTrader([]), FakeMt5(None))
    assert [c["json"] for c in posts_to(s, "/api/bot/status")] == [{"runningVersion": 0}]


def test_closed_trades_are_buffered_via_on_closed_and_cleared_only_once_delivered() -> None:
    s = FakeSession([FakeResp(200), FakeResp(503)])  # account ok, trades push fails
    r = WebTradeReporter(CFG, session=s)
    r.on_closed(make_closed(ticket=9))
    r.report(FakeTrader([]), FakeMt5(ACCT))
    assert len(r._pending_closed) == 1, "a failed push keeps the closed trade for retry"

    s2 = FakeSession([FakeResp(200), FakeResp(200)])
    r2 = WebTradeReporter(CFG, session=s2)
    r2.on_closed(make_closed(ticket=9))
    r2.report(FakeTrader([]), FakeMt5(ACCT))
    assert len(r2._pending_closed) == 0, "delivered successfully, buffer cleared"
    sent = s2.calls[1]["json"][0]
    assert sent["ticket"] == "9" and sent["pnl"] == 100.0 and sent["exitReason"] == "take profit" and sent["closedAt"] == "2026-11-21T00:00:00+00:00"


def test_network_and_server_errors_never_raise() -> None:
    s = FakeSession([requests.ConnectionError("down"), requests.Timeout("slow")])
    r = WebTradeReporter(CFG, session=s)
    r.report(FakeTrader([make_open()]), FakeMt5(ACCT))  # must not raise


def test_buffer_is_capped_so_a_long_outage_cannot_grow_memory_without_bound() -> None:
    r = WebTradeReporter(CFG, session=FakeSession([]))
    for i in range(150):
        r.on_closed(make_closed(ticket=i))
    assert len(r._pending_closed) == 100
    assert r._pending_closed[0].ticket == 50, "oldest entries are dropped first"
