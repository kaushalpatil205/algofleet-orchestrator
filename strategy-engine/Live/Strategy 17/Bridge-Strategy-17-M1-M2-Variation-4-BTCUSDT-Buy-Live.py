#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy 17 — Method 1 & Method 2 — Variation 4
-------------------------------------------------
5-min setup : Strategy 17 final-strategy-candle logic (unchanged).
1-min entry : M1 (backward search + forward threshold crossing) and M2
              (forward key-candle + level break) run in parallel.
              Mapped 1min candle = fcc_ts + 5min  ← BUG-FIX applied.
              Whichever method produces the first entry wins.  Tie → M1 wins.
Exit        : Standard ratio-based P&L  (1:0.5 … 1:10).

Extra columns after Status:
  9 EMA context columns — for 2H / 4H / 1D timeframes:
    "<TF> Price above/Below EMA 50 / EMA 100 / EMA 200"
  Logic: find the PREVIOUS COMPLETED candle on each TF relative to the
  1min entry datetime, then compare its close against EMA 50, 100, 200.
  Prints "Above" or "Below".
"""

import time as _time
import threading
import hashlib
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import numpy as np
import pandas as pd
import talib

# ================================================================
# CONFIG
# ================================================================ 

import json
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, os.path.basename(__file__).replace(".py", ".json"))
with open(_CONFIG_PATH, "r") as _f:
    _config = json.load(_f)

BOT_TOKEN = _config.get("BOT_TOKEN", "")
CHAT_ID = _config.get("CHAT_ID", "")
MT5_BRIDGE_URL = _config.get("MT5_BRIDGE_URL", "")
MT5_API_KEY = _config.get("MT5_API_KEY", "")
MAGIC = int(_config.get("MAGIC", 17001))
FLATTEN_BEFORE_WEEKEND = bool(_config.get("FLATTEN_BEFORE_WEEKEND", True))
FLATTEN_BEFORE_DAILY_BREAK = bool(_config.get("FLATTEN_BEFORE_DAILY_BREAK", False))
FLATTEN_LEAD_MIN = int(_config.get("FLATTEN_LEAD_MIN", 10))
TRAIL_INTERVAL_SEC = int(_config.get("TRAIL_INTERVAL_SEC", 10))
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Postgres trade persistence (best-effort; trading continues if DB is down).
# trade_db.py lives one level up (Live/), shared by every strategy subfolder.
import sys
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
import trade_db
trade_db.init("S17-M1M2-V4-BTCUSDT-BUY", _config.get("TRADE_DB_URL", ""), magic=MAGIC, bridge_url=MT5_BRIDGE_URL)




COIN_NAME = "BTCUSDT"
SYMBOL = "BTCUSD"
SYMBOL_BRIDGE = "BTCUSD"

BASE_LOG_DIR = Path("./bridge/Strategy 17 M1 M2 Variation 4 Live Logs")
BASE_LOG_DIR.mkdir(parents=True, exist_ok=True)
_BUY_LOG_PATH = BASE_LOG_DIR / f"Strategy17_M1M2_Var4_{COIN_NAME}_BUY_5min.csv"

LOOKBACK_5M  = 2500
LOOKBACK_1M  = 6000
LOOKBACK_2H  = 500
LOOKBACK_4H  = 300
LOOKBACK_1D  = 200

RECENT_1M_COUNT = 10
SCAN_SLEEP_SEC = 5

STARTUTC       = pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
RISK_PER_TRADE = float(_config.get("RISK_PER_TRADE", 100.0))

SMOOTH_MIN_RUN   = 4
BB_PERIOD        = 20
BB_STD           = 2.0
MAX_HIGH_UPDATES = 2

ALL_RATIOS   = [0.5] + list(range(1, 11))
RATIOS_FULL  = list(range(1, 11))
TRAIL_CPS    = list(range(2, 10))

METHOD2_BUFFER_CANDLES = 3
METHOD2_CHASE_WINDOW   = 6
EMA50_COL = "ema50"

WIN_1H  = 60;    WIN_2H  = 120;   WIN_4H  = 240
WIN_12H = 720;   WIN_24H = 1440;  WIN_48H = 2880
WIN_7D  = 10080; WIN_10D = 14400; WIN_30D = 43200

VOL_COL_MAP = OrderedDict({
    "v_48_4":  "48:4 Hrs Volume %",
    "v_7_1":   "7:1 Days Volume %",
    "v_10_2":  "10:2 Days Volume %",
    "v_30_10": "30:10 Days Volume %",
    "v_12_1":  "12:1 Hours Volume %",
    "v_24_1":  "24:1 Hours Volume %",
    "v_24_2":  "24:2 Hours Volume %",
    "v_48_2":  "48:2 Hours Volume %",
})

K_SMA     = "SMA(kama_ma_dataset)"
K_EMAKAMA = "EMAKAMA"
K_KLINE   = "KamaLine"
K_EMA50   = "EMA 50"


# ================================================================
# HELPERS
# ================================================================

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
    if isinstance(obj, (str, int, float, bool, np.floating, np.integer)):
        return [f"{sp}{obj}"]
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
    return "\n".join(_kv_lines(obj, 0))

def vblocks(blocks: List[Any]) -> str:
    parts = []
    for b in blocks:
        if not b: continue
        parts.append(vkv(b) if isinstance(b, (dict, OrderedDict)) else str(b))
    return "\n\n".join(parts)


# ================================================================
# LIVE DATA FETCHING
# ================================================================

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}

_candle_cache = {}

def get_rolling_mt5_candles(symbol: str, timeframe: str, required_lookback: int):
    global _candle_cache
    if _candle_cache.get((symbol, timeframe)) is None:
        df = fetch_live_mt5_candles(symbol, timeframe, required_lookback)
        _candle_cache[(symbol, timeframe)] = df
        return df
    else:
        new_df = fetch_live_mt5_candles(symbol, timeframe, 10)
        if new_df is None or len(new_df) == 0:
            return _candle_cache[(symbol, timeframe)]  # fetch failed; serve cache unchanged
        import pandas as pd
        # cached frames are datetime-INDEXED (see fetch_live_mt5_candles);
        # bring the index back to a column before dedupe/sort, then restore
        df = pd.concat([_candle_cache[(symbol, timeframe)].reset_index(), new_df.reset_index()])
        df = df.drop_duplicates(subset=["datetime"], keep="last")
        df = df.sort_values("datetime")
        df = df.tail(required_lookback).set_index("datetime")
        _candle_cache[(symbol, timeframe)] = df
        return df

def fetch_live_mt5_candles(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    try:
        url = f"{MT5_BRIDGE_URL}/market/candles/{symbol}?timeframe={timeframe}&count={count}"
        import requests
        r = requests.get(url, headers={"X-Api-Key": MT5_API_KEY}, timeout=8)
        r.raise_for_status()
        data = r.json()
        
        candles = data.get("candles", [])
        if not candles:
            import pandas as pd
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]).set_index("datetime")
            
        import pandas as pd
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
        log(f"Exness fetch error [{symbol} {timeframe}]: {e}")
        import pandas as pd
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]).set_index("datetime")


# ================================================================
# INDICATORS — 5min / 1min  (full stack)
# ================================================================

def add_smooth_macd_cycles(df: pd.DataFrame) -> pd.DataFrame:
    _, _, hist_raw = talib.MACD(df["close"].values.astype(float),
                                fastperiod=12, slowperiod=26, signalperiod=9)
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
    df["sm_hist"] = hist; df["sm_sign"] = sign
    df["sm_cycle"] = cycle; df["sm_color"] = color
    return df

def _pine_ema_series(series: pd.Series, length: int) -> pd.Series:
    alpha = 2.0 / (length + 1); out = np.zeros(len(series)); out[0] = series.iloc[0]
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
    ha["open"] = ha_open; ha["close"] = ha_close.values
    ha["high"] = np.maximum.reduce([df["high"].values, ha_open, ha_close.values])
    ha["low"]  = np.minimum.reduce([df["low"].values,  ha_open, ha_close.values])
    return ha

def _pine_kama_series(series: pd.Series, length=5, fast=2.5, slow=20) -> pd.Series:
    xvnoise = abs(series - series.shift(1))
    nsignal = abs(series - series.shift(length))
    nnoise  = xvnoise.rolling(length).sum()
    nfast = 2.0 / (fast + 1); nslow = 2.0 / (slow + 1)
    kama = np.zeros(len(series))
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

def calc_kama_line(src: np.ndarray, length=14, fast_length=2,
                   slow_length=30, hp_period=48) -> np.ndarray:
    n = len(src); pi = 2.0 * np.arcsin(1.0)
    alpha1 = ((np.cos(.707*2*pi/hp_period) + np.sin(.707*2*pi/hp_period) - 1.0)
              / np.cos(.707*2*pi/hp_period))
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

def prepare_df(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Full indicator stack for 5min / 1min data."""
    df = df_raw.copy()
    close = df["close"].values.astype(float)
    op = df["open"].values.astype(float)
    hi = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    bb_upper, _, bb_lower = talib.BBANDS(close, timeperiod=BB_PERIOD,
                                          nbdevup=BB_STD, nbdevdn=BB_STD, matype=0)
    df["bb_upper"] = bb_upper; df["bb_lower"] = bb_lower
    df["sma_kama"] = calc_sma_kama(df, length=20)
    df["emakama"]  = pd.Series(calc_emakama(close), index=df.index)
    ohlc4 = (op + hi + lo + close) / 4.0
    df["kama_line"] = pd.Series(calc_kama_line(ohlc4), index=df.index)
    df[EMA50_COL]   = talib.EMA(close, timeperiod=50)
    df = add_smooth_macd_cycles(df)
    return df


# ================================================================
# INDICATORS — 2H / 4H / 1D  (EMA-only, lightweight)
# ================================================================

def prepare_df_tf(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Lightweight indicator prep for higher-timeframe data.
    Only computes EMA 50, EMA 100, EMA 200.
    """
    df = df_raw.copy()
    close = df["close"].values.astype(float)
    df["ema50"]  = talib.EMA(close, timeperiod=50)
    df["ema100"] = talib.EMA(close, timeperiod=100)
    df["ema200"] = talib.EMA(close, timeperiod=200)
    return df


# ================================================================
# FAST ARRAYS
# ================================================================

def make_fast_arrays(df: pd.DataFrame) -> Dict[str, Any]:
    """Full fast-array dict for 5min / 1min data."""
    fa = {
        "idx":      df.index,
        "open":     df["open"].values.astype(float),
        "high":     df["high"].values.astype(float),
        "low":      df["low"].values.astype(float),
        "close":    df["close"].values.astype(float),
        "bb_upper": df["bb_upper"].values.astype(float),
        "bb_lower": df["bb_lower"].values.astype(float),
        "sma_kama": df["sma_kama"].values.astype(float),
        "emakama":  df["emakama"].values.astype(float),
        "kama_line":df["kama_line"].values.astype(float),
        "sm_cycle": df["sm_cycle"].values.astype(int),
        "sm_color": df["sm_color"].values.astype(object),
        "volume":   df["volume"].values.astype(float),
    }
    fa["ema50"] = (df[EMA50_COL].values.astype(float)
                   if EMA50_COL in df.columns else np.full(len(df), np.nan))
    return fa

def make_tf_arrays(df: pd.DataFrame) -> Dict[str, Any]:
    """Lightweight array dict for 2H / 4H / 1D EMA data."""
    return {
        "idx":    df.index,
        "close":  df["close"].values.astype(float),
        "ema50":  df["ema50"].values.astype(float),
        "ema100": df["ema100"].values.astype(float),
        "ema200": df["ema200"].values.astype(float),
    }


# ================================================================
# VOLUME
# ================================================================

class VolumeCalculator:
    def __init__(self, df: pd.DataFrame):
        self.idx = df.index
        self.cum = df["volume"].cumsum()

    def _win(self, end_ts: pd.Timestamp, mins: int) -> float:
        st = end_ts - pd.Timedelta(minutes=mins)
        ep = self.idx.searchsorted(end_ts, side="right") - 1
        sp = self.idx.searchsorted(st, side="right")
        if ep < 0 or sp >= len(self.idx) or sp > ep: return 0.0
        return float(self.cum.iloc[ep] - (self.cum.iloc[sp - 1] if sp > 0 else 0.0))

    def ratios(self, ts: pd.Timestamp) -> Dict[str, float]:
        s1  = self._win(ts, WIN_1H);  s2  = self._win(ts, WIN_2H)
        s4  = self._win(ts, WIN_4H);  s12 = self._win(ts, WIN_12H)
        s24 = self._win(ts, WIN_24H); s48 = self._win(ts, WIN_48H)
        s7  = self._win(ts, WIN_7D);  s10 = self._win(ts, WIN_10D)
        s30 = self._win(ts, WIN_30D)
        a1 = s1*24; a2 = s2*12; a4 = s4*6; a12 = s12*2
        a24 = s24; a48 = s48/2; a7d = s7/7; a10d = s10/10; a30d = s30/30
        def p(n, d): return round(100.0*n/d, 2) if d and d > 0 else 0.0
        return {
            "v_48_4":  p(a4, a48),   "v_7_1":   p(a24, a7d),
            "v_10_2":  p(a48, a10d), "v_30_10":  p(a10d, a30d),
            "v_12_1":  p(a12, a1),   "v_24_1":   p(a24, a1),
            "v_24_2":  p(a24, a2),   "v_48_2":   p(a48, a2),
        }


# ================================================================
# EMA CONTEXT CHECK  (2H / 4H / 1D)
# ================================================================

def check_ema_position(arr_tf: Dict[str, Any],
                       entry_ts: pd.Timestamp) -> Dict[str, Optional[str]]:
    """
    Given a TF fast-array and an entry datetime:
      1. Locate the CURRENT (still-forming) candle using searchsorted.
      2. Step back one position to get the PREVIOUS COMPLETED candle.
      3. Compare its close against EMA 50 / 100 / 200.
      Returns {"ema50": "Above"/"Below"/None, "ema100": ..., "ema200": ...}
    """
    idx = arr_tf["idx"]
    n   = len(idx)
    if n < 2:
        return {"ema50": None, "ema100": None, "ema200": None}

    # searchsorted side='right' → first position whose value > entry_ts
    # Subtract 1 → last candle that started AT OR BEFORE entry_ts = current forming candle
    cur_pos  = int(idx.searchsorted(entry_ts, side="right")) - 1
    prev_pos = cur_pos - 1   # previous COMPLETED candle

    if prev_pos < 0 or prev_pos >= n:
        return {"ema50": None, "ema100": None, "ema200": None}

    cl = float(arr_tf["close"][prev_pos])

    def chk(ema_arr: np.ndarray) -> Optional[str]:
        v = float(ema_arr[prev_pos])
        if np.isnan(v): return None
        return "Above" if cl > v else "Below"

    return {
        "ema50":  chk(arr_tf["ema50"]),
        "ema100": chk(arr_tf["ema100"]),
        "ema200": chk(arr_tf["ema200"]),
    }


# ================================================================
# COMMON HELPERS
# ================================================================

def build_cycle_map(arr: Dict[str, Any]) -> List[Dict[str, Any]]:
    cyc = arr["sm_cycle"]; col = arr["sm_color"]; idx = arr["idx"]; out = []
    for cid in pd.unique(cyc):
        pos = np.where(cyc == cid)[0]
        if len(pos) == 0: continue
        sp = int(pos[0]); ep = int(pos[-1])
        out.append({"cycle_id": int(cid), "start_pos": sp, "end_pos": ep,
                    "start_ts": idx[sp], "end_ts": idx[ep], "color": str(col[sp])})
    return out

def kama_values_at_arr(arr: Dict[str, Any], pos: int) -> Dict[str, float]:
    def safe(a): x = float(a[pos]); return x if not np.isnan(x) else np.nan
    return {K_SMA:     safe(arr["sma_kama"]),
            K_EMAKAMA: safe(arr["emakama"]),
            K_KLINE:   safe(arr["kama_line"])}

def level_values_at_arr(arr: Dict[str, Any], pos: int) -> Dict[str, float]:
    def safe(a): x = float(a[pos]); return x if not np.isnan(x) else np.nan
    return {K_SMA:     safe(arr["sma_kama"]),
            K_EMAKAMA: safe(arr["emakama"]),
            K_KLINE:   safe(arr["kama_line"]),
            K_EMA50:   safe(arr["ema50"])}

def _extreme_kama(kv, side):
    valid = {k: v for k, v in kv.items() if not (v is None or np.isnan(v))}
    if not valid: return None, np.nan
    name = max(valid, key=lambda k: valid[k]) if side == "buy" else min(valid, key=lambda k: valid[k])
    return name, valid[name]

def _extreme_level(levels, side):
    valid = {k: v for k, v in levels.items() if not (v is None or np.isnan(v))}
    if not valid: return None, np.nan
    name = max(valid, key=lambda k: valid[k]) if side == "buy" else min(valid, key=lambda k: valid[k])
    return name, valid[name]

def _touches_key_band_arr(arr, pos, side):
    if side == "buy":
        return (not np.isnan(arr["bb_lower"][pos])) and (arr["low"][pos] <= arr["bb_lower"][pos])
    return (not np.isnan(arr["bb_upper"][pos])) and (arr["high"][pos] >= arr["bb_upper"][pos])

def _closes_past_threshold(close_: float, threshold: float, side: str) -> bool:
    return close_ > threshold if side == "buy" else close_ < threshold

def _candidate_exceeds_threshold(hi: float, lo: float, threshold: float, side: str) -> bool:
    return hi > threshold if side == "buy" else lo < threshold

def _close_beyond_bb_arr(arr, pos, side):
    if side == "buy":
        return (not np.isnan(arr["bb_upper"][pos])) and (arr["close"][pos] > arr["bb_upper"][pos])
    return (not np.isnan(arr["bb_lower"][pos])) and (arr["close"][pos] < arr["bb_lower"][pos])

def _best_value_of_high_or_bb_arr(arr, pos, side):
    if side == "buy":
        hi = arr["high"][pos]; bb = arr["bb_upper"][pos]
        if np.isnan(bb) or hi >= bb:
            return float(hi), "upper bb" if (not np.isnan(bb) and bb > hi) else "highest high"
        return float(bb), "upper bb"
    lo = arr["low"][pos]; bb = arr["bb_lower"][pos]
    if np.isnan(bb) or lo <= bb:
        return float(lo), "lower bb" if (not np.isnan(bb) and bb < lo) else "lowest low"
    return float(bb), "lower bb"

def _special_same_candle_bb_close(arr, pos, side):
    if side == "buy":
        bbl = arr["bb_lower"][pos]
        return (not np.isnan(bbl)) and (arr["close"][pos] < bbl)
    bbu = arr["bb_upper"][pos]
    return (not np.isnan(bbu)) and (arr["close"][pos] > bbu)

def add_paired_active_info(setup, other_active):
    ie = setup.get("inv_extra", OrderedDict())
    if "Paired active setup macd cycle startime" in ie:
        setup["inv_extra"] = ie; return
    lbl = ("Two Setup which remained active later on after completion of this setup"
           if setup.get("status") == "Final Strategy Candle Found"
           else "Two Setup which remained active later on after invalidation of this setup")
    ie["Paired active setup macd cycle startime"] = _s(other_active[0].get("cycle_start_ts")) if other_active else None
    ie["Paired active setup key candle datetime"] = _s(other_active[0].get("key_ts")) if other_active else None
    ie[lbl] = ""
    for i in range(2):
        pfx = "1st" if i == 0 else "2nd"
        if i < len(other_active):
            ie[f"{pfx} active setup macd cycle startime"] = _s(other_active[i].get("cycle_start_ts"))
            ie[f"{pfx} active setup key candle datetime"] = _s(other_active[i].get("key_ts"))
        else:
            ie[f"{pfx} active setup macd cycle startime"] = None
            ie[f"{pfx} active setup key candle datetime"] = None
    setup["inv_extra"] = ie


# ================================================================
# 5MIN STRATEGY
# ================================================================

def _new_5m_setup(cycle, pos, ts, key_extreme, kv, side):
    ek_name, ek_val = _extreme_kama(kv, side)
    return {"side": side, "cycle_id": cycle["cycle_id"],
            "cycle_start_pos": cycle["start_pos"], "cycle_end_pos": cycle["end_pos"],
            "cycle_start_ts": cycle["start_ts"], "cycle_end_ts": cycle["end_ts"],
            "key_pos": pos, "key_ts": ts, "key_extreme": key_extreme, "kv_at_key": kv,
            "key_update_count": 0, "key_history": [], "ek_name": ek_name, "ek_val": ek_val,
            "threshold": ek_val, "high_update_count": 0, "high_update_history": [],
            "fcc_pos": None, "fcc_ts": None, "fcc_close": None,
            "fcc_hi_or_lo": None, "fcc_above_bb": None,
            "status": None, "inv_extra": OrderedDict()}

def _reset_after_key_update_5m(setup, pos, ts, key_extreme, kv):
    old = OrderedDict({
        "key_pos": setup["key_pos"], "key_ts": _s(setup["key_ts"]),
        "key_extreme": _f(setup["key_extreme"]), "ek_name": setup["ek_name"],
        "ek_val": _f(setup["ek_val"]),
        "kv_at_key": {k: _f(v) for k, v in setup["kv_at_key"].items()}})
    setup["key_history"].append(old); setup["key_update_count"] += 1
    setup["key_pos"] = pos; setup["key_ts"] = ts
    setup["key_extreme"] = key_extreme; setup["kv_at_key"] = kv
    ek_name, ek_val = _extreme_kama(kv, setup["side"])
    setup["ek_name"] = ek_name; setup["ek_val"] = ek_val; setup["threshold"] = ek_val
    setup["high_update_count"] = 0; setup["high_update_history"] = []
    setup["fcc_pos"] = None; setup["fcc_ts"] = None; setup["fcc_close"] = None
    setup["fcc_hi_or_lo"] = None; setup["fcc_above_bb"] = None

def scan_5m_final_strategy_candles(df: pd.DataFrame, side: str) -> List[Dict[str, Any]]:
    arr = make_fast_arrays(df); idx = arr["idx"]
    hi_a, lo_a, cl_a = arr["high"], arr["low"], arr["close"]
    cycles = build_cycle_map(arr); target_color = "Red" if side == "buy" else "Green"
    cycle_by_id = {c["cycle_id"]: c for c in cycles}
    active: List[Dict] = []; done: List[Dict] = []

    def emit(s, live_active):
        others = [x for x in live_active if x is not s]
        add_paired_active_info(s, others[:2]); done.append(s.copy())

    for pos in range(len(idx)):
        ts = idx[pos]; current_cid = int(arr["sm_cycle"][pos])
        current_color = str(arr["sm_color"][pos]); cycle = cycle_by_id[current_cid]

        if current_color == target_color:
            same_cycle = [s for s in active if s["cycle_id"] == current_cid]
            if not same_cycle:
                if _touches_key_band_arr(arr, pos, side):
                    kv = kama_values_at_arr(arr, pos)
                    key_extreme = lo_a[pos] if side == "buy" else hi_a[pos]
                    new_s = _new_5m_setup(cycle, pos, ts, key_extreme, kv, side)
                    if len(active) >= 2:
                        oldest = active.pop(0)
                        oldest["status"] = "Invalidated due to New Setup found only 2 can be active"
                        oldest["inv_extra"] = OrderedDict({
                            "Invalidation Reason": "Invalidated due to New Setup found only 2 can be active",
                            f"{target_color} MACD CYCLE start time due to which setup invalidated": _s(new_s["cycle_start_ts"]),
                            f"{target_color} MACD CYCLE endtime time due to which setup invalidated": _s(new_s["cycle_end_ts"]),
                            "Key candle datetime due to which setup invalidated": _s(new_s["key_ts"]),
                            "New Setup found Datetime": _s(ts)})
                        emit(oldest, active + [new_s])
                    active.append(new_s)
            else:
                s = same_cycle[0]
                if side == "buy":
                    new_key = _touches_key_band_arr(arr, pos, side) and (lo_a[pos] < s["key_extreme"])
                else:
                    new_key = _touches_key_band_arr(arr, pos, side) and (hi_a[pos] > s["key_extreme"])
                if new_key:
                    kv = kama_values_at_arr(arr, pos)
                    key_extreme = lo_a[pos] if side == "buy" else hi_a[pos]
                    _reset_after_key_update_5m(s, pos, ts, key_extreme, kv)

        for s in list(active):
            if pos <= s["key_pos"]: continue
            threshold = s["threshold"]
            if np.isnan(threshold): continue
            closes_past = _closes_past_threshold(cl_a[pos], threshold, side)
            makes_new   = (_candidate_exceeds_threshold(hi_a[pos], lo_a[pos], threshold, side)
                           and not closes_past)
            if closes_past:
                s["fcc_pos"] = pos; s["fcc_ts"] = ts; s["fcc_close"] = cl_a[pos]
                s["fcc_hi_or_lo"] = hi_a[pos] if side == "buy" else lo_a[pos]
                s["fcc_above_bb"] = _close_beyond_bb_arr(arr, pos, side)
                s["status"] = "Final Strategy Candle Found"
                active_after = [x for x in active if x is not s]
                emit(s, active_after); active = active_after; continue
            if makes_new:
                if s["high_update_count"] >= MAX_HIGH_UPDATES:
                    s["status"] = "Invalidated due Candle has made third high while updating kama levels"
                    active_after = [x for x in active if x is not s]
                    emit(s, active_after); active = active_after; continue
                new_thresh = hi_a[pos] if side == "buy" else lo_a[pos]
                s["high_update_count"] += 1
                s["high_update_history"].append({"val": _f(new_thresh), "ts": _s(ts)})
                s["threshold"] = new_thresh

    for s in list(active):
        s["status"] = "Forming - open at end of live data"
        active_after = [x for x in active if x is not s]
        emit(s, active_after); active = active_after

    dt_map: Dict[str, List[int]] = {}
    for i, s in enumerate(done):
        edt = _s(s.get("fcc_ts"))
        if edt and s.get("status") == "Final Strategy Candle Found":
            dt_map.setdefault(edt, []).append(i)
    for edt, idxs in dt_map.items():
        if len(idxs) < 2: continue
        idxs = sorted(idxs, key=lambda i: str(done[i].get("cycle_start_ts") or ""))
        newest = idxs[-1]
        for old_i in idxs[:-1]:
            newer = done[newest]
            done[old_i]["status"] = (f"Invalidated due to {'Buy' if side=='buy' else 'Sell'}"
                                     " occured at same Datetime")
            ie = done[old_i].get("inv_extra", OrderedDict())
            ie["Invalidation Reason"] = done[old_i]["status"]
            ie[f"{target_color} MACD CYCLE start time due to which setup invalidated"] = _s(newer["cycle_start_ts"])
            ie[f"{target_color} MACD CYCLE endtime time due to which setup invalidated"] = _s(newer["cycle_end_ts"])
            ie["Key candle datetime due to which setup invalidated"] = _s(newer["key_ts"])
            ie[f"Final {'buy' if side=='buy' else 'sell'} datetime"] = edt
            done[old_i]["inv_extra"] = ie
    done.sort(key=lambda s: str(s.get("cycle_start_ts") or ""))
    return done


# ================================================================
# METHOD 1 — BACKWARD SETUP
# ================================================================

def eval_cycle_to_mapped_nearest(arr, cycle, mapped_pos, side):
    idx = arr["idx"]; sp = cycle["start_pos"]; ep = cycle["end_pos"]
    if ep >= mapped_pos: return None
    state = None
    for pos in range(sp, mapped_pos + 1):
        hi = arr["high"][pos]; lo = arr["low"][pos]; cl = arr["close"][pos]
        in_key_cycle = (pos <= ep)
        if state is None:
            if in_key_cycle and _touches_key_band_arr(arr, pos, side):
                kv = kama_values_at_arr(arr, pos); ek_name, ek_val = _extreme_kama(kv, side)
                state = {"cycle_start_ts": cycle["start_ts"], "cycle_end_ts": cycle["end_ts"],
                         "key_pos": pos, "key_ts": idx[pos],
                         "key_extreme": lo if side == "buy" else hi,
                         "kv_at_key": kv, "ek_name": ek_name, "ek_val": ek_val,
                         "threshold": ek_val, "key_update_count": 0,
                         "high_update_count": 0, "high_update_history": [],
                         "fcc_pos": None, "fcc_ts": None, "fcc_close": None}
            continue
        if in_key_cycle:
            new_key = _touches_key_band_arr(arr, pos, side) and (
                lo < state["key_extreme"] if side == "buy" else hi > state["key_extreme"])
            if new_key:
                kv = kama_values_at_arr(arr, pos); ek_name, ek_val = _extreme_kama(kv, side)
                state.update({"key_pos": pos, "key_ts": idx[pos],
                               "key_extreme": lo if side == "buy" else hi,
                               "kv_at_key": kv, "ek_name": ek_name, "ek_val": ek_val,
                               "threshold": ek_val, "key_update_count": state["key_update_count"] + 1,
                               "high_update_count": 0, "high_update_history": [],
                               "fcc_pos": None, "fcc_ts": None, "fcc_close": None})
                continue
        if pos <= state["key_pos"]: continue
        threshold = state["threshold"]
        if np.isnan(threshold): continue
        closes_past = _closes_past_threshold(cl, threshold, side)
        makes_new   = _candidate_exceeds_threshold(hi, lo, threshold, side) and (not closes_past)
        if closes_past:
            state["fcc_pos"] = pos; state["fcc_ts"] = idx[pos]; state["fcc_close"] = cl
            return state
        if makes_new:
            if state["high_update_count"] >= MAX_HIGH_UPDATES: return None
            new_thresh = hi if side == "buy" else lo
            state["high_update_count"] += 1
            state["high_update_history"].append({"val": _f(new_thresh), "ts": _s(idx[pos])})
            state["threshold"] = new_thresh
    return None

def find_backward_setup_on_1m_nearest(arr1, cycles1, mapped_pos, lower_bound_pos, side):
    target_color = "Red" if side == "buy" else "Green"
    candidates = [c for c in cycles1
                  if c["color"] == target_color
                  and c["start_pos"] >= lower_bound_pos
                  and c["start_pos"] < mapped_pos]
    candidates.sort(key=lambda c: c["end_pos"], reverse=True)
    for cyc in candidates:
        found = eval_cycle_to_mapped_nearest(arr1, cyc, mapped_pos, side)
        if found is not None: return found
    return None

def find_method1_entry_fast(arr1, mapped_pos, backward_setup, side):
    idx = arr1["idx"]; key_pos = backward_setup["key_pos"]
    if mapped_pos >= len(idx): return {"found": False}
    if side == "buy":
        key_bb = arr1["bb_upper"][key_pos]; key_hl = arr1["high"][key_pos]
        key_best_src = "highest high" if (np.isnan(key_bb) or key_hl >= key_bb) else "upper bb"
    else:
        key_bb = arr1["bb_lower"][key_pos]; key_hl = arr1["low"][key_pos]
        key_best_src = "lowest low" if (np.isnan(key_bb) or key_hl <= key_bb) else "lower bb"
    rng_hi  = arr1["high"][key_pos:mapped_pos + 1]
    rng_lo  = arr1["low"][key_pos:mapped_pos + 1]
    rng_bbu = arr1["bb_upper"][key_pos:mapped_pos + 1]
    rng_bbl = arr1["bb_lower"][key_pos:mapped_pos + 1]
    if side == "buy":
        rel_hi = int(np.argmax(rng_hi)); max_hi = float(rng_hi[rel_hi]); hi_idx = key_pos + rel_hi
        valid_bbu = np.where(~np.isnan(rng_bbu))[0]
        if len(valid_bbu):
            rel_bb = valid_bbu[np.argmax(rng_bbu[valid_bbu])]
            max_bb = float(rng_bbu[rel_bb]); bb_idx = key_pos + int(rel_bb)
        else: max_bb = np.nan; bb_idx = None
        if np.isnan(max_bb) or max_hi >= max_bb:
            backward_threshold = max_hi; backward_src = "highest high"; backward_ts = idx[hi_idx]
        else:
            backward_threshold = max_bb; backward_src = "upper bb"; backward_ts = idx[bb_idx]
        min_lo = float(np.min(rng_lo)); lo_idx = key_pos + int(np.argmin(rng_lo)); min_bb = np.nan
    else:
        rel_lo = int(np.argmin(rng_lo)); min_lo = float(rng_lo[rel_lo]); lo_idx = key_pos + rel_lo
        valid_bbl = np.where(~np.isnan(rng_bbl))[0]
        if len(valid_bbl):
            rel_bb = valid_bbl[np.argmin(rng_bbl[valid_bbl])]
            min_bb = float(rng_bbl[rel_bb]); bb_idx = key_pos + int(rel_bb)
        else: min_bb = np.nan; bb_idx = None
        if np.isnan(min_bb) or min_lo <= min_bb:
            backward_threshold = min_lo; backward_src = "lowest low"; backward_ts = idx[lo_idx]
        else:
            backward_threshold = min_bb; backward_src = "lower bb"; backward_ts = idx[bb_idx]
        max_hi = float(np.max(rng_hi)); hi_idx = key_pos + int(np.argmax(rng_hi)); max_bb = np.nan

    threshold = backward_threshold
    got_update = False
    most_updated_val = None
    most_updated_ts = None
    most_updated_src = None
    entry_ts = None
    entry_price = None
    entry_from = None
    close_above_prev_dt = None
    close_above_prev_val = None

    for pos in range(mapped_pos + 1, len(idx)):
        cl = arr1["close"][pos]

        # ── ENTRY: close crosses current threshold ────────────────────────
        if _closes_past_threshold(cl, threshold, side):
            entry_ts             = idx[pos]
            entry_price          = float(cl)
            entry_from           = "Updated High" if got_update else "Backward Candle"
            close_above_prev_dt  = idx[pos]
            close_above_prev_val = float(cl)
            break

        # ── THRESHOLD UPDATE ──────────────────────────────────────────────
        # Rule: ONLY update when the actual candle hi/lo (not BB alone)
        # crosses the current threshold intracandle without the close doing so.
        # If the actual extreme does cross, BB is ALSO checked — if BB is even
        # more extreme it becomes the updated value.
        # This prevents BB drift from triggering spurious updates that would
        # block a valid close-based entry on the very next candle.
        if side == "buy":
            actual_hi = float(arr1["high"][pos])
            if actual_hi > threshold:               # price DID trade above threshold
                bb_up = arr1["bb_upper"][pos]
                if not np.isnan(bb_up) and bb_up > actual_hi:
                    cand_val, cand_src = float(bb_up), "upper bb"
                else:
                    cand_val, cand_src = actual_hi, "highest high"
                threshold        = cand_val
                got_update       = True
                most_updated_val = cand_val
                most_updated_ts  = idx[pos]
                most_updated_src = cand_src
        else:
            actual_lo = float(arr1["low"][pos])
            if actual_lo < threshold:               # price DID trade below threshold
                bb_lo = arr1["bb_lower"][pos]
                if not np.isnan(bb_lo) and bb_lo < actual_lo:
                    cand_val, cand_src = float(bb_lo), "lower bb"
                else:
                    cand_val, cand_src = actual_lo, "lowest low"
                threshold        = cand_val
                got_update       = True
                most_updated_val = cand_val
                most_updated_ts  = idx[pos]
                most_updated_src = cand_src

    return {
        "found": entry_ts is not None,
        "mapped_pos": mapped_pos,
        "mapped_ts": idx[mapped_pos] if mapped_pos < len(idx) else None,
        "key_bb_value": _f(key_bb), "key_high_low_value": _f(key_hl),
        "key_best_value_type": key_best_src,
        "highest_candle_value_range": _f(max_hi if side == "buy" else min_lo),
        "highest_candle_dt_range": _s(idx[hi_idx] if side == "buy" else idx[lo_idx]),
        "highest_bb_value_range": _f(max_bb if side == "buy" else min_bb),
        "highest_bb_dt_range": _s(idx[bb_idx]) if bb_idx is not None else None,
        "backward_threshold": _f(backward_threshold),
        "backward_threshold_src": backward_src,
        "backward_threshold_dt": _s(backward_ts),
        "after_mapped_updated": "Yes" if got_update else "No",
        "most_updated_val": _f(most_updated_val),
        "most_updated_ts": _s(most_updated_ts),
        "most_updated_src": most_updated_src,
        "entry_from": entry_from,
        "entry_ts": entry_ts,
        "entry_price": _f(entry_price),
        "close_above_prev_dt": _s(close_above_prev_dt),
        "close_above_prev_val": _f(close_above_prev_val),
    }


# ================================================================
# METHOD 2 — FORWARD KEY CANDLE
# ================================================================

def _new_m2_key_state(arr1, pos, side):
    levels = level_values_at_arr(arr1, pos); lvl_name, lvl_val = _extreme_level(levels, side)
    return {"key_pos": pos, "key_ts": arr1["idx"][pos], "levels": levels,
            "level_name": lvl_name, "level_value": lvl_val,
            "break_pos": None, "break_ts": None, "break_close": None,
            "phase": "watch", "buffer_vals": [], "buffer_extreme": None,
            "buffer_extreme_ts": None, "chase_threshold": None,
            "chase_threshold_ts": None, "chase_updates": 0, "chase_count": 0,
            "scenario": None, "entry_pos": None, "entry_ts": None, "entry_price": None}

def _m2_invalid_block(state, side, reason):
    x = OrderedDict()
    x["Key Candle DT"] = _s(state.get("key_ts"))
    x["Highest Value at Key candle" if side == "buy" else "Lowest Value at Key candle"] = _f(state.get("level_value"))
    x["Invalidated Reason"] = reason
    return x

def find_method2_on_1m(arr1, mapped_pos, side):
    idx = arr1["idx"]; hi_a = arr1["high"]; lo_a = arr1["low"]; cl_a = arr1["close"]
    invalidated: List[OrderedDict] = []; current = None; key_updates = 0

    for pos in range(mapped_pos, len(idx)):
        ts = idx[pos]; hi = hi_a[pos]; lo = lo_a[pos]; cl = cl_a[pos]
        is_key = _touches_key_band_arr(arr1, pos, side)

        if is_key:
            if current is None: current = _new_m2_key_state(arr1, pos, side); continue
            if pos > current["key_pos"]:
                key_updates += 1; current = _new_m2_key_state(arr1, pos, side); continue

        if current is None or pos <= current["key_pos"]: continue

        if current["phase"] == "watch":
            threshold = current["level_value"]
            if np.isnan(threshold): continue
            closes_past = _closes_past_threshold(cl, threshold, side)
            makes_new   = _candidate_exceeds_threshold(hi, lo, threshold, side) and (not closes_past)
            if closes_past:
                current["break_pos"] = pos; current["break_ts"] = ts; current["break_close"] = cl
                if not _special_same_candle_bb_close(arr1, pos, side):
                    current["scenario"] = "Scenario 1"; current["entry_pos"] = pos
                    current["entry_ts"] = ts; current["entry_price"] = cl
                    return {"found": True, "status": "Intrade",
                            "key_updates": key_updates, "invalidated": invalidated, "final": current}
                current["phase"] = "buffer"
                anchor = lo if side == "buy" else hi
                current["buffer_vals"] = [(pos, anchor)]
                current["buffer_extreme"] = anchor; current["buffer_extreme_ts"] = ts; continue
            if makes_new:
                invalidated.append(_m2_invalid_block(current, side,
                    f"Invalidated due to new {'High' if side=='buy' else 'Low'} Level found at DT {_s(ts)}"))
                current = None; continue

        elif current["phase"] == "buffer":
            extreme_val = lo if side == "buy" else hi
            current["buffer_vals"].append((pos, extreme_val))
            if ((side == "buy" and extreme_val < current["buffer_extreme"]) or
                    (side == "sell" and extreme_val > current["buffer_extreme"])):
                current["buffer_extreme"] = extreme_val; current["buffer_extreme_ts"] = ts
            if len(current["buffer_vals"]) >= METHOD2_BUFFER_CANDLES:
                current["chase_threshold"] = current["buffer_extreme"]
                current["chase_threshold_ts"] = current["buffer_extreme_ts"]
                current["chase_updates"] = 0; current["chase_count"] = 0; current["phase"] = "chase"

        elif current["phase"] == "chase":
            current["chase_count"] += 1; threshold = current["chase_threshold"]
            if side == "buy":
                closes_entry = cl < threshold; makes_new = (lo < threshold) and (not closes_entry)
            else:
                closes_entry = cl > threshold; makes_new = (hi > threshold) and (not closes_entry)
            if closes_entry:
                current["scenario"] = "Scenario 2"; current["entry_pos"] = pos
                current["entry_ts"] = ts; current["entry_price"] = cl
                return {"found": True, "status": "Intrade",
                        "key_updates": key_updates, "invalidated": invalidated, "final": current}
            if makes_new:
                current["chase_threshold"] = lo if side == "buy" else hi
                current["chase_threshold_ts"] = ts; current["chase_updates"] += 1
            if current["chase_count"] >= METHOD2_CHASE_WINDOW:
                invalidated.append(_m2_invalid_block(current, side,
                    f"Invalidated due to entry not found in next {METHOD2_CHASE_WINDOW} candle"))
                current = None; continue

    if current is not None:
        invalidated.append(_m2_invalid_block(current, side, "Invalidated - open at end of data"))
    return {"found": False, "status": "Invalidated due to Method 2 entry not found",
            "key_updates": key_updates, "invalidated": invalidated, "final": current}


# ================================================================
# HARD SL
# ================================================================

def compute_hard_sl_from_5m_window(arr5, final_pos, side):
    start = max(0, final_pos - 4); idx = arr5["idx"]; wpos = range(start, final_pos + 1)
    if side == "buy":
        lv = arr5["low"][start:final_pos + 1]; lowidx = start + int(np.argmin(lv))
        lowest_low = float(arr5["low"][lowidx])
        kstack = [(float(arr5[key][p]), idx[p], nm)
                  for p in wpos
                  for nm, key in [(K_SMA, "sma_kama"), (K_EMAKAMA, "emakama"), (K_KLINE, "kama_line")]
                  if not np.isnan(arr5[key][p])]
        if kstack: lowest_kama, lowest_kama_ts, _ = min(kstack, key=lambda x: x[0])
        else:      lowest_kama, lowest_kama_ts = np.nan, None
        hard_sl = lowest_low if (np.isnan(lowest_kama) or lowest_low <= lowest_kama) else lowest_kama
        hf = "candle Low" if (np.isnan(lowest_kama) or lowest_low <= lowest_kama) else "kama Levels"
        return {"prev5_kama_extreme": _f(lowest_kama), "prev5_kama_dt": _s(lowest_kama_ts),
                "prev5_candle_extreme": _f(lowest_low), "prev5_candle_dt": _s(idx[lowidx]),
                "hard_sl": _f(hard_sl), "hard_sl_from": hf}
    hv = arr5["high"][start:final_pos + 1]; highidx = start + int(np.argmax(hv))
    highest_high = float(arr5["high"][highidx])
    kstack = [(float(arr5[key][p]), idx[p], nm)
              for p in wpos
              for nm, key in [(K_SMA, "sma_kama"), (K_EMAKAMA, "emakama"), (K_KLINE, "kama_line")]
              if not np.isnan(arr5[key][p])]
    if kstack: highest_kama, highest_kama_ts, _ = max(kstack, key=lambda x: x[0])
    else:      highest_kama, highest_kama_ts = np.nan, None
    hard_sl = highest_high if (np.isnan(highest_kama) or highest_high >= highest_kama) else highest_kama
    hf = "candle High" if (np.isnan(highest_kama) or highest_high >= highest_kama) else "kama Levels"
    return {"prev5_kama_extreme": _f(highest_kama), "prev5_kama_dt": _s(highest_kama_ts),
            "prev5_candle_extreme": _f(highest_high), "prev5_candle_dt": _s(idx[highidx]),
            "hard_sl": _f(hard_sl), "hard_sl_from": hf}


# ================================================================
# BACKTEST  (ratio-based)
# ================================================================

def _pct(entry: float, ref: float) -> float:
    return abs(entry - ref) / entry * 100.0 if entry != 0 else 0.0

def _tgt(entry: float, sl: float, side: str, r: float) -> float:
    risk = abs(entry - sl)
    return entry + risk * r if side == "buy" else entry - risk * r

def _pnl(exit_px: float, entry: float, qty: float, side: str) -> float:
    return (exit_px - entry) * qty if side == "buy" else (entry - exit_px) * qty

def run_backtest_v1(arr1, entry_pos, entry_price, hard_sl, side):
    idx = arr1["idx"]; hi_a = arr1["high"]; lo_a = arr1["low"]; cl_a = arr1["close"]
    hard_sl_pct = _pct(entry_price, hard_sl)
    risk   = abs(entry_price - hard_sl)
    qty    = (RISK_PER_TRADE / risk) if risk > 0 else 0.0
    invest = qty * entry_price
    targets  = {r: _tgt(entry_price, hard_sl, side, r) for r in ALL_RATIOS}
    cur_sl   = {r: hard_sl for r in ALL_RATIOS}
    sl_label = {r: "Hard SL" for r in ALL_RATIOS}
    sl_ref   = {r: f"Hard SL|{_f(hard_sl)}" for r in ALL_RATIOS}
    done     = {r: False for r in ALL_RATIOS}
    results  = {r: OrderedDict() for r in ALL_RATIOS}
    hit_cps  = set()

    def record_exit(r, status, px, ts, due=None):
        results[r] = OrderedDict({
            "SL Price": _f(cur_sl[r]), "Target Price": _f(targets[r]),
            "Exit Status": status, "Exit Price": _f(px), "Exit Datetime": _s(ts),
            "SL hit Due to": due,
            "SL Hit due to which Ratio trailing Price": sl_ref[r] if str(status).startswith("SL Hit") else None,
            "Holding Time (hrs)": round((ts - idx[entry_pos]).total_seconds() / 3600.0, 4),
            "P/L": _f(_pnl(px, entry_price, qty, side)),
        })
        done[r] = True

    def apply_trail(cp_ratio):
        if cp_ratio == 2:
            anchor_px = entry_price; anchor_lbl = "Breakeven"; anchor_ref = f"1:2|{_f(anchor_px)}"
        else:
            anchor_px = targets[cp_ratio - 2]; anchor_lbl = f"1:{cp_ratio-2} Target"
            anchor_ref = f"1:{cp_ratio}|{_f(anchor_px)}"
        for rr in RATIOS_FULL:
            if rr > cp_ratio and not done[rr]:
                cur_sl[rr] = anchor_px; sl_label[rr] = anchor_lbl; sl_ref[rr] = anchor_ref

    for pos in range(entry_pos + 1, len(idx)):
        ts = idx[pos]; hi = hi_a[pos]; lo = lo_a[pos]; cl = cl_a[pos]
        if not done[0.5]:
            hit_tgt = (hi >= targets[0.5]) if side == "buy" else (lo <= targets[0.5])
            hit_sl  = (cl <= cur_sl[0.5]) if side == "buy" else (cl >= cur_sl[0.5])
            if hit_tgt:  record_exit(0.5, "Target Hit", targets[0.5], ts)
            elif hit_sl: record_exit(0.5, f"SL Hit - {sl_label[0.5]}", cur_sl[0.5], ts, sl_label[0.5])
        for cp in TRAIL_CPS:
            hit_cp = (hi >= targets[cp]) if side == "buy" else (lo <= targets[cp])
            if cp not in hit_cps and hit_cp: hit_cps.add(cp); apply_trail(cp)
        for r in RATIOS_FULL:
            if done[r]: continue
            hit_tgt = (hi >= targets[r]) if side == "buy" else (lo <= targets[r])
            hit_sl  = (cl <= cur_sl[r]) if side == "buy" else (cl >= cur_sl[r])
            if hit_tgt:  record_exit(r, "Target Hit", targets[r], ts); continue
            if hit_sl:   record_exit(r, f"SL Hit - {sl_label[r]}", cur_sl[r], ts, sl_label[r])
        if all(done[r] for r in ALL_RATIOS): break

    for r in ALL_RATIOS:
        if not done[r]: record_exit(r, "Open", float(cl_a[-1]), idx[-1])

    head = OrderedDict({
        "Entry Price": _f(entry_price), "Hard SL Price": _f(hard_sl),
        "Assign Hard SL Percentage": _f(hard_sl_pct),
        "Qty": _f(qty), "Investment Value": _f(invest),
    })
    blocks = [head] + [OrderedDict({f"Ratio 1:{r}": results[r]}) for r in ALL_RATIOS]
    return results, vblocks(blocks), qty, invest


# ================================================================
# BASE ROW — VARIATION 1
# ================================================================

def base_row_v1(side: str) -> OrderedDict:
    r = OrderedDict(); tc = "Red" if side == "buy" else "Green"
    # ── 5min block ──────────────────────────────────────────────
    r[f"{tc} MACD cycle Startime"] = None
    r[f"{tc} MACD cycle Endtime"]  = None
    r["No. of Time key Candle got Updated Before obtaining final key candle"] = None
    r["Final Key Candle Datetime"] = None
    if side == "buy":
        r["Final Key Candle Highest kama Level obtained at key candle"] = None
        r["Candle close above key candle Highest Kama Level Datetime"] = None
        r["Candle close above Highest Kama Level close"] = None
        r["Candle close above Key candle Highest Kama Level Closes above Upper BB or not"] = None
        r["Candle close above key candle Highest Kama Level High"] = None
        r["Most High Updated after Highest KAMA Level"] = None
        r["Most High Updated  Value after Highest KAMA Level Value"] = None
        r["Most High Updated at candle Datetime"] = None
        r["No. of Time High got Updated before obtaining Final Check candle"] = None
        r["Highest high in 3 Candle - Buffer Period"] = None
    else:
        r["Final Key Candle Lowest kama Level obtained at key candle"] = None
        r["Candle close below key candle Lowest Kama Level Datetime"] = None
        r["Candle close below Lowest Kama Level close"] = None
        r["Candle close below Key candle Lowest Kama Level Closes below Lower BB or not"] = None
        r["Candle close below key candle Lowest Kama Level Low"] = None
        r["Most Low Updated after Lowest KAMA Level"] = None
        r["Most Low Updated  Value after Lowest KAMA Level Value"] = None
        r["Most Low Updated at candle Datetime"] = None
        r["No. of Time Low got Updated before obtaining Final Check candle"] = None
        r["Lowest low in 3 Candle - Buffer Period"] = None
    r["Final Strategy Candle Datetime"] = None
    # ── M1 columns ──────────────────────────────────────────────
    r["M1 - Backward Final Key Candle"] = None
    r["M1 - Highest Value obtained from Backward Candles"] = None
    r["M1 - Highest Value obtained from Final Key Candle to 5min candle mapped to 1min Datetime"] = None
    r["M1- Highest Value obtained from Backward Candle"] = None
    if side == "buy":
        r["M1  - Most Updated High Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Most Updated High DT after 5min candle mapped to 1min DT"] = None
        r["M1 - Most Updated High from Upper BB or High Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Entry found from"] = None
        r["M1 - Candle Close above Previous Updated high Datetime"] = None
        r["M1 - Candle Close above Previous Updated high Value"] = None
    else:
        r["M1  - Most Updated Low Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Most Updated Low DT after 5min candle mapped to 1min DT"] = None
        r["M1 - Most Updated Low from Lower BB or Low Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Entry found from"] = None
        r["M1 - Candle Close below Previous Updated low Datetime"] = None
        r["M1 - Candle Close below Previous Updated low Value"] = None
    # ── M2 columns ──────────────────────────────────────────────
    r["M2-Final Key Candle found Datetime on 1min"] = None
    if side == "buy":
        r["M2-Highest Value at Key candle on 1min"] = None
        r["M2-Highest Value at Key candle from Level on 1min"] = None
        r["M2-Candle close above the Highesh level obtained from key candle"] = None
        r["M2-Candle close above the Highesh level obtained from key candle  Datetime"] = None
    else:
        r["M2-Lowest Value at Key candle on 1min"] = None
        r["M2-Lowest Value at Key candle from Level on 1min"] = None
        r["M2-Candle close below the Lowest level obtained from key candle"] = None
        r["M2-Candle close below the Lowest level obtained from key candle  Datetime"] = None
    r["Method 2 Entry"] = None
    r["Hard SL consider from : kama Levels or candle Low" if side == "buy"
      else "Hard SL consider from : kama Levels or candle High"] = None
    r["5min Strategy Final candle Price"] = None
    r["Status"] = None
    # ── 9 EMA context columns (after Status) ────────────────────
    r["2hrs Price above/Below EMA 50"]  = None
    r["2hrs Price above/Below EMA 100"] = None
    r["2hrs Price above/Below EMA 200"] = None
    r["4hrs Price above/Below EMA 50"]  = None
    r["4hrs Price above/Below EMA 100"] = None
    r["4hrs Price above/Below EMA 200"] = None
    r["1D Price above/Below EMA 50"]    = None
    r["1D Price above/Below EMA 100"]   = None
    r["1D Price above/Below EMA 200"]   = None
    # ── final winner + trade ────────────────────────────────────
    r["Final Buy found from which Method" if side == "buy"
      else "Final Sell found from which Method"] = None
    r["Entry Datetime"] = None; r["Entry Price"] = None
    r["0.5 Target Price a/c Actual Entry Price"] = None
    r["0.5 SL Price a/c Actual Entry Price"] = None
    r["0.5 Target Price/ SL Price achieved DT"] = None
    r["Final Entry Price"] = None; r["Final Entry Date"] = None
    r["Hard SL Price"]  = None; r["Assign Hard SL Percentage"] = None
    r["Qty"] = None; r["Investment Value for Ratios"] = None
    # ── ratio columns ───────────────────────────────────────────
    r["1:0.5 Exit Datetime"] = None; r["1:0.5 Exit Price"] = None
    r["1:0.5 SL hit Due to"] = None; r["1:0.5 Holding Time (hrs)"] = None
    r["P/L 1:0.5"] = None
    for ratio in RATIOS_FULL:
        r[f"1:{ratio} Exit Datetime"] = None; r[f"1:{ratio} Exit Price"] = None
        r[f"1:{ratio} SL hit Due to"] = None; r[f"1:{ratio} Holding Time (hrs)"] = None
        r[f"P/L 1:{ratio}"] = None
        if ratio >= 2: r[f"Status 1:{ratio}"] = None
    # ── volume ──────────────────────────────────────────────────
    for _, col in VOL_COL_MAP.items(): r[col] = None
    # ── info ────────────────────────────────────────────────────
    r["Strategy Additional info"] = None
    r["Method 1 Additional info"] = None
    r["Method 2 Additional info"] = None
    r["BackTest Result"] = None
    return r


# ================================================================
# FILL 5MIN BLOCK
# ================================================================

def _fill_5m_block(row, setup5, side):
    tc = "Red" if side == "buy" else "Green"
    row[f"{tc} MACD cycle Startime"] = _s(setup5["cycle_start_ts"])
    row[f"{tc} MACD cycle Endtime"]  = _s(setup5["cycle_end_ts"])
    row["No. of Time key Candle got Updated Before obtaining final key candle"] = setup5["key_update_count"]
    row["Final Key Candle Datetime"]        = _s(setup5["key_ts"])
    row["Final Strategy Candle Datetime"]   = _s(setup5["fcc_ts"])
    row["5min Strategy Final candle Price"] = _f(setup5.get("fcc_close"))
    if side == "buy":
        row["Final Key Candle Highest kama Level obtained at key candle"] = setup5["ek_name"]
        row["Candle close above key candle Highest Kama Level Datetime"] = _s(setup5.get("fcc_ts"))
        row["Candle close above Highest Kama Level close"] = _f(setup5.get("fcc_close"))
        row["Candle close above Key candle Highest Kama Level Closes above Upper BB or not"] = (
            "Yes" if setup5.get("fcc_above_bb") else "No")
        row["Candle close above key candle Highest Kama Level High"] = _f(setup5.get("fcc_hi_or_lo"))
        row["Most High Updated after Highest KAMA Level"] = (
            "Yes" if setup5.get("high_update_count", 0) > 0 else "No")
        if setup5.get("high_update_history"):
            row["Most High Updated  Value after Highest KAMA Level Value"] = setup5["high_update_history"][-1]["val"]
            row["Most High Updated at candle Datetime"] = setup5["high_update_history"][-1]["ts"]
        row["No. of Time High got Updated before obtaining Final Check candle"] = setup5.get("high_update_count")
    else:
        row["Final Key Candle Lowest kama Level obtained at key candle"] = setup5["ek_name"]
        row["Candle close below key candle Lowest Kama Level Datetime"] = _s(setup5.get("fcc_ts"))
        row["Candle close below Lowest Kama Level close"] = _f(setup5.get("fcc_close"))
        row["Candle close below Key candle Lowest Kama Level Closes below Lower BB or not"] = (
            "Yes" if setup5.get("fcc_above_bb") else "No")
        row["Candle close below key candle Lowest Kama Level Low"] = _f(setup5.get("fcc_hi_or_lo"))
        row["Most Low Updated after Lowest KAMA Level"] = (
            "Yes" if setup5.get("high_update_count", 0) > 0 else "No")
        if setup5.get("high_update_history"):
            row["Most Low Updated  Value after Lowest KAMA Level Value"] = setup5["high_update_history"][-1]["val"]
            row["Most Low Updated at candle Datetime"] = setup5["high_update_history"][-1]["ts"]
        row["No. of Time Low got Updated before obtaining Final Check candle"] = setup5.get("high_update_count")


# ================================================================
# ADDITIONAL INFO BUILDERS
# ================================================================

def build_strategy_additional_info_v1(setup5, side):
    add = OrderedDict(); tc = "Red" if side == "buy" else "Green"
    add[f"{tc} MACD cycle Startime"] = _s(setup5.get("cycle_start_ts"))
    add[f"{tc} MACD cycle Endtime"]  = _s(setup5.get("cycle_end_ts"))
    for idxh, hist in enumerate(setup5.get("key_history", [])):
        ord_n = idxh + 1; suf = {1: "1st", 2: "2nd", 3: "3rd"}.get(ord_n, f"{ord_n}th")
        pfx = f"{suf} Key Candle"
        add[f"{pfx} Datetime"] = hist.get("key_ts")
        add[f"{pfx} {'Highest' if side=='buy' else 'Lowest'} kama Level obtained at key candle"] = hist.get("ek_name")
        kvh = hist.get("kv_at_key", {})
        add[f"For {pfx}"] = OrderedDict({
            "from kama_ma_dataset.py - SMA Value at Key Candle": _f(kvh.get(K_SMA)),
            "KAMAEMA Value - At Key candle": _f(kvh.get(K_EMAKAMA)),
            "from kama lines code.py-Kama Line Value - At Key Candle": _f(kvh.get(K_KLINE)),
        })
        nxt = (setup5["key_history"][idxh + 1]["key_ts"]
               if idxh + 1 < len(setup5["key_history"])
               else _s(setup5.get("key_ts")))
        add[f"Invalidated due to new key candle found at datetime for {pfx}"] = nxt
    fo = setup5.get("key_update_count", 0) + 1
    fs = {1: "1st", 2: "2nd", 3: "3rd"}.get(fo, f"{fo}th")
    add[f"{fs} Key Candle Datetime Final"] = _s(setup5.get("key_ts"))
    add[f"{fs} {'Highest' if side=='buy' else 'Lowest'} kama Level obtained at key candle Final"] = setup5.get("ek_name")
    kvf = setup5.get("kv_at_key", {})
    add[f"For {fs} Key Candle Final"] = OrderedDict({
        "from kama_ma_dataset.py - SMA Value at Key Candle": _f(kvf.get(K_SMA)),
        "KAMAEMA Value - At Key candle": _f(kvf.get(K_EMAKAMA)),
        "from kama lines code.py-Kama Line Value - At Key Candle": _f(kvf.get(K_KLINE)),
    })
    if setup5.get("fcc_ts") is not None:
        if side == "buy":
            add["Candle close above key candle Highest Kama Level Datetime"] = _s(setup5.get("fcc_ts"))
            add["Candle close above Highest KAMA Level Close"] = _f(setup5.get("fcc_close"))
            add["Most High Updated after Highest KAMA Level"] = (
                "Yes" if setup5.get("high_update_count", 0) > 0 else "No")
            if setup5.get("high_update_history"):
                last = setup5["high_update_history"][-1]
                add["Most High Updated Value"] = last.get("val")
                add["Most High Updated at candle Datetime"] = last.get("ts")
            add["No. of Time High got Updated"] = setup5.get("high_update_count")
        else:
            add["Candle close below key candle Lowest Kama Level Datetime"] = _s(setup5.get("fcc_ts"))
            add["Candle close below Lowest KAMA Level Close"] = _f(setup5.get("fcc_close"))
            add["Most Low Updated after Lowest KAMA Level"] = (
                "Yes" if setup5.get("high_update_count", 0) > 0 else "No")
            if setup5.get("high_update_history"):
                last = setup5["high_update_history"][-1]
                add["Most Low Updated Value"] = last.get("val")
                add["Most Low Updated at candle Datetime"] = last.get("ts")
            add["No. of Time Low got Updated"] = setup5.get("high_update_count")
        add["Final Strategy Candle Datetime"] = _s(setup5.get("fcc_ts"))
        add["Final Strategy Candle Close"]    = _f(setup5.get("fcc_close"))
    for k, v in setup5.get("inv_extra", OrderedDict()).items(): add[k] = v
    return vkv(add)

def build_m1_additional_info_v1(setup5, mapped_ts, backward, m1_method, sl_info, side, status):
    add = OrderedDict(); tc = "Red" if side == "buy" else "Green"
    add[f"final {'buy' if side=='buy' else 'sell'} of Strategy Datetime"] = _s(setup5.get("fcc_ts"))
    add["5min candle mapped to 1min Datetime"] = _s(mapped_ts)
    add["For Method 1"] = ""
    add["Previous Setup finding start time"]   = _s(setup5["cycle_start_ts"])
    add["Previous Setup finding endtime time"] = _s(mapped_ts)
    if backward is None:
        add[f"Final Setup found on 1min in Previous {tc} MACD cycle Details"] = "None"
        add["Invalidation Reason"] = "Invalidated due to setup not found in setup finding zone"
        add["Method Status"] = status
        return vkv(add)
    add[f"Final Setup found on 1min in Previous {tc} MACD cycle Details"] = ""
    add[f"Previous {tc} MACD cycle Startime"] = _s(backward["cycle_start_ts"])
    add[f"Previous {tc} MACD cycle Endtime"]  = _s(backward["cycle_end_ts"])
    add["Final Key Candle found Datetime"] = _s(backward["key_ts"])
    add[f"from which kama level {'Highest' if side=='buy' else 'Lowest'} kama Level obtained"] = backward["ek_name"]
    add["from kama_ma_dataset.py - SMA Value at Key Candle"] = _f(backward["kv_at_key"].get(K_SMA))
    add["KAMAEMA Value - At Key candle"] = _f(backward["kv_at_key"].get(K_EMAKAMA))
    add["from kama lines code.py-Kama Line Value - At Key Candle"] = _f(backward["kv_at_key"].get(K_KLINE))
    m = m1_method or {}
    if side == "buy":
        add["Key candle UPPER BB Value"] = m.get("key_bb_value")
        add["Key candle High Value"] = m.get("key_high_low_value")
        add["Highest Value obtained at key candle"] = m.get("key_best_value_type")
        add["Highest high Value from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_candle_value_range")
        add["Highest high Value Candle DT from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_candle_dt_range")
        add["Highest Upper BB Value from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_bb_value_range")
        add["Highest Upper BB Value Candle DT from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_bb_dt_range")
        add["Highest Value obtained from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("backward_threshold_src")
        add["Highest Value obtained from Backward Candle"] = m.get("backward_threshold")
        add["Most Updated High Value after 5min candle mapped to 1min Datetime"] = m.get("most_updated_val")
        add["Most Updated High Datetime after 5min candle mapped to 1min Datetime"] = m.get("most_updated_ts")
        add["Most Updated High from Upper BB or High Value after 5min candle mapped to 1min Datetime"] = m.get("most_updated_src")
        add["After 1min DT if high got updated or not"] = m.get("after_mapped_updated")
        add["M1 - Entry found from"] = m.get("entry_from")
        add["M1 - Candle Close above Previous Updated high Datetime"] = m.get("close_above_prev_dt")
        add["M1 - Candle Close above Previous Updated high Value"] = m.get("close_above_prev_val")
        if sl_info:
            add["previous 5 candle Lowest Low Kama Level"] = sl_info.get("prev5_kama_extreme")
            add["Previous 5 candle Lowest Low Kama Level obtained at which candle DT"] = sl_info.get("prev5_kama_dt")
            add["Previous 5 candle lowest Low Value"] = sl_info.get("prev5_candle_extreme")
            add["previous 5 candle Lowest Low value DT"] = sl_info.get("prev5_candle_dt")
    else:
        add["Key candle LOWER BB Value"] = m.get("key_bb_value")
        add["Key candle Low Value"] = m.get("key_high_low_value")
        add["Lowest Value obtained at key candle"] = m.get("key_best_value_type")
        add["Lowest low Value from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_candle_value_range")
        add["Lowest low Value Candle DT from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_candle_dt_range")
        add["Lowest Lower BB Value from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_bb_value_range")
        add["Lowest Lower BB Value Candle DT from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("highest_bb_dt_range")
        add["Lowest Value obtained from Final Key Candle to 5min candle mapped to 1min Datetime"] = m.get("backward_threshold_src")
        add["Lowest Value obtained from Backward Candle"] = m.get("backward_threshold")
        add["Most Updated Low Value after 5min candle mapped to 1min Datetime"] = m.get("most_updated_val")
        add["Most Updated Low Datetime after 5min candle mapped to 1min Datetime"] = m.get("most_updated_ts")
        add["Most Updated Low from Lower BB or Low Value after 5min candle mapped to 1min Datetime"] = m.get("most_updated_src")
        add["After 1min DT if low got updated or not"] = m.get("after_mapped_updated")
        add["M1 - Entry found from"] = m.get("entry_from")
        add["M1 - Candle Close below Previous Updated low Datetime"] = m.get("close_above_prev_dt")
        add["M1 - Candle Close below Previous Updated low Value"] = m.get("close_above_prev_val")
        if sl_info:
            add["previous 5 candle Highest High Kama Level"] = sl_info.get("prev5_kama_extreme")
            add["Previous 5 candle Highest High Kama Level obtained at which candle DT"] = sl_info.get("prev5_kama_dt")
            add["Previous 5 candle Highest High Value"] = sl_info.get("prev5_candle_extreme")
            add["previous 5 candle Highest High value DT"] = sl_info.get("prev5_candle_dt")
    if sl_info:
        add["hard SL consider from"] = sl_info.get("hard_sl_from")
        add["hard SL Value"] = sl_info.get("hard_sl")
    add["Method Status"] = status
    return vkv(add)

def build_m2_additional_info_v1(setup5, mapped_ts, m2_result, sl_info, side, status):
    add = OrderedDict()
    add[f"final {'buy' if side=='buy' else 'sell'} of Strategy Datetime"] = _s(setup5.get("fcc_ts"))
    add["5min candle mapped to 1min Datetime"] = _s(mapped_ts)
    for i, blk in enumerate(m2_result.get("invalidated", []), start=1):
        if i == 1: add["Invalidated setup key candles on 1min"] = ""
        add[f"{i}. Invalidated Setup"] = blk
    fs = m2_result.get("final")
    if fs is not None:
        add["Final Key Candle found Datetime"] = _s(fs.get("key_ts"))
        add[f"from which kama level or ema 50 {'Highest' if side=='buy' else 'Lowest'} value Level"] = fs.get("level_name")
        add[f"{'Highest' if side=='buy' else 'Lowest'} Value Level obtained at Final key candle"] = _f(fs.get("level_value"))
        lv = fs.get("levels", {})
        add["from kama_ma_dataset.py - SMA Value at Key Candle"] = _f(lv.get(K_SMA))
        add["KAMAEMA Value - At Key candle"] = _f(lv.get(K_EMAKAMA))
        add["from kama lines code.py-Kama Line Value - At Key Candle"] = _f(lv.get(K_KLINE))
        add["EMA 50 value at key candle"] = _f(lv.get(K_EMA50))
        if side == "buy":
            add["Candle close above the Highesh level obtained from key candle"] = _f(fs.get("break_close"))
            add["Candle close above the Highesh level obtained from key candle  Datetime"] = _s(fs.get("break_ts"))
        else:
            add["Candle close below the Lowest level obtained from key candle"] = _f(fs.get("break_close"))
            add["Candle close below the Lowest level obtained from key candle  Datetime"] = _s(fs.get("break_ts"))
        add["Method 2 Entry"] = fs.get("scenario")
        if fs.get("scenario") == "Scenario 2":
            if side == "buy": add["Highest high in 3 Candle - Buffer Period"] = _f(fs.get("buffer_extreme"))
            else:             add["Lowest low in 3 Candle - Buffer Period"]   = _f(fs.get("buffer_extreme"))
        if sl_info:
            if side == "buy":
                add["previous 5 candle Lowest Low Kama Level"] = sl_info.get("prev5_kama_extreme")
                add["Previous 5 candle Lowest Low Kama Level obtained at which candle DT"] = sl_info.get("prev5_kama_dt")
                add["Previous 5 candle lowest Low Value"] = sl_info.get("prev5_candle_extreme")
                add["previous 5 candle Lowest Low value DT"] = sl_info.get("prev5_candle_dt")
            else:
                add["previous 5 candle Highest High Kama Level"] = sl_info.get("prev5_kama_extreme")
                add["Previous 5 candle Highest High Kama Level obtained at which candle DT"] = sl_info.get("prev5_kama_dt")
                add["Previous 5 candle Highest High Value"] = sl_info.get("prev5_candle_extreme")
                add["previous 5 candle Highest High value DT"] = sl_info.get("prev5_candle_dt")
    if sl_info:
        add["hard SL consider from"] = sl_info.get("hard_sl_from")
        add["hard SL Value"] = sl_info.get("hard_sl")
    add["Method Status"] = status
    return vkv(add)


# ================================================================
# PROCESS ONE 5MIN SETUP → ONE ROW
# ================================================================

def process_variation1_setup(
    setup5, arr5, arr1, cycles1,
    volcalc1: VolumeCalculator,
    arr2h: Dict[str, Any],
    arr4h: Dict[str, Any],
    arr1d: Dict[str, Any],
    side: str
) -> OrderedDict:

    row = base_row_v1(side)
    _fill_5m_block(row, setup5, side)
    row["Strategy Additional info"] = build_strategy_additional_info_v1(setup5, side)

    if setup5["status"] != "Final Strategy Candle Found":
        if "Forming" in setup5["status"]:
            row["Status"] = setup5["status"]
            return row
        row["Status"] = setup5["status"]
        return row

    # ── BUG-FIX: mapped 1min candle = fcc_ts + 5min ─────────────
    # The 5-min candle labelled fcc_ts closes at fcc_ts + 4:59.
    # The first 1min candle after the 5min close is fcc_ts + 5min.
    mapped_ts  = pd.Timestamp(setup5["fcc_ts"]) + pd.Timedelta(minutes=5)
    mapped_pos = int(arr1["idx"].searchsorted(mapped_ts))
    if mapped_pos >= len(arr1["idx"]):
        row["Status"] = "Invalidated due to mapped 1min candle not found"
        return row

    lower_bound_pos = int(arr1["idx"].searchsorted(setup5["cycle_start_ts"]))
    sl_info = compute_hard_sl_from_5m_window(arr5, setup5["fcc_pos"], side)
    hard_sl = float(sl_info["hard_sl"])

    # ── RUN M1 ───────────────────────────────────────────────────
    backward  = find_backward_setup_on_1m_nearest(arr1, cycles1, mapped_pos, lower_bound_pos, side)
    m1_method = (find_method1_entry_fast(arr1, mapped_pos, backward, side)
                 if backward else {"found": False})

    # ── RUN M2 ───────────────────────────────────────────────────
    m2_result = find_method2_on_1m(arr1, mapped_pos, side)
    m2_final  = m2_result.get("final")

    # ── FILL M1 COLUMNS ──────────────────────────────────────────
    if backward:
        row["M1 - Backward Final Key Candle"] = _s(backward["key_ts"])
        row["M1 - Highest Value obtained from Backward Candles"] = m1_method.get("backward_threshold")
        row["M1 - Highest Value obtained from Final Key Candle to 5min candle mapped to 1min Datetime"] = m1_method.get("backward_threshold_src")
        row["M1- Highest Value obtained from Backward Candle"] = m1_method.get("backward_threshold")
        if side == "buy":
            row["M1  - Most Updated High Value after 5min candle mapped to 1min DT"] = m1_method.get("most_updated_val")
            row["M1 - Most Updated High DT after 5min candle mapped to 1min DT"]     = m1_method.get("most_updated_ts")
            row["M1 - Most Updated High from Upper BB or High Value after 5min candle mapped to 1min DT"] = m1_method.get("most_updated_src")
            row["M1 - Entry found from"] = m1_method.get("entry_from")
            row["M1 - Candle Close above Previous Updated high Datetime"] = m1_method.get("close_above_prev_dt")
            row["M1 - Candle Close above Previous Updated high Value"]    = m1_method.get("close_above_prev_val")
        else:
            row["M1  - Most Updated Low Value after 5min candle mapped to 1min DT"] = m1_method.get("most_updated_val")
            row["M1 - Most Updated Low DT after 5min candle mapped to 1min DT"]     = m1_method.get("most_updated_ts")
            row["M1 - Most Updated Low from Lower BB or Low Value after 5min candle mapped to 1min DT"] = m1_method.get("most_updated_src")
            row["M1 - Entry found from"] = m1_method.get("entry_from")
            row["M1 - Candle Close below Previous Updated low Datetime"] = m1_method.get("close_above_prev_dt")
            row["M1 - Candle Close below Previous Updated low Value"]    = m1_method.get("close_above_prev_val")

    # ── FILL M2 COLUMNS ──────────────────────────────────────────
    if m2_final is not None:
        row["M2-Final Key Candle found Datetime on 1min"] = _s(m2_final.get("key_ts"))
        if side == "buy":
            row["M2-Highest Value at Key candle on 1min"]            = _f(m2_final.get("level_value"))
            row["M2-Highest Value at Key candle from Level on 1min"]  = m2_final.get("level_name")
            row["M2-Candle close above the Highesh level obtained from key candle"] = _f(m2_final.get("break_close"))
            row["M2-Candle close above the Highesh level obtained from key candle  Datetime"] = _s(m2_final.get("break_ts"))
        else:
            row["M2-Lowest Value at Key candle on 1min"]             = _f(m2_final.get("level_value"))
            row["M2-Lowest Value at Key candle from Level on 1min"]   = m2_final.get("level_name")
            row["M2-Candle close below the Lowest level obtained from key candle"] = _f(m2_final.get("break_close"))
            row["M2-Candle close below the Lowest level obtained from key candle  Datetime"] = _s(m2_final.get("break_ts"))
        row["Method 2 Entry"] = m2_final.get("scenario")
        if m2_final.get("scenario") == "Scenario 2":
            buf_key = ("Highest high in 3 Candle - Buffer Period" if side == "buy"
                       else "Lowest low in 3 Candle - Buffer Period")
            row[buf_key] = _f(m2_final.get("buffer_extreme"))

    # ── HARD SL COLUMN ───────────────────────────────────────────
    hsl_col = ("Hard SL consider from : kama Levels or candle Low" if side == "buy"
               else "Hard SL consider from : kama Levels or candle High")
    row[hsl_col] = sl_info["hard_sl_from"]

    # ── DETERMINE WINNER ─────────────────────────────────────────
    m1_entry_ts: Optional[pd.Timestamp] = None
    m2_entry_ts: Optional[pd.Timestamp] = None
    if m1_method.get("found") and m1_method.get("entry_ts") is not None:
        m1_entry_ts = pd.Timestamp(m1_method["entry_ts"])
    if m2_result.get("found") and m2_final and m2_final.get("entry_ts") is not None:
        m2_entry_ts = pd.Timestamp(m2_final["entry_ts"])

    if m1_entry_ts is not None and m2_entry_ts is not None:
        winner = "M1" if m1_entry_ts <= m2_entry_ts else "M2"
    elif m1_entry_ts is not None: winner = "M1"
    elif m2_entry_ts is not None: winner = "M2"
    else: winner = None

    def _m1_status(w):
        if w == "M1": return "Intrade"
        return ("Invalidated due to setup not found in setup finding zone"
                if backward is None else "Invalidated - method 1 entry not found")

    def _m2_status(w):
        if w == "M2": return "Intrade"
        return m2_result.get("status", "Invalidated due to Method 2 entry not found")

    if winner is None:
        row["Status"]     = "Forming - waiting for M1/M2 entry"
        row["Hard SL Price"] = _f(hard_sl)
        row["Method 1 Additional info"] = build_m1_additional_info_v1(
            setup5, mapped_ts, backward, m1_method, sl_info, side, _m1_status(None))
        row["Method 2 Additional info"] = build_m2_additional_info_v1(
            setup5, mapped_ts, m2_result, sl_info, side, _m2_status(None))
        return row

    fin_key = ("Final Buy found from which Method" if side == "buy"
               else "Final Sell found from which Method")
    row[fin_key]    = winner
    entry_ts        = m1_entry_ts if winner == "M1" else m2_entry_ts
    entry_price     = float(m1_method["entry_price"]) if winner == "M1" else float(m2_final["entry_price"])
    entry_pos       = int(arr1["idx"].searchsorted(entry_ts))

    row["Entry Datetime"] = _s(entry_ts)
    row["Entry Price"]    = _f(entry_price)
    row["Hard SL Price"]  = _f(hard_sl)
    row["Assign Hard SL Percentage"] = _f(_pct(entry_price, hard_sl))

    # ── SL VALIDATION ────────────────────────────────────────────
    invalid_vs_sl = ((side == "buy"  and entry_price <= hard_sl) or
                     (side == "sell" and entry_price >= hard_sl))
    if invalid_vs_sl:
        row["Status"] = ("Invalidated due final buy on 1 min is below SL value"
                         if side == "buy"
                         else "Invalidated due final sell on 1 min is above SL value")
        row["Method 1 Additional info"] = build_m1_additional_info_v1(
            setup5, mapped_ts, backward, m1_method, sl_info, side, _m1_status(winner))
        row["Method 2 Additional info"] = build_m2_additional_info_v1(
            setup5, mapped_ts, m2_result, sl_info, side, _m2_status(winner))
        return row

    row["Status"] = "Intrade"

    # ── 9 EMA CONTEXT COLUMNS ────────────────────────────────────
    # Check previous completed candle on 2H / 4H / 1D vs EMA 50/100/200
    for arr_tf, prefix in [(arr2h, "2hrs"), (arr4h, "4hrs"), (arr1d, "1D")]:
        ema_chk = check_ema_position(arr_tf, entry_ts)
        row[f"{prefix} Price above/Below EMA 50"]  = ema_chk["ema50"]
        row[f"{prefix} Price above/Below EMA 100"] = ema_chk["ema100"]
        row[f"{prefix} Price above/Below EMA 200"] = ema_chk["ema200"]

    # ── BACKTEST ─────────────────────────────────────────────────
    ratio_res, bt_text, qty, invest = run_backtest_v1(arr1, entry_pos, entry_price, hard_sl, side)
    row["Qty"] = _f(qty); row["Investment Value for Ratios"] = _f(invest)

    b05 = ratio_res[0.5]
    row["1:0.5 Exit Datetime"]      = b05.get("Exit Datetime")
    row["1:0.5 Exit Price"]         = b05.get("Exit Price")
    row["1:0.5 SL hit Due to"]      = b05.get("SL hit Due to")
    row["1:0.5 Holding Time (hrs)"] = b05.get("Holding Time (hrs)")
    row["P/L 1:0.5"]                = b05.get("P/L")

    for r_ in RATIOS_FULL:
        rr = ratio_res[r_]
        row[f"1:{r_} Exit Datetime"]      = rr.get("Exit Datetime")
        row[f"1:{r_} Exit Price"]         = rr.get("Exit Price")
        row[f"1:{r_} SL hit Due to"]      = rr.get("SL hit Due to")
        row[f"1:{r_} Holding Time (hrs)"] = rr.get("Holding Time (hrs)")
        row[f"P/L 1:{r_}"]               = rr.get("P/L")
        if r_ >= 2: row[f"Status 1:{r_}"] = rr.get("Exit Status")

    # ── VOLUME ───────────────────────────────────────────────────
    vols = volcalc1.ratios(entry_ts)
    for k, col in VOL_COL_MAP.items(): row[col] = vols.get(k, 0.0)

    # ── ADDITIONAL INFO ──────────────────────────────────────────
    row["Method 1 Additional info"] = build_m1_additional_info_v1(
        setup5, mapped_ts, backward, m1_method, sl_info, side, _m1_status(winner))
    row["Method 2 Additional info"] = build_m2_additional_info_v1(
        setup5, mapped_ts, m2_result, sl_info, side, _m2_status(winner))
    row["BackTest Result"] = bt_text
    row["_arr1"] = arr1
    row["_entry_pos"] = entry_pos
    row["_side"] = side
    return row



# ================================================================
# VARIATION 4 DELAYED ENTRY
# ================================================================

def apply_variation_4_logic(row, arr1, arr5, arr2h, arr4h, arr1d, volcalc1, side):
    entry_price = float(row["Entry Price"])
    hard_sl = float(row["Hard SL Price"])
    target_0_5 = _tgt(entry_price, hard_sl, side, 0.5)
    
    row["0.5 Target Price a/c Actual Entry Price"] = _f(target_0_5)
    row["0.5 SL Price a/c Actual Entry Price"] = _f(hard_sl)
    
    entry_ts = pd.Timestamp(row["Entry Datetime"])
    entry_pos = int(arr1["idx"].searchsorted(entry_ts))
    
    final_entry_ts = None
    final_entry_price = None
    achieved_dt = None
    delayed_status = None
    
    for pos in range(entry_pos + 1, len(arr1["idx"])):
        ts = arr1["idx"][pos]
        hi = arr1["high"][pos]
        lo = arr1["low"][pos]
        cl = arr1["close"][pos]
        
        hit_tgt = (hi >= target_0_5) if side == "buy" else (lo <= target_0_5)
        hit_sl  = (cl <= hard_sl) if side == "buy" else (cl >= hard_sl)
        
        if hit_tgt:
            achieved_dt = ts
            final_entry_ts = ts
            final_entry_price = float(cl)
            break
        elif hit_sl:
            achieved_dt = ts
            delayed_status = "Invalidated due to 0.5 Target Price did not achieved"
            break

    if final_entry_ts is None:
        if delayed_status is None:
            delayed_status = "Intrade"
        row["Status"] = delayed_status
        row["0.5 Target Price/ SL Price achieved DT"] = _s(achieved_dt)
        
        # Clear the initial backtest/ratio results that V1 populated, since it's now invalidated
        row["Qty"] = None
        row["Investment Value for Ratios"] = None
        row["BackTest Result"] = None
        for fld in ["1:0.5 Exit Datetime", "1:0.5 Exit Price", "1:0.5 SL hit Due to", "1:0.5 Holding Time (hrs)", "P/L 1:0.5"]:
            row[fld] = None
        for r_ in RATIOS_FULL:
            row[f"1:{r_} Exit Datetime"] = None
            row[f"1:{r_} Exit Price"] = None
            row[f"1:{r_} SL hit Due to"] = None
            row[f"1:{r_} Holding Time (hrs)"] = None
            row[f"P/L 1:{r_}"] = None
            if r_ >= 2: row[f"Status 1:{r_}"] = None
        return

    # Delay reached Target!
    row["Status"] = "Intrade"
    row["0.5 Target Price/ SL Price achieved DT"] = _s(achieved_dt)
    row["Final Entry Price"] = _f(final_entry_price)
    row["Final Entry Date"]  = _s(final_entry_ts)
    final_entry_pos = int(arr1["idx"].searchsorted(final_entry_ts))

    # Recalculate EMA with final_entry_ts
    for arr_tf, prefix in [(arr2h, "2hrs"), (arr4h, "4hrs"), (arr1d, "1D")]:
        ema_chk = check_ema_position(arr_tf, final_entry_ts)
        row[f"{prefix} Price above/Below EMA 50"]  = ema_chk["ema50"]
        row[f"{prefix} Price above/Below EMA 100"] = ema_chk["ema100"]
        row[f"{prefix} Price above/Below EMA 200"] = ema_chk["ema200"]

    # Recalculate Backtest with final_entry_price and final_entry_pos
    ratio_res, bt_text, qty, invest = run_backtest_v1(arr1, final_entry_pos, final_entry_price, hard_sl, side)
    row["Qty"] = _f(qty); row["Investment Value for Ratios"] = _f(invest)

    b05 = ratio_res[0.5]
    row["1:0.5 Exit Datetime"]      = b05.get("Exit Datetime")
    row["1:0.5 Exit Price"]         = b05.get("Exit Price")
    row["1:0.5 SL hit Due to"]      = b05.get("SL hit Due to")
    row["1:0.5 Holding Time (hrs)"] = b05.get("Holding Time (hrs)")
    row["P/L 1:0.5"]                = b05.get("P/L")

    for r_ in RATIOS_FULL:
        rr = ratio_res[r_]
        row[f"1:{r_} Exit Datetime"]      = rr.get("Exit Datetime")
        row[f"1:{r_} Exit Price"]         = rr.get("Exit Price")
        row[f"1:{r_} SL hit Due to"]      = rr.get("SL hit Due to")
        row[f"1:{r_} Holding Time (hrs)"] = rr.get("Holding Time (hrs)")
        row[f"P/L 1:{r_}"]               = rr.get("P/L")
        if r_ >= 2: row[f"Status 1:{r_}"] = rr.get("Exit Status")

    # Recalculate Volumes
    vols = volcalc1.ratios(final_entry_ts)
    for k, col in VOL_COL_MAP.items(): row[col] = vols.get(k, 0.0)

    # Append Insights to Strategy Additional Info
    strat_info = row.get("Strategy Additional info", "")
    insights = OrderedDict()
    insights["Final Entry Datetime on 1min"] = _s(final_entry_ts)
    
    def map_dt(arr_tf, ts):
        if not ts: return None
        idx = arr_tf["idx"]
        pos = int(idx.searchsorted(ts, side="right")) - 1
        if pos >= 0 and pos < len(idx): return _s(idx[pos])
        return None
        
    insights["Final Entry DT Mapped to 5min DT"] = map_dt(arr5, final_entry_ts)
    insights["Final Entry DT Mapped to 2hrs DT"] = map_dt(arr2h, final_entry_ts)
    insights["Final Entry DT Mapped to 4hrs DT"] = map_dt(arr4h, final_entry_ts)
    insights["Final Entry DT Mapped to 1Day DT"] = map_dt(arr1d, final_entry_ts)
    
    # Remove old Insights block if present (from previous runs)
    if "\n\nInsights:\n" in strat_info:
        strat_info = strat_info.split("\n\nInsights:\n")[0]
    
    strat_info += "\n\nInsights:\n" + vkv(insights)
    row["Strategy Additional info"] = strat_info
    row["BackTest Result"] = bt_text
    row["_arr1"] = arr1
    row["_entry_pos"] = entry_pos
    row["_side"] = side


# ================================================================
# ENGINE — VARIATION 1
# ================================================================

def run_strategy17_variation1(df5, df1, volcalc1, arr2h, arr4h, arr1d, side):
    arr5    = make_fast_arrays(df5)
    arr1    = make_fast_arrays(df1)
    cycles1 = build_cycle_map(arr1)
    setups5 = scan_5m_final_strategy_candles(df5, side)

    rows: List[OrderedDict] = []
    last_print_date = None

    for s5 in setups5:
        dt = s5.get("fcc_ts") or s5.get("cycle_end_ts")
        d  = pd.Timestamp(dt).date() if dt is not None else None
        if d is not None and d != last_print_date:
            log(f"[VAR1 {side.upper()}] Processing Final Strategy Candle Date: {d}")
            last_print_date = d
        rows.append(
            process_variation1_setup(s5, arr5, arr1, cycles1,
                                     volcalc1, arr2h, arr4h, arr1d, side)
        )

    # ── DEDUP: same Entry Datetime → keep newest MACD cycle ──────
    entry_map: Dict[str, List[int]] = {}
    tc = "Red" if side == "buy" else "Green"
    for i, row in enumerate(rows):
        edt = row.get("Entry Datetime")
        if edt and row.get("Status") == "Intrade":
            entry_map.setdefault(str(edt), []).append(i)

    for edt, idxs in entry_map.items():
        if len(idxs) < 2: continue
        idxs_sorted = sorted(
            idxs, key=lambda i: str(rows[i].get(f"{tc} MACD cycle Startime") or ""))
        newest = idxs_sorted[-1]
        for old_i in idxs_sorted[:-1]:
            old = rows[old_i]; new = rows[newest]
            old["Status"] = (f"Invalidated due to {'Buy' if side=='buy' else 'Sell'}"
                             " occured at same Datetime")
            extra = OrderedDict({
                "Invalidation Reason": old["Status"],
                f"{tc} MACD CYCLE start time due to which setup invalidated": new.get(f"{tc} MACD cycle Startime"),
                f"{tc} MACD CYCLE endtime time due to which setup invalidated": new.get(f"{tc} MACD cycle Endtime"),
                "Key candle datetime due to which setup invalidated": new.get("Final Key Candle Datetime"),
                f"Final {'buy' if side=='buy' else 'sell'} datetime": edt,
            })
            old_add = old.get("Strategy Additional info") or ""
            old["Strategy Additional info"] = old_add + "\n\n" + vkv(extra)
            # Clear EMA columns on invalidated row
            for prefix in ["2hrs", "4hrs", "1D"]:
                for ema in [50, 100, 200]:
                    old[f"{prefix} Price above/Below EMA {ema}"] = None
            # Clear trade fields
            for fld in ["Entry Datetime", "Entry Price", "0.5 Target Price a/c Actual Entry Price",
                        "0.5 SL Price a/c Actual Entry Price", "0.5 Target Price/ SL Price achieved DT",
                        "Final Entry Price", "Final Entry Date", "Hard SL Price",
                        "Assign Hard SL Percentage", "Qty", "Investment Value for Ratios",
                        "BackTest Result", "1:0.5 Exit Datetime", "1:0.5 Exit Price",
                        "1:0.5 SL hit Due to", "1:0.5 Holding Time (hrs)", "P/L 1:0.5"]:
                old[fld] = None
            for r_ in RATIOS_FULL:
                old[f"1:{r_} Exit Datetime"] = None; old[f"1:{r_} Exit Price"] = None
                old[f"1:{r_} SL hit Due to"] = None; old[f"1:{r_} Holding Time (hrs)"] = None
                old[f"P/L 1:{r_}"] = None
                if r_ >= 2: old[f"Status 1:{r_}"] = None
            for _, col in VOL_COL_MAP.items(): old[col] = None
            fin_key = ("Final Buy found from which Method" if side == "buy"
                       else "Final Sell found from which Method")
            old[fin_key] = None


    # ── APPLY V4 DELAYED ENTRY TO SURVIVORS ──────────────────────
    for row in rows:
        if row.get("Status") == "Intrade":
            apply_variation_4_logic(row, arr1, arr5, arr2h, arr4h, arr1d, volcalc1, side)

    rows.sort(key=lambda r: str(r.get(f"{tc} MACD cycle Startime") or ""))
    return rows


# ================================================================
# TELEGRAM / CSV / MAIN
# ================================================================

_fired_events = set()

def load_fired_events():
    global _fired_events
    if _BUY_LOG_PATH.exists():
        try:
            df = pd.read_csv(_BUY_LOG_PATH, dtype=str)
            if "Event ID" in df.columns:
                _fired_events.update(df["Event ID"].dropna().tolist())
        except Exception:
            pass

def save_fired_events():
    """Persist _fired_events to a JSON file so restarts don't re-fire old alerts."""
    try:
        import json
        _fe_path = BASE_LOG_DIR / "_fired_events.json"
        with open(_fe_path, "w") as _f:
            json.dump(list(_fired_events), _f)
    except Exception as _e:
        log(f"[save_fired_events] error: {_e}")

def tg_post(text: str):
    try:
        r1 = requests.post(TELEGRAM_URL, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
        if r1.status_code != 200:
            log(f"Telegram HTTP {r1.status_code}: {r1.text[:300]}")
    except Exception as e:
        log(f"Telegram exception: {e}")


_ticket_map = {}
_event_to_ticket: dict = {}


def log_trade_error(symbol, err_msg):
    log(f"Trade Error for {symbol}: {err_msg}")
    tg_post(f"⚠️ TRADE ERROR\nSymbol: {symbol}\n{err_msg}")

def mt5_bridge_trade(symbol: str, action_type: int, volume: float, sl: float = 0.0):
    try:
        url = f"{MT5_BRIDGE_URL}/trade"
        headers = {"X-Api-Key": MT5_API_KEY, "Content-Type": "application/json"}
        try: v = float(volume)
        except Exception: v = 0.01
        try: s = float(sl)
        except Exception: s = 0.0

        payload = {
            "action": 1,
            "symbol": symbol,
            "volume": v,
            "type": action_type,
            "price": 0.0,
            "sl": s,
            "magic": MAGIC,
            "comment": "S17 Bridge"
        }
        import requests
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        log(f"MT5 Bridge Trade HTTP {r.status_code}: {r.text[:300]}")
        try:
            j = r.json()
            order_id = int(j.get("order_id", 0))
            retcode = int(j.get("result", 0))
            comment = j.get("comment", "")
            if order_id <= 0 or retcode != 10009:
                err_msg = f"Trade failed. Retcode: {retcode}, Comment: {comment}"
                log_trade_error(symbol, err_msg)
            return (order_id, retcode, comment)
        except Exception as e:
            err_msg = f"JSON parse error: {e}"
            log_trade_error(symbol, err_msg)
            return (0, 0, err_msg)
    except Exception as e:
        err_msg = f"HTTP Request exception: {e}"
        log_trade_error(symbol, err_msg)
        log(f"MT5 Bridge exception: {e}")
        return (0, 0, err_msg)

def mt5_bridge_modify_sl(ticket: int, new_sl: float):
    try:
        url = f"{MT5_BRIDGE_URL}/modify"
        payload = {"ticket": int(ticket), "sl": float(new_sl)}
        import requests
        r = requests.post(url, json=payload, headers={"X-Api-Key": MT5_API_KEY, "Content-Type": "application/json"}, timeout=20)
        log(f"MT5 Bridge Modify SL HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.status_code == 200 and int(r.json().get("result", 0)) == 10009
        except Exception:
            return r.status_code == 200
    except Exception as e:
        log(f"MT5 Bridge Modify exception: {e}")
        return False


# ── Market-close flatten (see MARKET_CLOSE_FLATTEN.md) ─────────────────
# Close this strategy's open positions shortly BEFORE the symbol's session
# closes, so trades never sit through a closed market's reopening gap
# (2026-07-12: four USOIL positions gap-filled at Sunday reopen, -43.16).
# Session times key off New York wall clock: oil/gold pause 17:00-18:00 ET
# daily and everything non-crypto closes Friday 17:00 ET. Using
# America/New_York makes the UTC times DST-proof.

_MC_SESSIONS = {
    "USOIL":  {"daily_break": True,  "weekend": True},
    "XAUUSD": {"daily_break": True,  "weekend": True},
    "USDJPY": {"daily_break": False, "weekend": True},
    "EURUSD": {"daily_break": False, "weekend": True},
    "BTCUSD": {"daily_break": False, "weekend": False},   # 24/7
}



def is_market_closed(inst_symbol: str, _now=None) -> bool:
    s = _MC_SESSIONS.get(inst_symbol)
    if not s: return False
    import datetime as _mdt
    from zoneinfo import ZoneInfo as _MZi
    now = _now or _mdt.datetime.now(_MZi("America/New_York"))
    if s.get("weekend"):
        if (now.weekday() == 4 and now.hour >= 17) or (now.weekday() == 5) or (now.weekday() == 6 and now.hour < 17):
            return True
    if s.get("daily_break"):
        if now.weekday() < 4 and now.hour == 17:
            return True
    return False

def market_close_flatten_due(inst_symbol: str, _now=None):
    """(due, reason) — inside the lead window before this symbol's close?"""
    s = _MC_SESSIONS.get(inst_symbol)
    if not s:
        return (False, "")
    import datetime as _mdt
    from zoneinfo import ZoneInfo as _MZi
    now = _now or _mdt.datetime.now(_MZi("America/New_York"))
    mins = now.hour * 60 + now.minute
    if not (17 * 60 - FLATTEN_LEAD_MIN <= mins < 17 * 60):
        return (False, "")
    if now.weekday() == 4 and s["weekend"] and FLATTEN_BEFORE_WEEKEND:
        return (True, "weekend_close")
    if now.weekday() < 4 and s["daily_break"] and FLATTEN_BEFORE_DAILY_BREAK:
        return (True, "daily_break")
    return (False, "")


def mt5_bridge_close_ticket(ticket: int):
    try:
        url = f"{MT5_BRIDGE_URL}/close"
        import requests
        r = requests.post(url, json={"ticket": int(ticket)},
                          headers={"X-Api-Key": MT5_API_KEY, "Content-Type": "application/json"},
                          timeout=20)
        log(f"MT5 Bridge Close HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.status_code == 200 and int(r.json().get("result", 0)) == 10009
        except Exception:
            return r.status_code == 200
    except Exception as e:
        log(f"MT5 Bridge Close exception: {e}")
        return False


def flatten_for_market_close(positions: list, inst_symbol: str):
    """Close this strategy's open positions in the pre-close window.
    Returns the set of tickets closed (they must skip trailing)."""
    due, why = market_close_flatten_due(inst_symbol)
    if not due:
        return set()
    closed = set()
    for p in positions:
        try:
            ticket = int(p.get("ticket", 0) or 0)
            magic = int(p.get("magic", 0) or 0)
        except Exception:
            continue
        rec = _ticket_map.get(ticket)
        if not rec or rec.get("symbol") != inst_symbol or magic != MAGIC or ticket <= 0:
            continue
        if not mt5_bridge_close_ticket(ticket):
            log(f"[FLATTEN] close FAILED ticket={ticket} ({why}) — will retry next pass")
            continue
        pnl = float(p.get("profit") or 0)
        log(f"[FLATTEN] closed ticket={ticket} ({why}) pnl={pnl}")
        trade_db.record_close(ticket, reason=f"flatten_{why}", pnl=pnl)
        rec["closed_notified"] = True
        tg_post(f"\U0001F6D1 MARKET-CLOSE FLATTEN\nTicket: {ticket}\nSymbol: {inst_symbol}\n"
                f"Reason: {why}\nFloating P/L at close: {pnl}")
        closed.add(ticket)
    return closed


# ── ratio ladder ──────────────────────────────────────────────────────
# Targets are precomputed up to 1:10 only, but a runner can travel further
# than that. These helpers extrapolate the ladder from its own 1R step so
# trailing keeps ratcheting for as long as the position is open, instead of
# freezing the stop at the last precomputed anchor.

def _ratio_step(rec):
    """Signed 1R distance between consecutive ratio targets — positive for a
    buy, negative for a sell. Read off the ladder itself so it also holds
    after a crash-recovery reload (which hands the keys back as floats)."""
    t = rec.get("targets") or {}
    t1, t2 = t.get(1), t.get(2)
    if t1 is not None and t2 is not None:
        return float(t2) - float(t1)
    entry = float(rec.get("entry_price") or 0)
    if t1 is not None and entry:
        return float(t1) - entry
    return 0.0


def _ratio_base(rec, step):
    """Entry price the ladder was built from."""
    t1 = (rec.get("targets") or {}).get(1)
    if t1 is not None:
        return float(t1) - step
    return float(rec.get("entry_price") or 0)


def _ratio_target(rec, cp):
    """Price of the 1:cp target, extrapolated past the precomputed ladder."""
    t = (rec.get("targets") or {}).get(cp)
    if t is not None:
        return float(t)
    step = _ratio_step(rec)
    if not step:
        return None
    return _ratio_base(rec, step) + cp * step


def _trail_checkpoints(rec, hit_px):
    """Trail rungs 1:2 … 1:N, N being the highest ratio price has reached.

    N is deliberately uncapped: the trail follows the trade rung by rung
    until the stop is finally taken out. The old fixed ladder ran out at
    1:10, which parked the stop two ratios back and let an open runner hand
    the rest of the move back to the market."""
    step = _ratio_step(rec)
    if not step or hit_px is None:
        return range(0)
    reached = int((float(hit_px) - _ratio_base(rec, step)) / step + 1e-9)
    return range(2, reached + 1)


def trail_conservative_positions(positions: list, current_px: float, inst_symbol: str,
                                  low_px: float = None, high_px: float = None):
    if not positions: return
    # ── data-sanity guard (see TRAIL_SL_FIX.md) ──────────────────────────
    # The market-close boundary can deliver degenerate candles (zero/absurd
    # extremes). A bogus low fires EVERY sell trail ratio at once (a bogus
    # high does the same for buys) and walks the SL to impossible levels.
    # Trust an extreme only within +/-20% of the candle close; otherwise fall
    # back to the close itself. No close at all -> skip this trailing pass.
    if not current_px or current_px <= 0:
        return
    def _sane_extreme(v):
        try:
            return v is not None and float(v) > 0 and abs(float(v) - current_px) / current_px <= 0.20
        except (TypeError, ValueError):
            return False
    if low_px is not None and not _sane_extreme(low_px):
        log(f"[TRAIL] ignoring bogus candle low={low_px} (close={current_px})")
        low_px = None
    if high_px is not None and not _sane_extreme(high_px):
        log(f"[TRAIL] ignoring bogus candle high={high_px} (close={current_px})")
        high_px = None
    for p in positions:
        try:
            ticket = int(p.get("ticket", 0))
            magic  = int(p.get("magic", 0))
            p_side = int(p.get("type", 0))
        except Exception: continue
        if magic != MAGIC or ticket <= 0: continue
        if ticket not in _ticket_map: continue

        rec = _ticket_map[ticket]
        if rec.get("symbol") != inst_symbol: continue
        # cross-symbol contamination guard (see TRAIL_SL_FIX.md): if the
        # price we're evaluating is wildly off this position's own open
        # price, the candle frame belongs to another symbol — skip.
        _open_px = float(p.get("price_open") or 0)
        if _open_px > 0 and abs(current_px - _open_px) / _open_px > 0.5:
            log(f"[TRAIL] px sanity: current_px={current_px} vs open={_open_px} — skipping ticket={ticket}")
            continue
        cur_sl  = float(p.get("sl", 0))
        # Guarantee anchor exists to prevent UnboundLocalError during trailing
        anchor = rec.get("current_sl", rec.get("entry_price", 0.0))
        # Use candle LOW for SELL hits, HIGH for BUY hits (more accurate than close)
        _hit_px = (low_px if low_px is not None else current_px) if p_side == 1 else (high_px if high_px is not None else current_px)
        for cp in _trail_checkpoints(rec, _hit_px):
            if cp in rec["trail_hit"]: continue
            tgt = _ratio_target(rec, cp)
            if tgt is None: continue
            hit = (_hit_px <= tgt) if p_side == 1 else (_hit_px >= tgt)
            if not hit: continue
            rec["trail_hit"].add(cp)
            if cp == 2:
                anchor = rec["entry_price"]
            else:
                _anch = _ratio_target(rec, cp - 2)
                if _anch is not None: anchor = _anch
            if anchor is None: continue
            if p_side == 1:  # SELL — trail SL downward (lower value = better)
                if cur_sl > 0 and anchor >= cur_sl: continue  # anchor not lower → no improvement
            else:            # BUY — trail SL upward (higher value = better)
                if cur_sl > 0 and anchor <= cur_sl: continue  # anchor not higher → no improvement
            new_sl = round(float(anchor), 6)
            log(f"[TRAIL] ticket={ticket} cp=1:{cp} anchor={new_sl} target={tgt} current_px={current_px}")
            _mod_ok = mt5_bridge_modify_sl(ticket, new_sl)
            trade_db.record_trail(ticket, new_sl, cp, executed=bool(_mod_ok))
            if not _mod_ok:
                # broker refused (or bridge unreachable): un-mark the ratio so
                # a later candle retries, keep cur_sl at the broker's truth,
                # and skip the Telegram post for a move that never happened
                rec["trail_hit"].discard(cp)
                continue
            cur_sl = new_sl
            tg_post(f"📐 SL TRAILED\nTicket: {ticket}\nNew SL: {new_sl}\nAnchor: 1:{cp} target hit at {tgt}\nCurrent price: {current_px}")
        rec["current_sl"] = cur_sl


def format_telegram(r: dict) -> str:
    ep  = r.get("Entry Price", 0)
    t05 = r.get("0.5 Target Price a/c Actual Entry Price", 0)
    fp  = r.get("Final Entry Price", 0)
    hsl = r.get("Hard SL Price", 0)
    return (
        f"🟢 BUY {SYMBOL}\n"
        f"Strategy: Strategy 17 M1 M2 Variation 4 LIVE\n"
        f"Final Strategy Candle Datetime: {r.get('Final Strategy Candle Datetime','')}\n"
        f"Final Buy found from which Method: {r.get('Final Buy found from which Method','')}\n"
        f"Entry Datetime: {r.get('Entry Datetime','')}\n"
        f"Entry Price: {float(ep) if ep else 0.0:.6f}\n"
        f"0.5 Target Price a/c Actual Entry Price: {float(t05) if t05 else 0.0:.6f}\n"
        f"0.5 Target Price/ SL Price achieved DT: {r.get('0.5 Target Price/ SL Price achieved DT','')}\n"
        f"Final Entry Price: {float(fp) if fp else 0.0:.6f}\n"
        f"Final Entry Date: {r.get('Final Entry Date','')}\n"
	f"Qty: {r.get('Qty', 'N/A')}\n"
        f"Hard SL Price: {float(hsl) if hsl else 0.0:.6f}\n"
        f"2hrs EMA Context: 50={r.get('2hrs Price above/Below EMA 50','N/A')} | 100={r.get('2hrs Price above/Below EMA 100','N/A')} | 200={r.get('2hrs Price above/Below EMA 200','N/A')}\n"
        f"4hrs EMA Context: 50={r.get('4hrs Price above/Below EMA 50','N/A')} | 100={r.get('4hrs Price above/Below EMA 100','N/A')} | 200={r.get('4hrs Price above/Below EMA 200','N/A')}\n"
        f"1D EMA Context: 50={r.get('1D Price above/Below EMA 50','N/A')} | 100={r.get('1D Price above/Below EMA 100','N/A')} | 200={r.get('1D Price above/Below EMA 200','N/A')}"
    )

def format_telegram_exit(symbol: str, strategy: str, side: str, trade_id: str,
                         entry_dt: str, hard_sl: float, ratio_str: str,
                         msg_type: str, new_trail_applied: str, new_trail_at: str,
                         event_type: str, exit_price: str, exit_dt: str, pnl: str) -> str:
    side_str = "buy" if side.lower() == "buy" else "sell"
    return (
        f"Asset: {symbol} (Theoretical)\n"
        f"Strategy: {strategy}\n"
        f"Entry Signal: {side_str}\n"
        f"Trade Entry Id: {trade_id}\n"
        f"Final Entry Datetime: {entry_dt}\n"
        f"Hard SL Price: {hard_sl:.6f}\n"
        f"Ratio: {ratio_str}\n"
        f"Message Type: {msg_type} (Theoretical Simulation)\n"
        f"New Trailed SL Applied: {new_trail_applied}\n"
        f"New Trail SL at: {new_trail_at}\n"
        f"Event Occured Type: {event_type}\n"
        f"Exit Price: {exit_price}\n"
        f"Exit Datetime: {exit_dt}\n"
        f"PNL: {pnl}\n\n⚠️ Note: This is a theoretical result based on price simulation, not a live MT5 execution."
    )

def process_telegram_exits(row: dict, symbol: str, side: str, strategy_name: str, recent_1m: pd.DatetimeIndex):
    global _fired_events
    if row.get("Status") != "Intrade": return
        
    fcc_ts = row.get("Final Strategy Candle Datetime")
    ev_id = hashlib.sha256(f"{side}|{symbol}|{fcc_ts}".encode()).hexdigest()[:24]
    
    entry_dt_str = str(row.get("Entry Datetime", ""))
    if not entry_dt_str or entry_dt_str == "None" or entry_dt_str == "nan": return
        
    try:
        fscd_obj = pd.to_datetime(fcc_ts)
        fscd_str = fscd_obj.strftime("%Y-%m-%d %H:%M")
    except Exception:
        fscd_str = str(fcc_ts)
        
    try:
        dt_obj = pd.to_datetime(entry_dt_str)
        formatted_dt = dt_obj.strftime("%Y-%m-%d-%H:%M")
    except Exception:
        formatted_dt = entry_dt_str.replace(" ", "-")
        
    trade_id = f"FSCD-{fscd_str} - ET-{formatted_dt}."
    try: hard_sl = float(row.get("Hard SL Price", 0.0))
    except Exception: hard_sl = 0.0
    try: entry_price = float(row.get("Entry Price", 0.0))
    except Exception: entry_price = 0.0
    
    entry_dt_val = str(row.get("Final Entry Date", ""))
    if not entry_dt_val or entry_dt_val == "None" or entry_dt_val == "nan":
        entry_dt_val = entry_dt_str
        
    ratios = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    
    # Target Hits
    for r in ratios:
        exit_dt_str = str(row.get(f"1:{r} Exit Datetime", "None"))
        exit_price = str(row.get(f"1:{r} Exit Price", ""))
        pnl = str(row.get(f"P/L 1:{r}", ""))
        status = str(row.get(f"Status 1:{r}", "None"))
        due = str(row.get(f"1:{r} SL hit Due to", "None"))
        
        if exit_dt_str == "None" or exit_dt_str == "nan": continue
        try: exit_ts = pd.Timestamp(exit_dt_str)
        except Exception: continue
            
        is_target_hit = False
        if r >= 2:
            if status == "Target Hit": is_target_hit = True
        else:
            if due == "None":
                try:
                    epx = float(exit_price)
                    target_1_1 = entry_price + abs(entry_price - hard_sl) if side.lower() == "buy" else entry_price - abs(entry_price - hard_sl)
                    if side.lower() == "buy" and epx >= target_1_1 * 0.9999: is_target_hit = True
                    elif side.lower() == "sell" and epx <= target_1_1 * 1.0001: is_target_hit = True
                except Exception: pass
            
        if is_target_hit:
            msg_ev = f"{ev_id}_TGT_{r}"
            if msg_ev not in _fired_events and exit_ts in recent_1m:
                _fired_events.add(msg_ev)
                save_fired_events()
                
                new_trail_applied = "no"
                new_trail_at = "N/A"
                if r >= 2:
                    new_trail_applied = "yes"
                    if r == 2: new_trail_at = "Entry Price (Breakeven)"
                    else: new_trail_at = f"1:{r-2} target"
                
                msg = format_telegram_exit(
                    symbol=symbol, strategy=strategy_name, side=side, trade_id=trade_id,
                    entry_dt=entry_dt_val, hard_sl=hard_sl, ratio_str=f"1:{r}",
                    msg_type="Target Hit", new_trail_applied=new_trail_applied, new_trail_at=new_trail_at,
                    event_type=f"1:{r} target Hit", exit_price=exit_price, exit_dt=exit_dt_str, pnl=pnl
                )
                log(f"\n🔔 EXIT TGT [{symbol}]:\n{msg}")
                tg_post(msg)

    # SL Hits
    sl_groups = {} 
    for r in ratios:
        exit_dt_str = str(row.get(f"1:{r} Exit Datetime", "None"))
        exit_price = str(row.get(f"1:{r} Exit Price", ""))
        due = str(row.get(f"1:{r} SL hit Due to", "None"))
        status = str(row.get(f"Status 1:{r}", "None"))
        
        if exit_dt_str == "None" or exit_dt_str == "nan": continue
        is_sl_hit = False
        if r >= 2:
            if "SL Hit" in status: is_sl_hit = True
        else:
            if due != "None" and due != "nan": is_sl_hit = True
            
        if is_sl_hit:
            key = (exit_dt_str, due, exit_price)
            sl_groups.setdefault(key, []).append(r)
            
    for key, r_list in sl_groups.items():
        exit_dt_str, due, exit_price = key
        try: exit_ts = pd.Timestamp(exit_dt_str)
        except Exception: continue
            
        msg_ev = f"{ev_id}_SL_GRP_{exit_dt_str}_{due}"
        if msg_ev not in _fired_events and exit_ts in recent_1m:
            _fired_events.add(msg_ev)
            _fired_events.add(msg_ev)
            if len(r_list) == 1: ratio_str = f"1:{r_list[0]}"
            else: ratio_str = f"1:{r_list[0]} to 1:{r_list[-1]}"
                
            is_trailed = (due != "Hard SL" and due != "None" and due != "nan")
            if is_trailed:
                msg_type = f"Trailed SL hit - {due}"
                evt_type = f"SL hit - {due} from {ratio_str} ratios"
            else:
                msg_type = "SL Hit"
                evt_type = f"SL hit from {ratio_str} ratios"
                
            total_pnl = 0.0
            for r in r_list:
                try: total_pnl += float(str(row.get(f"P/L 1:{r}", "0")))
                except Exception: pass
                
            msg = format_telegram_exit(
                symbol=symbol, strategy=strategy_name, side=side, trade_id=trade_id,
                entry_dt=entry_dt_val, hard_sl=hard_sl, ratio_str=ratio_str,
                msg_type=msg_type, new_trail_applied="no", new_trail_at="N/A",
                event_type=evt_type, exit_price=exit_price, exit_dt=exit_dt_str, pnl=f"{total_pnl:.6f}"
            )
            log(f"\n🔔 EXIT SL [{symbol}]:\n{msg}")
            tg_post(msg)

def run_live_scan():
    global _fired_events
    log("Fetching live candles...")
    # Fetch all five timeframes in parallel — pass time collapses from the
    # sum of five bridge calls to the slowest single one. Each future writes
    # its own (symbol, timeframe) cache key, so there is no overlap.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as _tf_pool:
        _tf_futs = {tf: _tf_pool.submit(get_rolling_mt5_candles, SYMBOL, tf, lb)
                    for tf, lb in (("M5", LOOKBACK_5M), ("M1", LOOKBACK_1M),
                                   ("H2", LOOKBACK_2H), ("H4", LOOKBACK_4H),
                                   ("D1", LOOKBACK_1D))}
    df5_raw = _tf_futs["M5"].result()
    df1_raw = _tf_futs["M1"].result()
    df2h_raw = _tf_futs["H2"].result()
    df4h_raw = _tf_futs["H4"].result()
    df1d_raw = _tf_futs["D1"].result()
    
    if df5_raw.empty or df1_raw.empty:
        log("Data missing, skipping...")
        return

    df5 = prepare_df(df5_raw)
    df1 = prepare_df(df1_raw)
    df2h = prepare_df_tf(df2h_raw) if not df2h_raw.empty else pd.DataFrame()
    df4h = prepare_df_tf(df4h_raw) if not df4h_raw.empty else pd.DataFrame()
    df1d = prepare_df_tf(df1d_raw) if not df1d_raw.empty else pd.DataFrame()

    arr5 = make_fast_arrays(df5)
    arr1 = make_fast_arrays(df1)
    arr2h = make_tf_arrays(df2h) if not df2h.empty else {"idx": pd.DatetimeIndex([]), "close": np.array([]), "ema50": np.array([]), "ema100": np.array([]), "ema200": np.array([])}
    arr4h = make_tf_arrays(df4h) if not df4h.empty else arr2h
    arr1d = make_tf_arrays(df1d) if not df1d.empty else arr2h

    cycles1 = build_cycle_map(arr1)
    volcalc1 = VolumeCalculator(df1_raw)

    side = "buy"
    setups5 = scan_5m_final_strategy_candles(df5, side)
    
    rows_to_save = []
    recent_1m = df1_raw.index[-RECENT_1M_COUNT:]

    for s5 in setups5:
        row = process_variation1_setup(s5, arr5, arr1, cycles1, volcalc1, arr2h, arr4h, arr1d, side)
        
        # --- NEW TRADING QTY CONTRACT COLUMN LOGIC ---
        _symbol = SYMBOL
        _c_size = 1
        _clean_symbol = _symbol.upper().replace("_", "")
        if "XAUUSD" in _clean_symbol: _c_size = 100
        elif "BTC" in _clean_symbol: _c_size = 1
        elif "JPY" in _clean_symbol: _c_size = 100000
        elif "OIL" in _clean_symbol or "WTICOUSD" in _clean_symbol or "BCOUSD" in _clean_symbol: _c_size = 1000
        elif "EURUSD" in _clean_symbol: _c_size = 100000

        from collections import OrderedDict
        import pandas as pd
        new_row = OrderedDict()
        for k, v in row.items():
            new_row[k] = v
            if k == "Qty":
                q = row.get("Qty")
                if q not in (None, "", "None") and not pd.isna(q):
                    try:
                        _q_lots = float(q) / _c_size
                        # JPY-quoted pairs: Qty (= risk / price-distance) is in
                        # JPY value units, so converting to lots also needs the
                        # JPY->USD rate (~= price) — without it the position is
                        # ~rate x under-sized (empirical audit 2026-07-20: real
                        # USDJPY value is 100000/rate USD per lot per point)
                        if "JPY" in _clean_symbol:
                            _rate = float(row.get("Entry Price") or 0)
                            if _rate > 0:
                                _q_lots *= _rate
                        new_row["Trading qty Contract"] = max(round(_q_lots, 2), 0.01)
                    except Exception:
                        new_row["Trading qty Contract"] = None
                else:
                    new_row["Trading qty Contract"] = None
        row = new_row
        fcc_ts = _s(s5.get("fcc_ts") or s5.get("cycle_end_ts"))
        row["Final Strategy Candle Datetime"] = fcc_ts
        row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        # Give it a unique ID based on FCC TS
        ev_id = hashlib.sha256(f"buy|{fcc_ts}".encode()).hexdigest()[:24]
        row["Event ID"] = ev_id

        if row.get("Status") == "Intrade":
            apply_variation_4_logic(row, arr1, arr5, arr2h, arr4h, arr1d, volcalc1, side)
        
        # Check if entry is new and finalized
        fin_ts = pd.Timestamp(row.get("Final Entry Date")) if row.get("Final Entry Date") else None
        is_new_entry = fin_ts is not None and fin_ts in recent_1m
        
        if row.get("Status") != "Intrade" and "Invalidated" in row.get("Status", ""):
            # Add to list, do not send TG
            ev_id = row.get("Event ID")
            if ev_id in _event_to_ticket:
                ticket = _event_to_ticket[ev_id]
                rec = _ticket_map.get(ticket)
                if rec:
                    row["Live MT5 Order ID"] = ticket
                    row["Live Entry Price"] = rec.get("entry_price", "")
                    row["Live Corrected Hard SL"] = rec.get("hard_sl", "")
                    if "_arr1" in row:
                        _, l_bt, l_q, l_inv = run_backtest_v1(row["_arr1"], row["_entry_pos"], float(rec.get("entry_price", 0.0)), float(rec.get("hard_sl", 0.0)), row["_side"])
                        row["Executed Result"] = l_bt
                        row["Executed Qty"] = l_q
                        row["Executed Investment Value"] = l_inv
                    row["Live Exit Datetime"] = rec.get("exit_datetime", "")
            row.pop("_arr1", None)
            row.pop("_entry_pos", None)
            row.pop("_side", None)
            rows_to_save.append(row)
        else:
            ev_id = row.get("Event ID")
            if ev_id in _event_to_ticket:
                ticket = _event_to_ticket[ev_id]
                rec = _ticket_map.get(ticket)
                if rec:
                    row["Live MT5 Order ID"] = ticket
                    row["Live Entry Price"] = rec.get("entry_price", "")
                    row["Live Corrected Hard SL"] = rec.get("hard_sl", "")
                    if "_arr1" in row:
                        _, l_bt, l_q, l_inv = run_backtest_v1(row["_arr1"], row["_entry_pos"], float(rec.get("entry_price", 0.0)), float(rec.get("hard_sl", 0.0)), row["_side"])
                        row["Executed Result"] = l_bt
                        row["Executed Qty"] = l_q
                        row["Executed Investment Value"] = l_inv
                    row["Live Exit Datetime"] = rec.get("exit_datetime", "")
            row.pop("_arr1", None)
            row.pop("_entry_pos", None)
            row.pop("_side", None)
            rows_to_save.append(row)
            if is_new_entry:
                msg_ev = f"{ev_id}_TELEGRAM"
                if msg_ev not in _fired_events:
                    _fired_events.add(msg_ev)
                    save_fired_events()
                    msg = format_telegram(row)
                    log(f"\n🔔 NEW SIGNAL:\n{msg}")
                    trade_db.record_signal(ev_id, SYMBOL_BRIDGE, "buy",
                                           signal_price=row.get("Entry Price"),
                                           qty=row.get("Qty"),
                                           hard_sl=row.get("Hard SL Price"))
                    symbol = SYMBOL
                    # --- Send to MT5 Bridge ---
                    qty     = row.get("Qty", 0.01)
                    hard_sl = row.get("Hard SL Price", 0.0)
                    try: raw_qty = float(qty) if qty not in (None, "", "None") else 0.01
                    except Exception: raw_qty = 0.01
                    raw_qty = max(raw_qty, 0.01)
                    rounded_qty = round(round(raw_qty / 0.01) * 0.01, 2)
                    
                    # Override rounded_qty with Trading qty Contract if exists
                    t_qty = row.get("Trading qty Contract")
                    import pandas as pd
                    if t_qty not in (None, "", "None") and not pd.isna(t_qty):
                        try:
                            rounded_qty = float(t_qty)
                        except Exception:
                            pass

                    order_id, retcode, t_comment = mt5_bridge_trade(symbol, 0, rounded_qty, hard_sl)
                    actual_entry = None  # per-signal reset — never inherit a previous row's fill price
                    if order_id > 0 and retcode == 10009:
                        try:
                            import time
                            # The position can take a moment to appear on the
                            # bridge — try up to 3 times to find the fill price.
                            matched_pos = None
                            for _pe_try in range(3):
                                time.sleep(1.0)
                                try:
                                    r_pos = requests.get(f"{MT5_BRIDGE_URL}/positions", headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
                                    if r_pos.status_code == 200:
                                        pos_list = r_pos.json()
                                        pos_list = pos_list.get("positions", pos_list) if isinstance(pos_list, dict) else pos_list
                                        pos_list = pos_list if isinstance(pos_list, list) else []
                                        for p in pos_list:
                                            if int(p.get("ticket", 0)) == order_id:
                                                matched_pos = p
                                                break
                                except Exception as _pe_fe:
                                    log(f"[POST-ENTRY] positions fetch failed (try {_pe_try + 1}/3): {_pe_fe}")
                                if matched_pos:
                                    break
                            if matched_pos:
                                actual_entry = float(matched_pos.get("price_open"))
                            else:
                                log(f"[POST-ENTRY] ticket {order_id} not found after 3 tries — falling back to the signal entry price for the SL correction")
                            # The correction must ALWAYS run: the order was placed
                            # with the raw signal SL, which is only a placeholder.
                            # Without the fill price, the signal entry price
                            # (fill ± slippage) still gives a far safer SL than
                            # leaving the placeholder on the position.
                            _entry_for_sl = actual_entry if actual_entry is not None else float(row.get("Entry Price", 0) or 0)
                            if _entry_for_sl > 0 and rounded_qty > 0:
                                FIXED_RISK_USD = RISK_PER_TRADE
                                _s_upper = symbol.upper()
                                if "JPY" in _s_upper:
                                    price_diff = (FIXED_RISK_USD * _entry_for_sl) / (_c_size * rounded_qty)
                                else:
                                    price_diff = FIXED_RISK_USD / (_c_size * rounded_qty)

                                digits = 5 if "EURUSD" in _s_upper else (2 if "BTC" in _s_upper else 3)
                                price_diff = round(price_diff, digits)

                                exact_sl = round(_entry_for_sl - price_diff, digits)
                                _sl_ok = False
                                for _mod_try in range(3):
                                    if mt5_bridge_modify_sl(order_id, exact_sl):
                                        _sl_ok = True
                                        break
                                    time.sleep(1.0)
                                if _sl_ok:
                                    hard_sl = exact_sl
                                    _px_kind = "fill" if actual_entry is not None else "signal"
                                    log(f"[POST-ENTRY] corrected SL for ticket {order_id} to {exact_sl} based on {_px_kind} price {_entry_for_sl}")
                                else:
                                    log(f"[POST-ENTRY] SL correction FAILED for ticket {order_id} — position keeps placed SL {hard_sl}")
                                    tg_post(f"⚠️ POST-ENTRY SL CORRECTION FAILED\nTicket: {order_id}\nIntended SL: {exact_sl}\nSL on broker: {hard_sl}\nCheck the position manually.")
                        except Exception as ex_pe:
                            log(f"[POST-ENTRY] error during post-entry SL correction: {ex_pe}")
                        exec_status = f"\n✅ MT5 Execution: SUCCESS (Order ID: {order_id})"
                    else:
                        exec_status = f"\n❌ MT5 Execution: FAILED (Retcode: {retcode}, Error: {t_comment})"

                    msg += exec_status
                    tg_post(msg)
                    trade_db.mark_telegram_sent(ev_id)

                    if order_id > 0 and retcode == 10009:
                        targets = {}
                        base_entry = actual_entry if actual_entry is not None else float(row.get("Entry Price", 0) or 0)
                        safe_hard_sl = float(hard_sl or 0)
                        for r_ in [1,2,3,4,5,6,7,8,9,10]:
                            targets[r_] = _tgt(base_entry, safe_hard_sl, side, r_)
                        targets[0.5] = _tgt(base_entry, safe_hard_sl, side, 0.5)
    
                        _ticket_map[order_id] = {
                            "symbol": SYMBOL_BRIDGE,
                            "event_id": ev_id,
                            "side": "buy",
                            "entry_price": base_entry,  # actual Exness fill price
                            "actual_entry_price": base_entry,  # explicit key for clarity
                            "hard_sl": float(hard_sl or 0),
                            "targets": targets,
                            "current_sl": float(hard_sl or 0),
                            "trail_hit": set(),
                        }
                        _event_to_ticket[ev_id] = order_id
                        trade_db.record_execution(ev_id, order_id, retcode,
                                                  entry_price=base_entry,
                                                  qty=rounded_qty,
                                                  hard_sl=safe_hard_sl,
                                                  targets=targets)
                        log(f"[TRAIL] registered ticket={order_id}")
                        tg_post(f"✅ POSITION OPENED\nTicket: {order_id}\nEntry: {_ticket_map[order_id]['entry_price']}\nHard SL: {hard_sl}\nQty: {rounded_qty}")
                    else:
                        trade_db.record_execution(ev_id, order_id, retcode, error=t_comment)
                        tg_post(f"⚠️ MT5 ORDER FAILED\n(no ticket returned from bridge)")

            
            # Only send Telegram alerts for rows with a live MT5 ticket
            if row.get("Event ID") in _event_to_ticket:
                process_telegram_exits(row, SYMBOL, "buy", "Strategy 17 M1 M2 Variation 4", recent_1m)

    if rows_to_save:
        new_df = pd.DataFrame(rows_to_save)
        # Deduplicate entry by FCC TS (Event ID)
        if _BUY_LOG_PATH.exists():
            try:
                old_df = pd.read_csv(_BUY_LOG_PATH)
                # Keep new ones, remove old ones with same Event ID
                old_df = old_df[~old_df["Event ID"].isin(new_df["Event ID"])]
                merged = pd.concat([old_df, new_df], ignore_index=True)
            except Exception:
                merged = new_df
        else:
            merged = new_df
        
        merged.to_csv(_BUY_LOG_PATH, index=False)

    # Stop management (market-close flatten, SL trailing, closed-position
    # detection) moved to the dedicated trailing thread — run_trailing_pass().
    # Scans now only scan.



def recover_open_trades():
    """Rebuild _ticket_map/_event_to_ticket from the DB after a restart so
    trailing resumes on positions opened by a previous run. Trades whose
    ticket is no longer open on MT5 are marked CLOSED instead."""
    if not trade_db.enabled():
        return
    saved = trade_db.load_open_trades()
    if not saved:
        return
    open_tickets = None
    try:
        r = requests.get(f"{MT5_BRIDGE_URL}/positions",
                         headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            live = data.get("positions", data) if isinstance(data, dict) else data
            live = live if isinstance(live, list) else []
            open_tickets = {int(p.get("ticket", 0)) for p in live}
    except Exception as e:
        log(f"[RECOVERY] /positions check failed: {e} — resuming all DB trades")
    recovered = 0
    for tr in saved:
        ticket = tr["ticket"]
        if open_tickets is not None and ticket not in open_tickets:
            log(f"[RECOVERY] ticket={ticket} closed while offline → marking CLOSED")
            trade_db.record_close(ticket, reason="closed_while_offline")
            continue
        _ticket_map[ticket] = {
            "symbol": tr["symbol"],
            "event_id": tr["event_id"],
            "side": tr["side"],
            "entry_price": tr["entry_price"],
            "actual_entry_price": tr["entry_price"],
            "hard_sl": tr["hard_sl"],
            "targets": tr["targets"],
            "current_sl": tr["current_sl"],
            "trail_hit": tr["trail_hit"],
        }
        _event_to_ticket[tr["event_id"]] = ticket
        trade_db.record_recovery_event(ticket, "resumed trailing after restart")
        recovered += 1
        log(f"[RECOVERY] resumed ticket={ticket} entry={tr['entry_price']} "
            f"current_sl={tr['current_sl']} trail_hit={sorted(tr['trail_hit'])}")
    if recovered:
        tg_post(f"♻️ RECOVERY: resumed trailing for {recovered} open position(s) after restart")

# ── Dedicated stop-management thread ────────────────────────────────────
# Trailing, market-close flatten and closed-position detection used to run
# INSIDE the per-symbol signal scans: one slow candle fetch delayed stop
# management for every open position, and an overrun scan minute skipped it
# entirely. They now run here on their own fixed cadence, decoupled from
# scanning. trade_db is already thread-safe (it serves the heartbeat
# thread); dict/set ops on _ticket_map are atomic under the GIL and the
# only writer of trail state is this thread.

def run_trailing_pass():
    """One stop-management pass over this strategy's open positions."""
    if not _ticket_map:
        return
    try:
        r = requests.get(f"{MT5_BRIDGE_URL}/positions",
                         headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
        if r.status_code != 200:
            return
        data = r.json()
        live = data.get("positions", data) if isinstance(data, dict) else data
        live = live if isinstance(live, list) else []
        open_tickets = {int(p.get("ticket", 0)) for p in live}
        # symbols where THIS strategy currently holds positions
        syms = set()
        for p in live:
            rec = _ticket_map.get(int(p.get("ticket", 0) or 0))
            if rec and rec.get("symbol"):
                syms.add(rec["symbol"])
        for sym in sorted(syms):
            closed = flatten_for_market_close(live, sym)
            if closed:
                live = [p for p in live if int(p.get("ticket", 0) or 0) not in closed]
            df1 = get_rolling_mt5_candles(sym, "M1", LOOKBACK_1M)
            if df1 is None or df1.empty:
                continue
            trail_conservative_positions(live, float(df1["close"].iloc[-1]), sym,
                                         low_px=float(df1["low"].iloc[-1]),
                                         high_px=float(df1["high"].iloc[-1]))
        for t, rec in list(_ticket_map.items()):
            if t not in open_tickets and not rec.get("closed_notified"):
                log(f"[EXIT] ticket={t} closed")
                tg_post(f"🔒 POSITION CLOSED\nTicket: {t}\nEntry: {rec.get('entry_price')}\nLast SL: {rec.get('current_sl')}")
                rec["exit_datetime"] = datetime.now(timezone.utc).isoformat()
                rec["closed_notified"] = True
                trade_db.record_close(t, reason="not_in_positions")
    except Exception as e:
        log(f"[TRAIL-LOOP] error: {e}")


def _trailing_loop():
    while True:
        try:
            run_trailing_pass()
        except Exception as e:
            log(f"[TRAIL-LOOP] unexpected: {e}")
        _time.sleep(TRAIL_INTERVAL_SEC)



def main():
    log("=" * 60)
    log("🚀 Strategy 17 M1 M2 Variation 4 LIVE SCANNER")
    log("=" * 60)
    
    load_fired_events()
    recover_open_trades()
    tg_post("🚀 Strategy 17 M1 M2 Variation 4 LIVE — STARTED\nSymbol: BTCUSDT\nMode: BUY side only\nNew code with FIXED_RISK_USD and slippage adjustment.\nFixed SL Trailing Logic")

    threading.Thread(target=_trailing_loop, daemon=True, name="stop-mgmt").start()
    log(f"[TRAIL-LOOP] stop-management thread started (every {TRAIL_INTERVAL_SEC}s)")

    last_scan_key = None
    try:
        while True:
            now = datetime.now(timezone.utc)
            scan_key = (now.year, now.month, now.day, now.hour, now.minute)
            # Scan once per minute, even if the previous pass overran the
            # minute boundary — the old `second <= 15` window silently
            # skipped entire minutes whenever a pass ran long.
            if scan_key != last_scan_key:
                log(f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] Scanning...")
                try:
                    run_live_scan()
                except Exception as e:
                    log(f"Error in scan: {e}")
                last_scan_key = scan_key
            _time.sleep(SCAN_SLEEP_SEC)
    except KeyboardInterrupt:
        log("Stopped by user.")

if __name__ == "__main__":
    main()