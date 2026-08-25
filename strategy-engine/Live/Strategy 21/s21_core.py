"""Strategy 21 — the live logic, on the Version 2 engine.

MOVED, NOT REWRITTEN, with one exception. Every function below is byte-identical
to Bridge-S21-1_10-Ratios-BTCUSD-Live.py except run_strategy21, which had order
placement, post-entry stop correction and Telegram exit alerts inlined in the
middle of its row loop. That block is cut; the engine does it now.

scripts/gen_s21_core.py regenerates this file and refuses to run if the
original's shape has changed, so the cut cannot silently drift.

Strategy 21 is a different engine from Strategy 17: 15-minute three-candle wick
setups, a 3-minute EMA50-filtered entry, a Fibonacci hard stop from an
RSI/MACD-cycle chain on both 3m and 15m, a five-indicator soft stop, and
two-pass Stage-2 exit confirmation. Only the infrastructure is shared.

Two Version 1 behaviours change here, deliberately, because the engine applies
one rule to every strategy:

  * The stop is always corrected after a fill. S21 skipped the correction when
    the fill lookup failed, leaving the placeholder stop on a live position.
  * The ratio ladder is measured from the CORRECTED stop. S21 built its ladder
    from the raw signal stop while the broker held the corrected one, so its
    trail rungs did not correspond to the money actually at risk.
"""

# Annotations must not be evaluated at definition time. The Version 1 bodies
# are copied verbatim and some annotate parameters with names this module
# binds further down (volcalc1: VolumeCalculator). Python 3.14 defers
# annotation evaluation by default, which hides that; the strategy host runs
# 3.12, where it is a NameError at import. Deferring explicitly makes both
# behave the same.
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import talib

from engine import Signal, Strategy
from engine.fmt import _f, _kv_lines, _s, vkv
from engine.indicators import (
    FIB_LEVELS_RAW, VolumeBars, add_smooth_macd_cycles, calc_emakama,
    calc_kama_line, calc_sma_kama, get_fib_levels,
)
from engine.notify import log

# --- constants, verbatim from Version 1 ----------------------------------------
COIN_NAME      = "BTCUSDT"
SYMBOL         = "BTCUSD"
SYMBOL_BRIDGE  = "BTCUSD"
LOOKBACK_15M = 2500
LOOKBACK_3M  = 4000
LOOKBACK_1M  = 6000
ALL_RATIOS      = [0.5] + list(range(1, 11))
RATIOS_FULL     = list(range(1, 11))
RECENT_1M_COUNT = 10


# --- wiring for the verbatim bodies -------------------------------------------
# Names the Version 1 bodies reach for that the engine now owns. Bound here so
# those bodies stay unmodified; make_strategy() points them at the real thing.

VolumeCalculator = VolumeBars
RISK_PER_TRADE = 500.0


def tg_post(text):
    log(text)


def save_fired_events():
    return None                       # the engine journal flushes on write


def mt5_bridge_close_ticket(ticket):
    """Rebound to the engine bridge by make_strategy().

    S21 really does close positions on a Stage-2 or Soft-SL exit — worth saying
    plainly, because the repository README describes those exits as theoretical
    simulation only. They are not: process_telegram_exits() closes the ticket.
    """
    log(f"[S21] close requested for {ticket} before wiring — ignored")
    return False


class _LedgerSet:
    """Set interface over the engine journal.

    process_telegram_exits does `x in _fired_events` and `_fired_events.add(x)`
    to keep from re-sending the same exit alert. The engine journal already
    provides exactly that, persisted, so this adapts one to the other instead
    of keeping a second in-memory set that a restart would lose.
    """

    def __init__(self):
        self._journal = None

    def bind(self, journal):
        self._journal = journal

    def __contains__(self, key):
        return self._journal.has_fired(key) if self._journal else False

    def add(self, key):
        if self._journal:
            self._journal.mark_fired(key)


class _EventIndex:
    """event_id -> ticket, backed by the engine position book.

    run_strategy21 re-anchors its theoretical ratio simulation to the price
    actually filled when a position exists for the setup. Version 1 read two
    module-level dicts to do that; these adapters present the same interface
    over the book so the body stays unmodified.
    """

    def __init__(self):
        self._book = None

    def bind(self, book):
        self._book = book

    def __contains__(self, event_id):
        return self._book.has_event(event_id) if self._book else False

    def __getitem__(self, event_id):
        ticket = self._book.ticket_of(event_id) if self._book else None
        if ticket is None:
            raise KeyError(event_id)
        return ticket


class _TicketMap:
    """ticket -> the dict shape Version 1 kept, backed by the position book."""

    def __init__(self):
        self._book = None

    def bind(self, book):
        self._book = book

    def get(self, ticket, default=None):
        pos = self._book.get(ticket) if self._book else None
        if pos is None:
            return default
        return {"symbol": pos.symbol, "side": pos.side, "event_id": pos.event_id,
                "entry_price": pos.entry_price, "hard_sl": pos.hard_sl,
                "current_sl": pos.current_sl, "exit_datetime": pos.exit_datetime}

    def __contains__(self, ticket):
        return (self._book.get(ticket) is not None) if self._book else False


_fired_events = _LedgerSet()
_event_to_ticket = _EventIndex()
_ticket_map = _TicketMap()


# --- strategy logic ------------------------------------------------------------

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
            "start_dt": idx[i-2], "end_dt": idx[i], "extreme_close": setup_extreme_close,
            "dt_1": idx[i-2], "extreme_1": hi[i-2] if side == "sell" else lo[i-2],
            "dt_2": idx[i-1], "extreme_2": hi[i-1] if side == "sell" else lo[i-1],
            "dt_3": idx[i],   "extreme_3": hi[i] if side == "sell" else lo[i],
            "pos_3": i
        })
    return setups


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

    lines = []
    for r in ALL_RATIOS:
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


def create_empty_row(side: str) -> OrderedDict:
    row = OrderedDict()
    row["15min Setup Starttime"] = "None"
    row["15min Setup Endtime"] = "None"
    row["15min candle mapped to 3min is"] = "None"
    if side == "sell":
        row["Previous 3 minute candle open and close above EMA 50"] = "None"
        row["Candle closing below Highest close Value"] = "None"
    else:
        row["Previous 3 minute candle open and close below EMA 50"] = "None"
        row["Candle closing above Lowest close Value"] = "None"
    
    cols1 = [
        "Status", "Entry Datetime", "Entry Price", "Hard SL obtained from",
        "Hard SL Value", "Hard SL Percentage", "Actual SL Points",
        "SL Value Consider for Qty", "SL Points for Qty",
        "SL Value Percentage consider for Qty", "Qty", "Investment Value for Ratios"
    ]
    for c in cols1: row[c] = "None"
    
    for r in [0.5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
        lbl = "1:0.5" if r == 0.5 else f"1:{r}"
        row[f"Status {lbl}"] = "None"
        row[f"{lbl} Exit Datetime"] = "None"
        row[f"{lbl} Exit Price"] = "None"
        row[f"{lbl} SL hit Due to"] = "None"
        row[f"{lbl} Holding Time (hrs)"] = "None"
        row[f"P/L {lbl}"] = "None"
        
    cols2 = [
        "Current 15min:last 12 hrs Vol", "Current 15min:last 24 hrs Vol",
        "Current 15min:last 48 hrs Vol", "Strategy Additional info",
        "Soft SL Additional info", "Backtest Result", "Logged At UTC",
        "Event ID", "Live MT5 Order ID", "Live Entry Price",
        "Live Corrected Hard SL", "Executed Result", "Executed Qty",
        "Executed Investment Value", "Live Exit Datetime"
    ]
    for c in cols2: row[c] = "None"
    return row


def format_telegram(r: dict, side: str, order_id: int = 0) -> str:
    color = "🟢 BUY" if side == "buy" else "🔴 SELL"
    msg = (
        f"{color} {SYMBOL}\n"
        f"Asset Name: {SYMBOL}\n"
        f"Strategy: Strategy 21 Live\n"
        f"Trade Entry Id: {r.get('Event ID','')}\n"
        f"15min Setup Starttime: {r.get('15min Setup Starttime','')}\n"
        f"15min Setup Endtime: {r.get('15min Setup Endtime','')}\n"
        f"15min candle mapped to 3min is: {r.get('15min candle mapped to 3min is','')}\n"
        f"Previous 3 minute candle open and close {'below' if side=='buy' else 'above'} EMA 50: {r.get('Previous 3 minute candle open and close below EMA 50' if side=='buy' else 'Previous 3 minute candle open and close above EMA 50','')}\n"
        f"Candle closing {'above Lowest' if side=='buy' else 'below Highest'} close Value: {r.get('Candle closing above Lowest close Value' if side=='buy' else 'Candle closing below Highest close Value','')}\n"
        f"Status: {r.get('Status','')}\n"
        f"Entry Datetime: {r.get('Entry Datetime','')}\n"
        f"Entry Price: {_f(r.get('Entry Price',0.0))}\n"
        f"Hard SL obtained from: {r.get('Hard SL obtained from','')}\n"
        f"Hard SL Value: {_f(r.get('Hard SL Value',0.0))}\n"
        f"Hard SL Percentage: {_f(r.get('Hard SL Percentage',0.0))}\n"
        f"SL Value Consider for Qty: {_f(r.get('SL Value Consider for Qty',0.0))}\n"
        f"SL Value Percentage consider for Qty: {_f(r.get('SL Value Percentage consider for Qty',0.0))}\n"
        f"Qty: {_f(r.get('Qty',0.0))}\n"
    )
    if order_id > 0:
        msg += f"\n✅ MT5 Execution: SUCCESS (Order ID: {order_id})"
    return msg


def format_telegram_exit(r, side, ratio_str, msg_type, event_type, exit_price, exit_dt, pnl, new_trail_applied, new_trail_at):
    return (
        f"Asset: {SYMBOL} (Theoretical)\n"
        f"Strategy: Strategy 21 Live\n"
        f"Entry Signal: {side}\n"
        f"Trade Entry Id: {r.get('Event ID','')}\n"
        f"Final Entry Datetime: {r.get('Entry Datetime','')}\n"
        f"Hard SL Price: {_f(r.get('Hard SL Value',0.0))}\n"
        f"Ratio: {ratio_str}\n"
        f"Message Type: {msg_type}\n"
        f"New Trailed SL Applied: {new_trail_applied}\n"
        f"New Trail SL at: {new_trail_at}\n"
        f"Event Occured Type: {event_type}\n"
        f"Exit Price: {_f(exit_price)}\n"
        f"Exit Datetime: {exit_dt}\n"
        f"PNL: {_f(pnl)}\n\n"
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
        status = str(row.get(f"Status {lbl}", "None"))
        due = str(row.get(f"{lbl} SL hit Due to", "None"))
        if exit_dt_str == "None" or exit_dt_str == "nan": continue
        try: exit_ts = pd.Timestamp(exit_dt_str)
        except: continue

        is_target = (status == "Target Hit")
        if is_target:
            msg_ev = f"{ev_id}_TGT_{r}"
            if msg_ev not in _fired_events and exit_ts in recent_1m:
                _fired_events.add(msg_ev)
                save_fired_events()
                nt_app = "yes" if r >= 2 else "no"
                nt_at = "Entry Price (Breakeven)" if r == 2 else (f"1:{r-2} target" if r > 2 else "N/A")
                msg = format_telegram_exit(row, side, f"1:{r}", "Target Hit (Theoretical Simulation)", f"1:{r} target Hit", exit_price, exit_dt_str, pnl, nt_app, nt_at)
                tg_post(msg)

    sl_groups = {}
    for r in ALL_RATIOS:
        lbl = "1:0.5" if r == 0.5 else f"1:{r}"
        exit_dt_str = str(row.get(f"{lbl} Exit Datetime", "None"))
        due = str(row.get(f"{lbl} SL hit Due to", "None"))
        exit_price = str(row.get(f"{lbl} Exit Price", ""))
        status = str(row.get(f"Status {lbl}", "None"))
        if exit_dt_str == "None" or exit_dt_str == "nan": continue
        if "SL" in status:
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
            
            if "Target" not in due:
                ticket = row.get("Live MT5 Order ID")
                if ticket:
                    if mt5_bridge_close_ticket(ticket):
                        tg_post(f"🔒 POSITION CLOSED MANUALLY (STAGE 2 / SOFT SL)\nTicket: {ticket}\nReason: {e_type}\nExit Datetime: {exit_dt_str}")


def run_strategy21(arr15, arr3, arr1, side, recent_1m, volcalc15):
    setups = scan_15m_setups(arr15, side)
    idx3, idx1 = arr3["idx"], arr1["idx"]
    setup_entries = []; entry_dt_to_max_start = {}

    for s in setups:
        end_15m_dt = s["end_dt"]; map_3m_dt = end_15m_dt + timedelta(minutes=15)
        pos3 = idx3.searchsorted(map_3m_dt, side="left")
        if pos3 >= len(idx3) or pos3 < 3:
            setup_entries.append(("invalid", "Invalidated due to entry signal not found", False))
            continue
        
        prev3_ok = True
        for p in range(pos3 - 3, pos3):
            op3, cl3, ema3 = arr3["open"][p], arr3["close"][p], arr3["ema50"][p]
            if side == "sell" and not (op3 > ema3 and cl3 > ema3): prev3_ok = False; break
            if side == "buy" and not (op3 < ema3 and cl3 < ema3): prev3_ok = False; break
        if not prev3_ok:
            setup_entries.append(("invalid", "Invalidated due to 3min candle does not open close above EMA 50" if side == "sell" else "Invalidated due to 3min candle does not open close below EMA 50", False))
            continue
        
        entry_3m_pos = None
        for p in range(pos3, len(idx3)):
            cl3, ema3 = arr3["close"][p], arr3["ema50"][p]
            if side == "sell" and cl3 < ema3 and cl3 < s["extreme_close"]: entry_3m_pos = p; break
            if side == "buy" and cl3 > ema3 and cl3 > s["extreme_close"]: entry_3m_pos = p; break
        if entry_3m_pos is None:
            setup_entries.append(("invalid", "Invalidated due to entry signal not found", True))
            continue
        
        entry_dt = idx3[entry_3m_pos]; entry_price = float(arr3["close"][entry_3m_pos])
        setup_entries.append((entry_3m_pos, entry_dt, entry_price, map_3m_dt))
        if entry_dt not in entry_dt_to_max_start or s["start_dt"] > entry_dt_to_max_start[entry_dt]:
            entry_dt_to_max_start[entry_dt] = s["start_dt"]

    rows = []
    for i, s in enumerate(setups):
        row = create_empty_row(side)
        row["15min Setup Starttime"] = _s(s["start_dt"])
        row["15min Setup Endtime"] = _s(s["end_dt"])
        map_3m_dt = s["end_dt"] + timedelta(minutes=15)
        row["15min candle mapped to 3min is"] = _s(map_3m_dt)
        entry_info = setup_entries[i]
        
        if isinstance(entry_info, tuple) and entry_info[0] == "invalid":
            prev3_ok_status = entry_info[2]
            row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "yes" if prev3_ok_status else "no"
            row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "no"
            row["Status"] = entry_info[1]
            row["Event ID"] = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            row["Strategy Additional info"] = "None"
            row["Soft SL Additional info"] = "None"
            rows.append(row)
            continue
        entry_3m_pos, entry_dt, entry_price, map_3m_dt = entry_info
        
        if s["start_dt"] < entry_dt_to_max_start[entry_dt]:
            row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "yes"
            row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "yes"
            row["Entry Datetime"] = _s(entry_dt)
            row["Entry Price"] = entry_price
            row["Status"] = "Invalidated to Entry occured at same datetime"
            row["Event ID"] = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            
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
            rows.append(row)
            continue

        row["Previous 3 minute candle open and close above EMA 50" if side == "sell" else "Previous 3 minute candle open and close below EMA 50"] = "yes"
        row["Candle closing below Highest close Value" if side == "sell" else "Candle closing above Lowest close Value"] = "yes"
        row["Entry Datetime"] = _s(entry_dt)
        row["Entry Price"] = entry_price
        
        sl3 = compute_hard_sl_on_tf(arr3, entry_dt, side)
        sl15 = compute_hard_sl_on_tf(arr15, entry_dt, side)
        if not sl3.get("ok") or not sl15.get("ok"):
            row["Status"] = "Invalidated due to Hard SL computation failure"
            row["Event ID"] = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            row["Strategy Additional info"] = "None"
            row["Soft SL Additional info"] = "None"
            rows.append(row)
            continue
        
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
            row["Hard SL obtained from"] = hard_sl_from
            row["Hard SL Value"] = hard_sl_price
            row["Hard SL Percentage"] = hard_sl_pct
            row["Actual SL Points"] = sl_points
            row["Event ID"] = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            row["Strategy Additional info"] = "None"
            row["Soft SL Additional info"] = "None"
            rows.append(row); continue
        if side == "buy" and hard_sl_pct <= 1.0:
            row["Status"] = "Invalidated due to Hard SL % is Less than or equal to 1 percentage"
            row["Hard SL obtained from"] = hard_sl_from
            row["Hard SL Value"] = hard_sl_price
            row["Hard SL Percentage"] = hard_sl_pct
            row["Actual SL Points"] = sl_points
            row["Event ID"] = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
            row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            row["Strategy Additional info"] = "None"
            row["Soft SL Additional info"] = "None"
            rows.append(row); continue

        row["Status"] = "Intrade"
        adj_sl_points = sl_points * 1.30
        sl_price_for_qty = entry_price + adj_sl_points if side == "sell" else entry_price - adj_sl_points
        sl_pct_for_qty = (adj_sl_points / entry_price * 100.0) if entry_price > 0 else 0.0
        qty = RISK_PER_TRADE / adj_sl_points if adj_sl_points > 0 else 0.0
        
        row["Hard SL obtained from"] = hard_sl_from
        row["Hard SL Value"] = hard_sl_price
        row["Hard SL Percentage"] = hard_sl_pct
        row["Actual SL Points"] = sl_points
        row["SL Value Consider for Qty"] = sl_price_for_qty
        row["SL Points for Qty"] = adj_sl_points
        row["SL Value Percentage consider for Qty"] = sl_pct_for_qty
        row["Qty"] = max(round(qty, 2), 0.01)
        row["Investment Value for Ratios"] = row["Qty"] * entry_price
        
        pos_1m = idx1.searchsorted(entry_dt, side="left")
        hsl_stage2 = track_stage2_exit(arr1, pos_1m, hard_sl_price, side)
        hard_sl_exit_dt = hsl_stage2["exit_dt"] if hsl_stage2.get("exit_found") else None
        
        ssl = compute_soft_sl(arr3, arr1, entry_3m_pos, side)
        soft_sl_exit_dt = ssl["stage2"]["exit_dt"] if ssl.get("ok") and ssl["stage2"].get("exit_found") else None
        
        ev_id_temp = row.get("Event ID") or hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
        final_entry_price = entry_price
        final_adj_sl_points = adj_sl_points
        if ev_id_temp in _event_to_ticket:
            rec = _ticket_map.get(_event_to_ticket[ev_id_temp])
            if rec:
                final_entry_price = rec["entry_price"]
                final_adj_sl_points = abs(final_entry_price - rec["hard_sl"])
        
        bt_results, bt_text = run_backtest_engine(arr1, pos_1m, final_entry_price, final_adj_sl_points, hard_sl_price, hard_sl_exit_dt, soft_sl_exit_dt, side)
        for r in ALL_RATIOS:
            lbl = f"1:{r}" if r != 0.5 else "1:0.5"
            res = bt_results[r]
            row[f"Status {lbl}"] = res.get("exit_status")
            row[f"{lbl} Exit Datetime"] = res.get("exit_datetime")
            row[f"{lbl} Exit Price"] = res.get("exit_price")
            row[f"{lbl} SL hit Due to"] = res.get("sl_hit_due_to")
            row[f"{lbl} Holding Time (hrs)"] = res.get("holding_hours")
            row[f"P/L {lbl}"] = res.get("pnl")

        vols = volcalc15.ratios(s["end_dt"])
        row["Current 15min:last 12 hrs Vol"] = vols.get("Current 15min:last 12 hrs Vol")
        row["Current 15min:last 24 hrs Vol"] = vols.get("Current 15min:last 24 hrs Vol")
        row["Current 15min:last 48 hrs Vol"] = vols.get("Current 15min:last 48 hrs Vol")
        
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
            ss_info["Final Exit Candle Datetime :"] = _s(soft_sl_exit_dt)
            ss_info["Final Exit Price :"] = _f(s2.get("exit_price"))
            ss_info["Final Exit Due to Soft SL :"] = "yes" if s2.get("exit_found") else "no"

            row["Soft SL Additional info"] = vkv(ss_info)
        else:
            row["Soft SL Additional info"] = "None"
        
        row["Backtest Result"] = bt_text
        row["Executed Result"] = bt_text

        ev_id = hashlib.sha256(f"{side}|{s['start_dt']}".encode()).hexdigest()[:24]
        row["Logged At UTC"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        row["Event ID"] = ev_id
        row["Live MT5 Order ID"] = None
        row["Live Entry Price"] = None
        row["Live Corrected Hard SL"] = None
        row["Executed Qty"] = None
        row["Executed Investment Value"] = None
        row["Live Exit Datetime"] = None

        # Version 1 placed the order, corrected the stop and posted the exit
        # alerts right here, in the middle of building a row. The engine owns
        # all of that now, so the loop keeps only what it is for: producing the
        # row. What the order needs is stashed for the caller to turn into a
        # Signal.
        row["_entry_dt"] = entry_dt
        row["_entry_price"] = entry_price
        row["_hard_sl"] = hard_sl_price

        rows.append(row)
    
    return rows


# --- the engine wrapper -------------------------------------------------------

def build_signals(ctx):
    """Scan both sides and return every setup found.

    S21 trades long and short from one process, so this runs the strategy twice
    over the same frames and journals each side to its own CSV — the same shape
    Version 1's run_live_scan() had.
    """
    df15, df3, df1 = ctx.candles("M15"), ctx.candles("M3"), ctx.candles("M1")

    # Version 1 dropped the newest bar on every timeframe: it is still forming,
    # and acting on a partial candle produces signals that vanish when it closes.
    df15, df3, df1 = df15.iloc[:-1], df3.iloc[:-1], df1.iloc[:-1]
    if df15.empty or df3.empty or df1.empty:
        log("[S21] a timeframe came back empty — skipping this scan")
        return []

    arr15, arr3, arr1 = prepare_15m_data(df15), prepare_3m_data(df3), prepare_1m_data(df1)
    volcalc15 = VolumeBars(df15)
    recent_1m = df1.index[-RECENT_1M_COUNT:]

    signals = []
    for side in ("sell", "buy"):
        for row in run_strategy21(arr15, arr3, arr1, side, recent_1m, volcalc15):
            entry_dt = row.pop("_entry_dt", None)
            entry_price = row.pop("_entry_price", None)
            hard_sl = row.pop("_hard_sl", None)
            ev_id = row.get("Event ID") or ctx.event_id(side, entry_dt)
            row["Event ID"] = ev_id
            signals.append(Signal(
                symbol=ctx.symbol, side=side, event_id=ev_id,
                status=row.get("Status", ""),
                entry_price=entry_price,
                entry_dt=entry_dt,
                hard_sl=hard_sl,
                qty=row.get("Qty"),
                # S21 sizes in lots directly — no contract-size conversion,
                # which is why Version 1 passed row["Qty"] straight to /trade.
                lots=row.get("Qty"),
                row=row,
                csv=f"Strategy21_{ctx.symbol}_{side.upper()}_live.csv",
                fresh=bool(entry_dt is not None and entry_dt in recent_1m),
            ))
            signals[-1]._recent_1m = recent_1m
    return signals


def make_strategy(entry: str = None) -> Strategy:
    global RISK_PER_TRADE, tg_post, save_fired_events, mt5_bridge_close_ticket

    strategy = Strategy(
        id="S21-BTCUSD-LIVE",
        label="S21 · BTCUSD",
        symbols=[SYMBOL],
        timeframes={"M15": LOOKBACK_15M, "M3": LOOKBACK_3M, "M1": LOOKBACK_1M},
        sides=["buy", "sell"],
        log_dir="./bridge/Strategy 21 Live Logs",
        recent_bars=RECENT_1M_COUNT,
        comment="S21 Bridge",
    )
    import os as _os
    cfg = (_os.path.splitext(_os.path.abspath(entry))[0] + ".json") if entry else None
    strategy.build(config_file=cfg)

    RISK_PER_TRADE = strategy.risk
    tg_post = strategy.telegram.post
    save_fired_events = lambda: None
    mt5_bridge_close_ticket = strategy.bridge.close
    _fired_events.bind(strategy.journal)
    _event_to_ticket.bind(strategy.book)
    _ticket_map.bind(strategy.book)

    @strategy.scan
    def scan(ctx):
        return build_signals(ctx)

    @strategy.enrich
    def enrich(sig, pos):
        """Stage-2 / Soft-SL exit handling.

        Version 1 ran this for rows carrying a live ticket, right after
        execution. It needs "Live MT5 Order ID", which the engine has just
        stamped on the row, so the enrich hook is where it belongs now.
        """
        recent = getattr(sig, "_recent_1m", None)
        if recent is None:
            return
        process_telegram_exits(sig.row, sig.side, recent)

    return strategy
