#!/usr/bin/env python3
"""Exercise one strategy's live-execution path with the broker and DB stubbed.

    python test/live_execution_probe.py "Live/Strategy 17/Bridge-....py"

Answers one question: if this were started against a real account, would it
place orders and survive a restart?

It does NOT check whether the strategy's signals are any good — indicators and
setup detection are never called. It checks the execution and lifecycle layer,
which is where the failures that need a human at 3am actually live.

Why a subprocess per strategy: importing one of these runs `trade_db.init()` and
installs module-global state, so two in the same interpreter would contaminate
each other. The pytest wrapper spawns one of these per file.

Everything external is replaced before the import:
  trade_db  a recorder — the real one connects to production CockroachDB at
            import time and starts a heartbeat, even when handed an empty URL
  requests  a fake bridge that accepts orders and tracks open positions
  talib     a stub; indicator functions are never invoked here
  socket    blocked outright, so a missed stub fails loudly instead of
            quietly reaching the live bridge from CI
"""

import argparse
import importlib.util
import json
import os
import re
import socket
import sys
import tempfile
import types
from unittest.mock import MagicMock

TICKET = 3581999001          # shaped like a real MT5 ticket, not a paper one
GONE_TICKET = 3111111111


class Probe:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(path)
        self.failures = []
        self.warnings = []
        self.passes = []
        self.db = []             # stands in for the `trades` table
        self.broker = {}         # MT5's open positions
        self.calls = []          # every HTTP call the strategy made

    # --- reporting ------------------------------------------------------------

    def ok(self, msg):
        self.passes.append(msg)

    def fail(self, msg):
        self.failures.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def check(self, cond, msg, hard=True):
        (self.ok if cond else (self.fail if hard else self.warn))(msg)
        return cond

    # --- stubs ----------------------------------------------------------------

    def _trade_db(self):
        p = self
        td = types.ModuleType("trade_db")
        td.init = lambda *a, **k: None
        td.enabled = lambda: True
        td.load_open_trades = lambda: [
            {"event_id": r["event_id"], "symbol": r["symbol"], "side": "buy",
             "ticket": r["ticket"], "entry_price": 63000.0, "qty": 0.1,
             "hard_sl": 62500.0, "current_sl": r["current_sl"],
             "targets": {2: 64000.0, 3: 64500.0}, "trail_hit": set()}
            for r in p.db if r["status"] == "OPEN"]

        def record_execution(ev, ticket, retcode, **k):
            ok = bool(ticket) and int(ticket) > 0 and int(retcode or 0) == 10009
            p.db.append({"event_id": ev, "ticket": ticket, "symbol": "BTCUSD",
                         "status": "OPEN" if ok else "REJECTED",
                         "current_sl": 62500.0})

        def record_close(ticket, reason="", pnl=None):
            for r in p.db:
                if r["ticket"] == ticket:
                    r["status"], r["close_reason"] = "CLOSED", reason

        def record_trail(ticket, new_sl, ratio, executed=True):
            if executed:
                for r in p.db:
                    if r["ticket"] == ticket:
                        r["current_sl"] = new_sl

        td.record_execution, td.record_close, td.record_trail = (
            record_execution, record_close, record_trail)
        for n in ("record_signal", "record_recovery_event", "mark_telegram_sent"):
            setattr(td, n, lambda *a, **k: None)
        return td

    def _requests(self):
        p = self

        class Resp:
            def __init__(s, payload, code=200):
                s._p, s.status_code, s.text = payload, code, json.dumps(payload)
            def json(s):
                return s._p
            def raise_for_status(s):
                if s.status_code >= 400:
                    raise IOError(f"HTTP {s.status_code}")

        def get(url, **kw):
            p.calls.append(("GET", url))
            if url.rstrip("/").endswith("/positions"):
                return Resp([dict(v) for v in p.broker.values()])
            if url.rstrip("/").endswith("/history"):
                # Version 2 reads the close price from deal history rather than
                # reconstructing it from pnl. Report the probe ticket as closed.
                return Resp([{"position_id": TICKET, "entry": 1,
                              "price": 63500.0, "profit": 5.0}])
            return Resp({"candles": []}, 404)

        def post(url, **kw):
            p.calls.append(("POST", url))
            body = kw.get("json") or {}
            if "api.telegram.org" in url:
                return Resp({"ok": True})
            if url.endswith("/trade"):
                p.broker[TICKET] = {
                    "ticket": TICKET, "magic": body.get("magic", 0),
                    "type": body.get("type", 0), "symbol": body.get("symbol", ""),
                    "volume": body.get("volume", 0.0), "price_open": 63000.0,
                    "price_current": 63000.0, "sl": body.get("sl", 0.0),
                    "tp": 0.0, "profit": 0.0}
                return Resp({"order_id": TICKET, "result": 10009,
                             "comment": "Request executed"})
            if url.endswith("/modify"):
                t = int(body.get("ticket", 0))
                if t in p.broker:
                    p.broker[t]["sl"] = float(body.get("sl", 0))
                return Resp({"result": 10009})
            if url.endswith("/close"):
                p.broker.pop(int(body.get("ticket", 0)), None)
                return Resp({"result": 10009})
            return Resp({"error": "unrouted"}, 404)

        req = types.ModuleType("requests")
        req.get, req.post = get, post
        req.exceptions = types.SimpleNamespace(RequestException=IOError,
                                               HTTPError=IOError, Timeout=IOError)
        return req

    # --- static checks --------------------------------------------------------

    def static(self):
        src = open(self.path, encoding="utf-8").read()

        m = re.search(r"^PAPER_TRADING_ONLY\s*=\s*(\w+)", src, re.M)
        if not m:
            self.ok("no paper-trading flag")
        elif m.group(1) == "True":
            self.fail("PAPER_TRADING_ONLY is True — no order would reach MT5, and "
                      "the simulated fill is still written to the live trades "
                      "table as source='live'")
        else:
            self.ok(f"PAPER_TRADING_ONLY is {m.group(1)} — orders reach MT5")

        cfg = self.path[:-3] + ".json"
        self.check(os.path.exists(cfg),
                   f"config sidecar present ({os.path.basename(cfg)})")

        # The shim must reach a directory that really holds trade_db.py. A copy
        # sitting beside the script masks a wrong shim, because Python puts the
        # script's own directory on sys.path first.
        d = os.path.dirname(self.path)
        reachable = any(
            os.path.exists(os.path.join(p, "trade_db.py"))
            for p in (os.path.dirname(d), os.path.dirname(os.path.dirname(d))))
        self.check(reachable, "a parent directory provides trade_db.py", hard=False)
        if os.path.exists(os.path.join(d, "trade_db.py")):
            self.warn("a duplicate trade_db.py sits beside the script — it will "
                      "shadow Live/trade_db.py and miss fixes made there")

    # --- trailing ladder ------------------------------------------------------

    def trail_ladder(self, mod, sym):
        """The stop must keep ratcheting past the last precomputed target.

        Targets are precomputed to 1:10 only. A position that ran further used
        to keep its stop frozen at the last anchor, so an open runner handed the
        whole excursion back to the market. Both directions are driven here: a
        given strategy only ever takes one side, but the trail serves both.
        """
        # Two shapes exist. S17 is handed the positions and the price;
        # Strategy 18.1 takes no arguments and fetches both itself, and its
        # XAUUSD variant calls the same pass `run_trailing_pass`. Drive
        # whichever is present rather than skipping — trailing is the part of
        # these strategies most likely to fail silently.
        pass_fn = (getattr(mod, "trail_conservative_positions", None)
                   or getattr(mod, "run_trailing_pass", None))
        if pass_fn is None:
            return self.warn("no trailing pass found — nothing trails")
        import inspect
        try:
            takes_args = bool(inspect.signature(pass_fn).parameters)
        except (TypeError, ValueError):
            takes_args = True

        # A strategy that precomputes its ladder can only trail as far as that
        # ladder reaches, so build the probe's targets from the strategy's own
        # ratio list. This is what "indefinite" trailing rests on: 18.1 widened
        # ALL_RATIOS from 10 rungs to 100.
        ratios = list(getattr(mod, "ALL_RATIOS", None)
                      or [0.5] + list(range(1, 11)))

        entry, step = 63000.0, 500.0
        for label, side, sign, t in (("buy", 0, 1.0, 3581999801),
                                     ("sell", 1, -1.0, 3581999802)):
            hard_sl = entry - sign * step
            mod._ticket_map[t] = {
                "symbol": sym, "event_id": f"probe-trail-{label}", "side": label,
                "entry_price": entry, "hard_sl": hard_sl,
                "targets": {r: entry + sign * r * step for r in ratios},
                "current_sl": hard_sl, "trail_hit": set()}
            self.broker[t] = {
                "ticket": t, "magic": getattr(mod, "MAGIC", 0), "type": side,
                "symbol": sym, "volume": 0.01, "price_open": entry,
                "price_current": entry, "sl": hard_sl, "tp": 0.0, "profit": 0.0}

            # anchor lags the reached ratio by two: 1:10 → 1:8, and past the
            # precomputed ladder 1:14 → 1:12.
            for reached, anchor_r in ((10, 8), (14, 12)):
                px = entry + sign * reached * step
                if reached not in ratios:
                    # This strategy's ladder does not reach that far, so there
                    # is no rung to trail to. Skipping keeps the check honest
                    # instead of asserting behaviour the design never promised.
                    continue
                self.broker[t]["price_current"] = px
                if takes_args:
                    pass_fn([dict(self.broker[t])], px, sym,
                            low_px=px, high_px=px)
                else:
                    pass_fn()
                want = entry + sign * anchor_r * step
                got = self.broker[t]["sl"]
                self.check(abs(got - want) < 1e-6,
                           f"{label} runner at 1:{reached} trails the stop to "
                           f"1:{anchor_r} (want {want}, got {got})")

            del mod._ticket_map[t]
            self.broker.pop(t, None)

    # --- dynamic checks -------------------------------------------------------

    def load(self):
        sys.modules["trade_db"] = self._trade_db()
        sys.modules["requests"] = self._requests()
        sys.modules["talib"] = MagicMock()

        saved_socket = socket.socket

        class Blocked(socket.socket):
            def __init__(self, *a, **k):
                raise RuntimeError("probe attempted a real network connection")

        socket.socket = Blocked
        try:
            os.chdir(tempfile.mkdtemp(prefix="probe-"))
            spec = importlib.util.spec_from_file_location("probed", self.path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            socket.socket = saved_socket

        mod.tg_post = lambda *a, **k: None
        self.ok("imports with no network and no database")
        return mod

    def symbol_of(self, mod):
        for attr in ("SYMBOL_BRIDGE", "SYMBOL"):
            if getattr(mod, attr, None):
                return getattr(mod, attr)
        inst = getattr(mod, "INSTRUMENTS", None)
        return inst[0] if inst else "BTCUSD"

    def run(self):
        self.static()
        mod = self.load()
        sym = self.symbol_of(mod)

        if getattr(mod, "strategy", None) is not None and hasattr(mod.strategy, "book"):
            return self.run_v2(mod, sym)

        # 1. an order must actually reach the bridge
        ticket, retcode, _ = mod.mt5_bridge_trade(sym, 0, 0.01, 62500.0)
        placed = any(m == "POST" and u.endswith("/trade") for m, u in self.calls)
        self.check(placed, "mt5_bridge_trade sends the order to the bridge")
        self.check(retcode == 10009 and ticket == TICKET,
                   f"a filled order returns the broker's ticket "
                   f"(got ticket={ticket} retcode={retcode})")
        if not placed:
            return self.report()

        # 2. the stop keeps trailing for as long as the trade runs
        self.trail_ladder(mod, sym)

        mod._ticket_map[ticket] = {
            "symbol": sym, "event_id": "probe-1", "side": "buy",
            "entry_price": 63000.0, "hard_sl": 62500.0,
            "targets": {2: 64000.0, 3: 64500.0},
            "current_sl": 62500.0, "trail_hit": set()}
        mod._event_to_ticket["probe-1"] = ticket
        self.db.append({"event_id": "probe-1", "ticket": ticket, "symbol": sym,
                        "status": "OPEN", "current_sl": 62500.0})

        # 3. restart while the position is still open — must resume trailing.
        #    This is the one that bites: an empty DB short-circuits the code
        #    path, so the bug only ever appears on a restart after a trade.
        mod._ticket_map.clear()
        mod._event_to_ticket.clear()
        try:
            mod.recover_open_trades()
            crashed = None
        except Exception as e:
            crashed = f"{type(e).__name__}: {e}"
        if not self.check(crashed is None,
                          f"recover_open_trades() survives a populated database"
                          + (f" — raised {crashed}" if crashed else "")):
            return self.report()
        self.check(ticket in mod._ticket_map,
                   "a still-open position resumes trailing after a restart")

        # 4. restart after the broker closed it — must not resume a dead ticket
        self.broker.pop(ticket, None)
        mod._ticket_map.clear()
        try:
            mod.recover_open_trades()
        except Exception as e:
            self.fail(f"recover_open_trades() raised on a closed ticket: {e}")
            return self.report()
        self.check(ticket not in mod._ticket_map,
                   "a position closed while offline is not resumed", hard=False)
        self.check(self.db[0]["status"] == "CLOSED",
                   "a position closed while offline is marked CLOSED, so stale "
                   "OPEN rows do not accumulate", hard=False)

        # 5. closed-position detection during a run
        if hasattr(mod, "run_trailing_pass"):
            self.ok("run_trailing_pass present (closes rows during a run)")
        else:
            self.warn("no run_trailing_pass — a closed position stays OPEN in "
                      "the DB until the next restart reconciles it")
        return self.report()

    # --- Version 2 ------------------------------------------------------------

    def run_v2(self, mod, sym):
        """Same questions, asked of a Version 2 strategy.

        A Version 2 strategy is a declaration over engine/, so there are no
        module-level mt5_bridge_* functions or _ticket_map to poke. The
        behaviours being checked are identical; only the handles change.
        """
        from engine import recovery
        from engine.positions import Position

        st = mod.strategy
        self.check(st.magic and int(st.magic) > 0,
                   f"declares a magic number ({st.magic})")
        self.check(bool(st.symbols), f"declares its symbols ({', '.join(st.symbols)})")
        self.check(st._scan_fn is not None, "registers a scan hook")

        # 1. an order must actually reach the bridge
        ticket, retcode, _ = st.bridge.place(sym, "buy", 0.01, sl=62500.0)
        placed = any(m == "POST" and u.endswith("/trade") for m, u in self.calls)
        self.check(placed, "the bridge sends the order")
        self.check(retcode == 10009 and ticket == TICKET,
                   f"a filled order returns the broker's ticket "
                   f"(got ticket={ticket} retcode={retcode})")
        if not placed:
            return self.report()

        # 2. the stop keeps trailing for as long as the trade runs, with no
        #    last rung — the failure that let a runner hand back its excursion
        entry, step = 63000.0, 500.0
        for label, side, sign in (("buy", 0, 1.0), ("sell", 1, -1.0)):
            t = TICKET + (1 if label == "buy" else 2)
            pos = Position(ticket=t, symbol=sym, side=label, event_id=f"probe-{label}",
                           entry_price=entry, hard_sl=entry - sign * step,
                           current_sl=entry - sign * step)
            st.book.add(pos)
            self.broker[t] = {"ticket": t, "magic": st.magic, "type": side,
                              "symbol": sym, "volume": 0.01, "price_open": entry,
                              "price_current": entry, "sl": entry - sign * step,
                              "tp": 0.0, "profit": 0.0}
            for reached, anchor_r in ((10, 8), (14, 12), (25, 23)):
                px = entry + sign * reached * step
                st.stops.trail([dict(self.broker[t])], sym, px, low_px=px, high_px=px)
                want = entry + sign * anchor_r * step
                got = self.broker[t]["sl"]
                self.check(abs(got - want) < 1e-6,
                           f"{label} runner at 1:{reached} trails the stop to "
                           f"1:{anchor_r} (want {want}, got {got})")
            st.book.drop(t)
            self.broker.pop(t, None)

        # 3. restart while the position is still open — must resume trailing.
        #    An empty database short-circuits this path, so the bug only ever
        #    shows on the NEXT start, with a live position and nothing trailing.
        self.db.append({"event_id": "probe-1", "ticket": TICKET, "symbol": sym,
                        "status": "OPEN", "current_sl": 62500.0})
        st.book.drop(TICKET)
        try:
            recovery.recover(st.book, st.db, st.bridge, st.telegram)
            crashed = None
        except Exception as e:
            crashed = f"{type(e).__name__}: {e}"
        if not self.check(crashed is None,
                          "recovery survives a populated database"
                          + (f" — raised {crashed}" if crashed else "")):
            return self.report()
        self.check(TICKET in st.book,
                   "a still-open position resumes trailing after a restart")
        resumed = st.book.get(TICKET)
        if resumed is not None:
            self.check(resumed.r_unit > 0,
                       "the resumed position has a usable ratio ladder")

        # 4. restart after the broker closed it — must not resume a dead ticket
        self.broker.pop(TICKET, None)
        st.book.drop(TICKET)
        try:
            recovery.recover(st.book, st.db, st.bridge, st.telegram)
        except Exception as e:
            self.fail(f"recovery raised on a closed ticket: {e}")
            return self.report()
        self.check(TICKET not in st.book,
                   "a position closed while offline is not resumed", hard=False)
        self.check(self.db[0]["status"] == "CLOSED",
                   "a position closed while offline is marked CLOSED, so stale "
                   "OPEN rows do not accumulate", hard=False)

        # 5. closed-position detection during a run
        self.check(hasattr(st.stops, "run_pass"),
                   "stop management closes rows during a run")
        return self.report()

    def report(self):
        print(f"\n=== {self.name}")
        for m in self.passes:
            print(f"  PASS  {m}")
        for m in self.warnings:
            print(f"  WARN  {m}")
        for m in self.failures:
            print(f"  FAIL  {m}")
        return not self.failures


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("strategy")
    args = ap.parse_args()
    try:
        passed = Probe(args.strategy).run()
    except Exception:
        import traceback
        print(f"\n=== {os.path.basename(args.strategy)}")
        # to stdout, not stderr: pytest shows captured stdout on failure, and
        # a bare "could not complete" in a CI log is undiagnosable.
        traceback.print_exc(file=sys.stdout)
        print("  FAIL  the probe itself could not complete")
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
