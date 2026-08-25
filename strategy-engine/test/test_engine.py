"""End-to-end exercise of the Version 2 engine with the broker and DB faked.

Drives the whole path a live strategy takes — scan, place, correct the stop,
register, trail, close — and asserts the behaviours that cost real money when
they regress. Nothing here touches the network.
"""

import json
import os
import sys
import tempfile
import types

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# trade_db is imported by name inside engine.db; stub it before that happens
_stub = types.ModuleType("trade_db")
_stub.init = lambda *a, **k: None
_stub.enabled = lambda: True
_stub.load_open_trades = lambda: []
for _n in ("record_signal", "record_execution", "record_trail", "record_close",
           "mark_telegram_sent", "record_recovery_event"):
    setattr(_stub, _n, lambda *a, **k: None)
sys.modules.setdefault("trade_db", _stub)

from engine import Signal, Strategy               # noqa: E402
from engine.notify import NullTelegram            # noqa: E402


class FakeBridge:
    """Stands in for the MT5 bridge: accepts orders, tracks open positions."""

    def __init__(self, magic=99001, fill=None, reject=False, modify_ok=True):
        self.magic = magic
        self.positions_book = {}
        self.calls = []
        self.next_ticket = 5000001
        self.fill = fill
        self.reject = reject
        self.modify_ok = modify_ok

    def candles(self, symbol, timeframe, count):
        base = 1_700_000_000
        step = {"M1": 60, "M5": 300}.get(timeframe, 60)
        return [{"time": base + i * step, "open": 2400.0, "high": 2401.0,
                 "low": 2399.0, "close": 2400.0, "tick_volume": 5}
                for i in range(count)]

    def positions(self):
        return list(self.positions_book.values())

    def place(self, symbol, side, lots, sl=0.0):
        self.calls.append(("place", symbol, side, lots, sl))
        if self.reject:
            return 0, 10006, "rejected by dealer"
        t = self.next_ticket
        self.next_ticket += 1
        self.positions_book[t] = {
            "ticket": t, "magic": self.magic, "symbol": symbol,
            "type": 0 if side == "buy" else 1, "volume": lots,
            "price_open": self.fill if self.fill is not None else 2400.0,
            "price_current": 2400.0, "sl": sl, "tp": 0.0, "profit": 0.0,
        }
        return t, 10009, ""

    def find_fill(self, ticket, tries=3, delay=0.0):
        p = self.positions_book.get(int(ticket))
        return float(p["price_open"]) if p else None

    def modify_sl(self, ticket, new_sl, retries=3):
        self.calls.append(("modify", ticket, new_sl))
        if not self.modify_ok:
            return False
        if int(ticket) in self.positions_book:
            self.positions_book[int(ticket)]["sl"] = new_sl
        return True

    def close(self, ticket):
        self.calls.append(("close", ticket))
        return self.positions_book.pop(int(ticket), None) is not None

    def deal_price(self, ticket, days=3):
        return 2412.5


class RecordingDB:
    def __init__(self):
        self.events = []

    def enabled(self):
        return True

    def load_open_trades(self):
        return []

    def __getattr__(self, name):
        def rec(*a, **k):
            self.events.append((name, a, k))
        return rec


def build(tmpdir, scan_fn, symbols=("XAUUSD",), bridge=None, risk=100.0):
    cfg_path = os.path.join(tmpdir, "s99.json")
    with open(cfg_path, "w") as f:
        json.dump({"MAGIC": 99001, "MT5_BRIDGE_URL": "http://fake/1/demo",
                   "MT5_API_KEY": "ak_test", "RISK_PER_TRADE": risk,
                   "TRAIL_INTERVAL_SEC": 10}, f)

    s = Strategy(id="S99-TEST", label="S99 Test", symbols=list(symbols),
                 timeframes={"M5": 200, "M1": 300},
                 log_dir=os.path.join(tmpdir, "logs"))
    s.scan(scan_fn)
    s.build(config_file=cfg_path)
    s.bridge = bridge or FakeBridge()
    s.candles.bridge = s.bridge
    s.telegram = NullTelegram()
    s.db = RecordingDB()
    # rebuild the collaborators that captured the originals
    from engine.execution import Executor
    from engine.trailing import StopManager
    s.executor = Executor(s.book, s.bridge, s.db, s.telegram, s.journal, s.risk)
    s.stops = StopManager(s.book, s.bridge, s.db, s.telegram, {
        "flatten_lead_min": 10, "flatten_before_weekend": True,
        "flatten_before_daily_break": False})
    return s


def one_buy(ctx):
    idx = ctx.recent(10)
    return [Signal(symbol=ctx.symbol, side="buy",
                   event_id=ctx.event_id("buy", ctx.symbol, "fixed-ts"),
                   entry_price=2400.0, entry_dt=idx[-1], hard_sl=2395.0,
                   qty=50.0, row={"Event ID": "x", "Status": "Intrade"})]


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d


def test_signal_becomes_managed_position(tmp):
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")

    assert len(s.book) == 1, "a fresh Intrade signal must open exactly one position"
    pos = s.book.all()[0]
    # qty 50 on XAUUSD (contract size 100) -> 0.5 lots
    assert pos.qty == 0.5
    # stop corrected off the fill to put exactly $100 at risk:
    # 100 / (100 * 0.5) = 2.0 below a 2400 fill
    assert pos.hard_sl == pytest.approx(2398.0)
    assert pos.entry_price == 2400.0
    assert ("modify", pos.ticket, 2398.0) in s.bridge.calls


def test_order_is_placed_before_the_stop_is_known(tmp):
    """The order must carry the signal stop as a placeholder — never naked."""
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    place = next(c for c in s.bridge.calls if c[0] == "place")
    assert place[4] == 2395.0, "placement must carry the signal SL"


def test_missing_fill_still_corrects_the_stop(tmp):
    """Version 1's S21 left the placeholder on when the fill lookup failed."""
    b = FakeBridge()
    b.find_fill = lambda ticket, tries=3, delay=0.0: None
    s = build(tmp, one_buy, bridge=b)
    s.scan_symbol("XAUUSD")
    pos = s.book.all()[0]
    assert pos.hard_sl == pytest.approx(2398.0), \
        "the stop must be corrected from the signal price when no fill is seen"


def test_failed_stop_correction_keeps_the_placeholder_and_alerts(tmp):
    b = FakeBridge(modify_ok=False)
    s = build(tmp, one_buy, bridge=b)
    posted = []
    s.telegram = types.SimpleNamespace(post=posted.append)
    from engine.execution import Executor
    s.executor = Executor(s.book, s.bridge, s.db, s.telegram, s.journal, s.risk)
    s.scan_symbol("XAUUSD")
    pos = s.book.all()[0]
    assert pos.hard_sl == 2395.0, "a refused modify must not be recorded as applied"
    assert any("SL CORRECTION FAILED" in m for m in posted)


def test_rejected_order_is_recorded(tmp):
    """Version 1's S21 left rejected rows in SIGNAL status forever."""
    s = build(tmp, one_buy, bridge=FakeBridge(reject=True))
    s.scan_symbol("XAUUSD")
    assert len(s.book) == 0
    execs = [e for e in s.db.events if e[0] == "record_execution"]
    assert execs and execs[0][2].get("error"), "a rejection must record its error"


def test_an_event_fires_only_once(tmp):
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    s.scan_symbol("XAUUSD")
    s.scan_symbol("XAUUSD")
    assert len([c for c in s.bridge.calls if c[0] == "place"]) == 1, \
        "re-scanning the same setup must not place a second order"


def test_stale_entries_are_not_traded(tmp):
    def stale(ctx):
        return [Signal(symbol=ctx.symbol, side="buy",
                       event_id=ctx.event_id("buy", "old"),
                       entry_price=2400.0,
                       entry_dt=pd.Timestamp("2001-01-01", tz="UTC"),
                       hard_sl=2395.0, qty=50.0)]
    s = build(tmp, stale)
    s.scan_symbol("XAUUSD")
    assert len(s.book) == 0, "an entry outside the recent window must not trade"


def test_invalidated_setups_journal_but_never_trade(tmp):
    def invalid(ctx):
        return [Signal(symbol=ctx.symbol, side="buy",
                       event_id=ctx.event_id("buy", "inv"),
                       status="Invalidated due to a later setup",
                       entry_dt=ctx.recent(10)[-1], entry_price=2400.0,
                       hard_sl=2395.0, qty=50.0,
                       row={"Event ID": "inv", "Status": "Invalidated"})]
    s = build(tmp, invalid)
    s.scan_symbol("XAUUSD")
    assert len(s.book) == 0
    written = os.listdir(os.path.join(tmp, "logs"))
    assert any(f.endswith(".csv") for f in written), "the row must still be journalled"


def test_trailing_ratchets_indefinitely(tmp):
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    pos = s.book.all()[0]
    r = pos.r_unit                      # 2.0 after correction
    assert r == pytest.approx(2.0)

    # price reaches 1:14 — well past Version 1's precomputed 1:10 ceiling
    px = pos.entry_price + 14 * r
    s.stops.trail(s.bridge.positions(), "XAUUSD", px, low_px=px, high_px=px)
    want = pos.entry_price + 12 * r     # anchor lags two rungs
    assert s.bridge.positions_book[pos.ticket]["sl"] == pytest.approx(want)

    # and keeps going
    px = pos.entry_price + 30 * r
    s.stops.trail(s.bridge.positions(), "XAUUSD", px, low_px=px, high_px=px)
    assert s.bridge.positions_book[pos.ticket]["sl"] == pytest.approx(
        pos.entry_price + 28 * r)


def test_bogus_candle_extreme_cannot_walk_the_stop(tmp):
    """The 2026-07-12 USOIL failure: a degenerate extreme fired every rung."""
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    pos = s.book.all()[0]
    before = s.bridge.positions_book[pos.ticket]["sl"]
    s.stops.trail(s.bridge.positions(), "XAUUSD", 2400.0,
                  low_px=0.0001, high_px=999999.0)
    assert s.bridge.positions_book[pos.ticket]["sl"] == before


def test_foreign_magic_is_never_touched(tmp):
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    pos = s.book.all()[0]
    s.bridge.positions_book[pos.ticket]["magic"] = 12345      # someone else's
    before = s.bridge.positions_book[pos.ticket]["sl"]
    px = pos.entry_price + 14 * pos.r_unit
    s.stops.trail(s.bridge.positions(), "XAUUSD", px, low_px=px, high_px=px)
    assert s.bridge.positions_book[pos.ticket]["sl"] == before


def test_closed_position_is_reaped_with_an_exit_price(tmp):
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    pos = s.book.all()[0]
    del s.bridge.positions_book[pos.ticket]          # stopped out
    s.stops.run_pass(s.candles, 300)
    closes = [e for e in s.db.events if e[0] == "record_close"]
    assert closes, "a vanished position must be closed in the database"
    assert closes[0][2]["exit_price"] == 2412.5, \
        "the exit price must come from the broker's deal history"


def test_unreachable_bridge_closes_nothing(tmp):
    """A bridge outage must not be read as every position having closed."""
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    s.bridge.positions = lambda: None
    s.stops.run_pass(s.candles, 300)
    assert not [e for e in s.db.events if e[0] == "record_close"]


def test_recovery_readopts_open_positions(tmp):
    s = build(tmp, one_buy)
    s.scan_symbol("XAUUSD")
    pos = s.book.all()[0]

    from engine import recovery
    from engine.positions import Book
    fresh = Book()
    s.db.load_open_trades = lambda: [{
        "event_id": pos.event_id, "symbol": "XAUUSD", "side": "buy",
        "ticket": pos.ticket, "entry_price": pos.entry_price, "qty": pos.qty,
        "hard_sl": pos.hard_sl, "current_sl": pos.hard_sl, "trail_hit": {2, 3},
    }]
    n = recovery.recover(fresh, s.db, s.bridge, s.telegram)
    assert n == 1
    got = fresh.get(pos.ticket)
    assert got.trail_hit == {2, 3}, "rungs already taken must not be re-fired"
    assert got.r_unit == pytest.approx(pos.r_unit), \
        "the recovered ladder must match the live one"


def test_recovery_closes_positions_gone_while_offline(tmp):
    s = build(tmp, one_buy)
    from engine import recovery
    from engine.positions import Book
    s.db.load_open_trades = lambda: [{
        "event_id": "ghost", "symbol": "XAUUSD", "side": "buy",
        "ticket": 111, "entry_price": 2400.0, "qty": 0.5,
        "hard_sl": 2398.0, "current_sl": 2398.0, "trail_hit": set(),
    }]
    fresh = Book()
    assert recovery.recover(fresh, s.db, s.bridge, s.telegram) == 0
    assert [e for e in s.db.events if e[0] == "record_close"]


# --- singleton guard ----------------------------------------------------------

def test_refuses_to_start_when_already_running(tmp):
    """Two processes on one magic would place duplicate orders on live money."""
    s = build(tmp, one_buy)
    s.db.live_elsewhere = lambda max_age_sec=180: (True, "pid 999 on other-host")
    posted = []
    s.telegram = types.SimpleNamespace(post=posted.append)
    s._started = True

    s.run()

    assert not s.bridge.calls, "a refused start must not touch the bridge"
    assert any("START REFUSED" in m for m in posted), "the refusal must be alerted"


class _StartupReached(Exception):
    """Raised past the guard to stop run() without entering its scan loop."""


def _run_until_startup(s, monkeypatch, **kw):
    """Drive run() far enough to see whether the guard let it through."""
    monkeypatch.setattr(s, "_stop_loop", lambda: None)

    def boom():
        raise _StartupReached

    monkeypatch.setattr(s.journal, "load", boom)
    try:
        s.run(**kw)
    except _StartupReached:
        return True
    return False


def test_allow_duplicate_overrides_the_guard(tmp, monkeypatch):
    s = build(tmp, one_buy)
    s.db.live_elsewhere = lambda max_age_sec=180: (True, "pid 999 on other-host")
    s.telegram = types.SimpleNamespace(post=lambda *a: None)
    s._started = True
    assert _run_until_startup(s, monkeypatch, allow_duplicate=True), \
        "--allow-duplicate must get past the guard"


def test_database_outage_fails_open(tmp, monkeypatch):
    """Refusing to trade because the DB is down is the worse failure."""
    s = build(tmp, one_buy)
    s.db.live_elsewhere = lambda max_age_sec=180: (False, "check failed")
    s.telegram = types.SimpleNamespace(post=lambda *a: None)
    s._started = True
    assert _run_until_startup(s, monkeypatch), \
        "a failed check must not block startup"


def test_disabled_persistence_is_alerted_not_silent(tmp, monkeypatch):
    """The August 2026 incident: a broken root certificate on the strategy
    host disabled persistence for three strategies for a week. They kept
    trading, recorded nothing, never heartbeat, and would have orphaned every
    open position on restart. trade_db is fail-open by design; the silence
    was the problem."""
    s = build(tmp, one_buy)
    s.db.enabled = lambda: False
    s.db.live_elsewhere = lambda max_age_sec=180: (False, "disabled")
    posted = []
    s.telegram = types.SimpleNamespace(post=posted.append)
    s._started = True
    assert _run_until_startup(s, monkeypatch), "it must still start and trade"
    assert any("PERSISTENCE DISABLED" in m for m in posted), \
        "a strategy trading without persistence must say so loudly"


def test_allow_duplicate_config_key_relaxes_both_guards(tmp, monkeypatch):
    """A Version 2 strategy shadowing its Version 1 counterpart on a separate
    demo account sets ALLOW_DUPLICATE and is expected to start anyway."""
    import json as _json
    cfg = os.path.join(tmp, "dup.json")
    with open(cfg, "w") as f:
        _json.dump({"MAGIC": 99002, "MT5_BRIDGE_URL": "http://fake/2/demo",
                    "MT5_API_KEY": "ak", "ALLOW_DUPLICATE": True}, f)
    s = Strategy(id="S99-DUP", label="S99 Dup", symbols=["XAUUSD"],
                 timeframes={"M5": 200, "M1": 300},
                 log_dir=os.path.join(tmp, "duplogs"))
    s.scan(one_buy)
    s.build(config_file=cfg)
    s.telegram = types.SimpleNamespace(post=lambda *a: None)
    s.db = RecordingDB()
    s.db.live_elsewhere = lambda max_age_sec=180: (True, "another host")
    monkeypatch.setattr("engine.singleton.others_with_magic",
                        lambda magic, exclude_pid=None: [(1, "/live/old.py")])
    s._started = True
    assert _run_until_startup(s, monkeypatch), \
        "ALLOW_DUPLICATE must get past both the host and registry guards"


def test_strategy_id_suffix_separates_a_shadow_instance(tmp):
    """Two instances sharing a strategy_id do not merely look alike.

    `trades` is UNIQUE (strategy_id, event_id) and both derive the same
    deterministic event_id for the same setup, so the second one's INSERT is
    dropped by ON CONFLICT and its execution then overwrites the first one's
    row — with a ticket from a different account.
    """
    import json as _json
    cfg = os.path.join(tmp, "shadow.json")
    with open(cfg, "w") as f:
        _json.dump({"MAGIC": 99102, "MT5_BRIDGE_URL": "http://fake/9/demo",
                    "MT5_API_KEY": "ak", "STRATEGY_ID_SUFFIX": "-V2"}, f)
    s = Strategy(id="S99-TEST", label="S99 Test", symbols=["XAUUSD"],
                 timeframes={"M5": 200, "M1": 300},
                 log_dir=os.path.join(tmp, "shadowlogs"))
    s.scan(one_buy)
    s.build(config_file=cfg)
    assert s.id == "S99-TEST-V2", "the suffix must reach the registered id"
    assert s.magic == 99102
    assert "V2" in s.label


def test_no_suffix_leaves_the_id_untouched(tmp):
    s = build(tmp, one_buy)
    assert s.id == "S99-TEST"


def test_shadow_instance_gets_its_own_journal(tmp):
    """Sharing a log directory means sharing _fired_events.json, so each
    instance would skip the setups the other claimed first."""
    import json as _json
    cfg = os.path.join(tmp, "shadow2.json")
    with open(cfg, "w") as f:
        _json.dump({"MAGIC": 99103, "MT5_BRIDGE_URL": "http://fake/9/demo",
                    "MT5_API_KEY": "ak", "STRATEGY_ID_SUFFIX": "-V2"}, f)
    base = os.path.join(tmp, "bridge", "Some Strategy Logs")
    s = Strategy(id="S99-J", label="S99 J", symbols=["XAUUSD"],
                 timeframes={"M5": 200, "M1": 300}, log_dir=base)
    s.scan(one_buy)
    s.build(config_file=cfg)
    assert s.journal.dir.name.endswith("-V2"), \
        f"the shadow journal must be separate, got {s.journal.dir}"
    assert str(s.journal.dir) != base


def test_shadow_and_primary_ledgers_are_independent(tmp):
    """The concrete failure: one claiming an event must not silence the other."""
    from engine.journal import Journal
    primary = Journal(os.path.join(tmp, "logs"))
    shadow = Journal(os.path.join(tmp, "logs-V2"))
    ev = "shared-deterministic-event-id"
    assert primary.mark_fired(ev) is True
    assert shadow.has_fired(ev) is False, \
        "a shadow instance must still be able to trade the same setup"
    assert shadow.mark_fired(ev) is True


def test_run_does_not_rebuild_and_lose_the_config(tmp, monkeypatch):
    """make_strategy() builds eagerly to read the risk and wire Telegram.
    run() must not then rebuild with no config file — that lost the sidecar
    path and every strategy died on startup with "MAGIC is not set", holding
    a config that plainly had one."""
    s = build(tmp, one_buy)          # already built, with a config file
    s.db.live_elsewhere = lambda max_age_sec=180: (False, "clear")
    s.telegram = types.SimpleNamespace(post=lambda *a: None)
    magic_before = s.magic
    assert _run_until_startup(s, monkeypatch)
    assert s.magic == magic_before, "run() must not rebuild away the config"
