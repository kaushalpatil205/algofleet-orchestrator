"""Historical candle feed.

Loads a window once from the Parquet archive built by mt5-orchestrator PR #31
(`feat/deep-historical-candles`), holds it in memory, and serves the rolling
slices a strategy asks for. All windowing is local, so the choice of transport
barely matters — one bulk read up front, then no I/O for the rest of the run.

Two things here are load-bearing:

*Rolling-window fidelity.* Live, a strategy only ever sees its `LOOKBACK_*`
most recent bars. KAMA, EMA and MACD are recursive, so handing it a longer
history silently changes indicator values and the backtest quietly stops
describing the live system. `window()` therefore serves exactly `count` bars
ending at simulated now — never more, even though more is loaded.

*Bridge wire shape.* The strategies parse the bridge's JSON, not the archive's
rows: `time` in epoch seconds and `tick_volume`, which `fetch_live_mt5_candles`
turns into a UTC index and a `volume` column. Emitting the archive's own
`ts`/`volume` keys instead makes the strategy take its `except` branch and
return an empty frame — a backtest that reports "no setups" rather than failing.
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

UTC = timezone.utc

# Timeframes derived by resampling a finer one.
#
# H2 is never archived — the worker's TF_MAP has no H2 at all — so it is always
# derived. The strategies already set that precedent: S21 resamples M3 from M1
# itself because "Exness bridge does not support M3 properly".
#
# H4 and D1 *can* be archived (TF_MAP has both) but fetch_history.py seeds
# ["M1","M5","M15","H1"] by default, so a stock archive has neither while S17
# asks for both. They are used as a fallback only, when the archive returns
# nothing for them.
#
# CAVEAT on the fallback: MT5 aligns H4 and D1 to *broker server* time — Exness
# runs UTC+2/+3 — while resampling here aligns to UTC, so derived bars sit 2-3
# hours off the real ones. Bounded in effect, since these feed only
# check_ema_position's reporting columns and never gate an entry, but seeding
# them properly is the correct fix:
#     python scripts/fetch_history.py --symbol BTCUSD --timeframe H4 --years 2
DERIVED = {"H2": ("H1", "2h"), "H4": ("H1", "4h"), "D1": ("H1", "1D")}

ALWAYS_DERIVED = {"H2"}

TF_SECONDS = {"M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800,
              "H1": 3600, "H2": 7200, "H4": 14400, "D1": 86400}

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum"}


# --- sources ------------------------------------------------------------------
# Each returns [{ts, open, high, low, close, volume}] with ts in epoch SECONDS,
# ascending. They run before the socket guard goes up, so the HTTP one is free
# to use the real network.

class ParquetArchiveSource:
    """Reads PR #31's layout directly: history/{symbol}/{timeframe}/{year}.parquet.

    Deliberately does not import `history_store` — the archive format is a
    stable on-disk contract, and reading it here keeps the harness usable from
    a checkout that has no mt5-orchestrator beside it.
    """

    def __init__(self, root=None):
        self.root = root or os.environ.get("HISTORY_LOCAL_ROOT", "")

    def load(self, symbol, timeframe, date_from, date_to):
        import pyarrow.parquet as pq

        base = os.path.join(self.root, symbol, timeframe)
        if not os.path.isdir(base):
            return []
        y0 = datetime.fromtimestamp(date_from, UTC).year
        y1 = datetime.fromtimestamp(date_to, UTC).year
        rows = []
        for name in sorted(os.listdir(base)):
            if not name.endswith(".parquet"):
                continue
            if not (y0 <= int(name[:-8]) <= y1):
                continue
            d = pq.read_table(os.path.join(base, name)).to_pydict()
            for i in range(len(d["ts"])):
                ts = d["ts"][i]
                if date_from <= ts <= date_to:
                    rows.append({"ts": ts, "open": d["open"][i], "high": d["high"][i],
                                 "low": d["low"][i], "close": d["close"][i],
                                 "volume": d["volume"][i]})
        rows.sort(key=lambda r: r["ts"])
        return rows


class HistoryStoreSource:
    """Delegates to mt5-orchestrator's history_store, which will pull missing
    year files from S3 first. Use when that repo is importable."""

    def __init__(self, module=None, orchestrator_path=None):
        if module is None:
            import sys
            if orchestrator_path:
                sys.path.insert(0, orchestrator_path)
            import history_store as module
        self.hs = module

    def load(self, symbol, timeframe, date_from, date_to):
        return self.hs.read_archive(symbol, timeframe, date_from, date_to)


class DashboardSource:
    """GET {base}/api/candles/range — the endpoint PR #31 added for exactly this.

    Note it returns `timestamp` in MILLISECONDS while history_store returns `ts`
    in seconds; normalising here is why the rest of the harness can stay
    unaware of which source it got.
    """

    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        # Captured at construction, which the runner does before seeding
        # sys.modules. Importing it lazily inside load() would pick up the fake
        # transport and route the archive read back into the harness.
        import requests
        self._requests = requests

    def load(self, symbol, timeframe, date_from, date_to):
        r = self._requests.get(f"{self.base}/api/candles/range",
                         params={"symbol": symbol, "timeframe": timeframe,
                                 "date_from": int(date_from), "date_to": int(date_to)},
                         timeout=300)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return [{"ts": int(c["timestamp"]) // 1000, "open": c["open"],
                 "high": c["high"], "low": c["low"], "close": c["close"],
                 "volume": c.get("volume", 0)} for c in r.json()]


class CsvSource:
    """Whatever CSVs already exist for the original backtests. Accepts the same
    shapes as reference/Strategy 21's load_ohlcv(): a datetime-ish column under
    any of several names, plus OHLC(V)."""

    def __init__(self, paths):
        self.paths = paths            # {(symbol, timeframe): path}

    def load(self, symbol, timeframe, date_from, date_to):
        path = self.paths.get((symbol, timeframe))
        if not path or not os.path.exists(path):
            return []
        df = pd.read_csv(path, sep=None, engine="python")
        df.columns = [str(c).strip().lower() for c in df.columns]
        for alias in ("date", "time", "timestamp", "open_time"):
            if alias in df.columns and "datetime" not in df.columns:
                df = df.rename(columns={alias: "datetime"})
                break
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
        if "volume" not in df.columns:
            df["volume"] = 0
        df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
        df = df.sort_values("datetime")
        out = []
        for r in df.itertuples(index=False):
            ts = int(r.datetime.timestamp())
            if date_from <= ts <= date_to:
                out.append({"ts": ts, "open": float(r.open), "high": float(r.high),
                            "low": float(r.low), "close": float(r.close),
                            "volume": float(r.volume)})
        return out


# --- feed ---------------------------------------------------------------------

class Feed:
    """In-memory candle server for one replay run."""

    def __init__(self, source, clock, h2_mode="true-h2"):
        self.source = source
        self.clock = clock
        self.h2_mode = h2_mode
        self.frames = {}              # (symbol, timeframe) -> DataFrame
        self.requested = []           # for coverage reporting
        self.warnings = []            # fidelity caveats worth surfacing

    def preload(self, symbol, wire, date_from, date_to):
        """Load every timeframe once, each with its own warmup.

        `wire` is {timeframe: bars}. Warmup is computed per timeframe rather
        than once from the worst case: D1's 200-bar lookback reaches back over a
        year, and applying that to M1 as well would load three quarters of a
        million minute bars nobody asked for.
        """
        for tf, bars in wire.items():
            start = warmup_start(date_from, tf, bars)
            self._load_one(symbol, tf, start, int(date_to))
        return self

    def _load_one(self, symbol, tf, date_from, date_to):
        if tf in ALWAYS_DERIVED:
            # Reproduce the live bug on request: the worker's
            # tf_map.get(timeframe, 16385) silently answers an unknown "H2" with
            # H1 bars, so today's live H2 columns are computed from H1 data. A
            # faithful replay of *current* production wants that, not fixed H2.
            base_tf, rule = (("H1", None) if self.h2_mode == "live-h1"
                             else DERIVED[tf])
            return self._derive(symbol, tf, base_tf, rule, date_from, date_to)

        rows = self.source.load(symbol, tf, date_from, date_to)
        if rows:
            return self._store(symbol, tf, tf, self._frame(rows), len(rows))

        if tf in DERIVED:
            base_tf, rule = DERIVED[tf]
            self.warnings.append(
                f"{symbol} {tf} is not in the archive — resampled from {base_tf}. "
                f"MT5 aligns {tf} to broker server time, so these bars are "
                f"offset from the real ones; seed it with "
                f"`scripts/fetch_history.py --symbol {symbol} --timeframe {tf}`")
            return self._derive(symbol, tf, base_tf, rule, date_from, date_to)

        return self._store(symbol, tf, tf, self._frame([]), 0)

    def _derive(self, symbol, tf, base_tf, rule, date_from, date_to):
        rows = self.source.load(symbol, base_tf, date_from, date_to)
        df = self._frame(rows)
        if rule is not None and not df.empty:
            df = df.resample(rule).agg(_AGG).dropna()
        return self._store(symbol, tf, base_tf, df, len(rows))

    def _store(self, symbol, tf, base_tf, df, source_rows):
        self.requested.append((symbol, tf, base_tf, source_rows))
        self.frames[(symbol, tf)] = df
        return df

    @staticmethod
    def _frame(rows):
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                index=pd.DatetimeIndex([], tz=UTC, name="datetime"))
        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True)
        df = df[["datetime", "open", "high", "low", "close", "volume"]]
        df = df.drop_duplicates(subset=["datetime"], keep="last")
        return df.sort_values("datetime").set_index("datetime")

    def window(self, symbol, timeframe, count):
        """The last `count` bars at or before simulated now.

        `<=` rather than `<` on purpose: MT5 hands back the currently-forming
        bar as the final element, and the strategies rely on it — `recent_1m` is
        the tail of the 1m frame and gates which setup is considered new.
        """
        df = self.frames.get((symbol, timeframe))
        if df is None or df.empty:
            return df if df is not None else self._frame([])
        return df[df.index <= self.clock.now()].tail(int(count))

    def payload(self, symbol, timeframe, count):
        """The window in the bridge's wire shape."""
        df = self.window(symbol, timeframe, count)
        candles = [{"time": int(ts.timestamp()), "open": float(r.open),
                    "high": float(r.high), "low": float(r.low),
                    "close": float(r.close), "tick_volume": int(r.volume)}
                   for ts, r in zip(df.index, df.itertuples(index=False))]
        return {"symbol": symbol, "timeframe": timeframe, "candles": candles}

    def coverage(self, symbol, timeframe):
        df = self.frames.get((symbol, timeframe))
        if df is None or df.empty:
            return None, None, 0
        return df.index[0], df.index[-1], len(df)


def warmup_start(date_from, timeframe, bars):
    """Push one timeframe's load window back far enough that the first scanned
    bar already has a full lookback behind it.

    Calendar span is always wider than bar count implies — weekends, holidays,
    the daily break — so this over-reaches rather than computing exactly.
    """
    return int(date_from - TF_SECONDS.get(timeframe, 300) * bars * 2.5 - 86400)


def scan_strides(date_from, date_to, lookbacks, overlap=0.25):
    """`as_of` points for a strided Stage 1 sweep.

    A single scan cannot cover an arbitrary window: the strategy only ever sees
    `LOOKBACK_1M` one-minute bars, which at 6000 is about 4.2 days. Sweeping a
    longer period means stepping `as_of` forward in strides shorter than that
    and merging results by Event ID. Without this a run over "a year" silently
    reports only its final four days.
    """
    span = min(TF_SECONDS.get(tf, 300) * n for tf, n in lookbacks.items())
    stride = max(int(span * (1.0 - overlap)), 3600)
    out, t = [], int(date_from)
    while t < int(date_to):
        t = min(t + stride, int(date_to))
        out.append(datetime.fromtimestamp(t, UTC))
    return out
