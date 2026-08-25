"""Market sessions and the pre-close flatten window.

Session times key off New York wall clock: oil and gold pause 17:00-18:00 ET
daily, everything non-crypto closes Friday 17:00 ET, crypto never closes.
Using America/New_York rather than a fixed UTC offset makes these DST-proof.

Why flatten at all: on 2026-07-12 four USOIL positions were held through the
weekend and gap-filled at the Sunday reopen for -43.16. Closing shortly BEFORE
the session ends means a trade never sits through a reopening gap.

Table lifted unchanged from the Version 1 scripts, where each of the seven
carried its own copy — S21's copy was inlined twice inside two different
functions, and listed only BTCUSD.
"""

import datetime as _dt
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

SESSIONS = {
    "USOIL":  {"daily_break": True,  "weekend": True},
    "XAUUSD": {"daily_break": True,  "weekend": True},
    "USDJPY": {"daily_break": False, "weekend": True},
    "EURUSD": {"daily_break": False, "weekend": True},
    "BTCUSD": {"daily_break": False, "weekend": False},   # 24/7
    "BTCUSDT": {"daily_break": False, "weekend": False},  # 24/7
}

CLOSE_HOUR = 17   # 17:00 ET


def _now(now=None):
    return now or _dt.datetime.now(NY)


def is_market_closed(symbol, now=None):
    """True while the symbol's venue is shut. Unknown symbols are treated as
    always open — the same choice Version 1 made, so a newly added symbol
    trades rather than silently never scanning."""
    s = SESSIONS.get(symbol)
    if not s:
        return False
    t = _now(now)
    if s["weekend"]:
        if ((t.weekday() == 4 and t.hour >= CLOSE_HOUR)
                or t.weekday() == 5
                or (t.weekday() == 6 and t.hour < CLOSE_HOUR)):
            return True
    if s["daily_break"]:
        if t.weekday() < 4 and t.hour == CLOSE_HOUR:
            return True
    return False


def flatten_due(symbol, lead_min, before_weekend, before_daily_break, now=None):
    """(due, reason) — inside the lead window before this symbol's close?"""
    s = SESSIONS.get(symbol)
    if not s:
        return (False, "")
    t = _now(now)
    mins = t.hour * 60 + t.minute
    if not (CLOSE_HOUR * 60 - lead_min <= mins < CLOSE_HOUR * 60):
        return (False, "")
    if t.weekday() == 4 and s["weekend"] and before_weekend:
        return (True, "weekend_close")
    if t.weekday() < 4 and s["daily_break"] and before_daily_break:
        return (True, "daily_break")
    return (False, "")
