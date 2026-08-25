"""The strategy's own view of its open positions.

Version 1 kept two module-level dicts, `_ticket_map` and `_event_to_ticket`,
written by the scan thread and read by the stop-management thread. That worked
because dict operations are atomic under the GIL, but it left the invariant —
the two dicts agreeing — implicit. Here they are one object with a lock, so
adopting and dropping a position cannot leave a half-updated pair behind.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Set


@dataclass
class Position:
    """One live position this strategy opened and is managing.

    `entry_price` is the broker's actual fill and `hard_sl` the corrected
    fixed-risk stop derived from it. Those two define the ratio ladder — see
    engine/trailing.py — so they are the fields that must be right.
    """
    ticket: int
    symbol: str
    side: str                 # "buy" | "sell"
    event_id: str
    entry_price: float
    hard_sl: float
    current_sl: float
    qty: float = 0.0
    trail_hit: Set[int] = field(default_factory=set)
    closed_notified: bool = False
    exit_datetime: Optional[str] = None
    # Last price seen while the position was still open. Used as a fallback
    # exit price when the broker's deal history cannot be read.
    last_price: Optional[float] = None

    @property
    def direction(self):
        return 1.0 if self.side == "buy" else -1.0

    @property
    def r_unit(self):
        """One R: the distance between entry and the stop actually on the
        position. Zero means the ladder cannot be built and trailing is a
        no-op rather than a division by zero."""
        return abs(float(self.entry_price) - float(self.hard_sl))

    def mark_closed(self):
        self.closed_notified = True
        self.exit_datetime = datetime.now(timezone.utc).isoformat()


class Book:
    def __init__(self):
        self._by_ticket = {}
        self._by_event = {}
        self._lock = threading.RLock()

    def __contains__(self, ticket):
        with self._lock:
            return int(ticket) in self._by_ticket

    def __len__(self):
        with self._lock:
            return len(self._by_ticket)

    def __bool__(self):
        return len(self) > 0

    def add(self, pos):
        with self._lock:
            self._by_ticket[int(pos.ticket)] = pos
            self._by_event[pos.event_id] = int(pos.ticket)
        return pos

    def get(self, ticket):
        with self._lock:
            return self._by_ticket.get(int(ticket))

    def by_event(self, event_id):
        with self._lock:
            t = self._by_event.get(event_id)
            return self._by_ticket.get(t) if t is not None else None

    def ticket_of(self, event_id):
        with self._lock:
            return self._by_event.get(event_id)

    def has_event(self, event_id):
        with self._lock:
            return event_id in self._by_event

    def drop(self, ticket):
        with self._lock:
            pos = self._by_ticket.pop(int(ticket), None)
            if pos is not None:
                self._by_event.pop(pos.event_id, None)
            return pos

    def all(self):
        with self._lock:
            return list(self._by_ticket.values())

    def symbols(self):
        with self._lock:
            return sorted({p.symbol for p in self._by_ticket.values() if p.symbol})
