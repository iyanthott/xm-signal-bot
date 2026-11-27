"""False-discovery check for daily_lab.py: how many of its "PASSED" rows would a same-size RANDOM
entry rule clear purely by chance?

daily_lab.py has now run 12 strategies x ~110 markets = ~1,320 combinations through one pass bar
(n>=40, test_n>=15, train_expR>0, test_expR>0, train_PF>=1.1, test_PF>=1.2) and found 25 passes.
With that many hypotheses tested, some passes are expected from noise alone - nobody had checked
how many. This script answers that with a label-shuffle Monte Carlo: for each real market, replace
a strategy's actual entries with the SAME NUMBER of entries at RANDOM bars (so same trade count,
same market, same costs, same 2xATR stop, same holding-period cap), using one real strategy's
trade count per holding-period bucket as the target count (dip_long for 10d, macd_trend for 60d,
breakout for 120d - picked only as trade-count references, not because their exit logic is reused).
Exit is time-cap + stop only (no smart indicator exit) - if anything this UNDERSTATES the true
false-positive rate, since a real strategy's informed exit would help a random entry too.

    poetry run python research/noise_check.py            # ~25 seeds/bucket/market, full universe
    poetry run python research/noise_check.py --seeds 10  # faster, coarser estimate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from daily_lab import D1, _conservative  # noqa: E402
from strategy_lab import Result, atr  # noqa: E402

BUCKETS = {"short10": 10, "mid60": 60, "long120": 120}
REF_STRAT = {"short10": "dip_long", "mid60": "macd_trend", "long120": "breakout"}
PASS = {"n": 40, "test_n": 15}


def simulate_random(df: pd.DataFrame, meta: dict, max_days: int, n_target: int, rng: np.random.Generator) -> Result:
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df.time.to_numpy()
    a = atr(df, 14).to_numpy()
    n = len(df) - 1
    lo_b, hi_b = 200, n - max_days - 1
    if hi_b <= lo_b or n_target <= 0:
        return Result(np.array([]), np.array([]))
    pool = np.arange(lo_b, hi_b)
    k_pick = min(n_target * 3, len(pool))  # oversample: sequential processing skips some as "busy"
    candidates = np.sort(rng.choice(pool, size=k_pick, replace=False))
    dirs = {idx: d for idx, d in zip(candidates, rng.choice([1, -1], size=len(candidates)), strict=True)}
    spread_now = meta["spread_frac"]
    out_r, out_t = [], []
    i, taken = lo_b, 0
    while i < hi_b and taken < n_target:
        if i not in dirs or not np.isfinite(a[i]) or a[i] <= 0:
            i += 1
            continue
        d = dirs[i]
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
            if k - j + 1 >= max_days:
                k = min(k + 1, n)
                exit_px = o[k]
                break
            k += 1
        if exit_px is None:
            break
        nights = int((t[k] - t[j]) / np.timedelta64(1, "D"))
        swap = (meta["swap_long_frac"] if d == 1 else meta["swap_short_frac"]) * px * max(nights, 0)
        pnl = (exit_px - px) * d - (spread_now * px + swap)
        out_r.append(pnl / dist)
        out_t.append(j)
        taken += 1
        i = k + 1
    return Result(np.array(out_r), np.array(out_t))


def passes(res: Result, df: pd.DataFrame, test_frac: float = 0.3) -> bool:
    if res.n < PASS["n"]:
        return False
    split_time = df.time.iloc[int(len(df) * (1 - test_frac))]
    is_test = df.time.to_numpy()[res.t] >= split_time.to_datetime64()
    tr = Result(res.r[~is_test], res.t[~is_test]).stats()
    te = Result(res.r[is_test], res.t[is_test]).stats()
    if te.get("n", 0) < PASS["test_n"]:
        return False
    return (
        tr.get("expR", -1) > 0
        and te.get("expR", -1) > 0
        and tr.get("PF", 0) >= 1.1
        and te.get("PF", 0) >= 1.2
    )


def run(n_seeds: int) -> None:
    meta_all = {k: _conservative(v) for k, v in json.loads((D1 / "meta.json").read_text()).items()}
    ref = pd.read_csv(D1.parent / "d1_results.csv")
    rows = []
    for bucket, max_days in BUCKETS.items():
        ref_strat = REF_STRAT[bucket]
        counts = ref[ref.strategy == ref_strat].set_index("symbol")["n"].to_dict()
        for sym, meta in meta_all.items():
            n_target = int(counts.get(sym, 0))
            if n_target < PASS["n"]:  # a real strategy with <40 trades here isn't comparable either
                continue
            df = pd.read_csv(D1 / f"{sym}.csv", parse_dates=["time"])
            n_pass = 0
            for seed in range(n_seeds):
                rng = np.random.default_rng(hash((sym, bucket, seed)) % (2**32))
                res = simulate_random(df, meta, max_days, n_target, rng)
                if passes(res, df):
                    n_pass += 1
            rows.append({"bucket": bucket, "ref_strat": ref_strat, "symbol": sym, "n_target": n_target,
                         "trials": n_seeds, "random_passes": n_pass})
            print(f"{bucket:8s} {sym:12s} target_n={n_target:4d}  random pass {n_pass}/{n_seeds}", flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(D1.parent / "noise_check_results.csv", index=False)

    total_trials = int(out.trials.sum())
    total_random_passes = int(out.random_passes.sum())
    empirical_rate = total_random_passes / max(total_trials, 1)
    print(f"\n=== empirical null pass rate: {total_random_passes}/{total_trials} = {empirical_rate:.4%} ===")
    for bucket in BUCKETS:
        g = out[out.bucket == bucket]
        tr, tp = int(g.trials.sum()), int(g.random_passes.sum())
        print(f"  {bucket:8s} (ref {REF_STRAT[bucket]:12s}): {tp}/{tr} = {tp / max(tr, 1):.4%}  over {len(g)} markets")

    real_combos = 12 * out.symbol.nunique()  # 12 strategies tested in daily_lab.py, same market universe
    expected_chance_passes = empirical_rate * real_combos
    print(f"\nreal grid tested: ~{real_combos} strategy x market combos, 25 actually passed.")
    print(f"expected passes from chance alone at this empirical null rate: ~{expected_chance_passes:.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=25)
    a = ap.parse_args()
    run(a.seeds)
