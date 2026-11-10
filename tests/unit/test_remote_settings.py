from __future__ import annotations

import copy
import json
import shutil
import threading
from collections import defaultdict
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from trading_signal_bot.main import TradingSignalBotApp
from trading_signal_bot.remote_settings import (
    RESTART_EXIT_CODE,
    FetchResult,
    RemoteConfig,
    RemoteSettingsClient,
    SettingsSync,
    load_remote_config,
    map_to_yaml,
)
from trading_signal_bot.settings import load_yaml_config
from trading_signal_bot.utils import utc_now

REPO_ROOT = Path(__file__).resolve().parents[2]

GOOD: dict[str, Any] = {
    "schemaVersion": 1,
    "markets": [
        {"label": "XAUUSD", "mt5Symbol": "GOLD"},
        {"label": "EURUSD", "mt5Symbol": "EURUSD"},
    ],
    "indicators": {
        "lwma": {"fast": 100, "slow": 300},
        "stochastic": {"k": 25, "d": 8, "slowing": 8, "buyZone": [0, 15], "sellZone": [85, 100]},
    },
    "sessionFilter": {"enabled": True, "windows": [{"start": "08:00", "end": "22:00"}]},
    "regimeFilter": {"enabled": True, "adxPeriod": 14, "minAdx": 20},
    "riskContext": {
        "enabled": True,
        "atrPeriod": 14,
        "atrStopMultiplier": 1.5,
        "rrTargets": [1, 3],
    },
    "strategy": {"chainEnabled": True, "legacyScenariosEnabled": False, "summaryEnabled": True},
    "signalDedup": {"cooldownMinutes": 30},
    "monitoring": {"heartbeatEnabled": True},
    "healthAlerts": {"enabled": True},
}


def bad() -> dict[str, Any]:
    s = copy.deepcopy(GOOD)
    s["indicators"]["lwma"]["fast"] = 400  # fast >= slow: the bot's own loader refuses this
    return s


class FakeClient:
    """Stands in for RemoteSettingsClient: scripted fetch results, records status reports."""

    def __init__(self, results: list[FetchResult]) -> None:
        self.results = list(results)
        self.reports: list[tuple[int, str | None, int | None]] = []
        self.have_seen: list[int | None] = []

    def fetch(self, have_version: int | None) -> FetchResult:
        self.have_seen.append(have_version)
        return self.results.pop(0) if self.results else FetchResult("unchanged", have_version)

    def report(
        self, running_version: int, error: str | None = None, rejected_version: int | None = None
    ) -> bool:
        self.reports.append((running_version, error, rejected_version))
        return True


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copy(REPO_ROOT / "config" / "settings.yaml", tmp_path / "config" / "settings.yaml")
    return tmp_path


def make_sync(project: Path, client: FakeClient) -> SettingsSync:
    return SettingsSync(project / "config" / "settings.yaml", client)  # type: ignore[arg-type]


# ------------------------------------------------------------------ config


def test_disabled_without_token() -> None:
    assert load_remote_config({}) is None
    assert load_remote_config({"XM_SETTINGS_TOKEN": "  "}) is None


def test_https_required_except_localhost() -> None:
    with pytest.raises(ValueError, match="https"):
        load_remote_config({"XM_SETTINGS_TOKEN": "t", "XM_SETTINGS_URL": "http://example.com"})
    cfg = load_remote_config(
        {"XM_SETTINGS_TOKEN": "t", "XM_SETTINGS_URL": "http://localhost:3101/"}
    )
    assert cfg is not None and cfg.base_url == "http://localhost:3101"
    default = load_remote_config({"XM_SETTINGS_TOKEN": "t"})
    assert default is not None and default.base_url.startswith("https://")
    with pytest.raises(ValueError):
        load_remote_config({"XM_SETTINGS_TOKEN": "t", "XM_SETTINGS_POLL_SECONDS": "5"})


# ------------------------------------------------------------------ merge


def test_map_overlays_editable_fields_and_keeps_local_ones() -> None:
    base = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    out = map_to_yaml(base, GOOD)
    assert out["symbols"] == {"XAUUSD": "GOLD", "EURUSD": "EURUSD"}
    assert out["indicators"]["lwma"] == {"fast": 100, "slow": 300}
    assert out["indicators"]["stochastic"]["buy_zone"] == [0, 15]
    assert out["strategy"]["enable_legacy_scenarios"] is False
    assert out["signal_dedup"]["cooldown_minutes"] == 30
    # local-only sections are untouched
    assert out["logging"] == base["logging"]
    assert out["telegram"] == base["telegram"]
    assert out["signal_dedup"]["state_file"] == base["signal_dedup"]["state_file"]
    assert out["session_filter"]["timezone"] == base["session_filter"]["timezone"]


def test_map_rejects_wrong_schema_and_malformed() -> None:
    base = {"symbols": {}}
    with pytest.raises(ValueError, match="schemaVersion"):
        map_to_yaml(base, {**GOOD, "schemaVersion": 2})
    broken = copy.deepcopy(GOOD)
    del broken["indicators"]
    with pytest.raises(ValueError, match="malformed"):
        map_to_yaml(base, broken)


# ------------------------------------------------------------------ apply / reject


def test_valid_version_is_written_validated_and_cached(project: Path) -> None:
    sync = make_sync(project, FakeClient([FetchResult("new", 2, GOOD)]))
    result = sync.poll_once()
    assert (result.kind, result.version) == ("applied", 2)
    cfg = load_yaml_config(sync.effective_path)  # the bot's real loader accepts the file
    assert cfg.indicators.lwma_fast == 100 and set(cfg.symbols) == {"XAUUSD", "EURUSD"}
    assert json.loads((project / "data" / "remote_settings_state.json").read_text())["version"] == 2
    assert not (project / "config" / ".settings.candidate.yaml").exists()


def test_invalid_version_is_rejected_and_last_good_kept(project: Path) -> None:
    client = FakeClient(
        [FetchResult("new", 2, GOOD), FetchResult("new", 3, bad()), FetchResult("new", 3, bad())]
    )
    sync = make_sync(project, client)
    assert sync.poll_once().kind == "applied"
    good_bytes = sync.effective_path.read_bytes()

    rejected = sync.poll_once()
    assert (rejected.kind, rejected.version) == ("rejected", 3)
    assert "fast" in rejected.message
    assert (
        sync.effective_path.read_bytes() == good_bytes
    ), "a rejected version must never touch the effective file"
    assert sync.latest_applied == 2

    sync.running_version = 2
    assert sync.report() is True
    assert (
        client.reports[-1][0] == 2
        and "fast" in (client.reports[-1][1] or "")
        and client.reports[-1][2] == 3
    )

    # the same bad version is not re-validated as new; a newer good one applies and clears the error
    assert sync.poll_once().kind == "rejected"
    client.results.append(FetchResult("new", 4, GOOD))
    assert sync.poll_once().kind == "applied"
    sync.report()
    assert client.reports[-1][1] is None


def test_network_error_never_raises_and_keeps_settings(project: Path) -> None:
    sync = make_sync(
        project, FakeClient([FetchResult("error", message="website unreachable: ConnectionError")])
    )
    result = sync.poll_once()
    assert result.kind == "error"
    assert not sync.effective_path.exists()


def test_startup_uses_cache_when_website_down_and_base_when_no_cache(project: Path) -> None:
    # 1) first run with website up
    s1 = make_sync(project, FakeClient([FetchResult("new", 5, GOOD)]))
    assert s1.prepare_startup() == s1.effective_path and s1.running_version == 5
    # 2) restart with website DOWN -> rebuilt from the cached version, still v5
    s2 = make_sync(project, FakeClient([FetchResult("error", message="down")]))
    assert s2.prepare_startup() == s2.effective_path and s2.running_version == 5
    # 3) brand-new PC, website down, no cache -> plain local settings.yaml
    other = project / "fresh"
    (other / "config").mkdir(parents=True)
    (other / "data").mkdir()
    shutil.copy(REPO_ROOT / "config" / "settings.yaml", other / "config" / "settings.yaml")
    s3 = make_sync(other, FakeClient([FetchResult("error", message="down")]))
    assert s3.prepare_startup() == other / "config" / "settings.yaml" and s3.running_version is None


def test_corrupt_cache_does_not_stop_startup(project: Path) -> None:
    (project / "data" / "remote_settings_state.json").write_text("{not json", encoding="utf-8")
    sync = make_sync(project, FakeClient([FetchResult("error", message="down")]))
    assert sync.prepare_startup() == project / "config" / "settings.yaml"


# ------------------------------------------------------------------ HTTP client (real local server)


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *_: Any) -> None:  # silence
        pass

    def _send(self, code: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != "Bearer good-token":
            return self._send(401, {"error": "unauthorized"})
        if "have=7" in self.path:
            return self._send(200, {"upToDate": True, "version": 7})
        self._send(200, {"version": 7, "settings": GOOD})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append({"auth": self.headers.get("Authorization"), "body": body})
        self._send(200, {"ok": True})


@pytest.fixture
def server():
    _Handler.seen.clear()
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_client_against_real_http(server: str) -> None:
    ok = RemoteSettingsClient(RemoteConfig(server, "good-token"))
    new = ok.fetch(None)
    assert (new.kind, new.version) == ("new", 7) and new.settings == GOOD
    assert ok.fetch(7).kind == "unchanged"
    assert ok.report(7, error="boom", rejected_version=8) is True
    assert _Handler.seen[-1]["auth"] == "Bearer good-token"
    assert _Handler.seen[-1]["body"] == {"runningVersion": 7, "error": "boom", "rejectedVersion": 8}

    denied = RemoteSettingsClient(RemoteConfig(server, "wrong")).fetch(None)
    assert denied.kind == "error" and "token" in denied.message
    gone = RemoteSettingsClient(RemoteConfig("http://127.0.0.1:9", "x", timeout_seconds=1)).fetch(
        None
    )
    assert gone.kind == "error" and "unreachable" in gone.message


# ------------------------------------------------------------------ restart at a quiet moment


def _bare_app() -> TradingSignalBotApp:
    app = object.__new__(TradingSignalBotApp)
    app.exit_code = 0
    app._restart_version = None
    app._restart_not_after = None
    app._shutdown_requested = False
    app._pending_setups = defaultdict(dict)
    import logging

    app._logger = logging.getLogger("test")
    return app


def test_restart_waits_for_in_flight_setup_then_fires() -> None:
    app = _bare_app()
    assert app._restart_if_due() is False  # nothing requested

    app.request_restart(9)
    app._pending_setups["XAUUSD"][("BUY", "NORMAL")] = object()  # type: ignore[index]  # an M15->M1 setup in flight
    assert app._restart_if_due() is False and app._shutdown_requested is False

    app._pending_setups["XAUUSD"].clear()  # chain resolved -> quiet moment
    assert app._restart_if_due() is True
    assert app.exit_code == RESTART_EXIT_CODE and app._shutdown_requested is True
    assert "settings v9" in app._shutdown_reason()


def test_restart_not_deferred_forever() -> None:
    app = _bare_app()
    app.request_restart(3)
    app._pending_setups["XAUUSD"][("SELL", "NORMAL")] = object()  # type: ignore[index]
    assert app._restart_if_due() is False
    app._restart_not_after = utc_now() - timedelta(seconds=1)  # the 30-minute cap has passed
    assert app._restart_if_due() is True and app.exit_code == RESTART_EXIT_CODE
