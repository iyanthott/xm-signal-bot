from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.remote_settings import RemoteConfig
from trading_signal_bot.web_feed import (
    FeedSubscriber,
    WebSignalPublisher,
    format_shared_text,
    load_feed_config,
    signal_to_payload,
)

CFG = RemoteConfig(base_url="https://example.test", token="xmbot_abc", poll_seconds=60)


def make_signal(**over: Any) -> Signal:
    now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    base: dict[str, Any] = dict(
        id="sig-1",
        symbol="GOLD",
        direction=Direction.BUY,
        scenario=Scenario.BUY_S1,
        price=2650.5,
        created_at_utc=now,
        m1_bar_time_utc=now,
        risk_stop_distance=5.5,
        risk_invalidation_price=2645.0,
        risk_tp1_price=2660.0,
        risk_tp2_price=2670.0,
    )
    base.update(over)
    return Signal(**base)


class FakeResp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Scripted responses; records every call. A response may be an Exception to raise."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _next(self, kind: str, url: str, **kw: Any) -> FakeResp:
        self.calls.append({"kind": kind, "url": url, **kw})
        r = self.responses.pop(0) if self.responses else FakeResp(200, {})
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url: str, **kw: Any) -> FakeResp:
        return self._next("post", url, **kw)

    def get(self, url: str, **kw: Any) -> FakeResp:
        return self._next("get", url, **kw)


# ----------------------------------------------------------------------------- config


def test_features_are_off_by_default_and_flags_parse() -> None:
    cfg = load_feed_config({})
    assert cfg.publish is False and cfg.follow is False
    on = load_feed_config(
        {"XM_PUBLISH_SIGNALS": "1", "XM_FOLLOW_FEED": "True", "XM_FEED_POLL_SECONDS": "45"}
    )
    assert on.publish and on.follow and on.feed_poll_seconds == 45
    assert load_feed_config({"XM_PUBLISH_SIGNALS": "no"}).publish is False
    with pytest.raises(ValueError):
        load_feed_config({"XM_FEED_POLL_SECONDS": "5"})
    with pytest.raises(ValueError):
        load_feed_config({"XM_FEED_POLL_SECONDS": "abc"})


def test_payload_has_only_the_alert_fields() -> None:
    p = signal_to_payload(make_signal())
    assert set(p) == {
        "signalId",
        "symbol",
        "direction",
        "scenario",
        "price",
        "createdAtUtc",
        "riskStopDistance",
        "riskInvalidationPrice",
        "riskTp1Price",
        "riskTp2Price",
    }
    assert p["direction"] == "BUY" and p["scenario"] == "BUY_S1" and p["createdAtUtc"].endswith("Z")


# ----------------------------------------------------------------------------- publisher


def test_publish_posts_to_the_signals_endpoint_with_the_token() -> None:
    s = FakeSession([FakeResp(200, {"ok": True})])
    pub = WebSignalPublisher(CFG, session=s, sleeper=lambda _s: None)  # type: ignore[arg-type]
    assert pub.deliver(signal_to_payload(make_signal())) is True
    call = s.calls[0]
    assert call["url"] == "https://example.test/api/bot/signals"
    assert call["headers"]["Authorization"] == "Bearer xmbot_abc"
    assert call["json"]["signalId"] == "sig-1"


def test_retries_server_errors_and_network_errors_then_succeeds() -> None:
    sleeps: list[float] = []
    s = FakeSession(
        [
            FakeResp(503),
            requests.ConnectionError("down"),
            FakeResp(429),
            FakeResp(200, {"ok": True}),
        ]
    )
    pub = WebSignalPublisher(CFG, session=s, sleeper=sleeps.append)  # type: ignore[arg-type]
    assert pub.deliver(signal_to_payload(make_signal())) is True
    assert len(s.calls) == 4 and len(sleeps) == 3


def test_client_errors_are_not_retried_and_gives_up_after_the_retry_budget() -> None:
    s = FakeSession([FakeResp(400)])
    pub = WebSignalPublisher(CFG, session=s, sleeper=lambda _s: None)  # type: ignore[arg-type]
    assert pub.deliver({"signalId": "x"}) is False
    assert len(s.calls) == 1  # no retry on 4xx
    s2 = FakeSession([FakeResp(401)])
    assert WebSignalPublisher(CFG, session=s2, sleeper=lambda _s: None).deliver({"signalId": "x"}) is False  # type: ignore[arg-type]
    assert len(s2.calls) == 1
    s3 = FakeSession([FakeResp(500)] * 10)
    assert WebSignalPublisher(CFG, session=s3, sleeper=lambda _s: None).deliver({"signalId": "x"}) is False  # type: ignore[arg-type]
    assert len(s3.calls) == 6  # first try + 5 retries


def test_publish_never_blocks_or_raises_even_when_the_queue_is_full() -> None:
    pub = WebSignalPublisher(CFG, session=FakeSession([]), max_queue=2)  # type: ignore[arg-type]
    for i in range(10):
        pub.publish(
            make_signal(id=f"s{i}")
        )  # worker not started: queue fills, extra are dropped quietly
    assert pub.queue_size() == 2


# ----------------------------------------------------------------------------- subscriber


def item(i: int, **over: Any) -> dict[str, Any]:
    d = {
        "id": f"id-{i}",
        "botName": "Gold bot",
        "ownerName": "Adrian",
        "symbol": "GOLD",
        "direction": "BUY",
        "scenario": "BUY_S1",
        "price": 2650.5,
        "riskInvalidationPrice": 2645,
        "riskTp1Price": 2660,
        "riskTp2Price": None,
        "createdAtUtc": "2026-11-10T08:00:00Z",
    }
    d.update(over)
    return d


def make_sub(tmp: Path, responses: list[Any], notify: Any) -> tuple[FeedSubscriber, FakeSession]:
    s = FakeSession(responses)
    sub = FeedSubscriber(CFG, notify, 30, tmp / "cursor.txt", session=s)  # type: ignore[arg-type]
    return sub, s


def test_first_poll_only_stores_the_start_position(tmp_path: Path) -> None:
    sent: list[str] = []
    sub, s = make_sub(
        tmp_path,
        [FakeResp(200, {"items": [], "cursor": "2026-11-10T08:00:00.000Z|0000"})],
        lambda t: sent.append(t) or True,
    )
    assert sub.poll_once() == 0 and sent == []
    assert (tmp_path / "cursor.txt").read_text() == "2026-11-10T08:00:00.000Z|0000"
    assert s.calls[0]["params"] is None


def test_forwards_in_order_labelled_and_advances_the_cursor(tmp_path: Path) -> None:
    (tmp_path / "cursor.txt").write_text("c0")
    sent: list[str] = []
    sub, s = make_sub(
        tmp_path,
        [
            FakeResp(
                200,
                {"items": [item(1), item(2, symbol="US100Cash", direction="SELL")], "cursor": "c2"},
            )
        ],
        lambda t: sent.append(t) or True,
    )
    assert sub.poll_once() == 2
    assert s.calls[0]["params"] == {"cursor": "c0"}
    assert (
        sent[0].startswith("Shared alert from Adrian (Gold bot)")
        and "GOLD BUY" in sent[0]
        and "Price: 2650.5" in sent[0]
    )
    assert "Target 2" not in sent[0], "missing values are omitted, not shown as None"
    assert "US100Cash SELL" in sent[1]
    assert "Not advice" in sent[0]
    assert (tmp_path / "cursor.txt").read_text() == "c2"


def test_a_failed_telegram_send_keeps_the_cursor_and_nothing_is_sent_twice(tmp_path: Path) -> None:
    (tmp_path / "cursor.txt").write_text("c0")
    sent: list[str] = []
    ok = {"value": False}

    def notify(text: str) -> bool:
        if not ok["value"] and "id-2" in text:
            return False
        sent.append(text)
        return True

    batch = {"items": [item(1), item(2, symbol="id-2"), item(3)], "cursor": "c3"}
    sub, _ = make_sub(tmp_path, [FakeResp(200, batch), FakeResp(200, batch)], notify)
    assert sub.poll_once() == 1  # item 1 sent, item 2 failed -> stop, cursor unchanged
    assert (tmp_path / "cursor.txt").read_text() == "c0"
    ok["value"] = True
    assert sub.poll_once() == 2  # item 1 is skipped (already sent), 2 and 3 go out
    assert len(sent) == 3 and (tmp_path / "cursor.txt").read_text() == "c3"


def test_network_errors_bad_status_and_bad_payloads_are_harmless(tmp_path: Path) -> None:
    sent: list[str] = []
    for resp in (
        requests.ConnectionError("x"),
        FakeResp(401),
        FakeResp(500),
        FakeResp(200),
        FakeResp(200, ["not", "a", "dict"]),
        FakeResp(200, {"items": "no", "cursor": 5}),
    ):
        sub, _ = make_sub(tmp_path, [resp], lambda t: sent.append(t) or True)
        assert sub.poll_once() == 0
    assert sent == [] and not (tmp_path / "cursor.txt").exists()


def test_shared_text_is_plain_and_bounded() -> None:
    text = format_shared_text(
        item(1, ownerName="Eve\n*bold* [x](http://evil)" + "z" * 200, symbol="G" * 100)
    )
    assert "\n*bold*" not in text.splitlines()[0]
    assert len(text.splitlines()[0]) < 160 and len(text.splitlines()[1]) < 100
