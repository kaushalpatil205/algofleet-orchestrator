"""MT5 Bridge REST client.

Version 1 had two dialects of this: the S17 scripts logged every call and
recorded failures to disk, while S21's were one-liners that swallowed errors
silently. This is the S17 behaviour, because a rejected order that leaves no
trace is the failure that costs a weekend of not knowing.

Retries live here rather than at the call sites so every caller gets them.
`modify_sl` in particular is retried: a broker refusing a stop move is the
difference between a trailed winner and a frozen stop.
"""

import time

import requests

from .notify import log

OK_RETCODE = 10009      # MT5 TRADE_RETCODE_DONE
BUY, SELL = 0, 1


class Bridge:
    def __init__(self, url, api_key, magic, comment="engine", error_sink=None,
                 timeout=20, candle_timeout=8):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self.magic = magic
        self.comment = comment
        self.timeout = timeout
        self.candle_timeout = candle_timeout
        # called as sink(symbol, message) when an order fails — the strategy
        # points this at its CSV error log
        self.error_sink = error_sink

    # --- plumbing ---------------------------------------------------------

    @property
    def headers(self):
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _fail(self, symbol, msg):
        log(f"[BRIDGE] {symbol}: {msg}")
        if self.error_sink:
            try:
                self.error_sink(symbol, msg)
            except Exception:
                pass

    # --- reads ------------------------------------------------------------

    def positions(self):
        """Open positions on this account, or None when the bridge could not
        be reached. None and [] mean different things: an empty list is
        'nothing open', None is 'do not act on this'."""
        try:
            r = requests.get(f"{self.url}/positions", headers={"X-Api-Key": self.api_key},
                             timeout=10)
            if r.status_code != 200:
                return None
            data = r.json()
            live = data.get("positions", data) if isinstance(data, dict) else data
            return live if isinstance(live, list) else []
        except Exception as e:
            log(f"[BRIDGE] positions failed: {e}")
            return None

    def candles(self, symbol, timeframe, count):
        """Raw candle payload, or [] on any failure."""
        try:
            r = requests.get(
                f"{self.url}/market/candles/{symbol}",
                params={"timeframe": timeframe, "count": count},
                headers={"X-Api-Key": self.api_key}, timeout=self.candle_timeout)
            r.raise_for_status()
            return r.json().get("candles", []) or []
        except Exception as e:
            log(f"[BRIDGE] candle fetch failed [{symbol} {timeframe}]: {e}")
            return []

    def deal_price(self, ticket, days=3):
        """Close price for a finished position, from MT5 deal history.

        Version 1 never recorded an exit price, so the dashboard reconstructed
        it as entry + pnl/qty — correct only at contract size 1, which put
        every XAUUSD exit marker 100x too far from entry (schema commit
        6c3011e). Asking the broker is the fix.

        The bridge's /history takes a day window and returns every deal on the
        account, so this filters to the closing deal for one position:
        `position_id` identifies the position and `entry == 1` marks the deal
        that took it out.
        """
        try:
            r = requests.get(f"{self.url}/history", params={"days": int(days)},
                             headers={"X-Api-Key": self.api_key}, timeout=15)
            if r.status_code != 200:
                return None
            data = r.json()
            deals = data.get("deals", data) if isinstance(data, dict) else data
            if not isinstance(deals, list):
                return None
            for d in reversed(deals):
                try:
                    if int(d.get("position_id") or 0) != int(ticket):
                        continue
                    if int(d.get("entry", 0)) != 1:       # 1 = out
                        continue
                except (TypeError, ValueError):
                    continue
                px = d.get("price")
                if px:
                    return float(px)
        except Exception as e:
            log(f"[BRIDGE] deal history failed for {ticket}: {e}")
        return None

    # --- writes -----------------------------------------------------------

    def place(self, symbol, side, lots, sl=0.0):
        """Market order. -> (ticket, retcode, comment); ticket 0 means no fill."""
        action_type = BUY if side == "buy" else SELL
        payload = {
            "action": 1, "symbol": symbol, "volume": float(lots),
            "type": action_type, "price": 0.0, "sl": float(sl or 0.0),
            "magic": self.magic, "comment": self.comment,
        }
        try:
            r = requests.post(f"{self.url}/trade", json=payload,
                              headers=self.headers, timeout=self.timeout)
            log(f"[BRIDGE] trade HTTP {r.status_code}: {r.text[:300]}")
            j = r.json()
            ticket = int(j.get("order_id", 0))
            retcode = int(j.get("result", 0))
            comment = j.get("comment", "")
            if ticket <= 0 or retcode != OK_RETCODE:
                self._fail(symbol, f"trade failed. retcode={retcode} comment={comment}")
            return ticket, retcode, comment
        except Exception as e:
            msg = f"trade request failed: {e}"
            self._fail(symbol, msg)
            return 0, 0, msg

    def modify_sl(self, ticket, new_sl, retries=3):
        """Move a stop. Retried — a refused move silently frozen is how an
        open winner hands its excursion back to the market."""
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(f"{self.url}/modify",
                                  json={"ticket": int(ticket), "sl": float(new_sl)},
                                  headers=self.headers, timeout=self.timeout)
                log(f"[BRIDGE] modify SL HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code == 200:
                    try:
                        if int(r.json().get("result", 0)) == OK_RETCODE:
                            return True
                    except Exception:
                        return True     # 200 with an unparseable body: treat as done
            except Exception as e:
                log(f"[BRIDGE] modify SL failed (try {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(1.0)
        return False

    def close(self, ticket):
        try:
            r = requests.post(f"{self.url}/close", json={"ticket": int(ticket)},
                              headers=self.headers, timeout=self.timeout)
            log(f"[BRIDGE] close HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                return False
            try:
                return int(r.json().get("result", 0)) == OK_RETCODE
            except Exception:
                return True
        except Exception as e:
            log(f"[BRIDGE] close failed for {ticket}: {e}")
            return False

    def find_fill(self, ticket, tries=3, delay=1.0):
        """The fill price for a just-placed order.

        The position can take a moment to appear, so this polls. Returns None
        if it never shows — the caller must still correct the stop, falling
        back to the signal price, rather than leaving the placeholder on.
        """
        for attempt in range(1, tries + 1):
            time.sleep(delay)
            live = self.positions()
            for p in live or []:
                try:
                    if int(p.get("ticket", 0)) == int(ticket):
                        return float(p.get("price_open"))
                except (TypeError, ValueError):
                    continue
            log(f"[BRIDGE] ticket {ticket} not visible yet (try {attempt}/{tries})")
        return None
