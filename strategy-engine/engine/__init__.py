"""Shared runtime for MT5 live strategies.

A strategy file declares what it is and how to scan. Everything else —
configuration, the strategy registry and heartbeat, candle fetching and
caching, order placement, post-entry stop correction, indefinite ratio
trailing, market-close flatten, crash recovery, CSV journalling, Telegram —
lives here and is written once.

See engine/runtime.py for the strategy API, and README's Version 2 section
for a worked example.
"""

from .config import Config
from .notify import Telegram, log
from .positions import Book, Position
from .runtime import ScanContext, Signal, Strategy

__all__ = ["Strategy", "Signal", "ScanContext", "Config",
           "Position", "Book", "Telegram", "log"]
