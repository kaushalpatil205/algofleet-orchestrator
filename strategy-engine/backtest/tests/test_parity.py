"""Parity comparison logic. No database — compare() is pure."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backtest.parity import compare, load_replay


def _live(**over):
    row = {"event_id": "a1", "symbol": "BTCUSD", "side": "sell",
           "signal_price": 63000.0, "hard_sl": 63500.0, "qty": 0.15,
           "entry_price": 63001.5}
    row.update(over)
    return {row["event_id"]: row}


def _replay(**over):
    row = {"event_id": "a1", "signal_price": 63000.0, "hard_sl": 63500.0,
           "qty": 0.15, "entry_price": 63000.0}
    row.update(over)
    return {row["event_id"]: row}


def test_identical_runs_pass():
    r = compare(_live(), _replay())
    assert r["matched"] == 1 and r["tight_breaks"] == 0
    assert r["verdict"].startswith("PASS")


def test_a_missed_setup_fails():
    """The strongest signal in the whole harness: the live system found a setup
    the replay did not, so the replay is not reproducing live."""
    live = _live()
    live["b2"] = dict(live["a1"], event_id="b2")
    r = compare(live, _replay())
    assert r["live_only"] == ["b2"]
    assert r["verdict"].startswith("FAIL")


def test_extra_replay_setups_do_not_fail_on_their_own():
    """A replay window is usually wider at the edges than what live recorded,
    so replay-only ids are reported but are not by themselves a failure."""
    replay = _replay()
    replay["z9"] = dict(replay["a1"], event_id="z9")
    r = compare(_live(), replay)
    assert r["replay_only"] == ["z9"]
    assert r["verdict"].startswith("PASS")


def test_candle_derived_disagreement_fails():
    r = compare(_live(), _replay(hard_sl=63400.0))
    assert r["tight_breaks"] == 1
    assert r["verdict"].startswith("FAIL")
    assert r["diffs"][0]["fields"]["hard_sl"]["tight"] is True


def test_fill_price_divergence_is_reported_not_failed():
    """entry_price is the broker's actual fill against a simulated one —
    divergence is expected, and failing on it would make every real comparison
    red for the wrong reason."""
    r = compare(_live(), _replay(entry_price=62995.0))
    assert r["diffs"] and r["tight_breaks"] == 0
    assert r["diffs"][0]["fields"]["entry_price"]["tight"] is False
    assert r["verdict"].startswith("PASS")


def test_empty_live_window_is_not_a_pass():
    r = compare({}, _replay())
    assert r["verdict"].startswith("NO LIVE DATA")


def test_load_replay_unions_csv_setups_and_executed_trades(tmp_path):
    """Stage 1 records setups only in the CSV; Stage 2 adds trades.json. Parity
    must see both, or a signal sweep would look like it found nothing."""
    logdir = tmp_path / "bridge" / "logs"
    logdir.mkdir(parents=True)
    (logdir / "s.csv").write_text(
        "Event ID,Status,Entry Price,Hard SL Price,Trading qty Contract\n"
        "aaa,Intrade,63000,63500,0.15\n"
        "bbb,Invalidated,62000,62500,0.2\n")
    (tmp_path / "trades.json").write_text(json.dumps(
        [{"event_id": "aaa", "ticket": 900000001, "entry_price": 63001.0,
          "status": "OPEN"}]))

    got = load_replay(str(tmp_path))
    assert set(got) == {"aaa", "bbb"}
    assert got["aaa"]["ticket"] == 900000001      # merged from trades.json
    assert got["aaa"]["source"] == "csv+trades"
    assert got["bbb"]["source"] == "csv"
