"""Edge lab: strategy families not covered by daily_lab / strategy_lab / momentum_burst_lab / range_fade_lab.

Research only (not used by live bots). Fixed rules, identical for every market, same cost model and
train (first 70%) / test (last 30%) split as daily_lab.py, so a "pass" is not per-market curve-fitting.

DAILY (whole cached D1 universe, costs = 1.5x spread + broker swap charges, via daily_lab._conservative):
  ibs_long     : close > SMA200 and IBS=(C-L)/(H-L) < 0.2 -> buy next open; exit next open after a close
                 above the prior day's high, or 5 days; stop 2 x ATR14
  ibs_short    : mirror: close < SMA200, IBS > 0.8 -> sell; exit after a close below the prior day's low
  tom_long     : turn of month: buy the open of the last trading day of the month, sell at the close of
                 the 3rd trading day of the new month; stop 2 x ATR14
  nr7_break    : day after an NR7 day (narrowest range of 7): buy stop at its high, sell stop at its low;
                 stop = the opposite level; exit at the close of day 3. Both levels touched in one day
                 counts as a full loss (conservative - entered, then stopped)
  nr7_trend    : nr7_break, but only in the direction of the SMA200
  inside_trend : inside day with close > SMA50 > SMA200 -> buy stop at the inside day's high next day,
                 stop at its low (mirror for shorts); exit at the close of day 5
  monday_turn  : Monday closes below Friday and close > SMA200 -> buy Tuesday's open, sell Tuesday's close

INTRADAY (M15, strategy_lab.MARKETS, costs = strategy_lab.cost(), no swap - flat by the close):
  intraday_mom : US indices. Return from the prior day's 23:00 close to 17:00 server (first 30 minutes of
                 the US cash session) sets the direction; trade the last 30 minutes (22:30-23:00 server).
                 Results in basis points of price, not R.
  asian_break  : FX + GOLD. Range 02:00-09:00 server; first M15 close outside it 09:00-13:00 -> enter next
                 bar open; stop at the range midpoint; target 1R (asian_break_1r) or none (asian_break_eod);
                 flat at 22:00 server. SL and TP in one bar -> SL counts.

    poetry run python research/edge_lab.py daily
    poetry run python research/edge_lab.py intraday
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
from strategy_lab import MARKETS, Result, atr, cost, load  # noqa: E402

PASS_RULE = "n>=40, test_n>=15, expectancy > 0 in BOTH periods, train PF>=1.1, test PF>=1.2"


# ---------------------------------------------------------------- daily simulators
def sim_next_open(
    df: pd.DataFrame, meta: dict, entry: np.ndarray, exit_l: np.ndarray, exit_s: np.ndarray,
    max_days: int, stop_atr: float = 2.0, hold_close: int | None = None,
) -> Result:
    """Signal at close i -> enter open i+1. Exit: stop (gap fills at the open), a close-based exit flag
    (-> next open), max_days (-> next open), or hold_close (-> the close of that day, no look-ahead
    since the exit time is fixed in advance)."""
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df.time.to_numpy()
    a = atr(df, 14).to_numpy()
    out_r, out_t = [], []
    i, n = 200, len(df) - 1
    while i < n:
        d = entry[i]
        if d == 0 or not np.isfinite(a[i]) or a[i] <= 0:
            i += 1
            continue
        j = i + 1
        px = o[j]
        dist = stop_atr * a[i]
        stop = px - d * dist
        k, exit_px, exit_k = j, None, None
        while k < n:
            if d == 1 and lo[k] <= stop:
                exit_px, exit_k = min(stop, o[k]), k
                break
            if d == -1 and h[k] >= stop:
                exit_px, exit_k = max(stop, o[k]), k
                break
            held = k - j + 1
            if hold_close is not None and held >= hold_close:
                exit_px, exit_k = c[k], k
                break
            if (exit_l[k] if d == 1 else exit_s[k]) or held >= max_days:
                exit_px, exit_k = o[k + 1], k + 1
                break
            k += 1
        if exit_px is None:
            break
        nights = int((t[exit_k] - t[j]) / np.timedelta64(1, "D"))
        swap = (meta["swap_long_frac"] if d == 1 else meta["swap_short_frac"]) * px * max(nights, 0)
        pnl = (exit_px - px) * d - meta["spread_frac"] * px - swap
        out_r.append(pnl / dist)
        out_t.append(j)
        i = exit_k + 1
    return Result(np.array(out_r), np.array(out_t))


def sim_stop_entry(
    df: pd.DataFrame, meta: dict, up: np.ndarray, dn: np.ndarray, hold: int
) -> Result:
    """Orders placed after close i, live during day i+1 only: buy stop at up[i] (protective stop at dn[i]),
    sell stop at dn[i] (protective stop at up[i]); NaN = no order on that side. If both trigger the same
    day the trade is booked as a full loss (filled on one side, stopped on the other). Gap past a level
    fills at the open. Exit at the close of day `hold` (counting the entry day)."""
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df.time.to_numpy()
    out_r, out_t = [], []
    i, n = 200, len(df) - 1
    while i < n:
        u, w = up[i], dn[i]
        j = i + 1
        hit_u = np.isfinite(u) and h[j] >= u
        hit_d = np.isfinite(w) and lo[j] <= w
        if not (hit_u or hit_d):
            i += 1
            continue
        band = (u - w) if (np.isfinite(u) and np.isfinite(w)) else np.nan
        if not np.isfinite(band) or band <= 0:
            i += 1
            continue
        if hit_u and hit_d:
            ref = u if np.isfinite(u) else w
            out_r.append(-1.0 - meta["spread_frac"] * ref / band)
            out_t.append(j)
            i = j + 1
            continue
        d = 1 if hit_u else -1
        px = max(u, o[j]) if d == 1 else min(w, o[j])
        stop = w if d == 1 else u
        dist = abs(px - stop)
        k, exit_px = j, None
        while k <= min(n, j + hold - 1):
            if k > j and d == 1 and lo[k] <= stop:
                exit_px = min(stop, o[k])
                break
            if k > j and d == -1 and h[k] >= stop:
                exit_px = max(stop, o[k])
                break
            if k == min(n, j + hold - 1):
                exit_px = c[k]
                break
            k += 1
        nights = int((t[k] - t[j]) / np.timedelta64(1, "D"))
        swap = (meta["swap_long_frac"] if d == 1 else meta["swap_short_frac"]) * px * max(nights, 0)
        pnl = (exit_px - px) * d - meta["spread_frac"] * px - swap
        out_r.append(pnl / dist)
        out_t.append(j)
        i = k + 1
    return Result(np.array(out_r), np.array(out_t))


# ---------------------------------------------------------------- daily strategies
def run_strategy(df: pd.DataFrame, strat: str, m: dict) -> Result:
    o, h, lo, c = df.open, df.high, df.low, df.close
    sma200 = c.rolling(200).mean()
    none = np.zeros(len(df), bool)
    if strat in ("ibs_long", "ibs_short"):
        ibs = ((c - lo) / (h - lo).replace(0, np.nan)).fillna(0.5)
        if strat == "ibs_long":
            e = np.where((c > sma200) & (ibs < 0.2), 1, 0)
        else:
            e = np.where((c < sma200) & (ibs > 0.8), -1, 0)
        return sim_next_open(df, m, e, (c > h.shift()).to_numpy(), (c < lo.shift()).to_numpy(), 5)
    if strat == "tom_long":
        month = df.time.dt.to_period("M")
        last_td = (month != month.shift(-1)).to_numpy()  # calendar is known in advance, not look-ahead
        e = np.zeros(len(df), int)
        e[:-1] = np.where(last_td[1:], 1, 0)  # signal the evening before the last trading day
        return sim_next_open(df, m, e, none, none, 10, hold_close=4)
    if strat in ("nr7_break", "nr7_trend"):
        rng = h - lo
        nr7 = (rng <= rng.rolling(7).min()).to_numpy()
        up = np.where(nr7, h, np.nan)
        dn = np.where(nr7, lo, np.nan)
        if strat == "nr7_trend":  # one-sided order, the other level is the protective stop only
            bull = nr7 & (c > sma200).to_numpy()
            bear = nr7 & (c < sma200).to_numpy()
            r_up = _one_sided(df, m, np.where(bull, h, np.nan), np.where(bull, lo, np.nan), 1, 3)
            r_dn = _one_sided(df, m, np.where(bear, h, np.nan), np.where(bear, lo, np.nan), -1, 3)
            return _concat(r_up, r_dn)
        return sim_stop_entry(df, m, up, dn, 3)
    if strat == "inside_trend":
        inside = ((h < h.shift()) & (lo > lo.shift())).to_numpy()
        sma50 = c.rolling(50).mean()
        bull = (inside & ((c > sma50) & (sma50 > sma200)).to_numpy())
        bear = (inside & ((c < sma50) & (sma50 < sma200)).to_numpy())
        r_up = _one_sided(df, m, np.where(bull, h, np.nan), np.where(bull, lo, np.nan), 1, 5)
        r_dn = _one_sided(df, m, np.where(bear, h, np.nan), np.where(bear, lo, np.nan), -1, 5)
        return _concat(r_up, r_dn)
    if strat == "monday_turn":
        dow = df.time.dt.dayofweek
        e = np.where((dow == 0) & (c < c.shift()) & (c > sma200), 1, 0)
        return sim_next_open(df, m, e, none, none, 5, hold_close=1)
    raise ValueError(strat)


def _one_sided(df: pd.DataFrame, m: dict, hi: np.ndarray, lo_: np.ndarray, d: int, hold: int) -> Result:
    """Single stop order (buy at hi if d=1, sell at lo_ if d=-1); the other level is the protective stop.
    Same-day touch of the protective stop after the fill counts as a full loss (no intrabar order)."""
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df.time.to_numpy()
    out_r, out_t = [], []
    i, n = 200, len(df) - 1
    while i < n:
        lvl, stop = (hi[i], lo_[i]) if d == 1 else (lo_[i], hi[i])
        j = i + 1
        if not np.isfinite(lvl) or not (h[j] >= lvl if d == 1 else lo[j] <= lvl):
            i += 1
            continue
        px = max(lvl, o[j]) if d == 1 else min(lvl, o[j])
        dist = abs(px - stop)
        if dist <= 0:
            i += 1
            continue
        if (d == 1 and lo[j] <= stop) or (d == -1 and h[j] >= stop):
            out_r.append(-1.0 - m["spread_frac"] * px / dist)
            out_t.append(j)
            i = j + 1
            continue
        k, exit_px = j + 1, None
        while k <= min(n, j + hold - 1):
            if d == 1 and lo[k] <= stop:
                exit_px = min(stop, o[k])
                break
            if d == -1 and h[k] >= stop:
                exit_px = max(stop, o[k])
                break
            if k == min(n, j + hold - 1):
                exit_px = c[k]
                break
            k += 1
        if exit_px is None:
            k, exit_px = j, c[j]
        nights = int((t[k] - t[j]) / np.timedelta64(1, "D"))
        swap = (m["swap_long_frac"] if d == 1 else m["swap_short_frac"]) * px * max(nights, 0)
        out_r.append(((exit_px - px) * d - m["spread_frac"] * px - swap) / dist)
        out_t.append(j)
        i = k + 1
    return Result(np.array(out_r), np.array(out_t))


def _concat(a: Result, b: Result) -> Result:
    r, t = np.concatenate([a.r, b.r]), np.concatenate([a.t, b.t]).astype(int)
    order = np.argsort(t, kind="stable")
    return Result(r[order], t[order])


DAILY_STRATS = ["ibs_long", "ibs_short", "tom_long", "nr7_break", "nr7_trend", "inside_trend", "monday_turn"]


def split_stats(res: Result, df: pd.DataFrame, test_frac: float) -> tuple[dict, dict, dict]:
    split_time = df.time.iloc[int(len(df) * (1 - test_frac))].to_datetime64()
    is_test = df.time.to_numpy()[res.t] >= split_time if res.n else np.array([], bool)
    return (Result(res.r[~is_test], res.t[~is_test]).stats(), Result(res.r[is_test], res.t[is_test]).stats(),
            res.stats())


def run_daily(test_frac: float, only: list[str] | None) -> pd.DataFrame:
    meta = {k: _conservative(v) for k, v in json.loads((D1 / "meta.json").read_text()).items()}
    rows = []
    for sym, m in meta.items():
        df = pd.read_csv(D1 / f"{sym}.csv", parse_dates=["time"])
        years = (df.time.iloc[-1] - df.time.iloc[0]).days / 365.25
        for strat in DAILY_STRATS:
            if only and strat not in only:
                continue
            res = run_strategy(df, strat, m)
            tr, te, al = split_stats(res, df, test_frac)
            rows.append({
                "class": m["class"], "symbol": sym, "strategy": strat, "years": round(years, 1),
                "n": al.get("n", 0), "trades_yr": round(al.get("n", 0) / max(years, 0.1), 1),
                "win%": al.get("win%"), "PF": al.get("PF"), "expR": al.get("expR"), "maxDD_R": al.get("maxDD_R"),
                "train_n": tr.get("n", 0), "train_PF": tr.get("PF"), "train_expR": tr.get("expR"),
                "test_n": te.get("n", 0), "test_PF": te.get("PF"), "test_win%": te.get("win%"),
                "test_expR": te.get("expR"),
            })
    out = pd.DataFrame(rows)
    out.to_csv(D1.parent / "edge_daily_results.csv", index=False)
    report(out)
    return out


def report(out: pd.DataFrame) -> None:
    ok = out[(out.n >= 40) & (out.test_n >= 15) & (out.train_expR > 0) & (out.test_expR > 0)
             & (out.train_PF >= 1.1) & (out.test_PF >= 1.2)]
    pd.set_option("display.width", 240)
    pd.set_option("display.max_rows", 400)
    print(f"\n=== PASSED ({PASS_RULE}) : {len(ok)} of {len(out)} market x strategy tests ===")
    print(ok.sort_values(["strategy", "test_PF"], ascending=[True, False]).to_string(index=False))
    pooled = []
    for (cls, strat), g in out.groupby(["class", "strategy"]):
        tot = max(g.n.sum(), 1)
        pooled.append({"class": cls, "strategy": strat, "markets": len(g), "trades": int(g.n.sum()),
                       "avg_expR": round(float((g.expR.fillna(0) * g.n).sum() / tot), 3),
                       "pos_both": int(((g.train_expR > 0) & (g.test_expR > 0)).sum()),
                       "passed": int(g.index.isin(ok.index).sum())})
    print("\n=== POOLED by class x strategy (all markets, no selection) ===")
    print(pd.DataFrame(pooled).to_string(index=False))


# ---------------------------------------------------------------- intraday
def _day_groups(df: pd.DataFrame) -> pd.core.groupby.DataFrameGroupBy:
    df = df.copy()
    df["date"] = df.time.dt.date
    df["hm"] = df.time.dt.hour * 100 + df.time.dt.minute
    return df


def run_intraday_mom(key: str, test_frac: float, entry_hm: int = 2230, thresh_bp: float = 0.0) -> dict:
    df = _day_groups(load(key, "M15"))
    sp = cost(key, df) / df.close.to_numpy()  # spread as a fraction of price
    df["sp"] = sp
    r, t = [], []
    prev_close = None
    for _, g in df.groupby("date", sort=True):
        g = g.set_index("hm")
        if 1645 in g.index and prev_close is not None and entry_hm in g.index and 2245 in g.index:
            r1 = g.loc[1645, "close"] / prev_close - 1
            if abs(r1) * 1e4 > thresh_bp:
                d = 1 if r1 > 0 else -1
                px_in, px_out = g.loc[entry_hm, "open"], g.loc[2245, "close"]
                net = d * (px_out / px_in - 1) - g.loc[entry_hm, "sp"]
                r.append(net * 1e4)
                t.append(int(g.loc[entry_hm, "index"]) if "index" in g.columns else len(t))
        if 2245 in g.index:
            prev_close = g.loc[2245, "close"]
    res = Result(np.array(r), np.arange(len(r)))
    cut = int(len(r) * (1 - test_frac))
    return {"tr": Result(res.r[:cut], res.t[:cut]).stats(), "te": Result(res.r[cut:], res.t[cut:]).stats(),
            "all": res.stats()}


def run_asian_break(key: str, test_frac: float, rr: float | None, rng_end: int = 900,
                    win_end: int = 1300, flat_hm: int = 2200) -> dict:
    df = _day_groups(load(key, "M15"))
    sp = cost(key, df)
    df["sp"] = sp
    out = []
    for _, g in df.groupby("date", sort=True):
        rng = g[(g.hm >= 200) & (g.hm < rng_end)]
        if len(rng) < (rng_end - 200) // 100 * 3:
            continue
        hi, lo = rng.high.max(), rng.low.min()
        mid = (hi + lo) / 2
        after = g[g.hm >= rng_end].reset_index(drop=True)
        trig = after[(after.hm < win_end) & ((after.close > hi) | (after.close < lo))]
        if trig.empty:
            continue
        ti = trig.index[0]
        if ti + 1 >= len(after) or after.hm.iloc[ti + 1] >= flat_hm:
            continue
        d = 1 if after.close.iloc[ti] > hi else -1
        s = after.sp.iloc[ti]
        entry = after.open.iloc[ti + 1] + (s if d == 1 else 0)
        dist = (entry - mid) * d
        if dist <= s:
            continue
        sl = mid
        tp = entry + d * rr * dist if rr else None
        exit_px = None
        for k in range(ti + 1, len(after)):
            b = after.iloc[k]
            if b.hm >= flat_hm:
                exit_px = b.open + (0 if d == 1 else s)
                break
            hi_b, lo_b = (b.high, b.low) if d == 1 else (b.high + s, b.low + s)
            if (d == 1 and lo_b <= sl) or (d == -1 and hi_b >= sl):
                exit_px = sl
                break
            if tp is not None and ((d == 1 and hi_b >= tp) or (d == -1 and lo_b <= tp)):
                exit_px = tp
                break
        if exit_px is None:
            b = after.iloc[-1]
            exit_px = b.close + (0 if d == 1 else s)
        out.append((exit_px - entry) * d / dist)
    r = np.array(out)
    cut = int(len(r) * (1 - test_frac))
    return {"tr": Result(r[:cut], np.arange(cut)).stats(), "te": Result(r[cut:], np.arange(len(r) - cut)).stats(),
            "all": Result(r, np.arange(len(r))).stats()}


def run_intraday(test_frac: float) -> None:
    print("=== intraday_mom (US indices; bp of price per trade, net of spread) ===")
    for key in ("US500", "NAS100", "US30"):
        for entry_hm, label in ((2230, "last 30m"), (1700, "17:00->close")):
            for th in (0.0, 25.0):
                s = run_intraday_mom(key, test_frac, entry_hm, th)
                print(f"  {key:7s} {label:12s} |r1|>{th:>4.0f}bp  TRAIN {s['tr']}  TEST {s['te']}", flush=True)
    print("\n=== asian_break (FX + GOLD; R per trade, net of spread) ===")
    for key in MARKETS:
        if key in ("NAS100", "US500", "US30", "GER40"):
            continue
        for rr, label in ((1.0, "1R"), (None, "eod")):
            s = run_asian_break(key, test_frac, rr)
            print(f"  {key:7s} {label:3s}  TRAIN {s['tr']}  TEST {s['te']}", flush=True)


# ---------------------------------------------------------------- robustness of the daily shortlist
def _load_d1(sym: str) -> tuple[pd.DataFrame, dict]:
    meta = json.loads((D1 / "meta.json").read_text())
    return pd.read_csv(D1 / f"{sym}.csv", parse_dates=["time"]), _conservative(meta[sym])


def _windows(res: Result, df: pd.DataFrame, k: int = 4) -> list[str]:
    """Stats in k equal calendar slices of history (sequential walk, no selection)."""
    t = df.time.to_numpy()
    edges = np.linspace(200, len(df) - 1, k + 1).astype(int)
    out = []
    for a_, b_ in zip(edges[:-1], edges[1:], strict=False):
        sel = (res.t >= a_) & (res.t < b_)
        s = Result(res.r[sel], res.t[sel]).stats()
        out.append(f"{str(t[a_])[:4]}-{str(t[b_])[:4]} n{s.get('n', 0)} PF{s.get('PF', '-')} "
                   f"win{s.get('win%', '-')}")
    return out


def _dip_positions(df: pd.DataFrame, m: dict) -> np.ndarray:
    """Bars on which the live Dip bot (RSI2<15 over SMA200, exit close>SMA5 / 10d, 2xATR stop) is in a trade."""
    from daily_lab import signals as dl_signals

    e, xl, xs = dl_signals(df, "dip_long")
    res = sim_next_open(df, m, e, xl, xs, 10)
    busy = np.zeros(len(df), bool)
    lo, a = df.low.to_numpy(), atr(df, 14).to_numpy()
    sma5 = df.close.rolling(5).mean().to_numpy()
    c, o = df.close.to_numpy(), df.open.to_numpy()
    for j in res.t:
        stop = o[j] - 2 * a[j - 1]
        k = j
        while k < len(df) - 1:
            busy[k] = True
            if lo[k] <= stop or c[k] > sma5[k] or k - j + 1 >= 10:
                break
            k += 1
    return busy


def robust_ibs(sym: str) -> None:
    df, m = _load_d1(sym)
    c, h, lo = df.close, df.high, df.low
    sma200 = c.rolling(200).mean()
    ibs = ((c - lo) / (h - lo).replace(0, np.nan)).fillna(0.5)
    sma5 = c.rolling(5).mean()
    print(f"\n=== ibs_long robustness: {sym} ({df.time.iloc[0].date()}..{df.time.iloc[-1].date()}) ===")
    for exit_name, xl in (("prevHigh", (c > h.shift()).to_numpy()), ("sma5", (c > sma5).to_numpy())):
        for th in (0.1, 0.15, 0.2, 0.25, 0.3):
            for md in (3, 5, 7):
                e = np.where((c > sma200) & (ibs < th), 1, 0)
                res = sim_next_open(df, m, e, xl, xl, md)
                tr, te, al = split_stats(res, df, 0.3)
                print(f"  exit={exit_name:8s} ibs<{th:.2f} max{md}d  ALL n{al['n']} win{al['win%']} PF{al['PF']} "
                      f"expR{al['expR']} DD{al['maxDD_R']} | train PF{tr.get('PF')} test PF{te.get('PF')}")
    e = np.where((c > sma200) & (ibs < 0.2), 1, 0)
    base = sim_next_open(df, m, e, (c > h.shift()).to_numpy(), (c > h.shift()).to_numpy(), 5)
    print("  4 windows (base):", " | ".join(_windows(base, df)))
    busy = _dip_positions(df, m)
    from daily_lab import signals as dl_signals

    dip_entry = dl_signals(df, "dip_long")[0]
    e2 = np.where((e == 1) & ~busy & (dip_entry == 0), 1, 0)
    extra = sim_next_open(df, m, e2, (c > h.shift()).to_numpy(), (c > h.shift()).to_numpy(), 5)
    tr, te, al = split_stats(extra, df, 0.3)
    print(f"  ONLY trades the Dip bot would not already hold: n{al.get('n')} win{al.get('win%')} PF{al.get('PF')} "
          f"expR{al.get('expR')} | train PF{tr.get('PF')} test PF{te.get('PF')}")
    print("  4 windows (non-overlap):", " | ".join(_windows(extra, df)))


def robust_tom(sym: str) -> None:
    df, m = _load_d1(sym)
    month = df.time.dt.to_period("M")
    pos_from_end = df.groupby(month).cumcount(ascending=False).to_numpy()  # 0 = last trading day
    none = np.zeros(len(df), bool)
    print(f"\n=== tom_long robustness: {sym} ({df.time.iloc[0].date()}..{df.time.iloc[-1].date()}) ===")
    for enter_from_end in (0, 1, 2, 3):  # enter the open of the Nth-from-last trading day
        for hold in (2, 3, 4, 5, 6):
            e = np.zeros(len(df), int)
            e[:-1] = np.where(pos_from_end[1:] == enter_from_end, 1, 0)
            res = sim_next_open(df, m, e, none, none, 20, hold_close=hold + enter_from_end)
            tr, te, al = split_stats(res, df, 0.3)
            print(f"  enter TD-{enter_from_end} hold {hold + enter_from_end}d  ALL n{al['n']} win{al['win%']} "
                  f"PF{al['PF']} expR{al['expR']} DD{al['maxDD_R']} | train PF{tr.get('PF')} test PF{te.get('PF')}")
    e = np.zeros(len(df), int)
    e[:-1] = np.where(pos_from_end[1:] == 0, 1, 0)
    base = sim_next_open(df, m, e, none, none, 20, hold_close=4)
    print("  4 windows (base):", " | ".join(_windows(base, df)))
    rnd = []
    rng = np.random.default_rng(7)
    for _ in range(200):  # same number of 4-day long trades on random entry days -> is it just gold's drift?
        er = np.zeros(len(df), int)
        er[rng.choice(np.arange(200, len(df) - 10), size=int(e.sum()), replace=False)] = 1
        rnd.append(sim_next_open(df, m, er, none, none, 20, hold_close=4).stats().get("expR", 0))
    print(f"  random-entry 4-day longs: median expR {np.median(rnd):.3f}, 95th pct {np.percentile(rnd, 95):.3f} "
          f"vs ToM {base.stats()['expR']}")


# ---------------------------------------------------------------- round 2
def _bar_spread(key: str, df: pd.DataFrame) -> np.ndarray:
    """Per-bar spread in price: the larger of 1.2x the bar's own recorded spread and strategy_lab's typical
    active-hours spread (so quiet hours with wide spreads are charged what they really cost)."""
    from strategy_lab import point

    return np.maximum(1.2 * df.spread.to_numpy() * point(key), cost(key, df))


def h1_dip(key: str, rsi_lvl: float, start_h: int = 10, flat_h: int = 22) -> Result:
    """Intraday dip-buy on H1, flat before the 23:00 rollover (no swap). Daily uptrend (prior day's close
    above its 200-day SMA, built from H1) + H1 RSI(2) < rsi_lvl between start_h and flat_h-2 -> buy next
    bar open; exit next open after an H1 close above its 5-bar SMA, at the flat_h bar's open, or a
    2 x ATR(H1) stop."""
    from strategy_lab import daily, rsi

    df = load(key, "H1")
    d1 = daily(key)
    d1["up"] = d1.close > d1.close.rolling(200).mean()
    d1["date"] = d1.time.dt.date
    up_by_day = dict(zip(d1.date, d1.up.shift(), strict=False))  # yesterday's state, known at today's open
    df["date"] = df.time.dt.date
    hr = df.time.dt.hour.to_numpy()
    trend = df.date.map(up_by_day).fillna(False).astype(bool).to_numpy()
    r2 = rsi(df.close, 2).to_numpy()
    sma5 = df.close.rolling(5).mean().to_numpy()
    a = atr(df, 14).to_numpy()
    o, lo, c = df.open.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    sp = _bar_spread(key, df)
    dates = df.date.to_numpy()
    out_r, out_t = [], []
    i, n = 300, len(df) - 2
    while i < n:
        if not (trend[i] and r2[i] < rsi_lvl and start_h <= hr[i] < flat_h - 1 and np.isfinite(a[i])):
            i += 1
            continue
        j = i + 1
        if dates[j] != dates[i] or hr[j] >= flat_h:
            i += 1
            continue
        entry = o[j] + sp[j]
        dist = 2 * a[i]
        stop = entry - dist
        k, exit_px = j, None
        while k < n:
            if lo[k] <= stop:
                exit_px = min(stop, o[k])
                break
            if dates[k + 1] != dates[j] or hr[k + 1] >= flat_h or c[k] > sma5[k]:
                exit_px = o[k + 1]
                k += 1
                break
            k += 1
        if exit_px is None:
            break
        out_r.append((exit_px - entry) / dist)
        out_t.append(j)
        i = k + 1
    return Result(np.array(out_r), np.array(out_t))


def session_drift(key: str) -> None:
    """Mean net return (bp) of holding long through each session window, per year. A tradable drift must
    be positive in nearly every year, not just on average."""
    df = load(key, "H1")
    df["date"], df["hr"], df["yr"] = df.time.dt.date, df.time.dt.hour, df.time.dt.year
    sp_frac = _bar_spread(key, df) / df.close.to_numpy()
    df["sp"] = sp_frac
    windows = {"asia 01-09": (1, 9), "london 09-16": (9, 16), "ny 16-23": (16, 23)}
    print(f"\n--- {key}: long-only session drift, net bp per day (spread paid once) ---")
    rows = {}
    for name, (a_, b_) in windows.items():
        g = df[(df.hr >= a_) & (df.hr < b_)].groupby("date")
        day = pd.DataFrame({"o": g.open.first(), "c": g.close.last(), "sp": g.sp.first(),
                            "yr": g.yr.first()})
        day["net"] = (day.c / day.o - 1 - day.sp) * 1e4
        rows[name] = day.groupby("yr").net.mean().round(1)
    tab = pd.DataFrame(rows)
    print(tab.to_string())
    print("  years positive:", {k: int((v > 0).sum()) for k, v in tab.items()}, "of", len(tab))


def _nyse_holidays(years: range) -> set:
    from pandas.tseries.holiday import (
        AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay, USMartinLutherKingJr, USMemorialDay,
        USPresidentsDay, USThanksgivingDay, nearest_workday,
    )

    class Nyse(AbstractHolidayCalendar):
        rules = [
            Holiday("NewYear", month=1, day=1, observance=nearest_workday), USMartinLutherKingJr,
            USPresidentsDay, GoodFriday, USMemorialDay,
            Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
            Holiday("July4", month=7, day=4, observance=nearest_workday), USLaborDay, USThanksgivingDay,
            Holiday("Christmas", month=12, day=25, observance=nearest_workday),
        ]

    h = Nyse().holidays(f"{years.start}-01-01", f"{years.stop}-12-31")
    return {d.date() for d in h}


def pre_holiday(sym: str) -> None:
    """Buy the open of the last trading day before an NYSE holiday, sell its close (intraday, no swap)."""
    df, m = _load_d1(sym)
    hol = _nyse_holidays(range(df.time.dt.year.min(), df.time.dt.year.max() + 1))
    dates = df.time.dt.date.to_numpy()
    pre = np.zeros(len(df), bool)
    for i, d in enumerate(dates):
        nxt = pd.Timestamp(d) + pd.offsets.BDay(1)
        pre[i] = nxt.date() in hol
    e = np.zeros(len(df), int)
    e[:-1] = np.where(pre[1:], 1, 0)
    none = np.zeros(len(df), bool)
    res = sim_next_open(df, m, e, none, none, 5, hold_close=1)
    tr, te, al = split_stats(res, df, 0.3)
    rnd = []
    rng = np.random.default_rng(11)
    for _ in range(300):
        er = np.zeros(len(df), int)
        er[rng.choice(np.arange(200, len(df) - 3), size=max(int(e.sum()), 1), replace=False)] = 1
        rnd.append(sim_next_open(df, m, er, none, none, 5, hold_close=1).stats().get("expR", 0))
    print(f"  {sym:11s} pre-holiday n{al.get('n')} win{al.get('win%')} PF{al.get('PF')} expR{al.get('expR')} | "
          f"train PF{tr.get('PF')} test PF{te.get('PF')} | random-day 95th pct expR {np.percentile(rnd, 95):.3f}")


def run_round2() -> None:
    print("=== h1_dip: intraday dip-buy, flat before rollover (R per trade) ===")
    for key in ("US500", "NAS100", "US30", "GER40", "GOLD", "EURUSD", "GBPUSD", "USDJPY"):
        df = load(key, "H1")
        split = int(len(df) * 0.7)
        for lvl in (5, 10, 20):
            res = h1_dip(key, lvl)
            tr = Result(res.r[res.t < split], res.t[res.t < split]).stats()
            te = Result(res.r[res.t >= split], res.t[res.t >= split]).stats()
            print(f"  {key:7s} rsi2<{lvl:<3d} TRAIN n{tr.get('n')} win{tr.get('win%')} PF{tr.get('PF')} "
                  f"expR{tr.get('expR')} | TEST n{te.get('n')} win{te.get('win%')} PF{te.get('PF')} "
                  f"expR{te.get('expR')}", flush=True)
    print("\n=== session drift ===")
    for key in ("GOLD", "NAS100", "US500", "GER40"):
        session_drift(key)
    print("\n=== pre-holiday (US indices, D1) ===")
    for sym in ("US500Cash", "US100Cash", "US30Cash", "US2000Cash"):
        pre_holiday(sym)


def gold_session(key: str = "GOLD", stop_atr_d: float | None = 1.0) -> None:
    """Time-of-day drift on GOLD: long from the open of hour a to the open of hour b (server time), every
    weekday, flat before the 00:00 rollover (no swap). Optional protective stop = stop_atr_d x daily
    ATR(14) (known at entry). Cost = per-bar spread at entry AND exit (both sides, worst case)."""
    df = load(key, "H1")
    df["date"], df["hr"], df["yr"] = df.time.dt.date, df.time.dt.hour, df.time.dt.year
    df = df[df.time >= "2013-01-01"].reset_index(drop=True)
    sp = _bar_spread(key, df)
    print(f"\n--- {key} median spread by server hour (price units) ---")
    print(pd.Series(sp, index=df.hr).groupby(level=0).median().round(3).to_dict())
    from strategy_lab import daily

    d1 = daily(key)
    d1["atr"] = atr(d1, 14).shift()  # yesterday's ATR, known before today's session
    atr_by_day = dict(zip(d1.time.dt.date, d1.atr, strict=False))
    o, h, lo = df.open.to_numpy(), df.high.to_numpy(), df.low.to_numpy()
    by_day = df.groupby("date").indices

    def trade(a_: int, b_: int, d: int) -> tuple[np.ndarray, np.ndarray]:
        r, yrs = [], []
        for day, idx in by_day.items():
            hrs = df.hr.to_numpy()[idx]
            if a_ not in hrs or b_ not in hrs:
                continue
            ia, ib = idx[np.where(hrs == a_)[0][0]], idx[np.where(hrs == b_)[0][0]]
            a_d = atr_by_day.get(day, np.nan)
            if not np.isfinite(a_d):
                continue
            entry = o[ia] + (sp[ia] if d == 1 else 0)
            exit_px = o[ib] - (0 if d == 1 else -sp[ib])
            if stop_atr_d:
                stop = entry - d * stop_atr_d * a_d
                seg = slice(ia, ib)
                if (d == 1 and lo[seg].min() <= stop) or (d == -1 and h[seg].max() + sp[ia] >= stop):
                    exit_px = stop
            r.append(d * (exit_px - entry) / a_d)  # in units of daily ATR
            yrs.append(day.year)
        return np.array(r), np.array(yrs)

    print(f"\n--- {key} session grid (R = daily ATR; stop {stop_atr_d} x daily ATR) ---")
    for d, starts, ends in ((1, (1, 2, 3), (6, 7, 8, 9, 10, 11)), (-1, (9, 10, 11), (14, 15, 16, 17))):
        for a_ in starts:
            for b_ in ends:
                r, yrs = trade(a_, b_, d)
                cut = int(len(r) * 0.7)
                tr, te, al = (Result(r[:cut], np.arange(cut)).stats(),
                              Result(r[cut:], np.arange(len(r) - cut)).stats(), Result(r, np.arange(len(r))).stats())
                yearly = pd.Series(r).groupby(yrs).mean()
                print(f"  {'LONG ' if d == 1 else 'SHORT'} {a_:02d}->{b_:02d}  n{al['n']} win{al['win%']} PF{al['PF']} "
                      f"exp{al['expR']} DD{al['maxDD_R']} | train PF{tr['PF']} test PF{te['PF']} | "
                      f"yrs+ {int((yearly > 0).sum())}/{len(yearly)}")
    r, yrs = trade(1, 9, 1)
    print("\n  LONG 01->09 by year (mean daily-ATR units, n):")
    print("  ", {int(y): (round(float(g.mean()), 3), len(g)) for y, g in pd.Series(r).groupby(yrs)})


# ---------------------------------------------------------------- crypto intraday (no swap: flat before 23:00 server)
def _crypto_h1(sym: str) -> pd.DataFrame:
    df = pd.read_csv(Path("data/lab") / f"{sym}_H1.csv", parse_dates=["time"])
    df = df[df.time >= "2018-01-01"].reset_index(drop=True)  # thin/odd early broker history before 2018
    recent_frac = float((df.spread * 0.01 / df.close).tail(3000).median())
    df["sp"] = 1.5 * np.maximum(df.spread * 0.01 / df.close, recent_frac)  # fraction of price, 1.5x
    df["date"], df["hr"] = df.time.dt.date, df.time.dt.hour
    return df


def crypto_vbo_intraday(sym: str, k: float, flat_h: int = 23, stop_mode: str = "open") -> np.ndarray:
    """Williams breakout inside one server day: buy stop at day open + k x yesterday's range (sell stop
    mirror). Stop: the day's open ('open') or 1 x yesterday's range beyond entry ('range'). Flat at the
    flat_h bar's open. H1 granularity: both trigger levels in the same bar -> counted as a full loss."""
    df = _crypto_h1(sym)
    days = df.groupby("date")
    daily_ = days.agg(o=("open", "first"), h=("high", "max"), lo=("low", "min"))
    prev_rng = (daily_.h - daily_.lo).shift()
    out = []
    for day, g in days:
        rng = prev_rng.get(day, np.nan)
        if not np.isfinite(rng) or rng <= 0 or len(g) < 20:
            continue
        o0 = g.open.iloc[0]
        up, dn = o0 + k * rng, o0 - k * rng
        pos = None
        for _, b in g.iterrows():
            if b.hr >= flat_h:
                break
            if pos is None:
                hu, hd = b.high >= up, b.low <= dn
                if hu and hd:
                    out.append((-1.0, day))
                    pos = "done"
                    break
                if hu or hd:
                    d = 1 if hu else -1
                    entry = (max(up, b.open) if d == 1 else min(dn, b.open))
                    stop = o0 if stop_mode == "open" else entry - d * rng
                    risk = abs(entry - stop)
                    if risk <= 0:
                        pos = "done"
                        break
                    pos = (d, entry, stop, risk, b.sp)
                    if (d == 1 and b.low <= stop) or (d == -1 and b.high >= stop):  # same bar: assume stopped
                        out.append(((-risk - b.sp * entry) / risk, day))
                        pos = "done"
                        break
                    continue
            else:
                d, entry, stop, risk, sp = pos
                if (d == 1 and b.low <= stop) or (d == -1 and b.high >= stop):
                    exit_px = min(stop, b.open) if d == 1 else max(stop, b.open)
                    out.append((((exit_px - entry) * d - sp * entry) / risk, day))
                    pos = "done"
                    break
        if isinstance(pos, tuple):
            d, entry, stop, risk, sp = pos
            last = g[g.hr >= flat_h]
            exit_px = last.open.iloc[0] if len(last) else g.close.iloc[-1]
            out.append((((exit_px - entry) * d - sp * entry) / risk, day))
    return np.array(out, dtype=object)


def crypto_hour_table(sym: str) -> None:
    df = _crypto_h1(sym)
    df["ret"] = (df.close / df.open - 1) * 1e4
    df["yr"] = df.time.dt.year
    tab = df.pivot_table(index="hr", columns="yr", values="ret", aggfunc="mean").round(1)
    tab["yrs+"] = (tab > 0).sum(axis=1)
    print(f"\n--- {sym}: mean gross bp per server hour, by year (yrs+ = years positive of {tab.shape[1] - 1}) ---")
    print(tab.to_string())


def run_crypto() -> None:
    for sym in ("BTCUSD", "ETHUSD"):
        print(f"\n=== {sym} intraday VBO (R; flat 23:00 server, no swap) ===")
        for stop_mode in ("open", "range"):
            for k in (0.3, 0.5, 0.7, 1.0):
                res = crypto_vbo_intraday(sym, k, stop_mode=stop_mode)
                if not len(res):
                    continue
                r = res[:, 0].astype(float)
                yrs = np.array([d.year for d in res[:, 1]])
                cut = int(len(r) * 0.7)
                tr, te, al = (Result(r[:cut], np.arange(cut)).stats(), Result(r[cut:], np.arange(len(r) - cut)).stats(),
                              Result(r, np.arange(len(r))).stats())
                yearly = pd.Series(r).groupby(yrs).sum().round(1)
                print(f"  stop={stop_mode:5s} k={k}  n{al['n']} win{al['win%']} PF{al['PF']} exp{al['expR']} "
                      f"DD{al['maxDD_R']} | train PF{tr['PF']} test PF{te['PF']} | R/yr {yearly.to_dict()}", flush=True)
        crypto_hour_table(sym)


# ---------------------------------------------------------------- round 3
PAIRS = [
    ("US500Cash", "US30Cash"), ("US500Cash", "US100Cash"), ("US100Cash", "US2000Cash"), ("GER40Cash", "EU50Cash"),
    ("FRA40Cash", "EU50Cash"), ("UK100Cash", "EU50Cash"), ("GOLD", "SILVER"), ("EURUSD", "GBPUSD"),
    ("AUDUSD", "NZDUSD"), ("EURJPY", "GBPJPY"), ("BTCUSD", "ETHUSD"), ("OILCash", "BRENTCash"),
    ("US500Cash", "GER40Cash"), ("AUS200Cash", "JP225Cash"),
]


def pairs_revert(a_sym: str, b_sym: str, look: int = 20, z_in: float = 2.0, max_days: int = 10) -> pd.DataFrame:
    """Daily log-ratio z-score (look-day mean/std). |z| > z_in at the close -> next open: short the rich
    leg, long the cheap leg, equal notional. Exit next open after z crosses 0, or after max_days.
    P&L in bp of one leg's notional, net of both legs' spread (1.5x) and each leg's swap charge."""
    meta = json.loads((D1 / "meta.json").read_text())
    ma, mb = _conservative(meta[a_sym]), _conservative(meta[b_sym])
    a = pd.read_csv(D1 / f"{a_sym}.csv", parse_dates=["time"]).set_index("time")
    b = pd.read_csv(D1 / f"{b_sym}.csv", parse_dates=["time"]).set_index("time")
    j = a[["open", "close"]].join(b[["open", "close"]], lsuffix="_a", rsuffix="_b", how="inner").reset_index()
    lr = np.log(j.close_a / j.close_b)
    z = ((lr - lr.rolling(look).mean()) / lr.rolling(look).std()).to_numpy()
    oa, ob, t = j.open_a.to_numpy(), j.open_b.to_numpy(), j.time.to_numpy()
    rows, i, n = [], look + 1, len(j) - 1
    while i < n:
        if not np.isfinite(z[i]) or abs(z[i]) < z_in:
            i += 1
            continue
        da = -1 if z[i] > 0 else 1  # a rich -> short a, long b
        e = i + 1
        k = e
        while k < n and (k - e + 1) < max_days and not (np.sign(z[k]) != np.sign(z[i]) or z[k] == 0):
            k += 1
        x = k + 1 if k < n else n
        nights = max(int((t[x] - t[e]) / np.timedelta64(1, "D")), 0)
        ra = da * (oa[x] / oa[e] - 1)
        rb = -da * (ob[x] / ob[e] - 1)
        swap = nights * ((ma["swap_long_frac"] if da == 1 else ma["swap_short_frac"])
                         + (mb["swap_long_frac"] if da == -1 else mb["swap_short_frac"]))
        net = (ra + rb) / 2 - (ma["spread_frac"] + mb["spread_frac"]) / 2 - swap / 2
        rows.append((t[e], net * 1e4))
        i = x
    return pd.DataFrame(rows, columns=["time", "bp"])


def _bp_stats(df: pd.DataFrame, test_frac: float = 0.3) -> str:
    if df.empty:
        return "n0"
    cut = int(len(df) * (1 - test_frac))

    def s(x: pd.Series) -> str:
        w, l_ = x[x > 0].sum(), -x[x <= 0].sum()
        return f"n{len(x)} win{100 * (x > 0).mean():.0f}% PF{(w / l_ if l_ > 0 else np.inf):.2f} avg{x.mean():.1f}bp"

    return f"ALL {s(df.bp)} | TRAIN {s(df.bp.iloc[:cut])} | TEST {s(df.bp.iloc[cut:])}"


def halloween(sym: str) -> None:
    """Long from the first trading day of November to the last of April, every year, vs. the other half.
    Net of 1.5x spread once per year and the broker's long swap every calendar night."""
    df, m = _load_d1(sym)
    df["y"], df["mo"] = df.time.dt.year, df.time.dt.month
    rows = []
    for y in range(df.y.min(), df.y.max()):
        w = df[((df.y == y) & (df.mo >= 11)) | ((df.y == y + 1) & (df.mo <= 4))]
        s = df[(df.y == y + 1) & (df.mo >= 5) & (df.mo <= 10)]
        for name, seg in (("winter", w), ("summer", s)):
            if len(seg) < 100:
                continue
            nights = (seg.time.iloc[-1] - seg.time.iloc[0]).days
            gross = seg.close.iloc[-1] / seg.open.iloc[0] - 1
            rows.append((name, y, 100 * (gross - m["spread_frac"] - nights * m["swap_long_frac"]), 100 * gross))
    r = pd.DataFrame(rows, columns=["half", "yr", "net%", "gross%"])
    g = r.groupby("half")
    print(f"  {sym:11s} " + " | ".join(
        f"{h}: n{len(x)} net avg {x['net%'].mean():+.1f}% (gross {x['gross%'].mean():+.1f}%) yrs+ {(x['net%'] > 0).sum()}"
        for h, x in g))


def fresh_breakout_dip(df: pd.DataFrame, m: dict, side: int = 1) -> Result:
    """Momentum-gated dip: a 20-day high within the last 10 days and close > SMA50, then RSI(2) < 10 ->
    buy next open; exit next open after a close above the prior day's high, or 5 days; 2 x ATR stop.
    side=-1 mirrors it (fresh 20-day low, close < SMA50, RSI(2) > 90)."""
    from strategy_lab import rsi

    c, h, lo = df.close, df.high, df.low
    sma50, r2 = c.rolling(50).mean(), rsi(c, 2)
    if side == 1:
        fresh = (h >= h.rolling(20).max()).rolling(10).max().astype(bool)
        e = np.where(fresh & (c > sma50) & (r2 < 10), 1, 0)
    else:
        fresh = (lo <= lo.rolling(20).min()).rolling(10).max().astype(bool)
        e = np.where(fresh & (c < sma50) & (r2 > 90), -1, 0)
    return sim_next_open(df, m, e, (c > h.shift()).to_numpy(), (c < lo.shift()).to_numpy(), 5)


def big_move(df: pd.DataFrame, m: dict, mode: str) -> Result:
    """Day after a close-to-close move larger than 2 x ATR(14): 'fade' trades against it, 'follow' with it.
    Enter next open, exit that day's close (no swap)."""
    c = df.close
    a = atr(df, 14).shift()
    mv = c - c.shift()
    d = np.where(mv > 2 * a, 1, np.where(mv < -2 * a, -1, 0))
    if mode == "fade":
        d = -d
    none = np.zeros(len(df), bool)
    return sim_next_open(df, m, d, none, none, 5, hold_close=1)


def fbd_param(df: pd.DataFrame, m: dict, look: int, recent: int, rsi_lvl: float, max_days: int = 5) -> Result:
    from strategy_lab import rsi

    c, h = df.close, df.high
    fresh = (h >= h.rolling(look).max()).rolling(recent).max().astype(bool)
    e = np.where(fresh & (c > c.rolling(50).mean()) & (rsi(c, 2) < rsi_lvl), 1, 0)
    x = (c > h.shift()).to_numpy()
    return sim_next_open(df, m, e, x, x, max_days)


def bigmove_param(df: pd.DataFrame, m: dict, k: float, hold: int) -> Result:
    c = df.close
    a = atr(df, 14).shift()
    mv = c - c.shift()
    d = np.where(mv > k * a, 1, np.where(mv < -k * a, -1, 0))
    none = np.zeros(len(df), bool)
    return sim_next_open(df, m, d, none, none, hold + 1, hold_close=hold)


def robust3() -> None:
    from daily_lab import signals as dl_signals

    print("=== fresh-breakout dip: grid look{10,20,40} x recent{5,10} x rsi2<{5,10,15} ===")
    for sym in ("CHFJPY", "EURJPY", "GBPJPY", "US2000Cash", "GER40Cash", "HK50Cash", "JP225Cash", "BTCUSD",
                "ETHUSD", "US500Cash", "US100Cash"):
        df, m = _load_d1(sym)
        both = tot = 0
        pfs = []
        for look in (10, 20, 40):
            for recent in (5, 10):
                for lvl in (5, 10, 15):
                    tr, te, al = split_stats(fbd_param(df, m, look, recent, lvl), df, 0.3)
                    tot += 1
                    both += int((tr.get("expR") or -1) > 0 and (te.get("expR") or -1) > 0)
                    pfs.append(al.get("PF", 0))
        base = fbd_param(df, m, 20, 10, 10)
        e, xl, xs = dl_signals(df, "dip_long")
        dip = sim_next_open(df, m, e, xl, xs, 10).stats()
        busy = _dip_positions(df, m)
        only_new = Result(base.r[~busy[base.t - 1]], base.t[~busy[base.t - 1]]).stats() if base.n else {}
        print(f"  {sym:11s} combos +both {both}/{tot}, PF median {np.median(pfs):.2f} min {np.min(pfs):.2f} | "
              f"base {base.stats()} | dip_long PF {dip.get('PF')} n{dip.get('n')} | "
              f"not-in-Dip-trade PF {only_new.get('PF')} n{only_new.get('n')}")
        print("     windows:", " | ".join(_windows(base, df)))
    print("\n=== big-move follow (crypto): grid k{1.5,2,2.5,3} x hold{1,2,3} ===")
    for sym in ("BTCUSD", "ETHUSD"):
        df, m = _load_d1(sym)
        for k in (1.5, 2.0, 2.5, 3.0):
            for hold in (1, 2, 3):
                tr, te, al = split_stats(bigmove_param(df, m, k, hold), df, 0.3)
                print(f"  {sym} k{k} hold{hold}  n{al.get('n')} win{al.get('win%')} PF{al.get('PF')} "
                      f"exp{al.get('expR')} DD{al.get('maxDD_R')} | train PF{tr.get('PF')} test PF{te.get('PF')}")
        base = bigmove_param(df, m, 2.0, 1)
        print("     windows:", " | ".join(_windows(base, df)))


# ---------------------------------------------------------------- round 4
XS_INDICES = ["US500Cash", "US100Cash", "US30Cash", "GER40Cash", "UK100Cash", "FRA40Cash", "EU50Cash", "JP225Cash",
              "AUS200Cash", "HK50Cash", "SWI20Cash", "SpainCash", "IT40Cash", "NETH25Cash"]
XS_FX = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD", "EURJPY", "GBPJPY", "CHFJPY",
         "AUDJPY", "CADJPY", "EURGBP", "EURCHF"]


def _panel(syms: list[str]) -> tuple[dict[str, pd.DataFrame], dict[str, dict]]:
    meta = json.loads((D1 / "meta.json").read_text())
    out, metas = {}, {}
    for s in syms:
        if s not in meta:
            continue
        df = pd.read_csv(D1 / f"{s}.csv", parse_dates=["time"])
        df["date"] = df.time.dt.normalize()
        out[s] = df.set_index("date")
        metas[s] = _conservative(meta[s])
    return out, metas


def xs_weekly(syms: list[str], pick: str = "worst", k: int = 1, trend: bool = True) -> pd.DataFrame:
    """Every Friday close: rank markets by 5-day return / (ATR% x sqrt 5). Buy the k worst (or best) that
    are above their SMA200 (if trend) at Monday's open, sell at Friday's close. 5 swap nights charged
    (Wed triple). Reports net bp per pick and the same-week equal-weight average of ALL markets (beta)."""
    data, metas = _panel(syms)
    feats = {}
    for s, df in data.items():
        a = atr(df.reset_index(), 14).to_numpy() / df.close.to_numpy()
        feats[s] = pd.DataFrame({
            "z": (df.close / df.close.shift(5) - 1).to_numpy() / (a * np.sqrt(5)),
            "up": (df.close > df.close.rolling(200).mean()).to_numpy(),
            "o": df.open.to_numpy(), "c": df.close.to_numpy(),
        }, index=df.index)
    fridays = sorted(set().union(*[set(f.index[f.index.dayofweek == 4]) for f in feats.values()]))
    rows = []
    for fri in fridays:
        mon, nfri = fri + pd.Timedelta(days=3), fri + pd.Timedelta(days=7)
        cand, all_r = [], []
        for s, f in feats.items():
            if fri not in f.index or mon not in f.index or nfri not in f.index or not np.isfinite(f.at[fri, "z"]):
                continue
            m = metas[s]
            net = f.at[nfri, "c"] / f.at[mon, "o"] - 1 - m["spread_frac"] - 5 * m["swap_long_frac"]
            all_r.append(net)
            if not trend or f.at[fri, "up"]:
                cand.append((f.at[fri, "z"], s, net))
        if len(all_r) < len(feats) * 0.6 or not cand:
            continue
        cand.sort(key=lambda x: x[0], reverse=(pick == "best"))
        for _, s, net in cand[:k]:
            rows.append((mon, s, net * 1e4, float(np.mean(all_r)) * 1e4))
    return pd.DataFrame(rows, columns=["time", "sym", "bp", "bench_bp"])


def month_end_usd() -> pd.DataFrame:
    """Melvin-Prins month-end hedge rebalancing: at the close two trading days before month end, if US500
    has outperformed the average of GER40/UK100/JP225/EU50 month-to-date, sell USD on the last trading day
    (open -> close, no swap); if it underperformed, buy USD. Pooled over 6 USD pairs."""
    eq, _ = _panel(["US500Cash", "GER40Cash", "UK100Cash", "JP225Cash", "EU50Cash"])
    fx, fm = _panel(["EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCHF", "USDCAD"])
    usd_base = {"USDJPY", "USDCHF", "USDCAD"}
    closes = pd.DataFrame({s: d.close for s, d in eq.items()}).dropna()
    month = closes.index.to_period("M")
    rows = []
    for _, g in closes.groupby(month):
        if len(g) < 10:
            continue
        sig_day, last_day = g.index[-3], g.index[-1]
        prev_month_end = closes.index[closes.index < g.index[0]]
        if not len(prev_month_end):
            continue
        base = closes.loc[prev_month_end[-1]]
        mtd = closes.loc[sig_day] / base - 1
        diff = mtd["US500Cash"] - mtd[["GER40Cash", "UK100Cash", "JP225Cash", "EU50Cash"]].mean()
        usd_dir = -1 if diff > 0 else 1  # US outperformed -> foreigners sell USD
        for s, d in fx.items():
            if last_day not in d.index:
                continue
            d_pair = usd_dir if s in usd_base else -usd_dir
            o, c = d.at[last_day, "open"], d.at[last_day, "close"]
            net = d_pair * (c / o - 1) - fm[s]["spread_frac"]
            rows.append((last_day, s, net * 1e4, diff * 100))
    return pd.DataFrame(rows, columns=["time", "sym", "bp", "us_minus_rest_pct"])


def trend_pyramid(sym: str, k: float = 2.0, hold: int = 2) -> None:
    """Bot 4 (55/20 Donchian, 2xATR stop) alone vs. Bot 4 plus one add-on unit (same risk) after any day
    that moves > k x ATR in the open position's direction, held `hold` days. Equity in R, both sides."""
    from daily_lab import signals as dl_signals

    df, m = _load_d1(sym)
    o, h, lo, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    a = atr(df, 14).to_numpy()
    e, xl, xs = dl_signals(df, "breakout")
    base = sim_next_open(df, m, e, xl, xs, 120)
    pos = np.zeros(len(df), int)
    for j, r in zip(base.t, base.r, strict=False):
        d = int(e[j - 1])
        stop = o[j] - d * 2 * a[j - 1]
        k_ = j
        while k_ < len(df) - 1:
            pos[k_] = d
            if (d == 1 and lo[k_] <= stop) or (d == -1 and h[k_] >= stop) or (xl[k_] if d == 1 else xs[k_]) \
                    or k_ - j + 1 >= 120:
                break
            k_ += 1
        _ = r
    mv = c - np.r_[np.nan, c[:-1]]
    a_prev = np.r_[np.nan, a[:-1]]
    big = np.where(mv > k * a_prev, 1, np.where(mv < -k * a_prev, -1, 0))
    add_sig = np.where((big != 0) & (big == pos), big, 0)
    none = np.zeros(len(df), bool)
    add = sim_next_open(df, m, add_sig, none, none, hold + 1, hold_close=hold)

    def curve(res_list: list[Result]) -> tuple[float, float, int]:
        ev = sorted((t, r) for res in res_list for t, r in zip(res.t, res.r, strict=False))
        eq = np.cumsum([r for _, r in ev]) if ev else np.array([0.0])
        return float(eq[-1]), float(np.max(np.maximum.accumulate(eq) - eq)), len(ev)

    b_tot, b_dd, b_n = curve([base])
    p_tot, p_dd, p_n = curve([base, add])
    tr, te, al = split_stats(add, df, 0.3)
    print(f"  {sym:9s} Bot4 alone: {b_n} trades, {b_tot:+.1f}R, maxDD {b_dd:.1f}R, R/DD {b_tot / max(b_dd, 1e-9):.2f} | "
          f"+add-ons: {p_n} trades, {p_tot:+.1f}R, maxDD {p_dd:.1f}R, R/DD {p_tot / max(p_dd, 1e-9):.2f} | "
          f"add-ons alone n{al.get('n')} PF{al.get('PF')} train {tr.get('PF')} test {te.get('PF')}")


def run_round4() -> None:
    print("=== cross-sectional weekly: buy the k most-oversold (vol-adjusted 5-day) markets in an uptrend ===")
    for name, syms in (("indices", XS_INDICES), ("fx", XS_FX)):
        for pick in ("worst", "best"):
            for k in (1, 2):
                r = xs_weekly(syms, pick, k)
                if r.empty:
                    continue
                r["excess"] = r.bp - r.bench_bp
                half = int(len(r) * 0.7)
                print(f"  {name:7s} {pick:5s} k{k}: {_bp_stats(r)} | excess vs all-market avg: "
                      f"train {r.excess.iloc[:half].mean():+.1f}bp test {r.excess.iloc[half:].mean():+.1f}bp", flush=True)
    print("\n=== month-end USD hedge-rebalancing flow (last trading day, open->close) ===")
    me = month_end_usd()
    print(f"  pooled 6 pairs: {_bp_stats(me)}")
    strong = me[me.us_minus_rest_pct.abs() > 2]
    print(f"  only months with |US - rest| > 2%: {_bp_stats(strong)}")
    print("  by pair:", me.groupby("sym").bp.mean().round(1).to_dict())
    print("\n=== Bot 4 pyramiding on big moves (k=2 ATR, hold 2) ===")
    for sym in ("BTCUSD", "ETHUSD", "JP225Cash", "Netflix"):
        trend_pyramid(sym)


GAP_SESS = {"US500": (1630, 2245), "NAS100": (1630, 2245), "US30": (1630, 2245), "GER40": (1000, 1815)}


def gap_fade(key: str, tf: str = "M15", thr: float = 0.5, spread_mult: float = 1.0, stop_atr: float | None = None,
             side: int = 0, entry_hm: int | None = None, exit_hm: int | None = None) -> pd.DataFrame:
    """Fade the overnight move into the cash open. Overnight = prior session's last bar close -> entry bar's
    open. If |overnight| >= thr x daily ATR%, trade against it from the entry bar's open to the exit bar's
    close (same day, no swap). side=+1 only buys gap-downs, -1 only sells gap-ups, 0 both. Optional stop at
    stop_atr x daily ATR from entry (checked on bar highs/lows; worst case if touched). Net bp of price."""
    open_hm, close_hm = GAP_SESS[key]
    if tf == "H1":
        open_hm, close_hm = (1700, 2200) if open_hm == 1630 else (1000, 1800)
    elif tf == "M30":
        close_hm = 2230 if open_hm == 1630 else 1800
    e_hm, x_hm = entry_hm or open_hm, exit_hm or close_hm
    df = load(key, tf)
    df["date"], df["hm"] = df.time.dt.date, df.time.dt.hour * 100 + df.time.dt.minute
    df["sp"] = spread_mult * cost(key, df) / df.close.to_numpy()
    d1 = df.groupby("date").agg(h=("high", "max"), lo=("low", "min"), c=("close", "last"))
    atrp = ((d1.h - d1.lo) / d1.c).rolling(14).mean().shift()
    days = {d: g.set_index("hm") for d, g in df.groupby("date")}
    keys = sorted(days)
    rows = []
    for prev, cur in zip(keys[:-1], keys[1:], strict=False):
        p, g = days[prev], days[cur]
        if close_hm not in p.index or e_hm not in g.index or x_hm not in g.index:
            continue
        a = atrp.get(cur, np.nan)
        if not np.isfinite(a):
            continue
        on = g.at[e_hm, "open"] / p.at[close_hm, "close"] - 1
        if abs(on) < thr * a:
            continue
        d = -1 if on > 0 else 1
        if side and d != side:
            continue
        entry = g.at[e_hm, "open"]
        exit_px = g.at[x_hm, "close"]
        if stop_atr:
            stop = entry * (1 - d * stop_atr * a)
            seg = g.loc[e_hm:x_hm]
            if (d == 1 and seg.low.min() <= stop) or (d == -1 and seg.high.max() >= stop):
                exit_px = stop
        rows.append((pd.Timestamp(cur), (d * (exit_px / entry - 1) - g.at[e_hm, "sp"]) * 1e4))
    return pd.DataFrame(rows, columns=["time", "bp"])


def robust_gap() -> None:
    for key in ("US500", "NAS100", "US30", "GER40"):
        print(f"\n=== {key} gap fade ===")
        for thr in (0.4, 0.5, 0.6, 0.75, 1.0):
            r = gap_fade(key, thr=thr)
            r2 = gap_fade(key, thr=thr, spread_mult=2.0)
            print(f"  M15 thr{thr}: {_bp_stats(r)}\n      2x spread: {_bp_stats(r2)}")
        print("  -- thr 0.5 variants --")
        for label, kw in (("buy gap-downs only", {"side": 1}), ("sell gap-ups only", {"side": -1}),
                          ("stop 0.5xATR", {"stop_atr": 0.5}), ("stop 1xATR", {"stop_atr": 1.0}),
                          ("exit 19:00/13:00", {"exit_hm": 1845 if key != "GER40" else 1245}),
                          ("enter +30m", {"entry_hm": 1700 if key != "GER40" else 1030})):
            print(f"  {label:18s}: {_bp_stats(gap_fade(key, thr=0.5, **kw))}")
        h = gap_fade(key, tf="H1", thr=0.5)
        if not h.empty:
            yr = h.groupby(h.time.dt.year).bp.agg(["count", "mean"]).round(1)
            print(f"  H1 long history (enter 17:00/10:00) thr0.5 from {h.time.iloc[0].date()}: {_bp_stats(h)}")
            print("   by year (n, mean bp):", {int(y): (int(v['count']), v['mean']) for y, v in yr.iterrows()})


def run_round3() -> None:
    print("=== pairs mean reversion (z>2 on 20-day log ratio; bp of one leg, net) ===")
    for a_, b_ in PAIRS:
        for z_in in (2.0, 2.5):
            print(f"  {a_}/{b_} z>{z_in}: {_bp_stats(pairs_revert(a_, b_, z_in=z_in))}", flush=True)
    print("\n=== Halloween (long Nov-Apr vs May-Oct, net of swap) ===")
    for sym in ("US500Cash", "US100Cash", "US30Cash", "GER40Cash", "UK100Cash", "EU50Cash", "FRA40Cash",
                "JP225Cash", "AUS200Cash"):
        halloween(sym)
    meta = {k: _conservative(v) for k, v in json.loads((D1 / "meta.json").read_text()).items()}
    rows = []
    for sym, m in meta.items():
        df = pd.read_csv(D1 / f"{sym}.csv", parse_dates=["time"])
        for name, fn in (("fbd_long", lambda d_, m_: fresh_breakout_dip(d_, m_, 1)),
                         ("fbd_short", lambda d_, m_: fresh_breakout_dip(d_, m_, -1)),
                         ("bigmove_fade", lambda d_, m_: big_move(d_, m_, "fade")),
                         ("bigmove_follow", lambda d_, m_: big_move(d_, m_, "follow"))):
            res = fn(df, m)
            tr, te, al = split_stats(res, df, 0.3)
            rows.append({"class": m["class"], "symbol": sym, "strategy": name, "n": al.get("n", 0),
                         "win%": al.get("win%"), "PF": al.get("PF"), "expR": al.get("expR"),
                         "maxDD_R": al.get("maxDD_R"), "train_n": tr.get("n", 0), "train_PF": tr.get("PF"),
                         "train_expR": tr.get("expR"), "test_n": te.get("n", 0), "test_PF": te.get("PF"),
                         "test_win%": te.get("win%"), "test_expR": te.get("expR")})
    out = pd.DataFrame(rows)
    out.to_csv(D1.parent / "edge_round3_results.csv", index=False)
    report(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["daily", "intraday", "robust_ibs", "robust_tom", "round2", "gold_session",
                                    "crypto", "round3", "robust3", "round4", "robust_gap"])
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--sym", nargs="*", default=[])
    a = ap.parse_args()
    if a.cmd == "daily":
        run_daily(a.test_frac, a.only)
    elif a.cmd == "intraday":
        run_intraday(a.test_frac)
    elif a.cmd == "round2":
        run_round2()
    elif a.cmd == "crypto":
        run_crypto()
    elif a.cmd == "round3":
        run_round3()
    elif a.cmd == "robust3":
        robust3()
    elif a.cmd == "round4":
        run_round4()
    elif a.cmd == "robust_gap":
        robust_gap()
    elif a.cmd == "gold_session":
        gold_session(stop_atr_d=1.0)
        gold_session(stop_atr_d=None)
    else:
        for s in a.sym:
            (robust_ibs if a.cmd == "robust_ibs" else robust_tom)(s)
