"""A stand-in `requests` module, seeded into sys.modules before import.

Catches every outbound call the strategies make: bridge candles, positions,
trade/modify/close, and Telegram. It is deliberately the *whole* transport
rather than a patch of individual functions — the strategies reach for requests
from several places (module scope, inside `fetch_live_mt5_candles`, inside
`mt5_bridge_close_ticket`), and replacing the module covers all of them at once.

Unknown routes return 404 *and* are recorded. Silently answering everything
would let a strategy that started calling a new endpoint look healthy while
reading nothing.
"""

import json as _json
from urllib.parse import parse_qs, urlparse


class RequestException(IOError):
    pass


class HTTPError(RequestException):
    pass


class Timeout(RequestException):
    pass


class ConnectionError(RequestException):
    pass


class _Exceptions:
    RequestException = RequestException
    HTTPError = HTTPError
    Timeout = Timeout
    ConnectionError = ConnectionError


exceptions = _Exceptions


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = int(status_code)
        self._payload = payload

    @property
    def text(self):
        try:
            return _json.dumps(self._payload)
        except Exception:
            return str(self._payload)

    @property
    def content(self):
        return self.text.encode()

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPError(f"{self.status_code} for simulated request")


class Router:
    """Routes a bridge URL to the feed, the broker, or the Telegram sink."""

    def __init__(self, feed, broker, symbol_default=None):
        self.feed = feed
        self.broker = broker
        self.symbol_default = symbol_default
        self.telegram = []      # every message the run would have sent
        self.calls = []         # (method, path) transcript
        self.unmatched = []     # routes nothing claimed — a bug signal

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _query(url, params):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        for k, v in (params or {}).items():
            q[str(k)] = str(v)
        return q

    def _candles(self, path, query):
        # .../market/candles/{symbol}   (optionally /range on the newer bridge)
        parts = [p for p in path.split("/") if p]
        try:
            i = parts.index("candles")
        except ValueError:
            return Response(404, {"error": "bad candles path"})
        symbol = parts[i + 1] if len(parts) > i + 1 else self.symbol_default
        tf = query.get("timeframe", "H1")
        count = int(float(query.get("count", 100)))
        payload = self.feed.payload(symbol, tf, count)
        if not payload["candles"]:
            # Same shape the real worker uses, so the strategy's own error
            # handling sees what it would see live.
            return Response(404, {"error": f"no data for {symbol}", "timeframe": tf})
        return Response(200, payload)

    # --- transport ------------------------------------------------------------

    def get(self, url, **kw):
        path = urlparse(url).path
        query = self._query(url, kw.get("params"))
        self.calls.append(("GET", path))

        if "/market/candles" in path:
            return self._candles(path, query)
        if path.endswith("/positions"):
            return Response(200, self.broker.positions())

        self.unmatched.append(("GET", url))
        return Response(404, {"error": "no simulated route", "path": path})

    def post(self, url, **kw):
        parsed = urlparse(url)
        path = parsed.path
        body = kw.get("json") or {}
        self.calls.append(("POST", path))

        if parsed.netloc.endswith("api.telegram.org"):
            self.telegram.append(body.get("text", ""))
            return Response(200, {"ok": True})
        if path.endswith("/trade"):
            return Response(200, self.broker.trade(body))
        if path.endswith("/modify"):
            payload, status = self.broker.modify(body)
            return Response(status, payload)
        if path.endswith("/close"):
            payload, status = self.broker.close(body)
            return Response(status, payload)

        self.unmatched.append(("POST", url))
        return Response(404, {"error": "no simulated route", "path": path})


# --- module surface -----------------------------------------------------------
# Bound by the loader before the strategy is imported.

_router = None


def bind(router):
    global _router
    _router = router


def get(url, **kw):
    return _router.get(url, **kw)


def post(url, **kw):
    return _router.post(url, **kw)


def put(url, **kw):
    return Response(404, {"error": "no simulated route"})


def delete(url, **kw):
    return Response(404, {"error": "no simulated route"})


class Session:
    """Some call sites build a Session; give them one that routes the same way."""

    def get(self, url, **kw):
        return get(url, **kw)

    def post(self, url, **kw):
        return post(url, **kw)

    def mount(self, *a, **kw):
        pass

    def close(self):
        pass
