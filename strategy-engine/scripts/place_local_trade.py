#!/usr/bin/env python3
"""Place a REAL demo trade on a locally-running MetaTrader 5 for Mac terminal
(no VPS / hosted bridge needed) and record the whole lifecycle to the trade DB
so it shows up in the visualizer.

It drives MT5 through the terminal's own bundled Wine + embedded Python (which
ships the MetaTrader5 package) via scripts/_mt5_exec.py, and records
signal → open → trailing-SL → close through Live/trade_db.py.

Prereq: MetaTrader 5 for Mac installed and running (this script logs the
terminal into the given account itself). See TESTING.md.

    python scripts/place_local_trade.py \
        --login 415979703 --password '1Techmero@100' --server Exness-MT5Trial14 \
        --symbol BTCUSD --side buy --strategy-id LOCAL-TEST

Nothing is left open unless --no-close is given.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "Live"))
import trade_db  # noqa: E402

APP = "/Applications/MetaTrader 5.app"
WINE = f"{APP}/Contents/SharedSupport/wine/bin/wine64"
PREFIX = str(Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5")
EMBED_PY = r"C:\PythonEmbed\python.exe"


def _ensure_exec_copied():
    dst = Path(PREFIX) / "drive_c" / "_mt5_exec.py"
    dst.write_text((REPO / "scripts" / "_mt5_exec.py").read_text())


def load_account(name=None):
    """Resolve saved localhost MT5 credentials from scripts/mt5_accounts.json.
    `name` may be a login or None (uses the file's default)."""
    cfg_path = REPO / "scripts" / "mt5_accounts.json"
    if not cfg_path.exists():
        return None
    cfg = json.loads(cfg_path.read_text())
    accounts = cfg.get("accounts", {})
    key = str(name or cfg.get("default") or "")
    acc = accounts.get(key)
    return acc


def resolve_creds(args):
    """Fill login/password/server from --account (or the default account) when
    they aren't all passed explicitly."""
    if args.login and args.password and args.server:
        return {"login": args.login, "password": args.password, "server": args.server}
    acc = load_account(getattr(args, "account", None))
    if not acc:
        return None
    return {"login": args.login or acc["login"],
            "password": args.password or acc["password"],
            "server": args.server or acc["server"]}


def _mt5_env(creds):
    return dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all",
                MT5_LOGIN=str(creds["login"]), MT5_PASSWORD=creds["password"],
                MT5_SERVER=creds["server"])


def mt5(op, creds, **kw):
    """Run one MT5 op in the embedded Python under Wine; return parsed JSON.
    Each call pays the full Wine + MT5-login startup (~seconds) — fine for a
    handful of ops; use Mt5Session for op streams."""
    payload = json.dumps({"op": op, **kw})
    p = subprocess.run([WINE, EMBED_PY, r"C:\_mt5_exec.py", payload],
                       capture_output=True, text=True, env=_mt5_env(creds), timeout=120)
    for line in p.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise RuntimeError(f"no RESULT from MT5 op {op}: {p.stdout[-400:]}\n{p.stderr[-400:]}")


class Mt5Session:
    """Long-lived `_mt5_exec.py --serve` process: Wine + MT5 init happen ONCE,
    then each op is a stdin line -> stdout line round-trip in milliseconds.
    This is what makes per-second SL trailing feasible from macOS."""

    def __init__(self, creds):
        _ensure_exec_copied()
        self.p = subprocess.Popen(
            [WINE, EMBED_PY, r"C:\_mt5_exec.py", "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=_mt5_env(creds), bufsize=1)
        ready = self._read()
        if not ready.get("ok"):
            self.close()
            raise RuntimeError(f"MT5 session failed to start: {ready}")

    def _read(self):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("MT5 serve process died")
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])

    def __call__(self, op, **kw):
        self.p.stdin.write(json.dumps({"op": op, **kw}) + "\n")
        self.p.stdin.flush()
        return self._read()

    def close(self):
        try:
            self.p.stdin.write("quit\n")
            self.p.stdin.flush()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="login in scripts/mt5_accounts.json (default: file's default)")
    ap.add_argument("--login", type=int)
    ap.add_argument("--password")
    ap.add_argument("--server")
    ap.add_argument("--symbol", default="BTCUSD")
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--volume", type=float, default=0.0, help="0 = broker min")
    ap.add_argument("--strategy-id", default="LOCAL-TEST")
    ap.add_argument("--trail-steps", type=int, default=2)
    ap.add_argument("--hold", type=float, default=6.0)
    ap.add_argument("--no-close", action="store_true")
    ap.add_argument("--db-url", default=os.environ.get("TRADE_DB_URL", ""))
    args = ap.parse_args()
    creds = resolve_creds(args)
    if not creds:
        ap.error("need --account (with scripts/mt5_accounts.json) or --login/--password/--server")

    _ensure_exec_copied()
    db = args.db_url or (REPO / "Live" / "trade_db_url.local").read_text().strip()
    trade_db.init(args.strategy_id, db, magic=17999)

    # the terminal can briefly report trade_allowed=false right after an
    # account switch; retry a couple times before giving up
    for attempt in range(3):
        info = mt5("info", creds)
        print("account:", info)
        if info.get("ok") and info.get("trade_allowed"):
            break
        time.sleep(4)
    if not info.get("ok"):
        sys.exit("could not log into the terminal")
    if not info.get("trade_allowed"):
        sys.exit("Algo Trading is OFF — enable it in the terminal (toolbar 'Algo Trading') and retry")

    px = mt5("price", creds, symbol=args.symbol)
    if not px.get("ok") or not (px.get("bid") or 0) > 0:
        sys.exit(f"no valid price: {px}")
    symbol = px["symbol"]
    entry_ref = px["ask"] if args.side == "buy" else px["bid"]
    sign = 1 if args.side == "buy" else -1
    risk = round(entry_ref * 0.004, 2)
    sl0 = round(entry_ref - sign * risk, 2)
    tp = round(entry_ref + sign * risk * 3, 2)
    targets = {str(r): round(entry_ref + sign * risk * r, 2) for r in (1, 2, 3)}
    event_id = f"local-{int(time.time())}"
    print(f"{args.side.upper()} {symbol} ~{entry_ref}  SL {sl0}  TP {tp}")

    trade_db.record_signal(event_id, symbol, args.side, signal_price=entry_ref,
                           qty=args.volume or None, hard_sl=sl0)
    opened = mt5("open", creds, symbol=symbol, side=args.side, volume=args.volume,
                 sl=sl0, tp=tp, comment="viz-test")
    print("open ->", opened)
    ticket = opened.get("ticket")
    trade_db.record_execution(event_id, ticket, 10009 if opened.get("ok") else 0,
                              entry_price=opened.get("price"), qty=opened.get("volume"),
                              hard_sl=sl0, targets=targets,
                              error=None if opened.get("ok") else str(opened))
    if not opened.get("ok"):
        sys.exit(f"order rejected: {opened}")

    entry = opened.get("price") or entry_ref
    sl = sl0
    for i in range(1, args.trail_steps + 1):
        time.sleep(args.hold)
        # tighten the stop toward entry from the stop side — stays on the valid
        # side of the market even when price has barely moved in a short test
        sl = round(entry - sign * risk * max(0.15, 1.0 - 0.3 * i), 2)
        m = mt5("modify", creds, ticket=ticket, sl=sl)
        trade_db.record_trail(ticket, sl, ratio=i, executed=bool(m.get("ok")))
        print(f"trail #{i}: SL -> {sl}  {'ok' if m.get('ok') else m}")

    if args.no_close:
        print(f"left OPEN (ticket {ticket}); close it from the terminal or re-run with close")
        return
    time.sleep(args.hold)
    c = mt5("close", creds, ticket=ticket)
    trade_db.record_close(ticket, reason="test_close", pnl=c.get("pnl"))
    print("close ->", c)


if __name__ == "__main__":
    main()
