"""Pin engine/indicators.py to the Version 1 originals.

The indicator bodies were moved out of the strategy scripts, not rewritten.
This loads the functions straight out of the original files and asserts the
engine's copies produce identical output — so a well-meant tidy-up of the
shared copy shows up as a failing test rather than as changed signals on a
live account.

Both Version 1 engines are checked, because they each carried their own copy
and the copies were written in very different styles.
"""

import ast
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("trade_db", types.ModuleType("trade_db"))

talib = pytest.importorskip("talib")

from engine import indicators as ENG          # noqa: E402

S17 = os.path.join(ROOT, "Live/Strategy 17/Bridge-S17-M2-M3-V4-XAUUSD-Buy-Live.py")
S21 = os.path.join(ROOT, "Live/Strategy 21/Bridge-S21-1_10-Ratios-BTCUSD-Live.py")

SHARED = ["add_smooth_macd_cycles", "_pine_ema_series", "_pine_kama_series",
          "_heikin_ashi_df", "calc_sma_kama", "calc_emakama", "calc_kama_line"]


def _namespace(path):
    """Module-level constants and the indicator functions, with nothing that
    would load config or touch the network."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")
    ns = {"np": np, "pd": pd, "talib": talib, "log": lambda *a, **k: None,
          "Dict": dict, "List": list, "Any": object, "Optional": object,
          "Tuple": tuple}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.isupper()):
            try:
                exec("\n".join(lines[node.lineno - 1:node.end_lineno]), ns)
            except Exception:
                pass        # config-dependent constants are not needed here
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in SHARED:
            exec("\n".join(lines[node.lineno - 1:node.end_lineno]), ns)
    return ns


@pytest.fixture(scope="module")
def originals():
    return {"S17": _namespace(S17), "S21": _namespace(S21)}


@pytest.fixture(scope="module")
def frames():
    rng = np.random.default_rng(11)
    out = {}
    for name, close in (
        ("walk", 2400 + np.cumsum(rng.normal(0, 2, 400))),
        ("trend", np.linspace(60000, 65000, 400) + rng.normal(0, 50, 400)),
        ("flat", np.full(400, 1.2345)),
    ):
        out[name] = pd.DataFrame(
            {"open": close, "high": close + 1.0, "low": close - 1.0,
             "close": close, "volume": rng.integers(1, 100, 400).astype(float)},
            index=pd.date_range("2026-01-01", periods=400, freq="5min", tz="UTC"))
    return out


def _same(a, b):
    if isinstance(a, pd.DataFrame):
        assert set(a.columns) == set(b.columns)
        for c in a.columns:
            x, y = a[c], b[c]
            if x.dtype == object:
                assert x.equals(y), f"column {c} differs"
            else:
                assert np.allclose(x.astype(float), y.astype(float),
                                   equal_nan=True), f"column {c} differs"
        return
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    assert a.shape == b.shape
    assert np.allclose(a, b, equal_nan=True)


@pytest.mark.parametrize("engine", ["S17", "S21"])
@pytest.mark.parametrize("shape", ["walk", "trend", "flat"])
def test_series_indicators_match_original(originals, frames, engine, shape):
    ns, df = originals[engine], frames[shape]
    close = df["close"].values.astype(float)
    ohlc4 = (df["open"] + df["high"] + df["low"] + df["close"]).values / 4.0

    _same(ns["_pine_ema_series"](df["close"], 20), ENG._pine_ema_series(df["close"], 20))
    _same(ns["_pine_kama_series"](df["close"], 20), ENG._pine_kama_series(df["close"], 20))
    _same(ns["calc_sma_kama"](df), ENG.calc_sma_kama(df))
    _same(ns["calc_emakama"](close), ENG.calc_emakama(close))
    _same(ns["calc_kama_line"](ohlc4), ENG.calc_kama_line(ohlc4))
    _same(ns["_heikin_ashi_df"](df), ENG._heikin_ashi_df(df))


@pytest.mark.parametrize("engine", ["S17", "S21"])
@pytest.mark.parametrize("shape", ["walk", "trend", "flat"])
def test_macd_cycles_match_original(originals, frames, engine, shape):
    ns, df = originals[engine], frames[shape]
    _same(ns["add_smooth_macd_cycles"](df.copy()),
          ENG.add_smooth_macd_cycles(df.copy()))


def test_kama_line_agrees_across_both_engines(originals, frames):
    """The claim that made sharing one copy safe."""
    df = frames["walk"]
    ohlc4 = (df["open"] + df["high"] + df["low"] + df["close"]).values / 4.0
    a = originals["S17"]["calc_kama_line"](ohlc4)
    b = originals["S21"]["calc_kama_line"](ohlc4)
    assert np.array_equal(a, b), "the two Version 1 KAMA implementations diverged"
