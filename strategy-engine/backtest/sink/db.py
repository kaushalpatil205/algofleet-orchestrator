"""Publish a replay to the dashboard's tables.

Writes one `backtests` row plus `trades` rows carrying `source='backtest'` and
`backtest_id`, so a run shows up on /backtests and plots on /charts next to live
trades with no importer changes.

Two constraints shape this:

*`trades` is UNIQUE (strategy_id, event_id)*, and `event_id` is
`sha256(f"{side}|{fcc_ts}")[:24]` — deterministic from the setup's own
timestamp. Replaying a period that traded live therefore regenerates the exact
event_ids already in the table. Reusing the live `strategy_id` would collide
with real rows, so every run writes under `"<strategy_id>#bt<id>"`. That is also
what keeps two runs of the same window from overwriting each other.

*This is the production database.* Nothing here updates or deletes a row it did
not insert, and publishing is opt-in behind `--publish`.
"""

import json


def _connect(db_url):
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(db_url, connect_timeout=10)
    conn.autocommit = False
    return conn


def _num(x):
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return None if v != v else v          # drop NaN
    except (TypeError, ValueError):
        return None


def publish(db_url, meta, events, trades, label=None, note=None):
    """Insert the run. Returns (backtest_id, scoped_strategy_id, rows written)."""
    strategy_id = _strategy_id_of(events) or meta.get("strategy", "unknown")
    symbol = meta.get("symbol")
    timeframes = sorted({tf.split("/")[-1]
                         for tf in (meta.get("coverage") or {})})

    rows = [t for t in trades if t.get("event_id")]
    net = sum(_num(t.get("pnl")) or 0.0 for t in rows)

    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO backtests (strategy_id, label, symbol, note,
                                          source_name, trade_count, timeframes,
                                          net_pnl)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (strategy_id,
                 label or f"{meta.get('stage')} {meta.get('date_from')}"
                          f"..{meta.get('date_to')}",
                 symbol, note, meta.get("strategy"), len(rows),
                 json.dumps(timeframes), net))
            backtest_id = cur.fetchone()[0]

            scoped = f"{strategy_id}#bt{backtest_id}"
            written = 0
            trade_ids = {}
            for t in rows:
                status = t.get("status") or "SIGNAL"
                cur.execute(
                    """INSERT INTO trades (strategy_id, event_id, symbol, side,
                            magic, status, ticket, signal_price, entry_price, qty,
                            hard_sl, current_sl, targets, trail_hits, retcode,
                            error, pnl, close_reason, source, timeframe,
                            backtest_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,'backtest',%s,%s)
                       RETURNING id""",
                    (scoped, t["event_id"], t.get("symbol") or symbol,
                     t.get("side") or "sell", meta.get("magic"), status,
                     t.get("ticket"), _num(t.get("signal_price")),
                     _num(t.get("entry_price")), _num(t.get("qty")),
                     _num(t.get("hard_sl")), _num(t.get("current_sl")),
                     json.dumps(_jsonable(t.get("targets") or {})),
                     json.dumps(sorted(t.get("trail_hit") or [])),
                     t.get("retcode"), t.get("error"), _num(t.get("pnl")),
                     t.get("close_reason"),
                     meta.get("primary_timeframe"), backtest_id))
                trade_ids[t["event_id"]] = cur.fetchone()[0]
                written += 1

            for e in events:
                etype = e.get("event_type")
                tid = trade_ids.get(e.get("event_id"))
                if not etype or tid is None:
                    continue
                payload = {k: v for k, v in e.items()
                           if k not in ("seq", "kind", "event_type", "event_id")}
                cur.execute(
                    """INSERT INTO trade_events (trade_id, event_type, payload)
                       VALUES (%s, %s, %s)""",
                    (tid, etype, json.dumps(_jsonable(payload))))
        conn.commit()
        return backtest_id, scoped, written
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _strategy_id_of(events):
    for e in events:
        if e.get("kind") == "INIT":
            return e.get("strategy_id")
    return None


def _jsonable(obj):
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    return obj
