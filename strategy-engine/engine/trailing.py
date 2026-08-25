"""Ratio ladder, indefinite trailing, and the pre-close flatten.

THE LADDER. One R is the distance between the actual fill and the corrected
fixed-risk stop that sits on the position. Target 1:r is `entry + dir*r*R`.
The stop trails two rungs behind the highest rung price has reached: 1:2 puts
it at breakeven, 1:3 at 1:1, 1:4 at 1:2, and so on.

INDEFINITE BY CONSTRUCTION. Version 1 precomputed targets to 1:10 and stored
them; a runner past 1:10 kept a frozen stop two rungs back and handed the rest
of the move to the market. Rung prices are computed from `entry` and `R` here
instead of looked up, so there is no last rung to fall off.

WHICH SL DEFINES R. The two Version 1 engines disagreed. S17 built the ladder
from the corrected stop; S21 built it from the raw signal stop while the
broker held the corrected one, so its rungs did not correspond to the money
actually at risk. Version 2 uses the corrected stop everywhere — 1R means one
unit of real risk. This changes S21's rung prices relative to Version 1, which
is the intended fix, not a regression.

THE GUARDS. Both come from the 2026-07-12 USOIL incident (TRAIL_SL_FIX.md):
a degenerate candle at the session boundary fired every rung at once, and a
cross-symbol cache hit evaluated one symbol's position against another's
price. Extremes further than 20% from the close are discarded; a price more
than 50% away from a position's own open price means the frame belongs to a
different symbol and the position is skipped.
"""

from .notify import log

EXTREME_TOLERANCE = 0.20     # candle high/low sanity vs close
SYMBOL_TOLERANCE = 0.50      # price sanity vs the position's own open
TRAIL_LAG = 2                # rungs the stop trails behind price
FIRST_RUNG = 2               # 1:2 is the first move (to breakeven)


def target(pos, rung):
    """Price of the 1:`rung` target for a position."""
    r = pos.r_unit
    if not r:
        return None
    return float(pos.entry_price) + pos.direction * float(rung) * r


def reached_rung(pos, price):
    """Highest whole rung `price` has reached, in the position's favour."""
    r = pos.r_unit
    if not r or price is None:
        return 0
    progress = (float(price) - float(pos.entry_price)) * pos.direction
    return int(progress / r + 1e-9)


def rungs_to_check(pos, price):
    """Rungs 1:2 .. 1:N for the N price has reached. Unbounded above."""
    return range(FIRST_RUNG, reached_rung(pos, price) + 1)


def anchor_for(pos, rung):
    """Where the stop goes when `rung` is hit — two rungs back, breakeven
    at the first."""
    if rung <= FIRST_RUNG:
        return float(pos.entry_price)
    return target(pos, rung - TRAIL_LAG)


def improves(side, new_sl, current_sl):
    """A stop only ever moves in the position's favour."""
    if not current_sl or current_sl <= 0:
        return True
    return new_sl < current_sl if side == "sell" else new_sl > current_sl


def sane_extreme(value, close):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v > 0 and abs(v - close) / close <= EXTREME_TOLERANCE


class StopManager:
    """Owns everything the stop-management thread does to open positions."""

    def __init__(self, book, bridge, db, telegram, config):
        self.book = book
        self.bridge = bridge
        self.db = db
        self.tg = telegram
        self.flatten_lead_min = config["flatten_lead_min"]
        self.flatten_weekend = config["flatten_before_weekend"]
        self.flatten_daily = config["flatten_before_daily_break"]

    # --- trailing ---------------------------------------------------------

    def trail(self, positions, symbol, close_px, low_px=None, high_px=None):
        """One trailing pass over the live positions for one symbol."""
        if not positions or not close_px or close_px <= 0:
            return

        if low_px is not None and not sane_extreme(low_px, close_px):
            log(f"[TRAIL] ignoring bogus candle low={low_px} (close={close_px})")
            low_px = None
        if high_px is not None and not sane_extreme(high_px, close_px):
            log(f"[TRAIL] ignoring bogus candle high={high_px} (close={close_px})")
            high_px = None

        for raw in positions:
            try:
                ticket = int(raw.get("ticket", 0))
                broker_magic = int(raw.get("magic", 0))
            except (TypeError, ValueError):
                continue
            pos = self.book.get(ticket)
            if pos is None or pos.symbol != symbol or ticket <= 0:
                continue
            if broker_magic != self.bridge.magic:
                continue

            open_px = float(raw.get("price_open") or 0)
            if open_px > 0 and abs(close_px - open_px) / open_px > SYMBOL_TOLERANCE:
                log(f"[TRAIL] price sanity: close={close_px} vs open={open_px} "
                    f"— skipping ticket={ticket}")
                continue

            pos.last_price = close_px
            broker_sl = float(raw.get("sl", 0) or 0)
            # the broker's stop is the truth; the local copy can lag a
            # manual change made in the terminal
            current_sl = broker_sl or pos.current_sl
            hit_px = (low_px if pos.side == "sell" else high_px)
            if hit_px is None:
                hit_px = close_px

            for rung in rungs_to_check(pos, hit_px):
                if rung in pos.trail_hit:
                    continue
                tgt = target(pos, rung)
                if tgt is None:
                    continue
                new_sl = anchor_for(pos, rung)
                if new_sl is None:
                    continue
                new_sl = round(float(new_sl), 6)
                if not improves(pos.side, new_sl, current_sl):
                    pos.trail_hit.add(rung)     # rung passed, stop already better
                    continue

                pos.trail_hit.add(rung)
                log(f"[TRAIL] ticket={ticket} rung=1:{rung} -> sl={new_sl} "
                    f"(target {tgt}, price {close_px})")
                if self.bridge.modify_sl(ticket, new_sl):
                    current_sl = new_sl
                    pos.current_sl = new_sl
                    self.db.record_trail(ticket, new_sl, rung, executed=True)
                    self.tg.post(f"📐 SL TRAILED\nTicket: {ticket}\nNew SL: {new_sl}\n"
                                 f"Anchor: 1:{rung} target hit at {tgt}\n"
                                 f"Current price: {close_px}")
                else:
                    # broker refused: un-mark so a later candle retries, and
                    # do not claim a move that never happened
                    pos.trail_hit.discard(rung)
                    self.db.record_trail(ticket, new_sl, rung, executed=False)
                    log(f"[TRAIL] ticket={ticket} rung=1:{rung} REFUSED — will retry")
                    break

            pos.current_sl = current_sl

    # --- flatten ----------------------------------------------------------

    def flatten_if_due(self, positions, symbol):
        """Close this strategy's positions in the window before a session
        close. Returns the tickets closed, which must skip trailing."""
        from .sessions import flatten_due

        due, why = flatten_due(symbol, self.flatten_lead_min,
                               self.flatten_weekend, self.flatten_daily)
        if not due:
            return set()

        closed = set()
        for raw in positions:
            try:
                ticket = int(raw.get("ticket", 0) or 0)
                broker_magic = int(raw.get("magic", 0) or 0)
            except (TypeError, ValueError):
                continue
            pos = self.book.get(ticket)
            if pos is None or pos.symbol != symbol or ticket <= 0:
                continue
            if broker_magic != self.bridge.magic:
                continue
            if not self.bridge.close(ticket):
                log(f"[FLATTEN] close FAILED ticket={ticket} ({why}) — retry next pass")
                continue
            pnl = float(raw.get("profit") or 0)
            exit_px = raw.get("price_current")
            log(f"[FLATTEN] closed ticket={ticket} ({why}) pnl={pnl}")
            self.db.record_close(ticket, reason=f"flatten_{why}", pnl=pnl,
                                 exit_price=float(exit_px) if exit_px else None)
            pos.mark_closed()
            self.tg.post(f"🛑 MARKET-CLOSE FLATTEN\nTicket: {ticket}\nSymbol: {symbol}\n"
                         f"Reason: {why}\nFloating P/L at close: {pnl}")
            closed.add(ticket)
        return closed

    # --- closed-position detection ----------------------------------------

    def reap(self, open_tickets):
        """Notice positions that left the broker — stopped out, taken by TP,
        or closed by hand — and close their rows."""
        for pos in self.book.all():
            if pos.ticket in open_tickets or pos.closed_notified:
                continue
            exit_px = self.bridge.deal_price(pos.ticket) or pos.last_price
            log(f"[EXIT] ticket={pos.ticket} closed (exit≈{exit_px})")
            pos.mark_closed()
            self.db.record_close(pos.ticket, reason="not_in_positions",
                                 exit_price=exit_px)
            self.tg.post(f"🔒 POSITION CLOSED\nTicket: {pos.ticket}\n"
                         f"Entry: {pos.entry_price}\nLast SL: {pos.current_sl}\n"
                         f"Exit: {exit_px if exit_px else 'unknown'}")

    # --- one full pass ----------------------------------------------------

    def run_pass(self, candles, lookback_1m=6000):
        """Flatten, trail, then reap — the whole stop-management cycle."""
        if not self.book:
            return
        live = self.bridge.positions()
        if live is None:
            return          # bridge unreachable: act on nothing this pass
        open_tickets = set()
        for p in live:
            try:
                open_tickets.add(int(p.get("ticket", 0)))
            except (TypeError, ValueError):
                continue

        for symbol in self.book.symbols():
            closed = self.flatten_if_due(live, symbol)
            if closed:
                live = [p for p in live
                        if int(p.get("ticket", 0) or 0) not in closed]
                open_tickets -= closed
            close_px, low_px, high_px = candles.last_price(symbol, "M1", lookback_1m)
            if close_px is None:
                continue
            self.trail(live, symbol, close_px, low_px=low_px, high_px=high_px)

        self.reap(open_tickets)
