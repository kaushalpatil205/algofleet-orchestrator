#!/usr/bin/env python3
"""S17 M2M3 V4 · XAUUSD — BUY side only.

Version 2. The Strategy 17 logic lives in s17_core.py, shared by all six
variants; the infrastructure lives in engine/. What was a 3013-line
script is now this declaration — the table in scripts/gen_s17_variants.py is
the entire difference between the six.

Replaces: Bridge-S17-M2-M3-V4-XAUUSD-Buy-Live.py
"""

import os
import sys

# The strategy is started by path (Cronicle runs `python <this file>`), so the
# repo has to be put on sys.path before engine/ or s17_core can be imported.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "Live")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from s17_core import Spec, make_strategy      # noqa: E402

strategy = make_strategy(Spec(
    id='S17-M2M3-V4-XAUUSD-BUY',
    label='S17 M2M3 V4 · XAUUSD',
    telegram_name='Strategy 17 M3 M2 Variation 4',
    symbols=['XAUUSD'],
    side='buy',
    variation=4,
    method1='std',
    method2='v4',
    csv='Strategy17_M3M2_Var4_{symbol}_BUY_5min.csv',
    log_dir='./bridge/Strategy 17 M3 M2 Variation 4 XAUUSD Buy Live Logs',
), entry=__file__)

if __name__ == "__main__":
    strategy.cli()
