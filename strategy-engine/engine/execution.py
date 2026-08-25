"""Turning a signal into a managed position.

The sequence, and why each step is the way it is:

1. **Claim the event.** The fired ledger is the idempotence guard; claiming
   before placing means a crash between claim and fill cannot double-order.
2. **Record the signal** before the order goes out, so a trade exists in the
   database even if the bridge call never returns.
3. **Place with the signal stop as a placeholder.** The real stop cannot be
   computed until the fill price is known, and an order should never be naked.
4. **Find the fill.** Polled, because the position takes a moment to appear.
5. **Correct the stop — always.** From the fill if it was found, from the
   signal price if it was not. Version 1's S21 skipped the correction entirely
   when the fill lookup failed, leaving the placeholder on a live position;
   S17 fell back. This is S17's behaviour.
6. **Register the position.** The ladder derives from the corrected stop, so
   1R is the money actually at risk.

Every failure path writes something: a rejected order records its retcode
against the trade row (Version 1's S21 left such rows in SIGNAL status
forever), and a failed stop correction raises a Telegram alert, because it
leaves a live position carrying the wrong risk and needs a human.
"""

from . import sizing
from .notify import log
from .positions import Position


class Executor:
    def __init__(self, book, bridge, db, telegram, journal, risk_per_trade):
        self.book = book
        self.bridge = bridge
        self.db = db
        self.tg = telegram
        self.journal = journal
        self.risk = risk_per_trade

    def lots_for(self, signal):
        """Broker lots for a signal. `lots` on the signal wins if the strategy
        computed it; otherwise Qty is converted using the contract size."""
        if signal.lots is not None:
            return sizing.round_lots(signal.lots)
        lots = sizing.to_lots(signal.symbol, signal.qty, signal.entry_price)
        if lots is None:
            log(f"[EXEC] {signal.symbol}: no usable qty ({signal.qty!r}) — "
                f"falling back to the minimum lot")
            return sizing.MIN_LOT
        return sizing.round_lots(lots)

    def execute(self, signal):
        """Place and register one signal. Returns the Position, or None."""
        if not self.journal.mark_fired(signal.event_id):
            return None                      # already traded this setup

        lots = self.lots_for(signal)
        self.db.record_signal(signal.event_id, signal.symbol, signal.side,
                              signal_price=signal.entry_price, qty=lots,
                              hard_sl=signal.hard_sl)

        ticket, retcode, comment = self.bridge.place(
            signal.symbol, signal.side, lots, sl=signal.hard_sl or 0.0)

        if ticket <= 0 or retcode != 10009:
            self.db.record_execution(signal.event_id, ticket, retcode,
                                     error=comment)
            self.tg.post(f"⚠️ MT5 ORDER FAILED\nSymbol: {signal.symbol}\n"
                         f"Side: {signal.side.upper()}\nRetcode: {retcode}\n"
                         f"Error: {comment}")
            return None

        fill = self.bridge.find_fill(ticket)
        entry = fill if fill is not None else float(signal.entry_price or 0)
        basis = "fill" if fill is not None else "signal"
        if fill is None:
            log(f"[EXEC] ticket {ticket} never appeared — correcting the stop "
                f"from the signal price instead of leaving the placeholder")

        hard_sl = self._correct_stop(ticket, signal, entry, lots, basis)

        pos = self.book.add(Position(
            ticket=ticket, symbol=signal.symbol, side=signal.side,
            event_id=signal.event_id, entry_price=entry,
            hard_sl=hard_sl, current_sl=hard_sl, qty=lots,
            last_price=entry,
        ))
        self.db.record_execution(signal.event_id, ticket, retcode,
                                 entry_price=entry, qty=lots, hard_sl=hard_sl,
                                 targets=self._ladder_snapshot(pos))
        log(f"[EXEC] registered ticket={ticket} entry={entry} sl={hard_sl} lots={lots}")
        self.tg.post(f"✅ POSITION OPENED\nSymbol: {signal.symbol}\n"
                     f"Side: {signal.side.upper()}\nTicket: {ticket}\n"
                     f"Entry: {entry}\nHard SL: {hard_sl}\nQty: {lots}")
        return pos

    def _correct_stop(self, ticket, signal, entry, lots, basis):
        """Replace the placeholder stop with the exact fixed-risk stop.

        Returns the stop now believed to be on the position — the corrected
        one if the modify succeeded, the placeholder if it did not.
        """
        placeholder = float(signal.hard_sl or 0)
        if entry <= 0 or lots <= 0:
            log(f"[EXEC] cannot correct stop for {ticket}: entry={entry} lots={lots}")
            return placeholder

        exact = sizing.fixed_risk_sl(signal.symbol, signal.side, entry, lots, self.risk)
        if exact is None:
            return placeholder

        if self.bridge.modify_sl(ticket, exact):
            log(f"[EXEC] corrected SL for ticket {ticket} to {exact} "
                f"from {basis} price {entry}")
            return exact

        log(f"[EXEC] SL correction FAILED for ticket {ticket} — "
            f"position keeps the placed SL {placeholder}")
        self.tg.post(f"⚠️ POST-ENTRY SL CORRECTION FAILED\nTicket: {ticket}\n"
                     f"Intended SL: {exact}\nSL on broker: {placeholder}\n"
                     f"Risk on this position is NOT {self.risk} — check it manually.")
        return placeholder

    @staticmethod
    def _ladder_snapshot(pos):
        """Rungs 1:0.5 .. 1:10 recorded with the trade.

        Trailing no longer reads this — rung prices are derived from entry and
        stop, without an upper bound. It is stored because the dashboard and
        the parity checker both display the ladder, and a stored snapshot
        keeps those readable without recomputing.
        """
        from .trailing import target
        rungs = [0.5] + list(range(1, 11))
        out = {}
        for r in rungs:
            t = target(pos, r)
            if t is not None:
                out[r] = round(t, 6)
        return out
