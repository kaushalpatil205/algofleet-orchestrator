#!/usr/bin/env python3
"""Diff a replay against what the live system actually did.

This is the test that makes the rest of the harness worth trusting. Everything
else proves the plumbing runs; this proves the plumbing reproduces reality.

It works because `event_id` is deterministic — `sha256(f"{side}|{fcc_ts}")[:24]`
off the setup's own final-candle timestamp — so replaying a period that traded
live regenerates exactly the ids already in `trades`. The id set is therefore a
direct join, and a setup the replay missed (or invented) shows up immediately.

Read-only against the live database.

  python -m backtest.parity --replay runs/S17-...-20260805 \\
      --strategy-id S17-M3M2-V1-BTCUSDT-SELL --from 2026-06-01 --to 2026-07-01

Comparison is split by how much agreement each field deserves:

  signal_price, hard_sl  computed from candles — should match tightly
  qty                    derived from those two — should match tightly
  entry_price            the live broker's actual fill vs a simulated one;
                         divergence here is expected and reported, not failed
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "backtest"

from . import results

UTC = timezone.utc

TIGHT = ("signal_price", "hard_sl", "qty")
LOOSE = ("entry_price",)


def load_live(db_url, strategy_id, date_from, date_to):
    """Live rows for this strategy in the window. Never writes."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(db_url, connect_timeout=10)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT event_id, symbol, side, status, ticket, signal_price,
                          entry_price, qty, hard_sl, current_sl, pnl,
                          close_reason, created_at, opened_at
                     FROM trades
                    WHERE strategy_id = %s
                      AND source = 'live'
                      AND created_at >= %s AND created_at < %s
                    ORDER BY created_at""",
                (strategy_id, date_from, date_to))
            return {r["event_id"]: dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


def load_replay(rundir):
    """Every event_id the replay produced.

    Unions two sources on purpose. `trades.json` holds what reached execution,
    which in a Stage 1 sweep is almost nothing — the strategy only trades a
    setup whose entry lands in the last few minute bars of a scan. The CSV holds
    every setup it found, which is the set worth comparing.
    """
    out = {}

    csv_rows = results.load_rows(rundir)
    if not csv_rows.empty and "Event ID" in csv_rows.columns:
        for r in csv_rows.to_dict("records"):
            ev = r.get("Event ID")
            if not ev or str(ev) == "nan":
                continue
            out[str(ev)] = {
                "event_id": str(ev),
                "status": r.get("Status"),
                "signal_price": r.get("Entry Price"),
                "hard_sl": r.get("Hard SL Price"),
                "qty": r.get("Trading qty Contract"),
                "entry_datetime": r.get("Entry Datetime"),
                "source": "csv",
            }

    path = os.path.join(rundir, "trades.json")
    if os.path.exists(path):
        with open(path) as f:
            for t in json.load(f):
                ev = t.get("event_id")
                if not ev:
                    continue
                row = out.setdefault(str(ev), {"event_id": str(ev)})
                row.update({k: v for k, v in t.items() if v is not None})
                row["source"] = "csv+trades" if row.get("source") else "trades"
    return out


def _num(x):
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def compare(live, replay, rel_tol=1e-6):
    both = sorted(set(live) & set(replay))
    diffs = []
    for ev in both:
        lv, rv = live[ev], replay[ev]
        fields = {}
        for name in TIGHT + LOOSE:
            a, b = _num(lv.get(name)), _num(rv.get(name))
            if a is None or b is None:
                continue
            denom = max(abs(a), abs(b), 1e-12)
            rel = abs(a - b) / denom
            if rel > rel_tol:
                fields[name] = {"live": a, "replay": b, "rel": rel,
                                "tight": name in TIGHT}
        if fields:
            diffs.append({"event_id": ev, "fields": fields})

    tight_breaks = [d for d in diffs
                    if any(f["tight"] for f in d["fields"].values())]
    return {
        "live_only": sorted(set(live) - set(replay)),
        "replay_only": sorted(set(replay) - set(live)),
        "matched": len(both),
        "diffs": diffs,
        "tight_breaks": len(tight_breaks),
        "verdict": _verdict(live, replay, both, tight_breaks),
    }


def _verdict(live, replay, both, tight_breaks):
    if not live:
        return "NO LIVE DATA — nothing to compare against in this window"
    missed = set(live) - set(replay)
    if missed:
        return (f"FAIL — the replay missed {len(missed)} setup(s) the live "
                f"system found")
    if tight_breaks:
        return (f"FAIL — {len(tight_breaks)} matched setup(s) disagree on a "
                f"candle-derived field")
    return f"PASS — all {len(both)} live setup(s) reproduced"


def report(result, verbose=False):
    print(f"matched      {result['matched']}")
    print(f"live only    {len(result['live_only'])}")
    print(f"replay only  {len(result['replay_only'])}")
    print(f"differing    {len(result['diffs'])} "
          f"({result['tight_breaks']} on candle-derived fields)")
    print(f"\n{result['verdict']}")

    if result["live_only"]:
        print("\nsetups the live system found and the replay did not:")
        for ev in result["live_only"][:20]:
            print(f"  {ev}")

    if verbose:
        for d in result["diffs"][:40]:
            print(f"\n{d['event_id']}")
            for name, f in d["fields"].items():
                flag = "!" if f["tight"] else " "
                print(f"  {flag} {name:<14} live={f['live']:<16.6g} "
                      f"replay={f['replay']:<16.6g} rel={f['rel']:.2e}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--replay", required=True, help="a run directory")
    p.add_argument("--strategy-id", required=True,
                   help="the live strategy_id, e.g. S17-M3M2-V1-BTCUSDT-SELL")
    p.add_argument("--from", dest="date_from", required=True)
    p.add_argument("--to", dest="date_to", required=True)
    p.add_argument("--db-url", default=os.environ.get("TRADE_DB_URL"))
    p.add_argument("--rel-tol", type=float, default=1e-6)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if not args.db_url:
        raise SystemExit("--db-url or TRADE_DB_URL is required")

    lo = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=UTC)
    hi = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=UTC)

    live = load_live(args.db_url, args.strategy_id, lo, hi)
    replay = load_replay(args.replay)
    print(f"live rows    {len(live)}   replay setups {len(replay)}\n")

    result = compare(live, replay, rel_tol=args.rel_tol)
    report(result, verbose=args.verbose)

    with open(os.path.join(args.replay, "parity.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    return 0 if result["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
