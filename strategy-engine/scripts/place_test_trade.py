#!/usr/bin/env python3
"""Open -> trail the SL -> close a tiny position on an MT5 account through the
bridge, recording the whole lifecycle to the trade DB so it shows up live in
the visualizer. Use it to smoke-test the full pipeline on the demo account
(see TESTING.md).

The account must be registered on a bridge and have an API key. Point this at
either the hosted bridge or a local worker.

Examples
--------
Read the bridge URL + key straight from a Live strategy config:

    python scripts/place_test_trade.py --from-config \
        Live/Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live.json --side sell --volume 0.01

Or pass them explicitly (e.g. the demo test account once it is on a bridge):

    python scripts/place_test_trade.py \
        --bridge https://exness-bridge-mt5.pickleballify.com/415979703/demo \
        --api-key <KEY> --symbol BTCUSD --side buy --volume 0.01

Nothing is left open: the position is trailed a couple of steps and then
closed. Add --no-close to leave it open (so you can watch the floating P/L in
the visualizer), then close it later from the terminal.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "Live"))
import trade_db  # noqa: E402


def _bridge(base, key, method, path, **kw):
    url = base.rstrip("/") + path
    r = requests.request(method, url, headers={"X-Api-Key": key}, timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-config", help="Live/*.json to read MT5_BRIDGE_URL + MT5_API_KEY from")
    ap.add_argument("--bridge", help="bridge base URL .../<login>/<type>")
    ap.add_argument("--api-key")
    ap.add_argument("--symbol", default="BTCUSD")
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--volume", type=float, default=0.01)
    ap.add_argument("--strategy-id", default="TEST-MANUAL")
    ap.add_argument("--trail-steps", type=int, default=2)
    ap.add_argument("--hold", type=float, default=8.0, help="seconds between steps")
    ap.add_argument("--no-close", action="store_true")
    ap.add_argument("--db-url", default=os.environ.get("TRADE_DB_URL", ""))
    args = ap.parse_args()

    base, key = args.bridge, args.api_key
    if args.from_config:
        cfg = json.loads(Path(args.from_config).read_text())
        base = base or cfg.get("MT5_BRIDGE_URL")
        key = key or cfg.get("MT5_API_KEY")
    if not base or not key:
        ap.error("need --bridge + --api-key (or --from-config)")

    trade_db.init(args.strategy_id, args.db_url or None, magic=17999)

    px = _bridge(base, key, "GET", f"/price/{args.symbol}")
    price = px.get("ask") if args.side == "buy" else px.get("bid")
    price = price or px.get("price") or px.get("last")
    sign = 1 if args.side == "buy" else -1
    risk = price * 0.004
    hard_sl = round(price - sign * risk, 2)
    targets = {str(r): round(price + sign * risk * r, 2) for r in (1, 2, 3)}
    event_id = f"test-{int(time.time())}"
    print(f"{args.side.upper()} {args.volume} {args.symbol} @ ~{price}  SL {hard_sl}")

    trade_db.record_signal(event_id, args.symbol, args.side,
                           signal_price=price, qty=args.volume, hard_sl=hard_sl)

    res = _bridge(base, key, "POST", "/trade", json={
        "symbol": args.symbol, "type": args.side, "volume": args.volume,
        "sl": hard_sl, "comment": "viz-test"})
    ticket = res.get("ticket") or res.get("order")
    retcode = res.get("retcode", 0)
    entry = res.get("price") or price
    print("  ->", res)
    trade_db.record_execution(event_id, ticket, retcode, entry_price=entry,
                              qty=args.volume, hard_sl=hard_sl, targets=targets)
    if not ticket or retcode != 10009:
        print("order not accepted; stopping"); return

    sl = hard_sl
    for i in range(1, args.trail_steps + 1):
        time.sleep(args.hold)
        sl = round(entry + sign * risk * i * 0.5, 2)  # ratchet toward profit
        try:
            _bridge(base, key, "POST", "/modify", json={"ticket": ticket, "sl": sl})
            trade_db.record_trail(ticket, sl, ratio=i, executed=True)
            print(f"  trail #{i}: SL -> {sl}")
        except Exception as e:
            trade_db.record_trail(ticket, sl, ratio=i, executed=False)
            print(f"  trail #{i} failed: {e}")

    if args.no_close:
        print(f"left open (ticket {ticket}); close it from the terminal when done")
        return
    time.sleep(args.hold)
    close = _bridge(base, key, "POST", "/close", json={"ticket": ticket})
    pnl = close.get("profit")
    trade_db.record_close(ticket, reason="test_close", pnl=pnl)
    print(f"  closed (ticket {ticket}) pnl={pnl}")


if __name__ == "__main__":
    main()
