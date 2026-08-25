import os
import sys

output_path = "/Users/Vraj/Downloads/Git/strategy-engine/Live/Strategy 21/Bridge-S21-BTCUSD-Live.py"

content = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time as _time
import threading
import hashlib
import os
import json
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import numpy as np
import pandas as pd
import talib

_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, os.path.basename(__file__).replace(".py", ".json"))
with open(_CONFIG_PATH) as f:
    _config = json.load(f)

BOT_TOKEN      = _config["BOT_TOKEN"]
CHAT_ID        = _config["CHAT_ID"]
MT5_BRIDGE_URL = _config["MT5_BRIDGE_URL"]
MT5_API_KEY    = _config["MT5_API_KEY"]
MAGIC          = int(_config.get("MAGIC", 21001))
RISK_PER_TRADE = float(_config.get("RISK_PER_TRADE", 500.0))
TELEGRAM_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
FLATTEN_BEFORE_WEEKEND     = bool(_config.get("FLATTEN_BEFORE_WEEKEND", True))
FLATTEN_BEFORE_DAILY_BREAK = bool(_config.get("FLATTEN_BEFORE_DAILY_BREAK", False))
FLATTEN_LEAD_MIN           = int(_config.get("FLATTEN_LEAD_MIN", 10))
TRAIL_INTERVAL_SEC         = int(_config.get("TRAIL_INTERVAL_SEC", 10))

sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
import trade_db
trade_db.init("S21-BTCUSD-LIVE", _config.get("TRADE_DB_URL", ""), magic=MAGIC, bridge_url=MT5_BRIDGE_URL)

COIN_NAME      = "BTCUSDT"
SYMBOL         = "BTCUSD"
SYMBOL_BRIDGE  = "BTCUSD"

LOOKBACK_15M = 2500
LOOKBACK_3M  = 4000
LOOKBACK_1M  = 6000

SMOOTH_MIN_RUN  = 4
ALL_RATIOS      = [0.5] + list(range(1, 11))
RATIOS_FULL     = list(range(1, 11))
TRAIL_CPS       = list(range(2, 10))
SCAN_SLEEP_SEC  = 60
RECENT_1M_COUNT = 10

BASE_LOG_DIR   = Path("./bridge/Strategy 21 Live Logs")
BASE_LOG_DIR.mkdir(parents=True, exist_ok=True)
_BUY_LOG_PATH  = BASE_LOG_DIR / f"Strategy21_{SYMBOL}_BUY_live.csv"
_SELL_LOG_PATH = BASE_LOG_DIR / f"Strategy21_{SYMBOL}_SELL_live.csv"


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def log(msg: str):
    print(msg, flush=True)

def _s(ts) -> Optional[str]:
    if ts is None: return None
    if isinstance(ts, pd.Timestamp) and pd.isna(ts): return None
    return str(ts)

def _f(x, d: int = 6):
    if x is None: return None
    if isinstance(x, (float, np.floating)):
        if np.isnan(x): return None
        return round(float(x), d)
    if isinstance(x, (int, np.integer)): return int(x)
    if isinstance(x, pd.Timestamp): return str(x)
    return x

def _kv_lines(obj: Any, indent: int = 0) -> List[str]:
    sp = " " * indent
    if obj is None: return [f"{sp}None"]
    if isinstance(obj, (str, int, float, bool, np.floating, np.integer)): return [f"{sp}{obj}"]
    if isinstance(obj, list):
        out = []
        for v in obj:
            if isinstance(v, (dict, OrderedDict, list)):
                out.append(f"{sp}-"); out.extend(_kv_lines(v, indent + 2))
            else: out.append(f"{sp}- {v}")
        return out
    if isinstance(obj, (dict, OrderedDict)):
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, OrderedDict, list)):
                out.append(f"{sp}{k}:"); out.extend(_kv_lines(v, indent + 2))
            else: out.append(f"{sp}{k}: {_f(v)}")
        return out
    return [f"{sp}{obj}"]

def vkv(obj: Any) -> str:
    return "\\n".join(_kv_lines(obj, 0))


# -----------------------------------------------------------------------------
# FETCH LIVE DATA
# -----------------------------------------------------------------------------

_candle_cache = {}

def get_rolling_mt5_candles(symbol: str, timeframe: str, required_lookback: int):
    global _candle_cache
    key = (symbol, timeframe)
    if _candle_cache.get(key) is None:
        df = fetch_live_mt5_candles(symbol, timeframe, required_lookback)
        _candle_cache[key] = df
        return df
    else:
        new_df = fetch_live_mt5_candles(symbol, timeframe, 10)
        if new_df is None or len(new_df) == 0:
            return _candle_cache[key]
        df = pd.concat([_candle_cache[key].reset_index(), new_df.reset_index()])
        df = df.drop_duplicates(subset=["datetime"], keep="last")
        df = df.sort_values("datetime")
        df = df.tail(required_lookback).set_index("datetime")
        _candle_cache[key] = df
        return df

def fetch_live_mt5_candles(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    try:
        url = f"{MT5_BRIDGE_URL}/market/candles/{symbol}?timeframe={timeframe}&count={count}"
        r = requests.get(url, headers={"X-Api-Key": MT5_API_KEY}, timeout=8)
        r.raise_for_status()
        data = r.json()
        
        candles = data.get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]).set_index("datetime")
            
        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
        if "tick_volume" in df.columns:
            df = df.rename(columns={"tick_volume": "volume"})
        elif "volume" not in df.columns:
            df["volume"] = 0
            
        df = df[["datetime", "open", "high", "low", "close", "volume"]]
        df = df.sort_values("datetime").reset_index(drop=True)
        return df.set_index("datetime")
    except Exception as e:
        log(f"Fetch error [{symbol} {timeframe}]: {e}")
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]).set_index("datetime")


# -----------------------------------------------------------------------------
# INDICATORS
# -----------------------------------------------------------------------------

def add_smooth_macd_cycles(df: pd.DataFrame) -> pd.DataFrame:
    _, _, hist_raw = talib.MACD(df["close"].values.astype(float), fastperiod=12, slowperiod=26, signalperiod=9)
    hist = hist_raw.copy(); plus_run = 0; minus_run = 0
    for i in range(len(hist)):
        if np.isnan(hist[i]): continue
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
    sign  = np.where(np.isnan(hist), 0.0, np.sign(hist))
    cycle = pd.Series(sign, index=df.index).diff().ne(0).cumsum().values.astype(int)
    color = np.where(sign > 0, "Green", np.where(sign < 0, "Red", "Flat"))
    df["sm_hist"] = hist; df["sm_sign"] = sign; df["sm_cycle"] = cycle; df["sm_color"] = color
    return df

def calc_emakama(close: np.ndarray, er_len=10, fast_len=2, slow_len=30) -> np.ndarray:
    n = len(close); kama = np.full(n, np.nan, dtype=float)
    fast_sc = 2.0 / (fast_len + 1); slow_sc = 2.0 / (slow_len + 1)
    if n == 0: return kama
    kama[0] = close[0]
    for i in range(1, n):
        if i < er_len: kama[i] = close[i]; continue
        change = abs(close[i] - close[i - er_len])
        vol = sum(abs(close[j] - close[j - 1]) for j in range(i - er_len + 1, i + 1))
        er  = change / vol if vol != 0 else 0.0
        sc  = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (close[i] - kama[i - 1])
    return kama

def calc_kama_line(src: np.ndarray, length=14, fast_length=2, slow_length=30, hp_period=48) -> np.ndarray:
    n = len(src); pi = 2.0 * np.arcsin(1.0)
    alpha1 = ((np.cos(.707*2*pi/hp_period) + np.sin(.707*2*pi/hp_period) - 1.0) / np.cos(.707*2*pi/hp_period))
    a1 = np.exp(-1.414*pi/10.0); b1 = 2.0*a1*np.cos(1.414*180.0/10.0)
    c2 = b1; c3 = -a1*a1; c1 = 1.0 - c2 - c3
    fastest = 2.0 / (fast_length + 1); slowest = 2.0 / (slow_length + 1)
    hp = np.zeros(n); filt = np.zeros(n); kama = np.zeros(n)
    corr_arr = np.zeros(length); r1_arr = np.zeros(length); r2_arr = np.zeros(length)
    for i in range(n):
        s0 = src[i]; s1 = src[i-1] if i >= 1 else 0.0; s2 = src[i-2] if i >= 2 else 0.0
        hp1 = hp[i-1] if i >= 1 else 0.0; hp2 = hp[i-2] if i >= 2 else 0.0
        hp[i] = (((1-alpha1/2)**2)*(s0-2*s1+s2)+2*(1-alpha1)*hp1-((1-alpha1)**2)*hp2)
        f1 = filt[i-1] if i >= 1 else 0.0; f2 = filt[i-2] if i >= 2 else 0.0
        hp_p = hp[i-1] if i >= 1 else 0.0
        filt[i] = c1*(hp[i]+hp_p)/2.0 + c2*f1 + c3*f2
        for lag in range(length):
            m = lag; sx = sy = sxx = syy = sxy = 0.0
            for count in range(m + 1):
                ix = i - count; iy = i - lag - count
                x = filt[ix] if ix >= 0 else 0.0; y = filt[iy] if iy >= 0 else 0.0
                sx += x; sy += y; sxx += x*x; sxy += x*y; syy += y*y
            d = (m*sxx - sx*sx) * (m*syy - sy*sy)
            if d > 0: corr_arr[lag] = (m*sxy - sx*sy) / np.sqrt(d)
        sq_sum = np.zeros(length)
        for period in range(8, length):
            cp_ = sp_ = 0.0
            for n2 in range(8, length):
                cp_ += corr_arr[n2]*np.cos(360.0*n2/period)
                sp_ += corr_arr[n2]*np.sin(360.0*n2/period)
            sq_sum[period] = cp_*cp_ + sp_*sp_
        for period2 in range(8, length):
            r2_arr[period2] = r1_arr[period2]
            r1_arr[period2] = 0.2*sq_sum[period2]**2 + 0.8*r2_arr[period2]
        max_pwr = max((r1_arr[p] for p in range(8, length)), default=0.0)
        pwr = np.zeros(length)
        for period4 in range(8, length):
            pwr[period4] = (r1_arr[period4] / max_pwr) if max_pwr != 0 else 0.0
        spx_ = sp__ = 0.0
        for period5 in range(8, length):
            if pwr[period5] >= 0.5: spx_ += period5*pwr[period5]; sp__ += pwr[period5]
        dc = spx_ / sp__ if sp__ != 0 else 0.0
        dc = max(8.0, min(14.0, dc)); dc_int = int(dc)
        idx_dc = i - dc_int; src_dc = src[idx_dc] if idx_dc >= 0 else 0.0
        num = abs(s0 - src_dc); denom = 0.0
        for j in range(dc_int):
            ij = i - j; ij1 = i - j - 1
            if ij >= 0 and ij1 >= 0: denom += abs(src[ij] - src[ij1])
        er = num / denom if denom != 0 else 0.0
        sc = (er*(fastest-slowest)+slowest)**2
        kprev = kama[i-1] if i > 0 else 0.0
        kama[i] = kprev + sc*(s0-kprev)
    return kama

def _pine_ema_series(series: pd.Series, length: int) -> pd.Series:
    alpha = 2.0 / (length + 1); out = np.zeros(len(series)); out[0] = series.iloc[0]
    for i in range(1, len(series)): out[i] = alpha * series.iloc[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)

def _heikin_ashi_df(df: pd.DataFrame) -> pd.DataFrame:
    ha = pd.DataFrame(index=df.index)
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = np.zeros(len(df)); ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)): ha_open[i] = (ha_open[i - 1] + ha_close.iloc[i - 1]) / 2.0
    ha["open"] = ha_open; ha["close"] = ha_close.values
    ha["high"] = np.maximum.reduce([df["high"].values, ha_open, ha_close.values])
    ha["low"]  = np.minimum.reduce([df["low"].values,  ha_open, ha_close.values])
    return ha

def _pine_kama_series(series: pd.Series, length=5, fast=2.5, slow=20) -> pd.Series:
    xvnoise = abs(series - series.shift(1)); nsignal = abs(series - series.shift(length))
    nnoise  = xvnoise.rolling(length).sum()
    nfast = 2.0 / (fast + 1); nslow = 2.0 / (slow + 1); kama = np.zeros(len(series))
    for i in range(len(series)):
        if i == 0: kama[0] = 0.0; continue
        sig = nsignal.iloc[i] if not np.isnan(nsignal.iloc[i]) else 0.0
        noi = nnoise.iloc[i]  if not np.isnan(nnoise.iloc[i])  else 0.0
        er  = sig / noi if noi != 0 else 0.0
        sc  = (er * (nfast - nslow) + nslow) ** 2
        kama[i] = kama[i - 1] + sc * (series.iloc[i] - kama[i - 1])
    return pd.Series(kama, index=series.index)

def calc_sma_kama(df: pd.DataFrame, length: int = 20) -> pd.Series:
    ha = _heikin_ashi_df(df)
    hlc3 = (ha["high"] + ha["low"] + ha["close"]) / 3.0
    return _pine_ema_series(_pine_kama_series(hlc3), length)

def prepare_15m_data(df_raw: pd.DataFrame) -> Dict[str, Any]:
    df = df_raw.copy(); close = df["close"].values.astype(float)
    df["rsi14"] = talib.RSI(close, timeperiod=14)
    df = add_smooth_macd_cycles(df)
    return {
        "idx": df.index, "open": df["open"].values.astype(float), "high": df["high"].values.astype(float),
        "low": df["low"].values.astype(float), "close": close, "rsi14": df["rsi14"].values.astype(float),
        "sm_cycle": df["sm_cycle"].values.astype(int), "sm_color": df["sm_color"].values.astype(object),
        "volume": df["volume"].values.astype(float),
    }

def prepare_3m_data(df_raw: pd.DataFrame) -> Dict[str, Any]:
    df = df_raw.copy()
    close = df["close"].values.astype(float); op = df["open"].values.astype(float)
    hi = df["high"].values.astype(float); lo = df["low"].values.astype(float)
    ohlc4 = (op + hi + lo + close) / 4.0
    df["ema50"] = talib.EMA(close, timeperiod=50); df["rsi14"] = talib.RSI(close, timeperiod=14)
    df = add_smooth_macd_cycles(df)
    df["emakama"] = pd.Series(calc_emakama(close), index=df.index)
    df["kama_line"] = pd.Series(calc_kama_line(ohlc4), index=df.index)
    df["sma_kama"] = calc_sma_kama(df, length=20)
    df["bb_mid"] = talib.SMA(close, timeperiod=20)
    return {
        "idx": df.index, "open": op, "high": hi, "low": lo, "close": close,
        "ema50": df["ema50"].values.astype(float), "rsi14": df["rsi14"].values.astype(float),
        "sm_cycle": df["sm_cycle"].values.astype(int), "sm_color": df["sm_color"].values.astype(object),
        "emakama": df["emakama"].values.astype(float), "kama_line": df["kama_line"].values.astype(float),
        "sma_kama": df["sma_kama"].values.astype(float), "bb_mid": df["bb_mid"].values.astype(float),
    }

def prepare_1m_data(df_raw: pd.DataFrame) -> Dict[str, Any]:
    return {
        "idx": df_raw.index, "open": df_raw["open"].values.astype(float), "high": df_raw["high"].values.astype(float),
        "low": df_raw["low"].values.astype(float), "close": df_raw["close"].values.astype(float),
    }

FIB_LEVELS_RAW = [
    (0.0, "0"), (0.236, "0.236"), (0.382, "0.382"), (0.5, "0.5"), (0.618, "0.618"),
    (0.786, "0.786"), (1.0, "1"), (1.272, "1.272"), (1.414, "1.414"), (1.618, "1.618"),
    (2.0, "2"), (2.618, "2.618")
]

def get_fib_levels(hh: float, ll: float, side: str) -> List[Tuple[float, str]]:
    rng = hh - ll; levels = []
    for k, name in FIB_LEVELS_RAW:
        price = (ll + k * rng) if side == "sell" else (hh - k * rng)
        levels.append((price, name))
    if side == "sell": levels.sort(key=lambda x: x[0])
    else: levels.sort(key=lambda x: x[0], reverse=True)
    return levels

def scan_15m_setups(arr15: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    setups = []
    idx = arr15["idx"]; op = arr15["open"]; hi = arr15["high"]; lo = arr15["low"]; cl = arr15["close"]
    for i in range(2, len(idx)):
        if side == "sell":
            wicks_ok = (hi[i-2] > max(op[i-2], cl[i-2]) and hi[i-1] > max(op[i-1], cl[i-1]) and hi[i] > max(op[i], cl[i]))
            if not wicks_ok: continue
            h_line = min(hi[i-2], hi[i-1], hi[i])
            body_ok = (max(op[i-2], cl[i-2]) <= h_line and max(op[i-1], cl[i-1]) <= h_line and max(op[i], cl[i]) <= h_line)
            if not body_ok: continue
            setup_extreme_close = max(cl[i-2], cl[i-1], cl[i])
        else:
            wicks_ok = (lo[i-2] < min(op[i-2], cl[i-2]) and lo[i-1] < min(op[i-1], cl[i-1]) and lo[i] < min(op[i], cl[i]))
            if not wicks_ok: continue
            h_line = max(lo[i-2], lo[i-1], lo[i])
            body_ok = (min(op[i-2], cl[i-2]) >= h_line and min(op[i-1], cl[i-1]) >= h_line and min(op[i], cl[i]) >= h_line)
            if not body_ok: continue
            setup_extreme_close = min(cl[i-2], cl[i-1], cl[i])
        setups.append({
            "start_dt": idx[i-2], "end_dt": idx[i], "extreme_close": setup_extreme_close
        })
    return setups

def compute_hard_sl_on_tf(arr: Dict[str, Any], entry_ts: pd.Timestamp, side: str) -> Dict[str, Any]:
    idx = arr["idx"]
    pos = idx.searchsorted(entry_ts, side="right") - 1
    if pos < 1: return {"ok": False}
    rsi = arr["rsi14"]; cycles = arr["sm_cycle"]; hi = arr["high"]; lo = arr["low"]; op = arr["open"]; cl = arr["close"]

    event1_pos = None
    for p in range(pos, -1, -1):
        if not np.isnan(rsi[p]):
            if side == "sell" and rsi[p] < 30: event1_pos = p; break
            elif side == "buy" and rsi[p] > 70: event1_pos = p; break
    if event1_pos is None: return {"ok": False}

    event2_pos = None
    for p in range(event1_pos, -1, -1):
        if not np.isnan(rsi[p]):
            if side == "sell" and rsi[p] > 70: event2_pos = p; break
            elif side == "buy" and rsi[p] < 30: event2_pos = p; break
    if event2_pos is None: return {"ok": False}

    cid = cycles[event2_pos]
    cyc_mask = np.where(cycles == cid)[0]
    cyc_start_pos, cyc_end_pos = int(cyc_mask[0]), int(cyc_mask[-1])

    if side == "sell":
        rel = int(np.argmax(hi[cyc_start_pos:cyc_end_pos+1]))
        ext1_pos = cyc_start_pos + rel
        hh_val = float(hi[ext1_pos])
        sub_lo = lo[ext1_pos:pos+1]
        rel_sub = int(np.argmin(sub_lo))
        ext2_pos = ext1_pos + rel_sub
        ll_val = float(lo[ext2_pos])
    else:
        rel = int(np.argmin(lo[cyc_start_pos:cyc_end_pos+1]))
        ext1_pos = cyc_start_pos + rel
        ll_val = float(lo[ext1_pos])
        sub_hi = hi[ext1_pos:pos+1]
        rel_sub = int(np.argmax(sub_hi))
        ext2_pos = ext1_pos + rel_sub
        hh_val = float(hi[ext2_pos])

    fib_levels = get_fib_levels(hh_val, ll_val, side)
    cut_dt = cut_level_name = cut_level_val = cut_idx_in_levels = None

    for p in range(pos, ext1_pos - 1, -1):
        cand_min, cand_max = min(op[p], cl[p]), max(op[p], cl[p])
        if op[p] == cl[p]: continue
        for i_lvl, (lvl_price, lvl_name) in enumerate(fib_levels):
            if cand_min <= lvl_price <= cand_max:
                cut_dt = idx[p]; cut_level_name = lvl_name
                cut_level_val = lvl_price; cut_idx_in_levels = i_lvl
                break
        if cut_dt is not None: break
    if cut_dt is None: return {"ok": False}

    is_topmost = "yes" if cut_idx_in_levels == (len(fib_levels) - 1) else "no"
    if is_topmost == "yes":
        upper_val = None; sl_val = cut_level_val
    else:
        next_price, next_name = fib_levels[cut_idx_in_levels + 1]
        upper_val = next_price; sl_val = next_price

    return {"ok": True, "sl_val": sl_val}

def track_stage2_exit(arr1: Dict[str, Any], start_pos: int, initial_sl_price: float, side: str) -> Dict[str, Any]:
    idx = arr1["idx"]; op = arr1["open"]; cl = arr1["close"]; hi = arr1["high"]; lo = arr1["low"]
    end_p = len(idx)
    stage1_close_pos = None; most_updated_sl = initial_sl_price
    for p in range(start_pos, end_p):
        if side == "sell":
            if hi[p] > most_updated_sl and cl[p] <= most_updated_sl: most_updated_sl = hi[p]
            elif cl[p] > most_updated_sl: stage1_close_pos = p; break
        else:
            if lo[p] < most_updated_sl and cl[p] >= most_updated_sl: most_updated_sl = lo[p]
            elif cl[p] < most_updated_sl: stage1_close_pos = p; break
    if stage1_close_pos is None: return {"exit_found": False}
    red1_pos = None
    for p in range(stage1_close_pos + 1, end_p):
        if side == "sell" and cl[p] < op[p]: red1_pos = p; break
        elif side == "buy" and cl[p] > op[p]: red1_pos = p; break
    if red1_pos is None: return {"exit_found": False}
    if side == "sell": h1_val = float(np.max(hi[stage1_close_pos:red1_pos+1]))
    else: h1_val = float(np.min(lo[stage1_close_pos:red1_pos+1]))
    step1_final_pos = None; upd_h1 = h1_val
    for p in range(red1_pos + 1, end_p):
        if side == "sell":
            if hi[p] > upd_h1 and cl[p] <= upd_h1: upd_h1 = hi[p]
            elif cl[p] > upd_h1: step1_final_pos = p; break
        else:
            if lo[p] < upd_h1 and cl[p] >= upd_h1: upd_h1 = lo[p]
            elif cl[p] < upd_h1: step1_final_pos = p; break
    if step1_final_pos is None: return {"exit_found": False}
    red2_pos = None
    for p in range(step1_final_pos + 1, end_p):
        if side == "sell" and cl[p] < op[p]: red2_pos = p; break
        elif side == "buy" and cl[p] > op[p]: red2_pos = p; break
    if red2_pos is None: return {"exit_found": False}
    if side == "sell": h2_val = float(np.max(hi[step1_final_pos:red2_pos+1]))
    else: h2_val = float(np.min(lo[step1_final_pos:red2_pos+1]))
    step2_final_pos = None; upd_h2 = h2_val
    for p in range(red2_pos + 1, end_p):
        if side == "sell":
            if hi[p] > upd_h2 and cl[p] <= upd_h2: upd_h2 = hi[p]
            elif cl[p] > upd_h2: step2_final_pos = p; break
        else:
            if lo[p] < upd_h2 and cl[p] >= upd_h2: upd_h2 = lo[p]
            elif cl[p] < upd_h2: step2_final_pos = p; break
    if step2_final_pos is None: return {"exit_found": False}
    return {"exit_found": True, "exit_dt": idx[step2_final_pos], "exit_price": cl[step2_final_pos]}

def compute_soft_sl(arr3: Dict[str, Any], arr1: Dict[str, Any], entry_3m_pos: int, side: str) -> Dict[str, Any]:
    idx3 = arr3["idx"]; cycles3 = arr3["sm_cycle"]; colors3 = arr3["sm_color"]
    entry_cid = cycles3[entry_3m_pos]; target_cid = entry_cid + 2
    trig_pos_mask = np.where(cycles3 == target_cid)[0]
    if len(trig_pos_mask) < 4: return {"ok": False}
    trigger_pos = int(trig_pos_mask[3]); trigger_dt = idx3[trigger_pos]

    req_color = "Green" if side == "sell" else "Red"
    found_cycles = []; seen_cids = set()
    for p in range(trigger_pos, -1, -1):
        cid = cycles3[p]
        if cid in seen_cids: continue
        seen_cids.add(cid)
        if colors3[p] == req_color:
            cyc_bars = np.where(cycles3 == cid)[0]
            if len(cyc_bars) >= 15:
                found_cycles.append((int(cyc_bars[0]), int(cyc_bars[-1])))
                if len(found_cycles) == 2: break
    if len(found_cycles) < 2: return {"ok": False}

    cyc1_start, cyc1_end = found_cycles[0]
    cyc2_start, cyc2_end = found_cycles[1]
    zone_start_pos, zone_end_pos = cyc2_start, cyc1_end

    v_emakama = arr3["emakama"][zone_start_pos:zone_end_pos+1]
    v_kline = arr3["kama_line"][zone_start_pos:zone_end_pos+1]
    v_sma = arr3["sma_kama"][zone_start_pos:zone_end_pos+1]
    v_bb = arr3["bb_mid"][zone_start_pos:zone_end_pos+1]
    v_ema50 = arr3["ema50"][zone_start_pos:zone_end_pos+1]
    
    if side == "sell":
        max_emakama = float(np.nanmax(v_emakama))
        max_kline = float(np.nanmax(v_kline))
        max_sma = float(np.nanmax(v_sma))
        max_bb = float(np.nanmax(v_bb))
        max_ema50 = float(np.nanmax(v_ema50))
        zone_vals = {"emakama": max_emakama, "kama_line": max_kline, "sma_kama": max_sma, "bb_mid": max_bb, "ema50": max_ema50}
        best_key = max(zone_vals, key=zone_vals.get)
    else:
        max_emakama = float(np.nanmin(v_emakama))
        max_kline = float(np.nanmin(v_kline))
        max_sma = float(np.nanmin(v_sma))
        max_bb = float(np.nanmin(v_bb))
        max_ema50 = float(np.nanmin(v_ema50))
        zone_vals = {"emakama": max_emakama, "kama_line": max_kline, "sma_kama": max_sma, "bb_mid": max_bb, "ema50": max_ema50}
        best_key = min(zone_vals, key=zone_vals.get)
        
    init_sl_val = zone_vals[best_key]

    map_1m_dt = trigger_dt + timedelta(minutes=3)
    pos_1m = arr1["idx"].searchsorted(map_1m_dt, side="left")
    if pos_1m >= len(arr1["idx"]): return {"ok": False}
    s2_res = track_stage2_exit(arr1, pos_1m, init_sl_val, side)

    return {"ok": True, "stage2": s2_res}

def _tgt(entry_price, hard_sl, side, ratio):
    points = abs(entry_price - hard_sl)
    return entry_price - (ratio * points) if side == "sell" else entry_price + (ratio * points)

def run_backtest_engine(arr1: Dict[str, Any], entry_pos_1m: int, entry_price: float, adj_sl_points: float, hard_sl_price: float, hard_sl_exit_dt: Optional[pd.Timestamp], soft_sl_exit_dt: Optional[pd.Timestamp], side: str) -> Tuple[Dict[float, Dict[str, Any]], str]:
    idx1 = arr1["idx"]; hi = arr1["high"]; lo = arr1["low"]; cl = arr1["close"]
    qty = RISK_PER_TRADE / adj_sl_points if adj_sl_points > 0 else 0.0

    targets = {}; init_sls = {}
    for r in ALL_RATIOS:
        if side == "sell":
            targets[r] = entry_price - r * adj_sl_points
            init_sls[r] = entry_price + adj_sl_points
        else:
            targets[r] = entry_price + r * adj_sl_points
            init_sls[r] = entry_price - adj_sl_points

    cur_sl = dict(init_sls); sl_label = {r: "Hard SL" for r in ALL_RATIOS}
    done = {r: False for r in ALL_RATIOS}; results = {r: {} for r in ALL_RATIOS}

    def record(r, status, px, ts, due_sl=None):
        holding_hrs = round((ts - idx1[entry_pos_1m]).total_seconds() / 3600.0, 4)
        pnl = (entry_price - px) * qty if side == "sell" else (px - entry_price) * qty
        results[r] = {
            "sl_percent": round(adj_sl_points / entry_price * 100.0, 4),
            "target_percent": round(r * adj_sl_points / entry_price * 100.0, 4),
            "sl_price": _f(init_sls[r]), "target_price": _f(targets[r]),
            "new_sl_price": _f(cur_sl[r]), "sl_hit_dt": _s(ts) if due_sl else None,
            "exit_status": status, "sl_hit_due_to": due_sl, "exit_price": _f(px),
            "exit_datetime": _s(ts), "holding_hours": holding_hrs, "qty": _f(qty), "pnl": _f(pnl)
        }
        done[r] = True

    for p in range(entry_pos_1m + 1, len(idx1)):
        ts = idx1[p]; h = hi[p]; l = lo[p]; c = cl[p]
        is_hard_hit = (hard_sl_exit_dt is not None and ts >= hard_sl_exit_dt)
        is_soft_hit = (soft_sl_exit_dt is not None and ts >= soft_sl_exit_dt)

        for r in ALL_RATIOS:
            if done[r]: continue
            hit_target = (l <= targets[r]) if side == "sell" else (h >= targets[r])
            if hit_target:
                record(r, "Target Hit", targets[r], ts, due_sl=None)
                if r >= 2:
                    anchor_px = entry_price if r == 2 else targets[r - 2]
                    anchor_lbl = "Breakeven" if r == 2 else f"1:{int(r - 2)} Target"
                    for r_higher in ALL_RATIOS:
                        if r_higher >= r and not done[r_higher]:
                            cur_sl[r_higher] = anchor_px; sl_label[r_higher] = anchor_lbl
                continue

            hit_sl = (c >= cur_sl[r]) if side == "sell" else (c <= cur_sl[r])
            if hit_sl:
                if sl_label[r] == "Hard SL":
                    if is_soft_hit and (not is_hard_hit or soft_sl_exit_dt <= hard_sl_exit_dt):
                        record(r, "Soft SL hit", c, ts, due_sl="Soft SL")
                    else:
                        record(r, "Hard SL hit", cur_sl[r], ts, due_sl="Hard SL")
                else:
                    record(r, f"Trailed SL hit - {sl_label[r]}", cur_sl[r], ts, due_sl=sl_label[r])
                continue
            elif is_hard_hit and sl_label[r] == "Hard SL":
                record(r, "Hard SL hit", hard_sl_price, ts, due_sl="Hard SL")
                continue
            elif is_soft_hit and sl_label[r] == "Hard SL":
                record(r, "Soft SL hit", c, ts, due_sl="Soft SL")
                continue
        if all(done.values()): break

    for r in ALL_RATIOS:
        if not done[r]: record(r, "Open", cl[-1], idx1[-1])

    return results, ""

# -----------------------------------------------------------------------------
# ENGINE
# -----------------------------------------------------------------------------

_fired_events = set()
_ticket_map = {}
_event_to_ticket: dict = {}

def load_fired_events():
    global _fired_events
    for p in [_BUY_LOG_PATH, _SELL_LOG_PATH]:
        if p.exists():
            try:
                df = pd.read_csv(p, dtype=str)
                if "Event ID" in df.columns: _fired_events.update(df["Event ID"].dropna().tolist())
            except: pass

def save_fired_events():
    try:
        import json
        with open(BASE_LOG_DIR / "_fired_events.json", "w") as f: json.dump(list(_fired_events), f)
    except: pass

def tg_post(text: str):
    try: requests.post(TELEGRAM_URL, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    except: pass

def mt5_bridge_trade(symbol: str, action_type: int, volume: float, sl: float = 0.0):
    try:
        payload = {"action": 1, "symbol": symbol, "volume": float(volume), "type": action_type, "price": 0.0, "sl": float(sl), "magic": MAGIC, "comment": "S21 Bridge"}
        r = requests.post(f"{MT5_BRIDGE_URL}/trade", json=payload, headers={"X-Api-Key": MT5_API_KEY}, timeout=20)
        j = r.json()
        return (int(j.get("order_id", 0)), int(j.get("result", 0)), j.get("comment", ""))
    except Exception as e: return (0, 0, str(e))

def mt5_bridge_modify_sl(ticket: int, new_sl: float):
    try:
        r = requests.post(f"{MT5_BRIDGE_URL}/modify", json={"ticket": int(ticket), "sl": float(new_sl)}, headers={"X-Api-Key": MT5_API_KEY}, timeout=20)
        return r.status_code == 200 and int(r.json().get("result", 0)) == 10009
    except: return False

def mt5_bridge_close_ticket(ticket: int):
    try:
        r = requests.post(f"{MT5_BRIDGE_URL}/close", json={"ticket": int(ticket)}, headers={"X-Api-Key": MT5_API_KEY}, timeout=20)
        return r.status_code == 200 and int(r.json().get("result", 0)) == 10009
    except: return False

def market_close_flatten_due(inst_symbol: str):
    _MC = {"BTCUSD": {"daily_break": False, "weekend": False}}
    s = _MC.get(inst_symbol)
    if not s: return False, ""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    mins = now.hour * 60 + now.minute
    if not (17 * 60 - FLATTEN_LEAD_MIN <= mins < 17 * 60): return False, ""
    if now.weekday() == 4 and s["weekend"] and FLATTEN_BEFORE_WEEKEND: return True, "weekend_close"
    if now.weekday() < 4 and s["daily_break"] and FLATTEN_BEFORE_DAILY_BREAK: return True, "daily_break"
    return False, ""

def flatten_for_market_close(positions: list, inst_symbol: str):
    due, why = market_close_flatten_due(inst_symbol)
    if not due: return set()
    closed = set()
    for p in positions:
        ticket = int(p.get("ticket", 0) or 0)
        rec = _ticket_map.get(ticket)
        if not rec or rec.get("symbol") != inst_symbol or ticket <= 0: continue
        if mt5_bridge_close_ticket(ticket):
            pnl = float(p.get("profit") or 0)
            trade_db.record_close(ticket, reason=f"flatten_{why}", pnl=pnl)
            rec["closed_notified"] = True
            tg_post(f"\\U0001F6D1 MARKET-CLOSE FLATTEN\\nTicket: {ticket}\\nSymbol: {inst_symbol}\\nReason: {why}\\nFloating P/L at close: {pnl}")
            closed.add(ticket)
    return closed

def trail_conservative_positions(positions: list, current_px: float, inst_symbol: str, low_px: float = None, high_px: float = None):
    if not positions or not current_px or current_px <= 0: return
    def _sane(v): return v is not None and float(v) > 0 and abs(float(v) - current_px) / current_px <= 0.20
    if low_px is not None and not _sane(low_px): low_px = None
    if high_px is not None and not _sane(high_px): high_px = None
    for p in positions:
        ticket = int(p.get("ticket", 0)); magic = int(p.get("magic", 0)); p_side = int(p.get("type", 0))
        if magic != MAGIC or ticket <= 0 or ticket not in _ticket_map: continue
        rec = _ticket_map[ticket]
        if rec.get("symbol") != inst_symbol: continue
        _open_px = float(p.get("price_open") or 0)
        if _open_px > 0 and abs(current_px - _open_px) / _open_px > 0.5: continue
        targets = rec.get("targets", {})
        cur_sl = float(p.get("sl", 0))
        anchor = rec.get("current_sl", rec.get("entry_price", 0.0))
        for cp in TRAIL_CPS:
            if cp in rec["trail_hit"]: continue
            tgt = targets.get(cp)
            if tgt is None: continue
            _hit_px = (low_px if low_px is not None else current_px) if p_side == 1 else (high_px if high_px is not None else current_px)
            hit = (_hit_px <= tgt) if p_side == 1 else (_hit_px >= tgt)
            if not hit: continue
            rec["trail_hit"].add(cp)
            anchor = rec["entry_price"] if cp == 2 else targets.get(cp - 2, anchor)
            if anchor is None: continue
            if p_side == 1:
                if cur_sl > 0 and anchor >= cur_sl: continue
            else:
                if cur_sl > 0 and anchor <= cur_sl: continue
            new_sl = round(float(anchor), 6)
            _mod_ok = mt5_bridge_modify_sl(ticket, new_sl)
            trade_db.record_trail(ticket, new_sl, cp, executed=bool(_mod_ok))
            if not _mod_ok:
                rec["trail_hit"].discard(cp)
                continue
            cur_sl = new_sl
            tg_post(f"📐 SL TRAILED\\nTicket: {ticket}\\nNew SL: {new_sl}\\nAnchor: 1:{cp} target hit at {tgt}\\nCurrent price: {current_px}")
        rec["current_sl"] = cur_sl

def format_telegram(r: dict, side: str, order_id: int = 0) -> str:
    color = "🟢 BUY" if side == "buy" else "🔴 SELL"
    msg = (
        f"{color} {SYMBOL}\\n"
        f"Asset Name: {SYMBOL}\\n"
        f"Strategy: Strategy 21 Live\\n"
        f"Trade Entry Id: {r.get('Event ID','')}\\n"
        f"15min Setup Starttime: {r.get('15min Setup Starttime','')}\\n"
        f"15min Setup Endtime: {r.get('15min Setup Endtime','')}\\n"
        f"15min candle mapped to 3min is: {r.get('15min candle mapped to 3min is','')}\\n"
        f"Previous 3 minute candle open and close {'below' if side=='buy' else 'above'} EMA 50: {r.get('Previous 3 minute candle open and close below EMA 50' if side=='buy' else 'Previous 3 minute candle open and close above EMA 50','')}\\n"
        f"Candle closing {'above Lowest' if side=='buy' else 'below Highest'} close Value: {r.get('Candle closing above Lowest close Value' if side=='buy' else 'Candle closing below Highest close Value','')}\\n"
        f"Status: {r.get('Status','')}\\n"
        f"Entry Datetime: {r.get('Entry Datetime','')}\\n"
        f"Entry Price: {_f(r.get('Entry Price',0.0))}\\n"
        f"Hard SL obtained from: {r.get('Hard SL obtained from','')}\\n"
        f"Hard SL Value: {_f(r.get('Hard SL Value',0.0))}\\n"
        f"Hard SL Percentage: {_f(r.get('Hard SL Percentage',0.0))}\\n"
        f"SL Value Consider for Qty: {_f(r.get('SL Value Consider for Qty',0.0))}\\n"
        f"SL Value Percentage consider for Qty: {_f(r.get('SL Value Percentage consider for Qty',0.0))}\\n"
        f"Qty: {_f(r.get('Qty',0.0))}\\n"
    )
    if order_id > 0:
        msg += f"\\n✅ MT5 Execution: SUCCESS (Order ID: {order_id})"
    return msg

def format_telegram_exit(r, side, ratio_str, msg_type, event_type, exit_price, exit_dt, pnl, new_trail_applied, new_trail_at):
    return (
        f"Asset: {SYMBOL} (Theoretical)\\n"
        f"Strategy: Strategy 21 Live\\n"
        f"Entry Signal: {side}\\n"
        f"Trade Entry Id: {r.get('Event ID','')}\\n"
        f"Final Entry Datetime: {r.get('Entry Datetime','')}\\n"
        f"Hard SL Price: {_f(r.get('Hard SL Value',0.0))}\\n"
        f"Ratio: {ratio_str}\\n"
        f"Message Type: {msg_type}\\n"
        f"New Trailed SL Applied: {new_trail_applied}\\n"
        f"New Trail SL at: {new_trail_at}\\n"
        f"Event Occured Type: {event_type}\\n"
        f"Exit Price: {_f(exit_price)}\\n"
        f"Exit Datetime: {exit_dt}\\n"
        f"PNL: {_f(pnl)}\\n\\n"
        f"⚠️ Note: This is a theoretical result based on price simulation, not a live MT5 execution."
    )

def process_telegram_exits(row: dict, side: str, recent_1m: pd.DatetimeIndex):
    global _fired_events
    if row.get("Status") != "Intrade": return
    ev_id = row.get("Event ID")
    for r in ALL_RATIOS:
        lbl = "1:0.5" if r == 0.5 else f"1:{r}"
        exit_dt_str = str(row.get(f"{lbl} Exit Datetime", "None"))
        exit_price = str(row.get(f"{lbl} Exit Price", ""))
        pnl = str(row.get(f"P/L {lbl}", ""))
        status = str(row.get(f"Status {lbl}", "None")) if r >= 2 else "None"
        due = str(row.get(f"{lbl} SL hit Due to", "None"))
        if exit_dt_str == "None" or exit_dt_str == "nan": continue
        try: exit_ts = pd.Timestamp(exit_dt_str)
        except: continue

        is_target = (r >= 2 and status == "Target Hit") or (r == 0.5 and due == "None")
        if is_target:
            msg_ev = f"{ev_id}_TGT_{r}"
            if msg_ev not in _fired_events and exit_ts in recent_1m:
                _fired_events.add(msg_ev)
                save_fired_events()
                nt_app = "yes" if 2 <= r <= 9 else "no"
                nt_at = "Entry Price (Breakeven)" if r == 2 else (f"1:{r-2} target" if r > 2 else "N/A")
                msg = format_telegram_exit(row, side, f"1:{r}", "Target Hit (Theoretical Simulation)", f"1:{r} target Hit", exit_price, exit_dt_str, pnl, nt_app, nt_at)
                tg_post(msg)

    sl_groups = {}
    for r in ALL_RATIOS:
        lbl = "1:0.5" if r == 0.5 else f"1:{r}"
        exit_dt_str = str(row.get(f"{lbl} Exit Datetime", "None"))
        due = str(row.get(f"{lbl} SL hit Due to", "None"))
        exit_price = str(row.get(f"{lbl} Exit Price", ""))
        status = str(row.get(f"Status {lbl}", "None")) if r >= 2 else "None"
        if exit_dt_str == "None" or exit_dt_str == "nan": continue
        if (r >= 2 and "SL" in status) or (r == 0.5 and due != "None" and due != "nan"):
            sl_groups.setdefault((exit_dt_str, due, exit_price), []).append(r)
    
    for (exit_dt_str, due, exit_price), r_list in sl_groups.items():
        try: exit_ts = pd.Timestamp(exit_dt_str)
        except: continue
        msg_ev = f"{ev_id}_SL_GRP_{exit_dt_str}_{due}"
        if msg_ev not in _fired_events and exit_ts in recent_1m:
            _fired_events.add(msg_ev)
            save_fired_events()
            r_str = f"1:{r_list[0]}" if len(r_list) == 1 else f"1:{r_list[0]} to 1:{r_list[-1]}"
            if "Target" in due:
                m_type = f"Trailed SL hit - {due} (Theoretical Simulation)"
                e_type = f"SL hit - {due} from {r_str} ratios"
            else:
                m_type = f"{due} hit (Theoretical Simulation)"
                e_type = f"{due} hit from {r_str} ratios"
            
            tot_pnl = sum(float(str(row.get(f"P/L {'1:0.5' if r_==0.5 else f'1:{r_}'}", "0")) or "0") for r_ in r_list)
            msg = format_telegram_exit(row, side, r_str, m_type, e_type, exit_price, exit_dt_str, tot_pnl, "no", "N/A")
            tg_post(msg)

def run_strategy21(arr15, arr3, arr1, side, recent_1m):
    setups = scan_15m_setups(arr15, side)
    idx3, idx1 = arr3["idx"], arr1["idx"]
    setup_entries = []; entry_dt_to_max_start = {}

    for s in setups:
        end_15m_dt = s["end_dt"]; map_3m_dt = end_15m_dt + timedelta(minutes=15)
        pos3 = idx3.searchsorted(map_3m_dt, side="left")
        if pos3 >= len(idx3) or pos3 < 3: setup_entries.append(None); continue
        
        prev3_ok = True
        for p in range(pos3 - 3, pos3):
            op3, cl3, ema3 = arr3["open"][p], arr3["close"][p], arr3["ema50"][p]
            if side == "sell" and not (op3 > ema3 and cl3 > ema3): prev3_ok = False; break
            if side == "buy" and not (op3 < ema3 and cl3 < ema3): prev3_ok = False; break
        if not prev3_ok: setup_entries.append(None); continue
        
        entry_3m_pos = None
        for p in range(pos3, len(idx3)):
            cl3, ema3 = arr3["close"][p], arr3["ema50"][p]
            if side == "sell" and cl3 < ema3 and cl3 < s["extreme_close"]: entry_3m_pos = p; break
            if side == "buy" and cl3 > ema3 and cl3 > s["extreme_close"]: entry_3m_pos = p; break
        if entry_3m_pos is None: setup_entries.append(None); continue
        
        entry_dt = idx3[entry_3m_pos]; entry_price = float(arr3["close"][entry_3m_pos])
        setup_entries.append((entry_3m_pos, entry_dt, entry_price, map_3m_dt))
        if entry_dt not in entry_dt_to_max_start or s["start_dt"] > entry_dt_to_max_start[entry_dt]:
            entry_dt_to_max_start[entry_dt] = s["start_dt"]

    rows = []
    for i, s in enumerate(setups):
        row = OrderedDict()
        row["15min Setup Starttime"] = _s(s["start_dt"])
        row["15min Setup Endtime"] = _s(s["end_dt"])
        map_3m_dt = s["end_dt"] + timedelta(minutes=15)
        row["15min candle mapped to 3min is"] = _s(map_3m_dt)
        entry_info = setup_entries[i]
        
        if entry_info is None: continue
        entry_3m_pos, entry_dt, entry_price, map_3m_dt = entry_info
        
        if s["start_dt"] < entry_dt_to_max_start[entry_dt]: continue

        row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "yes"
        row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "yes"
        row["Entry Datetime"] = _s(entry_dt)
        row["Entry Price"] = entry_price
        
        sl3 = compute_hard_sl_on_tf(arr3, entry_dt, side)
        sl15 = compute_hard_sl_on_tf(arr15, entry_dt, side)
        if not sl3.get("ok") or not sl15.get("ok"): continue
        
        sl3_val, sl15_val = sl3["sl_val"], sl15["sl_val"]
        if side == "sell":
            hard_sl_from = "15min" if sl15_val >= sl3_val else "3min"
            hard_sl_price = max(sl3_val, sl15_val)
        else:
            hard_sl_from = "15min" if sl15_val <= sl3_val else "3min"
            hard_sl_price = min(sl3_val, sl15_val)
            
        sl_points = abs(entry_price - hard_sl_price)
        hard_sl_pct = (sl_points / entry_price * 100.0) if entry_price > 0 else 0.0
        
        # Hard SL Invalidation Check
        if side == "sell" and hard_sl_pct >= 1.5:
            row["Status"] = "Invalidated due to Hard SL % is Greater than or equal to 1.5 percentage"
            row["Hard SL Percentage"] = hard_sl_pct
            row["Event ID"] = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            rows.append(row); continue
        if side == "buy" and hard_sl_pct <= 1.0:
            row["Status"] = "Invalidated due to Hard SL % is Less than or equal to 1 percentage"
            row["Hard SL Percentage"] = hard_sl_pct
            row["Event ID"] = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            rows.append(row); continue

        row["Status"] = "Intrade"
        adj_sl_points = sl_points * 1.30
        sl_price_for_qty = entry_price + adj_sl_points if side == "sell" else entry_price - adj_sl_points
        sl_pct_for_qty = (adj_sl_points / entry_price * 100.0) if entry_price > 0 else 0.0
        qty = RISK_PER_TRADE / adj_sl_points if adj_sl_points > 0 else 0.0
        
        row["Hard SL obtained from"] = hard_sl_from
        row["Hard SL Value"] = hard_sl_price
        row["Hard SL Percentage"] = hard_sl_pct
        row["SL Value Consider for Qty"] = sl_price_for_qty
        row["SL Value Percentage consider for Qty"] = sl_pct_for_qty
        row["Qty"] = max(round(qty, 2), 0.01)
        
        pos_1m = idx1.searchsorted(entry_dt, side="left")
        hsl_stage2 = track_stage2_exit(arr1, pos_1m, hard_sl_price, side)
        hard_sl_exit_dt = hsl_stage2["exit_dt"] if hsl_stage2.get("exit_found") else None
        
        ssl = compute_soft_sl(arr3, arr1, entry_3m_pos, side)
        soft_sl_exit_dt = ssl["stage2"]["exit_dt"] if ssl.get("ok") and ssl["stage2"].get("exit_found") else None
        
        bt_results, _ = run_backtest_engine(arr1, pos_1m, entry_price, adj_sl_points, hard_sl_price, hard_sl_exit_dt, soft_sl_exit_dt, side)
        for r in ALL_RATIOS:
            lbl = f"1:{r}" if r != 0.5 else "1:0.5"
            res = bt_results[r]
            if r >= 2: row[f"Status {lbl}"] = res.get("exit_status")
            row[f"{lbl} Exit Datetime"] = res.get("exit_datetime")
            row[f"{lbl} Exit Price"] = res.get("exit_price")
            row[f"{lbl} SL hit Due to"] = res.get("sl_hit_due_to")
            row[f"{lbl} Holding Time (hrs)"] = res.get("holding_hours")
            row[f"P/L {lbl}"] = res.get("pnl")

        ev_id = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
        row["Event ID"] = ev_id
        row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        row["Live MT5 Order ID"] = None
        row["Live Entry Price"] = None
        row["Live Corrected Hard SL"] = None
        row["Executed Result"] = None
        row["Executed Qty"] = None
        row["Executed Investment Value"] = None
        row["Live Exit Datetime"] = None

        is_new_entry = entry_dt in recent_1m
        if is_new_entry and ev_id not in _fired_events:
            _fired_events.add(ev_id)
            save_fired_events()
            
            action_type = 0 if side == "buy" else 1
            order_id, retcode, _ = mt5_bridge_trade(SYMBOL, action_type, row["Qty"], hard_sl_price)
            if order_id > 0 and retcode == 10009:
                targets = {r: _tgt(entry_price, hard_sl_price, side, r) for r in ALL_RATIOS}
                _ticket_map[order_id] = {
                    "symbol": SYMBOL, "event_id": ev_id, "side": side,
                    "entry_price": entry_price, "hard_sl": hard_sl_price,
                    "targets": targets, "current_sl": hard_sl_price, "trail_hit": set()
                }
                _event_to_ticket[ev_id] = order_id
                trade_db.record_execution(ev_id, order_id, retcode, entry_price=entry_price, qty=row["Qty"], hard_sl=hard_sl_price, targets=targets)
            
            msg = format_telegram(row, side, order_id)
            tg_post(msg)
            trade_db.mark_telegram_sent(ev_id)

        if ev_id in _event_to_ticket:
            ticket = _event_to_ticket[ev_id]
            rec = _ticket_map.get(ticket)
            if rec:
                row["Live MT5 Order ID"] = ticket
                row["Live Entry Price"] = rec.get("entry_price", "")
                row["Live Corrected Hard SL"] = rec.get("hard_sl", "")
                row["Live Exit Datetime"] = rec.get("exit_datetime", "")
            process_telegram_exits(row, side, recent_1m)

        rows.append(row)
    
    return rows

def run_live_scan():
    global _fired_events
    log("Fetching live candles...")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "M15": pool.submit(get_rolling_mt5_candles, SYMBOL, "M15", LOOKBACK_15M),
            "M3":  pool.submit(get_rolling_mt5_candles, SYMBOL, "M3",  LOOKBACK_3M),
            "M1":  pool.submit(get_rolling_mt5_candles, SYMBOL, "M1",  LOOKBACK_1M),
        }
    df15_raw = futures["M15"].result()
    df3_raw  = futures["M3"].result()
    df1_raw  = futures["M1"].result()

    if df15_raw.empty or df3_raw.empty or df1_raw.empty:
        log("Data missing, skipping...")
        return

    arr15 = prepare_15m_data(df15_raw)
    arr3  = prepare_3m_data(df3_raw)
    arr1  = prepare_1m_data(df1_raw)
    recent_1m = df1_raw.index[-RECENT_1M_COUNT:]

    for side, path in [("sell", _SELL_LOG_PATH), ("buy", _BUY_LOG_PATH)]:
        rows = run_strategy21(arr15, arr3, arr1, side, recent_1m)
        if rows:
            new_df = pd.DataFrame(rows)
            if path.exists():
                try:
                    old_df = pd.read_csv(path)
                    old_df = old_df[~old_df["Event ID"].isin(new_df["Event ID"])]
                    merged = pd.concat([old_df, new_df], ignore_index=True)
                except: merged = new_df
            else: merged = new_df
            merged.to_csv(path, index=False)

def recover_open_trades():
    if not trade_db.enabled(): return
    saved = trade_db.load_open_trades()
    if not saved: return
    open_tickets = None
    try:
        r = requests.get(f"{MT5_BRIDGE_URL}/positions", headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            live = data.get("positions", data) if isinstance(data, dict) else data
            open_tickets = {int(p.get("ticket", 0)) for p in live}
    except Exception as e: log(f"[RECOVERY] check failed: {e}")
    recovered = 0
    for tr in saved:
        ticket = tr["ticket"]
        if open_tickets is not None and ticket not in open_tickets:
            trade_db.record_close(ticket, reason="closed_while_offline")
            continue
        _ticket_map[ticket] = {
            "symbol": tr["symbol"], "event_id": tr["event_id"], "side": tr["side"],
            "entry_price": tr["entry_price"], "hard_sl": tr["hard_sl"],
            "targets": tr["targets"], "current_sl": tr["current_sl"], "trail_hit": set(tr["trail_hit"]),
        }
        _event_to_ticket[tr["event_id"]] = ticket
        trade_db.record_recovery_event(ticket, "resumed trailing after restart")
        recovered += 1
    if recovered: tg_post(f"♻️ RECOVERY: resumed trailing for {recovered} open position(s) after restart")

def run_trailing_pass():
    if not _ticket_map: return
    try:
        r = requests.get(f"{MT5_BRIDGE_URL}/positions", headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
        if r.status_code != 200: return
        data = r.json()
        live = data.get("positions", data) if isinstance(data, dict) else data
        open_tickets = {int(p.get("ticket", 0)) for p in live}
        
        closed = flatten_for_market_close(live, SYMBOL)
        live = [p for p in live if int(p.get("ticket", 0)) not in closed]
        
        df1 = get_rolling_mt5_candles(SYMBOL, "M1", LOOKBACK_1M)
        if df1 is not None and not df1.empty:
            trail_conservative_positions(live, float(df1["close"].iloc[-1]), SYMBOL, low_px=float(df1["low"].iloc[-1]), high_px=float(df1["high"].iloc[-1]))
        
        for t, rec in list(_ticket_map.items()):
            if t not in open_tickets and not rec.get("closed_notified"):
                tg_post(f"🔒 POSITION CLOSED\\nTicket: {t}\\nEntry: {rec.get('entry_price')}\\nLast SL: {rec.get('current_sl')}")
                rec["exit_datetime"] = datetime.now(timezone.utc).isoformat()
                rec["closed_notified"] = True
                trade_db.record_close(t, reason="not_in_positions")
    except Exception as e: log(f"[TRAIL-LOOP] error: {e}")

def _trailing_loop():
    while True:
        try: run_trailing_pass()
        except: pass
        _time.sleep(TRAIL_INTERVAL_SEC)

def main():
    log("=" * 60)
    log("🚀 Strategy 21 Live — STARTED")
    log("=" * 60)
    load_fired_events()
    recover_open_trades()
    tg_post(f"🚀 Strategy 21 Live — STARTED\\nSymbol: {SYMBOL}\\nMode: BUY + SELL\\nRisk per trade: ${RISK_PER_TRADE}\\nSL Invalidations: BUY > 1%, SELL < 1.5%")
    threading.Thread(target=_trailing_loop, daemon=True, name="stop-mgmt").start()
    
    last_scan_key = None
    try:
        while True:
            now = datetime.now(timezone.utc)
            scan_key = (now.year, now.month, now.day, now.hour, now.minute)
            if scan_key != last_scan_key:
                log(f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] Scanning...")
                try: run_live_scan()
                except Exception as e: log(f"Error in scan: {e}")
                last_scan_key = scan_key
            _time.sleep(SCAN_SLEEP_SEC)
    except KeyboardInterrupt:
        log("Stopped by user.")

if __name__ == "__main__":
    main()
"""

with open(output_path, "w") as f:
    f.write(content)
