"""Research lab for "Power of 2" (Set 2): classic intraday day-trading archetypes NOT already
tested in this repo, looking for bots that each trade ~5-10x/day, aiming for 10-20 passing
archetypes (combined volume approaching 100+/day across the set -- NOT forced per-bot).

Already tested and REJECTED elsewhere, do not retest here:
  - ORB (EMA200-filtered opening-range breakout, M15, 12 markets): data/lab/results.json
    strategy=="orb" -- 0/12 markets positive in both train+test.
  - ADX+Bollinger mean-reversion fade (M15, 12 markets): same file, strategy=="mean_reversion"
    -- 0/12 positive in both halves.
  - EMA50/200 + RSI trend-pullback (H1, 12 markets): same file, strategy=="trend_pullback"
    -- 0/12 positive in both halves.
  - Daily momentum_burst/episodic_pivot (US stocks): data/lab/momentum_burst_results.csv --
    losers. Not reused here (different asset class/timeframe anyway).
  - The HF round's 6 families (ema_cross/bb_revert/donchian/twap_revert/rsi2_trend/big_bar_fade,
    GOLD+NAS100 M5/M15): hf_results.csv -- 0/24 combos positive in train.
  - Legacy LWMA+Stochastic chain strategy (retired 2026-11-24, confirmed losing live) -- the
    sig_stoch_cross here is a DIFFERENT, much simpler single-timeframe Stochastic cross, kept
    deliberately distinct, but is the same broad family -- treat any pass here with suspicion.

Costs/fills: reuses strategy_lab.py's simulate() (ATR stop, RR target, signal on closed bar,
entry at next open, buy at ask/sell at bid, worst-case stop-before-target on ambiguous bars,
1.2x typical-spread cost model) so every number here is directly comparable to results.json.
This is a generic exit model, not hand-tuned per archetype (e.g. VWAP reversion would ideally
exit at VWAP-touch, not a fixed RR) -- a limitation, noted in the report, not a defect to silently
work around.

    poetry run python research/power_of_2_lab.py fetch     # extra symbols/volume for this round
    poetry run python research/power_of_2_lab.py run        # grid on train, report on test
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy_lab import (  # noqa: E402
    LAB,
    MARKETS,
    Result,
    adx,
    atr,
    cost,
    ema,
    load,
    point,
    rsi,
    simulate,
)

EXTRA_SYMBOLS = {"SOLUSD": ("SOLUSD", None, 0.0)}  # extra crypto beyond BTC/ETH already in Set 1
ALL_MARKETS = {**MARKETS, **EXTRA_SYMBOLS, "BTCUSD": ("BTCUSD", None, 0.0), "ETHUSD": ("ETHUSD", None, 0.0)}


# ---------------------------------------------------------------- data (volume + extra symbols)
def fetch(bars: int = 99_000) -> None:
    import MetaTrader5 as mt5  # noqa: N813

    if not mt5.initialize():
        sys.exit(f"MT5 initialize failed: {mt5.last_error()}")
    LAB.mkdir(parents=True, exist_ok=True)
    tf_map = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
    targets = {"SOLUSD": "SOLUSD", "BTCUSD": "BTCUSD", "ETHUSD": "ETHUSD"}
    for key, sym in targets.items():
        if not mt5.symbol_select(sym, True):
            print(f"{key}: not selectable, skipping")
            continue
        for tf_name, tf in tf_map.items():
            p = LAB / f"{key}_{tf_name}.csv"
            if p.exists():
                continue
            r = mt5.copy_rates_from_pos(sym, tf, 0, bars)
            if r is None or len(r) == 0:
                print(f"{key} {tf_name}: no data ({mt5.last_error()})")
                continue
            df = pd.DataFrame(r)[["time", "open", "high", "low", "close", "spread", "tick_volume"]]
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df.to_csv(p, index=False)
            print(f"{key} {tf_name}: {len(df)} bars {df.time.iloc[0]} .. {df.time.iloc[-1]}")

    # add tick_volume to the already-cached M15 files used by every archetype below
    for key, (sym, _, _) in MARKETS.items():
        p = LAB / f"{key}_M15.csv"
        if not p.exists():
            continue
        existing = pd.read_csv(p, parse_dates=["time"])
        if "tick_volume" in existing.columns:
            continue
        r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, bars)
        if r is None or len(r) == 0:
            print(f"{key} M15 volume refetch failed")
            continue
        vol_df = pd.DataFrame(r)[["time", "tick_volume"]]
        vol_df["time"] = pd.to_datetime(vol_df["time"], unit="s")
        merged = existing.merge(vol_df, on="time", how="left")
        merged.to_csv(p, index=False)
        print(f"{key} M15: volume merged")

    # M5 for the FULL widened universe (prior round only had GER40/GOLD/NAS100/US30/US500 M5
    # cached from strategy_lab's earlier work) -- FX majors + crypto, so M5 coverage matches M15.
    m5_targets = {**{k: v[0] for k, v in MARKETS.items()}, **{k: v[0] for k, v in EXTRA_SYMBOLS.items()},
                  "BTCUSD": "BTCUSD", "ETHUSD": "ETHUSD"}
    for key, sym in m5_targets.items():
        p = LAB / f"{key}_M5.csv"
        if p.exists():
            continue
        if not mt5.symbol_select(sym, True):
            print(f"{key} M5: not selectable, skipping")
            continue
        r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, bars)
        if r is None or len(r) == 0:
            print(f"{key} M5: no data ({mt5.last_error()})")
            continue
        df = pd.DataFrame(r)[["time", "open", "high", "low", "close", "spread", "tick_volume"]]
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.to_csv(p, index=False)
        print(f"{key} M5: {len(df)} bars {df.time.iloc[0]} .. {df.time.iloc[-1]}")
    mt5.shutdown()


def load_ext(key: str, tf: str) -> pd.DataFrame | None:
    return load(key, tf)


# ---------------------------------------------------------------- shared helpers
def _day(df: pd.DataFrame) -> pd.Series:
    return df.time.dt.date


def macd(c: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> tuple[pd.Series, pd.Series]:
    line = ema(c, fast) - ema(c, slow)
    return line, ema(line, sig)


def bollinger(c: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = c.rolling(n).mean()
    sd = c.rolling(n).std()
    return mid - k * sd, mid, mid + k * sd


def keltner(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = ema(df.close, n)
    band = k * atr(df, n)
    return mid - band, mid, mid + band


# ---------------------------------------------------------------- archetypes -> signal arrays
def sig_vwap_reversion(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Session VWAP (reset daily) deviation fade: close stretches >= k x ATR from VWAP, then snaps back."""
    tp = (df.high + df.low + df.close) / 3
    vol = df.get("tick_volume", pd.Series(1.0, index=df.index)).fillna(1.0).clip(lower=1.0)
    day = _day(df)
    cum_pv = (tp * vol).groupby(day).cumsum()
    cum_v = vol.groupby(day).cumsum()
    vwap = cum_pv / cum_v
    dist = (df.close - vwap) / atr(df, 14)
    up = dist.shift() < -p["k"]  # was stretched below VWAP, mean-revert long
    dn = dist.shift() > p["k"]
    # confirmation: this bar already snapping back toward vwap
    up = up & (df.close > df.close.shift())
    dn = dn & (df.close < df.close.shift())
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_bb_breakout(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Bollinger squeeze (bandwidth at an N-bar low) then close breaks outside the band -> breakout."""
    lo, mid, hi = bollinger(df.close, 20, p["k"])
    bw = (hi - lo) / mid
    squeeze = bw == bw.rolling(p["lookback"]).min()
    was_squeezed = squeeze.shift().rolling(3).max() > 0
    up = was_squeezed & (df.close > hi) & (df.close.shift() <= hi.shift())
    dn = was_squeezed & (df.close < lo) & (df.close.shift() >= lo.shift())
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_keltner_breakout(df: pd.DataFrame, p: dict) -> np.ndarray:
    lo, mid, hi = keltner(df, 20, p["k"])
    up = (df.close > hi) & (df.close.shift() <= hi.shift())
    dn = (df.close < lo) & (df.close.shift() >= lo.shift())
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_rsi2_intraday(df: pd.DataFrame, p: dict) -> np.ndarray:
    """RSI(2) extreme scalp WITH the EMA50 trend, intraday M15 (distinct from Dip bot's daily RSI2)."""
    c = df.close
    e50 = ema(c, 50)
    r2 = rsi(c, 2)
    up = (c > e50) & (r2.shift() < p["lvl"]) & (r2 >= p["lvl"])
    dn = (c < e50) & (r2.shift() > 100 - p["lvl"]) & (r2 <= 100 - p["lvl"])
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_ema_ribbon(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Stacked EMA(8/21/55) trend continuation: ribbon aligned + rising, pullback to EMA21 then bounce."""
    c = df.close
    e8, e21, e55 = ema(c, 8), ema(c, 21), ema(c, 55)
    up_trend = (e8 > e21) & (e21 > e55) & (e21 > e21.shift(p["slope_bars"]))
    dn_trend = (e8 < e21) & (e21 < e55) & (e21 < e21.shift(p["slope_bars"]))
    touched_up = (df.low <= e21) & (df.low.shift() > e21.shift())
    touched_dn = (df.high >= e21) & (df.high.shift() < e21.shift())
    up = up_trend & touched_up & (c > e21)
    dn = dn_trend & touched_dn & (c < e21)
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_three_bar_reversal(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Three consecutive closes one way, then a reversal bar closing back past the first bar's open."""
    c, o = df.close, df.open
    down3 = (c.shift(3) > c.shift(2)) & (c.shift(2) > c.shift(1)) & (c.shift(1) > c)
    up3 = (c.shift(3) < c.shift(2)) & (c.shift(2) < c.shift(1)) & (c.shift(1) < c)
    rev_up = down3.shift() & (c > o.shift(3))
    rev_dn = up3.shift() & (c < o.shift(3))
    return np.where(rev_up, 1, np.where(rev_dn, -1, 0))


def sig_volume_breakout(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Range breakout confirmed by a tick_volume spike (proxy for real volume, not exchange-sourced)."""
    vol = df.get("tick_volume")
    if vol is None:
        return np.zeros(len(df))
    vol = vol.fillna(0)
    avg_vol = vol.rolling(20).mean()
    hi_n = df.high.rolling(p["n"]).max().shift()
    lo_n = df.low.rolling(p["n"]).min().shift()
    spike = vol > p["vmult"] * avg_vol
    up = spike & (df.close > hi_n)
    dn = spike & (df.close < lo_n)
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_session_overlap_momentum(df: pd.DataFrame, p: dict) -> np.ndarray:
    """London/NY overlap (server ~15:00-18:00): N consecutive same-direction closes -> continuation."""
    hour = df.time.dt.hour
    in_window = (hour >= 15) & (hour < 18)
    c = df.close
    up_run = (c > c.shift()) & (c.shift() > c.shift(2))
    dn_run = (c < c.shift()) & (c.shift() < c.shift(2))
    up = in_window & up_run.shift().fillna(False) & (c > c.shift())
    dn = in_window & dn_run.shift().fillna(False) & (c < c.shift())
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_donchian_scalp(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Short-period (10-20 bar) Donchian breakout, intraday only -- distinct from Trend bot's 55/20 Daily."""
    hi_n = df.high.rolling(p["n"]).max().shift()
    lo_n = df.low.rolling(p["n"]).min().shift()
    up = df.close > hi_n
    dn = df.close < lo_n
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_macd_cross(df: pd.DataFrame, p: dict) -> np.ndarray:
    line, sig_line = macd(df.close)
    e200 = ema(df.close, 200)
    cross_up = (line.shift() < sig_line.shift()) & (line >= sig_line)
    cross_dn = (line.shift() > sig_line.shift()) & (line <= sig_line)
    up = cross_up & (df.close > e200 if p["trend"] else True)
    dn = cross_dn & (df.close < e200 if p["trend"] else True)
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_stoch_cross(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Plain single-timeframe Stochastic(14,3,3) cross in the extreme zone. Same family as the
    retired legacy LWMA+Stochastic chain strategy -- different mechanics (no LWMA, no M1 chain),
    but flagged as suspect a priori."""
    low14 = df.low.rolling(14).min()
    high14 = df.high.rolling(14).max()
    k = 100 * (df.close - low14) / (high14 - low14).replace(0, np.nan)
    k = k.rolling(3).mean()
    d = k.rolling(3).mean()
    up = (k.shift() < d.shift()) & (k >= d) & (d < p["zone"])
    dn = (k.shift() > d.shift()) & (k <= d) & (d > 100 - p["zone"])
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_inside_bar_breakout(df: pd.DataFrame, p: dict) -> np.ndarray:
    inside = (df.high <= df.high.shift()) & (df.low >= df.low.shift())
    up = inside.shift().fillna(False) & (df.close > df.high.shift())
    dn = inside.shift().fillna(False) & (df.close < df.low.shift())
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_atr_expansion_fade(df: pd.DataFrame, p: dict) -> np.ndarray:
    a = atr(df, 14)
    rng = df.high - df.low
    climax = rng.shift() > p["mult"] * a.shift()
    was_up = df.close.shift() > df.open.shift()
    up = climax & ~was_up  # expansion DOWN bar -> fade long
    dn = climax & was_up  # expansion UP bar -> fade short
    return np.where(up, 1, np.where(dn, -1, 0))


def sig_sr_bounce(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Swing high/low (N-bar) touch + rejection -- generalized version of the already-rejected
    daily prior-day-range fade; expected to fail the same way, kept as a quick confirmation."""
    n = p["n"]
    sw_hi = df.high.rolling(n).max().shift()
    sw_lo = df.low.rolling(n).min().shift()
    near_hi = df.high >= sw_hi * 0.998
    near_lo = df.low <= sw_lo * 1.002
    reject_up = near_hi & (df.close < df.open)
    reject_dn = near_lo & (df.close > df.open)
    return np.where(reject_dn, 1, np.where(reject_up, -1, 0))


# ---------------------------------------------------------------- registry
ARCHETYPES: dict[str, tuple] = {
    "vwap_reversion": (sig_vwap_reversion, {"k": [1.5, 2.0]}, 16),
    "bb_breakout": (sig_bb_breakout, {"k": [2.0, 2.5], "lookback": [50, 100]}, 24),
    "keltner_breakout": (sig_keltner_breakout, {"k": [1.5, 2.0]}, 24),
    "rsi2_intraday": (sig_rsi2_intraday, {"lvl": [5, 10]}, 16),
    "ema_ribbon": (sig_ema_ribbon, {"slope_bars": [10, 20]}, 32),
    "three_bar_reversal": (sig_three_bar_reversal, {}, 16),
    "volume_breakout": (sig_volume_breakout, {"n": [20, 55], "vmult": [1.5, 2.0]}, 24),
    "session_overlap_momentum": (sig_session_overlap_momentum, {}, 12),
    "donchian_scalp": (sig_donchian_scalp, {"n": [10, 20]}, 20),
    "macd_cross": (sig_macd_cross, {"trend": [True, False]}, 24),
    "stoch_cross": (sig_stoch_cross, {"zone": [20, 30]}, 16),
    "inside_bar_breakout": (sig_inside_bar_breakout, {}, 16),
    "atr_expansion_fade": (sig_atr_expansion_fade, {"mult": [1.8, 2.2]}, 8),
    "sr_bounce": (sig_sr_bounce, {"n": [20, 40]}, 16),
}
NO_VOLUME_SKIP = {"GBPJPY", "EURJPY", "CHFJPY"}  # fine, kept for clarity if volume proxy is thin
SL_GRID, RR_GRID = [1.0, 1.5, 2.0], [1.0, 1.5, 2.0]


def run(test_frac: float, only: list[str] | None, markets: list[str] | None, tf: str) -> None:
    rows = []
    univ = markets or list(ALL_MARKETS.keys())
    for name, (fn, pgrid, max_hold) in ARCHETYPES.items():
        if only and name not in only:
            continue
        for key in univ:
            df = load_ext(key, tf)
            if df is None or len(df) < 1500:
                continue
            if name == "volume_breakout" and "tick_volume" not in df.columns:
                continue
            sp_all = cost(key, df) if key in MARKETS else _extcost(key, df)
            split = int(len(df) * (1 - test_frac))
            train = df.iloc[:split].reset_index(drop=True)
            test = df.iloc[split - 250 :].reset_index(drop=True)
            sp_tr, sp_te = sp_all[:split], sp_all[split - 250 :]
            best = None
            keys = list(pgrid.keys())
            grid = list(itertools.product(*pgrid.values())) if keys else [()]
            for pv in grid:
                p = dict(zip(keys, pv, strict=False))
                s_tr = fn(train, p)
                for sl, rr in itertools.product(SL_GRID, RR_GRID):
                    st = simulate(train, s_tr, sl, rr, sp_tr, max_hold).stats()
                    if st["n"] < 40:
                        continue
                    score = st["expR"] * np.sqrt(st["n"])
                    if best is None or score > best[0]:
                        best = (score, p, sl, rr, st)
            if best is None:
                continue
            _, p, sl, rr, st_tr = best
            res_te = simulate(test, fn(test, p), sl, rr, sp_te, max_hold)
            res_te = Result(res_te.r[res_te.t >= 250], res_te.t[res_te.t >= 250])
            st_te = res_te.stats()
            days_te = max((df.time.iloc[-1] - df.time.iloc[split]).days, 1)
            trades_per_day = round(st_te.get("n", 0) / days_te, 2)
            rows.append(
                {
                    "strategy": name,
                    "market": key,
                    "tf": tf,
                    "params": {**p, "sl_atr": sl, "rr": rr},
                    "train": st_tr,
                    "test": st_te,
                    "trades_per_day_test": trades_per_day,
                }
            )
            print(
                f"{name:26s} {key:7s} {tf:4s} train {st_tr} | TEST {st_te} | {trades_per_day}/day",
                flush=True,
            )
    LAB.mkdir(parents=True, exist_ok=True)
    out = LAB / f"power_of_2_results_{tf}.json"
    out.write_text(json.dumps(rows, indent=1, default=str))
    print(f"\nwrote {out}")


def _extcost(key: str, df: pd.DataFrame) -> np.ndarray:
    """Cost model for symbols outside strategy_lab's MARKETS (SOLUSD/BTCUSD/ETHUSD): same
    typical-spread-x1.2 logic, using this file's own point()-like scale."""
    recent = df[df.time >= df.time.iloc[-1] - pd.Timedelta(days=90)]
    h = recent.time.dt.hour
    active = recent[(h >= 0) & (h < 24)]
    spread_pts = active.spread.median() if len(active) else recent.spread.median()
    pt = 0.01 if key in ("BTCUSD", "ETHUSD") else 0.001
    spread_now = 1.2 * spread_pts * pt
    return (spread_now / df.close.iloc[-1]) * df.close.to_numpy()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "run"])
    ap.add_argument("--bars", type=int, default=99_000)
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--markets", nargs="*")
    ap.add_argument("--tf", default="M15")
    a = ap.parse_args()
    if a.cmd == "fetch":
        fetch(a.bars)
    else:
        run(a.test_frac, a.only, a.markets, a.tf)
