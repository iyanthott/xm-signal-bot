"""Optional website sharing for the alert-only bot (Poging Kita W5.3 / W5.4). Two independent, OFF-by-default features:

* WebSignalPublisher  (XM_PUBLISH_SIGNALS=1): after an alert has been sent to the owner's Telegram, publish the same alert to
  the website so it shows in the owner's Feed and reaches the people the owner chose to share it with. Runs on its own thread
  with its own retry queue: a website outage can never delay, drop or fail a Telegram alert.
* FeedSubscriber      (XM_FOLLOW_FEED=1): poll the website for alerts shared by bots this person follows and forward them to
  THIS person's own Telegram chat. Telegram credentials never leave this PC; the website never holds them.

Both reuse the settings token (XM_SETTINGS_TOKEN / XM_SETTINGS_URL), so the website decides which bot is talking from the token.
Nothing here can place a trade.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from trading_signal_bot.models import Signal
from trading_signal_bot.remote_settings import RemoteConfig

SIGNALS_PATH = "/api/bot/signals"
FEED_PATH = "/api/bot/feed"
_TRUE = {"1", "true", "yes", "on"}


def _flag(env: Mapping[str, str | None], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in _TRUE


@dataclass(frozen=True)
class FeedConfig:
    publish: bool = False
    follow: bool = False
    feed_poll_seconds: int = 30
    cursor_file: Path = Path("data/feed_cursor.txt")


def load_feed_config(env: Mapping[str, str | None]) -> FeedConfig:
    try:
        poll = int(env.get("XM_FEED_POLL_SECONDS") or 30)
    except ValueError as exc:
        raise ValueError("XM_FEED_POLL_SECONDS must be a whole number") from exc
    if poll < 15:
        raise ValueError("XM_FEED_POLL_SECONDS must be at least 15")
    return FeedConfig(
        publish=_flag(env, "XM_PUBLISH_SIGNALS"),
        follow=_flag(env, "XM_FOLLOW_FEED"),
        feed_poll_seconds=poll,
        cursor_file=Path(env.get("XM_FEED_CURSOR_FILE") or "data/feed_cursor.txt"),
    )


def signal_to_payload(signal: Signal) -> dict[str, Any]:
    """The same fields the owner sees in Telegram. No settings, indicators values or account data."""
    return {
        "signalId": signal.id,
        "symbol": signal.symbol,
        "direction": signal.direction.value,
        "scenario": signal.scenario.value,
        "price": signal.price,
        "createdAtUtc": signal.created_at_utc.isoformat().replace("+00:00", "Z"),
        "riskStopDistance": signal.risk_stop_distance,
        "riskInvalidationPrice": signal.risk_invalidation_price,
        "riskTp1Price": signal.risk_tp1_price,
        "riskTp2Price": signal.risk_tp2_price,
    }


# --------------------------------------------------------------------------- publisher

_RETRY_DELAYS = (2.0, 5.0, 15.0, 30.0, 60.0)


class WebSignalPublisher:
    """publish() never blocks and never raises; a worker thread delivers with retries."""

    def __init__(
        self,
        cfg: RemoteConfig,
        session: requests.Session | None = None,
        max_queue: int = 200,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._http = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {cfg.token}", "Accept": "application/json"}
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._sleep = sleeper or (lambda s: self._stop.wait(s))
        self._log = logging.getLogger("WebSignalPublisher")
        self._thread = threading.Thread(target=self._run, name="web-signal-publisher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def publish(self, signal: Signal) -> None:
        try:
            self._queue.put_nowait(signal_to_payload(signal))
        except queue.Full:
            self._log.warning("publish queue full; dropping signal %s", signal.id)
        except Exception:  # never let sharing break the alert path
            self._log.exception("could not queue signal for the website")

    def queue_size(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.deliver(payload)
            except Exception:
                self._log.exception("publishing %s failed unexpectedly", payload.get("signalId"))

    def deliver(self, payload: dict[str, Any]) -> bool:
        """Try up to len(_RETRY_DELAYS)+1 times. 4xx (except 429) means the data is bad or the token dead: do not retry."""
        for attempt in range(len(_RETRY_DELAYS) + 1):
            status = self._post_once(payload)
            if status == 200:
                return True
            if status is not None and status != 429 and status < 500:
                self._log.warning(
                    "website refused signal %s (HTTP %s); not retrying",
                    payload.get("signalId"),
                    status,
                )
                return False
            if attempt < len(_RETRY_DELAYS):
                self._sleep(_RETRY_DELAYS[attempt])
                if self._stop.is_set():
                    return False
        self._log.warning("gave up publishing signal %s after retries", payload.get("signalId"))
        return False

    def _post_once(self, payload: dict[str, Any]) -> int | None:
        try:
            resp = self._http.post(
                self._cfg.base_url + SIGNALS_PATH,
                headers=self._headers,
                json=payload,
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException:
            return None
        return resp.status_code


# --------------------------------------------------------------------------- subscriber


def _num(value: object) -> str | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:.5f}".rstrip("0").rstrip(".")
    return None


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value)[:limit].split())  # one line: newlines/tabs/repeated spaces collapse


def format_shared_text(item: Mapping[str, Any]) -> str:
    """Plain text (no markup, so nothing in a shared alert can inject formatting). Always labelled as shared."""
    lines = [
        f"Shared alert from {_clean(item.get('ownerName', '?'), 60)} ({_clean(item.get('botName', '?'), 60)})",
        f"{_clean(item.get('symbol', '?'), 40)} {_clean(item.get('direction', '?'), 10)}  {_clean(item.get('scenario', ''), 30).replace('_', ' ')}",
    ]
    for label, key in (
        ("Price", "price"),
        ("Invalidation", "riskInvalidationPrice"),
        ("Target 1", "riskTp1Price"),
        ("Target 2", "riskTp2Price"),
    ):
        text = _num(item.get(key))
        if text is not None:
            lines.append(f"{label}: {text}")
    lines.append("Alert only. Not advice. This bot never trades.")
    return "\n".join(lines)


class FeedSubscriber(threading.Thread):
    """Polls the feed and hands each shared alert to `notify` (this person's own Telegram). Delivers each alert at most once."""

    def __init__(
        self,
        cfg: RemoteConfig,
        notify: Callable[[str], bool],
        interval: float,
        cursor_file: Path,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(name="web-feed-subscriber", daemon=True)
        self._cfg = cfg
        self._notify = notify
        self._interval = interval
        self._cursor_file = cursor_file
        self._http = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {cfg.token}", "Accept": "application/json"}
        self._stop_event = threading.Event()
        self._sent: OrderedDict[str, None] = OrderedDict()
        self._log = logging.getLogger("FeedSubscriber")

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                self._log.exception("feed poll failed")
            if self._stop_event.wait(self._interval):
                break

    def _read_cursor(self) -> str | None:
        try:
            text = self._cursor_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def _write_cursor(self, cursor: str) -> None:
        try:
            self._cursor_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cursor_file.with_suffix(".tmp")
            tmp.write_text(cursor, encoding="utf-8")
            tmp.replace(self._cursor_file)
        except OSError:
            self._log.warning(
                "could not save the feed position; shared alerts may repeat after a restart"
            )

    def poll_once(self) -> int:
        """Returns the number of alerts forwarded this round."""
        cursor = self._read_cursor()
        params = {"cursor": cursor} if cursor else None
        try:
            resp = self._http.get(
                self._cfg.base_url + FEED_PATH,
                headers=self._headers,
                params=params,
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException as exc:
            self._log.warning("website feed unreachable: %s", type(exc).__name__)
            return 0
        if resp.status_code == 401:
            self._log.error(
                "website rejected the token for the feed (revoked or wrong XM_SETTINGS_TOKEN)"
            )
            return 0
        if resp.status_code != 200:
            self._log.warning("website feed returned HTTP %s", resp.status_code)
            return 0
        try:
            body = resp.json()
        except ValueError:
            return 0
        if not isinstance(body, dict):
            return 0
        new_cursor = body.get("cursor")
        items = body.get("items")
        if not isinstance(new_cursor, str) or not isinstance(items, list):
            return 0

        forwarded = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", ""))
            if item_id in self._sent:
                continue
            if not self._notify(format_shared_text(item)):
                self._log.warning(
                    "could not forward a shared alert to Telegram; will retry on the next poll"
                )
                return forwarded  # keep the old cursor: the rest (and this one) come again, finished ones are skipped by id
            self._sent[item_id] = None
            while len(self._sent) > 500:
                self._sent.popitem(last=False)
            forwarded += 1
        if new_cursor != cursor:
            self._write_cursor(new_cursor)
        return forwarded
