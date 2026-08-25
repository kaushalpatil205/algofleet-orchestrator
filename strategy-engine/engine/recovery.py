"""Re-adopting open positions after a restart.

A strategy restart must never orphan a live position: orders carry no take
profit, so an un-managed position runs to whatever stop was last written and
nothing trails it. On startup the strategy reloads its OPEN rows, checks them
against what the broker actually holds, and resumes managing the survivors.

Rows whose ticket is gone are closed rather than adopted — the position was
taken out while the strategy was down.

One asymmetry worth knowing: Version 1 stored a precomputed target ladder and
rebuilt trailing from it. Version 2 derives the ladder from entry and stop
(see engine/trailing.py), so a recovered position's rungs come from the same
two numbers the broker holds. For S17 rows that is identical to Version 1. For
S21 rows it is the intended correction — Version 1 built S21's ladder from the
raw signal stop while the broker carried the corrected one.
"""

from .notify import log
from .positions import Position


def recover(book, db, bridge, telegram):
    """Re-adopt this strategy's open positions. Returns the count adopted."""
    if not db.enabled():
        log("[RECOVERY] persistence disabled — nothing to recover")
        return 0
    saved = db.load_open_trades()
    if not saved:
        return 0

    live = bridge.positions()
    open_tickets = None
    if live is None:
        log("[RECOVERY] /positions unreachable — resuming every DB row")
    else:
        open_tickets = set()
        for p in live:
            try:
                open_tickets.add(int(p.get("ticket", 0)))
            except (TypeError, ValueError):
                continue

    recovered = 0
    for row in saved:
        try:
            ticket = int(row["ticket"])
        except (KeyError, TypeError, ValueError):
            continue

        if open_tickets is not None and ticket not in open_tickets:
            log(f"[RECOVERY] ticket={ticket} closed while offline → marking CLOSED")
            db.record_close(ticket, reason="closed_while_offline",
                            exit_price=bridge.deal_price(ticket))
            continue

        entry = float(row.get("entry_price") or 0)
        hard_sl = float(row.get("hard_sl") or 0)
        current_sl = float(row.get("current_sl") or hard_sl or 0)
        pos = Position(
            ticket=ticket,
            symbol=row.get("symbol"),
            side=row.get("side"),
            event_id=row.get("event_id"),
            entry_price=entry,
            hard_sl=hard_sl,
            current_sl=current_sl,
            qty=float(row.get("qty") or 0),
            trail_hit={int(h) for h in (row.get("trail_hit") or set())
                       if float(h) == int(float(h))},
        )
        if not pos.r_unit:
            # entry == stop: the ladder cannot be built, so trailing would be
            # a silent no-op. Adopt it anyway so the position is still reaped
            # when it closes, but say so.
            log(f"[RECOVERY] ticket={ticket} has entry == stop ({entry}) — "
                f"adopted for close detection, but it will not trail")
        book.add(pos)
        db.record_recovery_event(ticket, "resumed trailing after restart")
        recovered += 1
        log(f"[RECOVERY] resumed ticket={ticket} entry={entry} "
            f"sl={current_sl} rungs_hit={sorted(pos.trail_hit)}")

    if recovered:
        telegram.post(f"♻️ RECOVERY: resumed trailing for {recovered} "
                      f"open position(s) after restart")
    return recovered
