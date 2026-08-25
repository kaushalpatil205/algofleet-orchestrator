"""One-shot trail-state reconciliation — companion to TRAIL_SL_FIX.md.

Rebuilds trail_hits and current_sl for EVERY open trade (all strategies, all
symbols) from the trade's *executed* TRAIL_MOVE events only, discarding state
left behind by modifies MT5 rejected (executed=false). Idempotent — safe to
re-run any time; it only touches rows whose stored state disagrees with the
executed-event history.

Run from the directory containing trade_db.py:

    python3 repair_trail_state.py            # report + apply
    python3 repair_trail_state.py --dry-run  # report only
"""

import sys

import trade_db

DRY_RUN = "--dry-run" in sys.argv

# Stored state vs. state derived from executed TRAIL_MOVE events.
# COALESCE(executed, true): a legacy event without the flag counts as executed.
MISMATCH_SQL = """
WITH derived AS (
    SELECT t.id,
           COALESCE(jsonb_agg(DISTINCT (e.payload -> 'ratio'))
                    FILTER (WHERE e.id IS NOT NULL), '[]'::jsonb) AS hits,
           (ARRAY_AGG((e.payload ->> 'new_sl')::float8 ORDER BY e.id DESC)
                    FILTER (WHERE e.id IS NOT NULL))[1]           AS last_sl
    FROM trades t
    LEFT JOIN trade_events e
      ON e.trade_id = t.id
     AND e.event_type = 'TRAIL_MOVE'
     AND COALESCE((e.payload ->> 'executed')::boolean, TRUE) IS TRUE
    WHERE t.status = 'OPEN'
    GROUP BY t.id
)
SELECT t.id, t.strategy_id, t.symbol, t.ticket,
       t.trail_hits AS stored_hits,   d.hits    AS executed_hits,
       t.current_sl AS stored_sl,     COALESCE(d.last_sl, t.hard_sl) AS true_sl
FROM trades t JOIN derived d ON d.id = t.id
WHERE t.status = 'OPEN'
  AND (t.trail_hits IS DISTINCT FROM d.hits
       OR t.current_sl IS DISTINCT FROM COALESCE(d.last_sl, t.hard_sl))
ORDER BY t.id
"""

FIX_SQL = """
UPDATE trades t
SET trail_hits = %s::jsonb, current_sl = %s, updated_at = now()
WHERE t.id = %s AND t.status = 'OPEN'
"""


def main():
    trade_db.init("TRAIL-REPAIR")
    if not trade_db.enabled():
        print("DB not reachable — aborting")
        return 1
    rows = trade_db._exec(MISMATCH_SQL, fetch="all") or []
    if not rows:
        print("Nothing to repair — every open trade already matches its "
              "executed trail history.")
    for r in rows:
        print(f"trade {r['id']} {r['strategy_id']} {r['symbol']} #{r['ticket']}: "
              f"trail_hits {r['stored_hits']} -> {r['executed_hits']} · "
              f"current_sl {r['stored_sl']} -> {r['true_sl']}")
        if not DRY_RUN:
            trade_db._exec(FIX_SQL, (trade_db.json.dumps(r["executed_hits"]),
                                     r["true_sl"], r["id"]))
    print(f"{'Would repair' if DRY_RUN else 'Repaired'} {len(rows)} trade(s).")
    # the repair run itself must not linger in the strategy registry
    trade_db._exec("DELETE FROM strategy_registry WHERE strategy_id = %s",
                   ("TRAIL-REPAIR",))
    return 0


if __name__ == "__main__":
    sys.exit(main())
