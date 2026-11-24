"""Weekly performance report: pulls REAL closed trades from MT5 history (not the local trades.json
files, which only ever hold open positions) for the last 7 days, grouped by bot (magic number), and
sends a plain-text summary to the same Telegram chat the bots already alert to.

Scheduled via Windows Task Scheduler (task name: XM-WeeklyReport), weekly, Monday 08:00 local time
(the PC's system clock is already Singapore Standard Time = UTC+8 = Asia/Manila, no DST either side,
so this lines up with the Manila clock rule without any extra conversion).

Run manually:
    poetry run python scripts/weekly_report.py             # last 7 days
    poetry run python scripts/weekly_report.py --days 14   # custom window
    poetry run python scripts/weekly_report.py --dry-run   # print instead of sending to Telegram
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

MANILA = ZoneInfo("Asia/Manila")

# Only the live fleet. Bots 1/2 (magic 570031/570032) were retired 2026-11-24 - deliberately excluded
# so a stray leftover deal from them doesn't pollute a report about the active strategies.
BOT_LABELS = {
    570033: "Dip",
    570034: "Trend",
    570035: "VBO",
    570036: "CBO",
}


def _stats(deals: list) -> dict:
    n = len(deals)
    if n == 0:
        return {"n": 0}
    pnls = [d["pnl"] for d in deals]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_pct": round(100 * len(wins) / n, 1),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "net": round(sum(pnls), 2),
    }


def build_report(days: int) -> str:
    import MetaTrader5 as mt5  # noqa: N813  (local import: only needed when actually running)

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")

    info = mt5.account_info()
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(days=days)
    deals = mt5.history_deals_get(start_utc, now_utc) or []

    by_bot: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        if d.entry != mt5.DEAL_ENTRY_OUT:
            continue
        label = BOT_LABELS.get(d.magic)
        if label is None:
            continue  # retired bots / manual deals - not part of this report
        by_bot[label].append({
            "time": datetime.fromtimestamp(d.time, tz=timezone.utc).astimezone(MANILA),
            "symbol": d.symbol,
            "pnl": d.profit + d.swap + d.commission,
        })

    positions = mt5.positions_get() or []
    open_by_bot: dict[str, list] = defaultdict(list)
    for p in positions:
        label = BOT_LABELS.get(p.magic)
        if label:
            open_by_bot[label].append(p)

    mt5.shutdown()

    now_manila = now_utc.astimezone(MANILA)
    lines = [
        f"Weekly Report — {now_manila.strftime('%Y-%m-%d %H:%M')} Manila",
        f"Window: last {days} days",
        f"Balance: ${info.balance:,.2f}  Equity: ${info.equity:,.2f}",
        "",
    ]

    total_n, total_net = 0, 0.0
    for label in BOT_LABELS.values():
        rec = _stats(by_bot.get(label, []))
        open_n = len(open_by_bot.get(label, []))
        if rec["n"] == 0:
            lines.append(f"[{label}] 0 closed this week, {open_n} open")
            continue
        total_n += rec["n"]
        total_net += rec["net"]
        lines.append(
            f"[{label}] {rec['n']} closed, {rec['wins']}W/{rec['losses']}L "
            f"({rec['win_pct']}% win), PF {rec['pf']}, net {rec['net']:+.2f}, {open_n} open"
        )
    lines.append("")
    lines.append(f"TOTAL: {total_n} closed trades, net {total_net:+.2f}")
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_dotenv(Path(__file__).parent.parent / ".env")
    report = build_report(args.days)
    print(report)
    if not args.dry_run:
        send_telegram(report)
