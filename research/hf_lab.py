"""High-frequency feasibility lab (Adrian asked for a 10+ trades/day bot, 2026-11-26).

Research only. GOLD and NAS100 only - the two markets where spread is smallest relative to intraday moves
(spread ~6-7% of a median M15 range vs 22-80% on FX). Six classic scalping families, fixed rule shapes,
SL/TP/hold chosen on the first 70% of bars only, reported on the untouched last 30%.

Fill/cost model = strategy_lab.simulate: signal on a closed bar, entry next bar's open, buys pay the spread
in, sells pay it out, SL and TP touched in one bar -> SL. Spread per bar = max(1.2 x the bar's own recorded
spread, strategy_lab's typical active-hours spread x 1.2). Signals only inside the active session.

    poetry run python research/hf_lab.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy_lab import Result, atr, cost, ema, load, point, rsi, simulate

SESSION = {"GOLD": (10, 22), "NAS100": (16.5, 22.5)}  # server hours where signals may fire


def _session_mask(df: pd.DataFrame, key: str) -> np.ndarray:
    a, b = SESSION[key]
    h = df.time.dt.hour + df.time.dt.minute / 60
    return ((h >= a) & (h < b)).to_numpy()


def signals(df: pd.DataFrame, name: str) -> np.ndarray:
    c, h, lo = df.close, df.high, df.low
    if name == "ema_cross":
        f, s, t = ema(c, 9), ema(c, 21), ema(c, 200)
        up = (f > s) & (f.shift() <= s.shift()) & (c > t)
        dn = (f < s) & (f.shift() >= s.shift()) & (c < t)
    elif name == "bb_revert":
        m, sd = c.rolling(20).mean(), c.rolling(20).std()
        up, dn = c < m - 2 * sd, c > m + 2 * sd
    elif name == "donchian":
        up, dn = c > h.rolling(20).max().shift(), c < lo.rolling(20).min().shift()
    elif name == "twap_revert":
        day = df.time.dt.date
        tp = (h + lo + c) / 3
        twap = tp.groupby(day).cumsum() / tp.groupby(day).cumcount().add(1)
        dev = c - twap
        sd = dev.rolling(100).std()
        up, dn = dev < -2 * sd, dev > 2 * sd
    elif name == "rsi2_trend":
        r, t = rsi(c, 2), ema(c, 200)
        up, dn = (r < 10) & (c > t), (r > 90) & (c < t)
    elif name == "big_bar_fade":
        a = atr(df, 14).shift()
        rng, body = h - lo, c - df.open
        up = (rng > 2.5 * a) & (body < 0)
        dn = (rng > 2.5 * a) & (body > 0)
    else:
        raise ValueError(name)
    return np.where(up, 1, np.where(dn, -1, 0))


FAMILIES = ["ema_cross", "bb_revert", "donchian", "twap_revert", "rsi2_trend", "big_bar_fade"]
GRID = {"sl_atr": [1.0, 2.0], "rr": [1.0, 2.0], "hold": [6, 12]}


def run(key: str, tf: str) -> list[dict]:
    df = load(key, tf)
    spreads = np.maximum(1.2 * df.spread.to_numpy() * point(key), cost(key, df))
    sess = _session_mask(df, key)
    split = int(len(df) * 0.7)
    days_tr = df.time.iloc[:split].dt.date.nunique()
    days_te = df.time.iloc[split:].dt.date.nunique()
    rows = []
    for fam in FAMILIES:
        sig = np.where(sess, signals(df, fam), 0)
        best = None
        for sl, rr, hold in itertools.product(*GRID.values()):
            res = simulate(df, sig, sl, rr, spreads, hold)
            tr = Result(res.r[res.t < split], res.t[res.t < split]).stats()
            if tr.get("n", 0) < 50:
                continue
            score = tr["expR"] * np.sqrt(tr["n"])
            if best is None or score > best[0]:
                best = (score, sl, rr, hold, res, tr)
        if best is None:
            continue
        _, sl, rr, hold, res, tr = best
        te = Result(res.r[res.t >= split], res.t[res.t >= split]).stats()
        rows.append({"market": key, "tf": tf, "family": fam, "sl_atr": sl, "rr": rr, "hold": hold,
                     "trades/day": round(te.get("n", 0) / max(days_te, 1), 1),
                     "train_n": tr["n"], "train_win%": tr["win%"], "train_PF": tr["PF"], "train_expR": tr["expR"],
                     "test_n": te.get("n", 0), "test_win%": te.get("win%"), "test_PF": te.get("PF"),
                     "test_expR": te.get("expR"), "train_days": days_tr, "test_days": days_te})
        print(f"  {key:6s} {tf:3s} {fam:12s} sl{sl} rr{rr} hold{hold:2d} | {rows[-1]['trades/day']:4.1f}/day | "
              f"TRAIN n{tr['n']} win{tr['win%']} PF{tr['PF']} expR{tr['expR']} | "
              f"TEST n{te.get('n')} win{te.get('win%')} PF{te.get('PF')} expR{te.get('expR')}", flush=True)
    return rows


if __name__ == "__main__":
    out = []
    for key in ("GOLD", "NAS100"):
        for tf in ("M5", "M15"):
            out += run(key, tf)
    pd.DataFrame(out).to_csv("data/lab/hf_results.csv", index=False)
    print("\nwrote data/lab/hf_results.csv")
