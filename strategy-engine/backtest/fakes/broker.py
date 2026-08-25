"""Simulated MT5, behind the same wire contract the bridge exposes.

Response shapes are copied from what the strategies actually parse, not from the
bridge's models — `mt5_bridge_trade` reads `order_id`/`result`/`comment` and
treats retcode 10009 as the only success, `modify`/`close` are judged on HTTP
200 plus `result == 10009`, and `/positions` is read for `ticket`, `magic`,
`type`, `price_open`, `sl` and `profit`.

Stage 1 needs only fills, so positions opened here stay open: the ratio ladder
that `run_backtest_v1` computes is the outcome of interest, and closing
positions underneath it would double-count exits. Stage 2 drives `settle()` per
bar to model stop-outs.
"""

RETCODE_DONE = 10009

# Mirrors the contract sizes the strategies apply when converting Qty to lots,
# so simulated P/L is expressed in the same units the live rows carry.
CONTRACT = {"XAUUSD": 100, "USDJPY": 100000, "EURUSD": 100000,
            "USOIL": 1000, "BTCUSD": 1}


def contract_size(symbol):
    s = (symbol or "").upper().replace("_", "")
    for key, size in CONTRACT.items():
        if key in s:
            return size
    if "BTC" in s:
        return 1
    if "JPY" in s:
        return 100000
    if "OIL" in s or "WTICOUSD" in s or "BCOUSD" in s:
        return 1000
    return 1


class SimBroker:
    def __init__(self, feed, clock, symbol, spread=0.0, slippage=0.0,
                 reject_modify=None):
        self.feed = feed
        self.clock = clock
        self.symbol = symbol
        self.spread = float(spread)
        self.slippage = float(slippage)
        # Optional predicate(ticket, new_sl) -> True to refuse a modify, so the
        # live rejection path (which un-marks the ratio and retries) can be
        # exercised rather than assumed.
        self.reject_modify = reject_modify
        self._next_ticket = 900000001
        self.open = {}       # ticket -> position dict
        self.closed = []
        self.orders = []     # every placement attempt, for the transcript

    # --- pricing --------------------------------------------------------------

    def price(self, symbol, side=None):
        """Last close at simulated now, adjusted for spread by side."""
        df = self.feed.window(symbol, "M1", 1)
        if df is None or df.empty:
            return None
        px = float(df["close"].iloc[-1])
        if side == 0:                     # buy pays the ask
            px += self.spread
        elif side == 1:                   # sell hits the bid
            px -= self.spread
        return px

    def _bar(self, symbol):
        df = self.feed.window(symbol, "M1", 1)
        if df is None or df.empty:
            return None
        r = df.iloc[-1]
        return float(r["high"]), float(r["low"]), float(r["close"])

    # --- bridge surface -------------------------------------------------------

    def trade(self, payload):
        symbol = payload.get("symbol") or self.symbol
        side = int(payload.get("type", 0))
        volume = float(payload.get("volume", 0) or 0)
        px = self.price(symbol, side)
        if px is None:
            self.orders.append({"symbol": symbol, "accepted": False,
                                "reason": "no price at sim-now"})
            return {"order_id": 0, "result": 0, "comment": "no price"}
        if volume <= 0:
            return {"order_id": 0, "result": 10014, "comment": "Invalid volume"}

        px += self.slippage if side == 0 else -self.slippage
        ticket = self._next_ticket
        self._next_ticket += 1
        self.open[ticket] = {
            "ticket": ticket,
            "symbol": symbol,
            "magic": int(payload.get("magic", 0) or 0),
            "type": side,
            "volume": volume,
            "price_open": px,
            "sl": float(payload.get("sl", 0) or 0),
            "tp": 0.0,
            "profit": 0.0,
            "comment": payload.get("comment", ""),
            "opened_at": self.clock.now().isoformat(),
        }
        self.orders.append({"ticket": ticket, "symbol": symbol, "side": side,
                            "volume": volume, "price": px, "accepted": True})
        return {"order_id": ticket, "result": RETCODE_DONE, "comment": "Request executed"}

    def modify(self, payload):
        ticket = int(payload.get("ticket", 0) or 0)
        new_sl = float(payload.get("sl", 0) or 0)
        pos = self.open.get(ticket)
        if not pos:
            return {"result": 0, "comment": "position not found"}, 404
        if self.reject_modify and self.reject_modify(ticket, new_sl):
            return {"result": 10036, "comment": "Invalid stops"}, 200
        pos["sl"] = new_sl
        return {"result": RETCODE_DONE, "comment": "Request executed"}, 200

    def close(self, payload):
        ticket = int(payload.get("ticket", 0) or 0)
        pos = self.open.get(ticket)
        if not pos:
            return {"result": 0, "comment": "position not found"}, 404
        px = self.price(pos["symbol"], 1 - pos["type"])
        self._retire(ticket, px, "manual_close")
        return {"result": RETCODE_DONE, "comment": "Request executed"}, 200

    def positions(self):
        out = []
        for pos in self.open.values():
            px = self.price(pos["symbol"], 1 - pos["type"])
            if px is not None:
                pos["profit"] = self._pnl(pos, px)
            out.append(dict(pos))
        return out

    # --- stage 2 --------------------------------------------------------------

    def settle(self):
        """Stop out anything the current bar's range touched.

        Uses the bar's extreme rather than its close: a stop sitting inside the
        bar would have been taken intrabar live, and judging on close alone
        flatters every trade that dipped through its stop and recovered.
        """
        for ticket, pos in list(self.open.items()):
            bar = self._bar(pos["symbol"])
            if bar is None:
                continue
            high, low, _ = bar
            sl = float(pos.get("sl") or 0)
            if sl <= 0:
                continue
            hit = (low <= sl) if pos["type"] == 0 else (high >= sl)
            if hit:
                self._retire(ticket, sl, "sl_hit")

    def _retire(self, ticket, px, reason):
        pos = self.open.pop(ticket)
        pos["price_close"] = px
        pos["profit"] = self._pnl(pos, px) if px is not None else 0.0
        pos["close_reason"] = reason
        pos["closed_at"] = self.clock.now().isoformat()
        self.closed.append(pos)
        return pos

    @staticmethod
    def _pnl(pos, px):
        size = contract_size(pos["symbol"])
        delta = (px - pos["price_open"]) if pos["type"] == 0 else (pos["price_open"] - px)
        return delta * pos["volume"] * size
