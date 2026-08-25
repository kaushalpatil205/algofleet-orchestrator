"""Rolling candle cache over the bridge.

Two behaviours carried over from Version 1, both load-bearing:

* **Warm then tail.** The first call for a (symbol, timeframe) pulls the full
  lookback; every call after that pulls only the last 10 bars and merges them
  in. A full re-fetch of 6000 M1 bars every minute, on five timeframes, is
  what made scans slow enough to overrun the minute boundary.

* **Serve stale on failure.** A failed fetch returns the cache unchanged
  rather than an empty frame. An empty frame reads downstream as "no data" and
  silently skips a scan; stale data is one minute old and still correct.

Timeframes the bridge does not serve are resampled from a base timeframe —
S21 does this for M3 (asking for 3x the M1 bars and aggregating). Version 1
hard-coded that single case inside S21's cache function; here any timeframe
can declare a base, so a strategy on M2 or M10 needs no new plumbing.

The cache is keyed by (symbol, timeframe) and written only by the scan thread
via `prefetch`, so the stop-management thread reading M1 through `get` never
races a partial write.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .notify import log

COLUMNS = ["open", "high", "low", "close", "volume"]

# Timeframes the bridge cannot serve, and what to build them from.
# {timeframe: (base timeframe, bars of base per bar, pandas resample rule)}
DERIVED = {
    "M2":  ("M1", 2,  "2min"),
    "M3":  ("M1", 3,  "3min"),
    "M10": ("M5", 2,  "10min"),
}

TAIL_REFRESH = 10      # bars re-fetched on a warm cache


def _empty():
    return pd.DataFrame(columns=["datetime"] + COLUMNS).set_index("datetime")


def _to_frame(candles):
    if not candles:
        return _empty()
    df = pd.DataFrame(candles)
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    if "tick_volume" in df.columns:
        df = df.rename(columns={"tick_volume": "volume"})
    elif "volume" not in df.columns:
        df["volume"] = 0
    df = df[["datetime"] + COLUMNS].sort_values("datetime").reset_index(drop=True)
    return df.set_index("datetime")


def _resample(df, rule):
    if df.empty:
        return _empty()
    out = df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    return out


class CandleCache:
    def __init__(self, bridge):
        self.bridge = bridge
        self._cache = {}
        self._lock = threading.Lock()

    def _fetch(self, symbol, timeframe, count):
        return _to_frame(self.bridge.candles(symbol, timeframe, count))

    def get(self, symbol, timeframe, lookback):
        """Rolling frame for one (symbol, timeframe), fetching as needed."""
        if timeframe in DERIVED:
            base_tf, per_bar, rule = DERIVED[timeframe]
            base = self.get(symbol, base_tf, lookback * per_bar)
            return _resample(base, rule).tail(lookback)

        key = (symbol, timeframe)
        with self._lock:
            cached = self._cache.get(key)

        if cached is None or cached.empty:
            df = self._fetch(symbol, timeframe, lookback)
            with self._lock:
                self._cache[key] = df
            return df

        fresh = self._fetch(symbol, timeframe, TAIL_REFRESH)
        if fresh is None or fresh.empty:
            return cached           # fetch failed — stale beats empty

        merged = pd.concat([cached.reset_index(), fresh.reset_index()])
        merged = (merged.drop_duplicates(subset=["datetime"], keep="last")
                        .sort_values("datetime")
                        .tail(lookback)
                        .set_index("datetime"))
        with self._lock:
            self._cache[key] = merged
        return merged

    def prefetch(self, symbol, timeframes):
        """Fetch several timeframes at once.

        Pass time collapses from the sum of the bridge calls to the slowest
        single one. Derived timeframes are resolved to their base first, so
        asking for M3 and M1 together fetches M1 once instead of twice.
        """
        wire = {}
        for tf, lookback in timeframes.items():
            if tf in DERIVED:
                base_tf, per_bar, _ = DERIVED[tf]
                wire[base_tf] = max(wire.get(base_tf, 0), lookback * per_bar)
            else:
                wire[tf] = max(wire.get(tf, 0), lookback)

        if not wire:
            return {}
        with ThreadPoolExecutor(max_workers=max(len(wire), 1)) as pool:
            futures = {tf: pool.submit(self.get, symbol, tf, lb)
                       for tf, lb in wire.items()}
        for tf, fut in futures.items():
            try:
                fut.result()
            except Exception as e:
                log(f"[CANDLES] prefetch {symbol} {tf} failed: {e}")

        return {tf: self.get(symbol, tf, lb) for tf, lb in timeframes.items()}

    def last_price(self, symbol, timeframe="M1", lookback=None):
        """(close, low, high) of the newest bar — what trailing evaluates on."""
        df = self.get(symbol, timeframe, lookback or TAIL_REFRESH)
        if df is None or df.empty:
            return None, None, None
        return (float(df["close"].iloc[-1]),
                float(df["low"].iloc[-1]),
                float(df["high"].iloc[-1]))
