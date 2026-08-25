"""Prove the Version 2 strategies produce the same setups as Version 1.

The migration moved code without changing it, but "without changing it" is a
claim, and these strategies trade real money. So this drives the Version 1
script's own row pipeline and the Version 2 one over identical candles and
compares what comes out, column by column.

Version 1 is loaded the way the CI probe loads it: trade_db, requests and the
network are all replaced first, so importing a live strategy here cannot reach
the bridge or production CockroachDB.
"""

import importlib.util
import json
import os
import socket
import sys
import tempfile
import types

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "Live"))
sys.path.insert(0, os.path.join(ROOT, "Live", "Strategy 17"))

pytest.importorskip("talib")

PAIRS = [
    # (Version 1 script, Version 2 script, side, variation)
    ("Bridge-S17-M2-M3-V4-XAUUSD-Buy-Live.py", "s17_m2m3_v4_xauusd_buy.py", "buy", 4),
    ("Bridge-S17_M3_M2_V1_XAUUSD_SELL_Live.py", "s17_m3m2_v1_xauusd_sell.py", "sell", 1),
    ("Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live.py", "s17_m3m2_v1_btcusd_sell.py", "sell", 1),
    ("Bridge-Strategy-17-M1-M2-Variation-4-BTCUSDT-Buy-Live.py",
     "s17_m1m2_v4_btcusd_buy.py", "buy", 4),
    ("Bridge-S17-M1-M2-V1-Forex-Live-Sell.py", "s17_m1m2_v1_forex_sell.py", "sell", 1),
    ("Bridge-S17-M2-M3-V4-Forex-Live-Buy.py", "s17_m2m3_v4_forex_buy.py", "buy", 4),
]
S17DIR = os.path.join(ROOT, "Live", "Strategy 17")


def _stub_trade_db():
    m = types.ModuleType("trade_db")
    m.init = lambda *a, **k: None
    m.enabled = lambda: False
    m.load_open_trades = lambda: []
    for n in ("record_signal", "record_execution", "record_trail", "record_close",
              "mark_telegram_sent", "record_recovery_event"):
        setattr(m, n, lambda *a, **k: None)
    return m


def _stub_requests():
    m = types.ModuleType("requests")

    class Resp:
        status_code, text = 200, "{}"
        def json(self): return {"candles": []}
        def raise_for_status(self): pass

    m.get = m.post = lambda *a, **k: Resp()
    m.exceptions = types.SimpleNamespace(RequestException=IOError,
                                         HTTPError=IOError, Timeout=IOError)
    return m


def load_v1(filename):
    """Import a Version 1 strategy with everything external sealed off."""
    sys.modules["trade_db"] = _stub_trade_db()
    sys.modules["requests"] = _stub_requests()
    saved = socket.socket

    class Blocked(socket.socket):
        def __init__(self, *a, **k):
            raise RuntimeError("parity test attempted a real network connection")

    socket.socket = Blocked
    cwd = os.getcwd()
    try:
        os.chdir(tempfile.mkdtemp(prefix="parity-v1-"))
        path = os.path.join(S17DIR, filename)
        spec = importlib.util.spec_from_file_location("v1_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        socket.socket = saved
        os.chdir(cwd)


def candles(n, seed, start, scale, freq):
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(0, scale, n))
    high = close + np.abs(rng.normal(0, scale, n))
    low = close - np.abs(rng.normal(0, scale, n))
    op = close + rng.normal(0, scale / 2, n)
    return pd.DataFrame(
        {"open": op, "high": np.maximum.reduce([op, high, close]),
         "low": np.minimum.reduce([op, low, close]), "close": close,
         "volume": rng.integers(50, 500, n).astype(float)},
        index=pd.date_range("2026-05-01", periods=n, freq=freq, tz="UTC"))


@pytest.fixture(scope="module")
def frames():
    """One candle set, resampled consistently across the five timeframes."""
    m1 = candles(6000, 42, 2400.0, 0.6, "1min")
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    return {
        "M1": m1,
        "M5": m1.resample("5min").agg(agg).dropna(),
        "H2": m1.resample("2h").agg(agg).dropna(),
        "H4": m1.resample("4h").agg(agg).dropna(),
        "D1": m1.resample("1D").agg(agg).dropna(),
    }


class FakeCtx:
    """What engine.runtime hands a scan hook, without the engine running."""

    def __init__(self, symbol, frames):
        self.symbol = symbol
        self._frames = frames

    def candles(self, timeframe, symbol=None):
        return self._frames[timeframe]

    def recent(self, n=10, timeframe="M1"):
        return self._frames[timeframe].index[-n:]

    @staticmethod
    def event_id(*parts):
        import hashlib
        raw = "|".join("" if p is None else str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


def v1_rows(mod, frames, side, variation, symbol):
    """Every setup Version 1 finds, using that module's OWN functions.

    This deliberately stops before Version 1's inline dedupe/invalidation
    block, so the comparison covers the strategy logic — the 5-minute setup
    scan, both entry methods, the hard stop, the row builder and the ratio
    simulation — rather than a paraphrase of twenty lines of bookkeeping.
    Dedupe is covered by its own test below.
    """
    df5, df1 = mod.prepare_df(frames["M5"]), mod.prepare_df(frames["M1"])
    df2h = mod.prepare_df_tf(frames["H2"])
    df4h = mod.prepare_df_tf(frames["H4"])
    df1d = mod.prepare_df_tf(frames["D1"])
    arr5, arr1 = mod.make_fast_arrays(df5), mod.make_fast_arrays(df1)
    arr2h, arr4h, arr1d = (mod.make_tf_arrays(df2h), mod.make_tf_arrays(df4h),
                           mod.make_tf_arrays(df1d))
    cycles1 = mod.build_cycle_map(arr1)
    volcalc1 = mod.VolumeCalculator(frames["M1"])
    rows = []
    for s5 in mod.scan_5m_final_strategy_candles(df5, side):
        row = mod.process_variation1_setup(s5, arr5, arr1, cycles1, volcalc1,
                                           arr2h, arr4h, arr1d, side)
        # the two columns Version 1 adds inline in run_live_scan_for_instrument,
        # reproduced here so both sides have run the same steps
        row = _v1_contract_qty(row, symbol)
        row["Final Strategy Candle Datetime"] = mod._s(
            s5.get("fcc_ts") or s5.get("cycle_end_ts"))
        rows.append(row)
    return rows


def _v1_contract_qty(row, symbol):
    """Version 1's inline 'Trading qty Contract' block, verbatim."""
    from collections import OrderedDict
    _clean = symbol.upper().replace("_", "")
    _c_size = 1
    if "XAUUSD" in _clean: _c_size = 100
    elif "BTC" in _clean: _c_size = 1
    elif "JPY" in _clean: _c_size = 100000
    elif "OIL" in _clean or "WTICOUSD" in _clean or "BCOUSD" in _clean: _c_size = 1000
    elif "EURUSD" in _clean: _c_size = 100000
    out = OrderedDict()
    for k, v in row.items():
        out[k] = v
        if k == "Qty":
            q = row.get("Qty")
            if q not in (None, "", "None") and not pd.isna(q):
                try:
                    lots = float(q) / _c_size
                    if "JPY" in _clean:
                        rate = float(row.get("Entry Price") or 0)
                        if rate > 0:
                            lots *= rate
                    out["Trading qty Contract"] = max(round(lots, 2), 0.01)
                except Exception:
                    out["Trading qty Contract"] = None
            else:
                out["Trading qty Contract"] = None
    return out


@pytest.mark.parametrize("v1_file,v2_file,side,variation", PAIRS)
def test_v2_reproduces_v1_setups(frames, v1_file, v2_file, side, variation):
    import s17_core

    v1 = load_v1(v1_file)
    symbol = "XAUUSD"

    expected = v1_rows(v1, frames, side, variation, symbol)

    # point s17_core at the same variant, then run its pipeline
    spec = s17_core.Spec(
        id="PARITY", label="parity", symbols=[symbol], side=side,
        variation=variation,
        method1="m1m2" if "M1-M2" in v1_file or "M1-M2-Variation" in v1_file else "std",
        method2="v4" if "M2-M3-V4" in v1_file else "std",
        csv="parity_{symbol}.csv", log_dir="./parity-logs")
    s17_core._ACTIVE = spec
    got = s17_core.build_rows(FakeCtx(symbol, frames), spec)

    assert len(got) == len(expected), (
        f"{v2_file}: Version 2 found {len(got)} setups, Version 1 found "
        f"{len(expected)}")

    # every column both sides produce, not a chosen few
    for i, (row, exp) in enumerate(zip(got, expected)):
        shared = [c for c in exp if c in row and not c.startswith("_")]
        assert len(shared) > 40, f"{v2_file}: only {len(shared)} comparable columns"
        for col in shared:
            a, b = row.get(col), exp.get(col)
            if isinstance(a, float) and isinstance(b, float):
                assert (a == pytest.approx(b, rel=1e-12)
                        or (np.isnan(a) and np.isnan(b))), \
                    f"{v2_file} setup {i}: {col} {a} != {b}"
            else:
                assert str(a) == str(b), \
                    f"{v2_file} setup {i}: {col} {a!r} != {b!r}"


def test_the_fixture_actually_produces_setups(frames):
    """Guard against a vacuous pass: if the synthetic candles yield no setups,
    every comparison above is trivially true and proves nothing."""
    import s17_core
    spec = s17_core.Spec(id="P", label="p", symbols=["XAUUSD"], side="buy",
                         variation=4, method1="std", method2="v4",
                         csv="p_{symbol}.csv", log_dir="./p")
    s17_core._ACTIVE = spec
    got = s17_core.build_signals(FakeCtx("XAUUSD", frames), spec)
    assert len(got) > 0, "the parity fixture produced no setups at all"


@pytest.mark.parametrize("v1_file,v2_file,side,variation",
                         [p for p in PAIRS if p[3] == 4])
def test_v4_delayed_entry_matches(frames, v1_file, v2_file, side, variation):
    """Variation 4's delayed-entry pass, applied to identical rows."""
    import s17_core

    v1 = load_v1(v1_file)
    symbol = "XAUUSD"
    spec = s17_core.Spec(
        id="PARITY", label="parity", symbols=[symbol], side=side, variation=4,
        method1="m1m2" if "M1-M2-Variation" in v1_file else "std",
        method2="v4" if "M2-M3-V4" in v1_file else "std",
        csv="parity_{symbol}.csv", log_dir="./parity-logs")
    s17_core._ACTIVE = spec

    ctx = FakeCtx(symbol, frames)
    F = s17_core.prepare_frames(ctx)
    mine = s17_core.build_rows(ctx, spec, F)
    theirs = v1_rows(v1, frames, side, variation, symbol)

    for row in mine:
        if row.get("Status") == "Intrade":
            s17_core.apply_variation_4_logic(row, F["arr1"], F["arr5"], F["arr2h"],
                                             F["arr4h"], F["arr1d"], F["volcalc1"],
                                             side)
    for row in theirs:
        if row.get("Status") == "Intrade":
            v1.apply_variation_4_logic(row, F["arr1"], F["arr5"], F["arr2h"],
                                       F["arr4h"], F["arr1d"], F["volcalc1"], side)

    for i, (a, b) in enumerate(zip(mine, theirs)):
        for col in ("Final Entry Price", "Final Entry Date", "Status",
                    "0.5 Target Price a/c Actual Entry Price"):
            assert str(a.get(col)) == str(b.get(col)), \
                f"{v2_file} setup {i}: {col} {a.get(col)!r} != {b.get(col)!r}"


def test_dedupe_invalidates_all_but_the_newest():
    """Two Intrade setups on the same entry minute: the newest survives, the
    older is blanked and marked invalidated."""
    import s17_core
    from collections import OrderedDict

    spec = s17_core.Spec(id="D", label="d", symbols=["XAUUSD"], side="buy",
                         variation=1, method1="std", method2="std",
                         csv="d_{symbol}.csv", log_dir="./d")
    s17_core._ACTIVE = spec

    def row(cycle_start, qty):
        r = s17_core.base_row_v1("buy")
        r["Status"] = "Intrade"
        r["Entry Datetime"] = "2026-05-01 10:00:00+00:00"
        r["Red MACD cycle Startime"] = cycle_start
        r["Qty"] = qty
        r["Strategy Additional info"] = ""
        return r

    rows = [row("2026-05-01 09:00:00", 1.0), row("2026-05-01 09:30:00", 2.0)]
    s17_core.dedupe_rows(rows, spec)

    assert rows[1]["Status"] == "Intrade", "the newest cycle must survive"
    assert rows[0]["Status"].startswith("Invalidated"), "the older must be invalidated"
    assert rows[0]["Qty"] is None, "an invalidated row must have its trade fields blanked"
    assert rows[0]["Entry Price"] is None
