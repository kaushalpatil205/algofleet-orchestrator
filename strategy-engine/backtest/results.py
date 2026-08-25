"""Reads back what a replayed strategy wrote.

Stage 1's real output is the strategy's own CSV log, not the recorded trade_db
stream. That is not a quirk of the harness — it is how the live scripts work.
Every setup found in the lookback window is simulated through `run_backtest_v1`
and written as a row carrying the full 1:0.5-1:10 ladder, but only a setup whose
entry falls inside the last `RECENT_1M_COUNT` one-minute bars passes the
`is_new_entry` gate and reaches the execution path. A strided Stage 1 sweep
therefore produces a complete backtest and almost no signals; Stage 2, stepping
a minute at a time, is what exercises execution.
"""

import glob
import os

import pandas as pd

# Columns the S17/S21 rows use for the same concepts under slightly different
# spellings ("BackTest Result" has an inner capital in the live scripts).
_STATUS = "Status"
_EVENT = "Event ID"


def find_logs(outdir):
    return sorted(glob.glob(os.path.join(outdir, "bridge", "**", "*.csv"),
                            recursive=True))


def load_rows(outdir):
    """Every row the run produced, deduped by Event ID (strides overlap)."""
    frames = []
    for path in find_logs(outdir):
        if os.path.basename(path) == "trade_errors.log":
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty:
            continue
        df["_source"] = os.path.basename(path)
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if _EVENT in out.columns:
        out = out.drop_duplicates(subset=[_EVENT], keep="last")
    return out.reset_index(drop=True)


def summarise(outdir):
    df = load_rows(outdir)
    if df.empty:
        return {"setups": 0, "intrade": 0, "statuses": {}, "logs": find_logs(outdir)}

    statuses = (df[_STATUS].value_counts().to_dict()
                if _STATUS in df.columns else {})
    intrade = int(sum(v for k, v in statuses.items() if str(k) == "Intrade"))

    out = {
        "setups": int(len(df)),
        "intrade": intrade,
        "statuses": {str(k): int(v) for k, v in statuses.items()},
        "logs": find_logs(outdir),
    }

    if "Entry Datetime" in df.columns:
        entries = pd.to_datetime(df["Entry Datetime"], utc=True, errors="coerce").dropna()
        if len(entries):
            out["first_entry"] = str(entries.min())
            out["last_entry"] = str(entries.max())
    for col in ("Entry Price", "Hard SL Price", "Trading qty Contract"):
        if col in df.columns:
            vals = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(vals):
                out.setdefault("ranges", {})[col] = [float(vals.min()), float(vals.max())]
    return out
