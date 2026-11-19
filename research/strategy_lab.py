"""Strategy lab: fast research backtests for candidate bot-3 strategies.

Not used by the live bots. Results are in R (1R = the stop-loss distance), net of spread.

    poetry run python research/strategy_lab.py fetch                 # cache MT5 history to data/lab
    poetry run python research/strategy_lab.py run [--test-frac 0.3] # grid on train, report on test

Rules that keep the numbers honest:
  * signal on a closed bar, entry at the NEXT bar's open (no look-ahead)
  * buys fill at ask (bid + spread), exits on the bid; sells the opposite
  * if stop and target are both touched inside one bar, the STOP counts (worst case)
  * parameters are chosen on the first (1 - test_frac) of history only; the test slice is untouched
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

LAB = Path("data/lab")

# symbol -> (MT5 name, spread in price units used for costs (None = live spread x1.5), session open hour in server time)
MARKETS: dict[str, tuple[str, float | None, float]] = {
    "GOLD": ("GOLD", None, 10.0),
    "NAS100": ("US100Cash", None, 16.5),
    "US500": ("US500Cash", None, 16.5),
    "US30": ("US30Cash", None, 16.5),
    "GER40": ("GER40Cash", None, 10.0),
    "EURUSD": ("EURUSD", None, 10.0),
    "GBPUSD": ("GBPUSD", None, 10.0),
    "USDJPY": ("USDJPY", None, 10.0),
    "GBPJPY": ("GBPJPY", None, 10.0),
    "EURJPY": ("EURJPY", None, 10.0),
    "AUDUSD": ("AUDUSD", None, 10.0),
    "EURGBP": ("EURGBP", None, 10.0),
}
TIMEFRAMES = {"M15": 15, "H1": 60}


# ---------------------------------------------------------------- data
def fetch(bars: int = 99_000) -> None:
    import MetaTrader5 as mt5  # noqa: N813

    if not mt5.initialize():
        sys.exit(f"MT5 initialize failed: {mt5.last_error()}")
    LAB.mkdir(parents=True, exist_ok=True)
    spreads = {}
    tf_map = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
    for key, (sym, _, _) in MARKETS.items():
        mt5.symbol_select(sym, True)
        info = mt5.symbol_info(sym)
        spreads[key] = info.spread * info.point
        for tf_name, tf in tf_map.items():
            r = mt5.copy_rates_from_pos(sym, tf, 0, bars)
            if r is None or len(r) == 0:
                print(f"{key} {tf_name}: no data ({mt5.last_error()})")
                continue
            df = pd.DataFrame(r)[["time", "open", "high", "low", "close", "spread"]]
            df["time"] = pd.to_datetime(df["time"], unit="s")  # server time
            df.to_csv(LAB / f"{key}_{tf_name}.csv", index=False)
            print(f"{key} {tf_name}: {len(df)} bars {df.time.iloc[0]} .. {df.time.iloc[-1]}")
    (LAB / "spreads.json").write_text(json.dumps(spreads, indent=1))
    mt5.shutdown()


def load(key: str, tf: str) -> pd.DataFrame | None:
    p = LAB / f"{key}_{tf}.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["time"])


SPREAD_MODE = {"mode": "typical", "mult": 1.2}


def point(key: str) -> float:
    if key in ("GOLD", "NAS100", "US500", "US30", "GER40"):
        return 0.01
    return 0.001 if key.endswith("JPY") else 1e-5


def cost(key: str, df: pd.DataFrame) -> np.ndarray:
    """Spread cost in price, per bar, scaled by price so old history (gold at $300) is not charged
    today's absolute spread.
      typical: mult x the median spread of the last 3 months of M15 bars during active hours (10-22 server)
      live:    mult x the spread at fetch time (can be a quiet, wide-spread hour)"""
    fixed = MARKETS[key][1]
    if fixed is not None:
        spread_now = fixed
    elif SPREAD_MODE["mode"] == "live":
        spread_now = SPREAD_MODE["mult"] * json.loads((LAB / "spreads.json").read_text())[key]
    else:
        m15 = load(key, "M15")
        recent = m15[m15.time >= m15.time.iloc[-1] - pd.Timedelta(days=90)]
        h = recent.time.dt.hour
        spread_now = SPREAD_MODE["mult"] * recent[(h >= 10) & (h < 22)].spread.median() * point(key)
    return (spread_now / df.close.iloc[-1]) * df.close.to_numpy()


# ---------------------------------------------------------------- indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(
        axis=1
    )
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df.high.diff(), -df.low.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * pd.Series(mdm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


# ---------------------------------------------------------------- strategies -> signal arrays (+1 buy, -1 sell, 0 none)
def sig_trend_pullback(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Trade with the big trend after a dip: EMA50 vs EMA200 trend, RSI dips past a level then crosses back."""
    c = df.close
    e50, e200, r = ema(c, 50), ema(c, 200), rsi(c, 14)
    lvl = p["rsi"]
    up = (e50 > e200) & (c > e200) & (r.shift() < lvl) & (r >= lvl)
    dn = (e50 < e200) & (c < e200) & (r.shift() > 100 - lvl) & (r <= 100 - lvl)
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_orb(df: pd.DataFrame, p: dict, open_hour: float) -> np.ndarray:
    """Opening-range breakout: range of the first hour after the session opens; first close beyond it
    within the next 3 hours, in the direction of the EMA200 (optional)."""
    t = df.time
    hour = t.dt.hour + t.dt.minute / 60
    day = t.dt.date
    in_range = (hour >= open_hour) & (hour < open_hour + 1)
    window = (hour >= open_hour + 1) & (hour < open_hour + 4)
    hi = df.high.where(in_range).groupby(day).transform("max")
    lo = df.low.where(in_range).groupby(day).transform("min")
    e200 = ema(df.close, 200)
    long_ok = (df.close > e200) if p["trend"] else True
    short_ok = (df.close < e200) if p["trend"] else True
    up = window & (df.close > hi) & long_ok
    dn = window & (df.close < lo) & short_ok
    s = pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=df.index)
    first = s.ne(0) & ~s.ne(0).groupby(day).cumsum().gt(1)  # first breakout of the day only
    return np.where(first, s, 0)


def sig_mean_reversion(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Fade over-stretched moves in quiet markets: ADX low, close outside Bollinger(20,2), RSI extreme."""
    c = df.close
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    r, a = rsi(c, 14), adx(df, 14)
    quiet = a < p["adx"]
    up = quiet & (c < mid - 2 * sd) & (r < 30)
    dn = quiet & (c > mid + 2 * sd) & (r > 70)
    return np.where(up, 1, np.where(dn, -1, 0))


# ---------------------------------------------------------------- simulator
@dataclass
class Result:
    r: np.ndarray  # R per trade, net of spread
    t: np.ndarray  # entry bar index

    @property
    def n(self) -> int:
        return len(self.r)

    def stats(self) -> dict:
        if self.n == 0:
            return {"n": 0}
        w, l_ = self.r[self.r > 0], self.r[self.r <= 0]
        eq = np.cumsum(self.r)
        dd = float(np.max(np.maximum.accumulate(eq) - eq)) if self.n else 0.0
        return {
            "n": self.n,
            "win%": round(100 * len(w) / self.n, 1),
            "PF": round(w.sum() / -l_.sum(), 2) if l_.sum() < 0 else float("inf"),
            "expR": round(float(self.r.mean()), 3),
            "totR": round(float(self.r.sum()), 1),
            "maxDD_R": round(dd, 1),
        }


def simulate(
    df: pd.DataFrame, sig: np.ndarray, sl_atr: float, rr: float, spreads: np.ndarray, max_hold: int
) -> Result:
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = atr(df, 14).to_numpy()
    out_r, out_t = [], []
    i, n = 200, len(df) - 1
    while i < n:
        d = sig[i]
        if d == 0 or not np.isfinite(a[i]) or a[i] <= 0:
            i += 1
            continue
        spread = spreads[i]
        dist = sl_atr * a[i]
        j = i + 1
        if d == 1:
            entry = o[j] + spread
            sl, tp = entry - dist, entry + rr * dist
        else:
            entry = o[j]
            sl, tp = entry + dist, entry - rr * dist
        exit_px = None
        k = j
        while k <= min(n, j + max_hold):
            if d == 1:
                if lo[k] <= sl:
                    exit_px = sl
                elif h[k] >= tp:
                    exit_px = tp
            else:
                if h[k] + spread >= sl:
                    exit_px = sl
                elif lo[k] + spread <= tp:
                    exit_px = tp
            if exit_px is not None:
                break
            k += 1
        if exit_px is None:  # time exit
            k = min(n, j + max_hold)
            exit_px = c[k] if d == 1 else c[k] + spread
        pnl = (exit_px - entry) if d == 1 else (entry - exit_px)
        out_r.append(pnl / dist)
        out_t.append(j)
        i = k + 1  # one position at a time
    return Result(np.array(out_r), np.array(out_t))


# ---------------------------------------------------------------- daily dip-buy (long only, exit on strength)
def daily(key: str) -> pd.DataFrame | None:
    """Daily bars built from H1. XM server time is NY-close aligned (UTC+2/+3), so the server day is the trading day."""
    h1 = load(key, "H1")
    if h1 is None:
        return None
    d = (
        h1.set_index("time")
        .resample("1D")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "spread": "median"})
        .dropna()
        .reset_index()
    )
    return d[d.time.dt.dayofweek < 5].reset_index(drop=True)  # drop thin Sunday stubs


def sig_dip_buy(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Long-only: market above its 200-day average (uptrend) and RSI(2) deeply oversold (sharp short dip)."""
    c = df.close
    up = (c > c.rolling(200).mean()) & (rsi(c, 2) < p["rsi2"])
    return np.where(up, 1, 0)


SWAP_PER_NIGHT = (
    0.0002  # long financing as a fraction of price per calendar night (XM: ~0.018% on indices/gold)
)


def simulate_dip(
    df: pd.DataFrame, sig: np.ndarray, sl_atr: float, max_days: int, spreads: np.ndarray
) -> Result:
    """Enter next open; exit at next open after a close above the 5-day average, at the hard stop
    (sl_atr x ATR14), or after max_days. Costs: spread on entry + swap per calendar night held."""
    o, lo, c = df.open.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    t = df.time.to_numpy()
    a = atr(df, 14).to_numpy()
    sma5 = df.close.rolling(5).mean().to_numpy()
    out_r, out_t = [], []
    i, n = 200, len(df) - 1
    while i < n:
        if sig[i] != 1 or not np.isfinite(a[i]):
            i += 1
            continue
        j = i + 1
        entry = o[j] + spreads[i]
        dist = sl_atr * a[i]
        sl = entry - dist
        k, exit_px = j, None
        while k < n:
            if lo[k] <= sl:
                exit_px = min(sl, o[k])  # gap through the stop fills at the open
                break
            if c[k] > sma5[k] or k - j + 1 >= max_days:
                exit_px = o[k + 1]
                k += 1
                break
            k += 1
        if exit_px is None:
            break
        nights = (t[k] - t[j]).astype("timedelta64[D]").astype(int)
        swap = SWAP_PER_NIGHT * entry * max(nights, 0)
        out_r.append((exit_px - entry - swap) / dist)
        out_t.append(j)
        i = k + 1
    return Result(np.array(out_r), np.array(out_t))


def run_dip(test_frac: float) -> None:
    for key in ("US500", "NAS100", "US30", "GER40", "GOLD"):
        df = daily(key)
        if df is None:
            continue
        sp = cost(key, df)
        split = int(len(df) * (1 - test_frac))
        print(
            f"\n{key} daily {df.time.iloc[0].date()}..{df.time.iloc[-1].date()}  test from {df.time.iloc[split].date()}"
        )
        for r2 in (5, 10, 15):
            sig = sig_dip_buy(df, {"rsi2": r2})
            for sl in (2.0, 3.0):
                res = simulate_dip(df, sig, sl, 10, sp)
                tr = Result(res.r[res.t < split], res.t[res.t < split]).stats()
                te = Result(res.r[res.t >= split], res.t[res.t >= split]).stats()
                print(f"  rsi2<{r2:2d} stop {sl}xATR | TRAIN {tr} | TEST {te}")


# ---------------------------------------------------------------- experiment
STRATS = {
    "trend_pullback": ("H1", sig_trend_pullback, {"rsi": [40, 45]}, 48),
    "orb": ("M15", None, {"trend": [True, False]}, 24),
    "mean_reversion": ("M15", sig_mean_reversion, {"adx": [20, 25]}, 32),
}
SL_GRID, RR_GRID = [1.0, 1.5, 2.0], [1.0, 1.5, 2.0]


def signals(name: str, df: pd.DataFrame, p: dict, key: str) -> np.ndarray:
    if name == "orb":
        return sig_orb(df, p, MARKETS[key][2])
    return STRATS[name][1](df, p)


def run(test_frac: float, only: list[str] | None) -> None:
    rows = []
    for name, (tf, _, pgrid, max_hold) in STRATS.items():
        if only and name not in only:
            continue
        for key in MARKETS:
            df = load(key, tf)
            if df is None or len(df) < 2000:
                continue
            sp_all = cost(key, df)
            split = int(len(df) * (1 - test_frac))
            train, test = df.iloc[:split].reset_index(drop=True), df.iloc[
                split - 250 :
            ].reset_index(drop=True)
            sp_tr, sp_te = sp_all[:split], sp_all[split - 250 :]
            best = None
            for pv in itertools.product(*pgrid.values()):
                p = dict(zip(pgrid.keys(), pv, strict=False))
                s_tr = signals(name, train, p, key)
                for sl, rr in itertools.product(SL_GRID, RR_GRID):
                    st = simulate(train, s_tr, sl, rr, sp_tr, max_hold).stats()
                    if st["n"] < 30:
                        continue
                    score = st["expR"] * np.sqrt(st["n"])  # favour edges backed by more trades
                    if best is None or score > best[0]:
                        best = (score, p, sl, rr, st)
            if best is None:
                continue
            _, p, sl, rr, st_tr = best
            res_te = simulate(test, signals(name, test, p, key), sl, rr, sp_te, max_hold)
            res_te = Result(
                res_te.r[res_te.t >= 250], res_te.t[res_te.t >= 250]
            )  # drop warm-up overlap
            st_te = res_te.stats()
            years_te = (df.time.iloc[-1] - df.time.iloc[split]).days / 365.25
            rows.append(
                {
                    "strategy": name,
                    "market": key,
                    "tf": tf,
                    "from": str(df.time.iloc[0].date()),
                    "params": {**p, "sl_atr": sl, "rr": rr},
                    "train": st_tr,
                    "test": st_te,
                    "test_trades_per_month": round(st_te.get("n", 0) / max(years_te * 12, 1e-9), 1),
                }
            )
            print(f"{name:15s} {key:7s} train {st_tr} | TEST {st_te}", flush=True)
    LAB.mkdir(parents=True, exist_ok=True)
    (LAB / "results.json").write_text(json.dumps(rows, indent=1, default=str))
    print(f"\nwrote {LAB / 'results.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "run", "dip"])
    ap.add_argument("--bars", type=int, default=99_000)  # requesting exactly the terminal max fails
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--spread-mode", choices=["typical", "live"], default="typical")
    ap.add_argument("--spread-mult", type=float, default=1.2)
    a = ap.parse_args()
    SPREAD_MODE.update(mode=a.spread_mode, mult=a.spread_mult)
    if a.cmd == "fetch":
        fetch(a.bars)
    elif a.cmd == "dip":
        run_dip(a.test_frac)
    else:
        run(a.test_frac, a.only)
