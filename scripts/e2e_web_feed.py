"""Manual end-to-end check of website sharing against a LOCAL website (http://localhost:3100). Telegram is stubbed.

1. In xm-web:  npx tsx tools/smoke/w5-e2e-setup.ts setup > fixtures.json   (local DB only), run the site on :3100
2. Here:       poetry run python scripts/e2e_web_feed.py fixtures.json
3. In xm-web:  npx tsx tools/smoke/w5-e2e-setup.ts cleanup
Proves: a real WebSignalPublisher publishes an alert; the follower's real FeedSubscriber forwards it exactly once, labelled as
shared; the stranger's feed stays empty; the owner never gets their own alert echoed back.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from trading_signal_bot.models import Direction, Scenario, Signal
from trading_signal_bot.remote_settings import RemoteConfig
from trading_signal_bot.web_feed import FeedSubscriber, WebSignalPublisher, signal_to_payload

BASE = "http://localhost:3100"


def main(path: str) -> int:
    fx = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    tmp = Path(tempfile.mkdtemp())
    got: dict[str, list[str]] = {"viewer": [], "stranger": [], "owner": []}
    subs = {
        name: FeedSubscriber(
            RemoteConfig(base_url=BASE, token=fx[f"{name}Token"]), lambda t, n=name: got[n].append(t) or True, 30, tmp / f"{name}.txt"
        )
        for name in ("viewer", "stranger", "owner")
    }
    for s in subs.values():
        s.poll_once()  # first poll: just records "from now"
    time.sleep(0.05)

    now = datetime.now(timezone.utc)
    sig = Signal(id=f"e2e-{int(time.time())}", symbol="GOLD", direction=Direction.SELL, scenario=Scenario.SELL_S1, price=2650.25,
                 created_at_utc=now, m1_bar_time_utc=now, risk_invalidation_price=2655.0, risk_tp1_price=2640.0)
    pub = WebSignalPublisher(RemoteConfig(base_url=BASE, token=fx["ownerToken"]))
    assert pub.deliver(signal_to_payload(sig)), "publish failed"
    assert pub.deliver(signal_to_payload(sig)), "idempotent re-publish failed"

    for s in subs.values():
        s.poll_once()
        s.poll_once()  # a second poll must not repeat anything
    ok = True

    def check(cond: bool, label: str) -> None:
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + label)
        ok = ok and cond

    check(len(got["viewer"]) == 1, "the follower received the shared alert exactly once")
    check(bool(got["viewer"]) and "Shared alert from e2e_owner" in got["viewer"][0] and "GOLD SELL" in got["viewer"][0] and "Price: 2650.25" in got["viewer"][0], "it is labelled as shared and carries symbol, direction, price")
    check(bool(got["viewer"]) and "Invalidation: 2655" in got["viewer"][0] and "Target 1: 2640" in got["viewer"][0], "it carries the stop/target levels")
    check(got["stranger"] == [], "the stranger (not shared, not following) received nothing")
    check(got["owner"] == [], "the owner is not sent their own alert back")
    print("\n" + ("END-TO-END OK" if ok else "END-TO-END FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
