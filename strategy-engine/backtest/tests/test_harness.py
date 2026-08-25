"""Harness tests.

The end-to-end case loads a real strategy, which mutates sys.modules and cwd
process-wide, so it lives last and is marked slow.
"""

import os
import socket
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backtest import feed as feed_mod
from backtest import results
from backtest.fakes import broker as broker_mod
from backtest.fakes import http as fake_http
from backtest.fakes import trade_db as fake_trade_db
from backtest.fakes.clock import VirtualClock, datetime_shim
from backtest.loader import NetworkSealed
from backtest.tests import fixtures

UTC = timezone.utc
SYMBOL = "BTCUSD"
STRATEGY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Live", "Strategy 17", "Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live.py")


@pytest.fixture(scope="session")
def archive(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("archive"))
    fixtures.build(root, SYMBOL, start="2026-04-01", end="2026-06-10")
    return root


@pytest.fixture
def built(archive):
    clock = VirtualClock(datetime(2026, 6, 5, 12, 0, tzinfo=UTC))
    f = feed_mod.Feed(feed_mod.ParquetArchiveSource(archive), clock)
    f.preload(SYMBOL, {"M5": 2500, "M1": 6000, "H2": 500},
              datetime(2026, 6, 1, tzinfo=UTC).timestamp(),
              datetime(2026, 6, 10, tzinfo=UTC).timestamp())
    return f, clock


# --- feed ---------------------------------------------------------------------

def test_payload_uses_the_bridge_wire_shape(built):
    f, _ = built
    payload = f.payload(SYMBOL, "M1", 10)
    assert set(payload) == {"symbol", "timeframe", "candles"}
    c = payload["candles"][0]
    # `time` and `tick_volume`, not the archive's `ts`/`volume`: the strategies
    # parse the bridge's shape, and the archive's would silently send them down
    # their except branch and return an empty frame.
    assert set(c) == {"time", "open", "high", "low", "close", "tick_volume"}
    assert isinstance(c["time"], int)


def test_payload_parses_the_way_the_strategy_parses_it(built):
    """Mirrors fetch_live_mt5_candles' own body, so a shape change fails here
    rather than surfacing as a backtest that finds no setups."""
    f, _ = built
    payload = f.payload(SYMBOL, "M1", 50)
    df = pd.DataFrame(payload["candles"])
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    df = df[["datetime", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("datetime").reset_index(drop=True).set_index("datetime")
    assert len(df) == 50
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing


def test_window_never_shows_the_future(built):
    f, clock = built
    clock.set(datetime(2026, 6, 3, 8, 0, tzinfo=UTC))
    df = f.window(SYMBOL, "M1", 500)
    assert df.index.max() <= clock.now()


def test_window_caps_at_the_requested_lookback(built):
    f, _ = built
    assert len(f.window(SYMBOL, "M1", 120)) == 120


def test_h2_is_derived_from_h1_on_even_hours(built):
    f, _ = built
    df = f.window(SYMBOL, "H2", 24)
    assert len(df) == 24
    assert all(ts.hour % 2 == 0 for ts in df.index)
    gaps = df.index.to_series().diff().dropna().unique()
    assert all(pd.Timedelta(g) == pd.Timedelta(hours=2) for g in gaps)


def test_h2_live_h1_mode_reproduces_the_bridge_bug(archive):
    """With --h2-mode live-h1 the H2 series is literally H1 bars, which is what
    production serves today: the worker's tf_map.get(timeframe, 16385) answers
    an unknown H2 with H1."""
    clock = VirtualClock(datetime(2026, 6, 5, 12, 0, tzinfo=UTC))
    f = feed_mod.Feed(feed_mod.ParquetArchiveSource(archive), clock, h2_mode="live-h1")
    f.preload(SYMBOL, {"H2": 500}, datetime(2026, 6, 1, tzinfo=UTC).timestamp(),
              datetime(2026, 6, 10, tzinfo=UTC).timestamp())
    df = f.window(SYMBOL, "H2", 24)
    gaps = df.index.to_series().diff().dropna().unique()
    assert all(pd.Timedelta(g) == pd.Timedelta(hours=1) for g in gaps)


def test_h4_and_d1_fall_back_to_h1_with_a_warning(tmp_path):
    """The archive is seeded M1/M5/M15/H1 by default, but S17 asks for H4 and
    D1 as well. They must be derived rather than silently coming back empty —
    an empty frame yields zero setups, which looks like a quiet market."""
    root = str(tmp_path / "archive")
    df = fixtures.minute_walk(datetime(2026, 5, 1, tzinfo=UTC),
                              datetime(2026, 6, 10, tzinfo=UTC))
    fixtures.write_archive(root, SYMBOL, df, timeframes=("M1", "M5", "M15", "H1"))

    clock = VirtualClock(datetime(2026, 6, 5, 12, 0, tzinfo=UTC))
    f = feed_mod.Feed(feed_mod.ParquetArchiveSource(root), clock)
    f.preload(SYMBOL, {"H4": 300, "D1": 200},
              datetime(2026, 6, 1, tzinfo=UTC).timestamp(),
              datetime(2026, 6, 10, tzinfo=UTC).timestamp())

    assert len(f.window(SYMBOL, "H4", 10)) == 10
    assert len(f.window(SYMBOL, "D1", 5)) == 5
    assert all(ts.hour % 4 == 0 for ts in f.window(SYMBOL, "H4", 10).index)
    assert {w.split()[1] for w in f.warnings} == {"H4", "D1"}


def test_archived_timeframe_is_preferred_over_deriving(tmp_path):
    root = str(tmp_path / "archive")
    df = fixtures.minute_walk(datetime(2026, 5, 1, tzinfo=UTC),
                              datetime(2026, 6, 10, tzinfo=UTC))
    fixtures.write_archive(root, SYMBOL, df, timeframes=("H1", "H4"))

    clock = VirtualClock(datetime(2026, 6, 5, 12, 0, tzinfo=UTC))
    f = feed_mod.Feed(feed_mod.ParquetArchiveSource(root), clock)
    f.preload(SYMBOL, {"H4": 300}, datetime(2026, 6, 1, tzinfo=UTC).timestamp(),
              datetime(2026, 6, 10, tzinfo=UTC).timestamp())
    assert f.warnings == []
    assert f.requested[0][2] == "H4"          # read natively, not from H1


def test_strides_cover_the_whole_window():
    lo = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    hi = datetime(2026, 2, 1, tzinfo=UTC).timestamp()
    strides = feed_mod.scan_strides(lo, hi, {"M5": 2500, "M1": 6000})
    assert strides[-1] == datetime.fromtimestamp(hi, UTC)
    # Each step must stay inside what one scan can see, or the sweep skips days.
    span = min(60 * 6000, 300 * 2500)
    assert max((b - a).total_seconds()
               for a, b in zip(strides, strides[1:])) <= span


def test_warmup_reaches_back_at_least_the_lookback():
    start = datetime(2026, 6, 1, tzinfo=UTC).timestamp()
    got = feed_mod.warmup_start(start, "M1", 6000)
    assert start - got >= 6000 * 60


# --- clock --------------------------------------------------------------------

def test_datetime_shim_reports_simulated_now():
    clock = VirtualClock(datetime(2026, 6, 5, 9, 30, tzinfo=UTC))
    shim = datetime_shim(clock)
    assert shim.now(UTC) == clock.now()
    clock.advance(3600)
    assert shim.now(UTC).hour == 10
    # Inherited constructors keep working — the strategies use strptime and
    # arithmetic on the same name.
    assert shim.strptime("2026-01-02", "%Y-%m-%d").year == 2026


def test_clock_converts_to_new_york_for_the_market_guards():
    clock = VirtualClock(datetime(2026, 6, 5, 21, 0, tzinfo=UTC))
    assert clock.now_ny().hour == 17          # 21:00 UTC = 17:00 EDT


# --- trade_db stub ------------------------------------------------------------

def test_stub_records_the_lifecycle():
    fake_trade_db.reset()
    fake_trade_db.init("S17-TEST", magic=17101)
    fake_trade_db.record_signal("ev1", "BTCUSD", "sell", signal_price=63000,
                                qty=0.1, hard_sl=63500)
    fake_trade_db.record_execution("ev1", 555, 10009, entry_price=63010,
                                   qty=0.1, hard_sl=63500, targets={2: 62000})
    fake_trade_db.record_trail(555, 63010, 2, executed=True)
    fake_trade_db.record_close(555, reason="sl_hit", pnl=-100)

    kinds = [e["kind"] for e in fake_trade_db.events]
    assert kinds == ["INIT", "SIGNAL", "EXECUTION", "TRAIL_MOVE", "CLOSE"]
    tr = fake_trade_db.trades["ev1"]
    assert tr["ticket"] == 555 and tr["status"] == "CLOSED"
    assert tr["current_sl"] == 63010 and tr["trail_hit"] == {2}


def test_rejected_trail_does_not_move_the_stop():
    """The live module treats current_sl as the broker's truth — a modify MT5
    refused must not drift it."""
    fake_trade_db.reset()
    fake_trade_db.init("S17-TEST", magic=17101)
    fake_trade_db.record_execution("ev1", 555, 10009, hard_sl=63500)
    fake_trade_db.record_trail(555, 63010, 2, executed=False)
    tr = fake_trade_db.trades["ev1"]
    assert tr["current_sl"] == 63500
    assert tr["trail_hit"] == set()


def test_load_open_trades_starts_flat():
    fake_trade_db.reset()
    assert fake_trade_db.load_open_trades() == []


# --- broker + router ----------------------------------------------------------

def test_broker_fills_modifies_and_reports_positions(built):
    f, clock = built
    b = broker_mod.SimBroker(f, clock, SYMBOL)
    res = b.trade({"symbol": SYMBOL, "type": 1, "volume": 0.5, "sl": 64000,
                   "magic": 17101})
    assert res["result"] == 10009 and res["order_id"] > 0
    ticket = res["order_id"]

    pos = b.positions()[0]
    assert pos["ticket"] == ticket and pos["magic"] == 17101 and pos["type"] == 1

    payload, status = b.modify({"ticket": ticket, "sl": 63500})
    assert status == 200 and payload["result"] == 10009
    assert b.open[ticket]["sl"] == 63500

    payload, status = b.close({"ticket": ticket})
    assert status == 200 and not b.open
    assert b.closed[0]["close_reason"] == "manual_close"


def test_broker_stops_out_on_the_bar_range_not_the_close(built):
    """A stop inside the bar would have been taken intrabar live; judging on
    close alone flatters every trade that dipped through and recovered."""
    f, clock = built
    b = broker_mod.SimBroker(f, clock, SYMBOL)
    bar = f.window(SYMBOL, "M1", 1).iloc[-1]
    res = b.trade({"symbol": SYMBOL, "type": 0, "volume": 1.0, "magic": 1})
    ticket = res["order_id"]
    # Stop between the bar's low and its close: untouched by close, hit by range.
    b.open[ticket]["sl"] = (float(bar["low"]) + float(bar["close"])) / 2
    assert float(bar["low"]) < b.open[ticket]["sl"]
    b.settle()
    assert not b.open and b.closed[-1]["close_reason"] == "sl_hit"


def test_router_records_routes_nothing_claimed(built):
    f, clock = built
    r = fake_http.Router(f, broker_mod.SimBroker(f, clock, SYMBOL))
    assert r.get("https://bridge.example/1/demo/wat").status_code == 404
    assert r.unmatched == [("GET", "https://bridge.example/1/demo/wat")]


def test_router_swallows_telegram(built):
    f, clock = built
    r = fake_http.Router(f, broker_mod.SimBroker(f, clock, SYMBOL))
    res = r.post("https://api.telegram.org/botX/sendMessage",
                 json={"chat_id": "1", "text": "hello"})
    assert res.status_code == 200 and r.telegram == ["hello"]


def test_candles_404_matches_the_worker(built):
    f, clock = built
    r = fake_http.Router(f, broker_mod.SimBroker(f, clock, SYMBOL))
    res = r.get("https://bridge.example/1/demo/market/candles/NOPE?timeframe=M1&count=5")
    assert res.status_code == 404
    with pytest.raises(fake_http.HTTPError):
        res.raise_for_status()


# --- isolation ----------------------------------------------------------------

def test_network_is_sealed():
    with NetworkSealed():
        with pytest.raises(RuntimeError, match="real network"):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)   # restored
    s.close()


# --- end to end ---------------------------------------------------------------

@pytest.mark.slow
def test_stage1_replays_a_live_strategy(archive, tmp_path):
    from backtest.run import main

    meta = main([
        "--strategy", STRATEGY,
        "--from", "2026-06-01", "--to", "2026-06-05",
        "--source", "parquet", "--archive-root", archive,
        "--out", str(tmp_path / "run"), "--quiet",
    ])

    # The real trade_db must never have been reached.
    assert sys.modules["trade_db"] is fake_trade_db
    assert meta["unmatched_routes"] == []
    # The strategy's own log is the Stage 1 product; signals stay near zero
    # because is_new_entry only fires on the last few minute bars of a scan.
    assert meta["setups"]["setups"] > 0
    assert meta["setups"]["intrade"] > 0

    summary = results.summarise(str(tmp_path / "run"))
    assert summary["setups"] == meta["setups"]["setups"]
