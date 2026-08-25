"""Backtest harness for the live strategies.

Runs the byte-identical scripts under `Live/` over historical candles by
substituting their I/O at import time — no edits to production code. See
`loader.py` for the injection points and `run.py` for the entry point.
"""
