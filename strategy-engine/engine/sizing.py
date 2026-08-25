"""Position sizing and the fixed-risk stop distance.

CARRIED OVER VERBATIM from the Version 1 scripts. Sizing is strategy logic,
not infrastructure, and it is not being redesigned here — this module exists
so the same arithmetic stops being copy-pasted seven times, not to change it.

The JPY branch in particular is empirical: an audit on 2026-07-20 found
USDJPY positions roughly `rate` times under-sized, because Qty (risk divided
by price distance) is expressed in JPY value units, so converting to lots
needs the JPY->USD rate as well. Real USDJPY value is 100000/rate USD per lot
per point. Do not "simplify" this without re-running that audit.
"""

CONTRACT_SIZES = (
    # matched against the uppercased symbol with separators stripped, in order
    ("XAUUSD", 100),
    ("BTC", 1),
    ("JPY", 100000),
    ("OIL", 1000),
    ("WTICOUSD", 1000),
    ("BCOUSD", 1000),
    ("EURUSD", 100000),
)

LOT_STEP = 0.01
MIN_LOT = 0.01


def clean_symbol(symbol):
    return (symbol or "").upper().replace("_", "")


def contract_size(symbol):
    s = clean_symbol(symbol)
    for token, size in CONTRACT_SIZES:
        if token in s:
            return size
    return 1


def price_digits(symbol):
    """Rounding used for stop prices. Same per-symbol choice as Version 1."""
    s = clean_symbol(symbol)
    if "EURUSD" in s:
        return 5
    if "BTC" in s:
        return 2
    return 3


def to_lots(symbol, qty, entry_price=None):
    """Strategy Qty (risk / price-distance) -> broker lots.

    Returns None when qty is missing, so the caller can fall back rather than
    silently sending a minimum-size order.
    """
    if qty in (None, "", "None"):
        return None
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return None
    if q != q:                # NaN
        return None
    # A zero or negative Qty still yields the minimum lot, NOT None. That is
    # Version 1's behaviour and it is load-bearing for the journal column
    # ("Trading qty Contract"), which is diffed against backtest output. It
    # also means a degenerate signal — one whose stop distance came out at or
    # below zero — is still sent as a 0.01 lot order rather than skipped.
    # Preserved deliberately: sizing is strategy logic and is not being
    # redesigned here. Worth revisiting on purpose, not as a side effect.
    s = clean_symbol(symbol)
    lots = q / contract_size(symbol)
    if "JPY" in s:
        try:
            rate = float(entry_price or 0)
        except (TypeError, ValueError):
            rate = 0.0
        if rate > 0:
            lots *= rate
    return max(round(lots, 2), MIN_LOT)


def round_lots(qty):
    """Snap a raw lot figure to the broker's lot step."""
    try:
        raw = float(qty)
    except (TypeError, ValueError):
        raw = MIN_LOT
    raw = max(raw, MIN_LOT)
    return round(round(raw / LOT_STEP) * LOT_STEP, 2)


def fixed_risk_sl(symbol, side, entry_price, lots, risk_usd):
    """The stop that puts exactly `risk_usd` at risk from `entry_price`.

    This is the correction applied after a fill: the order goes out carrying
    the raw signal stop as a placeholder, then the stop is moved here, derived
    from the price the broker actually filled at.
    """
    entry = float(entry_price or 0)
    lots = float(lots or 0)
    if entry <= 0 or lots <= 0:
        return None
    cs = contract_size(symbol)
    if "JPY" in clean_symbol(symbol):
        price_diff = (risk_usd * entry) / (cs * lots)
    else:
        price_diff = risk_usd / (cs * lots)
    d = price_digits(symbol)
    price_diff = round(price_diff, d)
    if side == "sell":
        return round(entry + price_diff, d)
    return round(entry - price_diff, d)
