#!/usr/bin/env python3
"""Generate the six Strategy 17 variant files and their config sidecars.

Each is a declaration, not a script: everything they used to hold is either in
s17_core.py (the S17 logic, shared) or engine/ (the infrastructure, shared).
The table below is the entire difference between the six.
"""
import ast
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S17 = os.path.join(HERE, "Live", "Strategy 17")

VARIANTS = [
 dict(file="s17_m3m2_v1_btcusd_sell", old="Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live",
      id="S17-M3M2-V1-BTCUSDT-SELL", label="S17 M3M2 V1 · BTCUSD",
      tg="Strategy 17 M3 M2 Variation 1", symbols=["BTCUSD"], side="sell",
      variation=1, m1="std", m2="std",),
 dict(file="s17_m3m2_v1_xauusd_sell", old="Bridge-S17_M3_M2_V1_XAUUSD_SELL_Live",
      id="S17-M3M2-V1-XAUUSD-SELL", label="S17 M3M2 V1 · XAUUSD",
      tg="Strategy 17 M3 M2 Variation 1", symbols=["XAUUSD"], side="sell",
      variation=1, m1="std", m2="std",),
 dict(file="s17_m1m2_v4_btcusd_buy", old="Bridge-Strategy-17-M1-M2-Variation-4-BTCUSDT-Buy-Live",
      id="S17-M1M2-V4-BTCUSDT-BUY", label="S17 M1M2 V4 · BTCUSD",
      tg="Strategy 17 M1 M2 Variation 4", symbols=["BTCUSD"], side="buy",
      variation=4, m1="m1m2", m2="std",),
 dict(file="s17_m2m3_v4_xauusd_buy", old="Bridge-S17-M2-M3-V4-XAUUSD-Buy-Live",
      id="S17-M2M3-V4-XAUUSD-BUY", label="S17 M2M3 V4 · XAUUSD",
      tg="Strategy 17 M3 M2 Variation 4", symbols=["XAUUSD"], side="buy",
      variation=4, m1="std", m2="v4",),
 dict(file="s17_m2m3_v4_forex_buy", old="Bridge-S17-M2-M3-V4-Forex-Live-Buy",
      id="S17-M2M3-V4-FOREX-BUY", label="S17 M2M3 V4 · Forex",
      tg="Strategy 17 M3 M2 Variation 4", symbols=["USDJPY","EURUSD","USOIL"], side="buy",
      variation=4, m1="std", m2="v4",),
 dict(file="s17_m1m2_v1_forex_sell", old="Bridge-S17-M1-M2-V1-Forex-Live-Sell",
      id="S17-M1M2-V1-FOREX-SELL", label="S17 M1M2 V1 · Forex",
      tg="Strategy 17 M1 M2 Variation 1", symbols=["USDJPY","EURUSD","USOIL"], side="sell",
      variation=1, m1="m1m2", m2="std",),
]

def _from_v1(path):
    """Read the journal paths out of the Version 1 script itself.

    These were hand-transcribed at first and five of six were wrong, which is
    not a cosmetic mistake: the log directory holds the fired-event ledger, so
    pointing a strategy at a fresh one loses the record of which setups have
    already been traded. Reading them from the source removes the chance to
    get it wrong.

    The names are not systematic — Bridge-S17_M3_M2_V1_XAUUSD_SELL_Live writes
    to a directory with "Forex" in it, and the BTCUSD scripts name their CSVs
    after COIN_NAME ("BTCUSDT") rather than the symbol they actually trade
    ("BTCUSD"). Both are historical accidents, and both are where the real
    history lives.
    """
    src = open(path, encoding="utf-8").read()

    m = _RE_LOGDIR.search(src)
    if not m:
        raise SystemExit(f"{os.path.basename(path)}: no BASE_LOG_DIR found")
    log_dir = m.group(1)

    consts = {}
    for name in ("COIN_NAME", "SYMBOL", "SYMBOL_BRIDGE"):
        cm = re.search(rf'^{name}\s*=\s*"([^"]+)"', src, re.M)
        if cm:
            consts[name] = cm.group(1)

    cm = _RE_CSV.search(src)
    if not cm:
        raise SystemExit(f"{os.path.basename(path)}: no CSV path found")
    csv = cm.group(1)
    # The template interpolates whatever the script used. {symbol} stays a
    # placeholder for the engine to fill per-symbol; a constant is baked in.
    for name, value in consts.items():
        csv = csv.replace("{" + name + "}", value)
    return log_dir, csv


_RE_LOGDIR = re.compile(r'BASE_LOG_DIR\s*=\s*Path\("([^"]+)"\)')
_RE_CSV = re.compile(r'(?:csv_path|_SELL_LOG_PATH|_BUY_LOG_PATH)\s*=\s*'
                      r'BASE_LOG_DIR\s*/\s*f"([^"]+)"')


TEMPLATE = '''#!/usr/bin/env python3
"""{label} — {side_word}.

Version 2. The Strategy 17 logic lives in s17_core.py, shared by all six
variants; the infrastructure lives in engine/. What was a {oldlines}-line
script is now this declaration — the table in scripts/gen_s17_variants.py is
the entire difference between the six.

Replaces: {old}.py
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
    id={id!r},
    label={label!r},
    telegram_name={tg!r},
    symbols={symbols!r},
    side={side!r},
    variation={variation},
    method1={m1!r},
    method2={m2!r},
    csv={csv!r},
    log_dir={logdir!r},
), entry=__file__)

if __name__ == "__main__":
    strategy.cli()
'''


def main():
    for v in VARIANTS:
        oldpath = os.path.join(S17, v["old"] + ".py")
        oldlines = sum(1 for _ in open(oldpath)) if os.path.exists(oldpath) else 0
        v["logdir"], v["csv"] = _from_v1(oldpath)
        body = TEMPLATE.format(
            side_word=("BUY side only" if v["side"] == "buy" else "SELL side only"),
            oldlines=oldlines, **v)
        open(os.path.join(S17, v["file"] + ".py"), "w").write(body)

        old_cfg = os.path.join(S17, v["old"] + ".json")
        cfg = json.load(open(old_cfg)) if os.path.exists(old_cfg) else {}
        new_cfg = {k: cfg[k] for k in
                   ("MAGIC", "MT5_BRIDGE_URL", "MT5_API_KEY", "RISK_PER_TRADE")
                   if k in cfg}
        json.dump(new_cfg, open(os.path.join(S17, v["file"] + ".json"), "w"), indent=4)
        print(f"  {v['file']}.py  ({oldlines} -> {body.count(chr(10))} lines)  "
              f"magic={new_cfg.get('MAGIC')}")
        print(f"      logs -> {v['logdir']}")
        print(f"      csv  -> {v['csv']}")


if __name__ == "__main__":
    main()
