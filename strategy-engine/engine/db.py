"""Trade persistence, as an object the runtime can be handed.

This deliberately DELEGATES to the top-level `trade_db` module rather than
reimplementing or importing it by path. Two harnesses replace persistence by
putting their own module at `sys.modules["trade_db"]` before a strategy is
imported — the backtest loader, whose `_assert_persistence_sealed()` would
otherwise let a replay write to production CockroachDB, and the CI probe.
Reaching around that name would silently defeat both.

Every method swallows its own exceptions. The rule from Version 1 stands: the
database must never block or break trading.
"""

import trade_db as _mod          # intentionally by name — see above

from .notify import log


class TradeDB:
    def __init__(self, strategy_id, db_url=None, magic=None, bridge_url=None,
                 label=None, timeframe=None, symbols=None, extra=None):
        self.strategy_id = strategy_id
        # Version 1 left strategy_registry.symbols empty, so the dashboard had
        # to infer a strategy's symbols from the trades it had already made —
        # one that had never traded showed none. Declare them instead.
        try:
            _mod.init(strategy_id, db_url, magic=magic, bridge_url=bridge_url,
                      label=label, timeframe=timeframe, extra=extra,
                      symbols=list(symbols or []))
        except TypeError:
            # a stub predating the symbols argument
            _mod.init(strategy_id, db_url, magic=magic, bridge_url=bridge_url,
                      label=label, timeframe=timeframe, extra=extra)
        except Exception as e:
            log(f"[DB] init failed, continuing without persistence: {e}")

    def enabled(self):
        try:
            return bool(_mod.enabled())
        except Exception:
            return False

    def _call(self, name, *args, **kwargs):
        fn = getattr(_mod, name, None)
        if fn is None:
            return None
        try:
            return fn(*args, **kwargs)
        except TypeError:
            # a stub with an older signature (the probe and backtest fakes
            # predate exit_price) — retry without the optional extras
            for drop in ("exit_price",):
                if drop in kwargs:
                    kwargs.pop(drop)
                    try:
                        return fn(*args, **kwargs)
                    except Exception as e:
                        log(f"[DB] {name} failed: {e}")
                        return None
            log(f"[DB] {name} signature mismatch")
            return None
        except Exception as e:
            log(f"[DB] {name} failed: {e}")
            return None

    # --- lifecycle --------------------------------------------------------

    def record_signal(self, event_id, symbol, side, signal_price=None, qty=None,
                      hard_sl=None, extra=None):
        return self._call("record_signal", event_id, symbol, side,
                          signal_price=signal_price, qty=qty, hard_sl=hard_sl,
                          extra=extra)

    def record_execution(self, event_id, ticket, retcode, entry_price=None,
                         qty=None, hard_sl=None, targets=None, error=None):
        return self._call("record_execution", event_id, ticket, retcode,
                          entry_price=entry_price, qty=qty, hard_sl=hard_sl,
                          targets=targets, error=error)

    def record_trail(self, ticket, new_sl, ratio, executed=True):
        return self._call("record_trail", ticket, new_sl, ratio, executed=executed)

    def record_close(self, ticket, reason="not_in_positions", pnl=None,
                     exit_price=None):
        return self._call("record_close", ticket, reason=reason, pnl=pnl,
                          exit_price=exit_price)

    def mark_telegram_sent(self, event_id):
        return self._call("mark_telegram_sent", event_id)

    def record_recovery_event(self, ticket, note):
        return self._call("record_recovery_event", ticket, note)

    def load_open_trades(self):
        return self._call("load_open_trades") or []

    def live_elsewhere(self, max_age_sec=180):
        """(already_running, description) — see trade_db.live_elsewhere."""
        fn = getattr(_mod, "live_elsewhere", None)
        if fn is None:
            return False, "singleton check unavailable"
        try:
            return fn(max_age_sec)
        except Exception as e:
            log(f"[DB] singleton check failed: {e}")
            return False, f"check failed: {e}"
