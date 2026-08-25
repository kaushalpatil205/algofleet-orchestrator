"""No strategy may hard-require a key that only exists in Live/engine.json.

Live/engine.json holds the fleet's Telegram credentials and database URL. It
is gitignored, so it is present on a developer's machine and absent in CI and
on a freshly provisioned host.

That asymmetry is a trap. Strategy 18.1 read `_config["BOT_TOKEN"]` with a
subscript, which raised KeyError at import. Every local test passed, because
the file was sitting there; CI failed on all six, and on the host they would
not have started at all.

A subscript on a shared key is therefore a deployment bug, and this catches it
by reading the source rather than by importing — no dependency on which files
happen to exist on the machine running the tests.
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Shared keys whose absence must degrade rather than stop the strategy:
# notifications go quiet, persistence turns off, settings fall back to their
# defaults. MT5_API_KEY is deliberately NOT here — a strategy with no API key
# should fail loudly at startup, not run believing it is trading.
MUST_DEGRADE = {
    "BOT_TOKEN", "CHAT_ID", "TRADE_DB_URL",
    "FLATTEN_BEFORE_WEEKEND", "FLATTEN_BEFORE_DAILY_BREAK",
    "FLATTEN_LEAD_MIN", "TRAIL_INTERVAL_SEC", "RISK_PER_TRADE",
}
SHARED_KEYS = MUST_DEGRADE

# Only strategies this repository actually deploys. The original Version 1
# S17 and S21 scripts are kept so the parity tests can load them; they run
# from their own directories on the host with their own complete configs, and
# holding them to a standard set for the deployed fleet would be noise.
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import sync_cronicle as _SC          # noqa: E402
STRATEGIES = sorted(s["path"] for s in _SC.discover())


def _hard_required_shared_keys(path):
    """Shared keys read as `_config["KEY"]` — a subscript, not .get()."""
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
    except SyntaxError:
        return set()
    bad = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        val = node.value
        if not (isinstance(val, ast.Name) and val.id.endswith("config")):
            continue
        key = node.slice
        if isinstance(key, ast.Constant) and key.value in SHARED_KEYS:
            bad.add(key.value)
    return bad


@pytest.mark.parametrize("path", STRATEGIES,
                         ids=[os.path.relpath(p, os.path.join(ROOT, "Live"))
                              for p in STRATEGIES])
def test_shared_keys_are_read_with_a_default(path):
    bad = _hard_required_shared_keys(path)
    assert not bad, (
        f"{os.path.relpath(path, ROOT)} requires {sorted(bad)} from the "
        f"gitignored Live/engine.json. Read them with .get(KEY, default) so "
        f"the strategy still starts on a host that has no shared config."
    )


def test_the_check_can_actually_fail(tmp_path):
    """A detector that never fires is worse than none."""
    p = tmp_path / "bad_strategy.py"
    p.write_text('_config = {}\nBOT_TOKEN = _config["BOT_TOKEN"]\n')
    assert _hard_required_shared_keys(str(p)) == {"BOT_TOKEN"}
    p.write_text('_config = {}\nBOT_TOKEN = _config.get("BOT_TOKEN", "")\n')
    assert _hard_required_shared_keys(str(p)) == set()
