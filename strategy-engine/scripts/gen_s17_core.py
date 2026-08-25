#!/usr/bin/env python3
"""Generate Live/Strategy 17/s17_core.py from the Version 1 scripts.

Every strategy function is copied byte for byte out of the originals. What
this decides is only WHERE each one goes:

  * 30 functions are identical (or AST-identical) in all six variants -> core.
  * find_method1_entry_fast, find_method2_on_1m and base_row_v1 each have two
    genuinely different bodies -> both are kept, suffixed, and dispatched.
  * format_telegram had five bodies differing only in the strategy name, the
    side wording and V4's extra columns -> parameterised.
  * _best_value_of_high_or_bb_arr and run_strategy17_variation1 are dead in
    all six (defined, never called) -> dropped.

Re-run it after changing a Version 1 script and diff the result.
"""
import ast, glob, hashlib, os, re, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S17 = os.path.join(HERE, "Live", "Strategy 17")
REF = os.path.join(S17, "Bridge-S17-M2-M3-V4-XAUUSD-Buy-Live.py")
V1REF = os.path.join(S17, "Bridge-S17_M3_M2_V1_XAUUSD_SELL_Live.py")
M1M2REF = os.path.join(S17, "Bridge-S17-M1-M2-V1-Forex-Live-Sell.py")

SHARED = ["build_cycle_map","kama_values_at_arr","level_values_at_arr","_extreme_kama",
 "_extreme_level","_touches_key_band_arr","_closes_past_threshold",
 "_candidate_exceeds_threshold","_close_beyond_bb_arr","_special_same_candle_bb_close",
 "add_paired_active_info","_new_5m_setup","_reset_after_key_update_5m",
 "scan_5m_final_strategy_candles","eval_cycle_to_mapped_nearest",
 "find_backward_setup_on_1m_nearest","_new_m2_key_state","_m2_invalid_block",
 "compute_hard_sl_from_5m_window","_pct","_tgt","_pnl","run_backtest_v1",
 "_fill_5m_block","build_strategy_additional_info_v1","build_m1_additional_info_v1",
 "build_m2_additional_info_v1","process_variation1_setup","format_telegram_exit",
 "process_telegram_exits"]

CONSTS = ["ALL_RATIOS","RATIOS_FULL","TRAIL_CPS","METHOD2_BUFFER_CANDLES",
          "METHOD2_CHASE_WINDOW","MAX_HIGH_UPDATES","VOL_COL_MAP","STARTUTC",
          "LOOKBACK_5M","LOOKBACK_1M","LOOKBACK_2H","LOOKBACK_4H","LOOKBACK_1D",
          "RECENT_1M_COUNT"]



WRAPPER = r'''# --- wiring for the verbatim bodies -------------------------------------------
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
'''


def top(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")
    fns, consts = {}, {}
    for n in tree.body:
        t = "\n".join(lines[n.lineno - 1:n.end_lineno])
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            fns[n.name] = t
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            consts[n.targets[0].id] = t
    return fns, consts


def main():
    ref_f, ref_c = top(REF)
    v1_f, _ = top(V1REF)
    m1m2_f, _ = top(M1M2REF)

    out = ['''"""Strategy 17 — the logic all six live variants share.

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

''']

    out.append("# --- constants, verbatim from Version 1 " + "-" * 40 + "\n")
    for c in CONSTS:
        if c in ref_c:
            out.append(ref_c[c] + "\n")
    out.append("\n")

    out.append("\n# --- shared logic " + "-" * 62 + "\n\n")
    for name in SHARED:
        out.append(ref_f[name].rstrip() + "\n\n\n")

    out.append("# --- variant-specific logic " + "-" * 52 + "\n")
    out.append('''# Kept in both forms rather than merged: these bodies really do differ, and
# picking one would change signals on the strategies that use the other.

''')
    # find_method1: M1-M2 flavour vs the rest
    out.append(re.sub(r"^def find_method1_entry_fast",
                      "def _find_method1_m1m2", m1m2_f["find_method1_entry_fast"],
                      count=1, flags=re.M).rstrip() + "\n\n\n")
    out.append(re.sub(r"^def find_method1_entry_fast",
                      "def _find_method1_std", ref_f["find_method1_entry_fast"],
                      count=1, flags=re.M).rstrip() + "\n\n\n")
    # find_method2: M2-M3-V4 flavour vs the rest
    out.append(re.sub(r"^def find_method2_on_1m",
                      "def _find_method2_v4", ref_f["find_method2_on_1m"],
                      count=1, flags=re.M).rstrip() + "\n\n\n")
    out.append(re.sub(r"^def find_method2_on_1m",
                      "def _find_method2_std", v1_f["find_method2_on_1m"],
                      count=1, flags=re.M).rstrip() + "\n\n\n")
    # base_row: variation 1 vs variation 4
    out.append(re.sub(r"^def base_row_v1",
                      "def _base_row_var1", v1_f["base_row_v1"],
                      count=1, flags=re.M).rstrip() + "\n\n\n")
    out.append(re.sub(r"^def base_row_v1",
                      "def _base_row_var4", ref_f["base_row_v1"],
                      count=1, flags=re.M).rstrip() + "\n\n\n")
    out.append(ref_f["apply_variation_4_logic"].rstrip() + "\n\n\n")

    out.append(WRAPPER)
    open(os.path.join(S17, "s17_core.py"), "w").write("".join(out))
    print("wrote Live/Strategy 17/s17_core.py")


if __name__ == "__main__":
    main()
