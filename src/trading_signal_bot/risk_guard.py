"""Account-wide open-risk circuit breaker, shared by every DemoTrader instance on this MT5
account (main, M1, Dip, Trend, VBO, CBO all run their own DemoTrader with their own state_file).

Each bot persists its own open trades (ticket -> risk_money, exit_reason) in its own
state_file. This sums risk_money across every known state file for trades still open
(exit_reason empty) and compares the total against the account balance. One bot tripping the
breaker only blocks NEW entries - everywhere - until total open risk drops back under the cap;
it never touches any bot's existing positions.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE_FILES: tuple[Path, ...] = (
    Path("data/trades.json"),
    Path("data/m1/trades.json"),
    Path("data/dip/trades.json"),
    Path("data/trend/trades.json"),
    Path("data/vbo/trades.json"),
    Path("data/cbo/trades.json"),
)


def total_open_risk_money(state_files: tuple[Path, ...] = DEFAULT_STATE_FILES) -> float:
    total = 0.0
    for path in state_files:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for trade in raw.values():
            if not trade.get("exit_reason"):
                total += float(trade.get("risk_money") or 0.0)
    return total


def breaker_tripped(
    balance: float, cap_pct: float, state_files: tuple[Path, ...] = DEFAULT_STATE_FILES
) -> tuple[bool, float]:
    """Returns (tripped, current open-risk % of balance). cap_pct <= 0 disables the breaker."""
    if cap_pct <= 0 or balance <= 0:
        return False, 0.0
    open_pct = total_open_risk_money(state_files) / balance * 100.0
    return open_pct >= cap_pct, open_pct
