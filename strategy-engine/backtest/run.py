#!/usr/bin/env python3
"""Replay a live strategy over historical candles.

  python -m backtest.run --strategy "Live/Strategy 17/Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live.py" \
      --from 2026-06-01 --to 2026-07-01 --source parquet

Stage 1 (signal replay) sweeps the window and lets the strategy's own
`run_backtest_v1` produce the 1:0.5-1:10 ladder for every setup it finds. It
sweeps in strides rather than one pass: a strategy only ever sees `LOOKBACK_1M`
one-minute bars — about 4.2 days at 6000 — so a single scan of a month-long
window would silently report only its last four days.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):                     # allow direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "backtest"

from . import results
from .fakes.clock import VirtualClock
from .fakes.clock import no_real_sleep
from .feed import CsvSource, DashboardSource, HistoryStoreSource, ParquetArchiveSource
from .feed import scan_strides
from .loader import NetworkSealed, load

UTC = timezone.utc


def _day(s):
    """YYYY-MM-DD, or with a time — execution replay is usually scoped to hours,
    not days, so a date-only window would be unusable for it."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise SystemExit(f"unparseable date {s!r} — use YYYY-MM-DD[ HH:MM]")


def build_source(args):
    if args.source == "parquet":
        root = args.archive_root or os.environ.get("HISTORY_LOCAL_ROOT")
        if not root:
            raise SystemExit("--archive-root or HISTORY_LOCAL_ROOT is required "
                             "for --source parquet")
        return ParquetArchiveSource(root)
    if args.source == "history-store":
        return HistoryStoreSource(orchestrator_path=args.orchestrator_path)
    if args.source == "dashboard":
        return DashboardSource(args.dashboard_url)
    if args.source == "csv":
        paths = {}
        for spec in args.csv or []:
            tf, path = spec.split("=", 1)
            paths[(args.symbol, tf)] = path
        return CsvSource(paths)
    raise SystemExit(f"unknown source {args.source}")


def stage1(strat, date_from, date_to, verbose=True):
    """Strided sweep. Each stride moves simulated now forward and rescans."""
    strides = scan_strides(date_from.timestamp(), date_to.timestamp(),
                           strat.adapter.lookbacks)
    strat.module.load_fired_events()
    strat.recover()
    for i, as_of in enumerate(strides, 1):
        strat.clock.set(as_of)
        if verbose:
            print(f"  [{i}/{len(strides)}] scanning as of {as_of:%Y-%m-%d %H:%M}",
                  flush=True)
        strat.scan()
    return strides


def stage2(strat, date_from, date_to, verbose=True):
    """Bar-by-bar execution replay: scan each simulated minute, trail on the
    strategy's own interval, and settle stops against each bar's range."""
    step = 60
    trail_every = int(getattr(strat.module, "TRAIL_INTERVAL_SEC", 10))
    strat.module.load_fired_events()
    strat.recover()

    t = int(date_from.timestamp())
    end = int(date_to.timestamp())
    since_trail = 0
    bars = 0
    while t <= end:
        strat.clock.set(datetime.fromtimestamp(t, UTC))
        strat.scan()
        strat.broker.settle()
        since_trail += step
        if since_trail >= trail_every:
            strat.trail()
            since_trail = 0
        bars += 1
        if verbose and bars % 240 == 0:
            print(f"  {datetime.fromtimestamp(t, UTC):%Y-%m-%d %H:%M} "
                  f"({bars} bars, {len(strat.broker.open)} open)", flush=True)
        t += step
    return bars


def write_report(strat, outdir, meta):
    os.makedirs(outdir, exist_ok=True)
    td = strat.trade_db

    def _clean(obj):
        if isinstance(obj, set):
            return sorted(obj)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_clean(v) for v in obj]
        return obj

    paths = {}
    for name, payload in (
        ("events.json", _clean(td.events)),
        ("trades.json", _clean(list(td.trades.values()))),
        ("orders.json", _clean(strat.broker.orders)),
        ("positions_closed.json", _clean(strat.broker.closed)),
        ("summary.json", meta),
    ):
        p = os.path.join(outdir, name)
        with open(p, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        paths[name] = p

    tg = os.path.join(outdir, "telegram.log")
    with open(tg, "w") as f:
        f.write("\n\n---\n\n".join(strat.router.telegram))
    paths["telegram.log"] = tg
    return paths


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", required=True, help="path to a Live/ strategy .py")
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    p.add_argument("--stage", choices=["signal", "execution"], default="signal")
    p.add_argument("--source", choices=["parquet", "history-store", "dashboard", "csv"],
                   default="parquet")
    p.add_argument("--archive-root", help="root of history/{symbol}/{tf}/{year}.parquet")
    p.add_argument("--orchestrator-path", help="dir containing history_store.py")
    p.add_argument("--dashboard-url", default="http://127.0.0.1:8600")
    p.add_argument("--csv", action="append",
                   help="TF=path, repeatable (e.g. M1=/data/btc_m1.csv)")
    p.add_argument("--symbol", help="override the symbol (csv source needs it)")
    p.add_argument("--h2-mode", choices=["true-h2", "live-h1"], default="true-h2",
                   help="live-h1 reproduces today's bridge behaviour, where an "
                        "unknown H2 silently returns H1 bars")
    p.add_argument("--spread", type=float, default=0.0)
    p.add_argument("--slippage", type=float, default=0.0)
    p.add_argument("--out", default=None, help="output dir (default: runs/<name>-<stamp>)")
    p.add_argument("--publish", action="store_true",
                   help="write the run to the dashboard's backtests/trades "
                        "tables (opt-in: this is the production database)")
    p.add_argument("--db-url", default=os.environ.get("TRADE_DB_URL"))
    p.add_argument("--label", help="label for the backtests row")
    p.add_argument("--note", help="note for the backtests row")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    date_from, date_to = _day(args.date_from), _day(args.date_to)
    if date_from >= date_to:
        raise SystemExit("--from must be before --to")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = os.path.splitext(os.path.basename(args.strategy))[0]
    outdir = os.path.abspath(args.out or os.path.join("runs", f"{base}-{stamp}"))

    # 1. source first — it captures the real requests before injection
    source = build_source(args)
    clock = VirtualClock(date_to)

    strat = load(args.strategy, clock, source, outdir, h2_mode=args.h2_mode,
                 spread=args.spread, slippage=args.slippage)
    symbol = args.symbol or strat.adapter.symbols[0]

    if not args.quiet:
        print(f"strategy  {base}  ({strat.adapter.name})")
        print(f"symbols   {', '.join(strat.adapter.symbols)}")
        print(f"window    {date_from:%Y-%m-%d} -> {date_to:%Y-%m-%d}  stage={args.stage}")
        print(f"out       {outdir}\n")

    # 6. preload — the last step allowed to touch the network
    for sym in strat.adapter.symbols:
        strat.feed.preload(sym, strat.adapter.wire, date_from.timestamp(),
                           date_to.timestamp())
        for tf in strat.adapter.wire:
            lo, hi, n = strat.feed.coverage(sym, tf)
            if not args.quiet:
                span = f"{lo:%Y-%m-%d} -> {hi:%Y-%m-%d}" if n else "EMPTY"
                print(f"  {sym:<8} {tf:<4} {n:>8,} bars  {span}")
    for w in strat.feed.warnings:
        print(f"  WARNING {w}")
    if not args.quiet:
        print()

    empty =[(s, tf) for s in strat.adapter.symbols for tf in strat.adapter.wire
             if strat.feed.coverage(s, tf)[2] == 0]
    if empty:
        # Fail rather than produce a clean-looking report over no data — an
        # empty frame makes the strategy return zero setups, which is
        # indistinguishable from a genuine no-trade period.
        raise SystemExit(f"no candles for {empty} — seed the archive first "
                         f"(scripts/fetch_history.py) or widen the window")

    # 7. sealed from here
    with strat.activate(), NetworkSealed(), no_real_sleep(clock):
        if args.stage == "signal":
            strides = stage1(strat, date_from, date_to, verbose=not args.quiet)
            steps = len(strides)
        else:
            steps = stage2(strat, date_from, date_to, verbose=not args.quiet)

    td = strat.trade_db
    setups = results.summarise(outdir)
    meta = {
        "strategy": base,
        "family": strat.adapter.name,
        "symbols": strat.adapter.symbols,
        "symbol": symbol,
        "stage": args.stage,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "h2_mode": args.h2_mode,
        "steps": steps,
        "setups": setups,
        "signals": sum(1 for e in td.events if e["kind"] == "SIGNAL"),
        "executions": sum(1 for e in td.events if e["kind"] == "EXECUTION"),
        "accepted": sum(1 for e in td.events
                        if e["kind"] == "EXECUTION" and e.get("accepted")),
        "trail_moves": sum(1 for e in td.events if e["kind"] == "TRAIL_MOVE"),
        "closes": sum(1 for e in td.events if e["kind"] == "CLOSE"),
        "telegram_messages": len(strat.router.telegram),
        "unmatched_routes": strat.router.unmatched,
        "feed_warnings": strat.feed.warnings,
        "coverage": {f"{s}/{tf}": strat.feed.coverage(s, tf)[2]
                     for s in strat.adapter.symbols for tf in strat.adapter.wire},
    }

    if args.publish:
        if not args.db_url:
            raise SystemExit("--publish needs --db-url or TRADE_DB_URL")
        from .sink import db as db_sink
        backtest_id, scoped, written = db_sink.publish(
            args.db_url, meta, td.events, list(td.trades.values()),
            label=args.label, note=args.note)
        meta["published"] = {"backtest_id": backtest_id,
                             "strategy_id": scoped, "trades": written}
        if not args.quiet:
            print(f"\npublished backtest #{backtest_id} as {scoped} "
                  f"({written} trades)")

    paths = write_report(strat, outdir, meta)

    if not args.quiet:
        print(f"\nsetups  {setups['setups']}  ({setups['intrade']} in-trade)")
        for status, n in sorted(setups["statuses"].items(), key=lambda kv: -kv[1]):
            print(f"          {n:>4}  {status[:78]}")
        print(f"\nsignals {meta['signals']}   executions {meta['executions']} "
              f"({meta['accepted']} accepted)   trails {meta['trail_moves']}   "
              f"closes {meta['closes']}")
        if args.stage == "signal" and setups["setups"] and not meta["signals"]:
            # Expected, not a failure: a setup only reaches the execution path
            # when its entry lands in the last RECENT_1M_COUNT minute bars of a
            # scan. Say so, or the zero reads as a broken harness.
            print("          (stage=signal produces the ratio ladder per setup; "
                  "execution needs --stage execution)")
        if strat.router.unmatched:
            print(f"WARNING unmatched routes: {strat.router.unmatched}")
        print(f"report  {paths['summary.json']}")
    return meta


if __name__ == "__main__":
    main()
