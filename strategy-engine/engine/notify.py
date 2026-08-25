"""Telegram notifications and console logging.

Every send is best-effort: a Telegram outage must never interrupt trading, so
failures are logged and swallowed. That was already true in Version 1 — this
module just stops each strategy carrying its own copy.
"""

import sys
import threading
from datetime import datetime, timezone

import requests

_lock = threading.Lock()


def log(msg):
    """Console log. Line-buffered and flushed because Cronicle captures
    stdout, and an unflushed buffer means a wedged strategy's last words are
    lost exactly when they are needed."""
    print(msg, flush=True)


def stamped(msg):
    log(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S UTC}] {msg}")


class Telegram:
    def __init__(self, bot_token, chat_id, prefix=""):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage" if bot_token else ""
        self.chat_id = chat_id
        self.prefix = prefix

    def post(self, text):
        if not self.url or not self.chat_id:
            return False
        body = f"{self.prefix}{text}" if self.prefix else text
        try:
            with _lock:
                r = requests.post(self.url, json={"chat_id": self.chat_id, "text": body},
                                  timeout=20)
            return r.status_code == 200
        except Exception as e:
            log(f"[TELEGRAM] send failed: {e}")
            return False


class NullTelegram:
    """Stand-in for replays and probes — same surface, sends nothing."""

    prefix = ""

    def post(self, text):
        return False
