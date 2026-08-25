"""Imports a live strategy with its I/O replaced, changing none of its bytes.

Ordering is the whole trick, and getting it wrong is not a subtle failure:

  1. build the candle source  — captures the *real* requests before it is shadowed
  2. seed sys.modules         — trade_db and requests, BEFORE the import
  3. chdir into the run dir   — the strategies mkdir "./bridge/..." at import
  4. import the strategy      — by path, via importlib
  5. patch module attributes  — datetime, _time, and the market-close guards
  6. preload the feed         — the last step allowed to touch the network
  7. install the socket guard — everything after this is sealed

Step 2 is the one that matters most. The real `trade_db.init()` runs at strategy
import time and falls back to a hardcoded CockroachDB URL when handed an empty
one, so a strategy imported without the stub already in place connects to
production, writes a `strategy_registry` row, and starts a heartbeat thread.
"""

import contextlib
import importlib.util
import os
import re
import socket
import sys

from .fakes import clock as clock_mod
from .fakes import http as fake_http
from .fakes import trade_db as fake_trade_db

LIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Live")


# --- strategy families --------------------------------------------------------

class Adapter:
    """What the harness needs to know that differs between strategy families."""

    def __init__(self, name, symbols, wire, lookbacks):
        self.name = name
        self.symbols = symbols          # symbols fetched over the wire
        self.wire = wire                # {timeframe: bars to preload}
        self.lookbacks = lookbacks      # {timeframe: bars} for stride sizing


def detect(mod):
    symbols = list(getattr(mod, "INSTRUMENTS", []) or
                   [getattr(mod, "SYMBOL_BRIDGE", None) or getattr(mod, "SYMBOL", None)])
    symbols = [s for s in symbols if s]

    if hasattr(mod, "LOOKBACK_15M"):
        # S21: 15m -> 3m -> 1m, but M3 never goes over the wire — the strategy
        # resamples it from M1 itself, asking for 3x the M3 lookback.
        m1 = max(int(getattr(mod, "LOOKBACK_1M", 6000)),
                 int(getattr(mod, "LOOKBACK_3M", 4000)) * 3)
        wire = {"M15": int(getattr(mod, "LOOKBACK_15M", 2500)), "M1": m1}
        return Adapter("S21", symbols, wire,
                       {"M15": int(getattr(mod, "LOOKBACK_15M", 2500)),
                        "M1": int(getattr(mod, "LOOKBACK_1M", 6000))})

    wire = {
        "M5": int(getattr(mod, "LOOKBACK_5M", 2500)),
        "M1": int(getattr(mod, "LOOKBACK_1M", 6000)),
        "H2": int(getattr(mod, "LOOKBACK_2H", 500)),
        "H4": int(getattr(mod, "LOOKBACK_4H", 300)),
        "D1": int(getattr(mod, "LOOKBACK_1D", 200)),
    }
    return Adapter("S17", symbols, wire,
                   {"M5": wire["M5"], "M1": wire["M1"]})


# --- isolation ----------------------------------------------------------------

class NetworkSealed:
    """Blocks socket creation for the duration of a replay.

    These scripts place real orders. A harness bug that let one reach the actual
    bridge would not be a wrong number in a report, so the failure mode here is
    a loud exception rather than a best-effort stub returning something benign.
    """

    def __init__(self):
        self._saved = None

    def __enter__(self):
        self._saved = socket.socket

        class Blocked(socket.socket):
            def __init__(self, *a, **kw):
                raise RuntimeError(
                    "backtest attempted a real network connection — "
                    "every strategy call should route through fakes.http")

        socket.socket = Blocked
        return self

    def __exit__(self, *exc):
        socket.socket = self._saved
        return False


@contextlib.contextmanager
def _chdir(path):
    prev = os.getcwd()
    os.makedirs(path, exist_ok=True)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


# --- loading ------------------------------------------------------------------

class LoadedStrategy:
    def __init__(self, module, adapter, feed, broker, router, clock, outdir):
        self.module = module
        self.adapter = adapter
        self.feed = feed
        self.broker = broker
        self.router = router
        self.clock = clock
        self.outdir = outdir
        self.trade_db = fake_trade_db

    def activate(self):
        """Run with the output dir as cwd.

        The strategies build every log path relative to `./bridge/...` — the
        directory is created at import, the CSV is written during the scan, and
        `log_trade_error` opens its own file later still. Holding cwd for the
        whole run redirects all of them at once, including any relative path not
        enumerated here, which rebinding known attributes would miss.
        """
        return _chdir(self.outdir)

    # The harness drives these directly and never calls main(), which loops
    # forever and spawns the real trailing thread. Driving them here keeps the
    # replay single-threaded and therefore deterministic.
    def scan(self):
        self.module.run_live_scan()

    def trail(self):
        self.module.run_trailing_pass()

    def recover(self):
        self.module.recover_open_trades()


def load(strategy_path, clock, source, outdir, h2_mode="true-h2",
         spread=0.0, slippage=0.0):
    from .fakes.broker import SimBroker
    from .feed import Feed

    strategy_path = os.path.abspath(strategy_path)
    if not os.path.exists(strategy_path):
        raise FileNotFoundError(strategy_path)

    # 2. seed sys.modules before anything imports the real thing
    fake_trade_db.reset()
    fake_trade_db.bind_clock(clock.now)
    sys.modules["trade_db"] = fake_trade_db
    sys.modules["requests"] = fake_http

    outdir = os.path.abspath(outdir)
    with _chdir(outdir):
        # 4. import by path
        name = "bt_" + re.sub(r"\W", "_", os.path.basename(strategy_path)[:-3])
        spec = importlib.util.spec_from_file_location(name, strategy_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)

    _assert_persistence_sealed()

    adapter = detect(mod)
    if not adapter.symbols:
        raise RuntimeError(f"could not determine symbols for {strategy_path}")

    feed = Feed(source, clock, h2_mode=h2_mode)
    broker = SimBroker(feed, clock, adapter.symbols[0], spread=spread,
                       slippage=slippage)
    router = fake_http.Router(feed, broker, symbol_default=adapter.symbols[0])
    fake_http.bind(router)

    # 5. patch the clock seams
    mod.datetime = clock_mod.datetime_shim(clock)
    mod._time = clock_mod.TimeShim(clock)
    _wrap_market_guards(mod, clock)

    return LoadedStrategy(mod, adapter, feed, broker, router, clock, outdir)


def _assert_persistence_sealed():
    """Prove the real trade_db never executed.

    Checking that sys.modules["trade_db"] is the stub only proves the name still
    points at it. This also walks loaded modules for one whose file is the real
    Live/trade_db.py — imported under any other name, it would already have
    opened a CockroachDB connection and registered a live strategy row.
    """
    if sys.modules.get("trade_db") is not fake_trade_db:
        raise RuntimeError("real trade_db was imported — persistence is not sealed")

    real = os.path.join(LIVE_DIR, "trade_db.py")
    for name, m in list(sys.modules.items()):
        path = getattr(m, "__file__", None) or ""
        if path and os.path.abspath(path) == os.path.abspath(real):
            raise RuntimeError(
                f"real trade_db executed as sys.modules[{name!r}] — "
                "a backtest may have written to the live database")


def _wrap_market_guards(mod, clock):
    """Feed simulated time to the market-close guards.

    They do `import datetime as _mdt` *inside* the function body and work in New
    York wall time, so rebinding mod.datetime cannot reach them. Both already
    take a `_now` override, so wrapping to supply it leaves their logic — the
    session table, the Friday 17:00 ET rule, the lead window — untouched.
    """
    for fname in ("is_market_closed", "market_close_flatten_due"):
        original = getattr(mod, fname, None)
        if original is None:
            continue

        def wrapped(*args, _orig=original, **kw):
            if kw.get("_now") is None and len(args) < 2:
                kw["_now"] = clock.now_ny()
            return _orig(*args, **kw)

        setattr(mod, fname, wrapped)
