# Historical candles from the MT5 bridge

How to pull Exness OHLCV history for backtesting and live-vs-backtest
validation, and what the real limits are.

Base URL and credentials:

```
https://exness-bridge-mt5.pickleballify.com/<login>/<account_type>
X-Api-Key: <the account's key>
```

`account_type` is `demo` or `real`. Every example below uses login
`277746877` on `demo`.

> ### Use the history terminal, not a trading account
>
> `277746877` above is the account flagged `data_terminal: true` — it carries
> the large `MaxBars` while the seven trading terminals stay lean. Point history
> calls there, and not only for depth.
>
> A worker serves one request at a time. `/info` probes the series with many
> bounded reads and `/range` blocks while MT5 downloads from the broker, so
> either can leave that account's worker unresponsive for tens of seconds —
> long enough for the bridge to answer everything else with
> `worker for <login> not reachable`.
>
> Observed on `415891519`, a live S17 account: `/info?timeframe=M1` returned
> normally, the next two calls failed, and the watchdog had it healthy again
> ~40s later. Open positions were unaffected — they live on the broker — but for
> that window the strategy got no candles and ran no trailing pass.
>
> Depth differs sharply too. The same `/info` on `415891519` reports
> `bars_available: 99999, limited_by: "max_bars"` — 69.4 days of a 24/7 symbol,
> against 1.42 years of M1 on the history terminal. A depth measured on a
> trading account is off by roughly 7x.
>
> Check which is which with `GET /{login}/{type}` and read `data_terminal`.

---

## Quick start

The one you probably want — a year of M1:

```bash
curl -H "X-Api-Key: $MT5_API_KEY" \
  "https://exness-bridge-mt5.pickleballify.com/277746877/demo/market/candles/EURUSD?timeframe=M1&count=375000" \
  -o eurusd_m1_1y.json
```

Real result: **375,000 bars, 2025-08-01 → 2026-08-05, 37.2 MB, 9.9s.**

Start smaller to check your wiring:

```bash
curl -H "X-Api-Key: $MT5_API_KEY" \
  "https://exness-bridge-mt5.pickleballify.com/277746877/demo/market/candles/EURUSD?timeframe=H1&count=3"
```

```json
{"symbol":"EURUSD","timeframe":"H1","candles":[
  {"time":1785920400,"open":1.15354,"high":1.15423,"low":1.15333,"close":1.15394,"tick_volume":1729},
  {"time":1785924000,"open":1.15395,"high":1.15407,"low":1.15349,"close":1.15391,"tick_volume":1497},
  {"time":1785927600,"open":1.15391,"high":1.15420,"low":1.15391,"close":1.15420,"tick_volume":165}]}
```

`time` is **epoch seconds, UTC**, and marks the bar's *open*. Bars come back
oldest first. `tick_volume` is tick count, not traded volume — Exness is a
broker feed, so there is no real volume.

---

## Endpoints

### `GET /market/candles/{symbol}` — newest N bars

| Param | Default | Notes |
|---|---|---|
| `timeframe` | `H1` | `M1 M5 M15 M30 H1 H4 D1` |
| `count` | `100` | Clamped to just under the terminal's MaxBars |

Walks backwards from now. Use it when you want "the last N bars" and don't
care about exact dates.

### `GET /market/candles/{symbol}/range` — an explicit window

```bash
curl -H "X-Api-Key: $MT5_API_KEY" \
  "https://exness-bridge-mt5.pickleballify.com/277746877/demo/market/candles/EURUSD/range?timeframe=H1&date_from=1767225600&date_to=1767312000"
```

```json
{"symbol":"EURUSD","timeframe":"H1","candles":[
  {"time":1767304800,"open":1.17451,"high":1.17493,"low":1.17451,"close":1.17479,"tick_volume":390},
  {"time":1767308400,"open":1.17483,"high":1.17506,"low":1.17452,"close":1.17506,"tick_volume":772},
  {"time":1767312000,"open":1.17506,"high":1.17560,"low":1.17496,"close":1.17558,"tick_volume":780}]}
```

`date_from` / `date_to` are epoch seconds, both required, `date_from` must be
less than `date_to`. This is the endpoint to use for reproducible backtests —
pin the window and you get the same bars every run.

Generating the timestamps:

```bash
date -u -d '2026-01-01' +%s          # GNU/Linux
python -c "import datetime as d; print(int(d.datetime(2026,1,1,tzinfo=d.timezone.utc).timestamp()))"
```

### `GET /market/candles/{symbol}/info` — how far back can I go?

```bash
curl -H "X-Api-Key: $MT5_API_KEY" \
  "https://exness-bridge-mt5.pickleballify.com/277746877/demo/market/candles/EURUSD/info?timeframe=H1"
```

```json
{"symbol":"EURUSD","timeframe":"H1","earliest":1621256400,"latest":1785927600,
 "bars_available":32046,"max_bars":500000,"limited_by":"broker"}
```

`limited_by` is the useful field:

- **`broker`** — that is genuinely all Exness has. Asking for more is pointless.
- **`max_bars`** — our terminal config is the constraint, not the broker.
  Raising `MT5_DATA_MAX_BARS` and restarting the history terminal gets you more.

Call this before a long backtest rather than discovering the floor halfway
through.

---

## How much history is actually there

Measured on `EURUSD`, August 2026:

| Timeframe | Reach | Bars | Limited by |
|---|---|---|---|
| M1 | ~1.4 years | 500,000 | **max_bars** |
| M5 | 5.2 years | 383,742 | broker |
| M15 | 5.2 years | 128,095 | broker |
| H1 | 5.2 years | 32,046 | broker |

Depth varies by symbol — XAUUSD M5 reaches **7.06 years** (2019-07-15), BTCUSD
H1 reaches 2021-06-17. Use `/info` per symbol instead of assuming.

Two hard limits worth understanding:

1. **A single request cannot exceed the terminal's MaxBars.** Ask for more and
   MT5 returns nothing at all rather than truncating, so the request fails
   instead of degrading. Requests are clamped just under it for you.
2. **MaxBars also bounds what the terminal *retains*.** This is the
   non-obvious one. At the stock 100,000, M1 stopped 97 days back and *no
   amount of date-range paging reached further* — the terminal had already
   discarded the rest. The history terminal now runs at 500,000, which is why
   M1 reaches ~1.4 years. Going deeper on M1 means raising it again and
   restarting, not paging harder.

---

## Timeframes: what actually exists

Every timeframe MT5 supports natively is now served:

```
M1 M2 M3 M4 M5 M6 M10 M12 M15 M20 M30
H1 H2 H3 H4 H6 H8 H12
D1 W1 MN1
```

Anything outside that list returns **400** with the supported set, rather than
being quietly approximated.

> **H2 and M3 were broken until recently, in two different ways.** `TF_MAP`
> held only `M1 M5 M15 M30 H1 H4 D1`, and `/market/candles` fell back to H1 for
> anything missing (`tf_map.get(timeframe, 16385)`) — so a request for H2
> returned **H1 bars labelled H2**, while `/range` and `/info` returned 400.
> Strategy 17's "2hrs Price above/Below EMA" columns were therefore computed
> from H1. Reporting columns only — `check_ema_position` does not gate entries
> or sizing — so no trade was mispriced by it.
>
> Adding strict validation removed the silent fallback but turned H2 into a
> hard 400, breaking S17 and S21 outright. Both are genuine MT5 timeframes that
> were simply absent from the map; they are now served properly. Verified live:
> H2 returns 120-minute spacing, M3 3-minute, H1 unchanged at 60.
>
> If you have stored S17 output from before this, its 2-hour EMA columns came
> from H1 bars and are worth recomputing.

Separately, **H4 and D1 exist but are not archived by default** —
`fetch_history.py` seeds `M1 M5 M15 H1`. Strategy 17 asks for both, so seed them
explicitly before backtesting it:

```bash
# run from a checkout of mt5-orchestrator
python scripts/fetch_history.py --symbol BTCUSD --timeframe H4 --years 2
python scripts/fetch_history.py --symbol BTCUSD --timeframe D1 --years 2
```

Resampling them from H1 instead is a stopgap, not a fix: MT5 aligns H2/H4/D1 to
**broker server time** (Exness runs UTC+2/+3) while a naive resample aligns to
UTC, so derived bars land 2-3 hours off the real ones.

---

## The first request for old data is slow

MT5 downloads a range from the broker the first time anything asks for it. A
cold request can take tens of seconds; the identical request afterwards takes
under a second. Measured: `H1 count=50000` took **10.1s and failed** on a cold
cache, then **0.80s and succeeded** once warm.

So:

- Set a generous client timeout — **180s or more** for anything deep.
- **Retry once** before concluding data is missing. A first-attempt failure on
  an old window usually means the download is still running, not that the data
  is absent.
- Warm the series with a small `/info` call before a big fetch if you want the
  slow part to happen somewhere predictable.

The bridge returns **504** (not 500) when it gives up waiting, so a 504 means
"retry shortly", not "no such data".

---

## Errors

| Status | Meaning | Example body |
|---|---|---|
| 400 | Bad timeframe or window | `{"detail":{"error":"unknown timeframe M7","supported":["D1","H1","H4","M1","M15","M30","M5"]}}` |
| 401 | Missing/invalid key | `{"detail":"Invalid or missing auth. Provide X-Api-Key: <key> ..."}` |
| 404 | Unknown symbol, or nothing in that window | `{"detail":{"error":"symbol NOSUCH not available","mt5_error":[-1,"Terminal: Call failed"]}}` |
| 413 | Window wider than MaxBars | `{"detail":{"error":"window exceeds MaxBars; narrow date_from/date_to","max_bars":500000,"estimated_bars":706260,"timeframe":"M1"}}` |
| 503 | Worker/terminal down | `{"detail":"worker for 277746877 not reachable"}` |
| 504 | Timed out, probably still downloading | `{"detail":"worker ... timed out after 180s — MT5 may still be downloading history, retry shortly"}` |

On **413**, halve your window and retry — the response tells you both the
ceiling and the estimate, so you can size the next request directly. The check
happens before MT5 is called, so an over-wide window fails fast instead of
coming back as a confusing empty result. On **504**, retry the same request.

Rough safe window sizes at `max_bars=500000`: ~1 year of M1, ~4 years of M5,
~13 years of M15. Paginate anything larger, or use the archive below.

Symbol names are broker-specific: `XAUUSD` (gold), `XAGUSD` (silver), `USOIL`
(WTI), `BTCUSD`. There is no `GOLD` or `WTI`. List them with:

```bash
curl -H "X-Api-Key: $MT5_API_KEY" \
  "https://exness-bridge-mt5.pickleballify.com/277746877/demo/symbols?search=XAU"
```

---

## Prefer the archive for backtests

Pulling years of candles through MT5 on every backtest run is slow and puts
load on a terminal that is also paper trading. History is archived once to
S3-compatible storage as Parquet and read from there instead.

Currently archived: **4.32M bars across 25 series**, about 71.5 MB (~17
bytes/bar against ~98 as JSON). Seeded set is `BTCUSD EURUSD USDJPY XAUUSD
XAGUSD USOIL` × `M1 M5 M15 H1`; `AUDUSD H1` arrived on its own, simply by
being requested — see read-through below.

Fetch and archive a symbol:

```bash
export HISTORY_BRIDGE_API_KEY=<key>
python scripts/fetch_history.py --symbol EURUSD --years 2
python scripts/fetch_history.py --probe --symbol XAUUSD   # just show the floors
```

It pages backwards, skips what it already holds, and stops on its own once the
broker stops returning older bars — so re-running after an interruption is
cheap and safe.

Read it back in Python:

```python
# history_store.py lives in the mt5-orchestrator repo
import history_store
from datetime import datetime, timezone

bars = history_store.read_archive(
    "EURUSD", "M1",
    int(datetime(2025, 8, 1, tzinfo=timezone.utc).timestamp()),
    int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()))
# [{'ts': 1754006400, 'open': 1.1712, 'high': ..., 'volume': 42}, ...]
```

`read_archive` pulls the year files it needs out of the bucket on first use and
serves them locally afterwards, so a machine with no MT5 installed — the
dashboard host, a CI runner, your laptop — can read the full history.

**`read_archive` never talks to MT5.** It returns only what has been archived,
so an un-seeded symbol comes back empty. For fetch-on-miss use `ensure_range`:

```python
bars = history_store.ensure_range("AUDUSD", "H1", date_from, date_to, fetch_fn)
```

It checks the bucket, works out which **edges** of the window are missing,
calls `fetch_fn(symbol, timeframe, from, to)` for just those, stores the result
and serves the whole window. Any symbol works, not only the seeded ones: the
first request pays the MT5 fetch, every later one is a Parquet read.

Interior gaps are deliberately left alone — weekends, holidays and halts are
normal market structure, and chasing them would re-request ranges the broker
will never fill, on every single call.

Note the archive returns `ts`, while the bridge returns `time`. Same meaning,
epoch seconds UTC.

Via the dashboard, for a window past the 6000-bar chart ceiling. It needs a
session, so log in first and keep the cookie:

```bash
DASH=https://dashboard.new-cronicle.pickleballify.com

curl -s -c /tmp/dash.jar -X POST "$DASH/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"YOUR_USER","password":"YOUR_PASSWORD"}'

curl -s -b /tmp/dash.jar \
  "$DASH/api/candles/range?symbol=EURUSD&timeframe=M1&date_from=<epoch>&date_to=<epoch>"
```

That route returns `timestamp` in **milliseconds**, while the bridge and
`history_store` use **seconds**. Normalise at the boundary or every bar lands in
1970.

It is **read-through**: it serves what the archive holds and pulls only the
missing edges off the bridge, storing them as it goes. So it works for symbols
nobody has seeded, and the second call for the same window is a local read.
Fetching prefers the account flagged as the history terminal, since that is the
only one retaining deep history.

For a backtest that must be reproducible, pass **`fetch=false`**:

```bash
curl -s -b /tmp/dash.jar \
  "$DASH/api/candles/range?symbol=EURUSD&timeframe=M1&date_from=<epoch>&date_to=<epoch>&fetch=false"
```

That reads the archive only and 404s rather than quietly pulling new bars
mid-run — the difference between a rerun that reproduces and one that does not.

---

## Validating Exness against Binance

The reason this exists: strategies are backtested on Binance data but paper
traded on Exness, so the two feeds need comparing over the same window.

Things that will bite you:

- **Exness is a broker feed, Binance is an exchange feed.** Prices differ by
  spread and there is no common notion of volume — `tick_volume` counts ticks.
- **Weekends.** FX and metals close; crypto does not. Bar counts will not match
  for `EURUSD` or `XAUUSD`, and that is correct, not a gap.
- **Bar timestamps are the open, in UTC**, on both sides. Confirm your Binance
  export uses the same convention before concluding the feeds disagree.
- **Compare on H1 or M15 first.** They reach 5+ years on both sides, so any
  mismatch is a real difference rather than an artefact of M1's shorter reach.

---

## Reference

Maintained in the **mt5-orchestrator** repo and mirrored here for the strategy
engine. Edit it there, not in this file:
[`docs/historical_candles.md`](https://github.com/sumittechmero/mt5-orchestrator/blob/main/docs/historical_candles.md)

- Bridge source: [`mt5_worker.py`](https://github.com/sumittechmero/mt5-orchestrator/blob/main/mt5_worker.py),
  [`main.py`](https://github.com/sumittechmero/mt5-orchestrator/blob/main/main.py)
- Archive: [`history_store.py`](https://github.com/sumittechmero/mt5-orchestrator/blob/main/history_store.py),
  [`scripts/fetch_history.py`](https://github.com/sumittechmero/mt5-orchestrator/blob/main/scripts/fetch_history.py)
- Full API surface: [`docs/api_schema.md`](https://github.com/sumittechmero/mt5-orchestrator/blob/main/docs/api_schema.md)
- `backtest/` in this repo — replays live strategies over this history
