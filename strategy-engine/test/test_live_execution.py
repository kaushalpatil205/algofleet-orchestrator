"""Gate: every strategy under Live/ must be able to trade and survive a restart.

One subprocess per strategy — importing one runs `trade_db.init()` and installs
module-global state, so two in the same interpreter would contaminate each
other. See live_execution_probe.py for what each run actually asserts.
"""

import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE = pathlib.Path(__file__).with_name("live_execution_probe.py")


def strategies():
    """Every live strategy under Live/, both generations.

    A runnable strategy is a .py with a config sidecar — that requirement is
    what separates one from a helper or a generator sitting in the same tree.
    Version 2 files are named for the strategy (s17_*, s21_*); Version 1 files
    are the Bridge-* scripts, still gated while they remain on disk.
    """
    out = []
    for path in (REPO / "Live").rglob("*.py"):
        if not path.with_suffix(".json").exists():
            continue
        if path.name.startswith("_") or path.stem.endswith("_core"):
            continue
        out.append(path)
    return sorted(out)


def test_at_least_one_strategy_discovered():
    """A rename or a moved directory would otherwise turn this whole gate into
    a silent no-op that passes every PR."""
    found = strategies()
    assert found, f"no strategies discovered under {REPO / 'Live'}"


def test_both_generations_are_gated():
    """While Version 1 scripts are still on disk they must keep being checked —
    they are what runs until each Cronicle event is pointed at Version 2."""
    names = [p.stem for p in strategies()]
    v2 = [n for n in names if n.startswith(("s17_", "s21_"))]
    assert len(v2) >= 7, f"expected the 7 Version 2 strategies, found {v2}"


def test_every_magic_is_unique():
    """Two strategies sharing a magic means neither can be told apart in the
    account history, and the registry's UNIQUE constraint rejects the second."""
    import json
    seen = {}
    for path in strategies():
        if path.stem.startswith(("s17_", "s21_")):
            magic = json.loads(path.with_suffix(".json").read_text()).get("MAGIC")
            if magic is None:
                continue
            assert magic not in seen, (
                f"magic {magic} claimed by both {seen[magic]} and {path.stem}")
            seen[magic] = path.stem


@pytest.mark.parametrize("strategy", strategies(),
                         ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_live_execution_ready(strategy):
    r = subprocess.run([sys.executable, str(PROBE), str(strategy)],
                       capture_output=True, text=True, timeout=300)
    report = "\n".join(l for l in r.stdout.splitlines()
                       if l.startswith(("===", "  PASS", "  WARN", "  FAIL")))
    print(report)
    if r.returncode != 0:
        detail = report or (r.stdout + r.stderr)
        pytest.fail(f"{strategy.relative_to(REPO)} is not ready for live "
                    f"execution:\n{detail}", pytrace=False)
