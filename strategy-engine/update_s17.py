import os
import glob

is_market_closed_func = """
def is_market_closed(inst_symbol: str, _now=None) -> bool:
    s = _MC_SESSIONS.get(inst_symbol)
    if not s: return False
    import datetime as _mdt
    from zoneinfo import ZoneInfo as _MZi
    now = _now or _mdt.datetime.now(_MZi("America/New_York"))
    if s.get("weekend"):
        if (now.weekday() == 4 and now.hour >= 17) or (now.weekday() == 5) or (now.weekday() == 6 and now.hour < 17):
            return True
    if s.get("daily_break"):
        if now.weekday() < 4 and now.hour == 17:
            return True
    return False

"""

def update_file(fpath):
    with open(fpath, "r") as f:
        content = f.read()
    
    if "def is_market_closed" in content:
        print(f"Skipping {fpath}, already updated.")
        return
        
    if "def market_close_flatten_due" not in content:
        print(f"Cannot find target in {fpath}")
        return
        
    content = content.replace("def market_close_flatten_due", is_market_closed_func + "def market_close_flatten_due")
    
    loop_target = """                for inst in INSTRUMENTS:
                    try:
                        run_live_scan_for_instrument(inst)"""
    loop_replacement = """                for inst in INSTRUMENTS:
                    try:
                        if is_market_closed(inst):
                            continue
                        run_live_scan_for_instrument(inst)"""
                        
    content = content.replace(loop_target, loop_replacement)
    
    with open(fpath, "w") as f:
        f.write(content)
    print(f"Updated {fpath}")

files = glob.glob("Live/Strategy 17/Bridge-*.py")
for f in files:
    update_file(f)
