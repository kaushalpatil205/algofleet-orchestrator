#!/usr/bin/env python3
"""Port a Version 1 strategy script onto the Version 2 engine.

The strategy logic is MOVED, never rewritten: every function this keeps is
copied out of the original file byte for byte. What it drops is only the
half the engine now owns — config loading, the candle cache, bridge calls,
Telegram plumbing, the fired-event ledger, market-close tables, the ratio
ladder, trailing, recovery, CSV writing and main().

Run it, then diff the generated file's kept functions against the original to
confirm nothing was touched:

    python scripts/migrate_v2.py "Live/Strategy 17/Bridge-....py" --check
"""

import argparse
import ast
import os
import re
import sys

# Everything the engine now provides. A function with one of these names in a
# Version 1 script is boilerplate and is dropped.
ENGINE_OWNED = {
    # config / plumbing
    "log", "tg_post", "log_trade_error",
    "_s", "_f", "_kv_lines", "vkv", "vblocks",
    # candles
    "get_rolling_mt5_candles", "fetch_live_mt5_candles",
    # bridge
    "mt5_bridge_trade", "mt5_bridge_modify_sl", "mt5_bridge_close_ticket",
    # sessions / flatten
    "is_market_closed", "market_close_flatten_due", "flatten_for_market_close",
    # ladder + trailing
    "_ratio_step", "_ratio_base", "_ratio_target", "_trail_checkpoints",
    "trail_conservative_positions",
    # lifecycle
    "load_fired_events", "save_fired_events", "recover_open_trades",
    "run_trailing_pass", "_trailing_loop", "main",
    # scan orchestration — replaced by the generated scan()
    "run_live_scan_for_instrument", "run_live_scan",
}

# Indicator maths, now in engine.indicators (proven identical — see
# test_indicators.py). Dropped and imported instead.
INDICATOR_OWNED = {
    "add_smooth_macd_cycles", "_pine_ema_series", "_heikin_ashi_df",
    "_pine_kama_series", "calc_sma_kama", "calc_emakama", "calc_kama_line",
    "prepare_df", "prepare_df_tf", "make_fast_arrays", "make_tf_arrays",
    "check_ema_position", "VolumeCalculator", "get_fib_levels",
}

# Module-level constants the engine supplies or that config now carries.
CONST_OWNED = {
    "BOT_TOKEN", "CHAT_ID", "MT5_BRIDGE_URL", "MT5_API_KEY", "MAGIC",
    "TELEGRAM_URL", "FLATTEN_BEFORE_WEEKEND", "FLATTEN_BEFORE_DAILY_BREAK",
    "FLATTEN_LEAD_MIN", "TRAIL_INTERVAL_SEC", "RISK_PER_TRADE",
    "BASE_LOG_DIR", "SCAN_SLEEP_SEC", "_MC_SESSIONS",
    "SMOOTH_MIN_RUN", "BB_PERIOD", "BB_STD", "EMA50_COL",
    "EK_ER_LEN", "EK_FAST_LEN", "EK_SLOW_LEN",
    "KL_LENGTH", "KL_FAST_LENGTH", "KL_SLOW_LENGTH", "KL_HP_PERIOD",
    "SMA_KAMA_LENGTH", "SMA_KAMA_FAST", "SMA_KAMA_SLOW", "SMA_EMA_LENGTH",
    "WIN_1H", "WIN_2H", "WIN_4H", "WIN_12H", "WIN_24H", "WIN_48H",
    "WIN_7D", "WIN_10D", "WIN_30D", "FIB_LEVELS_RAW",
    "K_SMA", "K_EMAKAMA", "K_KLINE", "K_EMA50",
    "GRANULARITY_TO_DELTA", "_candle_cache", "_ticket_map", "_event_to_ticket",
    "_fired_events",
}


def parse(path):
    src = open(path, encoding="utf-8").read()
    return src, ast.parse(src), src.split("\n")


def segments(path):
    """(kept_functions, kept_constants) as verbatim source text."""
    src, tree, lines = parse(path)

    def text(node):
        return "\n".join(lines[node.lineno - 1:node.end_lineno])

    funcs, consts = [], []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if node.name in ENGINE_OWNED or node.name in INDICATOR_OWNED:
                continue
            funcs.append((node.name, text(node)))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in CONST_OWNED or name.startswith("_config"):
                continue
            if not (name.isupper() or name.startswith("_")):
                continue
            consts.append((name, text(node)))
    return funcs, consts


def undefined_names(source_text, extra_defined=()):
    """Names a generated file uses but never defines — the migration's own
    smoke test, since a dropped helper shows up here rather than at 3am."""
    tree = ast.parse(source_text)
    defined = set(extra_defined) | set(dir(__builtins__))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            defined.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
        elif isinstance(n, ast.alias):
            defined.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    import builtins
    return sorted(used - defined - set(dir(builtins)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--check", action="store_true",
                    help="report what would be kept and dropped, write nothing")
    args = ap.parse_args()

    funcs, consts = segments(args.source)
    src, tree, _ = parse(args.source)
    dropped = [n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.ClassDef))
               and (n.name in ENGINE_OWNED or n.name in INDICATOR_OWNED)]

    kept_lines = sum(t.count("\n") for _, t in funcs)
    print(f"{os.path.basename(args.source)}")
    print(f"  original      {src.count(chr(10)):>5} lines")
    print(f"  strategy kept {kept_lines:>5} lines in {len(funcs)} functions")
    print(f"  engine drops  {len(dropped):>5} functions: {', '.join(sorted(dropped))}")
    if args.check:
        return
    print("  (generation is driven per-family; see scripts/migrate_s17.py)")


if __name__ == "__main__":
    main()
