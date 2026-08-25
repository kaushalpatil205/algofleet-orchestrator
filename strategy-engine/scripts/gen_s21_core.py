#!/usr/bin/env python3
"""Generate Live/Strategy 21/s21_core.py from the Version 1 script.

Same rule as Strategy 17: the logic is moved, not rewritten. The one edit is
to run_strategy21, which had the order placement, the post-entry stop
correction and the Telegram exit alerts inlined in the middle of its row loop.
That block is cut out — the engine does it now — and the loop keeps only the
part that produces rows. The cut is done by matching exact source lines so it
fails loudly if the original changes underneath it.
"""
import ast
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S21DIR = os.path.join(HERE, "Live", "Strategy 21")
SRC = os.path.join(S21DIR, "Bridge-S21-1_10-Ratios-BTCUSD-Live.py")

KEEP = ["prepare_15m_data", "prepare_3m_data", "prepare_1m_data",
        "scan_15m_setups", "compute_hard_sl_on_tf", "track_stage2_exit",
        "compute_soft_sl", "_tgt", "run_backtest_engine", "create_empty_row",
        "format_telegram", "format_telegram_exit", "process_telegram_exits",
        "run_strategy21"]

CONSTS = ["COIN_NAME", "SYMBOL", "SYMBOL_BRIDGE", "LOOKBACK_15M", "LOOKBACK_3M",
          "LOOKBACK_1M", "ALL_RATIOS", "RATIOS_FULL", "RECENT_1M_COUNT"]

# The execution block, cut verbatim. Both markers must be present.
CUT_FROM = "        is_new_entry = entry_dt in recent_1m\n"
CUT_TO = "            process_telegram_exits(row, side, recent_1m)\n"

REPLACEMENT = '''        # Version 1 placed the order, corrected the stop and posted the exit
        # alerts right here, in the middle of building a row. The engine owns
        # all of that now, so the loop keeps only what it is for: producing the
        # row. What the order needs is stashed for the caller to turn into a
        # Signal.
        row["_entry_dt"] = entry_dt
        row["_entry_price"] = entry_price
        row["_hard_sl"] = hard_sl_price
'''


WRAPPER = '''# --- the engine wrapper -------------------------------------------------------

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
'''


def main():
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")
    fns, consts = {}, {}
    for n in tree.body:
        t = "\n".join(lines[n.lineno - 1:n.end_lineno])
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            fns[n.name] = t
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name):
            consts[n.targets[0].id] = t

    body = fns["run_strategy21"] + "\n"
    if CUT_FROM not in body or CUT_TO not in body:
        raise SystemExit("run_strategy21 no longer matches the expected shape — "
                         "the execution block markers were not found")
    start = body.index(CUT_FROM)
    end = body.index(CUT_TO) + len(CUT_TO)
    cut_lines = body[start:end].count("\n")
    fns["run_strategy21"] = body[:start] + REPLACEMENT + body[end:]
    print(f"  cut {cut_lines} lines of execution out of run_strategy21")

    out = ['''"""Strategy 21 — the live logic, on the Version 2 engine.

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

''']

    out.append("# --- constants, verbatim from Version 1 " + "-" * 40 + "\n")
    for c in CONSTS:
        if c in consts:
            out.append(consts[c] + "\n")
    out.append('''

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


''')

    out.append("# --- strategy logic " + "-" * 60 + "\n\n")
    for name in KEEP:
        out.append(fns[name].rstrip() + "\n\n\n")

    out.append(WRAPPER)
    open(os.path.join(S21DIR, "s21_core.py"), "w").write("".join(out))
    print("wrote Live/Strategy 21/s21_core.py")


if __name__ == "__main__":
    main()
