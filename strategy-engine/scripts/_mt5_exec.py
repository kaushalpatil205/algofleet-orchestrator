"""Embedded-Python MT5 op runner (runs under the MT5-for-Mac bundled Wine's
C:\\PythonEmbed\\python.exe, which ships the MetaTrader5 package).

Two modes:
  argv[1] = JSON command   — one-shot: init, execute, print result, exit.
  argv[1] = --serve        — daemon: init ONCE, then read one JSON command per
                             stdin line and print one result per line. Turns
                             the several-seconds Wine+login cost into a
                             one-time cost so callers get millisecond ops
                             (needed for per-second SL trailing).

Every result is one line prefixed `RESULT `. Credentials come from the
MT5_LOGIN / MT5_PASSWORD / MT5_SERVER environment variables so they are never
on the command line. Used by scripts/place_local_trade.py — see TESTING.md.

Commands: {"op":"info"} | {"op":"price","symbol":..} |
{"op":"open","symbol":,"side":,"volume":,"sl":,"tp":,"comment":} |
{"op":"modify","ticket":,"sl":} | {"op":"close","ticket":}
"""
import json
import os
import sys
import time

import MetaTrader5 as mt5


def _tick(symbol, tries=20):
    """A symbol just added to Market Watch streams its first real tick a
    moment later; poll until bid/ask are populated."""
    for _ in range(tries):
        t = mt5.symbol_info_tick(symbol)
        if t and t.bid > 0 and t.ask > 0:
            return t
        time.sleep(0.4)
    return mt5.symbol_info_tick(symbol)


def _err(msg, extra=None):
    return {"ok": False, "error": msg, "last_error": mt5.last_error(), **(extra or {})}


def _pick_symbol(name):
    """Resolve a symbol name to what this broker actually lists (BTCUSD vs
    BTCUSDm, etc.) and make sure it's in Market Watch."""
    if mt5.symbol_info(name) is not None:
        mt5.symbol_select(name, True)
        return name
    for s in mt5.symbols_get() or []:
        if s.name.upper().startswith(name.upper()):
            mt5.symbol_select(s.name, True)
            return s.name
    return None


def run(cmd):
    op = cmd.get("op")

    if op == "info":
        ai = mt5.account_info()
        ti = mt5.terminal_info()
        if ai is None or ti is None:
            return _err("no account/terminal info")
        return {"ok": True, "login": ai.login, "server": ai.server,
                "balance": ai.balance, "equity": ai.equity, "currency": ai.currency,
                "trade_allowed": ti.trade_allowed and ai.trade_allowed, "build": ti.build}

    if op == "price":
        sym = _pick_symbol(cmd["symbol"])
        t = _tick(sym) if sym else None
        if not t or t.bid <= 0:
            return _err("no tick", {"symbol": sym})
        return {"ok": True, "symbol": sym, "bid": t.bid, "ask": t.ask}

    if op == "open":
        sym = _pick_symbol(cmd["symbol"])
        info = mt5.symbol_info(sym) if sym else None
        if not info:
            return _err("unknown symbol", {"symbol": cmd["symbol"]})
        tick = _tick(sym)
        if not tick or tick.bid <= 0:
            return _err("no tick for order", {"symbol": sym})
        is_buy = cmd["side"] == "buy"
        price = tick.ask if is_buy else tick.bid
        vol = max(info.volume_min, float(cmd.get("volume") or info.volume_min))
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": vol,
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price": price, "deviation": 50,
            "magic": int(cmd.get("magic", 17999)),
            "comment": cmd.get("comment", "viz-test")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if cmd.get("sl"):
            req["sl"] = float(cmd["sl"])
        if cmd.get("tp"):
            req["tp"] = float(cmd["tp"])
        r = mt5.order_send(req)
        if r is None:
            return _err("order_send returned None")
        d = r._asdict()
        # some brokers reject IOC; retry once with FOK
        if r.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
            req["type_filling"] = mt5.ORDER_FILLING_FOK
            r = mt5.order_send(req)
            d = r._asdict() if r else {"retcode": -1}
        return {"ok": r is not None and r.retcode == mt5.TRADE_RETCODE_DONE,
                "retcode": d.get("retcode"), "ticket": d.get("order"), "deal": d.get("deal"),
                "price": d.get("price") or price, "volume": vol, "symbol": sym,
                "comment": d.get("comment")}

    if op == "modify":
        ticket = int(cmd["ticket"])
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return _err("position not found", {"ticket": ticket})
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": pos[0].symbol,
               "sl": float(cmd["sl"])}
        if cmd.get("tp"):
            req["tp"] = float(cmd["tp"])
        r = mt5.order_send(req)
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        return {"ok": ok, "retcode": getattr(r, "retcode", None),
                "ticket": ticket, "sl": float(cmd["sl"])}

    if op == "close":
        ticket = int(cmd["ticket"])
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"ok": True, "already_closed": True, "ticket": ticket}
        p = pos[0]
        tick = mt5.symbol_info_tick(p.symbol)
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": ticket, "price": tick.bid if is_buy else tick.ask,
            "deviation": 50, "magic": p.magic, "comment": "viz-test-close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(req)
        if r and r.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
            req["type_filling"] = mt5.ORDER_FILLING_FOK
            r = mt5.order_send(req)
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        # realized pnl of exactly the closing deal we just did
        pnl = None
        if ok and r.deal:
            d = mt5.history_deals_get(ticket=int(r.deal))
            if d:
                pnl = d[0].profit + d[0].commission + d[0].swap
        return {"ok": ok, "retcode": getattr(r, "retcode", None),
                "ticket": ticket, "deal": getattr(r, "deal", None), "pnl": pnl}

    return _err(f"unknown op {op}")


def _emit(res):
    print("RESULT " + json.dumps(res), flush=True)


def _connect():
    if not mt5.initialize():
        _emit(_err("initialize failed"))
        sys.exit(0)
    login = int(os.environ.get("MT5_LOGIN", "0"))
    pw = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")
    if login and not mt5.login(login, password=pw, server=server):
        _emit(_err(f"login failed for {login}@{server}"))
        sys.exit(0)
    if mt5.account_info() is None or mt5.terminal_info() is None:
        _emit(_err("no account/terminal info"))
        sys.exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        _connect()
        _emit({"ok": True, "serve": True})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line == "quit":
                break
            try:
                res = run(json.loads(line))
            except Exception as e:  # keep serving; one bad op must not kill the session
                res = {"ok": False, "error": str(e)}
            _emit(res)
        mt5.shutdown()
        return

    cmd = json.loads(sys.argv[1])
    _connect()
    try:
        _emit(run(cmd))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
