"""Synthetic candles written in PR #31's archive layout.

Lets the harness be exercised end to end before `feat/deep-historical-candles`
merges and before anyone has seeded a real archive. The on-disk contract —
history/{symbol}/{timeframe}/{year}.parquet, ts in epoch seconds, int64 volume —
is copied from history_store.SCHEMA so a fixture and a real archive are
interchangeable to everything downstream.
"""

import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

UTC = timezone.utc

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum"}

RESAMPLE = {"M5": "5min", "M15": "15min", "M30": "30min",
            "H1": "1h", "H4": "4h", "D1": "1D"}


def minute_walk(start, end, seed=7, price=62000.0, vol=0.0009):
    """A random walk on 1-minute bars, shaped enough to form real setups.

    Trend segments rather than pure noise: the strategies look for MACD cycles
    and KAMA/BB structure, and a driftless walk produces far fewer of them than
    any real market would.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq="1min", tz=UTC, inclusive="left")
    n = len(idx)

    # Piecewise drift: ~6h segments with alternating bias. The per-segment sigma
    # is divided by the segment length because the value is applied to every bar
    # in the segment — without that the drift compounds across hundreds of
    # segments and a four-month series wanders by more than an order of
    # magnitude, which no amount of realistic volatility would produce.
    seg = 360
    drift = np.repeat(rng.normal(0, vol * 0.35 / seg, size=(n // seg) + 1), seg)[:n]
    steps = rng.normal(0, vol, size=n) + drift
    close = price * np.exp(np.cumsum(steps))

    open_ = np.concatenate([[price], close[:-1]])
    spread = np.abs(rng.normal(0, vol * 0.6, size=n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(50, 5000, size=n)

    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volume}, index=idx)


def write_archive(root, symbol, df_m1, timeframes=("M1", "M5", "M15", "H1", "H4", "D1")):
    """Derive each timeframe from the 1m series and write per-year Parquet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([("ts", pa.int64()), ("open", pa.float64()),
                        ("high", pa.float64()), ("low", pa.float64()),
                        ("close", pa.float64()), ("volume", pa.int64())])

    written = {}
    for tf in timeframes:
        df = df_m1 if tf == "M1" else df_m1.resample(RESAMPLE[tf]).agg(_AGG).dropna()
        for year, chunk in df.groupby(df.index.year):
            path = os.path.join(root, symbol, tf, f"{year}.parquet")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            table = pa.Table.from_pydict({
                "ts": [int(t.timestamp()) for t in chunk.index],
                "open": chunk["open"].astype(float).tolist(),
                "high": chunk["high"].astype(float).tolist(),
                "low": chunk["low"].astype(float).tolist(),
                "close": chunk["close"].astype(float).tolist(),
                "volume": chunk["volume"].astype("int64").tolist(),
            }, schema=schema)
            pq.write_table(table, path, compression="zstd")
            written[(tf, year)] = table.num_rows
    return written


def build(root, symbol="BTCUSD", start="2026-03-01", end="2026-07-02", seed=7):
    df = minute_walk(datetime.fromisoformat(start).replace(tzinfo=UTC),
                     datetime.fromisoformat(end).replace(tzinfo=UTC), seed=seed)
    return write_archive(root, symbol, df)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/bt-archive"
    counts = build(out)
    for (tf, year), n in sorted(counts.items()):
        print(f"{tf:<4}{year}  {n:>9,} bars")
    print(f"\nwrote {out}")
