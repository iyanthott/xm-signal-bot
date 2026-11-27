"""M1 feasibility lab (Adrian asked "how about trading M1?", 2026-11-26).

Research only. Data: ~3.3 months of M1 per market (2026-08-17 -> 2026-11-26), the most the terminal serves at
its 100,000 max-bars setting - so any "pass" here is provisional and needs more history before a bot.

Part 1 `families`: hf_lab's six scalping families on M1 (GOLD, NAS100, US500, US30, GER40), holds 5-60 min,
  SL/TP/hold chosen on the first 70% only, reported on the last 30%. Same fill/cost model as hf_lab
  (strategy_lab.simulate; spread per bar = max(1.2 x bar spread, typical spread x 1.2)).
Part 2 `open_drive`: the cash open is where one-minute timing could matter. After the first N minutes of the
  session (US 16:30 server, GER40 10:00), trade WITH or AGAINST the first-N-minute move, exit after H minutes.
  Net of 1x/1.5x spread, per-trade bp, train/test split by date.

    poetry run python research/m1_lab.py families
    poetry run python research/m1_lab.py open_drive
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from hf_lab import FAMILIES, signals
from strategy_lab import Result, cost, point, simulate

LAB = Path("data/lab")
SESSION = {"GOLD": (10, 22), "NAS100": (16.5, 22.5), "US500": (16.5, 22.5), "US30": (16.5, 22.5), "GER40": (10, 18)}
OPEN_HM = {"NAS100": (16, 30), "US500": (16, 30), "US30": (16, 30), "GER40": (10, 0)}
GRID = {"sl_atr": [1.5, 3.0], "rr": [1.0, 2.0], "hold": [5, 15, 30, 60]}


def load_m1(key: str) -> pd.DataFrame:
    return pd.read_csv(LAB / f"{key}_M1.csv", parse_dates=["time"])


def families() -> pd.DataFrame:
    rows = []
    for key in SESSION:
        df = load_m1(key)
        spreads = np.maximum(1.2 * df.spread.to_numpy() * point(key), cost(key, df))
        h = df.time.dt.hour + df.time.dt.minute / 60
        a, b = SESSION[key]
        sess = ((h >= a) & (h < b)).to_numpy()
        split = int(len(df) * 0.7)
        days_te = df.time.iloc[split:].dt.date.nunique()
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
            rows.append({"market": key, "family": fam, "sl_atr": sl, "rr": rr, "hold": hold,
                         "trades/day": round(te.get("n", 0) / max(days_te, 1), 1),
                         "train_n": tr["n"], "train_PF": tr["PF"], "train_expR": tr["expR"],
                         "test_n": te.get("n", 0), "test_PF": te.get("PF"), "test_expR": te.get("expR")})
            r = rows[-1]
            print(f"  {key:6s} {fam:12s} sl{sl} rr{rr} hold{hold:2d} | {r['trades/day']:4.1f}/day | TRAIN n{tr['n']} "
                  f"PF{tr['PF']} expR{tr['expR']} | TEST n{r['test_n']} PF{r['test_PF']} expR{r['test_expR']}",
                  flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(LAB / "m1_families.csv", index=False)
    return out


def open_drive() -> pd.DataFrame:
    rows = []
    for key, (oh, om) in OPEN_HM.items():
        df = load_m1(key).set_index("time")
        sp_bp = 1e4 * float(np.median(cost(key, load_m1(key)) / load_m1(key).close.to_numpy()))
        days = sorted({t.date() for t in df.index})
        split_day = days[int(len(days) * 0.7)]
        for n_min, hold in itertools.product([1, 5, 15], [5, 15, 30, 60]):
            moves, rets, dates = [], [], []
            for d in days:
                t0 = pd.Timestamp(d) + pd.Timedelta(hours=oh, minutes=om)
                t_sig, t_exit = t0 + pd.Timedelta(minutes=n_min), t0 + pd.Timedelta(minutes=n_min + hold)
                if t0 not in df.index or t_sig not in df.index or t_exit not in df.index:
                    continue
                o, e, x = df.at[t0, "open"], df.at[t_sig, "open"], df.at[t_exit, "open"]
                moves.append(np.sign(e - o))
                rets.append(1e4 * (x / e - 1))
                dates.append(d)
            if len(rets) < 30:
                continue
            mv, rt, dt = np.array(moves), np.array(rets), np.array(dates)
            for mode, sgn in (("follow", 1), ("fade", -1)):
                for mult in (1.0, 1.5):
                    net = sgn * mv * rt - mult * sp_bp
                    net = net[mv != 0]
                    tr, te = net[dt[mv != 0] < split_day], net[dt[mv != 0] >= split_day]

                    def pf(x: np.ndarray) -> float:
                        lo = -x[x < 0].sum()
                        return round(x[x > 0].sum() / lo, 2) if lo > 0 else float("inf")

                    rows.append({"market": key, "first_min": n_min, "hold": hold, "mode": mode, "spread_x": mult,
                                 "n": len(net), "win%": round(100 * (net > 0).mean(), 1),
                                 "avg_bp": round(net.mean(), 1), "PF": pf(net),
                                 "train_PF": pf(tr), "test_PF": pf(te)})
    out = pd.DataFrame(rows)
    out.to_csv(LAB / "m1_open_drive.csv", index=False)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "families"
    if cmd == "families":
        families()
    elif cmd == "open_drive":
        r = open_drive()
        ok = r[(r.spread_x == 1.5) & (r.train_PF > 1) & (r.test_PF > 1)]
        print(f"{len(r)} rows; positive in train AND test at 1.5x spread: {len(ok)}")
        print(ok.sort_values("PF", ascending=False).head(15).to_string(index=False))
        print("\nbest 10 overall at 1.0x spread:")
        print(r[r.spread_x == 1.0].sort_values("PF", ascending=False).head(10).to_string(index=False))
    else:
        raise SystemExit(f"unknown command {cmd}")
