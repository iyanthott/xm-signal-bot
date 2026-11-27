"""Daily-timeframe research across the whole XM universe (indices, metals, energy, FX, crypto, ETFs, US stocks).

Fixed rules, identical for every market (no per-market tuning, so a "pass" is not curve-fitting):
  dip_long       : close > SMA200 and RSI(2) < 15 -> buy next open; exit next open after a close > SMA5
                   or after 10 days; stop 2 x ATR14
  rally_short    : mirror image: close < SMA200 and RSI(2) > 85 -> sell; exit after a close < SMA5 / 10 days
  breakout       : close above the 55-day high -> buy (below the 55-day low -> sell); exit next open after
                   a close beyond the opposite 20-day channel; stop 2 x ATR14; max 120 days
  engulf_pullback: close > SMA200 and RSI(14) < 40 and today is a bullish engulfing candle -> buy
                   (mirror: close < SMA200, RSI(14) > 60, bearish engulfing -> sell); same exit as dip_long
  breakout_candle: breakout entry (above) gated on the trigger bar being a strong-bodied candle (body
                   >= 60% of the day's range, closing in the outer quartile) -> tests whether candle
                   shape on the breakout bar improves on plain `breakout`; same exit as breakout
  holy_grail     : Linda Raschke/Laurence Connors setup. ADX(14) > 30 and rising (established, strengthening
                   trend) + price pulls back to touch the 20-day SMA in the trend direction -> enter next
                   open; exit on a close back through the 20-day SMA or after 15 days; stop 2 x ATR14
  vol_breakout   : Larry Williams-style volatility breakout. Buy stop at today's open + 0.5 x yesterday's
                   range, sell stop at open - 0.5 x range; fills at the stop level itself (not the open) if
                   touched intraday, skipped if both levels are touched the same day; stop 2 x ATR14; exit
                   at the close after 5 days if neither stop is hit
  macd_trend     : MACD(12,26) line crosses above its 9-day signal line while close > SMA200 -> buy
                   (mirror: cross below while close < SMA200 -> sell); exit on the opposite MACD/signal
                   cross or after 60 days; stop 2 x ATR14. New indicator vs. the SMA/RSI/ADX family above.
  cci_dip        : CCI(20) (typical-price distance from its mean, scaled by mean absolute deviation) <
                   -100 while close > SMA200 -> buy (mirror: CCI > 100 while close < SMA200 -> sell);
                   exit when CCI crosses back through 0 or after 10 days; stop 2 x ATR14. Same dip-in-an-
                   uptrend shape as dip_long but a different oscillator (typical price, not RSI).
  keltner_breakout: close crosses above EMA20 + 2 x ATR20 -> buy (mirror: below EMA20 - 2 x ATR20 -> sell);
                   a volatility-adaptive channel instead of breakout's fixed 55-day high/low. Exit on a
                   close back through EMA20 or after 120 days; stop 2 x ATR14.
  parabolic_sar  : Wilder's stop-and-reverse. Buy when the SAR flips from down- to up-trend, sell on the
                   mirror flip; exit on the opposite flip (the SAR trail itself, not a fixed-days rule) or
                   after 250 days (safety cap only); stop 2 x ATR14. No SMA200 filter - the indicator is
                   already a complete trend system.
  ichimoku       : Tenkan(9) crosses above Kijun(26) while close is above the cloud (max of Senkou A/B,
                   each lagged 26 days so no look-ahead) -> buy; mirror -> sell. Exit on the opposite
                   Tenkan/Kijun cross or after 60 days; stop 2 x ATR14.
  pivot_bounce   : classic floor-trader pivot from yesterday's H/L/C. Close > SMA200 and today's low
                   touches S1 (support) then closes back above it -> buy (mirror: close < SMA200, high
                   touches R1, closes back below -> sell); exit on a close back through the pivot or
                   after 10 days; stop 2 x ATR14.
Costs: 1.5x spread (max of live and recent bar median) and the broker's actual swap for that direction
(charges only; swap credits are ignored), both scaled to the price at the time.

    poetry run python research/daily_lab.py fetch     # D1 history + spread/swap -> data/lab/d1
    poetry run python research/daily_lab.py run       # -> data/lab/d1_results.csv + printed shortlist
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy_lab import Result, atr, ema, rsi  # noqa: E402

D1 = Path("data/lab/d1")

UNIVERSE: dict[str, list[str]] = {
    "index": [
        "US500Cash", "US100Cash", "US30Cash", "US2000Cash", "US400Cash", "USFANGCash", "GER40Cash",
        "GerMid50Cash", "GerTech30Cash", "UK100Cash", "FRA40Cash", "EU50Cash", "IT40Cash", "SpainCash",
        "NETH25Cash", "SWI20Cash", "JP225Cash", "AUS200Cash", "HK50Cash", "ChinaHCash", "CHN50Cash",
        "TaiwanCash", "Sing30Cash", "SA40Cash", "CA60Cash",
    ],
    "metal": ["GOLD", "SILVER", "XPTUSD", "XPDUSD"],
    "energy": ["OILCash", "BRENTCash", "NGASCash"],
    "fx": [
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURJPY", "GBPJPY", "EURGBP",
        "AUDJPY", "EURAUD", "EURCHF", "GBPCHF", "CADJPY", "CHFJPY", "AUDNZD", "AUDCAD", "EURCAD", "GBPAUD",
        "NZDJPY", "USDMXN", "USDZAR", "USDNOK", "USDSEK", "USDPLN", "USDSGD", "USDCNH",
    ],
    "crypto": ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "LTCUSD", "BCHUSD", "ADAUSD", "DOGEUSD", "LINKUSD", "DOTUSD"],
    "etf": ["SPY", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "SMH", "EEM", "EWJ", "EWZ", "GDX", "TLT", "GLD", "SLV"],
    "stock": [
        "Apple", "Microsoft", "Nvidia", "Amazon", "Google", "Facebook", "Tesla", "Broadcom", "BerkshireHathaway",
        "JPMorgan", "Visa", "Mastercard", "ExxonMobil", "UnitedHealth", "J&J", "WalMart", "Procter&Gam",
        "Costco", "Netflix", "AdvMicroDev", "Oracle", "Salesforce", "Adobe", "Intel", "Cisco", "Pepsico",
        "Coca-Cola", "McDonalds", "Disney", "Nike", "Boeing", "Caterpillar", "GoldmSachs", "HomeDepot",
        "EliLilly", "Merck&Co", "AbbVie", "Chevron", "Qualcomm", "Starbucks",
    ],
}


# ---------------------------------------------------------------- data
def fetch(bars: int) -> None:
    import MetaTrader5 as mt5  # noqa: N813

    if not mt5.initialize():
        sys.exit(f"MT5 initialize failed: {mt5.last_error()}")
    D1.mkdir(parents=True, exist_ok=True)
    meta_path = D1 / "meta.json"
    meta: dict[str, dict] = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    for cls, syms in UNIVERSE.items():
        for sym in syms:
            if sym in meta:  # resumable: already fetched
                continue
            if not mt5.symbol_select(sym, True):
                print(f"{sym}: not selectable")
                continue
            info = mt5.symbol_info(sym)
            r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 0, bars)
            if r is None or len(r) < 300 or info is None:
                print(f"{sym}: too little history ({0 if r is None else len(r)} bars)")
                continue
            df = pd.DataFrame(r)[["time", "open", "high", "low", "close", "spread"]]
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df = df[df.time.dt.dayofweek < 5] if cls != "crypto" else df
            df.to_csv(D1 / f"{sym}.csv", index=False)
            price = float(df.close.iloc[-1])
            spread_pts = max(float(info.spread), float(df.spread.iloc[-60:].median()))
            meta[sym] = {
                "class": cls,
                "spread_frac": 1.5 * spread_pts * info.point / price,
                "swap_long_frac": _swap_frac(info, info.swap_long, price),
                "swap_short_frac": _swap_frac(info, info.swap_short, price),
                "first": str(df.time.iloc[0].date()),
                "bars": len(df),
            }
            print(f"{cls:6s} {sym:16s} {len(df):5d} bars from {df.time.iloc[0].date()}  "
                  f"spread {meta[sym]['spread_frac']*1e4:.1f}bp  swap L/S {meta[sym]['swap_long_frac']*1e4:.2f}/"
                  f"{meta[sym]['swap_short_frac']*1e4:.2f}bp/night", flush=True)
            meta_path.write_text(json.dumps(meta, indent=1))  # save progress after every market
    mt5.shutdown()


def _swap_frac(info: object, swap: float, price: float) -> float:
    """Swap CHARGE per night as a fraction of price (credits -> 0). MT5 swap modes:
    1 points, 2/3/4 money per lot, 5/6 annual interest %. Unknown -> 0.02%/night."""
    mode = int(getattr(info, "swap_mode", 0))
    point = float(getattr(info, "point", 0.0))
    tick_val, tick_size = float(getattr(info, "trade_tick_value", 0)), float(getattr(info, "trade_tick_size", 0))
    if mode == 0:
        return 0.0
    if mode == 1:
        px = swap * point
    elif mode in (2, 3, 4) and tick_val > 0 and tick_size > 0:
        px = swap / (tick_val / tick_size)
    elif mode in (5, 6):
        px = price * swap / 100 / 360
    else:
        return 0.0002
    return max(0.0, -px / price)


# ---------------------------------------------------------------- strategies -> entries and exit rules
def _bullish_engulf(o: pd.Series, c: pd.Series) -> pd.Series:
    po, pc = o.shift(), c.shift()
    return (c > o) & (pc < po) & (o <= pc) & (c >= po) & ((c - o) >= (po - pc))


def _bearish_engulf(o: pd.Series, c: pd.Series) -> pd.Series:
    po, pc = o.shift(), c.shift()
    return (c < o) & (pc > po) & (o >= pc) & (c <= po) & ((o - c) >= (pc - po))


def _strong_body(o: pd.Series, h: pd.Series, lo: pd.Series, c: pd.Series) -> tuple[pd.Series, pd.Series]:
    """body >= 60% of the day's range; close in the outer quartile -> (strong bullish, strong bearish)."""
    rng = (h - lo).replace(0, np.nan)
    body_ok = (c - o).abs() / rng >= 0.6
    bull = body_ok & (c > o) & ((c - lo) / rng >= 0.75)
    bear = body_ok & (c < o) & ((h - c) / rng >= 0.75)
    return bull.fillna(False), bear.fillna(False)


def _adx_di(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder's ADX plus its +DI/-DI components (same smoothing as strategy_lab.adx, direction exposed)."""
    up, dn = df.high.diff(), -df.low.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * pd.Series(mdm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi


def _parabolic_sar(h: np.ndarray, lo: np.ndarray, af_step: float = 0.02, af_max: float = 0.2) -> np.ndarray:
    """Wilder's Parabolic SAR. Returns trend per bar (+1 up, -1 down) - the stop itself is not needed here,
    only its flips (entry trigger and trailing exit in one indicator, unlike every fixed-days exit above)."""
    n = len(h)
    trend = np.ones(n, dtype=int)
    sar = np.full(n, lo[0])
    ep, af = h[0], af_step
    for i in range(1, n):
        new_sar = sar[i - 1] + af * (ep - sar[i - 1])
        if trend[i - 1] == 1:
            new_sar = min(new_sar, lo[i - 1], lo[i - 2] if i >= 2 else lo[i - 1])
            if lo[i] < new_sar:
                trend[i], sar[i], ep, af = -1, ep, lo[i], af_step
            else:
                trend[i], sar[i] = 1, new_sar
                if h[i] > ep:
                    ep, af = h[i], min(af + af_step, af_max)
        else:
            new_sar = max(new_sar, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > new_sar:
                trend[i], sar[i], ep, af = 1, ep, h[i], af_step
            else:
                trend[i], sar[i] = -1, new_sar
                if lo[i] < ep:
                    ep, af = lo[i], min(af + af_step, af_max)
    return trend


def signals(df: pd.DataFrame, strat: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (entry direction per bar, exit-long flag per bar, exit-short flag per bar), evaluated at the close."""
    o, h, lo, c = df.open, df.high, df.low, df.close
    if strat in ("dip_long", "rally_short"):
        sma200, sma5, r2 = c.rolling(200).mean(), c.rolling(5).mean(), rsi(c, 2)
        if strat == "dip_long":
            entry = np.where((c > sma200) & (r2 < 15), 1, 0)
        else:
            entry = np.where((c < sma200) & (r2 > 85), -1, 0)
        return entry, (c > sma5).to_numpy(), (c < sma5).to_numpy()
    if strat == "engulf_pullback":
        sma200, sma5, r14 = c.rolling(200).mean(), c.rolling(5).mean(), rsi(c, 14)
        up = (c > sma200) & (r14 < 40) & _bullish_engulf(o, c)
        dn = (c < sma200) & (r14 > 60) & _bearish_engulf(o, c)
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, (c > sma5).to_numpy(), (c < sma5).to_numpy()
    if strat == "holy_grail":
        adx_, pdi, mdi = _adx_di(df, 14)
        sma20 = c.rolling(20).mean()
        rising = adx_ > adx_.shift(1)
        strong_up = (adx_ > 30) & rising & (pdi > mdi)
        strong_dn = (adx_ > 30) & rising & (mdi > pdi)
        up = strong_up & (lo <= sma20) & (c > sma20)
        dn = strong_dn & (h >= sma20) & (c < sma20)
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, (c < sma20).to_numpy(), (c > sma20).to_numpy()
    if strat == "macd_trend":
        sma200 = c.rolling(200).mean()
        macd_line = ema(c, 12) - ema(c, 26)
        signal_line = ema(macd_line, 9)
        above, below = macd_line > signal_line, macd_line < signal_line
        cross_up = above & ~above.shift(1).fillna(False)
        cross_dn = below & ~below.shift(1).fillna(False)
        up = cross_up & (c > sma200)
        dn = cross_dn & (c < sma200)
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, below.to_numpy(), above.to_numpy()
    if strat == "cci_dip":
        sma200 = c.rolling(200).mean()
        tp = (h + lo + c) / 3
        tp_sma = tp.rolling(20).mean()
        mad = (tp - tp_sma).abs().rolling(20).mean()
        cci = (tp - tp_sma) / (0.015 * mad.replace(0, np.nan))
        up = (c > sma200) & (cci < -100)
        dn = (c < sma200) & (cci > 100)
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, (cci > 0).fillna(False).to_numpy(), (cci < 0).fillna(False).to_numpy()
    if strat == "keltner_breakout":
        mid, a20 = ema(c, 20), atr(df, 20)
        upper, lower = mid + 2 * a20, mid - 2 * a20
        above, below = c > upper, c < lower
        cross_up = above & ~above.shift(1).fillna(False)
        cross_dn = below & ~below.shift(1).fillna(False)
        entry = np.where(cross_up, 1, np.where(cross_dn, -1, 0))
        return entry, (c < mid).to_numpy(), (c > mid).to_numpy()
    if strat == "parabolic_sar":
        trend = _parabolic_sar(h.to_numpy(), lo.to_numpy())
        up_trend = trend == 1
        flip_up = up_trend & ~np.roll(up_trend, 1)
        flip_up[0] = False
        flip_dn = ~up_trend & np.roll(up_trend, 1)
        flip_dn[0] = False
        entry = np.where(flip_up, 1, np.where(flip_dn, -1, 0))
        return entry, (~up_trend), up_trend
    if strat == "ichimoku":
        tenkan = (h.rolling(9).max() + lo.rolling(9).min()) / 2
        kijun = (h.rolling(26).max() + lo.rolling(26).min()) / 2
        span_a = ((tenkan + kijun) / 2).shift(26)
        span_b = ((h.rolling(52).max() + lo.rolling(52).min()) / 2).shift(26)
        cloud_top, cloud_bot = pd.concat([span_a, span_b], axis=1).max(axis=1), pd.concat(
            [span_a, span_b], axis=1
        ).min(axis=1)
        above = tenkan > kijun
        cross_up = above & ~above.shift(1).fillna(False)
        cross_dn = ~above & above.shift(1).fillna(False)
        up = cross_up & (c > cloud_top)
        dn = cross_dn & (c < cloud_bot)
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, (~above).to_numpy(), above.to_numpy()
    if strat == "pivot_bounce":
        ph, pl, pc = h.shift(), lo.shift(), c.shift()
        piv = (ph + pl + pc) / 3
        s1, r1 = 2 * piv - ph, 2 * piv - pl
        sma200 = c.rolling(200).mean()
        up = (c > sma200) & (lo <= s1) & (c > s1)
        dn = (c < sma200) & (h >= r1) & (c < r1)
        entry = np.where(up, 1, np.where(dn, -1, 0))
        return entry, (c < piv).to_numpy(), (c > piv).to_numpy()
    hi55, lo55 = h.rolling(55).max().shift(), lo.rolling(55).min().shift()
    hi20, lo20 = h.rolling(20).max().shift(), lo.rolling(20).min().shift()
    if strat == "breakout_candle":
        bull, bear = _strong_body(o, h, lo, c)
        entry = np.where((c > hi55) & bull, 1, np.where((c < lo55) & bear, -1, 0))
    else:
        entry = np.where(c > hi55, 1, np.where(c < lo55, -1, 0))
    return entry, (c < lo20).to_numpy(), (c > hi20).to_numpy()


MAX_DAYS = {
    "dip_long": 10, "rally_short": 10, "breakout": 120, "engulf_pullback": 10,
    "breakout_candle": 120, "holy_grail": 15, "vol_breakout": 5,
    "macd_trend": 60, "cci_dip": 10, "keltner_breakout": 120,
    "parabolic_sar": 250, "ichimoku": 60, "pivot_bounce": 10,
}


# ---------------------------------------------------------------- Williams-style volatility breakout
def simulate_vbo(df: pd.DataFrame, meta: dict, k: float = 0.5, sl_atr: float = 2.0, max_days: int = 5) -> Result:
    """Buy stop at today's open + k x yesterday's range, sell stop at open - k x range (Larry Williams'
    volatility breakout, no ATR in the trigger - only in the stop). Both levels known at today's open,
    so this is not look-ahead; fill is at the breakout level itself (stop-order fill), not at the open.
    If both levels are touched the same day the bar is ambiguous (no intrabar order) and skipped."""
    o, h, lo, c = (df[k_].to_numpy() for k_ in ("open", "high", "low", "close"))
    t = df.time.to_numpy()
    a = atr(df, 14).to_numpy()
    rng_prev = (df.high - df.low).shift().to_numpy()
    spread_now = meta["spread_frac"]
    out_r, out_t = [], []
    i, n = 200, len(df) - 1
    while i < n:
        if not np.isfinite(a[i]) or a[i] <= 0 or not np.isfinite(rng_prev[i]) or rng_prev[i] <= 0:
            i += 1
            continue
        level_up, level_dn = o[i] + k * rng_prev[i], o[i] - k * rng_prev[i]
        hit_up, hit_dn = h[i] >= level_up, lo[i] <= level_dn
        if hit_up == hit_dn:  # neither or both (ambiguous) -> skip
            i += 1
            continue
        d = 1 if hit_up else -1
        raw_entry = level_up if d == 1 else level_dn
        entry = raw_entry + spread_now * raw_entry * d
        dist = sl_atr * a[i]
        stop = entry - dist if d == 1 else entry + dist
        j, exit_px = i, None
        while j <= min(n, i + max_days):
            if d == 1 and lo[j] <= stop:
                exit_px = stop if j == i else min(stop, o[j])
                break
            if d == -1 and h[j] >= stop:
                exit_px = stop if j == i else max(stop, o[j])
                break
            if j == min(n, i + max_days):
                exit_px = c[j]
                break
            j += 1
        nights = int((t[j] - t[i]) / np.timedelta64(1, "D"))
        swap = (meta["swap_long_frac"] if d == 1 else meta["swap_short_frac"]) * entry * max(nights, 0)
        pnl = (exit_px - entry) * d - swap
        out_r.append(pnl / dist)
        out_t.append(i)
        i = j + 1
    return Result(np.array(out_r), np.array(out_t))


def simulate(df: pd.DataFrame, strat: str, meta: dict) -> Result:
    if strat == "vol_breakout":
        return simulate_vbo(df, meta)
    entry, exit_l, exit_s = signals(df, strat)
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df.time.to_numpy()
    a = atr(df, 14).to_numpy()
    spread_now, last = meta["spread_frac"], c[-1]
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
            if (exit_l[k] if d == 1 else exit_s[k]) or k - j + 1 >= MAX_DAYS[strat]:
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
    _ = last
    return Result(np.array(out_r), np.array(out_t))


# ---------------------------------------------------------------- run
def _conservative(m: dict) -> dict:
    """Guard against costs MT5 under-reports: zero swaps on energy/metals (unknown -> 0.02%/night) and
    zero/tiny spreads read while a market is closed (floor 2bp; 5bp for single stocks/ETFs)."""
    m = dict(m)
    if m["class"] in ("energy", "metal") and m["swap_long_frac"] == 0 and m["swap_short_frac"] == 0:
        m["swap_long_frac"] = m["swap_short_frac"] = 0.0002
    floor = 0.0005 if m["class"] in ("stock", "etf") else 0.0002
    m["spread_frac"] = max(m["spread_frac"], floor)
    return m


def run(test_frac: float) -> None:
    meta = {k: _conservative(v) for k, v in json.loads((D1 / "meta.json").read_text()).items()}
    rows = []
    for sym, m in meta.items():
        df = pd.read_csv(D1 / f"{sym}.csv", parse_dates=["time"])
        split_time = df.time.iloc[int(len(df) * (1 - test_frac))]
        years = (df.time.iloc[-1] - df.time.iloc[0]).days / 365.25
        for strat in MAX_DAYS:
            res = simulate(df, strat, m)
            is_test = df.time.to_numpy()[res.t] >= split_time.to_datetime64() if res.n else np.array([], bool)
            tr = Result(res.r[~is_test], res.t[~is_test]).stats()
            te = Result(res.r[is_test], res.t[is_test]).stats()
            al = res.stats()
            rows.append({
                "class": m["class"], "symbol": sym, "strategy": strat, "from": m["first"],
                "years": round(years, 1), "n": al.get("n", 0), "trades_yr": round(al.get("n", 0) / max(years, 0.1), 1),
                "win%": al.get("win%"), "PF": al.get("PF"), "expR": al.get("expR"), "maxDD_R": al.get("maxDD_R"),
                "train_n": tr.get("n", 0), "train_PF": tr.get("PF"), "train_expR": tr.get("expR"),
                "test_n": te.get("n", 0), "test_PF": te.get("PF"), "test_win%": te.get("win%"), "test_expR": te.get("expR"),
            })
    out = pd.DataFrame(rows)
    out.to_csv(D1.parent / "d1_results.csv", index=False)

    ok = out[(out.n >= 40) & (out.test_n >= 15) & (out.train_expR > 0) & (out.test_expR > 0)
             & (out.train_PF >= 1.1) & (out.test_PF >= 1.2)]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 300)
    print("\n=== PASSED (>=40 trades, positive in BOTH periods, train PF>=1.1, test PF>=1.2) ===")
    print(ok.sort_values(["strategy", "test_PF"], ascending=[True, False]).to_string(index=False))
    print("\n=== POOLED by class x strategy (all markets, no selection) ===")
    pooled = []
    for (cls, strat), g in out.groupby(["class", "strategy"]):
        pooled.append({"class": cls, "strategy": strat, "markets": len(g), "trades": int(g.n.sum()),
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
