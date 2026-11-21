from __future__ import annotations

import json
from pathlib import Path

from trading_signal_bot import risk_guard


def write_trades(path: Path, trades: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trades), encoding="utf-8")


def test_sums_only_open_trades_across_files(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    write_trades(a, {"1": {"risk_money": 50.0, "exit_reason": ""}, "2": {"risk_money": 30.0, "exit_reason": "SL"}})
    write_trades(b, {"3": {"risk_money": 20.0, "exit_reason": ""}})
    assert risk_guard.total_open_risk_money((a, b)) == 70.0


def test_missing_or_unreadable_files_are_ignored(tmp_path):
    missing = tmp_path / "missing.json"
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert risk_guard.total_open_risk_money((missing, bad)) == 0.0


def test_breaker_trips_at_or_above_cap(tmp_path):
    a = tmp_path / "a.json"
    write_trades(a, {"1": {"risk_money": 500.0, "exit_reason": ""}})  # 5% of 10000
    tripped, pct = risk_guard.breaker_tripped(10000.0, 5.0, (a,))
    assert tripped is True
    assert pct == 5.0


def test_breaker_not_tripped_below_cap(tmp_path):
    a = tmp_path / "a.json"
    write_trades(a, {"1": {"risk_money": 100.0, "exit_reason": ""}})  # 1% of 10000
    tripped, pct = risk_guard.breaker_tripped(10000.0, 5.0, (a,))
    assert tripped is False
    assert pct == 1.0


def test_cap_pct_zero_or_negative_disables_breaker(tmp_path):
    a = tmp_path / "a.json"
    write_trades(a, {"1": {"risk_money": 10000.0, "exit_reason": ""}})
    tripped, pct = risk_guard.breaker_tripped(10000.0, 0.0, (a,))
    assert tripped is False
    assert pct == 0.0
