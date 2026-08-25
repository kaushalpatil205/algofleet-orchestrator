"""CSV trade logs and the fired-event ledger.

Two separate jobs that Version 1 tangled together:

* **The CSV journal** is the strategy's own record — one row per setup, with
  whatever columns that strategy produces. Rows are merged by Event ID so a
  re-scan updates a setup in place instead of appending a duplicate.

* **The fired ledger** is an idempotence guard. It remembers which event ids
  have already produced an order or a Telegram message, so a restart mid-scan
  cannot place a second order for a setup already traded. It is flushed to
  disk on every addition, because the crash it protects against is exactly
  the one that would lose an in-memory set.

The ledger is seeded from the CSVs on startup as well as from its own JSON,
so a deployment that loses the JSON still will not re-fire historical setups.
"""

import json
import threading
from pathlib import Path

import pandas as pd

from .notify import log

LEDGER_NAME = "_fired_events.json"


class Journal:
    def __init__(self, log_dir):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.dir / LEDGER_NAME
        self._fired = set()
        self._lock = threading.Lock()

    # --- fired ledger -----------------------------------------------------

    def load(self):
        """Seed from the ledger JSON, then from any CSV in the directory."""
        try:
            if self.ledger_path.exists():
                with open(self.ledger_path) as f:
                    self._fired.update(json.load(f))
        except Exception as e:
            log(f"[JOURNAL] ledger unreadable, rebuilding from CSVs: {e}")
        for csv in self.dir.glob("*.csv"):
            try:
                df = pd.read_csv(csv)
                if "Event ID" in df.columns:
                    self._fired.update(df["Event ID"].dropna().astype(str).tolist())
            except Exception:
                continue
        log(f"[JOURNAL] {len(self._fired)} fired events known")
        return self._fired

    def has_fired(self, event_id):
        with self._lock:
            return event_id in self._fired

    def mark_fired(self, event_id):
        """Record and flush. Returns False if it had already fired, so the
        caller can use this as the claim on an event."""
        with self._lock:
            if event_id in self._fired:
                return False
            self._fired.add(event_id)
            snapshot = list(self._fired)
        try:
            tmp = self.ledger_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(snapshot, f)
            tmp.replace(self.ledger_path)      # atomic: never a truncated ledger
        except Exception as e:
            log(f"[JOURNAL] could not persist ledger: {e}")
        return True

    # --- CSV --------------------------------------------------------------

    def write_rows(self, filename, rows):
        """Merge rows into a CSV, replacing any row with the same Event ID."""
        if not rows:
            return
        path = self.dir / filename
        new_df = pd.DataFrame(rows)
        if "Event ID" in new_df.columns and path.exists():
            try:
                old = pd.read_csv(path)
                if "Event ID" in old.columns:
                    old = old[~old["Event ID"].astype(str)
                              .isin(new_df["Event ID"].astype(str))]
                    new_df = pd.concat([old, new_df], ignore_index=True)
            except Exception as e:
                log(f"[JOURNAL] could not merge {filename}, overwriting: {e}")
        try:
            new_df.to_csv(path, index=False)
        except Exception as e:
            log(f"[JOURNAL] could not write {filename}: {e}")

    def error(self, symbol, message):
        """Append to the order-error log — the sink Bridge calls on a
        rejected order, so a failure leaves a trace on disk."""
        try:
            from datetime import datetime, timezone
            with open(self.dir / "trade_errors.csv", "a") as f:
                stamp = datetime.now(timezone.utc).isoformat()
                f.write(f'"{stamp}","{symbol}","{str(message).replace(chr(34), chr(39))}"\n')
        except Exception:
            pass
