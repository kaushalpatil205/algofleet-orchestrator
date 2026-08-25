#!/usr/bin/env python3
"""S17 M1M2 V4 · BTCUSD — BUY side only.

Version 2. The Strategy 17 logic lives in s17_core.py, shared by all six
variants; the infrastructure lives in engine/. What was a 2784-line
script is now this declaration — the table in scripts/gen_s17_variants.py is
the entire difference between the six.

Replaces: Bridge-Strategy-17-M1-M2-Variation-4-BTCUSDT-Buy-Live.py
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
    id='S17-M1M2-V4-BTCUSDT-BUY',
    label='S17 M1M2 V4 · BTCUSD',
    telegram_name='Strategy 17 M1 M2 Variation 4',
    symbols=['BTCUSD'],
    side='buy',
    variation=4,
    method1='m1m2',
    method2='std',
    csv='Strategy17_M1M2_Var4_BTCUSDT_BUY_5min.csv',
    log_dir='./bridge/Strategy 17 M1 M2 Variation 4 Live Logs',
), entry=__file__)

if __name__ == "__main__":
    strategy.cli()
