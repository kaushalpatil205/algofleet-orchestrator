#!/usr/bin/env python3
"""Strategy 21 — BTCUSD, both sides.

Version 2. The S21 logic lives in s21_core.py; the infrastructure lives in
engine/. Replaces Bridge-S21-1_10-Ratios-BTCUSD-Live.py (1,761 lines).
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "Live")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from s21_core import make_strategy      # noqa: E402

strategy = make_strategy(entry=__file__)

if __name__ == "__main__":
    strategy.cli()
