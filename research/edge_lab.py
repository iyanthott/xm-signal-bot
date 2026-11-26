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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["daily", "intraday", "robust_ibs", "robust_tom", "round2", "gold_session",
                                    "crypto"])
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
    elif a.cmd == "gold_session":
        gold_session(stop_atr_d=1.0)
        gold_session(stop_atr_d=None)
    else:
        for s in a.sym:
            (robust_ibs if a.cmd == "robust_ibs" else robust_tom)(s)
