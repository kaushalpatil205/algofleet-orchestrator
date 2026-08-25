"""Recording stand-in for Live/trade_db.py.

Seeded into sys.modules *before* a strategy is imported. That ordering is not a
detail: the real module runs `trade_db.init(...)` at strategy import time, and
its init falls back to a hardcoded CockroachDB URL when handed an empty one — so
a strategy imported without this stub in place connects to production, writes a
`strategy_registry` row and starts a 60s heartbeat thread before a single line of
backtest code runs.

The public surface mirrors the real module exactly. Every call is appended to an
ordered log instead of a database. That log is the backtest's primary output:
because the strategies emit the same lifecycle calls live, replaying and diffing
against real `trades` rows is a direct comparison (see parity.py).

Like the real module, nothing here raises — persistence is best-effort there, and
a stub that threw would change control flow the live code never takes.
"""

from collections import OrderedDict

# Set by the loader so recorded events carry simulated, not wall-clock, time.
_now_fn = None

_strategy_id = None
_magic = None
_registered = None

events = []          # ordered [{seq, at, kind, ...}] — the replay transcript
trades = OrderedDict()   # event_id -> merged trade record
_by_ticket = {}      # ticket -> event_id


def bind_clock(now_fn):
    """Point recorded timestamps at the simulated clock."""
    global _now_fn
    _now_fn = now_fn


def reset():
    """Clear state between runs — the real module is process-global too."""
    global _strategy_id, _magic, _registered
    _strategy_id = _magic = _registered = None
    events.clear()
    trades.clear()
    _by_ticket.clear()


def _at():
    if _now_fn is None:
        return None
    ts = _now_fn()
    return ts.isoformat() if hasattr(ts, "isoformat") else ts


# The event_type names the real module writes into trade_events. Carried on
# every recorded event so a replay transcript speaks the same vocabulary as the
# live audit trail — which is what lets parity.py join the two directly.
EVENT_TYPE = {
    "SIGNAL": "SIGNAL",
    "EXECUTION": "MT5_RESULT",
    "TRAIL_MOVE": "TRAIL_MOVE",
    "CLOSE": "CLOSE_DETECTED",
    "RECOVERY": "RECOVERY",
}


def _log(kind, **fields):
    rec = {"seq": len(events), "at": _at(), "kind": kind,
           "event_type": EVENT_TYPE.get(kind)}
    rec.update(fields)
    events.append(rec)
    return rec


def _trade(event_id):
    if event_id not in trades:
        trades[event_id] = {"event_id": event_id}
    return trades[event_id]


# --- public API (mirrors Live/trade_db.py) ------------------------------------

def init(strategy_id, db_url=None, magic=None, bridge_url=None, label=None,
         timeframe=None, extra=None):
    global _strategy_id, _magic, _registered
    _strategy_id = strategy_id
    _magic = magic
    _registered = {"strategy_id": strategy_id, "magic": magic,
                   "bridge_url": bridge_url, "label": label,
                   "timeframe": timeframe, "extra": extra}
    _log("INIT", strategy_id=strategy_id, magic=magic)


def enabled():
    # True so the strategies take their normal persistence path — notably
    # recover_open_trades(), which returns early when persistence is off and
    # would otherwise skip a code path the live system always executes.
    return _strategy_id is not None


def record_signal(event_id, symbol, side, signal_price=None, qty=None,
                  hard_sl=None, extra=None):
    t = _trade(event_id)
    t.update({"symbol": symbol, "side": side, "signal_price": signal_price,
              "qty": qty, "hard_sl": hard_sl, "extra": extra,
              "status": "SIGNAL", "signal_at": _at()})
    _log("SIGNAL", event_id=event_id, symbol=symbol, side=side,
         signal_price=signal_price, qty=qty, hard_sl=hard_sl, extra=extra)


def record_execution(event_id, ticket, retcode, entry_price=None, qty=None,
                     hard_sl=None, targets=None, error=None):
    ok = bool(ticket) and int(ticket) > 0 and int(retcode or 0) == 10009
    t = _trade(event_id)
    t.update({"ticket": ticket, "retcode": retcode, "entry_price": entry_price,
              "qty": qty, "hard_sl": hard_sl, "targets": targets,
              "error": error, "status": "OPEN" if ok else "REJECTED",
              "current_sl": hard_sl, "trail_hit": set(),
              "executed_at": _at()})
    if ok:
        _by_ticket[int(ticket)] = event_id
    _log("EXECUTION", event_id=event_id, ticket=ticket, retcode=retcode,
         entry_price=entry_price, qty=qty, hard_sl=hard_sl, targets=targets,
         error=error, accepted=ok)


def record_trail(ticket, new_sl, ratio, executed=True):
    ev = _by_ticket.get(int(ticket))
    if ev:
        t = trades[ev]
        # Only an executed modify moves the stop. The real module is deliberate
        # about this: a rejected modify must not drift the DB's current_sl away
        # from what the broker actually holds.
        if executed:
            t["current_sl"] = new_sl
            t.setdefault("trail_hit", set()).add(ratio)
    _log("TRAIL_MOVE", ticket=ticket, event_id=ev, new_sl=new_sl,
         ratio=ratio, executed=executed)


def record_close(ticket, reason="not_in_positions", pnl=None):
    ev = _by_ticket.get(int(ticket))
    if ev:
        trades[ev].update({"status": "CLOSED", "close_reason": reason,
                           "pnl": pnl, "closed_at": _at()})
    _log("CLOSE", ticket=ticket, event_id=ev, reason=reason, pnl=pnl)


def mark_telegram_sent(event_id):
    _trade(event_id)["telegram_sent"] = True
    _log("TELEGRAM_SENT", event_id=event_id)


def record_recovery_event(ticket, note):
    _log("RECOVERY", ticket=ticket, event_id=_by_ticket.get(int(ticket)),
         note=note)


def load_open_trades():
    # A replay starts flat by construction: there is no previous run whose
    # positions could still be open. Returning [] makes recover_open_trades()
    # a no-op without disabling the code path.
    return []
