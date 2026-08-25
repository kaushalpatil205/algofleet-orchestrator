"""Simulated time.

The strategies read the clock in three distinct ways, and each needs its own
treatment:

1. `datetime.now(timezone.utc)` — resolved through the module-level
   `from datetime import datetime`, so rebinding the module attribute is enough.
2. `_time.sleep(...)` / a function-local `import time; time.sleep(...)` — the
   latter resolves sys.modules at call time, so the module attribute alone does
   not cover it.
3. `is_market_closed()` / `market_close_flatten_due()` — these do
   `import datetime as _mdt` *inside the function body* and work in New York
   wall time. Rebinding module attributes cannot reach them, but both already
   accept a `_now` override for testability, so the loader wraps them to pass
   simulated time instead. That keeps the guards' own logic untouched.
"""

import datetime as _dt
import time as _real_time
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = _dt.timezone.utc


class VirtualClock:
    """Monotonic simulated clock. Single-threaded by design — a replay drives
    time explicitly rather than racing a real one."""

    def __init__(self, start):
        self.set(start)

    def set(self, when):
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        self._now = when.astimezone(UTC)

    def now(self):
        return self._now

    def now_ny(self):
        return self._now.astimezone(NY)

    def advance(self, seconds):
        self._now = self._now + _dt.timedelta(seconds=float(seconds))
        return self._now

    def epoch(self):
        return self._now.timestamp()


def datetime_shim(clock):
    """A drop-in for the `datetime` class whose now()/utcnow() read the clock.

    Subclasses the real thing so every other constructor and method — strptime,
    fromtimestamp, arithmetic, comparison against real datetimes — keeps working
    untouched.
    """

    class SimDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            t = clock.now()
            return t.astimezone(tz) if tz is not None else t.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return clock.now().replace(tzinfo=None)

        @classmethod
        def today(cls):
            return clock.now().replace(tzinfo=None)

    return SimDatetime


class TimeShim:
    """Stands in for the `time` module on a strategy's `_time` attribute.

    sleep() advances simulated time rather than blocking: the post-entry fill
    poll sleeps 1s up to three times per trade, which is real elapsed time live
    and should be real *simulated* time here, just not real waiting.
    """

    def __init__(self, clock):
        self._clock = clock

    def sleep(self, seconds):
        self._clock.advance(seconds)

    def time(self):
        return self._clock.epoch()

    def monotonic(self):
        return self._clock.epoch()

    def __getattr__(self, name):
        # Anything not overridden (strftime, gmtime, ...) behaves normally.
        return getattr(_real_time, name)


class no_real_sleep:
    """Context manager neutralising the real `time.sleep` for the run.

    Needed because `run_live_scan` does a function-local `import time` for its
    post-entry poll, which bypasses the patched module attribute. Only sleep is
    touched — time.time() stays real so pandas and talib are unaffected.
    """

    def __init__(self, clock):
        self._clock = clock
        self._saved = None

    def __enter__(self):
        self._saved = _real_time.sleep
        _real_time.sleep = self._clock.advance
        return self

    def __exit__(self, *exc):
        _real_time.sleep = self._saved
        return False
