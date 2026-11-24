"""Research only (not used by live bots): tests whether Pradeep Bonde's ("StockBee") two public setups
hold up on XM's US-stock CFDs, at realistic cost, with the same train/test discipline as daily_lab.py.

  momentum_burst : a single day's close is >=4% above the prior close on >=1.5x the 20-day average
                    volume, with the broader trend already up (close > SMA200) -> buy next open
                    (mirror: <=-4% on expanding volume, close < SMA200 -> sell). Thesis (per third-party
                    write-ups of Bonde's method): the move keeps running for a few more days. Exit: close
                    back through SMA5, or 5 days, whichever comes first.
  episodic_pivot : a gap of >=10% at the open vs the PRIOR close, on >=2x the 20-day average volume,
                    after a quiet prior stretch (20-day high/low range <=15% of price -- a proxy for "has
                    been neglected") -> buy next open (mirror: gap down -> sell). This is the "Delayed
                    Reaction" variant (enter the day after the gap, not into the gap itself): the plain
                    reaction would require intrabar volume to confirm the catalyst before the bar closes,
                    which this daily-bar backtest cannot see without look-ahead, and third-party sources
                    describe it as the safer entry anyway. Exit: close back through SMA20, or 40 days.

Volume is MT5 "tick_volume" (count of price changes), NOT real traded share volume -- XM is a CFD
broker, not the exchange. This is a necessary proxy; a pass here is conditional on that caveat, not
a confirmation that real volume would behave the same way.

Costs: same model as daily_lab.py (1.5x spread, real swap charges, conservative floors for stocks).

    poetry run python research/momentum_burst_lab.py fetch
    poetry run python research/momentum_burst_lab.py run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from daily_lab import UNIVERSE, _conservative, _swap_frac  # noqa: E402
from strategy_lab import Result, atr, rsi  # noqa: E402

D1 = Path("data/lab/d1_mb")
STOCKS = UNIVERSE["stock"]


# ---------------------------------------------------------------- data
def fetch(bars: int) -> None:
    import MetaTrader5 as mt5  # noqa: N813

    if not mt5.initialize():
        sys.exit(f"MT5 initialize failed: {mt5.last_error()}")
    D1.mkdir(parents=True, exist_ok=True)
    meta_path = D1 / "meta.json"
    meta: dict[str, dict] = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    for sym in STOCKS:
        if sym in meta:
            continue
        if not mt5.symbol_select(sym, True):
            print(f"{sym}: not selectable")
            continue
        info = mt5.symbol_info(sym)
        r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 0, bars)
        if r is None or len(r) < 300 or info is None:
            print(f"{sym}: too little history ({0 if r is None else len(r)} bars)")
            continue
        df = pd.DataFrame(r)[["time", "open", "high", "low", "close", "tick_volume", "spread"]]
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df[df.time.dt.dayofweek < 5]
        df.to_csv(D1 / f"{sym}.csv", index=False)
        price = float(df.close.iloc[-1])
        spread_pts = max(float(info.spread), float(df.spread.iloc[-60:].median()))
        meta[sym] = {
            "class": "stock",
            "spread_frac": 1.5 * spread_pts * info.point / price,
            "swap_long_frac": _swap_frac(info, info.swap_long, price),
            "swap_short_frac": _swap_frac(info, info.swap_short, price),
            "first": str(df.time.iloc[0].date()),
            "bars": len(df),
        }
        print(f"{sym:16s} {len(df):5d} bars from {df.time.iloc[0].date()}  "
              f"spread {meta[sym]['spread_frac']*1e4:.1f}bp", flush=True)
        meta_path.write_text(json.dumps(meta, indent=1))
    mt5.shutdown()


# ---------------------------------------------------------------- signals
def signals(df: pd.DataFrame, strat: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Returns (entry direction, exit-long flag, exit-short flag, max_days)."""
    o, h, lo, c, v = df.open, df.high, df.low, df.close, df.tick_volume
    sma200, sma5, sma20 = c.rolling(200).mean(), c.rolling(5).mean(), c.rolling(20).mean()
    avg_vol20 = v.rolling(20).mean().shift()  # prior 20 days, excludes today (no look-ahead)
    if strat == "momentum_burst":
        day_ret = c / c.shift() - 1
        vol_ok = v >= 1.5 * avg_vol20
        up = (day_ret >= 0.04) & vol_ok & (c > sma200)
        dn = (day_ret <= -0.04) & vol_ok & (c < sma200)
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, (c < sma5).to_numpy(), (c > sma5).to_numpy(), 5
    if strat == "episodic_pivot":
        gap = o / c.shift() - 1
        vol_ok = v >= 2.0 * avg_vol20
        prior_range = (h.rolling(20).max().shift() - lo.rolling(20).min().shift()) / c.shift()
        quiet = prior_range <= 0.15
        up = (gap >= 0.10) & vol_ok & quiet
        dn = (gap <= -0.10) & vol_ok & quiet
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, (c < sma20).to_numpy(), (c > sma20).to_numpy(), 40
    raise ValueError(strat)


def simulate(df: pd.DataFrame, strat: str, meta: dict) -> Result:
    entry, exit_l, exit_s, max_days = signals(df, strat)
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df.time.to_numpy()
    a = atr(df, 14).to_numpy()
    spread_now = meta["spread_frac"]
    out_r, out_t = [], []
    i, n = 200, len(df) - 1
    while i < n:
        d = entry[i]
        if d == 0 or not np.isfinite(a[i]) or a[i] <= 0:
            i += 1
            continue
        j = i + 1
        px = o[j]
        dist = 2.0 * a[i]
        stop = px - dist if d == 1 else px + dist
        k, exit_px = j, None
        while k < n:
            if d == 1 and lo[k] <= stop:
                exit_px = min(stop, o[k])
                break
            if d == -1 and h[k] >= stop:
                exit_px = max(stop, o[k])
                break
            if (exit_l[k] if d == 1 else exit_s[k]) or k - j + 1 >= max_days:
                exit_px = o[k + 1]
                k += 1
                break
            k += 1
        if exit_px is None:
            break
        nights = int((t[k] - t[j]) / np.timedelta64(1, "D"))
        swap = (meta["swap_long_frac"] if d == 1 else meta["swap_short_frac"]) * px * max(nights, 0)
        cost = spread_now * px + swap
        pnl = (exit_px - px) * d - cost
        out_r.append(pnl / dist)
        out_t.append(j)
        i = k + 1
    return Result(np.array(out_r), np.array(out_t))


STRATS = ["momentum_burst", "episodic_pivot"]


def run(test_frac: float) -> None:
    meta_all = {k: _conservative(v) for k, v in json.loads((D1 / "meta.json").read_text()).items()}
    rows = []
    for sym, m in meta_all.items():
        df = pd.read_csv(D1 / f"{sym}.csv", parse_dates=["time"])
        if len(df) < 300:
            continue
        split_time = df.time.iloc[int(len(df) * (1 - test_frac))]
        years = (df.time.iloc[-1] - df.time.iloc[0]).days / 365.25
        for strat in STRATS:
            res = simulate(df, strat, m)
            is_test = df.time.to_numpy()[res.t] >= split_time.to_datetime64() if res.n else np.array([], bool)
            tr = Result(res.r[~is_test], res.t[~is_test]).stats()
            te = Result(res.r[is_test], res.t[is_test]).stats()
            al = res.stats()
            rows.append({
                "symbol": sym, "strategy": strat, "from": m["first"], "years": round(years, 1),
                "n": al.get("n", 0), "trades_yr": round(al.get("n", 0) / max(years, 0.1), 1),
                "win%": al.get("win%"), "PF": al.get("PF"), "expR": al.get("expR"), "maxDD_R": al.get("maxDD_R"),
                "train_n": tr.get("n", 0), "train_PF": tr.get("PF"), "train_expR": tr.get("expR"),
                "test_n": te.get("n", 0), "test_PF": te.get("PF"), "test_win%": te.get("win%"), "test_expR": te.get("expR"),
            })
    out = pd.DataFrame(rows)
    out.to_csv(D1.parent / "momentum_burst_results.csv", index=False)

    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 300)
    print("\n=== ALL RESULTS (stock universe, Bonde-style rules) ===")
    print(out.sort_values(["strategy", "test_PF"], ascending=[True, False]).to_string(index=False))

    ok = out[(out.n >= 40) & (out.test_n >= 15) & (out.train_expR > 0) & (out.test_expR > 0)
             & (out.train_PF >= 1.1) & (out.test_PF >= 1.2)]
    print("\n=== PASSED (same bar as daily_lab.py: >=40 trades, positive both periods, train PF>=1.1, test PF>=1.2) ===")
    print(ok.sort_values(["strategy", "test_PF"], ascending=[True, False]).to_string(index=False) if len(ok) else "(none)")

    print("\n=== POOLED by strategy (all 40 stocks, no cherry-picking) ===")
    pooled = []
    for strat, g in out.groupby("strategy"):
        pooled.append({"strategy": strat, "markets": len(g), "trades": int(g.n.sum()),
                       "avg_expR": round(float((g.expR.fillna(0) * g.n).sum() / max(g.n.sum(), 1)), 3),
                       "markets_positive_both": int(((g.train_expR > 0) & (g.test_expR > 0)).sum())})
    print(pd.DataFrame(pooled).to_string(index=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "run"])
    ap.add_argument("--bars", type=int, default=7000)
    ap.add_argument("--test-frac", type=float, default=0.3)
    a = ap.parse_args()
    fetch(a.bars) if a.cmd == "fetch" else run(a.test_frac)
