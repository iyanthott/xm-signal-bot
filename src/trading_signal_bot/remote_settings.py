"""Pull bot settings from the Poging Kita website (pogingkita.stayatdiez.com) and apply them safely.

Flow (SIGNALS_PLAN.md, phase P2):
  website (versioned settings, Poging Kita web app)  --outbound HTTPS, bearer token-->  this module
  -> merge onto the local base settings.yaml -> validate with the bot's own loader
  -> write config/settings.effective.yaml -> bot restarts at a quiet moment (supervisor script)

Safety rules:
  * Nothing here can place trades; it only reads settings and reports status.
  * A version the bot's loader rejects is NEVER applied; the last good version keeps running and the
    rejection is reported back to the website (shown to the editor).
  * Network/website failures never stop the bot: it keeps running on its last good settings.
  * Only the editable fields (markets, indicators, filters, SL/TP, alert options) come from the website.
    Paths, logging, Telegram queue, MT5/Telegram credentials stay local.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml

from trading_signal_bot.settings import load_yaml_config

DEFAULT_URL = "https://pogingkita.stayatdiez.com"
SETTINGS_PATH = "/api/bot/settings"
STATUS_PATH = "/api/bot/status"
SCHEMA_VERSION = 1
RESTART_EXIT_CODE = 75  # EX_TEMPFAIL: the supervisor script restarts the bot on this code


@dataclass(frozen=True)
class RemoteConfig:
    base_url: str
    token: str
    poll_seconds: int = 60
    timeout_seconds: float = 10.0


def load_remote_config(env: Mapping[str, str | None]) -> RemoteConfig | None:
    """Enabled only when XM_SETTINGS_TOKEN is set. Refuses plain http except to localhost (token would travel in clear)."""
    token = (env.get("XM_SETTINGS_TOKEN") or "").strip()
    if not token:
        return None
    url = (env.get("XM_SETTINGS_URL") or DEFAULT_URL).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"XM_SETTINGS_URL is not a valid URL: {url!r}")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError(
            "XM_SETTINGS_URL must use https (the token must not travel over plain http)"
        )
    try:
        poll = int(env.get("XM_SETTINGS_POLL_SECONDS") or 60)
    except ValueError as exc:
        raise ValueError("XM_SETTINGS_POLL_SECONDS must be a whole number") from exc
    if poll < 15:
        raise ValueError("XM_SETTINGS_POLL_SECONDS must be at least 15")
    return RemoteConfig(base_url=url, token=token, poll_seconds=poll)


# --------------------------------------------------------------------------- HTTP client


@dataclass(frozen=True)
class FetchResult:
    kind: str  # "new" | "unchanged" | "error"
    version: int | None = None
    settings: dict[str, Any] | None = None
    message: str = ""


class RemoteSettingsClient:
    def __init__(self, cfg: RemoteConfig, session: requests.Session | None = None) -> None:
        self._cfg = cfg
        self._http = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {cfg.token}", "Accept": "application/json"}

    def fetch(self, have_version: int | None) -> FetchResult:
        params = {"have": str(have_version)} if have_version is not None else None
        try:
            resp = self._http.get(
                self._cfg.base_url + SETTINGS_PATH,
                headers=self._headers,
                params=params,
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException as exc:
            return FetchResult("error", message=f"website unreachable: {type(exc).__name__}")
        if resp.status_code == 401:
            return FetchResult(
                "error", message="website rejected the token (revoked or wrong XM_SETTINGS_TOKEN)"
            )
        if resp.status_code != 200:
            return FetchResult("error", message=f"website returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            return FetchResult("error", message="website returned invalid JSON")
        if not isinstance(body, dict):
            return FetchResult("error", message="website returned an unexpected payload")
        if body.get("upToDate") is True:
            return FetchResult("unchanged", version=_as_int(body.get("version")))
        version = _as_int(body.get("version"))
        settings = body.get("settings")
        if version is None or not isinstance(settings, dict):
            return FetchResult("error", message="website payload is missing version/settings")
        return FetchResult("new", version=version, settings=settings)

    def report(
        self, running_version: int, error: str | None = None, rejected_version: int | None = None
    ) -> bool:
        body: dict[str, Any] = {"runningVersion": running_version}
        if error:
            body["error"] = error[:1000]
            if rejected_version is not None:
                body["rejectedVersion"] = rejected_version
        try:
            resp = self._http.post(
                self._cfg.base_url + STATUS_PATH,
                headers=self._headers,
                json=body,
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException:
            return False
        return resp.status_code == 200


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------- merge


def map_to_yaml(base: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    """Overlay the website's editable fields onto the local base settings. Pure; raises ValueError if malformed."""
    if remote.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported settings schemaVersion {remote.get('schemaVersion')!r} (bot understands {SCHEMA_VERSION})"
        )
    out = copy.deepcopy(base)
    try:
        out["symbols"] = {m["label"]: m["mt5Symbol"] for m in remote["markets"]}

        ind = out.setdefault("indicators", {})
        lw, st = remote["indicators"]["lwma"], remote["indicators"]["stochastic"]
        ind.setdefault("lwma", {}).update(fast=lw["fast"], slow=lw["slow"])
        ind.setdefault("stochastic", {}).update(
            k=st["k"],
            d=st["d"],
            slowing=st["slowing"],
            buy_zone=list(st["buyZone"]),
            sell_zone=list(st["sellZone"]),
        )

        sf = out.setdefault("session_filter", {})
        sf.setdefault("timezone", "Asia/Phnom_Penh")  # timezone is local-only
        sf["enabled"] = remote["sessionFilter"]["enabled"]
        sf["windows"] = [
            {"start": w["start"], "end": w["end"]} for w in remote["sessionFilter"]["windows"]
        ]

        rf, rc = remote["regimeFilter"], remote["riskContext"]
        out.setdefault("regime_filter", {}).update(
            enabled=rf["enabled"], adx_period=rf["adxPeriod"], min_adx=rf["minAdx"]
        )
        out.setdefault("risk_context", {}).update(
            enabled=rc["enabled"],
            atr_period=rc["atrPeriod"],
            atr_stop_multiplier=rc["atrStopMultiplier"],
            rr_targets=list(rc["rrTargets"]),
        )

        sg = remote["strategy"]
        strat = out.setdefault("strategy", {})
        strat["enable_legacy_scenarios"] = sg["legacyScenariosEnabled"]
        strat.setdefault("chain", {})["enabled"] = sg["chainEnabled"]
        strat.setdefault("summary", {})["enabled"] = sg["summaryEnabled"]

        out.setdefault("signal_dedup", {})["cooldown_minutes"] = remote["signalDedup"][
            "cooldownMinutes"
        ]
        out.setdefault("monitoring", {})["heartbeat_enabled"] = remote["monitoring"][
            "heartbeatEnabled"
        ]
        out.setdefault("health_alerts", {})["enabled"] = remote["healthAlerts"]["enabled"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"remote settings are malformed: {type(exc).__name__}: {exc}") from exc
    return out


# --------------------------------------------------------------------------- apply


@dataclass(frozen=True)
class PollResult:
    kind: str  # "unchanged" | "applied" | "rejected" | "error"
    version: int | None = None
    message: str = ""


class SettingsSync:
    """Owns the effective settings file and the small state file next to it. All validation uses the bot's own loader."""

    def __init__(
        self, base_path: Path, client: RemoteSettingsClient, logger: logging.Logger | None = None
    ) -> None:
        self._base = base_path.resolve()
        self._client = client
        self._log = logger or logging.getLogger("RemoteSettings")
        config_dir = self._base.parent
        project_root = config_dir.parent if config_dir.name == "config" else config_dir
        self.effective_path = config_dir / "settings.effective.yaml"
        self._candidate = config_dir / ".settings.candidate.yaml"
        self._state_path = project_root / "data" / "remote_settings_state.json"
        self.running_version: int | None = None  # version the CURRENT process started with
        self.latest_applied: int | None = None  # newest version written to the effective file
        self._rejected_version: int | None = None
        self._last_error: str | None = None
        self._outage_logged = False

    # -- startup ---------------------------------------------------------

    def prepare_startup(self) -> Path:
        """Return the settings file the bot should load now. Never raises: falls back to the local base file."""
        state = self._read_state()
        if state:
            try:
                self._build_effective(state["settings"])
                self.running_version = self.latest_applied = int(state["version"])
            except Exception as exc:
                self._log.warning("cached remote settings unusable (%s); using local settings", exc)
        result = self.poll_once(startup=True)
        if result.kind == "applied":
            self.running_version = result.version
        path = self.effective_path if self.running_version is not None else self._base
        self._log.info(
            "settings source: %s (website version %s)",
            "website" if self.running_version is not None else "local settings.yaml",
            self.running_version,
        )
        return path

    # -- polling ---------------------------------------------------------

    def poll_once(self, startup: bool = False) -> PollResult:
        res = self._client.fetch(self.latest_applied)
        if res.kind == "error":
            if not self._outage_logged:
                self._log.warning("settings sync: %s — continuing on current settings", res.message)
                self._outage_logged = True
            self._last_error = None
            return PollResult("error", message=res.message)
        if self._outage_logged:
            self._log.info("settings sync: website reachable again")
            self._outage_logged = False
        if res.kind == "unchanged":
            return PollResult("unchanged", res.version)

        assert res.version is not None and res.settings is not None
        if res.version == self._rejected_version:
            return PollResult(
                "rejected", res.version, self._last_error or ""
            )  # already reported; wait for a newer version
        try:
            self._build_effective(res.settings)
        except Exception as exc:
            self._rejected_version = res.version
            self._last_error = f"{type(exc).__name__}: {exc}"[:900]
            self._log.error(
                "settings v%s REJECTED, keeping last good: %s", res.version, self._last_error
            )
            return PollResult("rejected", res.version, self._last_error)
        self._rejected_version = None
        self._last_error = None
        self.latest_applied = res.version
        self._write_state(res.version, res.settings)
        self._log.info(
            "settings v%s validated and written (%s)",
            res.version,
            "startup" if startup else "restart pending",
        )
        return PollResult("applied", res.version)

    def report(self) -> bool:
        """Tell the website which version this process runs and any rejection. Best-effort."""
        running = self.running_version if self.running_version is not None else 0
        return self._client.report(
            running, self._last_error, self._rejected_version if self._last_error else None
        )

    # -- internals -------------------------------------------------------

    def _build_effective(self, remote_settings: dict[str, Any]) -> None:
        with self._base.open("r", encoding="utf-8") as fh:
            base = yaml.safe_load(fh)
        merged = map_to_yaml(base, remote_settings)
        try:
            with self._candidate.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(merged, fh, sort_keys=False, allow_unicode=True)
            load_yaml_config(
                self._candidate
            )  # the bot's own validation; raises on any invalid value
            os.replace(self._candidate, self.effective_path)
        finally:
            self._candidate.unlink(missing_ok=True)

    def _read_state(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return (
                data
                if isinstance(data, dict)
                and "version" in data
                and isinstance(data.get("settings"), dict)
                else None
            )
        except (OSError, ValueError):
            return None

    def _write_state(self, version: int, settings: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": version, "settings": settings}), encoding="utf-8")
        os.replace(tmp, self._state_path)


# --------------------------------------------------------------------------- background poller


class RemotePoller(threading.Thread):
    """Polls every `interval` seconds; calls `on_new_version(version)` when a valid new version has been written."""

    def __init__(
        self, sync: SettingsSync, interval: float, on_new_version: Callable[[int], None]
    ) -> None:
        super().__init__(name="remote-settings", daemon=True)
        self._sync = sync
        self._interval = interval
        self._cb = on_new_version
        self._stop_event = threading.Event()

    def run(self) -> None:
        log = logging.getLogger("RemoteSettings")
        self._sync.report()  # announce the running version right away
        while not self._stop_event.wait(self._interval):
            try:
                result = self._sync.poll_once()
                if result.kind == "applied" and result.version is not None:
                    self._cb(result.version)
                self._sync.report()  # also serves as the "bot is online" heartbeat
            except Exception:
                log.exception("settings poller iteration failed")

    def stop(self) -> None:
        self._stop_event.set()
