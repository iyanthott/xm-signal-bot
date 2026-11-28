"""Set 3 "3rd AI" - ONE-TIME initial basket establishment, not a perpetual bot.

Places 8 market BUY orders (thematic AI-supply-chain conviction basket, buy-and-hold,
deliberately no SL/TP - a stop would work against a long-term thesis by shaking out of
normal volatility). Approved by Adrian 2026-11-28 ("go ahead and built, lets move
according to your suggestions"), sized as unleveraged real-share-equivalent exposure
(~$1,700 total / 8 names) despite this account's 1:1000 leverage - leverage is not used
to size up.

Retries the full batch every ~12 minutes until every symbol fills, then exits for good
(see scripts/run_ai3_initial_buy.ps1 for the exit-0-stops / crash-retry wrapper). Records
fills to data/ai3/positions.json and notifies Adrian's personal Telegram on completion.

    poetry run python scripts/ai3_initial_buy.py   (reads .env)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import MetaTrader5 as mt5

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_signal_bot.settings import load_secrets  # noqa: E402

TOTAL_BUDGET_USD = 1700.0
COMMENT = "3RDAI-INIT"
POSITIONS_FILE = REPO_ROOT / "data" / "ai3" / "positions.json"
RETRY_SECONDS = 12 * 60
MAX_RETRY_MINUTES = 72 * 60  # 3 days safety cap, in case something is structurally wrong

BASKET = [
    {"symbol": "Nvidia", "magic": 570050, "thesis": "AI compute core"},
    {"symbol": "Broadcom", "magic": 570051, "thesis": "custom AI ASICs"},
    {"symbol": "ASMLHolding", "magic": 570052, "thesis": "sole EUV lithography supplier"},
    {"symbol": "Teradyne", "magic": 570053, "thesis": "Universal Robots/MiR + AI-chip test equipment"},
    {"symbol": "Arista", "magic": 570054, "thesis": "AI-cluster backend networking"},
    {"symbol": "Eaton", "magic": 570055, "thesis": "data-center power infrastructure"},
    {"symbol": "ON-Semiconductor", "magic": 570056, "thesis": "AI data-center power chips"},
    {"symbol": "Palantir", "magic": 570057, "thesis": "AI software, government + commercial"},
]

PER_NAME_TARGET_USD = TOTAL_BUDGET_USD / len(BASKET)


def load_existing() -> dict:
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    return {}


def save(positions: dict) -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POSITIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(positions, indent=2), encoding="utf-8")
    tmp.replace(POSITIONS_FILE)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    import requests

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        return bool(resp.json().get("ok"))
    except Exception as exc:  # noqa: BLE001
        print(f"telegram send failed: {exc}")
        return False


def already_filled(positions: dict, symbol: str) -> bool:
    return any(p.get("symbol") == symbol for p in positions.values())


def compute_volume(symbol: str) -> tuple[float, float, float]:
    """Returns (volume, price, actual_usd)."""
    info = mt5.symbol_info(symbol)
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 1)
    price = float(rates[0]["close"]) if rates is not None and len(rates) else 0.0
    if price <= 0:
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if tick else 0.0
    contract_size = info.trade_contract_size or 1.0
    vol_min = info.volume_min
    vol_step = info.volume_step

    raw_vol = PER_NAME_TARGET_USD / (price * contract_size)
    steps = max(1, round(raw_vol / vol_step))
    volume = max(vol_min, steps * vol_step)
    volume = round(volume, 3)
    actual_usd = volume * contract_size * price
    return volume, price, actual_usd


def attempt_batch(secrets, positions: dict) -> dict:
    for entry in BASKET:
        symbol = entry["symbol"]
        if already_filled(positions, symbol):
            continue

        volume, price, actual_usd = compute_volume(symbol)
        if price <= 0:
            print(f"{symbol}: no price available, skipping this round")
            continue

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY,
            "magic": entry["magic"],
            "comment": COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            print(f"{symbol}: order_send returned None, last_error={mt5.last_error()}")
            continue

        print(
            f"{symbol}: retcode={result.retcode} comment={result.comment} "
            f"vol={volume} price~{price:.2f} target_usd={actual_usd:.2f}"
        )

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            positions[str(result.order)] = {
                "ticket": result.order,
                "symbol": symbol,
                "direction": "BUY",
                "volume": volume,
                "entry": result.price,
                "magic": entry["magic"],
                "thesis": entry["thesis"],
                "target_usd": round(PER_NAME_TARGET_USD, 2),
                "actual_usd": round(actual_usd, 2),
                "opened_utc": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "comment": COMMENT,
            }
            save(positions)
    return positions


def main() -> None:
    secrets = load_secrets(REPO_ROOT / ".env")
    chat_id = os.environ.get("XM_P2_CHAT_ID") or ""

    if not mt5.initialize():
        print("MT5 initialize failed:", mt5.last_error())
        sys.exit(1)

    positions = load_existing()
    sizing_lines = []
    start = time.monotonic()
    first_attempt = True

    while True:
        positions = attempt_batch(secrets, positions)
        remaining = [e["symbol"] for e in BASKET if not already_filled(positions, e["symbol"])]

        if first_attempt:
            sizing_lines = [
                f"{e['symbol']}: target ${PER_NAME_TARGET_USD:.2f}" for e in BASKET
            ]
            msg = (
                "[3RDAI-INIT] Initial AI basket buy attempt sent (8 names, ~$1700 total, "
                "equal-weighted, no SL/TP - buy-and-hold conviction basket).\n\n"
                + "\n".join(sizing_lines)
                + "\n\nIf today's a weekend/holiday for US equities, orders will be "
                "rejected as market-closed and retried automatically every ~12 min "
                "until filled. You'll get a second message here once all 8 are in, "
                "or if something unexpected happens."
            )
            if chat_id and secrets.telegram_bot_token:
                send_telegram(secrets.telegram_bot_token, chat_id, msg)
            first_attempt = False

        if not remaining:
            filled_lines = [
                f"{p['symbol']}: {p['volume']} lots @ {p['entry']:.2f} (~${p['actual_usd']:.2f}), magic {p['magic']}"
                for p in positions.values()
            ]
            msg = (
                "[3RDAI-INIT] All 8 AI basket positions filled.\n\n"
                + "\n".join(filled_lines)
                + "\n\nNo SL/TP set on these - intentional, buy-and-hold thematic positions."
            )
            if chat_id and secrets.telegram_bot_token:
                send_telegram(secrets.telegram_bot_token, chat_id, msg)
            print("all positions filled, exiting cleanly")
            mt5.shutdown()
            sys.exit(0)

        elapsed_minutes = (time.monotonic() - start) / 60
        if elapsed_minutes > MAX_RETRY_MINUTES:
            msg = (
                "[3RDAI-INIT] WARNING: gave up after 3 days of retrying. Still missing: "
                + ", ".join(remaining)
                + ". Check manually - something is likely structurally wrong, not just a closed market."
            )
            if chat_id and secrets.telegram_bot_token:
                send_telegram(secrets.telegram_bot_token, chat_id, msg)
            print("giving up after max retry window, exiting 1")
            mt5.shutdown()
            sys.exit(1)

        print(f"remaining: {remaining} - sleeping {RETRY_SECONDS}s")
        time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
