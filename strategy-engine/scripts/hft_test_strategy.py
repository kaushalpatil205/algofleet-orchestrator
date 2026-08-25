#!/usr/bin/env python3
"""HFT test strategy — fires a trade every N seconds, trails its stop, and
closes it, recording the full lifecycle through Live/trade_db.py so live
activity streams into the visualizer/dashboard. It exists purely to exercise
the rest of the pipeline (persistence, trailing logic, dashboard aggregation)
without waiting on a real strategy's slow signal cadence.

Two modes:
  --sim   (default) writes the lifecycle to the trade DB using a live-seeded
          price walk — fast, safe, no real orders.
  --live  places REAL demo orders on the local MT5 terminal (via the same
          scripts/_mt5_exec.py path as place_local_trade.py). Slower (Wine
          startup per op), so it uses a longer effective cadence.

Safety: stop with Ctrl-C (open sim trades are closed out first); --max-trades
and --duration bound the run; set HALT_HFT=1 in the env to stop firing.

    # sim: a trade every 10s for 2 minutes, into strategy id HFT-TEST
    # (any python with psycopg2+requests; the mt5-orchestrator repo's
    #  trade-dashboard/.venv has both)
    python scripts/hft_test_strategy.py --interval 10 --duration 120

    # live demo orders on the test account
    python scripts/hft_test_strategy.py --live \
        --login 415979703 --password '1Techmero@100' --server Exness-MT5Trial14
"""
import argparse
import os
import random
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "Live"))
import trade_db  # noqa: E402

_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True
    print("\nstopping…")


def _seed_price(symbol):
    """One real price to seed the sim walk (falls back to a constant)."""
    import json
    import requests
    for cfg in (REPO / "Live").glob("*.json"):
        try:
            c = json.loads(cfg.read_text())
            base, key = c.get("MT5_BRIDGE_URL", "").rstrip("/"), c.get("MT5_API_KEY", "")
            if not base or not key:
                continue
            r = requests.get(f"{base}/price/{symbol}", headers={"X-Api-Key": key}, timeout=8)
            j = r.json()
            p = j.get("ask") or j.get("bid") or j.get("price")
            if p:
                return float(p)
        except Exception:
            continue
    return 62000.0


class LiveMT5:
    """Persistent MT5 session (scripts/place_local_trade.Mt5Session) — Wine +
    login once, then millisecond ops, so trails can fire every second."""
    def __init__(self, creds):
        import place_local_trade as plt
        self._session = plt.Mt5Session(creds)
        self.creds = creds

    def __call__(self, op, **kw):
        return self._session(op, **kw)

    def close(self):
        self._session.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy-id", default="HFT-TEST")
    ap.add_argument("--symbol", default="BTCUSD")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between new trades")
    ap.add_argument("--hold", type=float, default=25.0, help="seconds a trade stays open")
    ap.add_argument("--trail-secs", type=float, default=6.0, help="seconds between SL trails")
    ap.add_argument("--volume", type=float, default=0.01)
    ap.add_argument("--risk-frac", type=float, default=0.003,
                    help="risk unit as a fraction of price; the trail rides 0.7x this "
                         "behind price, so smaller = SL hugs the candles (visible when "
                         "zoomed in) and trails fire on smaller moves")
    ap.add_argument("--max-trades", type=int, default=0, help="0 = until duration / Ctrl-C")
    ap.add_argument("--duration", type=float, default=0, help="seconds; 0 = until Ctrl-C")
    ap.add_argument("--live", action="store_true", help="place REAL demo orders on local MT5")
    ap.add_argument("--follow", action="store_true",
                    help="test mode: reposition the SL to a fixed distance from the "
                         "CURRENT price on every trail tick, in both directions — "
                         "guarantees a steady trail cadence whether in profit or loss")
    ap.add_argument("--account", help="login in scripts/mt5_accounts.json (default: file's default)")
    ap.add_argument("--login", type=int)
    ap.add_argument("--password")
    ap.add_argument("--server")
    ap.add_argument("--db-url", default=os.environ.get("TRADE_DB_URL", ""))
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    db = args.db_url or (REPO / "Live" / "trade_db_url.local").read_text().strip()
    trade_db.init(args.strategy_id, db, magic=17999)

    mt5 = None
    if args.live:
        import place_local_trade as plt
        creds = plt.resolve_creds(args)
        if not creds:
            ap.error("--live needs --account (with scripts/mt5_accounts.json) or --login/--password/--server")
        mt5 = LiveMT5(creds)
        info = mt5("info")
        if not (info.get("ok") and info.get("trade_allowed")):
            sys.exit(f"MT5 not ready (enable Algo Trading): {info}")
        print("live on", info.get("login"), info.get("server"))
        # the persistent session makes ops cheap; just keep a sane floor
        args.interval = max(args.interval, 5.0)

    price = _seed_price(args.symbol)
    print(f"HFT {args.strategy_id} {args.symbol} — a trade every {args.interval}s "
          f"({'LIVE' if args.live else 'SIM'}), seed price {price}")

    open_trades = []          # sim: list of live trade dicts
    fired = 0
    started = time.time()
    next_open = started
    fake_ticket = 800000

    def now_ms():
        return int(time.time() * 1000)

    while not _stop:
        if os.environ.get("HALT_HFT") == "1":
            time.sleep(1)
            continue
        t = time.time()
        # random-walk the sim price a little each loop
        price = max(1000.0, price * (1 + random.uniform(-0.0006, 0.0006)))

        # fire a new trade on the cadence
        if t >= next_open and (args.max_trades == 0 or fired < args.max_trades):
            side = random.choice(["buy", "sell"])
            sign = 1 if side == "buy" else -1
            if args.live:
                # re-anchor to the real market — the sim walk drifts, and an SL
                # computed off a drifted price gets rejected (10016 invalid stops)
                pr = mt5("price", symbol=args.symbol)
                if pr.get("ok"):
                    price = (pr["bid"] + pr["ask"]) / 2
            risk = round(price * args.risk_frac, 2)
            event_id = f"hft-{int(t)}-{fired}"
            if args.live:
                sl0 = round(price - sign * risk, 2)
                tp = round(price + sign * risk * 3, 2)
                trade_db.record_signal(event_id, args.symbol, side,
                                       signal_price=price, qty=args.volume, hard_sl=sl0)
                res = mt5("open", symbol=args.symbol, side=side, volume=args.volume, sl=sl0, tp=tp)
                ticket = res.get("ticket")
                entry = res.get("price") or price
                trade_db.record_execution(event_id, ticket, 10009 if res.get("ok") else 0,
                                          entry_price=entry, qty=args.volume, hard_sl=sl0,
                                          targets={str(r): round(entry + sign * risk * r, 2) for r in (1, 2, 3)},
                                          error=None if res.get("ok") else str(res))
                if res.get("ok"):
                    open_trades.append({"event_id": event_id, "ticket": ticket, "side": side,
                                        "entry": entry, "sign": sign, "risk": risk, "sl": sl0,
                                        "opened": t, "last_trail": t, "step": 0, "live": True})
                    print(f"  #{fired} OPEN {side} @ {entry} tkt {ticket}")
            else:
                fake_ticket += 1
                entry = round(price, 2)
                sl0 = round(entry - sign * risk, 2)
                trade_db.record_signal(event_id, args.symbol, side,
                                       signal_price=entry, qty=args.volume, hard_sl=sl0)
                trade_db.record_execution(event_id, fake_ticket, 10009, entry_price=entry,
                                          qty=args.volume, hard_sl=sl0,
                                          targets={str(r): round(entry + sign * risk * r, 2) for r in (1, 2, 3)})
                open_trades.append({"event_id": event_id, "ticket": fake_ticket, "side": side,
                                    "entry": entry, "sign": sign, "risk": risk, "sl": sl0,
                                    "opened": t, "last_trail": t, "step": 0, "live": False})
                print(f"  #{fired} OPEN {side} @ {entry} tkt {fake_ticket}")
            fired += 1
            next_open += args.interval

        # manage open trades: ratchet the stop behind price (a real trail —
        # it follows favorable moves into profit and never loosens), then
        # close after the hold
        cur_px = price
        if args.live and any(t - tr["last_trail"] >= args.trail_secs for tr in open_trades):
            pr = mt5("price", symbol=args.symbol)
            if pr.get("ok"):
                cur_px = (pr["bid"] + pr["ask"]) / 2
        for tr in list(open_trades):
            age = t - tr["opened"]
            if t - tr["last_trail"] >= args.trail_secs:
                desired = round(cur_px - tr["sign"] * tr["risk"] * 0.7, 2)
                improved = tr["sign"] * (desired - tr["sl"]) >= tr["risk"] * 0.2
                followed = args.follow and abs(desired - tr["sl"]) >= tr["risk"] * 0.05
                if improved or followed:
                    tr["step"] += 1
                    r_mult = round(tr["sign"] * (desired - tr["entry"]) / tr["risk"], 1)
                    ratio = r_mult if r_mult >= 0.5 else None
                    if tr["live"]:
                        m = mt5("modify", ticket=tr["ticket"], sl=desired)
                        trade_db.record_trail(tr["ticket"], desired, ratio=ratio, executed=bool(m.get("ok")))
                    else:
                        trade_db.record_trail(tr["ticket"], desired, ratio=ratio, executed=True)
                    tr["sl"] = desired
                    print(f"  TRAIL tkt {tr['ticket']} SL→{desired}")
                tr["last_trail"] = t
            if age >= args.hold:
                if tr["live"]:
                    c = mt5("close", ticket=tr["ticket"])
                    trade_db.record_close(tr["ticket"], reason="hft_close", pnl=c.get("pnl"))
                else:
                    pnl = round((price - tr["entry"]) * tr["sign"] * args.volume, 2)
                    trade_db.record_close(tr["ticket"], reason="hft_close", pnl=pnl)
                open_trades.remove(tr)
                print(f"  CLOSE tkt {tr['ticket']}")

        if args.duration and (t - started) >= args.duration:
            break
        time.sleep(1)

    # close out anything still open on exit
    for tr in list(open_trades):
        if tr["live"]:
            c = mt5("close", ticket=tr["ticket"])
            trade_db.record_close(tr["ticket"], reason="hft_stop", pnl=c.get("pnl"))
        else:
            pnl = round((price - tr["entry"]) * tr["sign"] * args.volume, 2)
            trade_db.record_close(tr["ticket"], reason="hft_stop", pnl=pnl)
    if mt5 is not None:
        mt5.close()
    print(f"done — fired {fired} trades")


if __name__ == "__main__":
    main()
