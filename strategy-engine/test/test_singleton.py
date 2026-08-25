"""The host-level guard against two processes stamping one magic.

This is the guard that has to work when the database does not. A strategy with
broken persistence never heartbeats, so the registry check reads it as long
dead and would happily start a second copy — which is exactly the state three
live strategies were in when this was written.
"""

import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "Live"))
sys.modules.setdefault("trade_db", types.ModuleType("trade_db"))

from engine import singleton                       # noqa: E402


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """A /proc-shaped tree with one python process running a strategy."""
    def build(entries):
        proc = tmp_path / "proc"
        proc.mkdir(exist_ok=True)
        for pid, (argv, cwd) in entries.items():
            d = proc / str(pid)
            d.mkdir(exist_ok=True)
            (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            monkeypatch.setattr(os, "readlink",
                                lambda p, _c=cwd: _c, raising=False)
        monkeypatch.setattr(singleton, "PROC", str(proc))
        return proc
    return build


def _strategy(tmp_path, name, magic):
    py = tmp_path / f"{name}.py"
    py.write_text("# strategy\n")
    (tmp_path / f"{name}.json").write_text(f'{{"MAGIC": {magic}}}')
    return str(py)


def test_finds_another_process_on_the_same_magic(tmp_path, fake_proc, monkeypatch):
    script = _strategy(tmp_path, "other_strategy", 17104)
    fake_proc({4242: (["/usr/bin/python3", script], str(tmp_path))})
    found = singleton.others_with_magic(17104, exclude_pid=1)
    assert [p for p, _ in found] == [4242]
    assert "other_strategy.py" in singleton.describe(found)


def test_ignores_a_different_magic(tmp_path, fake_proc):
    script = _strategy(tmp_path, "unrelated", 21002)
    fake_proc({4242: (["/usr/bin/python3", script], str(tmp_path))})
    assert singleton.others_with_magic(17104, exclude_pid=1) == []


def test_ignores_itself(tmp_path, fake_proc):
    script = _strategy(tmp_path, "me", 17104)
    fake_proc({4242: (["/usr/bin/python3", script], str(tmp_path))})
    assert singleton.others_with_magic(17104, exclude_pid=4242) == []


def test_ignores_non_python_processes(tmp_path, fake_proc):
    script = _strategy(tmp_path, "s", 17104)
    fake_proc({4242: (["/usr/bin/node", script], str(tmp_path))})
    assert singleton.others_with_magic(17104, exclude_pid=1) == []


def test_catches_a_v1_script_with_the_magic_only_in_source(tmp_path, fake_proc):
    """Version 1 scripts default MAGIC inline; a cutover collision must still
    be caught when the sidecar is missing the key."""
    py = tmp_path / "Bridge-Old-Live.py"
    py.write_text('MAGIC = int(_config.get("MAGIC", 17104))\n')
    (tmp_path / "Bridge-Old-Live.json").write_text('{"MT5_API_KEY": "x"}')
    fake_proc({777: (["/usr/bin/python3", str(py)], str(tmp_path))})
    assert [p for p, _ in singleton.others_with_magic(17104, exclude_pid=1)] == [777]


def test_absent_proc_abstains(monkeypatch):
    """No /proc (macOS, a container) must abstain, never raise — a guard that
    crashes a startup is worse than one that says nothing."""
    monkeypatch.setattr(singleton, "PROC", "/nonexistent-proc")
    assert singleton.others_with_magic(17104) == []


def test_unreadable_entries_are_skipped(tmp_path, fake_proc):
    proc = fake_proc({})
    (proc / "9999").mkdir()          # no cmdline at all
    assert singleton.others_with_magic(17104, exclude_pid=1) == []


def test_bad_magic_argument_abstains():
    assert singleton.others_with_magic(None) == []
    assert singleton.others_with_magic("not-a-number") == []
