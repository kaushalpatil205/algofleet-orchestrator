#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy 21 Live Exness Bridge Script
-------------------------------------
Implements Strategy 21 paper trading / live simulation for BTCUSDT (BUY and SELL)
bridged with Exness MT5 Demo and Telegram alerts.

Key Live Rules:
1. Risk per trade: $500 (Qty = 500 / Adjusted SL Points).
2. Hard SL % Invalidation Rule:
   - BUY: Only take trades when Hard SL % > 1.0%. Otherwise invalidate with:
     "Invalidated due to Hard SL % is Less than or equal to 1 percentage".
   - SELL: Only take trades when Hard SL % < 1.5%. Otherwise invalidate with:
     "Invalidated due to Hard SL % is Greater than or equal to 1.5 percentage".
3. Invalidated signals are recorded in log files but DO NOT trigger Telegram signals or MT5 trades.
4. Telegram Entry signals follow exact BUY/SELL template.
5. Telegram Exit signals group Hard SL / Soft SL / Trailed SL hits across ratios into one notification per event.
6. Log files store the exact full Hard SL and Soft SL Additional info structure from Strategy 21_1:10 Ratios.py.
"""

import hashlib
import json
import logging
import os
import sys
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pytz
import requests
import talib

# =============================================================================
# CONFIG & CREDENTIALS
# =============================================================================

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "Strategy 21 Live Exness Bridge Script.json")
if os.path.exists(_CONFIG_PATH):
    with open(_CONFIG_PATH, "r") as _f:
        _config = json.load(_f)
else:
    _config = {}

BOT_TOKEN = _config.get("BOT_TOKEN", "8516308330:AAE5opWXPiGtpUdqtRD0u_LfyllXhUmK-7g")
CHAT_ID = _config.get("CHAT_ID", "8113300560")

MT5_DATA_BRIDGE_URL = _config.get("MT5_DATA_BRIDGE_URL", "https://exness-bridge-mt5.pickleballify.com/415891589/demo")
MT5_DATA_API_KEY = _config.get("MT5_DATA_API_KEY", "ak_nNfPupc8RUojoNk5C-X2hWWgFDqq9VgUSKqfRJyncJk")

MT5_TRADE_BRIDGE_URL = _config.get("MT5_TRADE_BRIDGE_URL", _config.get("MT5_BRIDGE_URL", "https://exness-bridge-mt5.pickleballify.com/277746877/demo"))
MT5_TRADE_API_KEY = _config.get("MT5_TRADE_API_KEY", _config.get("MT5_API_KEY", "ak_v9YwdXYOWwwN-uSX8-0b9G-EHwRXpTISr438S41qdao"))

TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

COIN_NAME = "BTCUSDT"
SYMBOL = "BTCUSD"
STARTUTC = pd.Timestamp("2017-10-01 00:00:00", tz="UTC")

LOOKBACK_15M = 2500
LOOKBACK_3M  = 5000
LOOKBACK_1M  = 6000
SCAN_SLEEP_SEC = 5

LOG_DIR = Path("/Users/Vraj/Downloads/Strategy 21 Live Logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
OUT_BUY_LOG  = LOG_DIR / "Strategy21_Live_BUY_Log.csv"
OUT_SELL_LOG = LOG_DIR / "Strategy21_Live_SELL_Log.csv"

SMOOTH_MIN_RUN = 4
RATIOS = [0.5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
RISK_PER_TRADE = 500.0

_fired_events = set()
_fe_path = LOG_DIR / "_fired_events.json"
if _fe_path.exists():
    try:
        with open(_fe_path, "r") as _f:
            _fired_events.update(json.load(_f))
    except Exception:
        pass


def save_fired_events():
    try:
        with open(_fe_path, "w") as _f:
            json.dump(list(_fired_events), _f)
    except Exception:
        pass


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def tg_post(text: str):
    try:
        r = requests.post(TELEGRAM_URL, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
        if r.status_code != 200:
            log(f"Telegram HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log(f"Telegram post exception: {e}")


def mt5_bridge_trade(symbol: str, action_type: int, volume: float, sl: float = 0.0):
    try:
        url = f"{MT5_TRADE_BRIDGE_URL}/trade"
        headers = {"X-Api-Key": MT5_TRADE_API_KEY, "Content-Type": "application/json"}
        payload = {
            "action": 1,
            "symbol": symbol,
            "volume": float(volume),
            "type": action_type,
            "price": 0.0,
            "sl": float(sl),
            "magic": 21001,
            "comment": "S21 Live Bridge"
        }
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        log(f"MT5 Bridge Trade response HTTP {r.status_code}: {r.text[:300]}")
        return r.status_code == 200
    except Exception as e:
        log(f"MT5 Bridge Trade exception: {e}")
        return False


# =============================================================================
# FORMATTERS
# =============================================================================

def _f(val: Any, decimals: int = 6) -> str:
    if val is None or pd.isna(val):
        return "None"
    try:
        return f"{float(val):.{decimals}f}".rstrip("0").rstrip(".") if decimals > 0 else str(val)
    except Exception:
        return str(val)


def _s(val: Any) -> str:
    if val is None or pd.isna(val):
        return "None"
    return str(val)


def _kv_lines(obj: Any, indent: int = 0) -> List[str]:
    sp = " " * indent
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
# DATA LOADING & INDICATORS
# =============================================================================

_candle_cache = {}


def get_rolling_mt5_candles(symbol: str, timeframe: str, required_lookback: int) -> pd.DataFrame:
    global _candle_cache
    if _candle_cache.get(timeframe) is None:
        df = fetch_live_mt5_candles(symbol, timeframe, required_lookback)
        _candle_cache[timeframe] = df
        return df
    else:
        new_df = fetch_live_mt5_candles(symbol, timeframe, 10)
        if new_df is None or len(new_df) == 0:
            return _candle_cache[timeframe]
        df = pd.concat([_candle_cache[timeframe].reset_index(), new_df.reset_index()])
        df = df.drop_duplicates(subset=["datetime"], keep="last")
        df = df.sort_values("datetime")
        df = df.tail(required_lookback).set_index("datetime")
        _candle_cache[timeframe] = df
        return df


def fetch_live_mt5_candles(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    try:
        url = f"{MT5_DATA_BRIDGE_URL}/market/candles/{symbol}?timeframe={timeframe}&count={count}"
        r = requests.get(url, headers={"X-Api-Key": MT5_DATA_API_KEY}, timeout=20)
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
        log(f"Exness fetch error [{symbol} {timeframe}]: {e}")
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]).set_index("datetime")


def _pine_ema_series(series: pd.Series, length: int) -> pd.Series:
    alpha = 2.0 / (length + 1.0)
    out = np.empty(len(series), dtype=float)
    out[:] = np.nan
    vals = series.values
    first_idx = -1
    for i in range(len(vals)):
        if not np.isnan(vals[i]):
            first_idx = i
            break
    if first_idx == -1:
        return pd.Series(out, index=series.index)
    out[first_idx] = vals[first_idx]
    for i in range(first_idx + 1, len(vals)):
        v = vals[i]
        out[i] = out[i - 1] if np.isnan(v) else (alpha * v + (1.0 - alpha) * out[i - 1])
    return pd.Series(out, index=series.index)


def _heikin_ashi_df(df: pd.DataFrame) -> pd.DataFrame:
    op = df["open"].values
    hi = df["high"].values
    lo = df["low"].values
    cl = df["close"].values
    n = len(df)
    ha_cl = (op + hi + lo + cl) / 4.0
    ha_op = np.empty(n, dtype=float)
    ha_op[0] = (op[0] + cl[0]) / 2.0
    for i in range(1, n):
        ha_op[i] = (ha_op[i - 1] + ha_cl[i - 1]) / 2.0
    ha_hi = np.maximum(hi, np.maximum(ha_op, ha_cl))
    ha_lo = np.minimum(lo, np.minimum(ha_op, ha_cl))
    return pd.DataFrame({"open": ha_op, "high": ha_hi, "low": ha_lo, "close": ha_cl}, index=df.index)


def calc_kama_line(src: np.ndarray, length: int = 14, fast_length: int = 2, slow_length: int = 30, hp_period: int = 48) -> np.ndarray:
    n = len(src)
    pi = 2.0 * np.arcsin(1.0)
    alpha1 = (np.cos(0.707 * 2 * pi / hp_period) + np.sin(0.707 * 2 * pi / hp_period) - 1.0) / np.cos(0.707 * 2 * pi / hp_period)
    a1 = np.exp(-1.414 * pi / 10.0)
    b1 = 2.0 * a1 * np.cos(np.radians(1.414 * 180.0 / 10.0))
    c2 = b1
    c3 = -a1 * a1
    c1 = 1.0 - c2 - c3

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
        for p4 in range(8, length):
            pwr[p4] = (r1_arr[p4] / max_pwr) if max_pwr != 0 else 0.0

        spx = sp = 0.0
        for p5 in range(8, length):
            if pwr[p5] >= 0.5:
                spx += p5 * pwr[p5]
                sp += pwr[p5]
        dominant_cycle = spx / sp if sp != 0 else 0.0
        if dominant_cycle < 1:
            dominant_cycle = 1.0
        sc = 2.0 / (dominant_cycle + 1.0)
        kama[i] = kama[i - 1] + sc * (src[i] - kama[i - 1]) if i >= 1 else src[i]

    return kama


def add_smooth_macd_cycles(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"].values.astype(float)
    ema12 = _pine_ema_series(df["close"], 12).values
    ema26 = _pine_ema_series(df["close"], 26).values
    macd = ema12 - ema26
    signal = _pine_ema_series(pd.Series(macd, index=df.index), 9).values
    hist = macd - signal

    colors = np.where(hist >= 0, "green", "red")
    n = len(df)
    sm_color = np.array(["none"] * n, dtype=object)
    sm_cycle = np.zeros(n, dtype=int)

    i = 0
    curr_id = 0
    while i < n:
        c = colors[i]
        j = i
        while j < n and colors[j] == c:
            j += 1
        run_len = j - i
        if run_len >= SMOOTH_MIN_RUN:
            curr_id += 1
            sm_color[i:j] = c
            sm_cycle[i:j] = curr_id
        else:
            prev_c = sm_color[i - 1] if i > 0 else "none"
            prev_id = sm_cycle[i - 1] if i > 0 else 0
            sm_color[i:j] = prev_c
            sm_cycle[i:j] = prev_id
        i = j

    df["sm_color"] = sm_color
    df["sm_cycle"] = sm_cycle
    return df


def _pine_kama_series(series: pd.Series, length: int = 14, fast: int = 2, slow: int = 30) -> pd.Series:
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
    df["ema50"] = _pine_ema_series(df["close"], 50)
    df["rsi14"] = talib.RSI(close, timeperiod=14)
    df = add_smooth_macd_cycles(df)

    ha = _heikin_ashi_df(df)
    hlc3 = (ha["high"] + ha["low"] + ha["close"]) / 3.0
    kama = _pine_kama_series(hlc3)
    emakama = _pine_ema_series(kama, 10)
    kama_line = calc_kama_line(hlc3.values)
    sma_kama = calc_sma_kama(df, 20)

    upper, middle, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)

    return {
        "idx": df.index,
        "open": df["open"].values.astype(float),
        "high": df["high"].values.astype(float),
        "low": df["low"].values.astype(float),
        "close": close,
        "ema50": df["ema50"].values.astype(float),
        "rsi14": df["rsi14"].values.astype(float),
        "sm_cycle": df["sm_cycle"].values.astype(int),
        "sm_color": df["sm_color"].values.astype(object),
        "emakama": emakama.values.astype(float),
        "kama_line": kama_line.astype(float),
        "sma_kama": sma_kama.values.astype(float),
        "bb_mid": middle.astype(float),
    }


def prepare_1m_data(df_raw: pd.DataFrame) -> Dict[str, Any]:
    df = df_raw.copy()
    return {
        "idx": df.index,
        "open": df["open"].values.astype(float),
        "high": df["high"].values.astype(float),
        "low": df["low"].values.astype(float),
        "close": df["close"].values.astype(float),
    }


# =============================================================================
# STRATEGY 21 EXACT SETUP SCANNING & LOGIC FROM STRATEGY 21_1:10 RATIOS.PY
# =============================================================================

def get_fib_levels(extreme1: float, extreme2: float, side: str) -> List[Tuple[float, str]]:
    diff = abs(extreme1 - extreme2)
    ratios = [
        (-1.5, "-1.5"), (-1.0, "-1.0"), (-0.786, "-0.786"), (-0.618, "-0.618"),
        (-0.5, "-0.5"), (-0.382, "-0.382"), (-0.236, "-0.236"), (0.0, "0.0"),
        (0.236, "0.236"), (0.382, "0.382"), (0.5, "0.5"), (0.618, "0.618"),
        (0.786, "0.786"), (1.0, "1.0"), (1.236, "1.236"), (1.382, "1.382"),
        (1.5, "1.5"), (1.618, "1.618"), (1.786, "1.786"), (2.0, "2.0"), (2.5, "2.5")
    ]
    levels = []
    for r_val, r_name in ratios:
        if side == "sell":
            price = extreme2 + r_val * diff
        else:
            price = extreme1 - r_val * diff
        levels.append((price, r_name))
    if side == "sell":
        levels.sort(key=lambda x: x[0])
    else:
        levels.sort(key=lambda x: x[0], reverse=True)
    return levels


def scan_15m_setups(arr15: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    idx = arr15["idx"]
    op = arr15["open"]
    hi = arr15["high"]
    lo = arr15["low"]
    cl = arr15["close"]
    n = len(idx)
    setups = []

    for i in range(2, n):
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


def check_3m_signal_ema50(arr3: Dict[str, Any], map_dt: pd.Timestamp, extreme_close: float, side: str) -> Optional[Tuple[int, pd.Timestamp, float]]:
    idx3 = arr3["idx"]
    op3 = arr3["open"]
    cl3 = arr3["close"]
    ema50 = arr3["ema50"]

    pos = idx3.searchsorted(map_dt, side="left")
    if pos >= len(idx3):
        return None

    for i in range(pos, min(pos + 30, len(idx3))):
        if i < 3:
            continue
        prev3_ok = True
        for p in range(i - 3, i):
            if side == "sell":
                if not (op3[p] > ema50[p] and cl3[p] > ema50[p]):
                    prev3_ok = False
                    break
            else:
                if not (op3[p] < ema50[p] and cl3[p] < ema50[p]):
                    prev3_ok = False
                    break
        if not prev3_ok:
            continue

        if side == "sell":
            if cl3[i] < ema50[i] and cl3[i] < extreme_close:
                return i, idx3[i], cl3[i]
        else:
            if cl3[i] > ema50[i] and cl3[i] > extreme_close:
                return i, idx3[i], cl3[i]
    return None


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

    if side == "sell":
        slice_hi = hi[start_pos:stage1_close_pos + 1]
        rel_h1 = int(np.argmax(slice_hi))
        h1_pos = start_pos + rel_h1
        h1_val = float(slice_hi[rel_h1])
        h1_dt = idx[h1_pos]
    else:
        slice_lo = lo[start_pos:stage1_close_pos + 1]
        rel_h1 = int(np.argmin(slice_lo))
        h1_pos = start_pos + rel_h1
        h1_val = float(slice_lo[rel_h1])
        h1_dt = idx[h1_pos]

    upd_h1_val = h1_val
    upd_h1_dt = h1_dt

    s2_s1_pos = None
    for p in range(stage1_close_pos, end_p):
        if side == "sell":
            if cl[p] > upd_h1_val:
                s2_s1_pos = p
                break
            if hi[p] > upd_h1_val:
                upd_h1_val = hi[p]
                upd_h1_dt = idx[p]
        else:
            if cl[p] < upd_h1_val:
                s2_s1_pos = p
                break
            if lo[p] < upd_h1_val:
                upd_h1_val = lo[p]
                upd_h1_dt = idx[p]

    if s2_s1_pos is None:
        return {
            "exit_found": False,
            "s1_dt": s1_dt, "s1_close": s1_close,
            "h1_val": h1_val, "h1_dt": h1_dt,
            "upd_h1_val": upd_h1_val, "upd_h1_dt": upd_h1_dt,
        }

    s2_s1_dt = idx[s2_s1_pos]
    s2_s1_close = cl[s2_s1_pos]

    red2_pos = None
    for p in range(s2_s1_pos + 1, end_p):
        if side == "sell":
            if cl[p] < op[p]:
                red2_pos = p
                break
        else:
            if cl[p] > op[p]:
                red2_pos = p
                break

    if red2_pos is None:
        return {
            "exit_found": False,
            "s1_dt": s1_dt, "s1_close": s1_close,
            "h1_val": h1_val, "h1_dt": h1_dt,
            "upd_h1_val": upd_h1_val, "upd_h1_dt": upd_h1_dt,
            "s2_s1_dt": s2_s1_dt, "s2_s1_close": s2_s1_close,
        }

    red2_dt = idx[red2_pos]
    if side == "sell":
        h2_val = float(hi[red2_pos])
    else:
        h2_val = float(lo[red2_pos])
    h2_dt = red2_dt

    upd_h2_val = h2_val
    upd_h2_dt = h2_dt

    final_exit_pos = None
    for p in range(red2_pos + 1, end_p):
        if side == "sell":
            if cl[p] > upd_h2_val:
                final_exit_pos = p
                break
            if hi[p] > upd_h2_val:
                upd_h2_val = hi[p]
                upd_h2_dt = idx[p]
        else:
            if cl[p] < upd_h2_val:
                final_exit_pos = p
                break
            if lo[p] < upd_h2_val:
                upd_h2_val = lo[p]
                upd_h2_dt = idx[p]

    if final_exit_pos is None:
        return {
            "exit_found": False,
            "s1_dt": s1_dt, "s1_close": s1_close,
            "h1_val": h1_val, "h1_dt": h1_dt,
            "upd_h1_val": upd_h1_val, "upd_h1_dt": upd_h1_dt,
            "s2_s1_dt": s2_s1_dt, "s2_s1_close": s2_s1_close,
            "red2_dt": red2_dt, "h2_val": h2_val, "h2_dt": h2_dt,
            "upd_h2_val": upd_h2_val, "upd_h2_dt": upd_h2_dt,
        }

    return {
        "exit_found": True,
        "exit_dt": idx[final_exit_pos],
        "exit_price": cl[final_exit_pos],
        "s1_dt": s1_dt, "s1_close": s1_close,
        "h1_val": h1_val, "h1_dt": h1_dt,
        "upd_h1_val": upd_h1_val, "upd_h1_dt": upd_h1_dt,
        "s2_s1_dt": s2_s1_dt, "s2_s1_close": s2_s1_close,
        "red2_dt": red2_dt, "h2_val": h2_val, "h2_dt": h2_dt,
        "upd_h2_val": upd_h2_val, "upd_h2_dt": upd_h2_dt,
    }


def compute_soft_sl(arr3: Dict[str, Any], arr1: Dict[str, Any], entry_3m_pos: int, side: str, hard_sl_exit_dt: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
    idx3 = arr3["idx"]
    cycles3 = arr3["sm_cycle"]
    colors3 = arr3["sm_color"]

    entry_cid = cycles3[entry_3m_pos]
    trigger_pos = None

    seen_cycles = []
    for p in range(entry_3m_pos, len(idx3)):
        cid = cycles3[p]
        if not seen_cycles or seen_cycles[-1] != cid:
            seen_cycles.append(cid)
        if len(seen_cycles) == 3:
            cyc_bars = np.where(cycles3 == cid)[0]
            if len(cyc_bars) >= 4:
                trigger_pos = cyc_bars[3]
            else:
                trigger_pos = cyc_bars[-1]
            break

    if trigger_pos is None:
        return {"ok": False}

    trigger_dt = idx3[trigger_pos]
    if hard_sl_exit_dt is not None and trigger_dt >= hard_sl_exit_dt:
        return {"ok": False}

    req_color = "green" if side == "sell" else "red"
    found_runs = []
    seen_cids = set()

    for p in range(trigger_pos, -1, -1):
        cid = cycles3[p]
        if cid in seen_cids:
            continue
        seen_cids.add(cid)
        if colors3[p] == req_color:
            cyc_bars = np.where(cycles3 == cid)[0]
            if len(cyc_bars) >= 15:
                found_runs.append((int(cyc_bars[0]), int(cyc_bars[-1])))
                if len(found_runs) == 2:
                    break

    if len(found_runs) < 2:
        return {"ok": False}

    run1_start, run1_end = found_runs[0]
    run2_start, run2_end = found_runs[1]
    zone_start = run2_start
    zone_end = run1_end

    emakama_v = arr3["emakama"][zone_start:zone_end + 1]
    kline_v = arr3["kama_line"][zone_start:zone_end + 1]
    sma_v = arr3["sma_kama"][zone_start:zone_end + 1]
    bb_v = arr3["bb_mid"][zone_start:zone_end + 1]
    ema50_v = arr3["ema50"][zone_start:zone_end + 1]
    sub_idx3 = idx3[zone_start:zone_end + 1]

    if side == "sell":
        v_emakama = float(np.max(emakama_v))
        v_kline = float(np.max(kline_v))
        v_sma = float(np.max(sma_v))
        v_bb = float(np.max(bb_v))
        v_ema50 = float(np.max(ema50_v))
        init_sl_val = max(v_emakama, v_kline, v_sma, v_bb, v_ema50)
    else:
        v_emakama = float(np.min(emakama_v))
        v_kline = float(np.min(kline_v))
        v_sma = float(np.min(sma_v))
        v_bb = float(np.min(bb_v))
        v_ema50 = float(np.min(ema50_v))
        init_sl_val = min(v_emakama, v_kline, v_sma, v_bb, v_ema50)

    best_dt = sub_idx3[0]
    for arr_sub in (emakama_v, kline_v, sma_v, bb_v, ema50_v):
        for idx_sub, val_sub in enumerate(arr_sub):
            if np.isclose(val_sub, init_sl_val):
                best_dt = sub_idx3[idx_sub]
                break

    map_3m_1m_dt = trigger_dt + timedelta(minutes=3)
    pos_1m = arr1["idx"].searchsorted(map_3m_1m_dt, side="left")
    if pos_1m >= len(arr1["idx"]):
        return {"ok": False}

    max_1m_pos = None
    if hard_sl_exit_dt is not None:
        max_1m_pos = arr1["idx"].searchsorted(hard_sl_exit_dt, side="left")

    s2_res = track_stage2_exit(arr1, pos_1m, init_sl_val, side, max_pos=max_1m_pos)

    return {
        "ok": True,
        "entry_cyc_start": idx3[np.where(cycles3 == entry_cid)[0][0]],
        "cyc3_end": trigger_dt,
        "trigger_dt": trigger_dt,
        "zone_start": idx3[zone_start],
        "zone_end": idx3[zone_end],
        "init_vals": {
            "emakama": v_emakama, "kama_line": v_kline,
            "sma_kama": v_sma, "bb_mid": v_bb,
            "ema50": v_ema50, "ext_dt": best_dt
        },
        "init_sl_val": init_sl_val,
        "map_3m_1m_dt": map_3m_1m_dt,
        "stage2": s2_res
    }


def run_backtest_engine(arr1: Dict[str, Any], entry_pos_1m: int, entry_price: float,
                        sl_points: float, hard_sl_price: float,
                        hard_sl_exit_dt: Optional[pd.Timestamp],
                        soft_sl_exit_dt: Optional[pd.Timestamp],
                        side: str) -> Tuple[Dict[float, Dict[str, Any]], str]:
    idx1 = arr1["idx"]
    hi = arr1["high"]
    lo = arr1["low"]
    cl = arr1["close"]

    targets = {}
    for r in RATIOS:
        if side == "sell":
            targets[r] = entry_price - sl_points * r
        else:
            targets[r] = entry_price + sl_points * r

    results = {}
    done = {r: False for r in RATIOS}
    cur_sl = {r: hard_sl_price for r in RATIOS}
    sl_label = {r: "Hard SL" for r in RATIOS}

    def record(r_val, st, ep, et, due_sl=None):
        hrs = (et - idx1[entry_pos_1m]).total_seconds() / 3600.0
        pnl = (entry_price - ep) if side == "sell" else (ep - entry_price)
        results[r_val] = {
            "exit_status": st, "exit_datetime": et, "exit_price": _f(ep),
            "sl_hit_due_to": due_sl, "holding_hours": _f(hrs, 2), "pnl": _f(pnl, 4)
        }
        done[r_val] = True

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
        lines.append(vkv(results[r]))
    return results, "\n".join(lines)


# =============================================================================
# TELEGRAM NOTIFICATIONS & BRIDGE EXECUTIONS
# =============================================================================

def format_telegram_entry(trade_id: str, s: dict, map_3m_dt: pd.Timestamp,
                          entry_dt: pd.Timestamp, entry_price: float,
                          hard_sl_from: str, hard_sl_price: float, hard_sl_pct: float,
                          sl_price_for_qty: float, sl_pct_for_qty: float, qty: float,
                          side: str) -> str:
    icon = "🟢 BUY" if side == "buy" else "🔴 SELL"
    ema_check_txt = "Previous 3 minute candle open and close below EMA 50" if side == "buy" else "Previous 3 minute candle open and close above EMA 50"
    close_check_txt = "Candle closing above Lowest close Value" if side == "buy" else "Candle closing below Highest close Value"
    return (
        f"{icon} {COIN_NAME}\n"
        f"Asset Name: {COIN_NAME}\n"
        f"Strategy: Strategy 21 Live Exness Bridge\n"
        f"Trade Entry Id: {trade_id}\n"
        f"15min Setup Starttime: {s['start_dt']}\n"
        f"15min Setup Endtime: {s['end_dt']}\n"
        f"15min candle mapped to 3min is: {map_3m_dt}\n"
        f"{ema_check_txt}: yes\n"
        f"{close_check_txt}: yes\n"
        f"Status : Intrade\n"
        f"Entry Datetime: {entry_dt}\n"
        f"Entry Price: {_f(entry_price)}\n"
        f"Hard SL obtained from: {hard_sl_from}\n"
        f"Hard SL Value: {_f(hard_sl_price)}\n"
        f"Hard SL Percentage: {_f(hard_sl_pct, 4)}\n"
        f"SL Value Consider for Qty: {_f(sl_price_for_qty, 4)}\n"
        f"SL Value Percentage consider for Qty: {_f(sl_pct_for_qty, 4)}\n"
        f"Qty: {_f(qty, 4)}"
    )


def format_telegram_exit(trade_id: str, ratio_str: str, exit_dt_str: str,
                         exit_price: str, due: str, status: str, side: str) -> str:
    icon = "🟢 BUY" if side == "buy" else "🔴 SELL"
    return (
        f"🔔 EXIT ALERT ({icon})\n"
        f"Trade Entry Id: {trade_id}\n"
        f"Ratios Exited: {ratio_str}\n"
        f"Exit Datetime: {exit_dt_str}\n"
        f"Exit Price: {exit_price}\n"
        f"Exit Status: {status}\n"
        f"SL hit Due to: {due}\n"
        f"⚠️ Theoretical Simulation Exit based on Strategy 21 Live Bridge"
    )


def process_telegram_exits(trade_id: str, row: dict, side: str):
    ratios = RATIOS
    sl_groups = {}

    for r in ratios:
        lbl = f"1:{r}" if r != 0.5 else "1:0.5"
        exit_dt_str = str(row.get(f"{lbl} Exit Datetime", "None"))
        exit_price = str(row.get(f"{lbl} Exit Price", ""))
        due = str(row.get(f"{lbl} SL hit Due to", "None"))
        status = str(row.get(f"Status {lbl}", "None"))

        if exit_dt_str == "None" or exit_dt_str == "nan" or status == "Open":
            continue

        if status == "Target Hit":
            ev_key = f"{trade_id}_TGT_{lbl}"
            if ev_key not in _fired_events:
                _fired_events.add(ev_key)
                save_fired_events()
                msg = format_telegram_exit(trade_id, lbl, exit_dt_str, exit_price, "None", "Target Hit", side)
                log(f"Sending Target Hit exit alert for {lbl}")
                tg_post(msg)
        elif "SL hit" in status or due not in ("None", "nan", None):
            key = (exit_dt_str, due, exit_price, status)
            sl_groups.setdefault(key, []).append(lbl)

    for key, r_list in sl_groups.items():
        exit_dt_str, due, exit_price, status = key
        ev_key = f"{trade_id}_SLGRP_{exit_dt_str}_{due}"
        if ev_key not in _fired_events:
            _fired_events.add(ev_key)
            save_fired_events()
            ratio_str = r_list[0] if len(r_list) == 1 else f"{r_list[0]} to {r_list[-1]}"
            msg = format_telegram_exit(trade_id, ratio_str, exit_dt_str, exit_price, due, status, side)
            log(f"Sending Grouped SL Hit exit alert for ratios {ratio_str}")
            tg_post(msg)


# =============================================================================
# MAIN STRATEGY ENGINE (LIVE PAPER TRADING)
# =============================================================================

def run_strategy21_live(arr15: Dict[str, Any], arr3: Dict[str, Any], arr1: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    setups = scan_15m_setups(arr15, side)
    idx3 = arr3["idx"]
    idx1 = arr1["idx"]
    rows = []

    valid_entries = []
    for s in setups:
        map_3m_dt = s["end_dt"] + timedelta(minutes=15)
        res = check_3m_signal_ema50(arr3, map_3m_dt, s["extreme_close"], side)
        if res is not None:
            entry_3m_pos, entry_dt, entry_price = res
            valid_entries.append((s, map_3m_dt, entry_3m_pos, entry_dt, entry_price))

    dt_groups = defaultdict(list)
    for idx_item, item in enumerate(valid_entries):
        entry_dt = item[3]
        dt_groups[entry_dt].append((idx_item, item))

    winners = set()
    for entry_dt, grp in dt_groups.items():
        grp_sorted = sorted(grp, key=lambda x: x[1][0]["start_dt"], reverse=True)
        winners.add(grp_sorted[0][0])

    last_print_date = None
    intrade_payloads = []

    for idx_item, (s, map_3m_dt, entry_3m_pos, entry_dt, entry_price) in enumerate(valid_entries):
        d = s["start_dt"].date()
        if d != last_print_date:
            log(f"[STRATEGY 21 {side.upper()}] Processing Final Strategy Candle Date: {d}")
            last_print_date = d
        log(f"[{side.upper()}] Checking setup condition for candle {s['start_dt']} -> Entry Datetime: {entry_dt}")

        row = OrderedDict()
        trade_id = f"SET-{s['start_dt'].strftime('%Y-%m-%d-%H:%M')} - ET-{entry_dt.strftime('%Y-%m-%d-%H:%M')}."

        row["15min Setup Starttime"] = _s(s["start_dt"])
        row["15min Setup Endtime"] = _s(s["end_dt"])
        row["15min Setup First candle DT"] = _s(s["dt_1"])
        row["15min Setup First candle High" if side == "sell" else "15min Setup First candle Low"] = _f(s["extreme_1"])
        row["15min Setup 2nd candle DT"] = _s(s["dt_2"])
        row["15min Setup 2nd candle High" if side == "sell" else "15min Setup 2nd candle Low"] = _f(s["extreme_2"])
        row["15min Setup 3rd candle DT"] = _s(s["dt_3"])
        row["15min Setup 3rd candle High" if side == "sell" else "15min Setup 3rd candle Low"] = _f(s["extreme_3"])
        row["Highest close Value in Setup" if side == "sell" else "Lowest close Value in Setup"] = _f(s["extreme_close"])
        row["15min candle mapped to 3min is"] = _s(map_3m_dt)
        row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "yes"
        row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "yes"
        row["Entry Datetime"] = _s(entry_dt)
        row["Entry Price"] = _f(entry_price)

        if idx_item not in winners:
            row["Status"] = "Invalidated to Entry occured at same datetime"
            rows.append(row)
            continue

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
        qty = RISK_PER_TRADE / adj_sl_points if adj_sl_points > 0 else 0.0
        invest_val = qty * entry_price

        row["Hard SL obtained from"] = hard_sl_from
        row["Hard SL Value"] = _f(hard_sl_price)
        row["Hard SL Percentage"] = _f(hard_sl_pct, 4)
        row["Actual SL Points"] = _f(sl_points, 4)
        row["SL Value Consider for Qty"] = _f(sl_price_for_qty, 4)
        row["SL Points for Qty"] = _f(adj_sl_points, 4)
        row["SL Value Percentage consider for Qty"] = _f(sl_pct_for_qty, 4)
        row["Qty"] = _f(qty, 4)
        row["Investment Value for Ratios"] = _f(invest_val)

        # Hard SL % Invalidation Filter
        if side == "buy" and hard_sl_pct <= 1.0:
            row["Status"] = "Invalidated due to Hard SL % is Less than or equal to 1 percentage"
            rows.append(row)
            continue
        elif side == "sell" and hard_sl_pct >= 1.5:
            row["Status"] = "Invalidated due to Hard SL % is Greater than or equal to 1.5 percentage"
            rows.append(row)
            continue

        row["Status"] = "Intrade"

        pos_1m = idx1.searchsorted(entry_dt, side="left")
        hsl_stage2 = track_stage2_exit(arr1, pos_1m, hard_sl_price, side)
        hard_sl_exit_dt = hsl_stage2["exit_dt"] if hsl_stage2.get("exit_found") else None

        ssl = compute_soft_sl(arr3, arr1, entry_3m_pos, side, hard_sl_exit_dt)
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

        # 100% IDENTICAL Strategy Additional Info block matching Strategy 21_1:10 Ratios.py
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

        # 100% IDENTICAL Soft SL Additional Info block matching Strategy 21_1:10 Ratios.py
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

            ss_info["Final Inital Soft SL zone Starttime :"] = _s(ssl["zone_start"])
            ss_info["Final Inital Soft SL zone endtime :"] = _s(ssl["zone_end"])
            ss_info["Final Inital Soft SL zone obtained values :"] = ""
            ss_info["Final KAMAEMA.py Values for SL :"] = _f(ssl["init_vals"].get("emakama"))
            ss_info["Final kama lines code.py Values for SL :"] = _f(ssl["init_vals"].get("kama_line"))
            ss_info["Final from kama_ma_dataset.py SMA Value for SL :"] = _f(ssl["init_vals"].get("sma_kama"))
            ss_info["Final Middle Bollinger band level Values for SL :"] = _f(ssl["init_vals"].get("bb_mid"))
            ss_info["Final EMA 50 Value for SL :"] = _f(ssl["init_vals"].get("ema50"))
            ss_info["Final Highest High Value found Inital Soft SL zone obtained values :" if side == "sell" else "Final Lowest Low Value found Inital Soft SL zone obtained values :"] = _f(ssl["init_sl_val"])

            row["Soft SL Additional info"] = vkv(ss_info)
        else:
            row["Soft SL Additional info"] = "None"

        row["Backtest Result"] = bt_text
        rows.append(row)

        if row["Status"] == "Intrade":
            intrade_payloads.append({
                "trade_id": trade_id, "s": s, "map_3m_dt": map_3m_dt,
                "entry_dt": entry_dt, "entry_price": entry_price,
                "hard_sl_from": hard_sl_from, "hard_sl_price": hard_sl_price,
                "hard_sl_pct": hard_sl_pct, "sl_price_for_qty": sl_price_for_qty,
                "sl_pct_for_qty": sl_pct_for_qty, "qty": qty, "side": side
            })

    # Send ONLY the entry alert for the previous recent most 1 Intrade signal
    if intrade_payloads:
        latest = intrade_payloads[-1]
        trade_id = latest["trade_id"]
        ev_key = f"{trade_id}_RECENT1_ENTRY"
        if ev_key not in _fired_events:
            _fired_events.add(ev_key)
            save_fired_events()
            msg = format_telegram_entry(
                latest["trade_id"], latest["s"], latest["map_3m_dt"], latest["entry_dt"],
                latest["entry_price"], latest["hard_sl_from"], latest["hard_sl_price"],
                latest["hard_sl_pct"], latest["sl_price_for_qty"], latest["sl_pct_for_qty"],
                latest["qty"], latest["side"]
            )
            log(f"Sending Telegram alert ONLY for most recent 1 Intrade Entry ({side.upper()}): {trade_id}")
            tg_post(msg)

            action_type = 0 if side == "buy" else 1
            mt5_bridge_trade(SYMBOL, action_type, latest["qty"], latest["hard_sl_price"])

    return rows


def run_live_scan():
    log("Fetching live candles from Exness MT5 bridge...")
    df15_raw = get_rolling_mt5_candles(SYMBOL, "M15", LOOKBACK_15M)
    df3_raw  = get_rolling_mt5_candles(SYMBOL, "M3", LOOKBACK_3M)
    df1_raw  = get_rolling_mt5_candles(SYMBOL, "M1", LOOKBACK_1M)

    if df15_raw.empty or df3_raw.empty or df1_raw.empty:
        log("Data missing from Exness API, skipping this scan interval...")
        return

    arr15 = prepare_15m_data(df15_raw)
    arr3  = prepare_3m_data(df3_raw)
    arr1  = prepare_1m_data(df1_raw)

    log("Running BUY Live Simulation on Exness data...")
    buy_rows = run_strategy21_live(arr15, arr3, arr1, "buy")
    df_buy = pd.DataFrame(buy_rows)
    df_buy.to_csv(OUT_BUY_LOG, index=False)
    log(f"BUY live logs saved to {OUT_BUY_LOG} ({len(df_buy)} rows)")

    log("Running SELL Live Simulation on Exness data...")
    sell_rows = run_strategy21_live(arr15, arr3, arr1, "sell")
    df_sell = pd.DataFrame(sell_rows)
    df_sell.to_csv(OUT_SELL_LOG, index=False)
    log(f"SELL live logs saved to {OUT_SELL_LOG} ({len(df_sell)} rows)")


def main():
    log("=" * 60)
    log("🚀 Strategy 21 Live Exness Bridge Script (BUY & SELL)")
    log("=" * 60)
    start_msg = f"🚀 Strategy 21 Live Exness Bridge Script Started for {COIN_NAME} (BUY & SELL)\nMonitoring 15m / 3m / 1m setups on Exness MT5 bridge..."
    log(start_msg)
    tg_post(start_msg)

    last_scan_key = None
    try:
        while True:
            now = datetime.now(timezone.utc)
            scan_key = (now.year, now.month, now.day, now.hour, now.minute)
            if now.second <= 15 and scan_key != last_scan_key:
                log(f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] Starting live Exness scan...")
                try:
                    run_live_scan()
                except Exception as e:
                    log(f"Error in scan: {e}")
                last_scan_key = scan_key
            time.sleep(SCAN_SLEEP_SEC)
    except KeyboardInterrupt:
        log("Stopped by user.")


if __name__ == "__main__":
    main()
