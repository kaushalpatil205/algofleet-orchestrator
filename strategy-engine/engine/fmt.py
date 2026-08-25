"""Value formatting for journal rows and Telegram messages.

Verbatim from the Version 1 scripts, where every one carried the same copy.
S17's and S21's `_kv_lines` differed only by a line wrap; `vkv`, `_s` and `_f`
were character-identical. `vblocks` existed only in S17.

These build the free-text "Additional info" blocks that go into the CSV
columns, so their exact output is part of the journal format — a strategy's
logs are read by people and diffed against backtests. Formatting changes here
change those files.
"""

from collections import OrderedDict
from typing import Any, List, Optional

import numpy as np
import pandas as pd


def _s(ts) -> Optional[str]:
    if ts is None: return None
    if isinstance(ts, pd.Timestamp) and pd.isna(ts): return None
    return str(ts)


def _f(x, d: int = 6):
    if x is None: return None
    if isinstance(x, (float, np.floating)):
        if np.isnan(x): return None
        return round(float(x), d)
    if isinstance(x, (int, np.integer)): return int(x)
    if isinstance(x, pd.Timestamp): return str(x)
    return x


def _kv_lines(obj: Any, indent: int = 0) -> List[str]:
    sp = " " * indent
    if obj is None: return [f"{sp}None"]
    if isinstance(obj, (str, int, float, bool, np.floating, np.integer)):
        return [f"{sp}{obj}"]
    if isinstance(obj, list):
        out = []
        for v in obj:
            if isinstance(v, (dict, OrderedDict, list)):
                out.append(f"{sp}-"); out.extend(_kv_lines(v, indent + 2))
            else: out.append(f"{sp}- {v}")
        return out
    if isinstance(obj, (dict, OrderedDict)):
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, OrderedDict, list)):
                out.append(f"{sp}{k}:"); out.extend(_kv_lines(v, indent + 2))
            else: out.append(f"{sp}{k}: {_f(v)}")
        return out
    return [f"{sp}{obj}"]


def vkv(obj: Any) -> str:
    return "\n".join(_kv_lines(obj, 0))


def vblocks(blocks: List[Any]) -> str:
    parts = []
    for b in blocks:
        if not b: continue
        parts.append(vkv(b) if isinstance(b, (dict, OrderedDict)) else str(b))
    return "\n\n".join(parts)
