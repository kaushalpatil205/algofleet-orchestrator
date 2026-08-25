"""Strategy 17 — the logic all six live variants share.

MOVED, NOT REWRITTEN. Every function below is byte-identical to the Version 1
scripts it came from; scripts/gen_s17_core.py regenerates this file and can be
re-run to prove it.

The six Version 1 scripts were ~3,000 lines each and differed from one another
by as little as nine lines. Roughly half of each was infrastructure, which now
lives in engine/. This is the other half: the actual Strategy 17 logic, held
once instead of six times.

Three functions genuinely differ between variants and are kept in both forms,
selected by the Spec rather than by which file you happen to be reading:

  find_method1_entry_fast  the M1-M2 pairing walks the threshold forward
                           differently from the M3-M2 / M2-M3 pairings
  find_method2_on_1m       the M2-M3 Variation 4 pairing records one extra
                           invalidation block
  base_row_v1              Variation 4 carries four more columns than
                           Variation 1 (the 1:0.5 target/SL block)

format_telegram had five bodies that differed only in the strategy name, the
buy/sell wording and Variation 4's extra columns, so it is parameterised.

Dropped as dead: _best_value_of_high_or_bb_arr and run_strategy17_variation1
were defined in all six scripts and called by none of them.
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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from engine import Signal, Strategy
from engine.fmt import _f, _kv_lines, _s, vblocks, vkv
from engine.indicators import (
    EMA50_COL, K_EMA50, K_EMAKAMA, K_KLINE, K_SMA,
    VolumeWindows, check_ema_position, make_fast_arrays, make_tf_arrays,
    prepare_df, prepare_df_tf,
)
from engine.notify import log
from engine.sizing import contract_size, to_lots

# --- constants, verbatim from Version 1 ----------------------------------------
ALL_RATIOS   = [0.5] + list(range(1, 11))
RATIOS_FULL  = list(range(1, 11))
TRAIL_CPS    = list(range(2, 10))
METHOD2_BUFFER_CANDLES = 3
METHOD2_CHASE_WINDOW   = 6
MAX_HIGH_UPDATES = 2
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
STARTUTC       = pd.Timestamp("2017-10-01 00:00:00", tz="UTC")
LOOKBACK_5M  = 2500
LOOKBACK_1M  = 6000
LOOKBACK_2H  = 500
LOOKBACK_4H  = 300
LOOKBACK_1D  = 200
RECENT_1M_COUNT = 10


# --- shared logic --------------------------------------------------------------

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


# --- variant-specific logic ----------------------------------------------------
# Kept in both forms rather than merged: these bodies really do differ, and
# picking one would change signals on the strategies that use the other.

def _find_method1_m1m2(arr1, mapped_pos, backward_setup, side):
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


def _find_method1_std(arr1, mapped_pos, backward_setup, side):
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
            bb_up = arr1["bb_upper"][pos]
        
            cand_val = threshold
            cand_src = None
        
            if actual_hi > cand_val:
                cand_val = actual_hi
                cand_src = "highest high"
        
            if not np.isnan(bb_up) and bb_up > cand_val:
                cand_val = float(bb_up)
                cand_src = "upper bb"
        
            if cand_src is not None:
                threshold = cand_val
                got_update = True
                most_updated_val = cand_val
                most_updated_ts = idx[pos]
                most_updated_src = cand_src
        
        else:
            actual_lo = float(arr1["low"][pos])
            bb_lo = arr1["bb_lower"][pos]
        
            cand_val = threshold
            cand_src = None
        
            if actual_lo < cand_val:
                cand_val = actual_lo
                cand_src = "lowest low"
        
            if not np.isnan(bb_lo) and bb_lo < cand_val:
                cand_val = float(bb_lo)
                cand_src = "lower bb"
        
            if cand_src is not None:
                threshold = cand_val
                got_update = True
                most_updated_val = cand_val
                most_updated_ts = idx[pos]
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


def _find_method2_v4(arr1, mapped_pos, side):
    idx = arr1["idx"]; hi_a = arr1["high"]; lo_a = arr1["low"]; cl_a = arr1["close"]
    invalidated: List[OrderedDict] = []; current = None; key_updates = 0

    for pos in range(mapped_pos, len(idx)):
        ts = idx[pos]; hi = hi_a[pos]; lo = lo_a[pos]; cl = cl_a[pos]
        is_key = _touches_key_band_arr(arr1, pos, side)

        if is_key:
            if current is None: current = _new_m2_key_state(arr1, pos, side); continue
            if pos > current["key_pos"]:
                invalidated.append(_m2_invalid_block(current, side,
                    f"Invalidated due to new key candle formed at DT {_s(ts)}"))
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


def _find_method2_std(arr1, mapped_pos, side):
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


def _base_row_var1(side: str) -> OrderedDict:
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


def _base_row_var4(side: str) -> OrderedDict:
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


# --- wiring for the verbatim bodies -------------------------------------------
# Three names the Version 1 functions reach for are now owned by the engine.
# Rather than edit those bodies — the whole point is that they are unmodified —
# the names are bound here and pointed at the engine's services by
# make_strategy(). RISK_PER_TRADE comes from config for the same reason.

VolumeCalculator = VolumeWindows      # renamed in engine.indicators

RISK_PER_TRADE = 100.0                # replaced from config by make_strategy()


def tg_post(text):                    # rebound by make_strategy()
    log(text)


def save_fired_events():              # the engine's journal flushes on write
    return None


# --- the Spec: what actually differs between the six variants ----------------

@dataclass
class Spec:
    """One S17 variant.

    Six of these replace six 3,000-line scripts. Everything here was a
    hard-coded literal repeated across those files.
    """
    id: str                       # strategy_id, also the registry key
    label: str                    # human name, used in Telegram
    symbols: List[str]
    side: str                     # "buy" | "sell"
    variation: int                # 1 | 4
    method1: str                  # "m1m2" | "std"
    method2: str                  # "v4"   | "std"
    csv: str                      # CSV filename template, {symbol} substituted
    log_dir: str
    telegram_name: str = ""       # defaults to label

    def __post_init__(self):
        if not self.telegram_name:
            self.telegram_name = self.label
        if self.side not in ("buy", "sell"):
            raise ValueError(f"{self.id}: side must be buy or sell, got {self.side!r}")
        if self.variation not in (1, 4):
            raise ValueError(f"{self.id}: variation must be 1 or 4")


# The active spec. Set once by make_strategy() before any scan runs, and read
# by the dispatchers below. process_variation1_setup is kept verbatim from
# Version 1 and calls these by their bare names, so the choice has to live
# somewhere it can see — one strategy per process makes that safe.
_ACTIVE: Optional[Spec] = None


def find_method1_entry_fast(arr1, mapped_pos, backward_setup, side):
    if _ACTIVE is not None and _ACTIVE.method1 == "m1m2":
        return _find_method1_m1m2(arr1, mapped_pos, backward_setup, side)
    return _find_method1_std(arr1, mapped_pos, backward_setup, side)


def find_method2_on_1m(arr1, mapped_pos, side):
    if _ACTIVE is not None and _ACTIVE.method2 == "v4":
        return _find_method2_v4(arr1, mapped_pos, side)
    return _find_method2_std(arr1, mapped_pos, side)


def base_row_v1(side: str) -> OrderedDict:
    if _ACTIVE is not None and _ACTIVE.variation == 4:
        return _base_row_var4(side)
    return _base_row_var1(side)


# --- Telegram ----------------------------------------------------------------

def format_telegram(r: dict, symbol: str) -> str:
    """The five Version 1 bodies differed only in the strategy name, the
    buy/sell wording, and Variation 4's two extra price lines."""
    spec = _ACTIVE
    side = spec.side if spec else "buy"
    word = "Buy" if side == "buy" else "Sell"
    head = "🟢 BUY" if side == "buy" else "🔴 SELL"
    ep = r.get("Entry Price", 0)
    lines = [
        f"{head} {symbol}",
        f"Strategy: {spec.telegram_name if spec else 'Strategy 17'} LIVE",
        f"Final Strategy Candle Datetime: {r.get('Final Strategy Candle Datetime','')}",
        f"Entry Datetime: {r.get('Entry Datetime','')}",
        f"Entry Price: {float(ep) if ep else 0.0:.6f}",
    ]
    if spec and spec.variation == 4:
        fep = r.get("Final Entry Price", 0)
        tgt = r.get("0.5 Target Price a/c Actual Entry Price", 0)
        lines += [
            f"Final Entry Price: {float(fep) if fep else 0.0:.6f}",
            f"0.5 Target Price a/c Actual Entry Price: {float(tgt) if tgt else 0.0:.6f}",
        ]
    lines += [
        f"Hard SL Price: {r.get('Hard SL Price','')}",
        f"Qty: {r.get('Qty','N/A')}",
        f"Final {word} found from which Method: "
        f"{r.get(f'Final {word} found from which Method','')}",
        f"Trade Entry Id: {r.get('Event ID','')}",
    ]
    return "\n".join(lines)


# --- the scan, parameterised --------------------------------------------------
# One body for all six. In Version 1 this was 330 lines repeated per script,
# differing only in the side strings, four extra blank fields on Variation 4,
# one apply_variation_4_logic call, and which column the entry timestamp came
# from. Those four differences are now branches on the Spec.

def _contract_qty_column(row, symbol):
    """Insert 'Trading qty Contract' immediately after 'Qty'.

    Column ORDER matters — these CSVs are diffed against backtest output — so
    the row is rebuilt rather than assigned into.
    """
    out = OrderedDict()
    for k, v in row.items():
        out[k] = v
        if k == "Qty":
            out["Trading qty Contract"] = to_lots(symbol, v, row.get("Entry Price"))
    return out


def _blank_invalidated(old, new, side, variation, tc):
    word = "Buy" if side == "buy" else "Sell"
    old["Status"] = f"Invalidated due to {word} occured at same Datetime"
    extra = OrderedDict({
        "Invalidation Reason": old["Status"],
        f"{tc} MACD CYCLE start time due to which setup invalidated":
            new.get(f"{tc} MACD cycle Startime"),
        f"{tc} MACD CYCLE endtime time due to which setup invalidated":
            new.get(f"{tc} MACD cycle Endtime"),
        "Key candle datetime due to which setup invalidated":
            new.get("Final Key Candle Datetime"),
        f"Final {word.lower()} datetime": new.get("Entry Datetime"),
    })
    old["Strategy Additional info"] = (old.get("Strategy Additional info") or "") \
        + "\n\n" + vkv(extra)
    for prefix in ["2hrs", "4hrs", "1D"]:
        for ema in [50, 100, 200]:
            old[f"{prefix} Price above/Below EMA {ema}"] = None
    fields = ["Entry Datetime", "Entry Price"]
    if variation == 4:
        fields += ["0.5 Target Price a/c Actual Entry Price",
                   "0.5 SL Price a/c Actual Entry Price",
                   "0.5 Target Price/ SL Price achieved DT",
                   "Final Entry Price", "Final Entry Date"]
    fields += ["Hard SL Price", "Assign Hard SL Percentage", "Qty",
               "Investment Value for Ratios", "BackTest Result",
               "1:0.5 Exit Datetime", "1:0.5 Exit Price", "1:0.5 SL hit Due to",
               "1:0.5 Holding Time (hrs)", "P/L 1:0.5"]
    for fld in fields:
        old[fld] = None
    for r_ in RATIOS_FULL:
        old[f"1:{r_} Exit Datetime"] = None
        old[f"1:{r_} Exit Price"] = None
        old[f"1:{r_} SL hit Due to"] = None
        old[f"1:{r_} Holding Time (hrs)"] = None
        old[f"P/L 1:{r_}"] = None
        if r_ >= 2:
            old[f"Status 1:{r_}"] = None
    for _, col in VOL_COL_MAP.items():
        old[col] = None
    old[f"Final {word} found from which Method"] = None


def prepare_frames(ctx):
    """Indicators and fast arrays for all five timeframes."""
    df5_raw, df1_raw = ctx.candles("M5"), ctx.candles("M1")
    df2h_raw, df4h_raw, df1d_raw = ctx.candles("H2"), ctx.candles("H4"), ctx.candles("D1")

    df5, df1 = prepare_df(df5_raw), prepare_df(df1_raw)
    df2h = prepare_df_tf(df2h_raw) if not df2h_raw.empty else pd.DataFrame()
    df4h = prepare_df_tf(df4h_raw) if not df4h_raw.empty else pd.DataFrame()
    df1d = prepare_df_tf(df1d_raw) if not df1d_raw.empty else pd.DataFrame()

    empty_tf = {"idx": pd.DatetimeIndex([]), "close": np.array([]),
                "ema50": np.array([]), "ema100": np.array([]), "ema200": np.array([])}
    arr2h = make_tf_arrays(df2h) if not df2h.empty else empty_tf
    arr4h = make_tf_arrays(df4h) if not df4h.empty else arr2h
    arr1d = make_tf_arrays(df1d) if not df1d.empty else arr2h

    arr1 = make_fast_arrays(df1)
    return {
        "df5": df5, "df1": df1, "df1_raw": df1_raw,
        "arr5": make_fast_arrays(df5), "arr1": arr1,
        "arr2h": arr2h, "arr4h": arr4h, "arr1d": arr1d,
        "cycles1": build_cycle_map(arr1), "volcalc1": VolumeWindows(df1_raw),
    }


def build_rows(ctx, spec, F=None):
    """Every setup on the 5-minute frame, before dedupe and before Variation 4.

    Kept separate from the rest of the pipeline so it can be compared directly
    against the Version 1 scripts — this is where all the strategy logic runs.
    """
    F = F or prepare_frames(ctx)
    rows = []
    for s5 in scan_5m_final_strategy_candles(F["df5"], spec.side):
        row = process_variation1_setup(s5, F["arr5"], F["arr1"], F["cycles1"],
                                       F["volcalc1"], F["arr2h"], F["arr4h"],
                                       F["arr1d"], spec.side)
        row = _contract_qty_column(row, ctx.symbol)
        row["Final Strategy Candle Datetime"] = _s(s5.get("fcc_ts") or s5.get("cycle_end_ts"))
        rows.append(row)
    return rows


def dedupe_rows(rows, spec):
    """Two setups resolving to the same entry minute: the newest wins.

    Verbatim in effect from Version 1's inline block, including the extra
    fields Variation 4 has to blank.
    """
    tc = "Red" if spec.side == "buy" else "Green"
    entry_map: Dict[str, List[int]] = {}
    for i, row in enumerate(rows):
        edt = row.get("Entry Datetime")
        if edt and row.get("Status") == "Intrade":
            entry_map.setdefault(str(edt), []).append(i)
    for edt, idxs in entry_map.items():
        if len(idxs) < 2:
            continue
        ordered = sorted(idxs,
                         key=lambda i: str(rows[i].get(f"{tc} MACD cycle Startime") or ""))
        newest = rows[ordered[-1]]
        for old_i in ordered[:-1]:
            _blank_invalidated(rows[old_i], newest, spec.side, spec.variation, tc)
    return rows


def build_signals(ctx, spec):
    """Scan one symbol and return the setups it found."""
    F = prepare_frames(ctx)
    rows = build_rows(ctx, spec, F)
    dedupe_rows(rows, spec)

    if spec.variation == 4:
        for row in rows:
            if row.get("Status") == "Intrade":
                apply_variation_4_logic(row, F["arr1"], F["arr5"], F["arr2h"],
                                        F["arr4h"], F["arr1d"], F["volcalc1"],
                                        spec.side)

    recent_1m = F["df1_raw"].index[-RECENT_1M_COUNT:]
    date_col = "Final Entry Date" if spec.variation == 4 else "Entry Datetime"
    signals = []
    for row in rows:
        ev_id = ctx.event_id(spec.side, ctx.symbol,
                             row.get("Final Strategy Candle Datetime"))
        row["Event ID"] = ev_id
        raw_dt = row.get(date_col)
        entry_dt = pd.Timestamp(raw_dt) if raw_dt else None
        arr1_ref, entry_pos_ref = row.pop("_arr1", None), row.pop("_entry_pos", None)
        row.pop("_side", None)
        sig = Signal(
            symbol=ctx.symbol, side=spec.side, event_id=ev_id,
            status=row.get("Status", ""),
            entry_price=row.get("Entry Price"),
            entry_dt=entry_dt,
            hard_sl=row.get("Hard SL Price"),
            qty=row.get("Qty"),
            lots=row.get("Trading qty Contract"),
            row=row,
            csv=spec.csv.format(symbol=ctx.symbol),
            fresh=bool(entry_dt is not None and entry_dt in recent_1m),
        )
        # stashed for the enrich hook, off the journal row
        sig._arr1, sig._entry_pos = arr1_ref, entry_pos_ref
        signals.append(sig)
    return signals


def make_strategy(spec: Spec, entry: str = None) -> Strategy:
    """Build the engine Strategy for one S17 variant.

    `entry` is the variant file's own __file__. Config is read from the JSON
    sidecar beside it — the same convention Version 1 used — which keeps
    working when the module is imported by path rather than run, as the CI
    probe and the backtest harness both do.
    """
    global _ACTIVE, RISK_PER_TRADE, tg_post, save_fired_events
    _ACTIVE = spec

    strategy = Strategy(
        id=spec.id, label=spec.label, symbols=spec.symbols,
        timeframes={"M5": LOOKBACK_5M, "M1": LOOKBACK_1M, "H2": LOOKBACK_2H,
                    "H4": LOOKBACK_4H, "D1": LOOKBACK_1D},
        sides=[spec.side], log_dir=spec.log_dir,
        recent_bars=RECENT_1M_COUNT, comment="S17 Bridge",
    )
    import os as _os
    cfg_file = (_os.path.splitext(_os.path.abspath(entry))[0] + ".json") if entry else None
    strategy.build(config_file=cfg_file)
    RISK_PER_TRADE = strategy.risk
    tg_post = strategy.telegram.post
    save_fired_events = lambda: None

    @strategy.scan
    def scan(ctx):
        signals = build_signals(ctx, spec)
        for sig in signals:
            if sig.tradeable and sig.fresh and not strategy.journal.has_fired(sig.event_id):
                log(f"\n🔔 NEW SIGNAL [{sig.symbol}]:\n{format_telegram(sig.row, sig.symbol)}")
        return signals

    @strategy.enrich
    def enrich(sig, pos):
        """Re-run the ratio simulation against the price actually filled, so
        the journal carries the executed result beside the theoretical one."""
        arr1 = getattr(sig, "_arr1", None)
        entry_pos = getattr(sig, "_entry_pos", None)
        if arr1 is None or entry_pos is None:
            return
        _, bt, qty, inv = run_backtest_v1(arr1, entry_pos, float(pos.entry_price),
                                          float(pos.hard_sl), sig.side)
        sig.row["Executed Result"] = bt
        sig.row["Executed Qty"] = qty
        sig.row["Executed Investment Value"] = inv

    return strategy
