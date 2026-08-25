"""The strategy runtime: what every live strategy shares.

A strategy declares what it is and how to scan; this runs it.

    from engine import Strategy, Signal

    strategy = Strategy(
        id="S22-MYIDEA-XAUUSD",
        label="S22 My Idea · XAUUSD",
        symbols=["XAUUSD"],
        timeframes={"M5": 2500, "M1": 6000},
        log_dir="./bridge/Strategy 22 Logs",
    )

    @strategy.scan
    def scan(ctx):
        df5 = ctx.candles("M5")
        ...
        return [Signal(symbol=ctx.symbol, side="buy", event_id=ctx.event_id("buy", ts),
                       entry_price=px, entry_dt=dt, hard_sl=sl, qty=q, row=row)]

    if __name__ == "__main__":
        strategy.run()

THE TWO THREADS. Signal scanning runs on the main thread once per wall-clock
minute; stop management runs on its own thread every TRAIL_INTERVAL_SEC.
Version 1 originally ran stops inside the scan, so one slow candle fetch
delayed stop management for every open position and a scan that overran the
minute skipped it entirely. They are decoupled here for that reason, and the
scan keys off a minute tuple rather than a "first N seconds" window so a long
pass cannot silently skip a whole minute.
"""

import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import recovery, sessions, singleton
from .bridge import Bridge
from .candles import CandleCache
from .config import Config
from .db import TradeDB
from .execution import Executor
from .journal import Journal
from .notify import Telegram, log, stamped
from .positions import Book
from .trailing import StopManager


@dataclass
class Signal:
    """One setup a scan produced.

    A signal with `status != "Intrade"` is journalled but never traded — that
    is how invalidated setups still reach the CSV.
    """
    symbol: str
    side: str
    event_id: str
    status: str = "Intrade"
    entry_price: Optional[float] = None
    entry_dt: Any = None
    hard_sl: Optional[float] = None
    qty: Optional[float] = None
    lots: Optional[float] = None       # set to bypass contract-size conversion
    row: Optional[Dict[str, Any]] = None
    csv: Optional[str] = None
    fresh: Optional[bool] = None       # override the entry-recency check

    @property
    def tradeable(self):
        return self.status == "Intrade"


class ScanContext:
    """What a scan hook is handed: candles already fetched, and the helpers
    it needs to build deterministic event ids."""

    def __init__(self, strategy, symbol, frames):
        self.strategy = strategy
        self.symbol = symbol
        self._frames = frames

    def candles(self, timeframe, symbol=None):
        """A prefetched frame. Asking for a timeframe the strategy did not
        declare fetches it rather than failing, but declaring it is faster —
        declared timeframes are fetched in parallel."""
        if symbol in (None, self.symbol) and timeframe in self._frames:
            return self._frames[timeframe]
        lookback = self.strategy.timeframes.get(timeframe, 1000)
        return self.strategy.candles.get(symbol or self.symbol, timeframe, lookback)

    def recent(self, n=10, timeframe="M1"):
        """Index of the last n bars — used to decide whether an entry is new
        enough to act on."""
        df = self.candles(timeframe)
        if df is None or df.empty:
            return []
        return df.index[-n:]

    @staticmethod
    def event_id(*parts):
        """Deterministic id for a setup.

        sha256 of the parts joined by '|', truncated to 24 chars — the same
        construction Version 1 used. It must stay deterministic: the fired
        ledger dedupes on it, and backtest parity joins live rows to replayed
        ones by it.
        """
        raw = "|".join("" if p is None else str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def now(self):
        return datetime.now(timezone.utc)


class Strategy:
    def __init__(self, id, label=None, symbols=(), timeframes=None, sides=("buy",),
                 log_dir=None, csv_name=None, recent_bars=10, scan_sleep=5,
                 config_file=None, comment=None):
        self.id = id
        self.label = label or id
        self.symbols = list(symbols)
        self.timeframes = dict(timeframes or {"M1": 6000})
        self.sides = list(sides)
        self.recent_bars = recent_bars
        self.scan_sleep = scan_sleep
        self.csv_name = csv_name or (lambda sym, side: f"{id}_{sym}_{side.upper()}.csv")
        self._log_dir = log_dir or f"./bridge/{id} Logs"
        self._config_file = config_file
        self._comment = comment
        self._scan_fn = None
        self._enrich_fn = None
        self._built = False
        self._started = False

    # --- registration -----------------------------------------------------

    def scan(self, fn):
        """Decorator registering the scan hook."""
        self._scan_fn = fn
        return fn

    def enrich(self, fn):
        """Decorator registering an optional row-enrichment hook.

        Called as fn(signal, position) after the engine has stamped the ticket
        onto the journal row, for strategies that want to add their own live
        columns — S17 re-runs its ratio simulation against the actual fill so
        the CSV carries both the theoretical and the executed result.
        """
        self._enrich_fn = fn
        return fn

    # --- wiring -----------------------------------------------------------

    def build(self, config_file=None):
        """Construct every collaborator. Separated from run() so tests and
        the backtest harness can build a strategy without starting threads."""
        # Remember which sidecar was used. make_strategy() builds eagerly to
        # read the risk and wire the Telegram poster, and run() must not then
        # rebuild from nothing — doing so lost the config path and every
        # strategy died on startup with "MAGIC is not set", holding a config
        # file that plainly had one.
        self._config_file = config_file or self._config_file
        cfg = (Config(strategy_path=self._config_file)
               if self._config_file else Config())
        self.config = cfg

        # STRATEGY_ID_SUFFIX turns a strategy into a separate INSTANCE of
        # itself — a Version 2 copy shadowing its Version 1 counterpart, say.
        # It is not cosmetic. `trades` is UNIQUE (strategy_id, event_id) and
        # both copies derive the same deterministic event_id for the same
        # setup, so two instances sharing a strategy_id do not merely look
        # alike: the second one's INSERT is dropped by ON CONFLICT and its
        # execution then OVERWRITES the first one's row with a ticket from a
        # different account. The suffix is what keeps their records apart.
        suffix = cfg.get("STRATEGY_ID_SUFFIX", str, default="")
        if suffix:
            self.id = f"{self.id}{suffix}"
            self.label = f"{self.label} {suffix.lstrip('-_')}"
            # The journal has to move with it. The log directory holds
            # _fired_events.json — the record of which setups have already
            # been traded — and two instances sharing one would each skip the
            # setups the other claimed first, so each would trade roughly half
            # the signals and neither run would mean anything. The CSVs would
            # collide too: both derive the same event ids, and the journal
            # merges rows by event id.
            self._log_dir = f"{self._log_dir.rstrip('/')}{suffix}"

        self.magic = cfg.get("MAGIC", int, required=True)
        self.risk = cfg.get("RISK_PER_TRADE", float, default=100.0)
        self.trail_interval = cfg.get("TRAIL_INTERVAL_SEC", int, default=10)

        self.telegram = Telegram(cfg.get("BOT_TOKEN", str, default=""),
                                 cfg.get("CHAT_ID", str, default=""))
        self.journal = Journal(self._log_dir)
        self.bridge = Bridge(
            cfg.get("MT5_BRIDGE_URL", str, required=True),
            cfg.get("MT5_API_KEY", str, required=True),
            magic=self.magic,
            comment=self._comment or f"{self.id[:24]}",
            error_sink=self.journal.error,
        )
        self.candles = CandleCache(self.bridge)
        self.book = Book()
        self.db = TradeDB(self.id, cfg.get("TRADE_DB_URL", str, default=""),
                          magic=self.magic,
                          bridge_url=cfg.get("MT5_BRIDGE_URL", str, default=""),
                          label=self.label, symbols=self.symbols)
        self.executor = Executor(self.book, self.bridge, self.db, self.telegram,
                                 self.journal, self.risk)
        self.stops = StopManager(self.book, self.bridge, self.db, self.telegram, {
            "flatten_lead_min": cfg.get("FLATTEN_LEAD_MIN", int, default=10),
            "flatten_before_weekend": cfg.get("FLATTEN_BEFORE_WEEKEND", bool, default=True),
            "flatten_before_daily_break": cfg.get("FLATTEN_BEFORE_DAILY_BREAK", bool, default=False),
        })
        self._built = True
        return self

    # --- scanning ---------------------------------------------------------

    def scan_symbol(self, symbol):
        """One scan pass for one symbol: fetch, hook, journal, execute."""
        if self._scan_fn is None:
            raise RuntimeError(f"{self.id} declared no scan hook — "
                               f"decorate one with @strategy.scan")
        frames = self.candles.prefetch(symbol, self.timeframes)
        missing = [tf for tf, df in frames.items() if df is None or df.empty]
        if missing:
            log(f"[SCAN] {symbol}: no data for {', '.join(missing)} — skipping")
            return []

        ctx = ScanContext(self, symbol, frames)
        signals = self._scan_fn(ctx) or []
        self._handle(ctx, signals)
        return signals

    def _is_fresh(self, ctx, signal):
        """Is this entry new enough to act on?

        A scan re-derives every setup in its lookback on every pass, so
        without this a restart would fire orders for setups hours old.
        """
        if signal.fresh is not None:
            return signal.fresh
        if signal.entry_dt is None:
            return False
        recent = ctx.recent(self.recent_bars)
        try:
            return signal.entry_dt in recent
        except Exception:
            return False

    def _handle(self, ctx, signals):
        by_csv = {}
        for sig in signals:
            if sig.tradeable and self._is_fresh(ctx, sig) \
                    and not self.journal.has_fired(sig.event_id):
                try:
                    self.executor.execute(sig)
                except Exception as e:
                    log(f"[EXEC] {sig.symbol} {sig.event_id} failed: {e}")
                    self.telegram.post(f"⚠️ EXECUTION ERROR\n{sig.symbol} "
                                       f"{sig.side.upper()}\n{e}")

            if sig.row is not None:
                self._enrich(sig)
                name = sig.csv or self.csv_name(sig.symbol, sig.side)
                by_csv.setdefault(name, []).append(sig.row)

        for name, rows in by_csv.items():
            self.journal.write_rows(name, rows)

    def _enrich(self, signal):
        """Stamp the live ticket back onto the journal row."""
        signal.row.setdefault("Event ID", signal.event_id)
        signal.row["Logged At UTC"] = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S UTC}"
        pos = self.book.by_event(signal.event_id)
        if pos is None:
            return
        signal.row["Live MT5 Order ID"] = pos.ticket
        signal.row["Live Entry Price"] = pos.entry_price
        signal.row["Live Corrected Hard SL"] = pos.hard_sl
        signal.row["Live Exit Datetime"] = pos.exit_datetime or ""
        if self._enrich_fn is not None:
            try:
                self._enrich_fn(signal, pos)
            except Exception as e:
                log(f"[ENRICH] {signal.event_id} failed: {e}")

    # --- threads ----------------------------------------------------------

    def _stop_loop(self):
        lookback_1m = self.timeframes.get("M1", 6000)
        while True:
            try:
                self.stops.run_pass(self.candles, lookback_1m)
            except Exception as e:
                log(f"[STOPS] pass failed: {e}")
            time.sleep(self.trail_interval)

    def run(self, config_file=None, allow_duplicate=False):
        if not self._built or config_file:
            self.build(config_file)
        self._started = True

        # ALLOW_DUPLICATE (config or env) turns both guards off for a strategy
        # that is MEANT to run beside another on the same magic — a Version 2
        # instance shadowing its Version 1 counterpart so the two can be
        # compared. That is only sane on a demo account, and only when the two
        # trade DIFFERENT accounts; on one account both copies place real
        # orders against the same balance and neither result means anything.
        if self.config.get("ALLOW_DUPLICATE", bool, default=False):
            allow_duplicate = True

        # Two guards, because they fail differently.
        #
        # The host check asks the operating system whether another process is
        # already stamping this magic. It catches a Version 1 script and a
        # Version 2 declaration colliding during a cutover, and it keeps
        # working when the database does not — which is the case that matters,
        # since a strategy with broken persistence does not heartbeat and
        # therefore looks dead to the registry check below.
        clash = singleton.others_with_magic(self.magic)
        if clash and not allow_duplicate:
            detail = singleton.describe(clash)
            log(f"[STARTUP] magic {self.magic} is ALREADY IN USE on this host: {detail}")
            log("[STARTUP] refusing to start — both processes would stamp the "
                "same magic and place duplicate orders. Stop the other one first.")
            self.telegram.post(
                f"⛔ {self.label} — START REFUSED\n"
                f"Magic {self.magic} already in use on this host:\n{detail}\n"
                f"A second copy would duplicate every order.")
            return
        if clash:
            log(f"[STARTUP] magic {self.magic} also used by "
                f"{singleton.describe(clash)} — starting anyway, duplicates allowed")
            self.telegram.post(
                f"⚠️ {self.label} — RUNNING AS A DUPLICATE\n"
                f"Magic {self.magic} is also stamped by:\n"
                f"{singleton.describe(clash)}\n"
                f"Account {self.config.get('MT5_BRIDGE_URL', str, default='?').rstrip('/').split('/')[-2:]}\n"
                f"Both copies will place orders. This is only meaningful if "
                f"they trade different accounts.")

        # Refuse to become a second copy of a strategy that is already running.
        # Two processes on one magic both scan the same candles, both fire on
        # the same setup, and place duplicate orders on a live account. Version
        # 2 lets CI create and start Cronicle events, so this is the guard that
        # makes that safe. --allow-duplicate overrides it for a deliberate
        # hand-off; a database outage fails open and trades.
        running, detail = self.db.live_elsewhere()
        if running and not allow_duplicate:
            log(f"[STARTUP] {self.id} is ALREADY RUNNING: {detail}")
            log("[STARTUP] refusing to start a second copy — it would place "
                "duplicate orders. Stop the other process first, or pass "
                "--allow-duplicate if you are certain.")
            self.telegram.post(
                f"⛔ {self.label} — START REFUSED\nAlready running: {detail}\n"
                f"A second copy would duplicate every order.")
            return
        if running:
            log(f"[STARTUP] {self.id} appears to be running ({detail}) — "
                f"starting anyway, --allow-duplicate was given")

        log("=" * 60)
        log(f"🚀 {self.label} — LIVE")
        log(f"   magic={self.magic}  symbols={', '.join(self.symbols)}  "
            f"risk=${self.risk}  trail={self.trail_interval}s")
        log("=" * 60)

        # Persistence failing is not a quiet condition. trade_db is fail-open
        # by design — a database outage must never stop trading — but that
        # silence is exactly how a broken root certificate on the strategy
        # host went unnoticed for a week in August 2026: three strategies kept
        # trading while recording nothing, never heartbeating, and would have
        # orphaned every open position on their next restart because
        # load_open_trades() had nothing to return. Trading continues; being
        # quiet about it does not.
        if not self.db.enabled():
            log("[STARTUP] *** PERSISTENCE IS DISABLED *** — this strategy will "
                "trade but record nothing, will not heartbeat, and will NOT "
                "re-adopt its open positions if restarted.")
            self.telegram.post(
                f"🚨 {self.label} — PERSISTENCE DISABLED\n"
                f"Trading continues, but nothing is being recorded and a "
                f"restart would orphan any open position. Fix the database "
                f"connection before restarting this strategy.")

        self.journal.load()
        recovery.recover(self.book, self.db, self.bridge, self.telegram)
        self.telegram.post(f"🚀 {self.label} — STARTED\n"
                           f"Symbols: {', '.join(self.symbols)}\n"
                           f"Magic: {self.magic}\nRisk: ${self.risk}")

        threading.Thread(target=self._stop_loop, daemon=True,
                         name="stop-mgmt").start()
        log(f"[STOPS] stop-management thread started (every {self.trail_interval}s)")

        last_key = None
        try:
            while True:
                now = datetime.now(timezone.utc)
                key = (now.year, now.month, now.day, now.hour, now.minute)
                if key != last_key:
                    stamped("scanning...")
                    for symbol in self.symbols:
                        try:
                            if sessions.is_market_closed(symbol):
                                continue
                            self.scan_symbol(symbol)
                        except Exception as e:
                            log(f"[SCAN] {symbol} failed: {e}")
                    last_key = key
                time.sleep(self.scan_sleep)
        except KeyboardInterrupt:
            log("Stopped by user.")

    def cli(self):
        """Entry point for the strategy files: `python <strategy>.py`."""
        import argparse
        ap = argparse.ArgumentParser(description=self.label)
        ap.add_argument("--allow-duplicate", action="store_true",
                        help="start even if another process is already running "
                             "this strategy (it will place duplicate orders)")
        args = ap.parse_args()
        self.run(allow_duplicate=args.allow_duplicate)
