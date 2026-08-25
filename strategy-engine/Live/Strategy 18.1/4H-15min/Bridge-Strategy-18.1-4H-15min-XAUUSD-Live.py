#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Aug  5 16:57:11 2026

@author: Vraj
"""

# ==========================================
# TEST MODE FLAG
# ==========================================
# False -> real MT5 orders are sent through the Exness bridge.
# True  -> executions are simulated; Telegram, CSV logs and the fetched-data
#          dump still run for real.
# ==========================================

"""
Strategy 18.1 (4H-15min) Live Bridge — XAUUSD  (Exness / MT5 bridge)
===================================================================
Timeframes: 1H (setup zone + setup finding + day levels) -> 5min (entry,
            hard SL, trailing SL)

STRATEGY LOGIC SOURCE
  Every strategy function in this file is a verbatim port of
  S18.1_4H-15min.py (the XAUUSD backtest).  Nothing from the BTCUSDT
  variant of Strategy 18.1 was merged into the logic — the BTCUSDT live
  bridge was used only for the live plumbing (event IDs, Telegram, MT5
  order routing, trailing thread, CSV merge).

XAUUSD-SPECIFIC BEHAVIOUR (this is what makes it differ from BTCUSDT)
  1. Day rollover uses the TradingView FX session date (New York 17:00
     rollover), NOT UTC midnight.  arr1d carries `session_date` /
     `session_day`, exactly like the CSVs the backtest reads.
  2. Candle timestamps are normalised to true UTC first (BROKER_TZ),
     because the session date is derived from New York wall clock.
  3. Order volume is expressed in LOTS: 1 XAUUSD lot = 100 oz, so
     `Trading qty Contract` = Qty / 100 (min 0.01).
  4. Market-close flatten follows the gold session: 17:00-18:00 ET daily
     pause and the Friday 17:00 ET weekly close.

NOTE ON INTERNAL VARIABLE NAMES (inherited from the backtest template):
  arr1d  -> holds 1H candle data   (zone/setup finding, day-level lookups)
  arr1h  -> holds 5min candle data (entry search, hard SL computation)
  arr5m  -> holds 5min candle data (trailing SL backtest)
  NO 1D candles are fetched or used anywhere.

DATA PROVENANCE — Exness (MT5 bridge) prepared to match the OANDA "TV
matching" CSVs the backtest reads.  Verified against the live bridge:
  * `time` is already true UTC (H1 bar 06:00 returned while the wall clock
    read 06:35 UTC), so BROKER_TZ="UTC" is correct — same as the S17 XAUUSD
    bridge's `pd.to_datetime(..., unit="s", utc=True)`.
  * The newest candle in every response is still forming -> dropped via
    `.iloc[:-1]`, which is the live equivalent of the downloader's
    `if not candle["complete"]: continue`.
  * `tick_volume` -> `volume`; bar timestamps are bar-OPEN in both feeds.
  * The OANDA downloader applies dailyAlignment=17 / America/New_York only
    to {H2,H4,D}.  This strategy uses H1 + M5, which are plain UTC-boundary
    bars in both feeds, so no alignment parameter is needed for parity.
  * add_tradingview_session_columns() then produces the identical
    datetime_ny / session_date / session_day / utc_day / ny_day columns.
Set SAVE_FETCHED_DATA in the JSON to dump exactly what was consumed, in the
downloader's column order, for a row-for-row diff.

KNOWN FEED DIFFERENCE — session opening bar (Exness gold vs OANDA FX)
  Exness XAUUSD observes the gold pause 17:00-18:00 New York, so it emits NO
  bar at NY hour 17: measured over 3,499 live H1 bars, every session's first
  bar is NY 18:00 and its last is NY 16:00, with zero bars at NY 17:00.
  OANDA (spot FX, no pause) does produce a NY-17:00 bar — e.g. the backtest
  row for 2019-09-06 uses 21:00 UTC (= NY 17:00 EDT) as a session's first
  candle.

  Session GROUPING is unaffected: tradingview_fx_session_date() rolls at
  NY >= 17 and that hour is simply empty here, so every Exness bar still
  lands on the same session_date it would under the downloader's rule
  (verified True on all 9,498 fetched H1+M5 bars).

  What DOES differ is find_entry_multiday()'s day-N level, which reads
  `idx1d[next_session_p1d]` — the first 1H bar of the new session:
      OANDA  -> NY 17:00 bar, level active from NY 18:00
      Exness -> NY 18:00 bar, level active from NY 19:00
  So the day-N open/close level comes from a different candle, one hour
  later.  This is intentional and correct for this instrument: on Exness the
  first candle of the new gold trading day genuinely is the NY-18:00 one.
  It does mean day-2+ entries will not reproduce the OANDA backtest exactly.
  Day-1 entries (which use the setup candle's own gc_open) are unaffected.
"""

import time as _time
import threading
import hashlib
import os
import re
import json
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import numpy as np
import pandas as pd
import time
from datetime import timezone

# Capture exact script boot time for strict live entry enforcement
SCRIPT_START_TIME = pd.Timestamp.utcnow()
import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)

import talib
import sys

# =============================================================================
# CONFIG LOADING
# =============================================================================

_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, os.path.basename(__file__).replace(".py", ".json"))
with open(_CONFIG_PATH) as f:
    _config = json.load(f)

# Fleet-wide settings — Telegram credentials and the database URL — live in
# Live/engine.json, which is gitignored and deployed beside the code. The
# strategy's own file still wins for anything it sets, so a per-strategy
# override keeps working exactly as before.
_SHARED_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(_SCRIPT_DIR)), "engine.json")
if os.path.exists(_SHARED_CONFIG_PATH):
    with open(_SHARED_CONFIG_PATH) as _sf:
        _config = {**json.load(_sf), **_config}

# Absent Telegram credentials disable notifications rather than stopping the
# strategy at import. They live in Live/engine.json, which is gitignored and
# is not present in CI or on a freshly provisioned host — a KeyError here
# meant the strategy could not start at all on a box that had never had one.
BOT_TOKEN      = _config.get("BOT_TOKEN", "")
CHAT_ID        = _config.get("CHAT_ID", "")
MT5_BRIDGE_URL = _config["MT5_BRIDGE_URL"]
MT5_API_KEY    = _config["MT5_API_KEY"]
MAGIC          = int(_config.get("MAGIC", 18102))
RISK_PER_TRADE = float(_config.get("RISK_PER_TRADE", 1000.0))
TELEGRAM_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Market-close flatten settings
FLATTEN_BEFORE_WEEKEND     = bool(_config.get("FLATTEN_BEFORE_WEEKEND", True))
FLATTEN_BEFORE_DAILY_BREAK = bool(_config.get("FLATTEN_BEFORE_DAILY_BREAK", False))
FLATTEN_LEAD_MIN           = int(_config.get("FLATTEN_LEAD_MIN", 10))
TRAIL_INTERVAL_SEC         = int(_config.get("TRAIL_INTERVAL_SEC", 10))

# --- Broker clock -> true UTC -------------------------------------------------
BROKER_TZ = str(_config.get("BROKER_TZ", "UTC"))

# --- Fetched-data verification dump ------------------------------------------
SAVE_FETCHED_DATA      = bool(_config.get("SAVE_FETCHED_DATA", False))
SAVE_FETCHED_DATA_MODE = str(_config.get("SAVE_FETCHED_DATA_MODE", "snapshot"))  # snapshot | append
FETCHED_DATA_DIR       = Path(_config.get(
    "FETCHED_DATA_DIR", "./bridge/Strategy 18.1 XAUUSD Fetched Data"))

# trade_db.py lives at Live/trade_db.py — one copy for the whole fleet.
# This used to point a level higher and rely on a duplicate of the module
# sitting beside the script; those duplicates are gone.
sys.path.insert(0, os.path.dirname(os.path.dirname(_SCRIPT_DIR)))
import trade_db
trade_db.init("S18.1-4H-15min-XAUUSD-LIVE" + _config.get("STRATEGY_ID_SUFFIX", ""), _config.get("TRADE_DB_URL", ""),
              magic=MAGIC, bridge_url=MT5_BRIDGE_URL)

# =============================================================================
# CONSTANTS
# =============================================================================

COIN_NAME     = "XAUUSD"
SYMBOL        = "XAUUSD"
SYMBOL_BRIDGE = "XAUUSD"
INSTRUMENTS   = ["XAUUSD"]

# MT5 timeframe codes
TF_HTF = "H4"
TF_MTF = "M15"
TF_LTF = "M5"

# Lookback counts for live candle fetching.  Spans below are MEASURED against
# the live bridge (gold trades ~5 days a week, so bar count != calendar days):
#
#   1H (arr1d — setup zones, MACD cycles, day levels):
#     3000 usable bars ≈ 185 calendar days.  Deliberately much deeper than the
#     5min window: a zone can START months before the setup inside it, and
#     find_setup_zones_1d / build_raw_cycle_map need that history to place the
#     zone boundary and MACD cycle correctly.
#
#   5min (arr1h — entry scan, hard SL; arr5m — trailing SL backtest):
#     6000 usable bars ≈ 30 calendar days.
#
# The two windows do NOT need to match, because run_strategy18_1 now skips any
# setup whose entry search would start before the 5min data begins (see
# ltf_start_ts there).  Previously such setups silently produced
# start_pos == ze_pos == 0 and were written out as false "Invalidated" rows.
LOOKBACK_HTF = 1500   # ≈ 185 calendar days
LOOKBACK_MTF = 3000   # ≈ 40 calendar days
LOOKBACK_LTF = 6000   # ≈ 30 calendar days

# Indicator warm-up.  The backtest computes indicators over history back to
# 2016 and only then filters to STARTUTC, so it never reads a warm-up value.
# A rolling live window has no such luxury: KamaLine's high-pass + super-
# smoother recursion is seeded at bar 0 and needs ~1,500 bars on 5min before
# it converges (still 1e+22 at bar 500, sane from ~1,500).  So we fetch
# LOOKBACK + WARMUP, compute indicators over everything, then drop the warm-up
# prefix — which reproduces the backtest's STARTUTC behaviour.
# arr1d (1H) only consumes BB / MACD / EMA, so it needs far less.
WARMUP_HTF = 250
WARMUP_MTF = 1000
WARMUP_LTF = 2000

FETCH_HTF = LOOKBACK_HTF + WARMUP_HTF
FETCH_MTF = LOOKBACK_MTF + WARMUP_MTF
FETCH_LTF = LOOKBACK_LTF + WARMUP_LTF

# Safety guard
MAX_SL_DISTANCE_PCT = float(_config.get("MAX_SL_DISTANCE_PCT", 10.0))

# XAUUSD contract
CONTRACT_SIZE = 100
PRICE_DIGITS  = 3

BB_PERIOD  = 20
BB_STD     = 2.0
EMA50_LEN  = 50

HTF_LABEL  = "4H"
MTF_LABEL  = "15min"
LTF_LABEL  = "5min"

HTF_MINUTES = 240
MTF_MINUTES = 15
LTF_MINUTES = 5

ALL_RATIOS   = [0.5] + list(range(1, 101))
RATIOS_FULL  = list(range(1, 101))
TRAIL_CPS    = list(range(2, 101))
SCAN_SLEEP_SEC    = 5
RECENT_LTF_COUNT  = 10

BASE_LOG_DIR   = Path("./bridge/Strategy 18.1 4H-15min XAUUSD Live Logs")
BASE_LOG_DIR.mkdir(parents=True, exist_ok=True)
_BUY_LOG_PATH  = BASE_LOG_DIR / f"Strategy18.1_4H-15min_{SYMBOL}_BUY_live.csv"
_SELL_LOG_PATH = BASE_LOG_DIR / f"Strategy18.1_4H-15min_{SYMBOL}_SELL_live.csv"

K_EMA50   = "EMA 50"
K_EMAKAMA = "KAMAEMA"
K_KLINE   = "KamaLine"
K_MBB     = "Middle BB"
K_SMA     = "SMA(kama_ma_dataset)"
FIVE_KEYS = [K_EMA50, K_EMAKAMA, K_KLINE, K_MBB, K_SMA]

# =============================================================================
# HELPERS
# =============================================================================

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
    return "\n".join(_kv_lines(obj, 0))

def map_htf_to_mtf_start(htf_ts):
    return htf_ts + pd.Timedelta(minutes=HTF_MINUTES)

def map_mtf_to_htf(mtf_ts):
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    minutes_since = (mtf_ts - epoch).total_seconds() / 60
    floored = (minutes_since // HTF_MINUTES) * HTF_MINUTES
    return epoch + pd.Timedelta(minutes=floored)

def map_mtf_to_ltf_start(mtf_ts):
    return mtf_ts + pd.Timedelta(minutes=MTF_MINUTES)

def _ordinal(n: int) -> str:
    s = {1: "1st", 2: "2nd", 3: "3rd"}
    return s.get(n, f"{n}th")

# =============================================================================
# TRADINGVIEW FX SESSION COLUMNS
# =============================================================================

def tradingview_fx_session_date(ts_utc):
    ts = pd.Timestamp(ts_utc)

    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")

    ts_ny = ts.tz_convert("America/New_York")

    if ts_ny.hour >= 17:
        return (ts_ny + pd.Timedelta(days=1)).date()

    return ts_ny.date()


def add_tradingview_session_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()

    dt_utc = pd.DatetimeIndex(out.index)
    if dt_utc.tz is None:
        dt_utc = dt_utc.tz_localize("UTC")
    else:
        dt_utc = dt_utc.tz_convert("UTC")

    dt_ny = dt_utc.tz_convert("America/New_York")

    out["session_date"] = [str(tradingview_fx_session_date(t)) for t in dt_utc]
    out["session_day"]  = pd.to_datetime(out["session_date"]).dt.day_name().values
    out["datetime_ny"]  = dt_ny.strftime("%Y-%m-%d %H:%M:%S")
    out["utc_day"]      = dt_utc.day_name()
    out["ny_day"]       = dt_ny.day_name()

    return out


# =============================================================================
# BROKER CLOCK -> TRUE UTC
# =============================================================================

def _normalize_broker_epoch(epoch_series) -> pd.Series:
    naive = pd.to_datetime(epoch_series, unit="s")

    tzs = BROKER_TZ.strip()

    if tzs.upper() in ("UTC", "GMT", "", "0", "+0", "+00:00", "-00:00"):
        return naive.dt.tz_localize("UTC")

    m = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", tzs)
    if m:
        sign   = 1 if m.group(1) == "+" else -1
        offset = pd.Timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)) * sign
        return (naive - offset).dt.tz_localize("UTC")

    return (naive.dt.tz_localize(tzs, ambiguous="NaT", nonexistent="shift_forward")
                 .dt.tz_convert("UTC"))


_broker_offset_logged = False

def _log_broker_offset(df: pd.DataFrame, timeframe: str):
    global _broker_offset_logged
    if _broker_offset_logged or df.empty:
        return
    try:
        last  = pd.Timestamp(df.index[-1])
        now   = pd.Timestamp(datetime.now(timezone.utc))
        delta = (now - last).total_seconds() / 3600.0
        log(f"[CLOCK] BROKER_TZ={BROKER_TZ} | newest {timeframe} candle {last} "
            f"| now {now.strftime('%Y-%m-%d %H:%M:%S')} UTC | age {delta:.2f} h "
            f"(expected < ~1.1 h for H1, < ~0.2 h for M5)")
        _broker_offset_logged = True
    except Exception:
        pass


# =============================================================================
# LIVE FETCH — EXNESS VIA MT5 BRIDGE
# =============================================================================

_candle_cache = {}

def _empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["datetime", "open", "high", "low", "close", "volume"]
    ).set_index("datetime")


def fetch_live_mt5_candles(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    try:
        url = f"{MT5_BRIDGE_URL}/market/candles/{symbol}?timeframe={timeframe}&count={count}"
        r = requests.get(url, headers={"X-Api-Key": MT5_API_KEY}, timeout=8)
        r.raise_for_status()
        data = r.json()

        candles = data.get("candles", [])
        if not candles:
            return _empty_ohlcv()

        df = pd.DataFrame(candles)
        df["datetime"] = _normalize_broker_epoch(df["time"])

        if "tick_volume" in df.columns:
            df = df.rename(columns={"tick_volume": "volume"})
        elif "volume" not in df.columns:
            df["volume"] = 0

        df = df[["datetime", "open", "high", "low", "close", "volume"]]
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df = (df.dropna(subset=["datetime", "open", "high", "low", "close"])
                .sort_values("datetime")
                .drop_duplicates(subset="datetime", keep="last")
                .reset_index(drop=True)
                .set_index("datetime"))

        latest = df.index[-1].strftime("%Y-%m-%d %H:%M:%S") if len(df) else "n/a"
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log(f"[{now_utc}] Fetched {timeframe} (Latest Candle: {latest} UTC)")
        _log_broker_offset(df, timeframe)
        return df

    except Exception as e:
        log(f"Exness fetch error [{symbol} {timeframe}]: {e}")
        return _empty_ohlcv()


def get_rolling_mt5_candles(symbol: str, timeframe: str, required_lookback: int) -> pd.DataFrame:
    global _candle_cache
    key = (symbol, timeframe.upper())

    if _candle_cache.get(key) is None or len(_candle_cache[key]) == 0:
        df = fetch_live_mt5_candles(symbol, timeframe, required_lookback)
        _candle_cache[key] = df
        return df

    new_df = fetch_live_mt5_candles(symbol, timeframe, 10)
    if new_df is None or len(new_df) == 0:
        return _candle_cache[key]

    df = pd.concat([_candle_cache[key].reset_index(), new_df.reset_index()])
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    df = df.sort_values("datetime")
    df = df.tail(required_lookback).set_index("datetime")
    _candle_cache[key] = df
    return df


def latest_closed_ltf_ts() -> Optional[pd.Timestamp]:
    """Timestamp of the newest CLOSED 5min candle, for new-candle detection.

    Deliberately calls fetch_live_mt5_candles() directly instead of
    get_rolling_mt5_candles(): the rolling helper ends with
    `.tail(required_lookback)`, so polling it with a small count would
    TRUNCATE the shared M5 cache to that many rows.  The next scan would then
    top it up by only 10 candles and the strategy would run on ~12 bars and
    never find an entry.  This probe never touches _candle_cache.
    """
    df = fetch_live_mt5_candles(SYMBOL, TF_LTF, 2)
    if df is None or len(df) < 2:
        return None
    return df.index[-2]          # [-1] is the still-forming candle


def prepare_live_frame(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw is None or df_raw.empty:
        return df_raw
    return add_tradingview_session_columns(df_raw)


# =============================================================================
# FETCHED-DATA DUMP
# =============================================================================

_DUMP_COLUMNS = [
    "datetime", "datetime_ny", "session_date", "session_day",
    "utc_day", "ny_day", "open", "high", "low", "close", "volume",
]

def dump_fetched_data(df: pd.DataFrame, timeframe_label: str):
    if not SAVE_FETCHED_DATA:
        return
    if df is None or df.empty:
        return
    try:
        FETCHED_DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = df.copy().reset_index()

        out["datetime"] = pd.to_datetime(out["datetime"], utc=True).dt.tz_convert(None)

        for col in _DUMP_COLUMNS:
            if col not in out.columns:
                out[col] = None

        out = out[_DUMP_COLUMNS]

        path = FETCHED_DATA_DIR / f"{SYMBOL}_{timeframe_label}_live_fetched.csv"

        if SAVE_FETCHED_DATA_MODE.lower() == "append":
            out.insert(0, "fetch_ts", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
            out.to_csv(path, mode="a", header=not path.exists(), index=False)
        else:
            out.to_csv(path, index=False)

        log(f"[DATA-DUMP] {timeframe_label}: {len(out):,} rows -> {path}")
    except Exception as e:
        log(f"[DATA-DUMP] failed for {timeframe_label}: {e}")


def log_data_summary(df: pd.DataFrame, timeframe_label: str):
    if not SAVE_FETCHED_DATA: return
    if df is None or df.empty:
        log(f"[DATA] {timeframe_label}: EMPTY")
        return
    first = df.index[0]
    last  = df.index[-1]
    last_ny  = df["datetime_ny"].iloc[-1]  if "datetime_ny"  in df.columns else "n/a"
    last_sd  = df["session_date"].iloc[-1] if "session_date" in df.columns else "n/a"
    last_sdy = df["session_day"].iloc[-1]  if "session_day"  in df.columns else "n/a"
    log(f"[DATA] {timeframe_label}: {len(df):,} rows | first {first} UTC | "
        f"last {last} UTC | last NY {last_ny} | session {last_sd} ({last_sdy})")

# =============================================================================
# INDICATORS
# =============================================================================

def _pine_ema_series(series: pd.Series, length: int) -> pd.Series:
    alpha = 2.0 / (length + 1)
    out   = np.zeros(len(series))
    out[0] = float(series.iloc[0])
    for i in range(1, len(series)):
        out[i] = alpha * float(series.iloc[i]) + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)

def _heikin_ashi_df(df: pd.DataFrame) -> pd.DataFrame:
    ha       = pd.DataFrame(index=df.index)
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open  = np.zeros(len(df))
    ha_open[0] = (float(df["open"].iloc[0]) + float(df["close"].iloc[0])) / 2.0
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + float(ha_close.iloc[i - 1])) / 2.0
    ha["open"]  = ha_open
    ha["close"] = ha_close.values
    ha["high"]  = np.maximum.reduce([df["high"].values, ha_open, ha_close.values])
    ha["low"]   = np.minimum.reduce([df["low"].values,  ha_open, ha_close.values])
    return ha

def _pine_kama_series(series: pd.Series, length=5, fast=2.5, slow=20) -> pd.Series:
    xvnoise = abs(series - series.shift(1))
    nsignal = abs(series - series.shift(length))
    nnoise  = xvnoise.rolling(length).sum()
    nfast   = 2.0 / (fast + 1)
    nslow   = 2.0 / (slow + 1)
    kama    = np.zeros(len(series))
    for i in range(len(series)):
        if i == 0:
            kama[0] = 0.0
            continue
        sig = nsignal.iloc[i] if not np.isnan(nsignal.iloc[i]) else 0.0
        noi = nnoise.iloc[i]  if not np.isnan(nnoise.iloc[i])  else 0.0
        er  = sig / noi if noi != 0 else 0.0
        sc  = (er * (nfast - nslow) + nslow) ** 2
        kama[i] = kama[i - 1] + sc * (float(series.iloc[i]) - kama[i - 1])
    return pd.Series(kama, index=series.index)

def calc_sma_kama(df: pd.DataFrame, length: int = 20) -> pd.Series:
    ha   = _heikin_ashi_df(df)
    hlc3 = (ha["high"] + ha["low"] + ha["close"]) / 3.0
    return _pine_ema_series(_pine_kama_series(hlc3), length)

def calc_emakama(close: np.ndarray, er_len=10, fast_len=2, slow_len=30) -> np.ndarray:
    n       = len(close)
    kama    = np.full(n, np.nan, dtype=float)
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
        vol    = 0.0
        for j in range(i - er_len + 1, i + 1):
            vol += abs(close[j] - close[j - 1])
        er     = change / vol if vol != 0 else 0.0
        sc     = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (close[i] - kama[i - 1])
    return kama

def calc_kama_line(src: np.ndarray, length=14, fast_length=2, slow_length=30, hp_period=48) -> np.ndarray:
    n      = len(src)
    pi     = 2.0 * np.arcsin(1.0)
    alpha1 = (
        (np.cos(.707 * 2 * pi / hp_period) + np.sin(.707 * 2 * pi / hp_period) - 1.0) /
        np.cos(.707 * 2 * pi / hp_period)
    )
    a1 = np.exp(-1.414 * pi / 10.0)
    b1 = 2.0 * a1 * np.cos(1.414 * 180.0 / 10.0)
    c2 = b1
    c3 = -a1 * a1
    c1 = 1.0 - c2 - c3
    fastest = 2.0 / (fast_length + 1)
    slowest = 2.0 / (slow_length + 1)

    hp     = np.zeros(n)
    filt   = np.zeros(n)
    kama   = np.zeros(n)
    corr_a = np.zeros(length)
    r1_a   = np.zeros(length)
    r2_a   = np.zeros(length)

    for i in range(n):
        s0 = src[i]
        s1 = src[i-1] if i >= 1 else 0.0
        s2 = src[i-2] if i >= 2 else 0.0
        hp1 = hp[i-1] if i >= 1 else 0.0
        hp2 = hp[i-2] if i >= 2 else 0.0
        hp[i] = (((1 - alpha1/2)**2)*(s0-2*s1+s2) + 2*(1-alpha1)*hp1 - ((1-alpha1)**2)*hp2)

        f1 = filt[i-1] if i >= 1 else 0.0
        f2 = filt[i-2] if i >= 2 else 0.0
        hp_p = hp[i-1] if i >= 1 else 0.0
        filt[i] = c1*(hp[i]+hp_p)/2.0 + c2*f1 + c3*f2

        for lag in range(length):
            sx=sy=sxx=syy=sxy=0.0
            for count in range(lag+1):
                ix = i - count
                iy = i - lag - count
                x = filt[ix] if ix >= 0 else 0.0
                y = filt[iy] if iy >= 0 else 0.0
                sx+=x; sy+=y; sxx+=x*x; sxy+=x*y; syy+=y*y
            d = (lag*sxx - sx*sx)*(lag*syy - sy*sy)
            if d > 0:
                corr_a[lag] = (lag*sxy - sx*sy)/np.sqrt(d)

        sq_sum = np.zeros(length)
        for period in range(8, length):
            cp_=sp_=0.0
            for n2 in range(8, length):
                cp_ += corr_a[n2]*np.cos(360.0*n2/period)
                sp_ += corr_a[n2]*np.sin(360.0*n2/period)
            sq_sum[period] = cp_*cp_ + sp_*sp_

        for p2 in range(8, length):
            r2_a[p2] = r1_a[p2]
            r1_a[p2] = 0.2*sq_sum[p2]**2 + 0.8*r2_a[p2]

        max_pwr = max((r1_a[p3] for p3 in range(8, length)), default=0.0)

        pwr = np.zeros(length)
        for p4 in range(8, length):
            pwr[p4] = r1_a[p4]/max_pwr if max_pwr != 0 else 0.0

        spx_=sp__=0.0
        for p5 in range(8, length):
            if pwr[p5] >= 0.5:
                spx_ += p5*pwr[p5]
                sp__  += pwr[p5]

        dc = spx_/sp__ if sp__ != 0 else 0.0
        dc = max(8.0, min(14.0, dc))
        dc_int = int(dc)

        idx_dc = i - dc_int
        src_dc = src[idx_dc] if idx_dc >= 0 else 0.0
        num    = abs(s0 - src_dc)
        denom  = sum(abs(src[i-j]-src[i-j-1]) for j in range(dc_int)
                     if i-j >= 0 and i-j-1 >= 0)
        er  = num/denom if denom != 0 else 0.0
        sc  = (er*(fastest-slowest)+slowest)**2
        kprev = kama[i-1] if i > 0 else 0.0
        kama[i] = kprev + sc*(s0 - kprev)

    return kama


# =============================================================================
# PREPARE DATAFRAME
# =============================================================================

def prepare_df(df_raw: pd.DataFrame) -> pd.DataFrame:
    df    = df_raw.copy()
    close = df["close"].values.astype(float)
    op    = df["open"].values.astype(float)
    hi    = df["high"].values.astype(float)
    lo    = df["low"].values.astype(float)

    bb_upper, bb_mid, bb_lower = talib.BBANDS(
        close, timeperiod=BB_PERIOD, nbdevup=BB_STD, nbdevdn=BB_STD, matype=0
    )
    df["bb_upper"] = bb_upper
    df["bb_mid"]   = bb_mid
    df["bb_lower"] = bb_lower

    df["ema50"]        = talib.EMA(close, timeperiod=EMA50_LEN)
    df["emakama"]      = pd.Series(calc_emakama(close), index=df.index)
    ohlc4              = (op + hi + lo + close) / 4.0
    df["kama_line"]    = pd.Series(calc_kama_line(ohlc4), index=df.index)
    df["sma_kama"]     = calc_sma_kama(df, length=20)
    
    return df

def add_raw_macd_cycles(df: pd.DataFrame) -> pd.DataFrame:
    _, _, hist_raw = talib.MACD(
        df["close"].values.astype(float),
        fastperiod=12, slowperiod=26, signalperiod=9
    )
    sign  = np.where(np.isnan(hist_raw), 0.0, np.sign(hist_raw))
    cycle = (pd.Series(sign, index=df.index).diff().ne(0).cumsum().values.astype(int))
    color = np.where(sign > 0, "Green", np.where(sign < 0, "Red", "Flat"))
    df["raw_hist"]  = hist_raw
    df["raw_sign"]  = sign
    df["raw_cycle"] = cycle
    df["raw_color"] = color
    return df

def make_fast_arrays_full(df: pd.DataFrame) -> Dict[str, Any]:
    fa: Dict[str, Any] = {
        "idx":          df.index,
        "open":         df["open"].values.astype(float),
        "high":         df["high"].values.astype(float),
        "low":          df["low"].values.astype(float),
        "close":        df["close"].values.astype(float),
        "volume":       df["volume"].values.astype(float),
    }
    for col in ["bb_upper", "bb_mid", "bb_lower", "ema50",
                "emakama", "kama_line", "sma_kama"]:
        if col in df.columns:
            fa[col] = df[col].values.astype(float)
    if "raw_cycle" in df.columns:
        fa["raw_cycle"] = df["raw_cycle"].values.astype(int)
        fa["raw_color"] = df["raw_color"].values.astype(object)
    if "session_date" in df.columns:
        fa["session_date"] = df["session_date"].values.astype(str)
    if "session_day" in df.columns:
        fa["session_day"] = df["session_day"].values.astype(str)
    return fa

def make_fast_arrays_ohlcv(df: pd.DataFrame) -> Dict[str, Any]:
    fa = {
        "idx":    df.index,
        "open":   df["open"].values.astype(float),
        "high":   df["high"].values.astype(float),
        "low":    df["low"].values.astype(float),
        "close":  df["close"].values.astype(float),
        "volume": df["volume"].values.astype(float),
    }
    if "session_date" in df.columns:
        fa["session_date"] = df["session_date"].values.astype(str)
    if "session_day" in df.columns:
        fa["session_day"] = df["session_day"].values.astype(str)
    return fa


# =============================================================================
# INDICATOR PICKERS
# =============================================================================

def six_values_at(arr: Dict[str, Any], pos: int) -> Dict[str, float]:
    def safe(k):
        v = float(arr[k][pos])
        return v if not np.isnan(v) else np.nan
    return {
        K_EMA50:   safe("ema50"),
        K_EMAKAMA: safe("emakama"),
        K_KLINE:   safe("kama_line"),
        K_MBB:     safe("bb_mid"),
        K_SMA:     safe("sma_kama"),
            }

def lowest_of_dict(vals: Dict[str, float]) -> Tuple[float, str]:
    valid = {k: v for k, v in vals.items() if v is not None and not np.isnan(v)}
    if not valid:
        return np.nan, "N/A"
    name = min(valid, key=lambda k: valid[k])
    return valid[name], name

def highest_of_dict(vals: Dict[str, float]) -> Tuple[float, str]:
    valid = {k: v for k, v in vals.items() if v is not None and not np.isnan(v)}
    if not valid:
        return np.nan, "N/A"
    name = max(valid, key=lambda k: valid[k])
    return valid[name], name

def five_candles_bb_values_at(arr1h: Dict[str, Any], pos: int, side: str) -> Tuple[Dict[str, float], Dict[str, str], str, str, float]:
    start = max(0, pos - 4)
    idx = arr1h["idx"]
    w_pos = list(range(start, pos + 1))
    
    start_ts_str = _s(idx[start])
    end_ts_str   = _s(idx[pos])
    
    vals_dict = {}
    ts_dict   = {}
    
    if side == "buy":
        extreme_hl_val = float(max(arr1h["high"][p] for p in w_pos))
    else:
        extreme_hl_val = float(min(arr1h["low"][p] for p in w_pos))
        
    for k in FIVE_KEYS:
        key = {
            K_EMA50:   "ema50",
            K_EMAKAMA: "emakama",
            K_KLINE:   "kama_line",
            K_MBB:     "bb_mid",
            K_SMA:     "sma_kama",
                    }[k]
        
        cand = []
        for p in w_pos:
            v = float(arr1h[key][p])
            if not np.isnan(v):
                cand.append((v, _s(idx[p])))
        
        if not cand:
            vals_dict[k] = np.nan
            ts_dict[k]   = None
        else:
            if side == "buy":
                best_v, best_ts = max(cand, key=lambda x: x[0])
            else:
                best_v, best_ts = min(cand, key=lambda x: x[0])
            vals_dict[k] = best_v
            ts_dict[k]   = best_ts
            
    return vals_dict, ts_dict, start_ts_str, end_ts_str, extreme_hl_val

def build_raw_cycle_map(arr: Dict[str, Any]) -> List[Dict[str, Any]]:
    cyc = arr["raw_cycle"]
    col = arr["raw_color"]
    idx = arr["idx"]
    out = []
    uniq = pd.unique(cyc)
    for cid in uniq:
        pos = np.where(cyc == cid)[0]
        if len(pos) == 0:
            continue
        sp = int(pos[0])
        ep = int(pos[-1])
        out.append({
            "cycle_id":  int(cid),
            "start_pos": sp,
            "end_pos":   ep,
            "start_ts":  idx[sp],
            "end_ts":    idx[ep],
            "color":     str(col[sp]),
        })
    return out


# =============================================================================
# PHASE 1-4: ZONES / SETUPS / ENTRY / HARD SL
# =============================================================================

def find_setup_zones_1d(arr1d: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    tgt_color = "Green" if side == "sell" else "Red"
    idx       = arr1d["idx"]
    n         = len(idx)
    raw_color = arr1d["raw_color"]
    bb_upper  = arr1d["bb_upper"]
    bb_lower  = arr1d["bb_lower"]
    hi_arr    = arr1d["high"]
    lo_arr    = arr1d["low"]
    raw_cycle = arr1d["raw_cycle"]

    def touches(pos: int) -> bool:
        if side == "sell":
            return (not np.isnan(bb_upper[pos])) and (hi_arr[pos] >= bb_upper[pos])
        else:
            return (not np.isnan(bb_lower[pos])) and (lo_arr[pos] <= bb_lower[pos])

    zones: List[Dict[str, Any]] = []
    zone_start_pos: Optional[int] = None
    zone_start_ts:  Optional[pd.Timestamp] = None
    zone_start_cyc: Optional[int] = None

    for pos in range(n):
        color = str(raw_color[pos])
        cyc   = int(raw_cycle[pos])

        if color == tgt_color and touches(pos):
            if zone_start_pos is None:
                zone_start_pos = pos
                zone_start_ts  = idx[pos]
                zone_start_cyc = cyc
            else:
                if cyc != zone_start_cyc:
                    zones.append({
                        "start_pos": zone_start_pos,
                        "start_ts":  zone_start_ts,
                        "start_cyc": zone_start_cyc,
                        "end_pos":   pos,
                        "end_ts":    idx[pos],
                        "end_cyc":   cyc,
                        "open":      False,
                    })
                    zone_start_pos = pos
                    zone_start_ts  = idx[pos]
                    zone_start_cyc = cyc

    if zone_start_pos is not None:
        zones.append({
            "start_pos": zone_start_pos,
            "start_ts":  zone_start_ts,
            "start_cyc": zone_start_cyc,
            "end_pos":   n - 1,
            "end_ts":    idx[n - 1],
            "end_cyc":   None,
            "open":      True,
        })

    return zones

def find_setups_in_zone(
    arr1d: Dict[str, Any],
    zone_start_pos: int,
    zone_end_pos: int,
    side: str,
) -> List[Dict[str, Any]]:
    idx    = arr1d["idx"]
    op_arr = arr1d["open"]
    cl_arr = arr1d["close"]
    mb_arr = arr1d["bb_mid"]

    anchor_open: Optional[float] = None
    anchor_ts:   Optional[pd.Timestamp] = None
    anchor_pos:  Optional[int] = None
    look_count:  int = 0

    trigger_found    = False
    trigger_ts:      Optional[pd.Timestamp] = None
    trigger_close:   Optional[float] = None
    trigger_anchor_open: Optional[float] = None
    trigger_anchor_ts:   Optional[pd.Timestamp] = None
    trigger_anchor_pos:  Optional[int] = None

    setups: List[Dict[str, Any]] = []
    pos = zone_start_pos

    while pos <= zone_end_pos:
        ts  = idx[pos]
        op  = float(op_arr[pos])
        cl  = float(cl_arr[pos])
        mb  = float(mb_arr[pos]) if not np.isnan(mb_arr[pos]) else None

        is_green = cl > op
        is_red   = cl < op

        if trigger_found:
            if (side == "sell" and is_green) or (side == "buy" and is_red):
                bb_touch_found = False
                bb_touch_ts    = None
                for p in range(trigger_anchor_pos, pos + 1):
                    if side == "buy":
                        up_val = float(arr1d["bb_upper"][p])
                        if not np.isnan(up_val) and float(arr1d["high"][p]) >= up_val:
                            bb_touch_found = True
                            bb_touch_ts = _s(idx[p])
                            break
                    else:
                        lo_val = float(arr1d["bb_lower"][p])
                        if not np.isnan(lo_val) and float(arr1d["low"][p]) <= lo_val:
                            bb_touch_found = True
                            bb_touch_ts = _s(idx[p])
                            break

                setups.append({
                    "final_gc_ts":        _s(ts),
                    "final_gc_open":      op,
                    "final_rc_ts":        _s(trigger_ts),
                    "final_rc_close":     trigger_close,
                    "anchor_ts":          _s(trigger_anchor_ts),
                    "anchor_open":        trigger_anchor_open,
                    "bb_touch_found":     bb_touch_found,
                    "bb_touch_ts":        bb_touch_ts,
                })
                trigger_found = False
                trigger_ts    = None
                trigger_close = None
                trigger_anchor_open = None
                trigger_anchor_ts   = None
                trigger_anchor_pos  = None
                anchor_open = None
                anchor_ts   = None
                anchor_pos  = None
                look_count  = 0
            pos += 1
            continue

        if anchor_open is None:
            if (side == "sell" and is_green) or (side == "buy" and is_red):
                anchor_open = op
                anchor_ts   = ts
                anchor_pos  = pos
                look_count  = 0
            pos += 1
            continue

        look_count += 1

        if (side == "sell" and is_green) or (side == "buy" and is_red):
            anchor_open = op
            anchor_ts   = ts
            anchor_pos  = pos
            look_count  = 0
            pos += 1
            continue

        if look_count > 3:
            anchor_open = None
            anchor_ts   = None
            anchor_pos  = None
            look_count  = 0
            continue

        if side == "sell":
            ok = (is_red
                  and cl < anchor_open
                  and mb is not None and cl < mb)
        else:
            ok = (is_green
                  and cl > anchor_open
                  and mb is not None and cl > mb)

        if ok:
            trigger_found        = True
            trigger_ts           = ts
            trigger_close        = cl
            trigger_anchor_open  = anchor_open
            trigger_anchor_ts    = anchor_ts
            trigger_anchor_pos   = anchor_pos
            anchor_open = None
            anchor_ts   = None
            anchor_pos  = None
            look_count  = 0

        pos += 1

    return setups

def find_entry_multiday(
    arr1h:     Dict[str, Any],
    arr1d:     Dict[str, Any],
    gc_ts:     pd.Timestamp,
    gc_open:   float,
    zone_end_ts: pd.Timestamp,
    side:      str,
) -> Tuple[List[Dict[str, Any]], bool, Optional[pd.Timestamp], Optional[float], Optional[int]]:
    idx1h   = arr1h["idx"]
    idx1d   = arr1d["idx"]
    n1h     = len(idx1h)

    op1h    = arr1h["open"]
    cl1h    = arr1h["close"]
    hi1h    = arr1h["high"]
    lo1h    = arr1h["low"]
    bb_u1h  = arr1h["bb_upper"]
    bb_l1h  = arr1h["bb_lower"]
    op1d    = arr1d["open"]
    cl1d    = arr1d["close"]

    ze_pos_1h = min(int(idx1h.searchsorted(zone_end_ts, side="left")), n1h - 1)

    next_htf_ts = map_htf_to_mtf_start(gc_ts)
    start_pos_1h  = int(idx1h.searchsorted(next_htf_ts, side="left"))

    if start_pos_1h >= n1h:
        return [], False, None, None, None

    def touches_bb_1h(pos: int) -> bool:
        if side == "sell":
            return (not np.isnan(bb_u1h[pos])) and (hi1h[pos] >= bb_u1h[pos])
        else:
            return (not np.isnan(bb_l1h[pos])) and (lo1h[pos] <= bb_l1h[pos])

    def _new_day_record(day_num: int, day_1d_ts, day_1d_color, level_type, level_val, session_dt=None, mapped_ts=None) -> Dict[str, Any]:
        return {
            "day_num":          day_num,
            "day_1d_ts":        _s(day_1d_ts),
            "session_dt":       session_dt,
            "mapped_ts":        mapped_ts,
            "day_1d_color":     day_1d_color,
            "day_level_type":   level_type,
            "day_level":        level_val,
            "bb_found":         False,
            "bb_ts":            None,
            "bb_vals":          None,
            "bb_ext_val":       None,
            "bb_ext_name":      None,
            "level_a_closed":   False,
            "level_a_closed_ts": None,
            "level_b_closed":   False,
            "level_b_closed_ts": None,
            "entry_found":      False,
            "entry_ts":         None,
            "entry_price":      None,
        }

    day_records: List[Dict[str, Any]] = [
        _new_day_record(1, gc_ts,
                        "Green" if side == "sell" else "Red",
                        "1st Setup GC Open" if side == "sell" else "1st Setup RC Open",
                        gc_open)
    ]

    entry_found: bool              = False
    entry_ts:    Optional[pd.Timestamp] = None
    entry_price: Optional[float]   = None
    entry_day:   Optional[int]     = None

    gc_pos_1d = int(idx1d.searchsorted(gc_ts))
    curr_session_date = arr1d["session_date"][gc_pos_1d]
    next_session_p1d = gc_pos_1d
    n1d = len(idx1d)
    while next_session_p1d < n1d and arr1d["session_date"][next_session_p1d] == curr_session_date:
        next_session_p1d += 1

    day_num_cur  = 1

    for pos in range(start_pos_1h, ze_pos_1h + 1):
        ts          = idx1h[pos]
        cl          = float(cl1h[pos])

        if next_session_p1d < n1d:
            # First 1H bar of the next FX session.  On Exness gold that is the
            # NY-18:00 bar (the 17:00-18:00 pause leaves NY hour 17 empty); on
            # the OANDA data the backtest used it was the NY-17:00 bar.  See
            # "KNOWN FEED DIFFERENCE" in the module docstring.
            htf_session_open_ts = idx1d[next_session_p1d]
            rollover_ts = htf_session_open_ts + pd.Timedelta(minutes=HTF_MINUTES)
            
            if ts >= rollover_ts:
                day_num_cur += 1
                
                d_op = float(op1d[next_session_p1d])
                d_cl = float(cl1d[next_session_p1d])
                is_green_1d = d_cl > d_op

                if side == "sell":
                    new_level      = d_op if is_green_1d else d_cl
                    new_color      = "Green" if is_green_1d else "Red"
                    new_level_type = "open"  if is_green_1d else "close"
                else:
                    new_level      = d_op if (not is_green_1d) else d_cl
                    new_color      = "Red"  if (not is_green_1d) else "Green"
                    new_level_type = "open" if (not is_green_1d) else "close"

                day_records.append(
                    _new_day_record(day_num_cur, htf_session_open_ts,
                                    new_color, new_level_type, new_level,
                                    arr1d["session_date"][next_session_p1d],
                                    _s(rollover_ts))
                )
                
                curr_session_date = arr1d["session_date"][next_session_p1d]
                while next_session_p1d < n1d and arr1d["session_date"][next_session_p1d] == curr_session_date:
                    next_session_p1d += 1

        for dr in day_records:
            if dr["entry_found"]:
                continue

            if not dr["bb_found"] and touches_bb_1h(pos):
                vals_dict, ts_dict, start_ts_str, end_ts_str, extreme_hl_val = five_candles_bb_values_at(arr1h, pos, side)
                if side == "sell":
                    ext_val, ext_name = lowest_of_dict(vals_dict)
                else:
                    ext_val, ext_name = highest_of_dict(vals_dict)
                dr["bb_found"]       = True
                dr["bb_ts"]          = _s(ts)
                dr["bb_vals"]        = {k: _f(v) for k, v in vals_dict.items()}
                dr["bb_ind_ts"]      = ts_dict
                dr["bb_start_ts"]    = start_ts_str
                dr["bb_end_ts"]      = end_ts_str
                dr["bb_extreme_val"] = _f(extreme_hl_val)
                dr["bb_ext_val"]     = _f(ext_val)
                dr["bb_ext_name"]    = ext_name

            if not dr["bb_found"]:
                lev_a = dr["day_level"]
                if not dr["level_a_closed"]:
                    if (side == "sell" and cl < lev_a) or (side == "buy" and cl > lev_a):
                        dr["level_a_closed"]    = True
                        dr["level_a_closed_ts"] = _s(ts)
                continue

            lev_a  = dr["day_level"]
            lev_b_raw = dr["bb_ext_val"]
            if lev_b_raw is None:
                continue
            lev_b = float(lev_b_raw)

            if not dr["level_a_closed"]:
                if (side == "sell" and cl < lev_a) or (side == "buy" and cl > lev_a):
                    dr["level_a_closed"]    = True
                    dr["level_a_closed_ts"] = _s(ts)

            if not dr["level_b_closed"]:
                if (side == "sell" and cl < lev_b) or (side == "buy" and cl > lev_b):
                    dr["level_b_closed"]    = True
                    dr["level_b_closed_ts"] = _s(ts)

            if dr["level_a_closed"] and dr["level_b_closed"]:
                a_ok = (side == "sell" and cl < lev_a) or (side == "buy" and cl > lev_a)
                b_ok = (side == "sell" and cl < lev_b) or (side == "buy" and cl > lev_b)

                if a_ok or b_ok:
                    dr["entry_found"] = True
                    dr["entry_ts"]    = _s(ts)
                    dr["entry_price"] = _f(cl)
                    if not entry_found:
                        entry_found = True
                        entry_ts    = ts
                        entry_price = cl
                        entry_day   = dr["day_num"]

        if entry_found:
            break

    return day_records, entry_found, entry_ts, entry_price, entry_day

def compute_hard_sl_1h(
    arr1h:        Dict[str, Any],
    entry_pos_1h: int,
    side:         str,
) -> Dict[str, Any]:
    start = max(0, entry_pos_1h - 4)
    idx   = arr1h["idx"]
    w_pos = list(range(start, entry_pos_1h + 1))

    if side == "sell":
        hi_vals = arr1h["high"][start: entry_pos_1h + 1]
        rel     = int(np.argmax(hi_vals))
        ext_pos = start + rel
        ext_val = float(arr1h["high"][ext_pos])
        cand    = [(ext_val, _s(idx[ext_pos]), "Highest High")]

        for p in w_pos:
            for k in FIVE_KEYS:
                key = {K_EMA50: "ema50", K_EMAKAMA: "emakama",
                       K_KLINE: "kama_line", K_MBB: "bb_mid",
                       K_SMA:   "sma_kama",  }[k]
                v = float(arr1h[key][p])
                if not np.isnan(v):
                    cand.append((v, _s(idx[p]), k))

        best_val, best_ts, best_name = max(cand, key=lambda x: x[0])
        return {
            "prev5_high_val": _f(ext_val),
            "prev5_high_ts":  _s(idx[ext_pos]),
            "prev5_ind_val":  None,
            "prev5_ind_name": None,
            "hard_sl":        _f(best_val),
            "hard_sl_from":   best_name,
            "hard_sl_ts":     best_ts,
        }

    else:
        lo_vals = arr1h["low"][start: entry_pos_1h + 1]
        rel     = int(np.argmin(lo_vals))
        ext_pos = start + rel
        ext_val = float(arr1h["low"][ext_pos])
        cand    = [(ext_val, _s(idx[ext_pos]), "Lowest Low")]

        for p in w_pos:
            for k in FIVE_KEYS:
                key = {K_EMA50: "ema50", K_EMAKAMA: "emakama",
                       K_KLINE: "kama_line", K_MBB: "bb_mid",
                       K_SMA:   "sma_kama",  }[k]
                v = float(arr1h[key][p])
                if not np.isnan(v):
                    cand.append((v, _s(idx[p]), k))

        best_val, best_ts, best_name = min(cand, key=lambda x: x[0])
        return {
            "prev5_low_val":  _f(ext_val),
            "prev5_low_ts":   _s(idx[ext_pos]),
            "prev5_ind_val":  None,
            "prev5_ind_name": None,
            "hard_sl":        _f(best_val),
            "hard_sl_from":   best_name,
            "hard_sl_ts":     best_ts,
        }


# =============================================================================
# BACKTEST — 5-MINUTE TRAILING SL
# =============================================================================

def _pct(entry: float, ref: float) -> float:
    return abs(entry - ref) / entry * 100.0 if entry != 0 else 0.0

def _tgt(entry: float, sl: float, side: str, r: float) -> float:
    risk = abs(entry - sl)
    return (entry + risk * r) if side == "buy" else (entry - risk * r)

def _pnl(exit_px: float, entry: float, qty: float, side: str) -> float:
    return (exit_px - entry) * qty if side == "buy" else (entry - exit_px) * qty

def run_backtest_5m(
    arr5m:       Dict[str, Any],
    entry_ts_1h: pd.Timestamp,
    entry_price: float,
    hard_sl:     float,
    side:        str,
) -> Tuple[Dict, str, float, float, Optional[str], Optional[float]]:
    idx5 = arr5m["idx"]
    n5   = len(idx5)

    track_start_ts = map_mtf_to_ltf_start(entry_ts_1h)
    start_pos      = int(idx5.searchsorted(track_start_ts, side="left"))
    start_pos      = min(start_pos, n5 - 1)

    hard_sl_pct = _pct(entry_price, hard_sl)
    risk        = abs(entry_price - hard_sl)
    qty         = (RISK_PER_TRADE / risk) if risk > 0 else 0.0
    invest      = qty * entry_price

    targets  = {r: _tgt(entry_price, hard_sl, side, r) for r in ALL_RATIOS}
    cur_sl   = {r: hard_sl for r in ALL_RATIOS}
    sl_label = {r: "Hard SL" for r in ALL_RATIOS}
    sl_ref   = {r: f"Hard SL|{_f(hard_sl)}" for r in ALL_RATIOS}
    done     = {r: False for r in ALL_RATIOS}
    results  = {r: OrderedDict() for r in ALL_RATIOS}
    hit_cps  = set()

    hsl_candle_ts:      Optional[str]   = None
    hsl_candle_extreme: Optional[float] = None

    def record_exit(r, status, px, ts, due=None):
        results[r] = OrderedDict({
            "SL Price":        _f(cur_sl[r]),
            "Target Price":    _f(targets[r]),
            "Exit Status":     status,
            "Exit Price":      _f(px),
            "Exit Datetime":   _s(ts),
            "SL hit Due to":   due,
            "SL Hit due to which Ratio trailing Price": (
                sl_ref[r] if str(status).startswith("SL Hit") else None),
            "Holding Time (hrs)": round(
                (ts - entry_ts_1h).total_seconds() / 3600.0, 4),
            "P/L": _f(_pnl(px, entry_price, qty, side)),
        })
        done[r] = True

    def apply_trail(cp: int):
        if cp == 2:
            anchor_px  = entry_price
            anchor_lbl = "Breakeven"
            anchor_ref = f"1:2|{_f(anchor_px)}"
        else:
            anchor_px  = targets[cp - 2]
            anchor_lbl = f"1:{cp-2} Target"
            anchor_ref = f"1:{cp}|{_f(anchor_px)}"
        for rr in RATIOS_FULL:
            if rr > cp and not done[rr]:
                cur_sl[rr]   = anchor_px
                sl_label[rr] = anchor_lbl
                sl_ref[rr]   = anchor_ref

    for pos in range(start_pos, n5):
        ts = idx5[pos]
        hi = float(arr5m["high"][pos])
        lo = float(arr5m["low"][pos])
        cl = float(arr5m["close"][pos])

        if hsl_candle_ts is None:
            if (side == "sell" and cl > hard_sl) or (side == "buy" and cl < hard_sl):
                hsl_candle_ts      = _s(ts)
                hsl_candle_extreme = _f(hi if side == "sell" else lo)

        if not done[0.5]:
            hit_tgt = (hi >= targets[0.5]) if side == "buy" else (lo <= targets[0.5])
            hit_sl  = (cl <= cur_sl[0.5]) if side == "buy" else (cl >= cur_sl[0.5])
            if hit_tgt:
                record_exit(0.5, "Target Hit", targets[0.5], ts)
            elif hit_sl:
                record_exit(0.5, f"SL Hit - {sl_label[0.5]}", cur_sl[0.5], ts, sl_label[0.5])

        for cp in TRAIL_CPS:
            if cp not in hit_cps:
                hit = (hi >= targets[cp]) if side == "buy" else (lo <= targets[cp])
                if hit:
                    hit_cps.add(cp)
                    apply_trail(cp)

        for r in RATIOS_FULL:
            if done[r]:
                continue
            hit_tgt = (hi >= targets[r]) if side == "buy" else (lo <= targets[r])
            hit_sl  = (cl <= cur_sl[r]) if side == "buy" else (cl >= cur_sl[r])
            if hit_tgt:
                record_exit(r, "Target Hit", targets[r], ts)
            elif hit_sl:
                record_exit(r, f"SL Hit - {sl_label[r]}", cur_sl[r], ts, sl_label[r])

        if all(done[r] for r in ALL_RATIOS):
            break

    for r in ALL_RATIOS:
        if not done[r]:
            last_ts = idx5[-1]
            last_px = float(arr5m["close"][-1])
            record_exit(r, "Open", last_px, last_ts)

    head = OrderedDict({
        "Entry Price":              _f(entry_price),
        "Hard SL Price":            _f(hard_sl),
        "Assign Hard SL Percentage": _f(hard_sl_pct),
        "Qty":                      _f(qty),
        "Investment Value":         _f(invest),
        "5min Tracking Start DT":   _s(track_start_ts),
        "5min Hard SL candle DT":   hsl_candle_ts,
        "5min Hard SL candle extreme": hsl_candle_extreme,
    })
    blocks = [vkv(head)]
    for r in ALL_RATIOS:
        blocks.append(vkv(OrderedDict({f"Ratio 1:{r}": results[r]})))
    bt_text = "\n\n".join(b for b in blocks if b)

    return results, bt_text, qty, invest, hsl_candle_ts, hsl_candle_extreme


# =============================================================================
# STRATEGY ADDITIONAL INFO BUILDER
# =============================================================================

def build_strategy_additional_info(
    zone:              Dict[str, Any],
    setup:             Dict[str, Any],
    day_records:       List[Dict[str, Any]],
    entry_day:         Optional[int],
    side:              str,
    zone_macd_start:   Optional[str],
    zone_macd_end:     Optional[str],
    zone_end_macd_start: Optional[str],
    zone_end_macd_end:   Optional[str],
    sl_info:           Dict[str, Any],
    hsl_tracking:      Dict[str, Any],
    start_1h_ts:       Optional[str],
    status:            str,
) -> str:
    add = OrderedDict()

    is_open = zone.get("open", False)
    add["Setup finding zone Start time"]  = _s(zone.get("start_ts"))
    add["Setup finding zone Endtime"]     = _s(zone.get("end_ts")) if not is_open else "N/A"
    add["MACD cycle Start time in which Setup Start time found"] = zone_macd_start
    add["MACD cycle End time in which Setup Start time found"]   = zone_macd_end
    
    add["MACD cycle Start time in which Setup Endtime found"] = zone_end_macd_start if not is_open else "N/A"
    add["MACD cycle End time in which Setup Endtime found"]   = zone_end_macd_end if not is_open else "N/A"
    
    if side == "sell":
        add["Candle touching upper BB DT"] = _s(zone.get("start_ts"))
    else:
        add["Candle touching Lower BB DT"] = _s(zone.get("start_ts"))

    if side == "sell":
        add["Final Green Candle Datetime"]              = setup.get("anchor_ts")
        add["Final Red Candle Closing Below GC Open"]   = setup.get("final_rc_ts")
        add["Final Red candle closes below middle BB"]  = "Yes"
        add["Candle Touching Lower BB found :"]         = "Yes" if setup.get("bb_touch_found") else "No"
        add["Candle Touching Lower BB found Datetime :"] = setup.get("bb_touch_ts") if setup.get("bb_touch_found") else "N/A"
        add["Green Candle Found after Setup RC Datetime"] = setup.get("final_gc_ts")
        add["Green Candle Found after Setup RC Open"]     = _f(setup.get("final_gc_open"))
    else:
        add["Final Red Candle Datetime"]                = setup.get("anchor_ts")
        add["Final Green Candle Closing Above RC Open"] = setup.get("final_rc_ts")
        add["Final Green candle closes above middle BB"] = "Yes"
        add["Candle Touching Upper BB found :"]          = "Yes" if setup.get("bb_touch_found") else "No"
        add["Candle Touching Upper BB found Datetime :"]  = setup.get("bb_touch_ts") if setup.get("bb_touch_found") else "N/A"
        add["Red Candle Found after Setup GC Datetime"] = setup.get("final_gc_ts")
        add["Red Candle Found after Setup GC Open"]     = _f(setup.get("final_gc_open"))

    add[f"1st found candle on {MTF_LABEL} datetime"] = start_1h_ts

    for i, dr in enumerate(day_records):
        dn = dr["day_num"]
        pfx = "" if dn == 1 else f"{_ordinal(dn)} day "

        if dn == 1:
            if side == "sell":
                add[f"1st Candle touching Upper BB on {MTF_LABEL} Datetime"] = dr.get("bb_ts")
            else:
                add[f"1st Candle touching Lower BB on {MTF_LABEL} Datetime"] = dr.get("bb_ts")
        else:
            add[f"{pfx}1D candle found DT"]              = dr.get("day_1d_ts")
            add["Session date"]                          = dr.get("session_dt")
            add[f"{HTF_LABEL} candle found mapped on {MTF_LABEL} datetime"] = dr.get("mapped_ts")
            add[f"{pfx}{HTF_LABEL} candle open or close considered"] = dr.get("day_level_type")
            add[f"{pfx}{HTF_LABEL} candle level"]                 = _f(dr.get("day_level"))
            if side == "sell":
                add[f"{pfx}Candle touching upper BB DT"] = dr.get("bb_ts")
            else:
                add[f"{pfx}Candle touching lower BB DT"] = dr.get("bb_ts")

        if dr.get("bb_vals"):
            lbl = f"{pfx}Value Found at BB touched candle"
            add[lbl] = ""
            add[f"{pfx}Previous 5 Candle Startime"] = dr.get("bb_start_ts")
            add[f"{pfx}Previous 5 Candle Endtime "]  = dr.get("bb_end_ts")
            if side == "sell":
                add[f"{pfx}Previous Candle Lowest low Value found"] = dr.get("bb_extreme_val")
            else:
                add[f"{pfx}Previous Candle Highest high Value found"] = dr.get("bb_extreme_val")

            for k in FIVE_KEYS:
                ind_ts = dr.get("bb_ind_ts", {}).get(k)
                if side == "sell":
                    add[f"{pfx}Lowest {k} found Candle Datetime"] = ind_ts
                else:
                    add[f"{pfx}Highest {k} found Candle Datetime"] = ind_ts
                add[f"  {pfx}{k}"] = dr["bb_vals"].get(k)

            add[f"{pfx}BB Ext Level"] = dr.get("bb_ext_val")
            add[f"{pfx}BB Ext from"]  = dr.get("bb_ext_name")

        if side == "sell":
            add[f"{pfx}Candle closing below GC Open"] = ("Yes" if dr.get("level_a_closed") else "Not found")
            add[f"{pfx}Candle closing below GC Open DT"] = dr.get("level_a_closed_ts")
            add[f"{pfx}Lowest BB Level closed"]       = ("Yes" if dr.get("level_b_closed") else "Not found")
            add[f"{pfx}Lowest BB Level closed DT"]    = dr.get("level_b_closed_ts")
        else:
            add[f"{pfx}Candle closing above RC Open"] = ("Yes" if dr.get("level_a_closed") else "Not found")
            add[f"{pfx}Candle closing above RC Open DT"] = dr.get("level_a_closed_ts")
            add[f"{pfx}Highest BB Level closed"]      = ("Yes" if dr.get("level_b_closed") else "Not found")
            add[f"{pfx}Highest BB Level closed DT"]   = dr.get("level_b_closed_ts")

    add["Entry Candle found on which day"] = (f"{_ordinal(entry_day)} day" if entry_day else "N/A")

    if sl_info:
        add["Assign SL Price"] = sl_info.get("hard_sl")
        if side == "sell":
            add["highest High in Previous 5 candle Value"] = sl_info.get("prev5_high_val")
        else:
            add["lowest Low in Previous 5 candle Value"]   = sl_info.get("prev5_low_val")
        add["SL indicator val"]  = sl_info.get("prev5_ind_val")
        add["SL indicator name"] = sl_info.get("prev5_ind_name")
        add["hard SL consider from"] = sl_info.get("hard_sl_from")

    if hsl_tracking:
        add["Candle mapped for tracking SL DT"] = hsl_tracking.get("track_start_ts")
        if side == "sell":
            add["5min Candle closing above hard SL Price DT"] = hsl_tracking.get("hsl_candle_ts")
            add["5min Candle closing above hard SL Price candle high"] = hsl_tracking.get("hsl_candle_extreme")
        else:
            add["5min Candle closing below hard SL Price DT"] = hsl_tracking.get("hsl_candle_ts")
            add["5min Candle closing below hard SL Price candle low"] = hsl_tracking.get("hsl_candle_extreme")

    add["Method Status"] = status

    return vkv(add)


# =============================================================================
# BASE ROW TEMPLATE
# =============================================================================

def create_base_record(side: str) -> OrderedDict:
    r = OrderedDict()

    r["Setup Start time"]  = None
    r["Setup Endtime"]     = None

    if side == "sell":
        r["Session date"]                                      = None
        r[f"{HTF_LABEL} candle found mapped on {MTF_LABEL} datetime"] = None
        r["Candle touching upper BB DT"]                       = None
        r["Final Green Candle Datetime"]                       = None
        r["Final Red Candle Closing Below GC Open Datetime"]   = None
        r["Final Red candle closes below middle BB"]           = None
        r["Setup Start time "]  = None
        r["Setup Endtime "]     = None
        r["1st Green Candle Found after Setup RC Datetime"]    = None
        r["1st Green Candle Found after Setup RC Open"]        = None
        r[f"1st Candle touching Upper BB on {MTF_LABEL} Datetime"]    = None
        r["Lowest Value at Upper BB touched candle"]           = None
        r["Lowest Value at Upper BB touched"]                  = None
    else:
        r["Session date"]                                      = None
        r[f"{HTF_LABEL} candle found mapped on {MTF_LABEL} datetime"] = None
        r["Final Red Candle Datetime"]                         = None
        r["Final Green Candle Closing Above RC Open Datetime"] = None
        r["Final Green candle closes above middle BB"]         = None
        r["Candle touching Lower BB DT"]                       = None
        r["Setup Start time "]  = None
        r["Setup Endtime "]     = None
        r["1st Red Candle Found after Setup GC Datetime"]      = None
        r["1st Red Candle Found after Setup GC Open"]          = None
        r[f"1st Candle touching Lower BB on {MTF_LABEL} Datetime"]    = None
        r["Highest Value at Lower BB touched candle"]          = None
        r["Highest Value at Lower BB touched"]                 = None

    r[f"Entry Found according to which {HTF_LABEL} level"]                              = None
    r[f"Final {HTF_LABEL} Datetime on {HTF_LABEL} TF"]                                  = None
    r[f"Final {HTF_LABEL} {MTF_LABEL} Candle touching BB DT"]                           = None
    r[f"Final {HTF_LABEL} Datetime on {HTF_LABEL} TF Candle Colour"]                    = None
    r[f"Final {HTF_LABEL} - {HTF_LABEL} TF Level"]                                      = None
    r[f"Final {HTF_LABEL} Datetime on {HTF_LABEL} TF level"]                            = None
    r[f"Final {HTF_LABEL} {MTF_LABEL} Candle touching Upper BB DT"]                     = None
    r[f"Final {HTF_LABEL} {MTF_LABEL} Candle touching upper BB Lowest Level Considered from"] = None

    if side == "sell":
        r[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing below {HTF_LABEL} TF level DT"] = None
        r[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing below Lowest Level DT"]          = None
    else:
        r[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing above {HTF_LABEL} TF level DT"] = None
        r[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing above Highest Level DT"]         = None

    r["Status"]                      = None
    r["Entry Price"]                 = None
    r["Entry Datetime"]              = None
    r["Hard SL Price"]               = None
    r["Assign Hard SL Percentage"]   = None
    r["SL Price considered from"]    = None
    r["Qty"]                         = None
    r["Trading qty Contract"]        = None
    r["Investment Value for Ratios"] = None

    r["1:0.5 Exit Datetime"]      = None
    r["1:0.5 Exit Price"]         = None
    r["1:0.5 SL hit Due to"]      = None
    r["1:0.5 Holding Time (hrs)"] = None
    r["P/L 1:0.5"]                = None

    for rt in RATIOS_FULL:
        r[f"1:{rt} Exit Datetime"]      = None
        r[f"1:{rt} Exit Price"]         = None
        r[f"1:{rt} SL hit Due to"]      = None
        r[f"1:{rt} Holding Time (hrs)"] = None
        r[f"P/L 1:{rt}"]               = None
        if rt >= 2: r[f"Status 1:{rt}"] = None

    r["Strategy Additional info"]  = None
    r["BackTest Result"]           = None
    r["Event ID"]                  = None
    r["Logged At UTC"]             = None
    r["Live MT5 Order ID"]         = None
    r["Live Entry Price"]          = None
    r["Live Corrected Hard SL"]    = None
    r["Executed Result"]           = None
    r["Executed Qty"]              = None
    r["Executed Investment Value"] = None
    r["Live Exit Datetime"]        = None
    return r

# =============================================================================
# TELEGRAM
# =============================================================================

def tg_post(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(TELEGRAM_URL, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        log(f"Telegram error: {e}")

def format_telegram(r: dict, side: str, order_id: int = 0) -> str:
    color = "🟢 BUY" if side == "buy" else "🔴 SELL"
    ep  = r.get("Entry Price", 0)
    hsl = r.get("Hard SL Price", 0)
    
    if side == "buy":
        anchor_col  = f"Final Red Candle Datetime: {r.get('Final Red Candle Datetime','')}"
        trigger_col = f"Final Green Candle Closing Above RC Open Datetime: {r.get('Final Green Candle Closing Above RC Open Datetime','')}"
        bb_col      = f"Candle touching Lower BB DT: {r.get('Candle touching Lower BB DT','')}"
        gc_col      = f"1st Red Candle Found after Setup GC Datetime: {r.get('1st Red Candle Found after Setup GC Datetime','')}"
        bb1_col     = f"1st Candle touching Lower BB on 15min Datetime: {r.get(f'1st Candle touching Lower BB on 15min Datetime','')}"
        ca_col      = f"Final 4H 15min Candle closing above 4H TF level DT: {r.get(f'Final 4H 15min Candle closing above 4H TF level DT','')}"
        cb_col      = f"Final 4H 15min Candle closing above Highest Level DT: {r.get(f'Final 4H 15min Candle closing above Highest Level DT','')}"
        up_bb_col   = f"Final 4H 15min Candle touching Upper BB DT: {r.get(f'Final 4H 15min Candle touching Upper BB DT','')}"
    else:
        anchor_col  = f"Final Green Candle Datetime: {r.get('Final Green Candle Datetime','')}"
        trigger_col = f"Final Red Candle Closing Below GC Open Datetime: {r.get('Final Red Candle Closing Below GC Open Datetime','')}"
        bb_col      = f"Candle touching Upper BB DT: {r.get('Candle touching Upper BB DT','')}"
        gc_col      = f"1st Green Candle Found after Setup RC Datetime: {r.get('1st Green Candle Found after Setup RC Datetime','')}"
        bb1_col     = f"1st Candle touching Upper BB on 15min Datetime: {r.get(f'1st Candle touching Upper BB on 15min Datetime','')}"
        ca_col      = f"Final 4H 15min Candle closing below 4H TF level DT: {r.get(f'Final 4H 15min Candle closing below 4H TF level DT','')}"
        cb_col      = f"Final 4H 15min Candle closing below Lowest Level DT: {r.get(f'Final 4H 15min Candle closing below Lowest Level DT','')}"
        up_bb_col   = f"Final 4H 15min Candle touching Lower BB DT: {r.get(f'Final 4H 15min Candle touching Lower BB DT','')}"

    msg = (
        f"{color} {SYMBOL}\n"
        f"Asset: {SYMBOL} (Theoretical)\n"
        f"Strategy: Strategy 18.1 4H-15min Live\n"
        f"Entry Signal: {'Buy' if side == 'buy' else 'Sell'}\n"
        f"Trade Entry Id: {r.get('Event ID','')}\n"
        f"Setup Start time: {r.get('Setup Start time','')}\n"
        f"{anchor_col}\n"
        f"{trigger_col}\n"
        f"{bb_col}\n"
        f"{gc_col}\n"
        f"{bb1_col}\n"
        f"Entry Found according to which 4H level: {r.get(f'Entry Found according to which 4H level','')}\n"
        f"Final 4H Datetime on 4H TF: {r.get(f'Final 4H Datetime on 4H TF','')}\n"
        f"Final 4H 15min Candle touching BB DT: {r.get(f'Final 4H 15min Candle touching BB DT','')}\n"
        f"Final 4H Datetime on 4H TF level: {r.get(f'Final 4H Datetime on 4H TF level','')}\n"
        f"{up_bb_col}\n"
        f"{ca_col}\n"
        f"{cb_col}\n"
        f"Status: {r.get('Status','')}\n"
        f"Entry Price: {_f(ep)}\n"
        f"Entry Datetime: {r.get('Entry Datetime','')}\n"
        f"Hard SL Price: {_f(hsl)}"
    )
    if order_id > 0:
        msg += f"\n\n*** LIVE MT5 ORDERS ***\nLive MT5 Order ID: {order_id}"
    return msg

def format_telegram_exit(r: dict, side: str, ratio_str: str, msg_type: str,
                          event_type: str, exit_price: float, exit_dt: str,
                          pnl: float, new_trail_applied: str, new_trail_at: str) -> str:
    return (
        f"Asset: {SYMBOL} (Theoretical)\n"
        f"Strategy: Strategy 18.1 4H-15min Live (XAUUSD)\n"
        f"Entry Signal: {side}\n"
        f"Trade Entry Id: {r.get('Event ID','')}\n"
        f"Final Entry Datetime: {r.get('Entry Datetime','')}\n"
        f"Hard SL Price: {_f(r.get('Hard SL Price', 0.0))}\n"
        f"Ratio: {ratio_str}\n"
        f"Message Type: {msg_type} (Theoretical Simulation)\n"
        f"New Trailed SL Applied: {new_trail_applied}\n"
        f"New Trail SL at: {new_trail_at}\n"
        f"Event Occured Type: {event_type}\n"
        f"Exit Price: {_f(exit_price)}\n"
        f"Exit Datetime: {exit_dt}\n"
        f"PNL: {_f(pnl)}\n\n"
        f"⚠️ Note: This is a theoretical result based on price simulation, not a live MT5 execution."
    )

def process_telegram_exits(row: dict, side: str, recent_ltf: pd.DatetimeIndex,
                           ratio_res: Optional[Dict[Any, Dict]] = None):
    """ratio_res is run_backtest_5m()'s per-ratio result map."""
    if row.get("Status") != "Intrade": return
    if not row.get("Live MT5 Order ID"): return
    ev_id = row.get("Event ID", "")
    
    # 1. Target Hits
    for r in ALL_RATIOS:
        lbl = "1:0.5" if r == 0.5 else f"1:{r}"
        exit_dt_str = str(row.get(f"{lbl} Exit Datetime", "None") or "None")
        exit_price_v = row.get(f"{lbl} Exit Price", "")
        pnl_v = row.get(f"P/L {lbl}", "")
        
        if exit_dt_str in ("None", "nan", "", "N/A"): continue
        try: exit_ts = pd.Timestamp(exit_dt_str)
        except Exception: continue
        if exit_ts not in recent_ltf: continue

        exit_status = ""
        if ratio_res:
            exit_status = str((ratio_res.get(r) or {}).get("Exit Status", "") or "")

        if "Target Hit" in exit_status:
            t_key = f"{ev_id}_{lbl}_TGT_exit"
            if t_key in _fired_events: continue
            
            try: ep = float(exit_price_v)
            except Exception: ep = 0.0
            try: pnl = float(pnl_v)
            except Exception: pnl = 0.0
            
            msg = format_telegram_exit(row, side, lbl, "Target Hit", f"{lbl} Target Hit",
                                       ep, exit_dt_str, pnl, "no", "N/A")
            log(f"\n🔔 EXIT TGT:\n{msg}")
            tg_post(msg)
            _fired_events.add(t_key)
            save_fired_events()

    # 2. SL Hits
    sl_groups = {}
    for r in ALL_RATIOS:
        lbl = "1:0.5" if r == 0.5 else f"1:{r}"
        exit_dt_str = str(row.get(f"{lbl} Exit Datetime", "None") or "None")
        exit_price_v = row.get(f"{lbl} Exit Price", "")
        due = str(row.get(f"{lbl} SL hit Due to", "None") or "None")
        status_val = str(row.get(f"Status {lbl}", "None") or "None") if r >= 2 else ""
        
        if exit_dt_str in ("None", "nan", "", "N/A"): continue
        
        exit_status = ""
        if ratio_res:
            exit_status = str((ratio_res.get(r) or {}).get("Exit Status", "") or "")

        if "SL Hit" in exit_status or "SL Hit" in due or "SL Hit" in status_val:
            key = (exit_dt_str, due, exit_price_v)
            sl_groups.setdefault(key, []).append(r)
            
    for key, r_list in sl_groups.items():
        exit_dt_str, due, exit_price_v = key
        try: exit_ts = pd.Timestamp(exit_dt_str)
        except Exception: continue
        if exit_ts not in recent_ltf: continue
        
        due_clean = due.replace("SL Hit - ", "")
        
        t_key = f"{ev_id}_SL_GRP_{exit_dt_str}_{due_clean}"
        if t_key in _fired_events: continue
        
        lbl_list = ["1:0.5" if r == 0.5 else f"1:{r}" for r in r_list]
        ratio_str = f"{lbl_list[0]} to {lbl_list[-1]}" if len(lbl_list) > 1 else lbl_list[0]
            
        is_trailed = (due_clean != "Hard SL" and due_clean != "None" and due_clean != "nan" and due_clean != "")
        if is_trailed:
            msg_type = f"Trailed SL hit - {due_clean}"
            event_type = f"SL hit - {due_clean} from {ratio_str} ratios"
        else:
            msg_type = "SL Hit"
            event_type = f"SL hit from {ratio_str} ratios"
            
        total_pnl = 0.0
        for lbl in lbl_list:
            try: total_pnl += float(str(row.get(f"P/L {lbl}", "0")))
            except Exception: pass
            
        try: ep = float(exit_price_v)
        except Exception: ep = 0.0
        
        msg = format_telegram_exit(row, side, ratio_str, msg_type, event_type,
                                   ep, exit_dt_str, total_pnl, "no", "N/A")
        log(f"\n🔔 EXIT SL:\n{msg}")
        tg_post(msg)
        _fired_events.add(t_key)
        save_fired_events()

# =============================================================================
# LIVE ENGINE STATE
# =============================================================================

_fired_events    = set()
_ticket_map      = {}
_event_to_ticket = {}

def load_fired_events():
    global _fired_events
    global _fired_entry_timestamps
    # Load from the CSVs (like Strategy 17) to prevent ANY historical trades
    # that were already recorded from firing again on script restart.
    for p in [_BUY_LOG_PATH, _SELL_LOG_PATH]:
        if p.exists():
            try:
                df = pd.read_csv(p, dtype=str)
                # In S18.1 the column name is 'Event ID' or sometimes 'Trade Entry Id'
                if "Event ID" in df.columns:
                    _fired_events.update(df["Event ID"].dropna().tolist())
                elif "Trade Entry Id" in df.columns:
                    _fired_events.update(df["Trade Entry Id"].dropna().tolist())
                if "Entry Datetime" in df.columns:
                    for idx, r in df.iterrows():
                        if pd.notna(r["Entry Datetime"]):
                            side_str = "buy" if "BUY" in p.name else "sell"
                            entry_dt = r["Entry Datetime"]
                            _fired_entry_timestamps.add(f"{side_str}|{entry_dt}")
            except:
                pass
                
    # Also load from the JSON file
    if (BASE_LOG_DIR / "_fired_events_4H-15min_XAUUSD.json").exists():
        try:
            with open((BASE_LOG_DIR / "_fired_events_4H-15min_XAUUSD.json"), "r") as f:
                json_events = set(json.load(f))
                _fired_events.update(json_events)
        except:
            pass

def save_fired_events():
    try:
        with open(BASE_LOG_DIR / "_fired_events_4H-15min_XAUUSD.json", "w") as f:
            json.dump(list(_fired_events), f)
    except Exception: pass

def mt5_bridge_trade(symbol: str, action_type: int, volume: float, sl: float = 0.0):
    try:
        payload = {"action": 1, "symbol": symbol, "volume": float(volume),
                   "type": action_type, "price": 0.0, "sl": float(sl),
                   "magic": MAGIC, "comment": "S18.1 XAUUSD Bridge"}
        r = requests.post(f"{MT5_BRIDGE_URL}/trade", json=payload,
                          headers={"X-Api-Key": MT5_API_KEY}, timeout=20)
        j = r.json()
        return (int(j.get("order_id", 0)), int(j.get("result", 0)), j.get("comment", ""))
    except Exception as e:
        return (0, 0, str(e))

def mt5_bridge_modify_sl(ticket: int, new_sl: float) -> bool:
    try:
        r = requests.post(f"{MT5_BRIDGE_URL}/modify",
                          json={"ticket": int(ticket), "sl": float(new_sl)},
                          headers={"X-Api-Key": MT5_API_KEY}, timeout=20)
        return r.status_code == 200 and int(r.json().get("result", 0)) == 10009
    except Exception: return False

def mt5_bridge_close_ticket(ticket: int) -> bool:
    try:
        r = requests.post(f"{MT5_BRIDGE_URL}/close",
                          json={"ticket": int(ticket)},
                          headers={"X-Api-Key": MT5_API_KEY}, timeout=20)
        return r.status_code == 200 and int(r.json().get("result", 0)) == 10009
    except Exception: return False

# =============================================================================
# MARKET-CLOSE FLATTEN
# =============================================================================

_MC_SESSIONS = {
    "XAUUSD": {"daily_break": True, "weekend": True},
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

def flatten_for_market_close(positions: list, inst_symbol: str):
    due, why = market_close_flatten_due(inst_symbol)
    if not due:
        return set()
    closed = set()
    for p in positions:
        try:
            ticket = int(p.get("ticket", 0) or 0)
            magic  = int(p.get("magic", 0) or 0)
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

# =============================================================================
# TRAILING SL
# =============================================================================

def run_trailing_pass():
    if not _ticket_map: return
    try:
        r_pos = requests.get(f"{MT5_BRIDGE_URL}/positions",
                             headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
        if r_pos.status_code != 200: return
        pos_list = r_pos.json()
        pos_list = pos_list.get("positions", pos_list) if isinstance(pos_list, dict) else pos_list
        pos_list = pos_list if isinstance(pos_list, list) else []
    except Exception as e:
        log(f"[TRAIL] fetch positions error: {e}"); return

    flattened = flatten_for_market_close(pos_list, SYMBOL)

    open_tickets = {int(p.get("ticket", 0)) for p in pos_list}
    for t, rec in list(_ticket_map.items()):
        if t not in open_tickets and not rec.get("closed_notified"):
            tg_post(
                f"🔒 POSITION CLOSED\n"
                f"Ticket: {t}\n"
                f"Entry: {rec.get('entry_price')}\n"
                f"Last SL: {rec.get('current_sl')}"
            )
            rec["exit_datetime"] = datetime.now(timezone.utc).isoformat()
            rec["closed_notified"] = True
            trade_db.record_close(t, reason="not_in_positions")

    for p in pos_list:
        try:
            ticket = int(p.get("ticket", 0))
        except Exception:
            continue
        if ticket in flattened: continue
        if ticket not in _ticket_map: continue

        rec        = _ticket_map[ticket]
        targets    = rec.get("targets", {})
        cur_sl     = float(p.get("sl", 0) or 0)
        trade_side = rec.get("side", "buy")
        current_px = float(p.get("price_current", 0) or 0)
        if current_px <= 0: continue

        for cp in TRAIL_CPS:
            if cp in rec.get("trail_hit", set()): continue
            tgt = targets.get(cp)
            if tgt is None: continue
            hit = (current_px >= tgt) if trade_side == "buy" else (current_px <= tgt)
            if not hit: continue

            rec.setdefault("trail_hit", set()).add(cp)
            if cp == 2:
                anchor = rec["entry_price"]
            else:
                anchor = targets.get(cp - 2, rec["entry_price"])
            if anchor is None: continue

            if trade_side == "buy":
                if cur_sl > 0 and anchor <= cur_sl: continue
            else:
                if cur_sl > 0 and anchor >= cur_sl: continue

            new_sl = round(float(anchor), PRICE_DIGITS)
            ok = mt5_bridge_modify_sl(ticket, new_sl)
            trade_db.record_trail(ticket, new_sl, cp, executed=bool(ok))
            if not ok:
                rec["trail_hit"].discard(cp)
                continue
            cur_sl = new_sl
            rec["current_sl"] = new_sl
            tg_post(
                f"📐 SL TRAILED\n"
                f"Ticket: {ticket}\n"
                f"New SL: {new_sl}\n"
                f"Anchor: 1:{cp} target hit at {tgt}\n"
                f"Current price: {current_px}"
            )
            log(f"[TRAIL] Ticket {ticket}: SL trailed to {new_sl} (1:{cp} hit at {tgt})")

def _trailing_loop():
    while True:
        try:
            run_trailing_pass()
        except Exception as e:
            log(f"[TRAIL-LOOP] error: {e}")
        _time.sleep(TRAIL_INTERVAL_SEC)

# =============================================================================
# STRATEGY 18.1 CORE ENGINE (XAUUSD)
# =============================================================================

def run_strategy18_1(arr1d, arr1h, arr5m, side: str, recent_ltf: pd.DatetimeIndex) -> List[OrderedDict]:
    zones     = find_setup_zones_1d(arr1d, side)
    cycles_1d = build_raw_cycle_map(arr1d)
    rows: List[OrderedDict] = []

    idx1d = arr1d["idx"]
    sess  = arr1d.get("session_date")

    # The 1H window reaches back much further than the 5min window (see the
    # LOOKBACK_* comments).  A setup whose entry search would begin before the
    # 5min data starts CANNOT be evaluated: find_entry_multiday would clamp
    # both start_pos and ze_pos to 0, scan a single candle, find no entry and
    # report "Invalidated ... zone Endtime occured" — a false negative.  Such
    # setups are skipped outright rather than logged with a wrong verdict.
    ltf_start_ts   = arr1h["idx"][0] if len(arr1h["idx"]) else None
    skipped_setups = 0

    for zone in reversed(zones):
        zs_pos  = zone["start_pos"]
        ze_pos  = zone["end_pos"]
        zs_ts   = zone["start_ts"]
        ze_ts   = zone["end_ts"]
        is_open = zone.get("open", False)

        zone_macd_start = zone_macd_end = None
        for cyc in cycles_1d:
            if cyc["start_pos"] <= zs_pos <= cyc["end_pos"]:
                zone_macd_start = _s(cyc["start_ts"])
                zone_macd_end   = _s(cyc["end_ts"])
                break

        zone_end_macd_start = zone_end_macd_end = None
        if not is_open:
            for cyc in cycles_1d:
                if cyc["start_pos"] <= ze_pos <= cyc["end_pos"]:
                    zone_end_macd_start = _s(cyc["start_ts"])
                    zone_end_macd_end   = _s(cyc["end_ts"])
                    break
        else:
            zone_end_macd_start = zone_end_macd_end = "N/A"

        setups = find_setups_in_zone(arr1d, zs_pos, ze_pos, side)
        if not setups: continue

        for setup in reversed(setups):
            gc_ts   = pd.Timestamp(setup["final_gc_ts"])
            gc_open = float(setup["final_gc_open"])

            # Entry search starts one 1H bar after the setup candle.  If that
            # instant predates the 5min window, the setup is out of scope.
            if ltf_start_ts is not None and map_htf_to_mtf_start(gc_ts) < ltf_start_ts:
                skipped_setups += 1
                continue

            row = create_base_record(side)
            row["Setup Start time"] = _s(zs_ts)
            row["Setup Endtime"]    = _s(ze_ts) if not is_open else "N/A"

            ev_id = hashlib.sha256(f"{side}|{zs_ts}|{setup['final_gc_ts']}".encode()).hexdigest()[:24]
            row["Event ID"]      = ev_id
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            gc_pos_1d = int(idx1d.searchsorted(gc_ts, side="left"))
            if sess is not None and 0 <= gc_pos_1d < len(sess):
                row["Session date"] = str(sess[gc_pos_1d])

            if side == "sell":
                row["Candle touching upper BB DT"] = _s(zs_ts)
            else:
                row["Candle touching Lower BB DT"] = _s(zs_ts)

            if side == "sell":
                row["Final Green Candle Datetime"]                      = setup.get("anchor_ts")
                row["Final Red Candle Closing Below GC Open Datetime"]  = setup.get("final_rc_ts")
                row["Final Red candle closes below middle BB"]          = "Yes"
                row["1st Green Candle Found after Setup RC Datetime"]   = _s(gc_ts)
                row["1st Green Candle Found after Setup RC Open"]       = _f(gc_open)
            else:
                row["Final Red Candle Datetime"]                        = setup.get("anchor_ts")
                row["Final Green Candle Closing Above RC Open Datetime"] = setup.get("final_rc_ts")
                row["Final Green candle closes above middle BB"]        = "Yes"
                row["1st Red Candle Found after Setup GC Datetime"]     = _s(gc_ts)
                row["1st Red Candle Found after Setup GC Open"]         = _f(gc_open)

            next_mtf_ts        = map_htf_to_mtf_start(gc_ts)
            start_5m_pos       = int(arr1h["idx"].searchsorted(next_mtf_ts, side="left"))
            actual_start_5m_ts = _s(arr1h["idx"][start_5m_pos]) if start_5m_pos < len(arr1h["idx"]) else None
            row[f"{HTF_LABEL} candle found mapped on {MTF_LABEL} datetime"] = actual_start_5m_ts

            if setup.get("bb_touch_found"):
                if side == "sell":
                    row["Status"] = "Invalidated due to candle touching lower BB found in the bearish zone"
                else:
                    row["Status"] = "Invalidated due to candle touching upper BB found in the bullish zone"
                row["Strategy Additional info"] = build_strategy_additional_info(
                    zone, setup, [], None, side,
                    zone_macd_start, zone_macd_end,
                    zone_end_macd_start, zone_end_macd_end,
                    {}, {}, actual_start_5m_ts, row["Status"]
                )
                rows.append(row)
                continue

            # If the zone is open (live market), allow the LTF scan to reach the absolute live edge.


            # Do NOT bound the entry search by the slow HTF candle.


            search_end_ts = ze_ts


            if is_open and len(arr1h["idx"]) > 0:


                search_end_ts = arr1h["idx"][-1]



            try:
                day_records, entry_found, entry_ts, entry_price, entry_day = find_entry_multiday(
                    arr1h, arr1d, gc_ts, gc_open, search_end_ts, side
                )
            except Exception as exc:
                log(f"  [WARN] Entry error at {_s(gc_ts)}: {exc}")
                row["Status"] = f"Error in entry search: {exc}"
                rows.append(row)
                continue

            if day_records:
                d1 = day_records[0]
                if side == "sell":
                    row[f"1st Candle touching Upper BB on {MTF_LABEL} Datetime"] = d1.get("bb_ts")
                    row["Lowest Value at Upper BB touched candle"]                = d1.get("bb_ext_val")
                    row["Lowest Value at Upper BB touched"]                       = d1.get("bb_ext_name")
                else:
                    row[f"1st Candle touching Lower BB on {MTF_LABEL} Datetime"] = d1.get("bb_ts")
                    row["Highest Value at Lower BB touched candle"]               = d1.get("bb_ext_val")
                    row["Highest Value at Lower BB touched"]                      = d1.get("bb_ext_name")

            if not entry_found:
                if is_open:
                    row["Status"] = "Finding Entry"
                else:
                    row["Status"] = "Invalidated on 1hr due setup finding Setup finding zone Endtime occured"
                row["Strategy Additional info"] = build_strategy_additional_info(
                    zone, setup, day_records, None, side,
                    zone_macd_start, zone_macd_end,
                    zone_end_macd_start, zone_end_macd_end,
                    {}, {}, actual_start_5m_ts, row["Status"]
                )
                rows.append(row)
                continue

            try:
                entry_ts_pd = pd.Timestamp(entry_ts)
                if entry_ts_pd.tzinfo is None:
                    entry_ts_pd = entry_ts_pd.tz_localize("UTC")
                else:
                    entry_ts_pd = entry_ts_pd.tz_convert("UTC")
            except Exception:
                entry_ts_pd = pd.Timestamp(entry_ts)
            entry_pos_5m = int(arr1h["idx"].searchsorted(entry_ts_pd, side="left"))

            dr_entry = next((d for d in day_records if d.get("entry_found")), None) or (day_records[-1] if day_records else {})

            if dr_entry.get("session_dt"):
                row["Session date"] = str(dr_entry.get("session_dt"))

            row[f"Entry Found according to which {HTF_LABEL} level"]                              = f"{_ordinal(entry_day)} day"
            row[f"Final {HTF_LABEL} Datetime on {HTF_LABEL} TF"]                                  = dr_entry.get("day_1d_ts")
            row[f"Final {HTF_LABEL} {MTF_LABEL} Candle touching BB DT"]                           = dr_entry.get("bb_ts")
            row[f"Final {HTF_LABEL} Datetime on {HTF_LABEL} TF Candle Colour"]                    = dr_entry.get("day_1d_color")
            row[f"Final {HTF_LABEL} - {HTF_LABEL} TF Level"]                                      = _f(dr_entry.get("day_level"))
            row[f"Final {HTF_LABEL} Datetime on {HTF_LABEL} TF level"]                            = _f(dr_entry.get("day_level"))
            row[f"Final {HTF_LABEL} {MTF_LABEL} Candle touching Upper BB DT"]                     = dr_entry.get("bb_ts")
            row[f"Final {HTF_LABEL} {MTF_LABEL} Candle touching upper BB Lowest Level Considered from"] = dr_entry.get("bb_ext_name")
            if side == "sell":
                row[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing below {HTF_LABEL} TF level DT"] = dr_entry.get("level_a_closed_ts")
                row[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing below Lowest Level DT"]          = dr_entry.get("level_b_closed_ts")
            else:
                row[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing above {HTF_LABEL} TF level DT"] = dr_entry.get("level_a_closed_ts")
                row[f"Final {HTF_LABEL} {MTF_LABEL} Candle closing above Highest Level DT"]         = dr_entry.get("level_b_closed_ts")

            sl_info = compute_hard_sl_1h(arr1h, entry_pos_5m, side)
            hard_sl = float(sl_info["hard_sl"])

            invalid_sl = (
                (side == "sell" and entry_price >= hard_sl) or
                (side == "buy"  and entry_price <= hard_sl)
            )
            if invalid_sl:
                row["Status"]                   = "Invalidated - entry price wrong vs SL"
                row["Entry Price"]              = _f(entry_price)
                row["Entry Datetime"]           = _s(entry_ts)
                row["Hard SL Price"]            = _f(hard_sl)
                row["SL Price considered from"] = sl_info.get("hard_sl_from")
                row["Strategy Additional info"] = build_strategy_additional_info(
                    zone, setup, day_records, entry_day, side,
                    zone_macd_start, zone_macd_end,
                    zone_end_macd_start, zone_end_macd_end,
                    sl_info, {}, actual_start_5m_ts, row["Status"]
                )
                rows.append(row); continue

            sl_dist_pct = (abs(entry_price - hard_sl) / entry_price * 100.0) if entry_price > 0 else 0.0
            if MAX_SL_DISTANCE_PCT > 0 and sl_dist_pct > MAX_SL_DISTANCE_PCT:
                row["Status"]                   = (f"Invalidated - hard SL distance out of range "
                                                   f"({sl_dist_pct:.2f}% > {MAX_SL_DISTANCE_PCT}%)")
                row["Entry Price"]              = _f(entry_price)
                row["Entry Datetime"]           = _s(entry_ts)
                row["Hard SL Price"]            = _f(hard_sl)
                row["Assign Hard SL Percentage"] = _f(sl_dist_pct)
                row["SL Price considered from"] = sl_info.get("hard_sl_from")
                log(f"  [GUARD] {side.upper()} {_s(entry_ts)} rejected: SL {hard_sl} is "
                    f"{sl_dist_pct:.2f}% from entry {entry_price}")
                row["Strategy Additional info"] = build_strategy_additional_info(
                    zone, setup, day_records, entry_day, side,
                    zone_macd_start, zone_macd_end,
                    zone_end_macd_start, zone_end_macd_end,
                    sl_info, {}, actual_start_5m_ts, row["Status"]
                )
                rows.append(row); continue

            pts    = abs(entry_price - hard_sl)
            sl_pct = (pts / entry_price * 100.0) if entry_price > 0 else 0.0
            qty    = RISK_PER_TRADE / pts if pts > 0 else 0.0

            lots = max(round(qty / CONTRACT_SIZE, 2), 0.01)

            row["Status"]                     = "Intrade"
            row["Entry Price"]                = _f(entry_price)
            row["Entry Datetime"]             = _s(entry_ts)
            row["Hard SL Price"]              = _f(hard_sl)
            row["Assign Hard SL Percentage"]  = _f(sl_pct)
            row["SL Price considered from"]   = sl_info.get("hard_sl_from")
            row["Qty"]                        = _f(qty)
            row["Trading qty Contract"]       = lots
            row["Investment Value for Ratios"] = _f(qty * entry_price)

            # Calculate when the entry candle actually closed
            entry_candle_close = entry_ts_pd + pd.Timedelta(minutes=15)
            
            # STRICT ENFORCEMENT: The entry must occur within the recent window AND 
            # the candle must have closed AFTER the script was started.
            is_new_entry = (entry_ts_pd in recent_ltf) and (entry_candle_close >= SCRIPT_START_TIME)
            log(f"[ENTRY-CHECK] ev_id={ev_id} entry_ts_pd={entry_ts_pd} is_new_entry={is_new_entry} already_fired={ev_id in _fired_events}")
            order_id     = 0
            if is_new_entry and ev_id not in _fired_events:
                _fired_events.add(ev_id)
                save_fired_events()

                action_type = 0 if side == "buy" else 1
                log(f"[ENTRY-FIRE] Placing {side.upper()} trade: lots={lots} hard_sl={hard_sl} ev_id={ev_id}")
                trade_db.record_signal(ev_id, SYMBOL, side,
                                       signal_price=entry_price,
                                       qty=lots, hard_sl=hard_sl)
                order_id, retcode, t_comment = mt5_bridge_trade(SYMBOL, action_type, lots, hard_sl)
                log(f"[ENTRY-RESULT] order_id={order_id} retcode={retcode} comment={t_comment}")

                final_entry_price = entry_price
                new_hard_sl       = hard_sl

                if order_id > 0 and retcode == 10009:
                    actual_entry = None
                    matched_pos  = None
                    for _pe_try in range(3):
                        _time.sleep(1.0)
                        try:
                            r_pos = requests.get(f"{MT5_BRIDGE_URL}/positions",
                                                 headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
                            if r_pos.status_code == 200:
                                pos_list = r_pos.json()
                                pos_list = pos_list.get("positions", pos_list) if isinstance(pos_list, dict) else pos_list
                                pos_list = pos_list if isinstance(pos_list, list) else []
                                matched_pos = next((p for p in pos_list if int(p.get("ticket", 0)) == order_id), None)
                        except Exception as _pe_fe:
                            log(f"[POST-ENTRY] positions fetch failed (try {_pe_try + 1}/3): {_pe_fe}")
                        if matched_pos:
                            break

                    if matched_pos:
                        actual_entry = float(matched_pos.get("price_open"))
                    else:
                        log(f"[POST-ENTRY] ticket {order_id} not found after 3 tries — "
                            f"falling back to the signal entry price for the SL correction")

                    _entry_for_sl = actual_entry if actual_entry is not None else float(entry_price or 0)
                    if _entry_for_sl > 0 and lots > 0:
                        price_diff = round(RISK_PER_TRADE / (CONTRACT_SIZE * lots), PRICE_DIGITS)
                        exact_sl   = round(_entry_for_sl + price_diff, PRICE_DIGITS) if side == "sell" \
                                     else round(_entry_for_sl - price_diff, PRICE_DIGITS)

                        _sl_ok = False
                        for _mod_try in range(3):
                            if mt5_bridge_modify_sl(order_id, exact_sl):
                                _sl_ok = True
                                break
                            _time.sleep(1.0)

                        if _sl_ok:
                            new_hard_sl = exact_sl
                            _px_kind = "fill" if actual_entry is not None else "signal"
                            log(f"[POST-ENTRY] corrected SL for ticket {order_id} to {exact_sl} "
                                f"based on {_px_kind} price {_entry_for_sl}")
                            tg_post(
                                f"✅ POSITION OPENED (LIVE MT5)\n"
                                f"Ticket: {order_id}\n"
                                f"Live Entry: {_entry_for_sl}\n"
                                f"Live Fail-Safe SL: {exact_sl}\n"
                                f"Lots: {lots}"
                            )
                        else:
                            log(f"[POST-ENTRY] SL correction FAILED for ticket {order_id} — "
                                f"position keeps placed SL {hard_sl}")
                            tg_post(f"⚠️ POST-ENTRY SL CORRECTION FAILED\nTicket: {order_id}\n"
                                    f"Intended SL: {exact_sl}\nSL on broker: {hard_sl}\n"
                                    f"Check the position manually.")

                    final_entry_price = _entry_for_sl if _entry_for_sl > 0 else entry_price

                    live_targets = {r: _tgt(final_entry_price, new_hard_sl, side, r) for r in ALL_RATIOS}
                    _ticket_map[order_id] = {
                        "symbol": SYMBOL, "event_id": ev_id, "side": side,
                        "entry_price": final_entry_price,
                        "actual_entry_price": final_entry_price,
                        "hard_sl": new_hard_sl,
                        "targets": live_targets, "current_sl": new_hard_sl, "trail_hit": set()
                    }
                    _event_to_ticket[ev_id] = order_id
                    trade_db.record_execution(ev_id, order_id, retcode,
                                              entry_price=final_entry_price,
                                              qty=lots, hard_sl=new_hard_sl,
                                              targets=live_targets)


                if order_id > 0:
                    row["Live MT5 Order ID"] = order_id
                msg = format_telegram(row, side, order_id)
                log(f"[TG-SEND] Sending Telegram for ev_id={ev_id} order_id={order_id}")
                tg_post(msg)
                log(f"[TG-SENT] Telegram dispatched for ev_id={ev_id}")
                trade_db.mark_telegram_sent(ev_id)

            final_entry_price = entry_price
            final_hard_sl     = hard_sl
            if ev_id in _event_to_ticket:
                ticket = _event_to_ticket[ev_id]
                rec    = _ticket_map.get(ticket)
                if rec:
                    row["Live MT5 Order ID"]      = ticket
                    row["Live Entry Price"]       = rec.get("entry_price", "")
                    row["Live Corrected Hard SL"] = rec.get("hard_sl", "")
                    row["Executed Qty"]           = lots
                    row["Executed Investment Value"] = _f(lots * CONTRACT_SIZE * float(rec.get("entry_price") or entry_price))
                    final_entry_price = rec["entry_price"]
                    final_hard_sl     = rec["hard_sl"]

            try:
                ratio_res, bt_text, qty_bt, invest_bt, hsl_ts, hsl_extreme = run_backtest_5m(
                    arr5m, entry_ts_pd, final_entry_price, final_hard_sl, side
                )
            except Exception as exc:
                log(f"  [WARN] Backtest error at {_s(entry_ts)}: {exc}")
                row["Status"] = f"Backtest error: {exc}"
                rows.append(row); continue

            hsl_tracking = {
                "track_start_ts":     _s(map_mtf_to_ltf_start(entry_ts_pd)),
                "hsl_candle_ts":      hsl_ts,
                "hsl_candle_extreme": hsl_extreme,
            }

            b05 = ratio_res[0.5]
            row["1:0.5 Exit Datetime"]      = b05.get("Exit Datetime")
            row["1:0.5 Exit Price"]         = b05.get("Exit Price")
            row["1:0.5 SL hit Due to"]      = b05.get("SL hit Due to")
            row["1:0.5 Holding Time (hrs)"] = b05.get("Holding Time (hrs)")
            row["P/L 1:0.5"]               = b05.get("P/L")

            for rt in RATIOS_FULL:
                rr = ratio_res[rt]
                row[f"1:{rt} Exit Datetime"]      = rr.get("Exit Datetime")
                row[f"1:{rt} Exit Price"]         = rr.get("Exit Price")
                row[f"1:{rt} SL hit Due to"]      = rr.get("SL hit Due to")
                row[f"1:{rt} Holding Time (hrs)"] = rr.get("Holding Time (hrs)")
                row[f"P/L 1:{rt}"]               = rr.get("P/L")
                if rt >= 2:
                    row[f"Status 1:{rt}"] = rr.get("Exit Status")

            row["Strategy Additional info"] = build_strategy_additional_info(
                zone, setup, day_records, entry_day, side,
                zone_macd_start, zone_macd_end,
                zone_end_macd_start, zone_end_macd_end,
                sl_info, hsl_tracking, actual_start_5m_ts, row["Status"]
            )
            row["BackTest Result"] = bt_text
            row["Executed Result"] = ""

            if ev_id in _event_to_ticket:
                rec = _ticket_map.get(_event_to_ticket[ev_id])
                if rec: row["Live Exit Datetime"] = rec.get("exit_datetime", "")

            process_telegram_exits(row, side, recent_ltf, ratio_res)
            rows.append(row)

    pass # [SCOPE] log removed per request

    rows.reverse()
    return rows

# =============================================================================
# LIVE SCAN LOOP
# =============================================================================

_last_csv_hash = {}

def run_live_scan():
    global _last_csv_hash, _fired_events

    log(f"Fetching live candles ({TF_HTF} + {TF_MTF} + {TF_LTF})...")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_htf = pool.submit(get_rolling_mt5_candles, SYMBOL, TF_HTF, FETCH_HTF)
        f_mtf = pool.submit(get_rolling_mt5_candles, SYMBOL, TF_MTF, FETCH_MTF)
        f_ltf = pool.submit(get_rolling_mt5_candles, SYMBOL, TF_LTF, FETCH_LTF)

    df_htf_raw = f_htf.result()
    df_mtf_raw = f_mtf.result()
    df_ltf_raw = f_ltf.result()

    if df_htf_raw.empty or df_mtf_raw.empty or df_ltf_raw.empty:
        log("Data missing, skipping scan.")
        return

    # Drop the last incomplete (still-forming) candle from each timeframe
    df_htf = df_htf_raw.iloc[:-1]
    df_mtf = df_mtf_raw.iloc[:-1]
    df_ltf = df_ltf_raw.iloc[:-1]

    # Add TradingView FX session columns
    df_htf = prepare_live_frame(df_htf)
    df_mtf = prepare_live_frame(df_mtf)
    df_ltf = prepare_live_frame(df_ltf)

    # --- arr1d role: HTF candles with MACD cycles + all indicators ---
    df_htf_prep = add_raw_macd_cycles(prepare_df(df_htf))
    if len(df_htf_prep) > WARMUP_HTF:
        df_htf_prep = df_htf_prep.iloc[WARMUP_HTF:]

    # --- arr1h role: MTF candles with indicators (entry search + hard SL) ---
    df_mtf_prep = prepare_df(df_mtf)
    if len(df_mtf_prep) > WARMUP_MTF:
        df_mtf_prep = df_mtf_prep.iloc[WARMUP_MTF:]

    # --- arr5m role: raw LTF OHLCV (trailing SL) ---
    df_ltf_used = df_ltf
    if len(df_ltf_used) > WARMUP_LTF:
        df_ltf_used = df_ltf_used.iloc[WARMUP_LTF:]

    log_data_summary(df_htf_prep, TF_HTF)
    log_data_summary(df_mtf_prep, TF_MTF)
    log_data_summary(df_ltf_used, TF_LTF)

    dump_fetched_data(df_htf_prep, TF_HTF)
    dump_fetched_data(df_mtf_prep, TF_MTF)
    dump_fetched_data(df_ltf_used, TF_LTF)

    arr1d = make_fast_arrays_full(df_htf_prep)
    arr1h = make_fast_arrays_full(df_mtf_prep)
    arr5m = make_fast_arrays_ohlcv(df_ltf_used)

    if "session_date" not in arr1d:
        log(f"[WARN] {TF_HTF} arrays carry no session_date — day rollover would be wrong. Skipping scan.")
        return

    recent_ltf = df_ltf_used.index[-RECENT_LTF_COUNT:]

    for side, path in [("sell", _SELL_LOG_PATH), ("buy", _BUY_LOG_PATH)]:
        rows = run_strategy18_1(arr1d, arr1h, arr5m, side, recent_ltf)
        if not rows:
            continue

        new_df = pd.DataFrame(rows)
        current_hash = hashlib.md5(new_df.to_csv(index=False).encode()).hexdigest()
        # Always write CSV if a new entry fired this scan (to capture Live MT5 Order ID)
        had_new_entry = any(
            str(r.get("Live MT5 Order ID", "")).strip() not in ("", "nan", "None", "0")
            for r in rows
        )
        if _last_csv_hash.get(side) == current_hash and not had_new_entry:
            continue
        _last_csv_hash[side] = current_hash

        if path.exists():
            try:
                old_df = pd.read_csv(path, dtype=str)
                old_df = old_df[~old_df["Event ID"].isin(new_df["Event ID"].astype(str))]
                merged = pd.concat([old_df, new_df], ignore_index=True)
            except Exception:
                merged = new_df
        else:
            merged = new_df
        merged.to_csv(path, index=False)

def recover_open_trades():
    if not trade_db.enabled(): return
    saved = trade_db.load_open_trades()
    if not saved: return
    for rec in saved:
        t_id = rec.get("ticket")
        if not t_id: continue
        if t_id not in _ticket_map:
            _ticket_map[t_id] = rec
            _event_to_ticket[rec["event_id"]] = t_id
            log(f"Recovered open trade: {t_id}")

def main():
    log("=" * 70)
    log("🚀 Strategy 18.1 (4H-15min) LIVE BRIDGE — XAUUSD (Exness)")
    log("   Timeframes: 4H (setup/day levels) → 15min (entry) → 5min (trailing SL)")
    log("   Session:    TradingView FX session date (New York 17:00 rollover)")
    log(f"   Mode: BUY + SELL |   ***  LIVE MT5 ORDERS  ***")
    log(f"   Risk/trade: {RISK_PER_TRADE} | Magic: {MAGIC} | Contract: {CONTRACT_SIZE} oz/lot")
    log(f"   BROKER_TZ: {BROKER_TZ} | SAVE_FETCHED_DATA: {SAVE_FETCHED_DATA}")
    log(f"   Window: {HTF_LABEL} {LOOKBACK_HTF}+{WARMUP_HTF} warm-up | "
        f"{MTF_LABEL} {LOOKBACK_MTF}+{WARMUP_MTF} warm-up | "
        f"{LTF_LABEL} {LOOKBACK_LTF}+{WARMUP_LTF} warm-up")
    log("=" * 70)

    load_fired_events()
    recover_open_trades()

    tg_post(
        f"🚀 Strategy 18.1 (4H-15min) LIVE — STARTED\n"
        f"Symbol: {SYMBOL}\n"
        f"Mode: BUY + SELL\n"
        f"Timeframes: 4H (setup) → 15min (entry) → 5min (trailing)\n"
        f"Session rollover: New York 17:00\n"
        f"Paper Trading: NO — LIVE MT5 ORDERS\n"
        f"Risk per trade: {RISK_PER_TRADE}"
    )

    threading.Thread(target=_trailing_loop, daemon=True, name="trail-mgmt").start()
    log(f"[TRAIL-LOOP] started (every {TRAIL_INTERVAL_SEC}s)")

    last_closed_candle = None
    try:
        while True:
            try:
                if is_market_closed(SYMBOL):
                    _time.sleep(SCAN_SLEEP_SEC)
                    continue

                current_closed = latest_closed_ltf_ts()
                if current_closed is not None and current_closed != last_closed_candle:
                    now = datetime.now(timezone.utc)
                    log(f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] "
                        f"New closed candle detected ({current_closed}). Scanning...")
                    try:
                        run_live_scan()
                    except Exception as e:
                        import traceback
                        log(f"Error in scan: {e}")
                        traceback.print_exc()
                    last_closed_candle = current_closed
            except Exception as e:
                log(f"Error checking for new candle: {e}")
            _time.sleep(SCAN_SLEEP_SEC)
    except KeyboardInterrupt:
        log("Stopped by user.")

if __name__ == "__main__":
    main()
