#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy 21 — Multi-Timeframe Strategy (15min -> 3min -> 1min)
--------------------------------------------------------------
Implements Strategy 21 for SELL and BUY (vice-versa) on BTCUSDT.

15min Setup : 3 consecutive candles with upper wicks piercing above horizontal line (SELL)
              or lower wicks piercing below horizontal line (BUY).
3min Signal : Mapped C candle to 3min -> check previous 3 candles open+close above/below EMA 50.
              Entry on close below EMA 50 & below setup highest close (SELL) or vice-versa (BUY).
Hard SL     : RSI(14) cycle -> MACD cycle -> Fibonacci retracement & extension levels
              on both 3min and 15min -> 1 level upper/lower of backward cut candle -> take conservative SL.
Soft SL     : Smoothed MACD cycles -> backward 2 green/red cycles (>=15 bars) -> zone highest/lowest
              among KAMAEMA, KamaLine, SMA, Middle BB, EMA 50 -> Stage 2 exit tracking on 1min.
Backtest    : Ratios 1:0.5 to 1:5 with trailing SL starting from 1:2.
"""

import os
import sys
import time
from collections import OrderedDict, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pytz
import talib

COIN_NAME = "BTCUSDT"
STARTUTC = pd.Timestamp("2017-10-01 00:00:00", tz="UTC")

IN_15M = Path("/Users/Vraj/Desktop/Data - BTCUSDT/ Data/BTCUSDT_15m_2017_to_Today.csv")
IN_3M  = Path("/Users/Vraj/Desktop/Data - BTCUSDT/ Data/BTCUSDT_3m_2017_to_Today.csv")
IN_1M  = Path("/Users/Vraj/Desktop/Data - BTCUSDT/ Data/BTCUSDT_1m_2017_to_Today.csv")

OUT_DIR = Path("/Users/Vraj/Downloads/Strategy 21_1:10 Ratios")
OUT_SELL_XLSX = OUT_DIR / "Strategy 21_1:10 Ratios-BTCUSDT-SELL.xlsx"
OUT_BUY_XLSX  = OUT_DIR / "Strategy 21_1:10 Ratios-BTCUSDT-BUY.xlsx"

SMOOTH_MIN_RUN = 4
RATIOS = [0.5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
RISK_PER_TRADE = 100.0


# =============================================================================
# LOGGING & FORMATTING HELPERS
# =============================================================================

def log(msg: str):
    print(msg, flush=True)

def _s(ts) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, pd.Timestamp) and pd.isna(ts):
        return None
    return str(ts)

def _f(x, d: int = 6):
    if x is None:
        return None
    if isinstance(x, (float, np.floating)):
        if np.isnan(x):
            return None
        return round(float(x), d)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, pd.Timestamp):
        return str(x)
    return x

def _kv_lines(obj: Any, indent: int = 0) -> List[str]:
    sp = " " * indent
    if obj is None:
        return [f"{sp}None"]
    if isinstance(obj, (str, int, float, bool, np.floating, np.integer)):
        return [f"{sp}{obj}"]
    if isinstance(obj, list):
        out = []
        for v in obj:
            if isinstance(v, (dict, OrderedDict, list)):
                out.append(f"{sp}-")
                out.extend(_kv_lines(v, indent + 2))
            else:
                out.append(f"{sp}- {v}")
        return out
    if isinstance(obj, (dict, OrderedDict)):
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, OrderedDict, list)):
                out.append(f"{sp}{k}:")
                out.extend(_kv_lines(v, indent + 2))
            else:
                out.append(f"{sp}{k}: {_f(v)}")
        return out
    return [f"{sp}{obj}"]

def vkv(obj: Any) -> str:
    return "\n".join(_kv_lines(obj, 0))


# =============================================================================
# LOAD
# =============================================================================

def _detect_fmt(s: str) -> Optional[str]:
    s = s.strip().replace("T", " ")
    if len(s) >= 19 and s[4] == "-":
        return "%Y-%m-%d %H:%M:%S"
    if len(s) == 16 and s[4] == "-":
        return "%Y-%m-%d %H:%M"
    if len(s) >= 19 and s[2] == "/":
        return "%d/%m/%Y %H:%M:%S"
    if len(s) == 16 and s[2] == "/":
        return "%d/%m/%Y %H:%M"
    return None

def load_ohlcv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(str(path))
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [str(c).strip().lower() for c in df.columns]

    for alias in ("date", "time", "timestamp", "open_time"):
        if alias in df.columns and "datetime" not in df.columns:
            df = df.rename(columns={alias: "datetime"})

    fmt = _detect_fmt(str(df["datetime"].iloc[0]))
    if fmt:
        df["datetime"] = pd.to_datetime(df["datetime"], format=fmt, utc=True, errors="coerce")
    else:
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")

    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df = (
        df.dropna(subset=["datetime", "open", "high", "low", "close"])
          .sort_values("datetime")
          .set_index("datetime")
    )
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume"]].copy()


# =============================================================================
# INDICATORS
# =============================================================================

def add_smooth_macd_cycles(df: pd.DataFrame) -> pd.DataFrame:
    _, _, hist_raw = talib.MACD(
        df["close"].values.astype(float),
        fastperiod=12, slowperiod=26, signalperiod=9
    )
    hist = hist_raw.copy()
    plus_run = 0
    minus_run = 0
    for i in range(len(hist)):
        if np.isnan(hist[i]):
            continue
        if hist[i] < 0:
            minus_run += 1
            if 0 < plus_run < SMOOTH_MIN_RUN and i > plus_run and hist[i - plus_run - 1] < 0:
                hist[i - plus_run:i] *= -1.0
            plus_run = 0
        else:
            plus_run += 1
            if 0 < minus_run < SMOOTH_MIN_RUN and i > minus_run and hist[i - minus_run - 1] > 0:
                hist[i - minus_run:i] *= -1.0
            minus_run = 0
    sign = np.where(np.isnan(hist), 0.0, np.sign(hist))
    cycle = pd.Series(sign, index=df.index).diff().ne(0).cumsum().values.astype(int)
    color = np.where(sign > 0, "Green", np.where(sign < 0, "Red", "Flat"))
    df["sm_hist"] = hist
    df["sm_sign"] = sign
    df["sm_cycle"] = cycle
    df["sm_color"] = color
    return df

def calc_emakama(close: np.ndarray, er_len=10, fast_len=2, slow_len=30) -> np.ndarray:
    n = len(close)
    kama = np.full(n, np.nan, dtype=float)
    fast_sc = 2.0 / (fast_len + 1)
    slow_sc = 2.0 / (slow_len + 1)
    if n == 0:
        return kama
    kama[0] = close[0]
    for i in range(1, n):
        if i < er_len:
            kama[i] = close[i]
            continue
        change = abs(close[i] - close[i - er_len])
        vol = sum(abs(close[j] - close[j - 1]) for j in range(i - er_len + 1, i + 1))
        er = change / vol if vol != 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (close[i] - kama[i - 1])
    return kama

def calc_kama_line(src: np.ndarray, length=14, fast_length=2, slow_length=30, hp_period=48) -> np.ndarray:
    n = len(src)
    pi = 2.0 * np.arcsin(1.0)
    alpha1 = (np.cos(0.707 * 2 * pi / hp_period) + np.sin(0.707 * 2 * pi / hp_period) - 1.0) / np.cos(0.707 * 2 * pi / hp_period)
    a1 = np.exp(-1.414 * pi / 10.0)
    b1 = 2.0 * a1 * np.cos(1.414 * 180.0 / 10.0)
    c2 = b1
    c3 = -a1 * a1
    c1 = 1.0 - c2 - c3
    fastest = 2.0 / (fast_length + 1)
    slowest = 2.0 / (slow_length + 1)
    hp = np.zeros(n)
    filt = np.zeros(n)
    kama = np.zeros(n)
    corr_arr = np.zeros(length)
    r1_arr = np.zeros(length)
    r2_arr = np.zeros(length)

    for i in range(n):
        s0 = src[i]
        s1 = src[i - 1] if i >= 1 else 0.0
        s2 = src[i - 2] if i >= 2 else 0.0
        hp1 = hp[i - 1] if i >= 1 else 0.0
        hp2 = hp[i - 2] if i >= 2 else 0.0
        hp[i] = ((1 - alpha1 / 2) ** 2) * (s0 - 2 * s1 + s2) + 2 * (1 - alpha1) * hp1 - ((1 - alpha1) ** 2) * hp2
        f1 = filt[i - 1] if i >= 1 else 0.0
        f2 = filt[i - 2] if i >= 2 else 0.0
        hp_p = hp[i - 1] if i >= 1 else 0.0
        filt[i] = c1 * (hp[i] + hp_p) / 2.0 + c2 * f1 + c3 * f2

        for lag in range(length):
            m = lag
            sx = sy = sxx = syy = sxy = 0.0
            for count in range(m + 1):
                ix = i - count
                iy = i - lag - count
                x = filt[ix] if ix >= 0 else 0.0
                y = filt[iy] if iy >= 0 else 0.0
                sx += x
                sy += y
                sxx += x * x
                sxy += x * y
                syy += y * y
            d = (m * sxx - sx * sx) * (m * syy - sy * sy)
            if d > 0:
                corr_arr[lag] = (m * sxy - sx * sy) / np.sqrt(d)

        sq_sum = np.zeros(length)
        for period in range(8, length):
            cp_ = sp_ = 0.0
            for n2 in range(8, length):
                cp_ += corr_arr[n2] * np.cos(360.0 * n2 / period)
                sp_ += corr_arr[n2] * np.sin(360.0 * n2 / period)
            sq_sum[period] = cp_ * cp_ + sp_ * sp_

        for period2 in range(8, length):
            r2_arr[period2] = r1_arr[period2]
            r1_arr[period2] = 0.2 * sq_sum[period2] ** 2 + 0.8 * r2_arr[period2]

        max_pwr = max((r1_arr[p] for p in range(8, length)), default=0.0)
        pwr = np.zeros(length)
        for period4 in range(8, length):
            pwr[period4] = (r1_arr[period4] / max_pwr) if max_pwr != 0 else 0.0

        spx_ = sp__ = 0.0
        for period5 in range(8, length):
            if pwr[period5] >= 0.5:
                spx_ += period5 * pwr[period5]
                sp__ += pwr[period5]
        dc = spx_ / sp__ if sp__ != 0 else 0.0
        dc = max(8.0, min(14.0, dc))
        dc_int = int(dc)

        idx_dc = i - dc_int
        src_dc = src[idx_dc] if idx_dc >= 0 else 0.0
        num = abs(s0 - src_dc)
        denom = 0.0
        for j in range(dc_int):
            ij = i - j
            ij1 = i - j - 1
            if ij >= 0 and ij1 >= 0:
                denom += abs(src[ij] - src[ij1])
        er = num / denom if denom != 0 else 0.0
        sc = (er * (fastest - slowest) + slowest) ** 2
        kprev = kama[i - 1] if i > 0 else 0.0
        kama[i] = kprev + sc * (s0 - kprev)
    return kama

def _pine_ema_series(series: pd.Series, length: int) -> pd.Series:
    alpha = 2.0 / (length + 1)
    out = np.zeros(len(series))
    out[0] = series.iloc[0]
    for i in range(1, len(series)):
        out[i] = alpha * series.iloc[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)

def _heikin_ashi_df(df: pd.DataFrame) -> pd.DataFrame:
    ha = pd.DataFrame(index=df.index)
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = np.zeros(len(df))
    ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + ha_close.iloc[i - 1]) / 2.0
    ha["open"] = ha_open
    ha["close"] = ha_close.values
    ha["high"] = np.maximum.reduce([df["high"].values, ha_open, ha_close.values])
    ha["low"] = np.minimum.reduce([df["low"].values, ha_open, ha_close.values])
    return ha

def _pine_kama_series(series: pd.Series, length=5, fast=2.5, slow=20) -> pd.Series:
    xvnoise = abs(series - series.shift(1))
    nsignal = abs(series - series.shift(length))
    nnoise = xvnoise.rolling(length).sum()
    nfast = 2.0 / (fast + 1)
    nslow = 2.0 / (slow + 1)
    kama = np.zeros(len(series))
    for i in range(len(series)):
        if i == 0:
            kama[0] = 0.0
            continue
        sig = nsignal.iloc[i] if not np.isnan(nsignal.iloc[i]) else 0.0
        noi = nnoise.iloc[i] if not np.isnan(nnoise.iloc[i]) else 0.0
        er = sig / noi if noi != 0 else 0.0
        sc = (er * (nfast - nslow) + nslow) ** 2
        kama[i] = kama[i - 1] + sc * (series.iloc[i] - kama[i - 1])
    return pd.Series(kama, index=series.index)

def calc_sma_kama(df: pd.DataFrame, length: int = 20) -> pd.Series:
    ha = _heikin_ashi_df(df)
    hlc3 = (ha["high"] + ha["low"] + ha["close"]) / 3.0
    return _pine_ema_series(_pine_kama_series(hlc3), length)

def prepare_15m_data(df_raw: pd.DataFrame) -> Dict[str, Any]:
    df = df_raw.copy()
    close = df["close"].values.astype(float)
    df["rsi14"] = talib.RSI(close, timeperiod=14)
    df = add_smooth_macd_cycles(df)
    return {
        "idx": df.index,
        "open": df["open"].values.astype(float),
        "high": df["high"].values.astype(float),
        "low": df["low"].values.astype(float),
        "close": close,
        "rsi14": df["rsi14"].values.astype(float),
        "sm_cycle": df["sm_cycle"].values.astype(int),
        "sm_color": df["sm_color"].values.astype(object),
        "volume": df["volume"].values.astype(float),
    }

def prepare_3m_data(df_raw: pd.DataFrame) -> Dict[str, Any]:
    df = df_raw.copy()
    close = df["close"].values.astype(float)
    op = df["open"].values.astype(float)
    hi = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    ohlc4 = (op + hi + lo + close) / 4.0

    df["ema50"] = talib.EMA(close, timeperiod=50)
    df["rsi14"] = talib.RSI(close, timeperiod=14)
    df = add_smooth_macd_cycles(df)
    df["emakama"] = pd.Series(calc_emakama(close), index=df.index)
    df["kama_line"] = pd.Series(calc_kama_line(ohlc4), index=df.index)
    df["sma_kama"] = calc_sma_kama(df, length=20)
    df["bb_mid"] = talib.SMA(close, timeperiod=20)

    return {
        "idx": df.index,
        "open": op,
        "high": hi,
        "low": lo,
        "close": close,
        "ema50": df["ema50"].values.astype(float),
        "rsi14": df["rsi14"].values.astype(float),
        "sm_cycle": df["sm_cycle"].values.astype(int),
        "sm_color": df["sm_color"].values.astype(object),
        "emakama": df["emakama"].values.astype(float),
        "kama_line": df["kama_line"].values.astype(float),
        "sma_kama": df["sma_kama"].values.astype(float),
        "bb_mid": df["bb_mid"].values.astype(float),
    }

def prepare_1m_data(df_raw: pd.DataFrame) -> Dict[str, Any]:
    df = df_raw.copy()
    close = df["close"].values.astype(float)
    return {
        "idx": df.index,
        "open": df["open"].values.astype(float),
        "high": df["high"].values.astype(float),
        "low": df["low"].values.astype(float),
        "close": close,
    }


# =============================================================================
# FIBONACCI RETRACEMENT & EXTENSIONS
# =============================================================================

FIB_LEVELS_RAW = [
    (0.0, "0"),
    (0.236, "0.236"),
    (0.382, "0.382"),
    (0.5, "0.5"),
    (0.618, "0.618"),
    (0.786, "0.786"),
    (1.0, "1"),
    (1.272, "1.272"),
    (1.414, "1.414"),
    (1.618, "1.618"),
    (2.0, "2"),
    (2.618, "2.618"),
]

def get_fib_levels(hh: float, ll: float, side: str) -> List[Tuple[float, str]]:
    rng = hh - ll
    levels = []
    for k, name in FIB_LEVELS_RAW:
        if side == "sell":
            price = ll + k * rng
        else:
            price = hh - k * rng
        levels.append((price, name))

    if side == "sell":
        levels.sort(key=lambda x: x[0])  # Ascending price (0 -> 2.618 upwards)
    else:
        levels.sort(key=lambda x: x[0], reverse=True)  # Descending price (0 -> 2.618 downwards)
    return levels


# =============================================================================
# 15MIN SETUP SCANNER
# =============================================================================

def scan_15m_setups(arr15: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    setups = []
    idx = arr15["idx"]
    op = arr15["open"]
    hi = arr15["high"]
    lo = arr15["low"]
    cl = arr15["close"]

    for i in range(2, len(idx)):
        if idx[i] < STARTUTC:
            continue

        if side == "sell":
            wicks_ok = (
                hi[i - 2] > max(op[i - 2], cl[i - 2]) and
                hi[i - 1] > max(op[i - 1], cl[i - 1]) and
                hi[i] > max(op[i], cl[i])
            )
            if not wicks_ok:
                continue
            h_line = min(hi[i - 2], hi[i - 1], hi[i])
            body_ok = (
                max(op[i - 2], cl[i - 2]) <= h_line and
                max(op[i - 1], cl[i - 1]) <= h_line and
                max(op[i], cl[i]) <= h_line
            )
            if not body_ok:
                continue
            setup_extreme_close = max(cl[i - 2], cl[i - 1], cl[i])
        else:
            wicks_ok = (
                lo[i - 2] < min(op[i - 2], cl[i - 2]) and
                lo[i - 1] < min(op[i - 1], cl[i - 1]) and
                lo[i] < min(op[i], cl[i])
            )
            if not wicks_ok:
                continue
            h_line = max(lo[i - 2], lo[i - 1], lo[i])
            body_ok = (
                min(op[i - 2], cl[i - 2]) >= h_line and
                min(op[i - 1], cl[i - 1]) >= h_line and
                min(op[i], cl[i]) >= h_line
            )
            if not body_ok:
                continue
            setup_extreme_close = min(cl[i - 2], cl[i - 1], cl[i])

        setups.append({
            "start_dt": idx[i - 2],
            "end_dt": idx[i],
            "dt_1": idx[i - 2], "extreme_1": hi[i - 2] if side == "sell" else lo[i - 2],
            "dt_2": idx[i - 1], "extreme_2": hi[i - 1] if side == "sell" else lo[i - 1],
            "dt_3": idx[i],     "extreme_3": hi[i] if side == "sell" else lo[i],
            "extreme_close": setup_extreme_close,
            "pos_3": i,
        })
    return setups


# =============================================================================
# HARD SL CALCULATOR ON TIMEFRAME
# =============================================================================

def compute_hard_sl_on_tf(arr: Dict[str, Any], entry_ts: pd.Timestamp, side: str) -> Dict[str, Any]:
    idx = arr["idx"]
    pos = idx.searchsorted(entry_ts, side="right") - 1
    if pos < 1:
        return {"ok": False}

    rsi = arr["rsi14"]
    cycles = arr["sm_cycle"]
    hi = arr["high"]
    lo = arr["low"]
    op = arr["open"]
    cl = arr["close"]

    event1_pos = None
    for p in range(pos, -1, -1):
        if not np.isnan(rsi[p]):
            if side == "sell" and rsi[p] < 30:
                event1_pos = p
                break
            elif side == "buy" and rsi[p] > 70:
                event1_pos = p
                break

    if event1_pos is None:
        return {"ok": False}

    event2_pos = None
    for p in range(event1_pos, -1, -1):
        if not np.isnan(rsi[p]):
            if side == "sell" and rsi[p] > 70:
                event2_pos = p
                break
            elif side == "buy" and rsi[p] < 30:
                event2_pos = p
                break

    if event2_pos is None:
        return {"ok": False}

    cid = cycles[event2_pos]
    cyc_mask = np.where(cycles == cid)[0]
    cyc_start_pos, cyc_end_pos = int(cyc_mask[0]), int(cyc_mask[-1])

    if side == "sell":
        rel = int(np.argmax(hi[cyc_start_pos:cyc_end_pos + 1]))
        ext1_pos = cyc_start_pos + rel
        hh_val = float(hi[ext1_pos])
        hh_dt = idx[ext1_pos]

        sub_lo = lo[ext1_pos:pos + 1]
        rel_sub = int(np.argmin(sub_lo))
        ext2_pos = ext1_pos + rel_sub
        ll_val = float(lo[ext2_pos])
        ll_dt = idx[ext2_pos]
    else:
        rel = int(np.argmin(lo[cyc_start_pos:cyc_end_pos + 1]))
        ext1_pos = cyc_start_pos + rel
        ll_val = float(lo[ext1_pos])
        ll_dt = idx[ext1_pos]

        sub_hi = hi[ext1_pos:pos + 1]
        rel_sub = int(np.argmax(sub_hi))
        ext2_pos = ext1_pos + rel_sub
        hh_val = float(hi[ext2_pos])
        hh_dt = idx[ext2_pos]

    fib_levels = get_fib_levels(hh_val, ll_val, side)

    cut_dt = None
    cut_level_name = None
    cut_level_val = None
    cut_idx_in_levels = None

    for p in range(pos, ext1_pos - 1, -1):
        cand_min = min(op[p], cl[p])
        cand_max = max(op[p], cl[p])
        if op[p] == cl[p]:
            continue

        for i_lvl, (lvl_price, lvl_name) in enumerate(fib_levels):
            if cand_min <= lvl_price <= cand_max:
                cut_dt = idx[p]
                cut_level_name = lvl_name
                cut_level_val = lvl_price
                cut_idx_in_levels = i_lvl
                break
        if cut_dt is not None:
            break

    if cut_dt is None:
        return {"ok": False}

    is_topmost = "yes" if cut_idx_in_levels == (len(fib_levels) - 1) else "no"

    if is_topmost == "yes":
        upper_name = "no 1 level up found" if side == "sell" else "no 1 level down found"
        upper_val = None
        sl_val = cut_level_val
    else:
        next_price, next_name = fib_levels[cut_idx_in_levels + 1]
        upper_name = next_name
        upper_val = next_price
        sl_val = next_price

    return {
        "ok": True,
        "event1_dt": idx[event1_pos], "event1_val": rsi[event1_pos],
        "event2_dt": idx[event2_pos], "event2_val": rsi[event2_pos],
        "cyc_start_dt": idx[cyc_start_pos], "cyc_end_dt": idx[cyc_end_pos],
        "extreme1_val": hh_val if side == "sell" else ll_val,
        "extreme1_dt": hh_dt if side == "sell" else ll_dt,
        "extreme2_val": ll_val if side == "sell" else hh_val,
        "extreme2_dt": ll_dt if side == "sell" else hh_dt,
        "cut_dt": cut_dt,
        "cut_level": cut_level_name,
        "cut_level_val": cut_level_val,
        "is_topmost": is_topmost,
        "upper_name": upper_name,
        "upper_val": upper_val,
        "sl_val": sl_val,
    }


# =============================================================================
# STAGE 2 MULTI-STAGE EXIT TRACKING ON 1MIN
# =============================================================================

def track_stage2_exit(
    arr1: Dict[str, Any],
    start_pos: int,
    initial_sl_price: float,
    side: str,
    max_pos: Optional[int] = None
) -> Dict[str, Any]:
    idx = arr1["idx"]
    op = arr1["open"]
    cl = arr1["close"]
    hi = arr1["high"]
    lo = arr1["low"]

    end_p = len(idx) if max_pos is None else min(len(idx), max_pos + 1)

    stage1_close_pos = None
    most_updated_sl = initial_sl_price
    most_updated_dt = None

    for p in range(start_pos, end_p):
        if side == "sell":
            if hi[p] > most_updated_sl and cl[p] <= most_updated_sl:
                most_updated_sl = hi[p]
                most_updated_dt = idx[p]
            elif cl[p] > most_updated_sl:
                stage1_close_pos = p
                break
        else:
            if lo[p] < most_updated_sl and cl[p] >= most_updated_sl:
                most_updated_sl = lo[p]
                most_updated_dt = idx[p]
            elif cl[p] < most_updated_sl:
                stage1_close_pos = p
                break

    if stage1_close_pos is None:
        return {"exit_found": False}

    s1_dt = idx[stage1_close_pos]
    s1_close = cl[stage1_close_pos]

    red1_pos = None
    for p in range(stage1_close_pos + 1, end_p):
        if side == "sell" and cl[p] < op[p]:
            red1_pos = p
            break
        elif side == "buy" and cl[p] > op[p]:
            red1_pos = p
            break

    if red1_pos is None:
        return {"exit_found": False, "s1_dt": s1_dt, "s1_close": s1_close}

    if side == "sell":
        h1_val = float(np.max(hi[stage1_close_pos:red1_pos + 1]))
        rel = int(np.argmax(hi[stage1_close_pos:red1_pos + 1]))
        h1_dt = idx[stage1_close_pos + rel]
    else:
        h1_val = float(np.min(lo[stage1_close_pos:red1_pos + 1]))
        rel = int(np.argmin(lo[stage1_close_pos:red1_pos + 1]))
        h1_dt = idx[stage1_close_pos + rel]

    step1_final_pos = None
    upd_h1 = h1_val
    upd_h1_dt = None

    for p in range(red1_pos + 1, end_p):
        if side == "sell":
            if hi[p] > upd_h1 and cl[p] <= upd_h1:
                upd_h1 = hi[p]
                upd_h1_dt = idx[p]
            elif cl[p] > upd_h1:
                step1_final_pos = p
                break
        else:
            if lo[p] < upd_h1 and cl[p] >= upd_h1:
                upd_h1 = lo[p]
                upd_h1_dt = idx[p]
            elif cl[p] < upd_h1:
                step1_final_pos = p
                break

    if step1_final_pos is None:
        return {
            "exit_found": False, "s1_dt": s1_dt, "s1_close": s1_close,
            "red1_dt": idx[red1_pos], "h1_val": h1_val, "h1_dt": h1_dt,
            "upd_h1_val": upd_h1, "upd_h1_dt": upd_h1_dt
        }

    s2_s1_dt = idx[step1_final_pos]
    s2_s1_close = cl[step1_final_pos]

    red2_pos = None
    for p in range(step1_final_pos + 1, end_p):
        if side == "sell" and cl[p] < op[p]:
            red2_pos = p
            break
        elif side == "buy" and cl[p] > op[p]:
            red2_pos = p
            break

    if red2_pos is None:
        return {
            "exit_found": False, "s1_dt": s1_dt, "s1_close": s1_close,
            "red1_dt": idx[red1_pos], "h1_val": h1_val, "h1_dt": h1_dt,
            "upd_h1_val": upd_h1, "upd_h1_dt": upd_h1_dt,
            "s2_s1_dt": s2_s1_dt, "s2_s1_close": s2_s1_close
        }

    if side == "sell":
        h2_val = float(np.max(hi[step1_final_pos:red2_pos + 1]))
        rel = int(np.argmax(hi[step1_final_pos:red2_pos + 1]))
        h2_dt = idx[step1_final_pos + rel]
    else:
        h2_val = float(np.min(lo[step1_final_pos:red2_pos + 1]))
        rel = int(np.argmin(lo[step1_final_pos:red2_pos + 1]))
        h2_dt = idx[step1_final_pos + rel]

    step2_final_pos = None
    upd_h2 = h2_val
    upd_h2_dt = None

    for p in range(red2_pos + 1, end_p):
        if side == "sell":
            if hi[p] > upd_h2 and cl[p] <= upd_h2:
                upd_h2 = hi[p]
                upd_h2_dt = idx[p]
            elif cl[p] > upd_h2:
                step2_final_pos = p
                break
        else:
            if lo[p] < upd_h2 and cl[p] >= upd_h2:
                upd_h2 = lo[p]
                upd_h2_dt = idx[p]
            elif cl[p] < upd_h2:
                step2_final_pos = p
                break

    if step2_final_pos is None:
        return {
            "exit_found": False, "s1_dt": s1_dt, "s1_close": s1_close,
            "red1_dt": idx[red1_pos], "h1_val": h1_val, "h1_dt": h1_dt,
            "upd_h1_val": upd_h1, "upd_h1_dt": upd_h1_dt,
            "s2_s1_dt": s2_s1_dt, "s2_s1_close": s2_s1_close,
            "red2_dt": idx[red2_pos], "h2_val": h2_val, "h2_dt": h2_dt,
            "upd_h2_val": upd_h2, "upd_h2_dt": upd_h2_dt
        }

    return {
        "exit_found": True, "exit_dt": idx[step2_final_pos], "exit_price": cl[step2_final_pos],
        "s1_dt": s1_dt, "s1_close": s1_close,
        "red1_dt": idx[red1_pos], "h1_val": h1_val, "h1_dt": h1_dt,
        "upd_h1_val": upd_h1, "upd_h1_dt": upd_h1_dt,
        "s2_s1_dt": s2_s1_dt, "s2_s1_close": s2_s1_close,
        "red2_dt": idx[red2_pos], "h2_val": h2_val, "h2_dt": h2_dt,
        "upd_h2_val": upd_h2, "upd_h2_dt": upd_h2_dt
    }


# =============================================================================
# SOFT SL CALCULATION & ZONE TRACKING
# =============================================================================

def compute_soft_sl(
    arr3: Dict[str, Any],
    arr1: Dict[str, Any],
    entry_3m_pos: int,
    side: str
) -> Dict[str, Any]:
    idx3 = arr3["idx"]
    cycles3 = arr3["sm_cycle"]
    colors3 = arr3["sm_color"]

    entry_cid = cycles3[entry_3m_pos]
    target_cid = entry_cid + 2
    trig_pos_mask = np.where(cycles3 == target_cid)[0]
    if len(trig_pos_mask) < 4:
        return {"ok": False}

    trigger_pos = int(trig_pos_mask[3])
    trigger_dt = idx3[trigger_pos]

    req_color = "Green" if side == "sell" else "Red"
    found_cycles = []
    seen_cids = set()

    for p in range(trigger_pos, -1, -1):
        cid = cycles3[p]
        if cid in seen_cids:
            continue
        seen_cids.add(cid)
        if colors3[p] == req_color:
            cyc_bars = np.where(cycles3 == cid)[0]
            if len(cyc_bars) >= 15:
                found_cycles.append((int(cyc_bars[0]), int(cyc_bars[-1])))
                if len(found_cycles) == 2:
                    break

    if len(found_cycles) < 2:
        return {"ok": False}

    cyc1_start, cyc1_end = found_cycles[0]
    cyc2_start, cyc2_end = found_cycles[1]

    zone_start_pos = cyc2_start
    zone_end_pos = cyc1_end

    def get_zone_extreme(start_p: int, end_p: int):
        v_emakama = arr3["emakama"][start_p:end_p + 1]
        v_kline = arr3["kama_line"][start_p:end_p + 1]
        v_sma = arr3["sma_kama"][start_p:end_p + 1]
        v_bb = arr3["bb_mid"][start_p:end_p + 1]
        v_ema50 = arr3["ema50"][start_p:end_p + 1]

        max_emakama = float(np.nanmax(v_emakama)) if side == "sell" else float(np.nanmin(v_emakama))
        max_kline = float(np.nanmax(v_kline)) if side == "sell" else float(np.nanmin(v_kline))
        max_sma = float(np.nanmax(v_sma)) if side == "sell" else float(np.nanmin(v_sma))
        max_bb = float(np.nanmax(v_bb)) if side == "sell" else float(np.nanmin(v_bb))
        max_ema50 = float(np.nanmax(v_ema50)) if side == "sell" else float(np.nanmin(v_ema50))

        zone_vals = {
            "emakama": max_emakama,
            "kama_line": max_kline,
            "sma_kama": max_sma,
            "bb_mid": max_bb,
            "ema50": max_ema50
        }
        if side == "sell":
            best_key = max(zone_vals, key=zone_vals.get)
        else:
            best_key = min(zone_vals, key=zone_vals.get)

        ext_val = zone_vals[best_key]
        arr_best = {
            "emakama": v_emakama,
            "kama_line": v_kline,
            "sma_kama": v_sma,
            "bb_mid": v_bb,
            "ema50": v_ema50
        }[best_key]
        best_pos_rel = int(np.nanargmax(arr_best)) if side == "sell" else int(np.nanargmin(arr_best))
        ext_dt = idx3[start_p + best_pos_rel]

        return ext_val, {
            "emakama": max_emakama,
            "kama_line": max_kline,
            "sma_kama": max_sma,
            "bb_mid": max_bb,
            "ema50": max_ema50,
            "ext_dt": ext_dt
        }

    init_sl_val, init_vals = get_zone_extreme(zone_start_pos, zone_end_pos)
    if init_sl_val is None:
        return {"ok": False}

    map_1m_dt = trigger_dt + timedelta(minutes=3)
    pos_1m = arr1["idx"].searchsorted(map_1m_dt, side="left")
    if pos_1m >= len(arr1["idx"]):
        return {"ok": False}

    s2_res = track_stage2_exit(arr1, pos_1m, init_sl_val, side)

    return {
        "ok": True,
        "entry_cyc_start": idx3[np.where(cycles3 == entry_cid)[0][0]],
        "cyc3_end": idx3[np.where(cycles3 == target_cid)[0][-1]],
        "trigger_dt": trigger_dt,
        "zone_start": idx3[zone_start_pos],
        "zone_end": idx3[zone_end_pos],
        "init_sl_val": init_sl_val,
        "init_vals": init_vals,
        "map_3m_1m_dt": map_1m_dt,
        "stage2": s2_res
    }


# =============================================================================
# BACKTEST ENGINE WITH TRAILING SL
# =============================================================================

def run_backtest_engine(
    arr1: Dict[str, Any],
    entry_pos_1m: int,
    entry_price: float,
    adj_sl_points: float,
    hard_sl_price: float,
    hard_sl_exit_dt: Optional[pd.Timestamp],
    soft_sl_exit_dt: Optional[pd.Timestamp],
    side: str
) -> Tuple[Dict[float, Dict[str, Any]], str]:
    idx1 = arr1["idx"]
    hi = arr1["high"]
    lo = arr1["low"]
    cl = arr1["close"]

    qty = RISK_PER_TRADE / adj_sl_points if adj_sl_points > 0 else 0.0

    targets = {}
    init_sls = {}
    for r in RATIOS:
        if side == "sell":
            targets[r] = entry_price - r * adj_sl_points
            init_sls[r] = entry_price + adj_sl_points
        else:
            targets[r] = entry_price + r * adj_sl_points
            init_sls[r] = entry_price - adj_sl_points

    cur_sl = dict(init_sls)
    sl_label = {r: "Hard SL" for r in RATIOS}
    done = {r: False for r in RATIOS}
    results = {r: {} for r in RATIOS}

    def record(r, status, px, ts, due_sl=None):
        holding_hrs = round((ts - idx1[entry_pos_1m]).total_seconds() / 3600.0, 4)
        pnl = (entry_price - px) * qty if side == "sell" else (px - entry_price) * qty
        results[r] = {
            "sl_percent": round(adj_sl_points / entry_price * 100.0, 4),
            "target_percent": round(r * adj_sl_points / entry_price * 100.0, 4),
            "sl_price": _f(init_sls[r]),
            "target_price": _f(targets[r]),
            "new_sl_price": _f(cur_sl[r]),
            "sl_hit_dt": _s(ts) if due_sl else None,
            "exit_status": status,
            "sl_hit_due_to": due_sl,
            "exit_price": _f(px),
            "exit_datetime": _s(ts),
            "holding_hours": holding_hrs,
            "qty": _f(qty),
            "pnl": _f(pnl)
        }
        done[r] = True

    for p in range(entry_pos_1m + 1, len(idx1)):
        ts = idx1[p]
        h = hi[p]
        l = lo[p]
        c = cl[p]

        is_hard_hit = (hard_sl_exit_dt is not None and ts >= hard_sl_exit_dt)
        is_soft_hit = (soft_sl_exit_dt is not None and ts >= soft_sl_exit_dt)

        for r in RATIOS:
            if done[r]:
                continue

            hit_target = (l <= targets[r]) if side == "sell" else (h >= targets[r])
            if hit_target:
                record(r, "Target Hit", targets[r], ts, due_sl=None)
                if r >= 2:
                    anchor_px = entry_price if r == 2 else targets[r - 2]
                    anchor_lbl = "Breakeven" if r == 2 else f"1:{int(r - 2)} Target"
                    for r_higher in RATIOS:
                        if r_higher >= r and not done[r_higher]:
                            cur_sl[r_higher] = anchor_px
                            sl_label[r_higher] = anchor_lbl
                continue

            hit_sl = (c >= cur_sl[r]) if side == "sell" else (c <= cur_sl[r])
            if hit_sl:
                if sl_label[r] == "Hard SL":
                    if is_soft_hit and (not is_hard_hit or soft_sl_exit_dt <= hard_sl_exit_dt):
                        record(r, "Soft SL hit", c, ts, due_sl="Soft SL")
                    else:
                        record(r, "Hard SL hit", cur_sl[r], ts, due_sl="Hard SL")
                else:
                    record(r, "Trailed SL hit", cur_sl[r], ts, due_sl=sl_label[r])
                continue
            elif is_hard_hit and sl_label[r] == "Hard SL":
                record(r, "Hard SL hit", hard_sl_price, ts, due_sl="Hard SL")
                continue
            elif is_soft_hit and sl_label[r] == "Hard SL":
                record(r, "Soft SL hit", c, ts, due_sl="Soft SL")
                continue

        if all(done.values()):
            break

    for r in RATIOS:
        if not done[r]:
            record(r, "Open", cl[-1], idx1[-1])

    lines = []
    for r in RATIOS:
        lbl_str = "1:0.5" if r == 0.5 else f"1:{r}"
        lines.append(f"label: {lbl_str}")
        lines.append(f"sl_percent: {results[r]['sl_percent']}")
        lines.append(f"target_percent: {results[r]['target_percent']}")
        lines.append(f"sl_price: {results[r]['sl_price']}")
        if r >= 2:
            lines.append(f"New SL Price : {results[r]['new_sl_price']}")
        lines.append(f"target_price: {results[r]['target_price']}")
        lines.append(f"SL hit Candle Datetime: {results[r]['sl_hit_dt']}")
        lines.append(f"exit_status: {results[r]['exit_status']}")
        lines.append(f"exit_price: {results[r]['exit_price']}")
        lines.append(f"exit_datetime: {results[r]['exit_datetime']}")
        lines.append(f"holding_hours: {results[r]['holding_hours']}")
        lines.append(f"qty: {results[r]['qty']}")
        lines.append(f"pnl: {results[r]['pnl']}\n")

    return results, "\n".join(lines)


# =============================================================================
# =============================================================================

def run_strategy21(
    arr15: Dict[str, Any],
    arr3: Dict[str, Any],
    arr1: Dict[str, Any],
    side: str
) -> pd.DataFrame:
    setups = scan_15m_setups(arr15, side)
    log(f"[{side.upper()}] Found {len(setups)} raw 15min setups.")

    idx3 = arr3["idx"]
    idx15 = arr15["idx"]
    idx1 = arr1["idx"]

    # Pre-scan entries for all setups to identify shared Entry Datetimes
    setup_entries = []
    entry_dt_to_max_start = {}

    for s in setups:
        end_15m_dt = s["end_dt"]
        map_3m_dt = end_15m_dt + timedelta(minutes=15)
        pos3 = idx3.searchsorted(map_3m_dt, side="left")
        if pos3 >= len(idx3) or pos3 < 3:
            setup_entries.append(None)
            continue

        prev3_ok = True
        for p in range(pos3 - 3, pos3):
            op3 = arr3["open"][p]
            cl3 = arr3["close"][p]
            ema3 = arr3["ema50"][p]
            if side == "sell":
                if not (op3 > ema3 and cl3 > ema3):
                    prev3_ok = False
                    break
            else:
                if not (op3 < ema3 and cl3 < ema3):
                    prev3_ok = False
                    break

        if not prev3_ok:
            setup_entries.append(None)
            continue

        entry_3m_pos = None
        for p in range(pos3, len(idx3)):
            cl3 = arr3["close"][p]
            ema3 = arr3["ema50"][p]
            if side == "sell":
                if cl3 < ema3 and cl3 < s["extreme_close"]:
                    entry_3m_pos = p
                    break
            else:
                if cl3 > ema3 and cl3 > s["extreme_close"]:
                    entry_3m_pos = p
                    break

        if entry_3m_pos is None:
            setup_entries.append(None)
            continue

        entry_dt = idx3[entry_3m_pos]
        entry_price = float(arr3["close"][entry_3m_pos])
        setup_entries.append((entry_3m_pos, entry_dt, entry_price, map_3m_dt))

        # Track the most recent setup start_dt for each entry_dt
        if entry_dt not in entry_dt_to_max_start or s["start_dt"] > entry_dt_to_max_start[entry_dt]:
            entry_dt_to_max_start[entry_dt] = s["start_dt"]

    rows = []
    last_print_date = None

    for i, s in enumerate(setups):
        end_15m_dt = s["end_dt"]
        d = end_15m_dt.date()
        if d != last_print_date:
            log(f"[STRATEGY 21 {side.upper()}] Processing Date: {d}")
            last_print_date = d

        map_3m_dt = end_15m_dt + timedelta(minutes=15)
        row = OrderedDict()
        row["15min Setup Starttime"] = _s(s["start_dt"])
        row["15min Setup Endtime"] = _s(s["end_dt"])
        row["15min candle mapped to 3min is"] = _s(map_3m_dt)

        entry_info = setup_entries[i]
        if entry_info is None:
            # Check why invalidated
            pos3 = idx3.searchsorted(map_3m_dt, side="left")
            if pos3 < len(idx3) and pos3 >= 3:
                prev3_ok = True
                for p in range(pos3 - 3, pos3):
                    op3 = arr3["open"][p]
                    cl3 = arr3["close"][p]
                    ema3 = arr3["ema50"][p]
                    if side == "sell":
                        if not (op3 > ema3 and cl3 > ema3):
                            prev3_ok = False
                            break
                    else:
                        if not (op3 < ema3 and cl3 < ema3):
                            prev3_ok = False
                            break
                row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "yes" if prev3_ok else "no"
                row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "no"
                if not prev3_ok:
                    row["Status"] = "Invalidated due to 3min candle does not open close above EMA 50" if side == "sell" else "Invalidated due to 3min candle does not open close below EMA 50"
                else:
                    row["Status"] = "Invalidated due to entry signal not found"
            else:
                row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "no"
                row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "no"
                row["Status"] = "Invalidated due to entry signal not found"
            rows.append(row)
            continue

        entry_3m_pos, entry_dt, entry_price, map_3m_dt = entry_info
        row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "yes"
        row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "yes"
        row["Entry Datetime"] = _s(entry_dt)
        row["Entry Price"] = _f(entry_price)

        # Check if another more recent setup shares this same entry_dt
        if s["start_dt"] < entry_dt_to_max_start[entry_dt]:
            row["Status"] = "Invalidated to Entry occured at same datetime"
            s_info = OrderedDict()
            s_info["15min Setup Starttime :"] = _s(s["start_dt"])
            s_info["15min Setup Endtime :"] = _s(s["end_dt"])
            s_info["15min Setup First candle DT :"] = _s(s["dt_1"])
            s_info["15min Setup First candle High :" if side == "sell" else "15min Setup First candle Low :"] = _f(s["extreme_1"])
            s_info["15min Setup 2nd candle DT :"] = _s(s["dt_2"])
            s_info["15min Setup 2nd candle High :" if side == "sell" else "15min Setup 2nd candle Low :"] = _f(s["extreme_2"])
            s_info["15min Setup 3rd candle DT :"] = _s(s["dt_3"])
            s_info["15min Setup 3rd candle High :" if side == "sell" else "15min Setup 3rd candle Low :"] = _f(s["extreme_3"])
            s_info["Highest close Value in Setup :" if side == "sell" else "Lowest close Value in Setup :"] = _f(s["extreme_close"])
            s_info["15min candle mapped to 3min is :"] = _s(map_3m_dt)
            s_info["Entry Datetime :"] = _s(entry_dt)
            s_info["Entry Price :"] = _f(entry_price)
            s_info["Status :"] = "Invalidated to Entry occured at same datetime"
            row["Strategy Additional info"] = vkv(s_info)
            row["Soft SL Additional info"] = "None"
            row["Backtest Result"] = "None"
            rows.append(row)
            continue

        row["Status"] = "Intrade"

        sl3 = compute_hard_sl_on_tf(arr3, entry_dt, side)
        sl15 = compute_hard_sl_on_tf(arr15, entry_dt, side)

        if not sl3.get("ok") or not sl15.get("ok"):
            row["Status"] = "Invalidated due to Hard SL computation failure"
            rows.append(row)
            continue

        sl3_val = sl3["sl_val"]
        sl15_val = sl15["sl_val"]

        if side == "sell":
            hard_sl_from = "15min" if sl15_val >= sl3_val else "3min"
            hard_sl_price = max(sl3_val, sl15_val)
        else:
            hard_sl_from = "15min" if sl15_val <= sl3_val else "3min"
            hard_sl_price = min(sl3_val, sl15_val)

        sl_points = abs(entry_price - hard_sl_price)
        hard_sl_pct = (sl_points / entry_price * 100.0) if entry_price > 0 else 0.0
        adj_sl_points = sl_points * 1.30
        sl_price_for_qty = entry_price + adj_sl_points if side == "sell" else entry_price - adj_sl_points
        sl_pct_for_qty = (adj_sl_points / entry_price * 100.0) if entry_price > 0 else 0.0

        row["Hard SL obtained from"] = hard_sl_from
        row["Hard SL Value"] = _f(hard_sl_price)
        row["Hard SL Percentage"] = _f(hard_sl_pct, 4)
        row["Actual SL Points"] = _f(sl_points, 4)
        row["SL Value Consider for Qty"] = _f(sl_price_for_qty, 4)
        row["SL Points for Qty"] = _f(adj_sl_points, 4)
        row["SL Value Percentage consider for Qty"] = _f(sl_pct_for_qty, 4)

        qty = RISK_PER_TRADE / adj_sl_points if adj_sl_points > 0 else 0.0
        invest_val = qty * entry_price

        row["Qty"] = _f(qty)
        row["Investment Value for Ratios"] = _f(invest_val)

        pos_1m = idx1.searchsorted(entry_dt, side="left")
        hsl_stage2 = track_stage2_exit(arr1, pos_1m, hard_sl_price, side)
        hard_sl_exit_dt = hsl_stage2["exit_dt"] if hsl_stage2.get("exit_found") else None

        ssl = compute_soft_sl(arr3, arr1, entry_3m_pos, side)
        soft_sl_exit_dt = ssl["stage2"]["exit_dt"] if ssl.get("ok") and ssl["stage2"].get("exit_found") else None

        bt_results, bt_text = run_backtest_engine(
            arr1, pos_1m, entry_price, adj_sl_points, hard_sl_price,
            hard_sl_exit_dt, soft_sl_exit_dt, side
        )

        for r in RATIOS:
            lbl = f"1:{r}" if r != 0.5 else "1:0.5"
            res = bt_results[r]
            row[f"Status {lbl}"] = res["exit_status"]
            row[f"{lbl} Exit Datetime"] = res["exit_datetime"]
            row[f"{lbl} Exit Price"] = res["exit_price"]
            row[f"{lbl} SL hit Due to"] = res.get("sl_hit_due_to")
            row[f"{lbl} Holding Time (hrs)"] = res["holding_hours"]
            row[f"P/L {lbl}"] = res["pnl"]

        pos_15m = s["pos_3"]
        vol_curr = float(arr15["volume"][pos_15m])
        vol_12h = float(np.sum(arr15["volume"][max(0, pos_15m - 47):pos_15m + 1]))
        vol_24h = float(np.sum(arr15["volume"][max(0, pos_15m - 95):pos_15m + 1]))
        vol_48h = float(np.sum(arr15["volume"][max(0, pos_15m - 191):pos_15m + 1]))

        row["Current 15min:last 12 hrs Vol"] = _f(vol_curr / vol_12h if vol_12h > 0 else 0.0, 4)
        row["Current 15min:last 24 hrs Vol"] = _f(vol_curr / vol_24h if vol_24h > 0 else 0.0, 4)
        row["Current 15min:last 48 hrs Vol"] = _f(vol_curr / vol_48h if vol_48h > 0 else 0.0, 4)

        # Exactly format Strategy Additional Info block matching specification
        s_info = OrderedDict()
        s_info["15min Setup Starttime :"] = _s(s["start_dt"])
        s_info["15min Setup Endtime :"] = _s(s["end_dt"])
        s_info["15min Setup First candle DT :"] = _s(s["dt_1"])
        s_info["15min Setup First candle High :" if side == "sell" else "15min Setup First candle Low :"] = _f(s["extreme_1"])
        s_info["15min Setup 2nd candle DT :"] = _s(s["dt_2"])
        s_info["15min Setup 2nd candle High :" if side == "sell" else "15min Setup 2nd candle Low :"] = _f(s["extreme_2"])
        s_info["15min Setup 3rd candle DT :"] = _s(s["dt_3"])
        s_info["15min Setup 3rd candle High :" if side == "sell" else "15min Setup 3rd candle Low :"] = _f(s["extreme_3"])
        s_info["Highest close Value in Setup :" if side == "sell" else "Lowest close Value in Setup :"] = _f(s["extreme_close"])
        s_info["15min candle mapped to 3min is :"] = _s(map_3m_dt)
        s_info["Previous 3 minute candle open and close above EMA 50 :" if side == "sell" else "Previous 3 minute candle open and close below EMA 50 :"] = "yes"
        s_info["Candle closing below Highest close Value :" if side == "sell" else "Candle closing above Lowest close Value :"] = "yes"
        s_info["Entry Datetime :"] = _s(entry_dt)
        s_info["Entry Price :"] = _f(entry_price)
        s_info["3min Hard SL :"] = _f(sl3_val)
        s_info["Previous Nearest RSI BELOW 30 Datetime :" if side == "sell" else "Previous Nearest RSI ABOVE 70 Datetime :"] = _s(sl3["event1_dt"])
        s_info["Previous Nearest RSI BELOW 30 Value :" if side == "sell" else "Previous Nearest RSI ABOVE 70 Value :"] = _f(sl3["event1_val"])
        s_info["Previous Nearest RSI above 70 Datetime :" if side == "sell" else "Previous Nearest RSI BELOW 30 Datetime :"] = _s(sl3["event2_dt"])
        s_info["Previous Nearest RSI above 70 Value :" if side == "sell" else "Previous Nearest RSI BELOW 30 Value :"] = _f(sl3["event2_val"])
        s_info["RSI above 70 MACD cycle Starttime :" if side == "sell" else "RSI below 30 MACD cycle Starttime :"] = _s(sl3["cyc_start_dt"])
        s_info["RSI above 70 MACD cycle endtime :" if side == "sell" else "RSI below 30 MACD cycle endtime :"] = _s(sl3["cyc_end_dt"])
        s_info["highest high found in that MACD cycle :" if side == "sell" else "lowest low found in that MACD cycle :"] = _f(sl3["extreme1_val"])
        s_info["highest high found candle Datetime :" if side == "sell" else "lowest low found candle Datetime :"] = _s(sl3["extreme1_dt"])
        s_info["Lowest low from highest high candle datetime to entry candle Datetime :" if side == "sell" else "Highest high from lowest low candle datetime to entry candle Datetime :"] = _f(sl3["extreme2_val"])
        s_info["candle cuts which fibonacci level Datetime :"] = _s(sl3["cut_dt"])
        s_info["candle cuts which fibonacci level :"] = sl3["cut_level"]
        s_info["candle cuts which fibonacci level Value of that level :"] = _f(sl3["cut_level_val"])
        s_info["is candle cut fibonacci level is top most level or not state :"] = sl3["is_topmost"]
        s_info["which is 1 level Upper of cut level :" if side == "sell" else "which is 1 level Lower of cut level :"] = sl3["upper_name"]
        s_info["1 level Upper of cut level Value :" if side == "sell" else "1 level Lower of cut level Value :"] = _f(sl3["upper_val"])
        s_info["SL on 3min :"] = _f(sl3_val)

        # 15min mapping & fields
        map_15m_dt = entry_dt.floor("15min")
        s_info["3min Entry Candle mapped to 15min :"] = _s(map_15m_dt)
        s_info["15min Previous Nearest RSI BELOW 30 Datetime :" if side == "sell" else "15min Previous Nearest RSI ABOVE 70 Datetime :"] = _s(sl15["event1_dt"])
        s_info["15min Previous Nearest RSI BELOW 30 Value :" if side == "sell" else "15min Previous Nearest RSI ABOVE 70 Value :"] = _f(sl15["event1_val"])
        s_info["15min Previous Nearest RSI above 70 Datetime :" if side == "sell" else "15min Previous Nearest RSI BELOW 30 Datetime :"] = _s(sl15["event2_dt"])
        s_info["15min Previous Nearest RSI above 70 Value :" if side == "sell" else "15min Previous Nearest RSI BELOW 30 Value :"] = _f(sl15["event2_val"])
        s_info["15min RSI above 70 MACD cycle Starttime :" if side == "sell" else "15min RSI below 30 MACD cycle Starttime :"] = _s(sl15["cyc_start_dt"])
        s_info["15min RSI above 70 MACD cycle endtime :" if side == "sell" else "15min RSI below 30 MACD cycle endtime :"] = _s(sl15["cyc_end_dt"])
        s_info["15min highest high found in that MACD cycle :" if side == "sell" else "15min lowest low found in that MACD cycle :"] = _f(sl15["extreme1_val"])
        s_info["15min highest high found candle Datetime :"] = _s(sl15["extreme1_dt"])
        s_info["15min Lowest low from highest high candle datetime to entry candle Datetime :" if side == "sell" else "15min Highest high from lowest low candle datetime to entry candle Datetime :"] = _f(sl15["extreme2_val"])
        s_info["15min candle cuts which fibonacci level Datetime :"] = _s(sl15["cut_dt"])
        s_info["15min candle cuts which fibonacci level :"] = sl15["cut_level"]
        s_info["15min candle cuts which fibonacci level Value of that level :"] = _f(sl15["cut_level_val"])
        s_info["15min is candle cut fibonacci level is top most level or not state :"] = sl15["is_topmost"]
        s_info["15min which is 1 level Upper of cut level :" if side == "sell" else "15min which is 1 level Lower of cut level :"] = sl15["upper_name"]
        s_info["15min 1 level Upper of cut level Value :" if side == "sell" else "15min 1 level Lower of cut level Value :"] = _f(sl15["upper_val"])
        s_info["15min SL Value obtained :"] = _f(sl15_val)
        s_info["Hard SL obtained from :"] = hard_sl_from
        s_info["3min Entry candle mappped to 1min for hard SL Datetime :"] = _s(entry_dt)

        # Hard SL Stage 2 details
        s_info["Final Stage 2 :"] = ""
        s_info["Final Candle close above Hard SL Datetime :" if side == "sell" else "Final Candle close below Hard SL Datetime :"] = _s(hsl_stage2.get("s1_dt"))
        s_info["Final Candle close above Hard SL close :" if side == "sell" else "Final Candle close below Hard SL close :"] = _f(hsl_stage2.get("s1_close"))
        s_info["Final Stage 2 Step 1 :"] = ""
        s_info["Final 1st Candle close for Hard SL on 1min :"] = _s(hsl_stage2.get("s1_dt"))
        s_info["Final Highest high from red candle to 1st Candle close for Hard SL on 1min Value :" if side == "sell" else "Final Lowest low from green candle to 1st Candle close for Hard SL on 1min Value :"] = _f(hsl_stage2.get("h1_val"))
        s_info["Final Highest high from red candle to 1st Candle close for Hard SL on 1min Datetime :" if side == "sell" else "Final Lowest low from green candle to 1st Candle close for Hard SL on 1min Datetime :"] = _s(hsl_stage2.get("h1_dt"))
        s_info["Final Most Updated high after first red candle Datetime :" if side == "sell" else "Final Most Updated low after first green candle Datetime :"] = _s(hsl_stage2.get("upd_h1_dt"))
        s_info["Final Most Updated high after first red candle Value :" if side == "sell" else "Final Most Updated low after first green candle Value :"] = _f(hsl_stage2.get("upd_h1_val"))
        s_info["Final Stage 2 Candle closing above the Most Updated high or Initial high Datetime :" if side == "sell" else "Final Stage 2 Candle closing below the Most Updated low or Initial low Datetime :"] = _s(hsl_stage2.get("s2_s1_dt"))
        s_info["Final Stage 2 Candle closing above the Most Updated high or Initial high Value :" if side == "sell" else "Final Stage 2 Candle closing below the Most Updated low or Initial low Value :"] = _f(hsl_stage2.get("s2_s1_close"))
        s_info["Final Stage 2 Step 1 Final candle Datetime :"] = _s(hsl_stage2.get("s2_s1_dt"))
        s_info["Final Stage 2 Step 1 Final candle close :"] = _f(hsl_stage2.get("s2_s1_close"))
        s_info["Final Stage 2 Step 2 :"] = ""
        s_info["Final 2nd Red candle Datetime :" if side == "sell" else "Final 2nd Green candle Datetime :"] = _s(hsl_stage2.get("red2_dt"))
        s_info["Final Highest high from 2nd red candle Value :" if side == "sell" else "Final Lowest low from 2nd green candle Value :"] = _f(hsl_stage2.get("h2_val"))
        s_info["Final Highest high from 2nd red candle Datetime :" if side == "sell" else "Final Lowest low from 2nd green candle Datetime :"] = _s(hsl_stage2.get("h2_dt"))
        s_info["Final Most Updated high after second red candle Value :" if side == "sell" else "Final Most Updated low after second green candle Value :"] = _f(hsl_stage2.get("upd_h2_val"))
        s_info["Final Most Updated high after second red candle Datetime :" if side == "sell" else "Final Most Updated low after second green candle Datetime :"] = _s(hsl_stage2.get("upd_h2_dt"))
        s_info["Final Candle close above Highest high or Most Updated high after 2nd red candle Value :" if side == "sell" else "Final Candle close below Lowest low or Most Updated low after 2nd green candle Value :"] = _f(hsl_stage2.get("exit_price"))
        s_info["Final Candle close above Highest high or Most Updated high after 2nd red candle Datetime :" if side == "sell" else "Final Candle close below Lowest low or Most Updated low after 2nd green candle Datetime :"] = _s(hsl_stage2.get("exit_dt"))
        s_info["Final Exit Candle Datetime :"] = _s(hard_sl_exit_dt)
        s_info["Final Exit Price :"] = _f(hsl_stage2.get("exit_price"))
        s_info["Final Exit Due to Hard SL :"] = "yes" if hsl_stage2.get("exit_found") else "no"

        row["Strategy Additional info"] = vkv(s_info)

        # Exactly format Soft SL Additional Info block
        if ssl.get("ok"):
            ss_info = OrderedDict()
            ss_info["Soft SL :"] = ""
            ss_info["1st MACD cycle startime :"] = _s(ssl["entry_cyc_start"])
            ss_info["3rd MACD cycle endtime :"] = _s(ssl["cyc3_end"])
            ss_info["Trigger Point Datetime :"] = _s(ssl["trigger_dt"])
            ss_info["Inital Soft SL zone Starttime :"] = _s(ssl["zone_start"])
            ss_info["Inital Soft SL zone endtime :"] = _s(ssl["zone_end"])
            ss_info["Inital Soft SL zone obtained values :"] = ""
            ss_info["KAMAEMA.py Values for SL :"] = _f(ssl["init_vals"].get("emakama"))
            ss_info["kama lines code.py Values for SL :"] = _f(ssl["init_vals"].get("kama_line"))
            ss_info["from kama_ma_dataset.py SMA Value for SL :"] = _f(ssl["init_vals"].get("sma_kama"))
            ss_info["Middle Bollinger band level Values for SL :"] = _f(ssl["init_vals"].get("bb_mid"))
            ss_info["EMA 50 Value for SL :"] = _f(ssl["init_vals"].get("ema50"))
            ss_info["Highest High Value found Inital Soft SL zone obtained values :" if side == "sell" else "Lowest Low Value found Inital Soft SL zone obtained values :"] = _f(ssl["init_sl_val"])
            ss_info["Highest High Value found Inital Soft SL zone Value Datetime :" if side == "sell" else "Lowest Low Value found Inital Soft SL Value Datetime :"] = _s(ssl["init_vals"].get("ext_dt"))
            ss_info["Inital Soft SL Value :"] = _f(ssl["init_sl_val"])
            ss_info["3min Candle Datetime which gets mapped to 1min :"] = _s(ssl["trigger_dt"])
            ss_info["3min candle mapped to 1min for Soft SL Exit :"] = _s(ssl["map_3m_1m_dt"])
            ss_info["Most Highest high updated after Inital Soft SL Value :" if side == "sell" else "Most Lowest low updated after Inital Soft SL Value :"] = _f(ssl["stage2"].get("upd_h1_val"))
            ss_info["Most Highest high updated after Inital Soft SL Value Datetime :" if side == "sell" else "Most Lowest low updated after Inital Soft SL Value Datetime :"] = _s(ssl["stage2"].get("upd_h1_dt"))
            ss_info["Candle close above Soft SL Value found from :"] = "Inital Soft SL Value"
            ss_info["Candle close above Soft SL Datetime :" if side == "sell" else "Candle close below Soft SL Datetime :"] = _s(ssl["stage2"].get("s1_dt"))
            ss_info["Candle close above Soft SL close :" if side == "sell" else "Candle close below Soft SL close :"] = _f(ssl["stage2"].get("s1_close"))

            s2 = ssl["stage2"]
            ss_info["Stage 2 :"] = ""
            ss_info["Candle close above Soft SL Datetime :"] = _s(s2.get("s1_dt"))
            ss_info["Candle close above Soft SL close :"] = _f(s2.get("s1_close"))
            ss_info["Stage 2 Step 1 :"] = ""
            ss_info["1st Candle close for Soft SL on 1min :"] = _s(s2.get("s1_dt"))
            ss_info["highest high from red candle to 1st Candle close for Soft SL on 1min Value :" if side == "sell" else "lowest low from green candle to 1st Candle close for Soft SL on 1min Value :"] = _f(s2.get("h1_val"))
            ss_info["highest high from red candle to 1st Candle close for Soft SL on 1min Datetime :" if side == "sell" else "lowest low from green candle to 1st Candle close for Soft SL on 1min Datetime :"] = _s(s2.get("h1_dt"))
            ss_info["Most Updated high after first red candle Datetime :" if side == "sell" else "Most Updated low after first green candle Datetime :"] = _s(s2.get("upd_h1_dt"))
            ss_info["Most Updated high after first red candle Value :" if side == "sell" else "Most Updated low after first green candle Value :"] = _f(s2.get("upd_h1_val"))
            ss_info["Stage 2 Candle closing above the Most Updated high or intial high Datetime :" if side == "sell" else "Stage 2 Candle closing below the Most Updated low or intial low Datetime :"] = _s(s2.get("s2_s1_dt"))
            ss_info["Stage 2 Candle closing above the Most Updated high or intial high Value :" if side == "sell" else "Stage 2 Candle closing below the Most Updated low or intial low Value :"] = _f(s2.get("s2_s1_close"))
            ss_info["Stage 2 Step 1 Final candle Datetime :"] = _s(s2.get("s2_s1_dt"))
            ss_info["Stage 2 Step 1 Final candle close :"] = _f(s2.get("s2_s1_close"))
            ss_info["Stage 2 Step 2 :"] = ""
            ss_info["2nd Red candle found after Stage 2 Candle closing above the Most Updated high or intial high Datetime :" if side == "sell" else "2nd Green candle found after Stage 2 Candle closing below the Most Updated low or intial low Datetime :"] = _s(s2.get("red2_dt"))
            ss_info["Highest high found from 2nd red candle to 2nd Red candle found after Stage 2 Candle closing above the Most Updated high or intial high Value :" if side == "sell" else "Lowest low found from 2nd green candle to 2nd Green candle found after Stage 2 Candle closing below the Most Updated low or intial low Value :"] = _f(s2.get("h2_val"))
            ss_info["Higest high found from 2nd red candle to 2nd Red candle found after Stage 2 Candle closing above the Most Updated high or intial high Value Datetime :" if side == "sell" else "Lowest low found from 2nd green candle to 2nd Green candle found after Stage 2 Candle closing below the Most Updated low or intial low Value Datetime :"] = _s(s2.get("h2_dt"))
            ss_info["Most Updated high after second red candle Value :" if side == "sell" else "Most Updated low after second green candle Value :"] = _f(s2.get("upd_h2_val"))
            ss_info["Most Updated high after second red candle Datetime :" if side == "sell" else "Most Updated low after second green candle Datetime :"] = _s(s2.get("upd_h2_dt"))
            ss_info["candle close above highest high or most updated high after 2nd red candle :" if side == "sell" else "candle close below lowest low or most updated low after 2nd green candle :"] = _f(s2.get("exit_price"))
            ss_info["candle close above highest high or most updated high after 2nd red candle Datetime :" if side == "sell" else "candle close below lowest low or most updated low after 2nd green candle Datetime :"] = _s(s2.get("exit_dt"))
            ss_info["Exit Candle Datetime :"] = _s(s2.get("exit_dt"))
            ss_info["Exit Price :"] = _f(s2.get("exit_price"))
            ss_info["Exit Due to Soft SL :"] = "yes" if s2.get("exit_found") else "no"

            # Final Soft SL zone mirror block
            ss_info["Final Inital Soft SL zone Starttime :"] = _s(ssl["zone_start"])
            ss_info["Final Inital Soft SL zone endtime :"] = _s(ssl["zone_end"])
            ss_info["Final Inital Soft SL zone obtained values :"] = ""
            ss_info["Final KAMAEMA.py Values for SL :"] = _f(ssl["init_vals"].get("emakama"))
            ss_info["Final kama lines code.py Values for SL :"] = _f(ssl["init_vals"].get("kama_line"))
            ss_info["Final from kama_ma_dataset.py SMA Value for SL :"] = _f(ssl["init_vals"].get("sma_kama"))
            ss_info["Final Middle Bollinger band level Values for SL :"] = _f(ssl["init_vals"].get("bb_mid"))
            ss_info["Final EMA 50 Value for SL :"] = _f(ssl["init_vals"].get("ema50"))
            ss_info["Final Highest High Value found Inital Soft SL zone obtained values :" if side == "sell" else "Final Lowest Low Value found Inital Soft SL zone obtained values :"] = _f(ssl["init_sl_val"])
            ss_info["Final Highest High Value found Inital Soft SL zone Value Datetime :" if side == "sell" else "Final Lowest Low Value found Inital Soft SL Value Datetime :"] = _s(ssl["init_vals"].get("ext_dt"))
            ss_info["Final Inital Soft SL Value on 3min :"] = _f(ssl["init_sl_val"])
            ss_info["3min Candle Datetime which gets mapped to 1min :"] = _s(ssl["trigger_dt"])
            ss_info["3min candle mapped to 1min for Soft SL Exit :"] = _s(ssl["map_3m_1m_dt"])
            ss_info["Final Most Highest high updated after Inital Soft SL Value :" if side == "sell" else "Final Most Lowest low updated after Inital Soft SL Value :"] = _f(s2.get("upd_h1_val"))
            ss_info["Final Most Highest high updated after Inital Soft SL Value Datetime :" if side == "sell" else "Final Most Lowest low updated after Inital Soft SL Value Datetime :"] = _s(s2.get("upd_h1_dt"))
            ss_info["Final Candle close above Soft SL Value found from :"] = "Inital Soft SL Value"
            ss_info["Final Stage 2 :"] = ""
            ss_info["Final Candle close above Soft SL Datetime :"] = _s(s2.get("s1_dt"))
            ss_info["Final Candle close above Soft SL close :"] = _f(s2.get("s1_close"))
            ss_info["Final Stage 2 Step 1 :"] = ""
            ss_info["Final 1st Candle close for Soft SL on 1min :"] = _s(s2.get("s1_dt"))
            ss_info["Final Highest high from red candle to 1st Candle close for Soft SL on 1min Value :" if side == "sell" else "Final Lowest low from green candle to 1st Candle close for Soft SL on 1min Value :"] = _f(s2.get("h1_val"))
            ss_info["Final Highest high from red candle to 1st Candle close for Soft SL on 1min Datetime :" if side == "sell" else "Final Lowest low from green candle to 1st Candle close for Soft SL on 1min Datetime :"] = _s(s2.get("h1_dt"))
            ss_info["Final Most Updated high after first red candle Datetime :" if side == "sell" else "Final Most Updated low after first green candle Datetime :"] = _s(s2.get("upd_h1_dt"))
            ss_info["Final Most Updated high after first red candle Value :" if side == "sell" else "Final Most Updated low after first green candle Value :"] = _f(s2.get("upd_h1_val"))
            ss_info["Final Stage 2 Candle closing above the Most Updated high or Initial high Datetime :" if side == "sell" else "Final Stage 2 Candle closing below the Most Updated low or Initial low Datetime :"] = _s(s2.get("s2_s1_dt"))
            ss_info["Final Stage 2 Candle closing above the Most Updated high or Initial high Value :" if side == "sell" else "Final Stage 2 Candle closing below the Most Updated low or Initial low Value :"] = _f(s2.get("s2_s1_close"))
            ss_info["Final Stage 2 Step 1 Final candle Datetime :"] = _s(s2.get("s2_s1_dt"))
            ss_info["Final Stage 2 Step 1 Final candle close :"] = _f(s2.get("s2_s1_close"))
            ss_info["Final Stage 2 Step 2 :"] = ""
            ss_info["Final 2nd Red candle Datetime :" if side == "sell" else "Final 2nd Green candle Datetime :"] = _s(s2.get("red2_dt"))
            ss_info["Final Highest high from 2nd red candle Value :" if side == "sell" else "Final Lowest low from 2nd green candle Value :"] = _f(s2.get("h2_val"))
            ss_info["Final Highest high from 2nd red candle Datetime :" if side == "sell" else "Final Lowest low from 2nd green candle Datetime :"] = _s(s2.get("h2_dt"))
            ss_info["Final Most Updated high after second red candle Value :" if side == "sell" else "Final Most Updated low after second green candle Value :"] = _f(s2.get("upd_h2_val"))
            ss_info["Final Most Updated high after second red candle Datetime :" if side == "sell" else "Final Most Updated low after second green candle Datetime :"] = _s(s2.get("upd_h2_dt"))
            ss_info["Final Candle close above Highest high or Most Updated high after 2nd red candle Value :" if side == "sell" else "Final Candle close below Lowest low or Most Updated low after 2nd green candle Value :"] = _f(s2.get("exit_price"))
            ss_info["Final Candle close above Highest high or Most Updated high after 2nd red candle Datetime :" if side == "sell" else "Final Candle close below Lowest low or Most Updated low after 2nd green candle Datetime :"] = _s(s2.get("exit_dt"))
            ss_info["Final Exit Candle Datetime :"] = _s(s2.get("exit_dt"))
            ss_info["Final Exit Price :"] = _f(s2.get("exit_price"))
            ss_info["Final Exit Due to Soft SL :"] = "yes" if s2.get("exit_found") else "no"

            row["Soft SL Additional info"] = vkv(ss_info)
        else:
            row["Soft SL Additional info"] = "None"

        row["Backtest Result"] = bt_text

        rows.append(row)

    return pd.DataFrame(rows)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main():
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    log("=" * 110)
    log("STRATEGY 21 — MULTI-TIMEFRAME ENGINE (15min -> 3min -> 1min)")
    log("=" * 110)

    log("Loading 15min data...")
    df15_raw = load_ohlcv(IN_15M)
    log("Loading 3min data...")
    df3_raw = load_ohlcv(IN_3M)
    log("Loading 1min data...")
    df1_raw = load_ohlcv(IN_1M)

    log("Preparing indicators...")
    arr15 = prepare_15m_data(df15_raw)
    arr3 = prepare_3m_data(df3_raw)
    arr1 = prepare_1m_data(df1_raw)

    for side, out_path in [("sell", OUT_SELL_XLSX), ("buy", OUT_BUY_XLSX)]:
        log(f"\n{'─' * 70}\nRUNNING {side.upper()}\n{'─' * 70}")
        res_df = run_strategy21(arr15, arr3, arr1, side)
        res_df.to_excel(out_path, index=False)
        log(f"Completed {side.upper()} — Saved {len(res_df)} rows to {out_path}")

    dt = time.time() - t0
    log("=" * 110)
    log(f"DONE ✅ — Total Execution Time: {dt:.2f}s ({dt/60.0:.2f} min)")
    log("=" * 110)


if __name__ == "__main__":
    main()
