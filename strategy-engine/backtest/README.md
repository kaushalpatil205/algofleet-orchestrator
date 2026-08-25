# Backtest harness

Runs the **byte-identical** scripts in `Live/` over historical candles. Nothing
under `Live/` is edited, imported differently, or forked — the harness replaces
what a strategy talks to, not what it does.

```bash
pip install -r backtest/requirements.txt

python -m backtest.run \
  --strategy "Live/Strategy 17/Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live.py" \
  --from 2026-06-01 --to 2026-07-01 \
  --source parquet --archive-root /path/to/history
```

## How it works

A strategy is imported by path with its I/O already replaced. Order matters:

| # | Step | Why |
|---|---|---|
| 1 | build the candle source | captures the *real* `requests` before it is shadowed |
| 2 | seed `sys.modules` | `trade_db` and `requests`, **before** the import |
| 3 | chdir into the run dir | the scripts `mkdir "./bridge/..."` at import time |
| 4 | import the strategy | `importlib`, by path |
| 5 | patch module attributes | `datetime`, `_time`, the market-close guards |
| 6 | preload the feed | the last step allowed to touch the network |
| 7 | seal the network | `socket.socket` raises from here on |

Step 2 is the one that matters. The real `trade_db.init()` runs at strategy
*import* time and falls back to a hardcoded CockroachDB URL when handed an empty
one — so a strategy imported without the stub already in place connects to
production, writes a `strategy_registry` row and starts a heartbeat thread.
`loader._assert_persistence_sealed()` checks this after every load, by module
identity *and* by file path.

`main()` is never called: it loops forever and spawns the real trailing thread.
The harness drives `run_live_scan()` and `run_trailing_pass()` itself, so a
replay is single-threaded and deterministic.

## The two stages

**`--stage signal`** sweeps the window and lets each setup run through the
strategy's own `run_backtest_v1`, producing the full 1:0.5–1:10 ladder. The
output is the strategy's own CSV under `runs/<name>/bridge/…`, not the recorded
signal stream — only a setup whose entry lands in the last `RECENT_1M_COUNT`
minute bars passes `is_new_entry` and reaches the execution path, so a strided
sweep yields a complete backtest and almost no signals. That is the live code's
behaviour, not an artefact.

It sweeps in **strides**, not one pass: a strategy only ever sees `LOOKBACK_1M`
minute bars — about 4.2 days at 6000 — so a single scan of a month-long window
would silently report only its last four days.

**`--stage execution`** steps a simulated minute at a time against
`fakes/broker.py`, running the trailing pass on the strategy's own
`TRAIL_INTERVAL_SEC` and settling stops against each bar's range. This is what
exercises fills, the post-entry SL correction and the flatten guards.

Measured cost is ~0.94 s per scan (S17, 6000×M1 + 2500×M5), so roughly 22 min
per simulated day. Shard long windows across processes with overlapping warmup.

## Candle data

Consumes the Parquet archive from mt5-orchestrator PR #31
(`feat/deep-historical-candles`) — it is not a second candle store.

| `--source` | Reads |
|---|---|
| `parquet` | `history/{symbol}/{tf}/{year}.parquet` directly (needs `--archive-root`) |
| `history-store` | `history_store.read_archive()`, which pulls year files from S3 first |
| `dashboard` | `GET /api/candles/range` (returns ms; normalised here) |
| `csv` | whatever CSVs the original backtests used |

Reads are archive-only — never `ensure_range` with a live fetch function — so a
backtest cannot reach MT5, touch the data terminal, or perturb trading.

### Timeframe gaps

S17 requests **M5, M1, H2, H4, D1**. The archive is seeded `M1/M5/M15/H1`
(`fetch_history.py`'s default), and the worker's `TF_MAP` has no H2 at all, so:

| TF | Source |
|---|---|
| M5, M1 | archive |
| H2 | always resampled from H1 — no such timeframe exists in `TF_MAP` |
| H4, D1 | archive if present, else resampled from H1 **with a warning** |

The H4/D1 fallback is a stopgap, not a fix: MT5 aligns them to *broker server*
time (Exness UTC+2/+3) while resampling aligns to UTC, so derived bars sit 2–3
hours off. Bounded in effect — these feed only `check_ema_position`'s reporting
columns and never gate an entry — but the right answer is to seed them:

```bash
python scripts/fetch_history.py --symbol BTCUSD --timeframe H4 --years 2
python scripts/fetch_history.py --symbol BTCUSD --timeframe D1 --years 2
```

`--h2-mode live-h1` reproduces what production serves today, where the worker's
`tf_map.get(timeframe, 16385)` answers an unknown H2 with H1 bars.

## Publishing

```bash
python -m backtest.run ... --publish --db-url "$TRADE_DB_URL" --label "S17 June"
```

Writes one `backtests` row plus `trades` rows with `source='backtest'` and
`backtest_id`, so the run appears on /backtests and plots on /charts.

Opt-in because it writes to the production database. Rows go under
`"<strategy_id>#bt<id>"` rather than the live `strategy_id`: `trades` is
`UNIQUE (strategy_id, event_id)` and `event_id` is deterministic
(`sha256(f"{side}|{fcc_ts}")[:24]`), so a replay of a traded period regenerates
ids that already exist. Scoping keeps backtest rows from colliding with live
ones, and successive runs of the same window from overwriting each other.
Nothing updates or deletes a row it did not insert.

## Parity — the test that matters

```bash
python -m backtest.parity --replay runs/<dir> \
  --strategy-id S17-M3M2-V1-BTCUSDT-SELL --from 2026-06-01 --to 2026-07-01
```

Read-only. Because `event_id` is deterministic, replaying a period that traded
live regenerates exactly the ids in `trades`, so the id sets join directly and a
missed setup is immediately visible.

Fields are judged by how much agreement they deserve: `signal_price`, `hard_sl`
and `qty` come from candles and must match tightly; `entry_price` is a real
broker fill against a simulated one, so divergence there is reported, not
failed. A setup the live system found and the replay did not is always a
failure — that is the harness not reproducing reality.

## Tests

```bash
python -m pytest backtest/tests -q
```

Fixtures synthesise candles in the archive's exact on-disk layout, so the suite
runs without a seeded archive or S3 credentials.
