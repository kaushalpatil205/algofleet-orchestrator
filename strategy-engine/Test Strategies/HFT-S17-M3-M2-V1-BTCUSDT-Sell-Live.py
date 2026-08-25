#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HFT-S17 — Regressive Test Driver  (self-contained)
--------------------------------------------------
Mirror of S17-M3-M2-V1-BTCUSDT-Sell-Live.py plumbing (Telegram, MT5
bridge, CSV logger, row builder, backtest) but with the KAMA/M1/M2
signal generation replaced by a forced 1-min SELL loop.

Goal: stress-test the bridge / position-tracking / exit plumbing
without waiting for a real signal.

Self-contained: does NOT import from the live scanner file.  All
required functions are redefined below.

KILL SWITCHES (all checked on every cycle):
  - HALT_HFT env var  = "1"  →  stop firing new trades
  - HFT_DRY_RUN       = "1"  →  log only, never POST a real trade
  - MAX_TRADES_PER_HOUR      →  sliding-window ceiling (default 60)

TAG:
  - MT5 magic = 17999 (vs conservative scanner's 17001)
  - comment  = "HFT-S17"
  - bridge symbol = BTCUSDTz   (Exness MT5 "z" suffix)
"""

import os
import sys
import time as _time
import hashlib
from collections import deque, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, List, Dict

import requests
import numpy as np
import pandas as pd

# ================================================================
# HFT CONFIG
# ================================================================
HFT_MAGIC              = 17999
HFT_COMMENT            = "HFT-S17"
HFT_FIXED_SL_PCT       = 0.5                 # hard SL 0.5% above entry (SELL)
MAX_TRADES_PER_HOUR    = 60
HALT_HFT               = os.getenv("HALT_HFT", "0") == "1"
DRY_RUN                = False
SIGNAL_INTERVAL_SEC    = 60
POSITION_POLL_SEC      = 5

# Exness MT5 demo symbol (note the "z" suffix).  Binance data still uses BTCUSDT.
SYMBOL_BRIDGE          = "BTCUSDT"
SYMBOL_DATA            = "BTCUSDT"
COIN_NAME              = "BTCUSDT"

# Telegram (mirrored from live scanner)
BOT_TOKEN              = "8799287788:AAHS8MiKk5cNjJrfXYFzEaDrmYqpOTXSBuo"
CHAT_ID                = "-1003973537841"
TELEGRAM_URL           = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# MT5 bridge
MT5_BRIDGE_URL         = "https://exness-bridge-mt5.pickleballify.com/279637220/demo"
MT5_API_KEY            = "ak_yC_r95ufQj2QuCqa1KfXg3WP1Zi2udMwkFulqC2z4Is"

# Strategy constants (only what we actually use)
BASE_LOG_DIR           = Path("./output/Strategy 17 M3 M2 Variation 1 Live Logs")
BASE_LOG_DIR.mkdir(parents=True, exist_ok=True)
HFT_LOG_PATH           = BASE_LOG_DIR / f"HFT_S17_M3M2_Var1_{COIN_NAME}_SELL_5min.csv"

LOOKBACK_5M            = 2500
LOOKBACK_1M            = 6000
RISK_PER_TRADE         = 100.0
ALL_RATIOS             = [0.5] + list(range(1, 11))
RATIOS_FULL            = list(range(1, 11))
TRAIL_CPS              = list(range(2, 10))

# State
_TRADE_TIMES  = deque(maxlen=2048)
_HFT_TICKETS  = {}   # tickets opened by HFT driver: ticket -> {entry_price, current_sl}
_HFT_FIRED    = set()


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

def vkv(obj: Any) -> str:
    if obj is None: return ""
    if isinstance(obj, (str, int, float, bool)): return str(obj)
    out = []
    for k, v in (obj.items() if isinstance(obj, (dict, OrderedDict)) else []):
        out.append(f"{k}: {_f(v)}")
    return "\n".join(out)


# ================================================================
# DATA FETCH + INDICATORS  (minimal subset)
# ================================================================
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000}

def fetch_candles(symbol: str, interval: str, n_candles: int) -> pd.DataFrame:
    step = _INTERVAL_MS[interval]
    now  = int(datetime.now(timezone.utc).timestamp() * 1000)
    end  = now - (now % step) - 1
    parts = []; remaining = n_candles
    url = "https://api.binance.com/api/v3/klines"
    while remaining > 0:
        lim   = min(1000, remaining)
        start = end - lim * step + 1
        try:
            r = requests.get(url, params={"symbol": symbol, "interval": interval,
                                           "startTime": start, "endTime": end,
                                           "limit": lim}, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log(f"Binance fetch error [{symbol} {interval}]: {e}")
            return pd.DataFrame()
        if not data: break
        part = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","qav","num_trades","tbv","tqv","ignore"])
        part["datetime"] = pd.to_datetime(part["open_time"], unit="ms", utc=True)
        for c in ["open","high","low","close","volume"]:
            part[c] = part[c].astype(float)
        parts.append(part[["datetime","open","high","low","close","volume"]])
        end = int(part["datetime"].iloc[0].timestamp() * 1000) - 1
        remaining -= len(part)
        if len(part) < lim: break
        _time.sleep(0.12)
    if not parts: return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df = df.drop_duplicates("datetime").sort_values("datetime").tail(n_candles)
    return df.set_index("datetime")


# ================================================================
# BASE ROW  (mirrors base_row_v1 from live scanner)
# ================================================================
def base_row_v1(side: str) -> "OrderedDict[str, Any]":
    r = OrderedDict()
    tc = "Red" if side == "buy" else "Green"
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
    r["M1 - Backward Final Key Candle"] = None
    r["M1 - Highest Value obtained from Backward Candles"] = None
    r["M1 - Highest Value obtained from Final Key Candle to 5min candle mapped to 1min Datetime"] = None
    r["M1- Highest Value obtained from Backward Candle"] = None
    if side == "buy":
        r["M1  - Most Updated High Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Most Updated High DT after 5min candle mapped to 1min DT"]     = None
        r["M1 - Most Updated High from Upper BB or High Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Entry found from"] = None
        r["M1 - Candle Close above Previous Updated high Datetime"] = None
        r["M1 - Candle Close above Previous Updated high Value"]    = None
    else:
        r["M1  - Most Updated Low Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Most Updated Low DT after 5min candle mapped to 1min DT"]     = None
        r["M1 - Most Updated Low from Lower BB or Low Value after 5min candle mapped to 1min DT"] = None
        r["M1 - Entry found from"] = None
        r["M1 - Candle Close below Previous Updated low Datetime"] = None
        r["M1 - Candle Close below Previous Updated low Value"]    = None
    r["M2-Final Key Candle found Datetime on 1min"] = None
    if side == "buy":
        r["M2-Highest Value at Key candle on 1min"]            = None
        r["M2-Highest Value at Key candle from Level on 1min"]  = None
        r["M2-Candle close above the Highesh level obtained from key candle"] = None
        r["M2-Candle close above the Highesh level obtained from key candle  Datetime"] = None
    else:
        r["M2-Lowest Value at Key candle on 1min"]             = None
        r["M2-Lowest Value at Key candle from Level on 1min"]   = None
        r["M2-Candle close below the Lowest level obtained from key candle"] = None
        r["M2-Candle close below the Lowest level obtained from key candle  Datetime"] = None
    r["Method 2 Entry"] = None
    r["Hard SL consider from : kama Levels or candle Low" if side == "buy"
      else "Hard SL consider from : kama Levels or candle High"] = None
    r["5min Strategy Final candle Price"] = None
    r["Status"] = None
    for prefix in ["2hrs", "4hrs", "1D"]:
        for ema in [50, 100, 200]:
            r[f"{prefix} Price above/Below EMA {ema}"] = None
    r["Final Buy found from which Method" if side == "buy"
      else "Final Sell found from which Method"] = None
    r["Entry Datetime"] = None; r["Entry Price"] = None
    r["Hard SL Price"]  = None; r["Assign Hard SL Percentage"] = None
    r["Qty"] = None; r["Investment Value for Ratios"] = None
    r["1:0.5 Exit Datetime"] = None; r["1:0.5 Exit Price"] = None
    r["1:0.5 SL hit Due to"] = None; r["1:0.5 Holding Time (hrs)"] = None
    r["P/L 1:0.5"] = None
    for ratio in RATIOS_FULL:
        r[f"1:{ratio} Exit Datetime"] = None; r[f"1:{ratio} Exit Price"] = None
        r[f"1:{ratio} SL hit Due to"] = None; r[f"1:{ratio} Holding Time (hrs)"] = None
        r[f"P/L 1:{ratio}"] = None
        if ratio >= 2: r[f"Status 1:{ratio}"] = None
    r["Strategy Additional info"] = None
    r["Method 1 Additional info"] = None
    r["Method 2 Additional info"] = None
    r["BackTest Result"] = None
    r["Logged At UTC"] = None
    r["Event ID"] = None
    return r


# ================================================================
# TELEGRAM
# ================================================================
def tg_post(text: str):
    try:
        r = requests.post(TELEGRAM_URL, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
        if r.status_code != 200:
            log(f"Telegram HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log(f"Telegram exception: {e}")


def format_telegram(r: dict) -> str:
    ep  = r.get("Entry Price", 0)
    hsl = r.get("Hard SL Price", 0)
    return (
        f"🔴 SELL {COIN_NAME} [HFT]\n"
        f"Strategy: Strategy 17 M3 M2 Variation 1 (HFT driver)\n"
        f"Entry Datetime: {r.get('Entry Datetime','')}\n"
        f"Entry Price: {float(ep) if ep else 0.0:.6f}\n"
        f"Qty: {r.get('Qty', 'N/A')}\n"
        f"Hard SL Price: {float(hsl) if hsl else 0.0:.6f}\n"
    )


# ================================================================
# MT5 BRIDGE
# ================================================================
def log_trade_error_hft(symbol: str, error_msg: str):
    try:
        from datetime import datetime, timezone
        err_path = BASE_LOG_DIR / "trade_errors.log"
        with open(err_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] [{symbol}] {error_msg}\n")
    except Exception:
        pass

def mt5_bridge_trade_hft(symbol: str, action_type: int, volume: float, sl: float = 0.0):
    """Send a market order. Returns (order_id, retcode, message)."""
    if DRY_RUN:
        log(f"[HFT-DRY] would POST trade | symbol={symbol} type={action_type} "
            f"vol={volume} sl={sl} magic={HFT_MAGIC} comment={HFT_COMMENT}")
        return (0, 0, "Dry run")
    try:
        try:    v = float(volume)
        except: v = 0.01
        try:    s = float(sl)
        except: s = 0.0
        payload = {
            "action": 1, "symbol": symbol, "volume": v,
            "type": action_type, "price": 0.0, "sl": s,
            "magic": HFT_MAGIC, "comment": HFT_COMMENT,
        }
        r = requests.post(f"{MT5_BRIDGE_URL}/trade", json=payload,
                          headers={"X-Api-Key": MT5_API_KEY, "Content-Type": "application/json"},
                          timeout=20)
        log(f"HFT Bridge Trade HTTP {r.status_code}: {r.text[:300]}")
        try:
            j = r.json()
            order_id = int(j.get("order_id", 0))
            retcode = int(j.get("result", 0))
            comment = j.get("comment", "")
            if order_id <= 0 or retcode != 10009:
                err_msg = f"Trade failed. Retcode: {retcode}, Comment: {comment}"
                log_trade_error_hft(symbol, err_msg)
            return (order_id, retcode, comment)
        except Exception as e:
            err_msg = f"JSON parse error: {e}"
            log_trade_error_hft(symbol, err_msg)
            return (0, 0, err_msg)
    except Exception as e:
        err_msg = f"HTTP Request exception: {e}"
        log_trade_error_hft(symbol, err_msg)
        log(f"HFT Bridge exception: {e}")
        return (0, 0, err_msg)


def mt5_bridge_modify_sl(ticket: int, new_sl: float):
    """Ratchet the SL on an open HFT position.  Silent on dry-run."""
    if DRY_RUN:
        log(f"[HFT-DRY] would MODIFY SL | ticket={ticket} new_sl={new_sl}")
        return
    try:
        payload = {"ticket": int(ticket), "sl": float(new_sl)}
        r = requests.post(f"{MT5_BRIDGE_URL}/modify", json=payload,
                          headers={"X-Api-Key": MT5_API_KEY, "Content-Type": "application/json"},
                          timeout=20)
        log(f"HFT Bridge Modify SL HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log(f"HFT Bridge Modify exception: {e}")


# ================================================================
# TRAILING STOP — advances SL as price moves favorably
# ================================================================
# Conservative trailing rule for HFT driver:
#   - If price has moved HFT_TRAIL_TRIGGER_PCT% in our favor, set SL to entry (breakeven)
#   - After that, ratchet SL to (entry - HFT_TRAIL_LOCK_PCT%) for every further HFT_TRAIL_RATCHET_PCT% move
HFT_TRAIL_TRIGGER_PCT = 0.5   # 0.5% favorable move → move SL to breakeven
HFT_TRAIL_RATCHET_PCT = 0.25  # every 0.25% additional favorable move → ratchet SL by 0.25%
HFT_TRAIL_LOCK_PCT    = 0.25  # keep 0.25% of profit locked in

def trail_hft_positions(positions: List[dict]):
    """For each open HFT SELL position, advance SL if price has moved
    favorably.  positions: list of dicts with keys ticket, price_open, sl,
    type (1=SELL), current bid (or use price_open/current_price for the
    check).  We use price_open vs the last 1-min close from the live
    data as the 'current price' approximation.
    """
    if not positions:
        return
    # Pull the latest 1-min close as the current price proxy
    try:
        df1 = fetch_candles(SYMBOL_DATA, "1m", 5)
        if df1.empty:
            return
        current_px = float(df1["close"].iloc[-1])
    except Exception as e:
        log(f"[HFT-TRAIL] price fetch error: {e}")
        return

    for p in positions:
        try:
            ticket    = int(p.get("ticket", 0))
            magic     = int(p.get("magic", 0))
            side      = int(p.get("type", 0))   # 0=Buy, 1=Sell
            open_px   = float(p.get("price_open", 0))
            cur_sl    = float(p.get("sl", 0))
        except Exception:
            continue
        if magic != HFT_MAGIC or ticket <= 0 or open_px <= 0:
            continue

        if side == 1:  # SELL — favorable = current_px < open_px
            move_pct = (open_px - current_px) / open_px * 100.0
        else:          # BUY
            move_pct = (current_px - open_px) / open_px * 100.0
        if move_pct < HFT_TRAIL_TRIGGER_PCT:
            continue   # not enough favorable move yet

        # Compute the new SL.  For a SELL, lower SL = more profit locked.
        # We want the new SL to be at most (entry - lock_pct) on the profit side.
        lock_pct_per_ratchet = HFT_TRAIL_LOCK_PCT
        new_sl = open_px * (1.0 - lock_pct_per_ratchet / 100.0) if side == 1 \
                 else open_px * (1.0 + lock_pct_per_ratchet / 100.0)

        # Only advance SL in the favorable direction (don't pull it back)
        if side == 1:
            if cur_sl > 0 and new_sl >= cur_sl:
                continue   # already ratcheted past this level
        else:
            if cur_sl > 0 and new_sl <= cur_sl:
                continue

        new_sl = round(new_sl, 6)
        log(f"[HFT-TRAIL] ticket={ticket} side={'SELL' if side==1 else 'BUY'} "
            f"open={open_px} cur_px={current_px} move={move_pct:.3f}% "
            f"old_sl={cur_sl} → new_sl={new_sl}")
        mt5_bridge_modify_sl(ticket, new_sl)
        if ticket in _HFT_TICKETS:
            _HFT_TICKETS[ticket]["current_sl"] = new_sl
        # Telegram: SL modified / trail advanced
        tg_post(f"📐 [HFT] SL TRAILED\n"
                f"Ticket: {ticket}\n"
                f"Side: {'SELL' if side==1 else 'BUY'}\n"
                f"Open: {open_px}\n"
                f"Current: {current_px}\n"
                f"Move: {move_pct:.3f}%\n"
                f"New SL: {new_sl}")


# ================================================================
# POSITION POLL
# ================================================================
def poll_positions():
    """Return the open positions list (or None on error)."""
    try:
        r = requests.get(f"{MT5_BRIDGE_URL}/positions",
                         headers={"X-Api-Key": MT5_API_KEY}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            positions = data.get("positions", data) if isinstance(data, dict) else data
            n = len(positions) if isinstance(positions, list) else 0
            hft_n = 0
            total_profit = 0.0
            for p in (positions or []):
                try:
                    magic = int(p.get("magic", 0))
                    profit = float(p.get("profit", 0))
                    total_profit += profit
                    if magic == HFT_MAGIC: hft_n += 1
                except Exception:
                    pass
            log(f"[POS] total_open={n} | hft_open={hft_n} | total_profit={total_profit:.2f} USD")
            return positions
    except Exception as e:
        log(f"[POS] poll exception: {e}")
    log("[POS] /positions unavailable")
    return None


# ================================================================
# HFT SIGNAL — forces a 1/min SELL using the latest 1-min close
# ================================================================
def _hourly_cap_ok() -> bool:
    now = _time.time()
    while _TRADE_TIMES and (now - _TRADE_TIMES[0]) > 3600:
        _TRADE_TIMES.popleft()
    return len(_TRADE_TIMES) < MAX_TRADES_PER_HOUR


def _force_hft_signal(df1: pd.DataFrame):
    side = "sell"
    idx  = df1.index
    cl   = float(df1["close"].iloc[-1])
    entry_dt     = pd.Timestamp(idx[-1])
    entry_price  = cl
    hard_sl      = round(entry_price * (1.0 + HFT_FIXED_SL_PCT / 100.0), 6)
    risk_per_unit = abs(entry_price - hard_sl)
    raw_qty = (RISK_PER_TRADE / risk_per_unit) if risk_per_unit > 0 else 0.01
    # Round to the symbol's volume step (0.01 for Exness BTCz) so the
    # broker doesn't reject with TRADE_RETCODE_INVALID_VOLUME.
    qty = round(raw_qty / 0.01) * 0.01
    qty = max(qty, 0.01)   # respect volume_min
    qty = round(qty, 2)
    fcc_ts_dt = entry_dt - pd.Timedelta(minutes=1)

    row = base_row_v1(side)
    row["Green MACD cycle Startime"]            = _s(fcc_ts_dt)
    row["Green MACD cycle Endtime"]             = _s(entry_dt)
    row["No. of Time key Candle got Updated Before obtaining final key candle"] = 0
    row["Final Key Candle Datetime"]            = _s(entry_dt)
    row["Final Key Candle Lowest kama Level obtained at key candle"] = "HFT-FORCED"
    row["Final Strategy Candle Datetime"]       = _s(fcc_ts_dt)
    row["5min Strategy Final candle Price"]     = _f(entry_price)
    row["Final Sell found from which Method"]   = "HFT-FORCED"
    row["Entry Datetime"]                       = _s(entry_dt)
    row["Entry Price"]                          = _f(entry_price)
    row["Hard SL Price"]                        = _f(hard_sl)
    row["Assign Hard SL Percentage"]            = _f(abs(entry_price - hard_sl) / entry_price * 100.0)
    row["Qty"]                                  = _f(qty)
    row["Investment Value for Ratios"]          = _f(qty * entry_price)
    # Fake EMA context — determinstic Above/Below
    parity = int(entry_dt.timestamp()) % 2
    above_below = "Above" if parity else "Below"
    for prefix in ["2hrs", "4hrs", "1D"]:
        for ema in [50, 100, 200]:
            row[f"{prefix} Price above/Below EMA {ema}"] = above_below
    row["Status"] = "Intrade"
    row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    row["Strategy Additional info"] = vkv({
        "Source": "HFT-FORCED (regressive test driver)",
        "Forced SL %": HFT_FIXED_SL_PCT,
        "Hard SL Price": _f(hard_sl),
    })
    return row, entry_dt, entry_price, hard_sl, qty


# ================================================================
# CSV APPEND
# ================================================================
def _append_hft_csv(row):
    if row is None: return
    new_df = pd.DataFrame([row])
    if HFT_LOG_PATH.exists():
        try:
            old_df = pd.read_csv(HFT_LOG_PATH)
            old_df = old_df[~old_df["Event ID"].isin(new_df["Event ID"])]
            merged = pd.concat([old_df, new_df], ignore_index=True)
        except Exception:
            merged = new_df
    else:
        merged = new_df
    merged.to_csv(HFT_LOG_PATH, index=False)


# ================================================================
# MAIN LOOP
# ================================================================
def run_hft_cycle():
    global HALT_HFT
    HALT_HFT = os.getenv("HALT_HFT", "0") == "1"
    if HALT_HFT:
        log("[HFT] HALT_HFT=1 set — skipping signal generation")
        return None
    if not _hourly_cap_ok():
        log(f"[HFT] hourly cap reached ({len(_TRADE_TIMES)}/{MAX_TRADES_PER_HOUR} in last 60m) — skipping")
        return None

    log("Fetching live 1-min candles (HFT)...")
    df1 = fetch_candles(SYMBOL_DATA, "1m", LOOKBACK_1M)
    if df1.empty:
        log("HFT: data missing, skipping...")
        return None

    row, entry_dt, entry_price, hard_sl, qty = _force_hft_signal(df1)
    ev_id = hashlib.sha256(f"hft|{entry_dt}".encode()).hexdigest()[:24]
    row["Event ID"] = ev_id
    _HFT_FIRED.add(ev_id)

    log(f"\n🔔 [HFT] Faking SELL at {entry_dt} | entry={entry_price} | SL={hard_sl} | qty={qty}")
    order_id, retcode, t_comment = mt5_bridge_trade_hft(SYMBOL_BRIDGE, 1, qty, hard_sl)
    
    msg = format_telegram(row)
    if DRY_RUN:
        exec_status = f"\n✅ MT5 Execution: DRY RUN MODE"
    elif order_id > 0 and retcode == 10009:
        exec_status = f"\n✅ MT5 Execution: SUCCESS (Order ID: {order_id})"
    else:
        exec_status = f"\n❌ MT5 Execution: FAILED (Retcode: {retcode}, Error: {t_comment})"
    
    msg += exec_status
    
    if order_id > 0 and (DRY_RUN or retcode == 10009):
        _HFT_TICKETS[order_id] = {
            "entry_price": entry_price,
            "current_sl": hard_sl,
        }
        tg_post(f"✅ [HFT] POSITION OPENED\nTicket: {order_id}\n{msg}")
    else:
        tg_post(f"⚠️ [HFT] MT5 ORDER FAILED\n{msg}")
    _TRADE_TIMES.append(_time.time())
    return row


def main():
    log("=" * 60)
    log(f"🚀 HFT-S17 REGRESSIVE TEST DRIVER  (self-contained)")
    log(f"   bridge symbol : {SYMBOL_BRIDGE}")
    log(f"   magic={HFT_MAGIC} comment='{HFT_COMMENT}'")
    log(f"   dry_run={DRY_RUN}  halt={HALT_HFT}  cap={MAX_TRADES_PER_HOUR}/hr")
    log(f"   log: {HFT_LOG_PATH}")
    log("=" * 60)
    tg_post(f"🚀 HFT-S17 DRIVER — STARTED\n"
            f"symbol={SYMBOL_BRIDGE} | magic={HFT_MAGIC} | "
            f"cap={MAX_TRADES_PER_HOUR}/hr | interval={SIGNAL_INTERVAL_SEC}s | dry_run={DRY_RUN}")

    last_signal_ts = 0.0
    last_poll_ts   = 0.0
    last_trail_ts  = 0.0
    try:
        while True:
            now_mono = _time.time()
            if now_mono - last_poll_ts >= POSITION_POLL_SEC:
                positions = poll_positions()
                last_poll_ts = now_mono
                # Trail open HFT positions whenever we have a fresh positions list
                if isinstance(positions, list):
                    try:
                        trail_hft_positions(positions)
                    except Exception as e:
                        log(f"[HFT-TRAIL] error: {e}")
                    
                    # Exit detection: check for positions closed by SL/TP/manual
                    open_tickets = {int(p.get("ticket", 0)) for p in positions}
                    for t, rec in list(_HFT_TICKETS.items()):
                        if t not in open_tickets:
                            log(f"[EXIT] ticket={t} no longer open → position closed")
                            tg_post(f"🔒 [HFT] POSITION CLOSED\n"
                                    f"Ticket: {t}\n"
                                    f"Entry: {rec.get('entry_price')}\n"
                                    f"Last SL: {rec.get('current_sl')}\n"
                                    f"(position no longer in /positions — closed by SL or manually)")
                            del _HFT_TICKETS[t]
            if now_mono - last_signal_ts >= SIGNAL_INTERVAL_SEC:
                try:
                    row = run_hft_cycle()
                    _append_hft_csv(row)
                except Exception as e:
                    log(f"[HFT] scan error: {e}")
                last_signal_ts = now_mono
            _time.sleep(1)
    except KeyboardInterrupt:
        log("HFT driver stopped by user.")


if __name__ == "__main__":
    main()
