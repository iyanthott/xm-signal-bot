"""Report this bot's own MT5 account snapshot and trades to the Poging Kita website (W7 dashboard). OFF by
default; enabled by setting the bot's web-report token (a separate Bot/token on the website from the settings
one - see docs). Reuses XM_SETTINGS_URL. State pushes are self-healing: open positions and the account snapshot
are simply re-sent every cycle with whatever is current, so there is no retry queue for them (unlike
WebSignalPublisher, which guards one-shot alerts). Closed trades are one-shot events, so they ARE buffered and
retried until the website confirms them. Never raises; a website outage never affects trading or Telegram.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from trading_signal_bot.remote_settings import DEFAULT_URL
from trading_signal_bot.trader import ClosedTrade, DemoTrader

ACCOUNT_PATH = "/api/bot/account"
TRADES_PATH = "/api/bot/trades"
MAX_BUFFERED_CLOSED = 100
LOG = logging.getLogger("WebTradeReporter")


@dataclass(frozen=True)
class WebReportConfig:
    base_url: str
    token: str
    timeout_seconds: float = 10.0


def load_web_report_config(env: Mapping[str, str | None], token_var: str) -> WebReportConfig | None:
    """Enabled only when `token_var` is set (e.g. XM_DIP_WEB_TOKEN). Each bot process has its own token, since
    each is its own Bot on the website. Refuses plain http except to localhost, same rule as settings."""
    token = (env.get(token_var) or "").strip()
    if not token:
        return None
    url = (env.get("XM_SETTINGS_URL") or DEFAULT_URL).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"XM_SETTINGS_URL is not a valid URL: {url!r}")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("XM_SETTINGS_URL must use https (the token must not travel over plain http)")
    return WebReportConfig(base_url=url, token=token)


def _open_trade_payload(t: Any) -> dict[str, Any]:
    return {
        "ticket": str(t.ticket), "symbol": t.symbol, "direction": t.direction, "volume": t.volume,
        "entryPrice": t.entry, "stopLoss": t.sl or None, "takeProfit": t.tp or None, "riskMoney": t.risk_money,
        "scenario": t.scenario, "openedAt": t.opened_utc, "closedAt": None, "exitPrice": None, "pnl": None,
        "exitReason": None,
    }


def _closed_trade_payload(t: ClosedTrade) -> dict[str, Any]:
    return {
        "ticket": str(t.ticket), "symbol": t.symbol, "direction": t.direction, "volume": t.volume,
        "entryPrice": t.entry, "stopLoss": t.sl or None, "takeProfit": t.tp or None, "riskMoney": t.risk_money,
        "scenario": t.scenario, "openedAt": t.opened_utc, "closedAt": t.closed_utc,
        "exitPrice": t.exit_price, "pnl": t.pnl, "exitReason": t.exit_reason or None,
    }


class WebTradeReporter:
    """`on_closed` is passed straight to DemoTrader(on_closed=...); `report(trader, mt5)` is called periodically
    from each bot's main loop. Both are best-effort: a failed HTTP call is logged and never raised."""

    def __init__(self, cfg: WebReportConfig, session: requests.Session | None = None) -> None:
        self._cfg = cfg
        self._http = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {cfg.token}", "Accept": "application/json"}
        self._pending_closed: list[ClosedTrade] = []
        self._log = LOG

    def on_closed(self, trade: ClosedTrade) -> None:
        self._pending_closed.append(trade)
        if len(self._pending_closed) > MAX_BUFFERED_CLOSED:
            self._pending_closed = self._pending_closed[-MAX_BUFFERED_CLOSED:]

    def report(self, trader: DemoTrader, mt5: Any) -> None:
        try:
            self._report_account(mt5)
        except Exception:
            self._log.exception("reporting the account snapshot failed")
        try:
            self._report_trades(trader)
        except Exception:
            self._log.exception("reporting trades failed")

    def _report_account(self, mt5: Any) -> None:
        info = mt5.account_info()
        if info is None:
            return
        payload = {
            "balance": float(info.balance),
            "equity": float(info.equity),
            "margin": float(info.margin),
            "marginFree": float(info.margin_free),
            "currency": str(info.currency),
            "reportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        self._post(ACCOUNT_PATH, payload)

    def _report_trades(self, trader: DemoTrader) -> None:
        payload = [_open_trade_payload(t) for t in trader.open_trades()]
        payload += [_closed_trade_payload(t) for t in self._pending_closed]
        if not payload:
            return
        if self._post(TRADES_PATH, payload):
            self._pending_closed = []

    def _post(self, path: str, payload: Any) -> bool:
        try:
            resp = self._http.post(
                self._cfg.base_url + path, headers=self._headers, json=payload, timeout=self._cfg.timeout_seconds
            )
        except requests.RequestException as exc:
            self._log.warning("website unreachable for %s: %s", path, type(exc).__name__)
            return False
        if resp.status_code != 200:
            self._log.warning("website rejected %s: HTTP %s %s", path, resp.status_code, resp.text[:200])
            return False
        return True
