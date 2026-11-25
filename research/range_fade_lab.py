"""Range-fade lab: prior-day high/low mean-reversion fade on M15.

Adrian's rule (confirmed 2026-11-25):
  Each trading day's High/Low (the previous weekday, weekend stubs dropped) marks two
  levels. On the next trading day's M15 bars:
    SELL trigger when price rises to within 5% of the prior day's range below the High
      (price >= High - 0.05*Range);  TP at the prior day's Low;  SL = High + 0.20*Range.
    BUY trigger (mirror) when price falls to within 5% of the range above the Low
      (price <= Low + 0.05*Range);   TP at the prior day's High; SL = Low - 0.20*Range.
  One trade per day (first unambiguous trigger wins, in time order). A bar that touches
  both triggers at once is skipped for THAT bar only (ambiguous, no intrabar order book) -
  scanning continues within the same day for a later, unambiguous bar.
  Fixed rule, no parameter grid - Adrian gave exact numbers, so this is a feasibility
  check, not a fit. MAX_BARS is a safety valve Adrian did not specify (he gave no time
  exit); flag this as an assumption when reporting results.

Cost/fill model: identical to strategy_lab.simulate() - typical 1.2x median M15 spread
(active hours, 90d) from strategy_lab.cost(); buys pay the spread entering (fill at ask),
sells pay it on exit (buy back at ask); if SL and TP are both touched in one bar, SL
counts (worst case, same convention as strategy_lab's docstring).

    poetry run python research/range_fade_lab.py --key GOLD
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy_lab import MARKETS, Result, adx, cost, load  # noqa: E402

MAX_BARS = 960  # ~10 trading days safety cap on holding time (not part of Adrian's rule)


def _prev_day_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Attach prev_high/prev_low/prev_range/prev_adx (previous WEEKDAY's values) to every M15 row.
    prev_adx is ADX(14) on the daily series as of the END of the previous day - known at the start
    of today, no look-ahead. Used by the trend filter: skip the fade when yesterday was already a
    strong-trend day (price more likely to keep running through today's levels than reverse)."""
    wd = df[df.time.dt.dayofweek < 5].copy()
    date = wd.time.dt.date
    daily = wd.groupby(date).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last")
    ).reset_index()
    daily.columns = ["date", "day_open", "day_high", "day_low", "day_close"]
    daily["day_adx"] = adx(daily.rename(columns={"day_open": "open", "day_high": "high", "day_low": "low", "day_close": "close"}), 14)
    daily["prev_high"] = daily.day_high.shift(1)
    daily["prev_low"] = daily.day_low.shift(1)
    daily["prev_range"] = daily.prev_high - daily.prev_low
    daily["prev_adx"] = daily.day_adx.shift(1)
    wd["date"] = date
    out = wd.merge(daily[["date", "prev_high", "prev_low", "prev_range", "prev_adx"]], on="date", how="left")
    return out.reset_index(drop=True)


def simulate(df: pd.DataFrame, spreads: np.ndarray, buffer_frac: float = 0.05, sl_frac: float = 0.20) -> Result:
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    date = df.date.to_numpy()
    prev_high, prev_low, rng = df.prev_high.to_numpy(), df.prev_low.to_numpy(), df.prev_range.to_numpy()
    sell_trig, buy_trig = prev_high - buffer_frac * rng, prev_low + buffer_frac * rng
    sl_sell, sl_buy = prev_high + sl_frac * rng, prev_low - sl_frac * rng
    tp_sell, tp_buy = prev_low, prev_high

    out_r, out_t = [], []
    n = len(df) - 1
    i, last_day = 0, None
    while i <= n:
        if not np.isfinite(rng[i]) or rng[i] <= 0:
            i += 1
            continue
        if date[i] == last_day:
            i += 1
            continue
        hit_sell, hit_buy = h[i] >= sell_trig[i], lo[i] <= buy_trig[i]
        if hit_sell == hit_buy:  # neither, or both (ambiguous) -> skip this bar only
            i += 1
            continue
        d = -1 if hit_sell else 1
        trig = sell_trig[i] if d == -1 else buy_trig[i]
        sl = sl_sell[i] if d == -1 else sl_buy[i]
        tp = tp_sell[i] if d == -1 else tp_buy[i]
        entry = trig + spreads[i] if d == 1 else trig
        dist = abs(entry - sl)
        j, exit_px = i, None
        while j <= min(n, i + MAX_BARS):
            if d == 1:
                if lo[j] <= sl:
                    exit_px = sl if j == i else min(sl, o[j])
                    break
                if h[j] >= tp:
                    exit_px = tp
                    break
            else:
                if h[j] + spreads[j] >= sl:
                    exit_px = sl if j == i else max(sl, o[j] + spreads[j])
                    break
                if lo[j] + spreads[j] <= tp:
                    exit_px = tp
                    break
            if j == min(n, i + MAX_BARS):
                exit_px = c[j] if d == 1 else c[j] + spreads[j]
                break
            j += 1
        pnl = (exit_px - entry) if d == 1 else (entry - exit_px)
        out_r.append(pnl / dist)
        out_t.append(i)
        last_day = date[i]
        i = j + 1
    return Result(np.array(out_r), np.array(out_t))


def simulate_confirmed(
    df: pd.DataFrame, spreads: np.ndarray, buffer_frac: float = 0.05, sl_frac: float = 0.20,
    trend_max: float | None = None,
) -> Result:
    """Enhancement (2026-11-25): the baseline rule fades the INSTANT price touches the zone,
    which loses everywhere (0/12 markets) because price usually keeps running through the level
    rather than reversing. This variant requires the touching bar to ALSO close back through the
    trigger as a same-bar rejection candle (close beyond the trigger, against open) before it
    counts as a signal - i.e. wait for proof of rejection, not just proximity. Entry moves to the
    NEXT bar's open (no look-ahead, since confirmation needs the full bar closed). A touch with no
    rejection is not a trade and does not block the rest of the day - scanning continues for a
    later, confirmed touch. trend_max (second enhancement): skip the day entirely if yesterday's
    daily ADX(14) was already >= trend_max (strong-trend day -> levels more likely to get run
    through than hold); None disables the filter."""
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    date = df.date.to_numpy()
    prev_high, prev_low, rng = df.prev_high.to_numpy(), df.prev_low.to_numpy(), df.prev_range.to_numpy()
    prev_adx = df.prev_adx.to_numpy()
    sell_trig, buy_trig = prev_high - buffer_frac * rng, prev_low + buffer_frac * rng
    sl_sell, sl_buy = prev_high + sl_frac * rng, prev_low - sl_frac * rng
    tp_sell, tp_buy = prev_low, prev_high

    out_r, out_t = [], []
    n = len(df) - 1
    i, last_day = 0, None
    while i < n:  # need bar i+1 to enter
        if not np.isfinite(rng[i]) or rng[i] <= 0:
            i += 1
            continue
        if trend_max is not None and (not np.isfinite(prev_adx[i]) or prev_adx[i] >= trend_max):
            i += 1
            continue
        if date[i] == last_day:
            i += 1
            continue
        hit_sell, hit_buy = h[i] >= sell_trig[i], lo[i] <= buy_trig[i]
        if hit_sell == hit_buy:  # neither, or both touched (ambiguous) -> no signal this bar
            i += 1
            continue
        d = -1 if hit_sell else 1
        confirmed = (c[i] < sell_trig[i] and c[i] < o[i]) if d == -1 else (c[i] > buy_trig[i] and c[i] > o[i])
        if not confirmed:
            i += 1
            continue
        sl = sl_sell[i] if d == -1 else sl_buy[i]
        tp = tp_sell[i] if d == -1 else tp_buy[i]
        e = i + 1
        entry = o[e] + spreads[e] if d == 1 else o[e]
        dist = abs(entry - sl)
        if dist <= 0:
            i += 1
            continue
        j, exit_px = e, None
        while j <= min(n, e + MAX_BARS):
            if d == 1:
                if lo[j] <= sl:
                    exit_px = sl if j == e else min(sl, o[j])
                    break
                if h[j] >= tp:
                    exit_px = tp
                    break
            else:
                if h[j] + spreads[j] >= sl:
                    exit_px = sl if j == e else max(sl, o[j] + spreads[j])
                    break
                if lo[j] + spreads[j] <= tp:
                    exit_px = tp
                    break
            if j == min(n, e + MAX_BARS):
                exit_px = c[j] if d == 1 else c[j] + spreads[j]
                break
            j += 1
        pnl = (exit_px - entry) if d == 1 else (entry - exit_px)
        out_r.append(pnl / dist)
        out_t.append(i)
        last_day = date[i]
        i = j + 1
    return Result(np.array(out_r), np.array(out_t))


def run_one(
    key: str, test_frac: float, buffer_frac: float, sl_frac: float, confirm: bool = False,
    trend_max: float | None = None,
) -> dict | None:
    raw = load(key, "M15")
    if raw is None:
        return None
    df = _prev_day_levels(raw)
    spreads = cost(key, df)
    split = int(len(df) * (1 - test_frac))
    res = (
        simulate_confirmed(df, spreads, buffer_frac, sl_frac, trend_max)
        if confirm
        else simulate(df, spreads, buffer_frac, sl_frac)
    )
    tr = Result(res.r[res.t < split], res.t[res.t < split]).stats()
    te = Result(res.r[res.t >= split], res.t[res.t >= split]).stats()
    al = res.stats()
    years = (df.time.iloc[-1] - df.time.iloc[0]).days / 365.25
    return {
        "market": key, "from": str(df.time.iloc[0].date()), "years": round(years, 1),
        "n": al.get("n", 0), "trades_yr": round(al.get("n", 0) / max(years, 0.1), 1),
        "win%": al.get("win%"), "PF": al.get("PF"), "expR": al.get("expR"), "maxDD_R": al.get("maxDD_R"),
        "train_n": tr.get("n", 0), "train_PF": tr.get("PF"), "train_expR": tr.get("expR"),
        "test_n": te.get("n", 0), "test_PF": te.get("PF"), "test_expR": te.get("expR"),
    }


def run(
    test_frac: float, buffer_frac: float, sl_frac: float, only: str | None, confirm: bool,
    trend_max: float | None,
) -> None:
    keys = [only] if only else list(MARKETS)
    rows = [r for k in keys if (r := run_one(k, test_frac, buffer_frac, sl_frac, confirm, trend_max)) is not None]
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    label = "CONFIRMED (rejection candle, entry next open)" if confirm else "BASELINE (blind touch)"
    print(f"Range-fade {label}  buffer={buffer_frac:.0%} sl={sl_frac:.0%}  across {len(out)} markets, "
          f"fixed rule, no per-market tuning:\n")
    print(out.to_string(index=False))
    ok = out[(out.n >= 40) & (out.train_expR > 0) & (out.test_expR > 0) & (out.test_n >= 15)]
    print(f"\n=== PASSED (positive expR in both train and test, n>=40, test_n>=15): {len(ok)} ===")
    if len(ok):
        print(ok.to_string(index=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=None, help="single market only (default: full MARKETS universe)")
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--buffer", type=float, default=0.05)
    ap.add_argument("--sl-frac", type=float, default=0.20)
    ap.add_argument("--confirm", action="store_true", help="require a same-bar rejection candle before entering")
    ap.add_argument("--trend-max", type=float, default=None, help="skip the day if prev daily ADX(14) >= this")
    a = ap.parse_args()
    run(a.test_frac, a.buffer, a.sl_frac, a.key, a.confirm, a.trend_max)
