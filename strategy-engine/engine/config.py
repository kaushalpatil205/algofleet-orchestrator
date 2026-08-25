"""Layered configuration for live strategies.

Resolution order, most specific first:

    1. environment variable        (deploy-time override, never committed)
    2. the strategy's own JSON     (Live/<family>/<script>.json)
    3. Live/engine.json            (fleet-wide shared settings, gitignored)
    4. the default passed by code

Before Version 2 every strategy JSON carried the Telegram bot token, the chat
id and the database URL — identical in all seven, and a live database password
committed to the repository. Those move to Live/engine.json, which is
gitignored and deployed alongside the code, so a new strategy's own config is
only what genuinely differs: its magic, its account, and its risk.
"""

import json
import os
from pathlib import Path

# Live/ — the shared config sits at its root, strategies in subfolders under it.
LIVE_DIR = Path(__file__).resolve().parent.parent / "Live"
SHARED_PATH = LIVE_DIR / "engine.json"

# Keys that belong to the fleet rather than to one strategy. Kept as a set so
# a strategy JSON that still carries one (a pre-Version-2 config, or a
# deliberate per-strategy override) keeps working: it simply wins, because
# the strategy layer is checked before the shared one.
SHARED_KEYS = frozenset({
    "BOT_TOKEN", "CHAT_ID", "TRADE_DB_URL", "MT5_API_KEY",
    "FLATTEN_BEFORE_WEEKEND", "FLATTEN_BEFORE_DAILY_BREAK",
    "FLATTEN_LEAD_MIN", "TRAIL_INTERVAL_SEC", "RISK_PER_TRADE",
})


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        raise RuntimeError(f"{path} is not readable JSON: {e}") from e


class Config:
    """One strategy's resolved settings.

    Reads are explicit (`cfg.get("MAGIC", int)`) rather than attribute access
    so a missing required key fails at startup with the key name in the
    message, instead of surfacing later as a zero volume or an empty URL.
    """

    def __init__(self, strategy_path=None, shared_path=None):
        self.strategy_path = Path(strategy_path) if strategy_path else None
        self.shared_path = Path(shared_path) if shared_path else SHARED_PATH
        self.strategy = _load(self.strategy_path) if self.strategy_path else {}
        self.shared = _load(self.shared_path)

    @classmethod
    def for_script(cls, script_file):
        """The sidecar JSON beside a strategy script: foo.py -> foo.json."""
        p = Path(script_file).resolve()
        return cls(strategy_path=p.with_suffix(".json"))

    def _raw(self, key):
        env = os.environ.get(key)
        if env not in (None, ""):
            return env
        if key in self.strategy:
            return self.strategy[key]
        if key in self.shared:
            return self.shared[key]
        return None

    def get(self, key, cast=str, default=None, required=False):
        v = self._raw(key)
        if v is None:
            if required:
                raise KeyError(
                    f"{key} is not set. Add it to {self.strategy_path.name if self.strategy_path else '<strategy>.json'}"
                    f", to {self.shared_path}, or to the environment.")
            return default
        if cast is bool:
            # env vars arrive as strings; JSON already gives a real bool
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "on")
        try:
            return cast(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key}={v!r} is not a valid {cast.__name__}") from e

    def source_of(self, key):
        """Which layer supplied a key — used by the startup banner so a
        surprising value can be traced to the file that set it."""
        if os.environ.get(key) not in (None, ""):
            return "env"
        if key in self.strategy:
            return "strategy"
        if key in self.shared:
            return "shared"
        return "default"
