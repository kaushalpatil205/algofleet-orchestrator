"""Run every Version 2 strategy's frame preparation on synthetic candles.

This exists because of a bug the rest of the suite could not see. When the
indicators moved into engine/, `calc_sma_kama` lost the `length` argument
that all seven Version 1 sources declare. Six strategies call it positionally
and were fine; Strategy 21 passes `length=20` by keyword, so every one of its
scans raised TypeError and it never produced a signal — while Cronicle happily
showed the job as running.

test_indicators.py did not catch it: it compares the engine's implementation
against Version 1's, calling both the way the engine calls them. The gap was
never the arithmetic, it was the call signature at the strategy's own call
site. So these tests call nothing directly — they drive the strategies'
frame-prep entry points and assert the indicator columns come out populated.
"""

import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("trade_db", types.ModuleType("trade_db"))

pytest.importorskip("talib")


def candles(n=400, seed=0):
    """Enough bars for the slowest indicator to warm up, with real movement —
    a flat series makes several of these degenerate and hides errors."""
    rng = np.random.default_rng(seed)
    close = 30000 + np.cumsum(rng.normal(0, 25, n))
    high = close + np.abs(rng.normal(0, 12, n))
    low = close - np.abs(rng.normal(0, 12, n))
    op = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {"open": op, "high": np.maximum(high, np.maximum(op, close)),
         "low": np.minimum(low, np.minimum(op, close)), "close": close,
         "volume": rng.uniform(1, 100, n)},
        index=pd.date_range("2026-01-01", periods=n, freq="3min", tz="UTC"),
    )


def _load(rel, name):
    import importlib.util
    path = os.path.join(ROOT, rel)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def s21():
    return _load("Live/Strategy 21/s21_core.py", "s21_core_undertest")


@pytest.fixture(scope="module")
def s17():
    return _load("Live/Strategy 17/s17_core.py", "s17_core_undertest")


def _populated(frame, key):
    """The column exists and is not entirely NaN once warmed up."""
    col = np.asarray(frame[key], dtype=float)
    assert col.size, f"{key} is empty"
    assert not np.all(np.isnan(col)), f"{key} is all NaN"


def test_s21_3m_frame_prep_runs(s21):
    """The exact call that was raising TypeError on every live scan."""
    out = s21.prepare_3m_data(candles())
    for key in ("ema50", "rsi14", "emakama", "kama_line", "sma_kama", "bb_mid"):
        _populated(out, key)


def test_s21_15m_frame_prep_runs(s21):
    out = s21.prepare_15m_data(candles(seed=1))
    for key in ("rsi14", "close", "volume"):
        _populated(out, key)


def test_s21_sma_kama_matches_the_engine_default(s21):
    """S21 passes length=20 explicitly; the engine defaults to the same. If
    those ever drift apart, S21's signals move without anyone editing S21."""
    from engine import indicators as ENG
    df = candles(seed=2)
    explicit = ENG.calc_sma_kama(df, length=20)
    default = ENG.calc_sma_kama(df)
    assert np.allclose(explicit.values, default.values, equal_nan=True)
    assert ENG.SMA_EMA_LENGTH == 20


def test_engine_indicators_accept_the_version_1_signatures():
    """Every V1 source declares calc_sma_kama(df, length=20). The shared copy
    must stay callable that way, positionally and by keyword."""
    from engine import indicators as ENG
    df = candles(seed=3)
    ENG.calc_sma_kama(df)
    ENG.calc_sma_kama(df, 20)
    ENG.calc_sma_kama(df, length=20)
