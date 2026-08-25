# Strategy Engine — MT5 Algo Trading System

Algorithmic trading strategies that run against the [MT5 Bridge](https://github.com/sumittechmero/mt5-orchestrator). The strategies fetch real-time market data (Binance API + bridge candles), compute technical indicators, detect trade signals, execute orders via the MT5 Bridge REST API, and manage open positions with dynamic ratio-based trailing stops. Every trade's full lifecycle is persisted to a shared **CockroachDB** store, and a local **trade dashboard** visualizes and operates the whole fleet.

Thirteen strategies run today across three families (17, 18.1, 21). Under **[Version 2](#-version-2--the-shared-engine)** the infrastructure they all share — configuration, candles, execution, trailing, recovery, journalling, persistence, Telegram — lives once in `engine/`, and a strategy file is a short declaration of what it is and how to scan.

---

## 🗺️ System Architecture

```text
                        ┌──────────────────────────────────────────────┐
                        │   Windows VPS  (mt5-orchestrator)            │
   ┌────────────┐       │  ┌──────────┐   ┌───────────────────────┐   │
   │ Strategies │──────▶│  │  Bridge  │──▶│ MT5 terminal workers  │   │
   │ Live/*.py  │ REST  │  │ main.py  │   │ (one per account)     │   │
   └─────┬──────┘       │  └────┬─────┘   └───────────────────────┘   │
         │              │       │ trade_sync.py (60s)                 │
         │ trade_db.py  └───────┼─────────────────────────────────────┘
         ▼                      ▼
   ┌─────────────────────────────────┐         ┌─────────────────────┐
   │   CockroachDB (shared store)  │◀───────▶│  Trade dashboard    │
   │ trades · trade_events ·         │  reads/ │  (local, :8600)     │
   │ equity_snapshots · backtests ·  │  writes │  charts · analytics │
   │ candles                         │         │  ops · alerts       │
   └─────────────────────────────────┘         └──────────┬──────────┘
                                                          │
   Telegram  ◀── strategy signals/exits ──────────────────┘
             ◀── dashboard ops alerts (up/down, trades, drawdown)
```

- **Strategies** (this repo, `Live/`) scan markets and place/manage orders through the bridge; each writes its trade lifecycle to Postgres via `Live/trade_db.py`, **self-registers** in the `strategy_registry` table (unique magic number, account, host, pid) and heartbeats every 60 s — so the dashboard attributes every trade with zero manual configuration.
- **MT5 Bridge** ([mt5-orchestrator](https://github.com/sumittechmero/mt5-orchestrator)) runs on a Windows VPS: one FastAPI gateway + one Python worker per MT5 terminal. Its `trade_sync.py` daemon also mirrors every account's raw MT5 history into the same Postgres store (rows keyed `MT5-<login>`), so history survives even when an account goes offline.
- **Trade dashboard** (`mt5-orchestrator/trade-dashboard/`) is the single pane of glass: live charts, accounts, trades, analytics, backtests, and an ops layer (Telegram alerts, equity recorder, close/flatten, auto-heal).

**Hosted bridge:** `https://exness-bridge-mt5.pickleballify.com`

---

## 📂 Repository Structure

```text
strategy-engine/
├── engine/                                 # Version 2 shared runtime
│   ├── __init__.py                         # the public surface
│   ├── runtime.py                          # Strategy, Signal, ScanContext, both threads
│   ├── config.py                           # env -> strategy JSON -> engine.json -> default
│   ├── candles.py                          # rolling cache, parallel prefetch
│   ├── bridge.py                           # MT5 Bridge REST client
│   ├── execution.py                        # signal -> order -> corrected stop -> ladder
│   ├── trailing.py                         # indefinite ratio ladder + flatten
│   ├── recovery.py                         # re-adopt positions after a restart
│   ├── positions.py                        # the position book
│   ├── journal.py                          # CSV + _fired_events.json
│   ├── db.py                               # persistence, delegating to trade_db
│   ├── indicators.py                       # the indicator stack (proven identical)
│   ├── sizing.py                           # contract sizes, fixed-risk stop
│   ├── sessions.py                         # market hours, flatten window
│   ├── notify.py                           # Telegram + logging
│   ├── fmt.py                              # journal/Telegram value formatting
│   └── singleton.py                        # /proc double-start guard (Linux)
├── Live/
│   ├── engine.json                         # fleet-wide config — GITIGNORED
│   ├── engine.example.json                 # redacted template, committed
│   ├── Strategy 17/
│   │   ├── s17_core.py                     # the S17 logic, shared by all six
│   │   ├── s17_m3m2_v1_btcusd_sell.py      (+ .json)   magic 17201
│   │   ├── s17_m3m2_v1_xauusd_sell.py      (+ .json)   magic 17202
│   │   ├── s17_m1m2_v4_btcusd_buy.py       (+ .json)   magic 17203
│   │   ├── s17_m2m3_v4_xauusd_buy.py       (+ .json)   magic 17204
│   │   ├── s17_m2m3_v4_forex_buy.py        (+ .json)   magic 17205
│   │   ├── s17_m1m2_v1_forex_sell.py       (+ .json)   magic 17206
│   │   └── Bridge-*.py                     # Version 1 originals, still gated by CI
│   ├── Strategy 21/
│   │   ├── s21_core.py                     # the S21 logic (holds the strategy id)
│   │   ├── s21_btcusd.py                   (+ .json)   magic 21102
│   │   └── Bridge-S21-*.py                 # Version 1 original
│   ├── Strategy 18.1/                      # self-contained V1-style, "MANAGED": true
│   │   ├── 1H-5min/Bridge-Strategy-18.1-*.py   (+ .json)   magics 18201-18202
│   │   ├── 2H-15min/Bridge-Strategy-18.1-*.py  (+ .json)   magics 18203-18204
│   │   └── 4H-15min/Bridge-Strategy-18.1-*.py  (+ .json)   magics 18205-18206
│   ├── trade_db.py                         # persistence + registry + heartbeat
│   └── repair_trail_state.py
├── backtest/                               # replays Live/ scripts over history
├── db/
│   └── schema.sql
├── docs/
├── scripts/
│   ├── sync_cronicle.py                    # reconcile the Cronicle schedule
│   ├── gen_s17_core.py                     # regenerate the shared S17 logic
│   ├── gen_s17_variants.py                 # regenerate the six declarations
│   ├── gen_s21_core.py
│   └── place_local_trade.py, ...           # local MT5 test tooling
├── test/
│   ├── test_engine.py                      # the engine, end to end, faked broker
│   ├── test_indicators.py                  # pinned against the V1 originals
│   ├── test_migration_parity.py            # V1 vs V2, every column
│   ├── test_frame_prep.py                  # each strategy's own call sites
│   ├── test_singleton.py                   # the /proc double-start guard
│   ├── test_shared_config_optional.py      # no strategy may require engine.json
│   ├── test_sync_cronicle.py
│   ├── test_live_execution.py              # the CI gate
│   └── live_execution_probe.py
├── output/
├── reference/                              # REFERENCE ONLY, never run
├── TESTING.md
└── README.md                               # This file
```

---

## 📈 Live Strategies

Thirteen strategies across three families. Each has a `.py` entry file and a
sibling `.json` config, and each stamps its own **unique EA magic number** (from
the config's `MAGIC` key) on every MT5 order — so raw account history identifies
the strategy forever. Magic blocks are allocated by family and generation, and
retired magics are never reused; `strategy_registry.magic` is `UNIQUE`, so a
collision is rejected loudly at startup rather than quietly shared.

| Block | Who |
|---|---|
| `171xx` | Strategy 17, production (Version 1) |
| `172xx` | Strategy 17, Version 2 shadows |
| `181xx` | Strategy 18.1, production |
| `182xx` | Strategy 18.1, shadows |
| `21001` / `21002` | Strategy 21, production |
| `21102` | Strategy 21, Version 2 shadow |
| `17001` | legacy shared magic, from before per-strategy allocation |
| `17999` | the HFT test driver |

### Strategy 17 (`Live/Strategy 17/`) — six variants

All six share `s17_core.py` and differ only in direction, symbols, and
entry-method pairing. Under Version 2 each is a ~35-line declaration.

| Prod | Shadow | Strategy id | Direction | Symbols | Entry methods |
|---|---|---|---|---|---|
| `17101` | `17201` | `S17-M3M2-V1-BTCUSDT-SELL` | Sell | BTCUSD | M3 setup + M2 execution |
| `17102` | `17202` | `S17-M3M2-V1-XAUUSD-SELL` | Sell | XAUUSD | M3 setup + M2 execution |
| `17103` | `17203` | `S17-M1M2-V4-BTCUSDT-BUY` | Buy | BTCUSD | M1 + M2 in parallel |
| `17104` | `17204` | `S17-M2M3-V4-XAUUSD-BUY` | Buy | XAUUSD | M2 + M3 in parallel |
| `17105` | `17205` | `S17-M2M3-V4-FOREX-BUY` | Buy | USDJPY, EURUSD, USOIL | M2 + M3 in parallel |
| `17106` | `17206` | `S17-M1M2-V1-FOREX-SELL` | Sell | USDJPY, EURUSD, USOIL | M1 + M2 in parallel |

### Strategy 21 (`Live/Strategy 21/`) — one script, both directions

| Prod | Shadow | Strategy id | Direction | Symbols | Setup |
|---|---|---|---|---|---|
| `21002` | `21102` | `S21-BTCUSD-LIVE` | Buy + Sell (one script) | BTCUSD | 15m three-wick setup → 3m EMA50 entry → 1m tracking |

Strategy 21 is a different engine from S17 — ported verbatim from `reference/Strategy 21/Strategy_21_Live_Exness_Bridge_Script.py` (see the `.txt` write-up there for the full logic): 15-minute three-candle wick patterns, 3-minute EMA50-filtered entries, a Fibonacci **Hard SL** derived from an RSI→MACD-cycle chain on both 3m and 15m (conservative pick, ×1.30 buffer for sizing `Qty = $500 / adj SL points`), a 5-indicator **Soft SL**, and two-pass **Stage-2** exit confirmation. Its execution model is **one market order per newest Intrade signal with the Hard SL attached at placement**, then corrected to the fixed-risk stop from the actual fill. Two things this document previously got wrong, corrected during the Version 2 migration: S21 **does** perform broker-side ratio trailing, and its Stage-2 / Soft-SL exits are **not** simulation only — `process_telegram_exits` closes the position at the broker. Under Version 2 its ratio ladder is measured from the corrected stop rather than the raw signal stop, so 1R is the money actually at risk.

Its declaration is `s21_btcusd.py`, but the strategy **id lives in `s21_core.py`**,
not the entry file — `sync_cronicle` looks in the sibling core when the entry
file does not name one.

### Strategy 18.1 (`Live/Strategy 18.1/`) — six bridges

The same setup logic across three timeframe pairings × two symbols. Unlike S17
and S21 these are **not migrated onto the engine**: each is a self-contained
Version 1-style script that calls `trade_db.init(...)` itself, opted into the
shared tooling with `"MANAGED": true`. They are nested one level deeper
(`Live/Strategy 18.1/<pairing>/`), which is why their `sys.path` bootstrap uses
a different number of `dirname` calls.

| Prod | Shadow | Strategy id | Symbol | Setup → execution |
|---|---|---|---|---|
| `18101` | `18201` | `S18.1-1H-5min-BTCUSDT-LIVE` | BTCUSD | 1H → 5min |
| `18102` | `18202` | `S18.1-1H-5min-XAUUSD-LIVE` | XAUUSD | 1H → 5min |
| `18103` | `18203` | `S18.1-2H-15min-BTCUSDT-LIVE` | BTCUSD | 2H → 15min |
| `18104` | `18204` | `S18.1-2H-15min-XAUUSD-LIVE` | XAUUSD | 2H → 15min |
| `18105` | `18205` | `S18.1-4H-15min-BTCUSDT-LIVE` | BTCUSD | 4H → 15min |
| `18106` | `18206` | `S18.1-4H-15min-XAUUSD-LIVE` | XAUUSD | 4H → 15min |

Production 18.1 (still on PR #47) trades BTCUSDT on account `414122125` and
XAUUSD on `416119992`, and persists to a **different CockroachDB cluster** from
the rest of the fleet. In production the two `2H-15min` scripts register under
the `1H-5min` strategy id — a copy-paste bug, left in place deliberately so
their existing history stays joinable. The shadows here have it corrected.

### Where they run

| | Account | Cronicle category | Owner |
|---|---|---|---|
| Production (V1) | per strategy — see each `.json` | Kaushal Jobs | kaushal |
| Version 2 shadows | `463858748` ("CI/CD Account Test", demo) | Sumit Jobs | sumit |

All 13 shadows carry `"STRATEGY_ID_SUFFIX": "-V2"`, so their runtime
`strategy_id` is the id above plus `-V2`, and their rows never touch the
production ones. See [Shadow deployment](#shadow-deployment) for why every one
of those axes has to be separated.

### Strategy internals

- **Setup timeframe (5-minute):** final strategy candle logic based on MACD cycles, KAMA (Kaufman Adaptive Moving Average) levels, Bollinger Bands, and Heikin-Ashi calculations.
- **Execution timeframe (1-minute):** two entry models race to trigger:
  - **Method 1 (M1):** backward search from the setup candle for backward/forward threshold crossings.
  - **Method 2 (M2):** forward key-candle detection and level breaks.
  - **Method 3 (M3):** setup-candle variant used by the M3-paired strategies.
  - *First method to signal wins; ties resolve to the lower-numbered method.*
- **Trend filtering (EMA context):** checks whether the previous completed candle closed above/below EMA 50/100/200 on 2H, 4H, and 1D.
- **Exit management:** ratio-based P&L exits from `1:0.5` to `1:10` risk-reward; the stop trails two ratios behind price — `1:2` moves it to breakeven, `1:3` to `1:1`, and so on. Trailing does not stop at the last reported ratio: past `1:10` the ladder is extrapolated from its own 1R step and keeps ratcheting until the stop is taken out, so a runner is never left with a frozen stop (every move is recorded as a `TRAIL_MOVE` event).
- **Crash recovery:** on startup a strategy calls `trade_db.load_open_trades()` and re-adopts any OPEN positions recorded in Postgres, so a restart never orphans a live trade. The recovery loop only adopts positions carrying the strategy's own magic — when changing a strategy's `MAGIC`, do it while it has no open positions.
- **Market-close flatten:** in the last minutes before a symbol's session close (NY wall clock, DST-proof) the strategy closes its own positions — weekend close always by default, the oil/gold daily 17:00 ET break optionally — so trades never sit through a reopening gap (see [MARKET_CLOSE_FLATTEN.md](MARKET_CLOSE_FLATTEN.md)).
- **Dedicated stop-management thread:** trailing, market-close flatten, and closed-position detection run on their own thread every `TRAIL_INTERVAL_SEC` (default 10 s), fully decoupled from signal scanning — a slow candle fetch can never delay or skip stop management. Scans themselves fetch all five timeframes in parallel (candle fetch timeout 8 s) and fire once per minute even when the previous pass overran the minute boundary.
- **Post-entry SL correction (hardened):** orders are placed with the raw signal SL as a placeholder, then corrected to the exact fixed-risk SL from the actual fill price. The fill lookup retries ×3; if it still fails, the correction falls back to the signal entry price rather than leaving the placeholder SL; the SL modify itself retries ×3 and a failure raises a Telegram alert for manual action.
- **Self-registration:** `trade_db.init()` publishes the strategy in `strategy_registry` (magic, label, account, host, pid) and starts a 60 s heartbeat — the dashboard's `/strategies` page shows LIVE/SILENT per strategy and alerts when a heartbeat is lost.
- **Telegram:** entries, exits, errors, and trailing-stop actions are broadcast to the configured channel.

### Config format

Settings are split in two. Fleet-wide values live in `Live/engine.json`
(**gitignored**, deployed to the host, seeded from the `ENGINE_CONFIG_JSON`
secret); a strategy's own sidecar holds only what genuinely differs.

**`Live/engine.json`** — shared by every strategy:

```json
{
    "BOT_TOKEN":      "<telegram bot token>",
    "CHAT_ID":        "<telegram chat id>",
    "TRADE_DB_URL":   "postgresql://...cockroachlabs.cloud:26257/defaultdb?sslmode=verify-full",
    "MT5_API_KEY":    "ak_...",
    "FLATTEN_BEFORE_WEEKEND": true,
    "FLATTEN_BEFORE_DAILY_BREAK": false,
    "FLATTEN_LEAD_MIN": 10,
    "TRAIL_INTERVAL_SEC": 10,
    "RISK_PER_TRADE": 100
}
```

**`Live/<family>/<strategy>.json`** — one strategy's own:

```json
{
    "MAGIC":          17201,
    "MT5_BRIDGE_URL": "https://exness-bridge-mt5.pickleballify.com/<login>/<type>",
    "RISK_PER_TRADE": 100,
    "STRATEGY_ID_SUFFIX": "-V2",
    "ALLOW_DUPLICATE": true,
    "MANAGED": true
}
```

The strategy layer wins over the shared one, so a pre-Version-2 config still
carrying `BOT_TOKEN` or `TRADE_DB_URL` keeps working unchanged. The last three
keys are tooling rather than trading: `STRATEGY_ID_SUFFIX` and `ALLOW_DUPLICATE`
make an instance a [shadow](#shadow-deployment), and `MANAGED` opts a
self-contained Version 1-style script into `sync_cronicle`'s management.

`RISK_PER_TRADE` (USD, default 100) sets the fixed dollar risk per trade —
`qty = RISK_PER_TRADE / SL distance` — so a hard-SL hit loses that amount
(± lot-step rounding). All strategies, S21 included, size this way; S21's
original ×1.30-padded divisor was replaced on 2026-07-17 (user-directed).

The dashboard reads these same configs (via `LIVE_CONFIG_DIR`) to discover which account each strategy trades on.

---

## 🗄️ Trade Persistence (`Live/trade_db.py` + CockroachDB)

One shared Postgres database is the source of truth for **all** trading activity — strategy-recorded trades, bridge-mirrored MT5 history, dashboard equity snapshots, and uploaded backtests.

### Tables

| Table | Written by | Purpose |
|---|---|---|
| `trades` | strategies (`trade_db.py`), bridge (`trade_sync.py`), dashboard (backtest upload) | One row per trade/signal with full lifecycle state |
| `trade_events` | strategies | Append-only audit trail (`TRAIL_MOVE`, recovery notes, …) |
| `strategy_registry` | strategies (`trade_db.init`) | Self-registration: magic (`UNIQUE`), label, account, host, pid, 60 s heartbeat (`last_seen`) |
| `equity_snapshots` | dashboard ops loop | Per-account balance/equity time series (every 60 s) |
| `backtests` | dashboard | Registry of uploaded backtest batches |
| `candles` | dashboard | Uploaded OHLC data for plotting backtests |

### Trade status lifecycle

```text
SIGNAL ──▶ OPEN ──▶ CLOSED          signal → MT5 accepted (retcode 10009) → position exited
   │
   ├─────▶ REJECTED                 MT5 rejected the order
   └─────▶ ERROR                    unexpected failure while processing
```

### `trade_db` API used by strategies

```python
import trade_db
# resolves the DB URL automatically, self-registers the strategy (magic,
# account parsed from the bridge URL, host, pid) and starts the heartbeat
trade_db.init("S17-M3M2-V1-BTCUSDT-SELL", magic=MAGIC, bridge_url=MT5_BRIDGE_URL)

trade_db.record_signal(event_id, symbol, side, signal_price=..., qty=..., hard_sl=..., targets={...})
trade_db.record_execution(event_id, ticket, retcode, entry_price=..., qty=...)
trade_db.record_trail(ticket, new_sl, ratio)             # each trailing-stop move
trade_db.record_close(ticket, reason="sl_hit", pnl=...)  # position gone from MT5
trade_db.load_open_trades()                              # crash recovery on startup
```

The DB URL resolves in priority order: `TRADE_DB_URL` env var → the strategy's `.json` config → the shared `Live/engine.json` → `Live/trade_db_url.local` → a hardcoded fallback in `trade_db.py`. Persistence is **fail-open**: if Postgres is unreachable the strategy keeps trading and logs the miss.

`record_trail` is **truth-keeping**: only a move MT5 actually accepted (`executed=True`) advances `current_sl`/`trail_hits` in the DB; a rejected modify is logged as an audit event (`executed:false`, shown in red in the dashboard's trail history) without drifting the stored stop away from the broker's.

### Trail-state reconciliation (`Live/repair_trail_state.py`)

Maintenance tool born from the 2026-07-09 market-close incident (full post-mortem in
[`TRAIL_SL_FIX.md`](TRAIL_SL_FIX.md)): a degenerate candle can make a strategy record trail moves
the broker rejected, leaving the DB claiming an SL the broker never accepted and marking trail
ratios as "used" so the trade never trails again — **and that bad state survives restarts**, because
crash recovery (`load_open_trades`) faithfully reloads it. The strategies can't self-heal state
they re-read as truth, hence an external reconciler:

- scans **every OPEN trade of every strategy and symbol**, rebuilds `trail_hits` + `current_sl`
  from that trade's *executed* `TRAIL_MOVE` events only, and resets anything rejected moves left behind;
- **idempotent** (re-running finds nothing to do) and side-effect-free beyond the repaired rows;
- run it from the directory containing `trade_db.py`:

```bash
python3 repair_trail_state.py --dry-run   # report mismatches only
python3 repair_trail_state.py            # report + apply
```

Run it after any incident that produced `executed:false` trail moves, and once at deploy time of
the trail-fix release — then restart the affected strategy jobs so crash recovery reloads the
cleaned state (a running process keeps its in-memory trail set until restarted).

### Bridge-side mirror (`trade_sync.py`)

The bridge daemon polls every account's positions + deal history each minute and upserts them as `strategy_id = "MT5-<login>"` rows, deduped against strategy-recorded tickets. Result: even manual trades and accounts that later go offline keep their full history in the shared DB.

---

## 📊 Trade Dashboard

Lives in [mt5-orchestrator](https://github.com/sumittechmero/mt5-orchestrator) at `trade-dashboard/`. Runs locally and polls live data every 2 s while a page is open.

```bash
cd /path/to/mt5-orchestrator/trade-dashboard
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time
export TRADE_DB_URL="postgresql://…"                     # or auto-resolved from Live configs
export LIVE_CONFIG_DIR="/path/to/strategy-engine/Live"   # this repo's Live/ folder
./.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8600
```

Open **http://localhost:8600**.

| Page | What it does |
|---|---|
| `/` Dashboard | Fleet overview: accounts strip, today's KPIs, strategy health grid, P/L charts |
| `/charts` | Candle charts (exact Exness prices) with the full trade lifecycle overlaid — entry, hard SL, trailing-SL staircase, targets, close. Click any trade anywhere to open it here |
| `/accounts` | Every known terminal with live state (`online / key mismatch / offline / not on bridge`), nickname, API-key editing, per-account page |
| `/account?login=…` | Single account: stats, **open positions with per-position Close + guarded Flatten-all + Restart-worker buttons**, bridge/worker logs, full trade history |
| `/trades` | Cross-strategy trade feed (DB + raw MT5 history merge) with **time-window filtering — last 24 h / 3 d / 7 d / 30 d / 90 d / all time / custom from→to dates** — plus strategy/account/source/status filters, a live summary line, click-to-chart |
| `/strategies` | The **strategy registry**: one status card per self-registered strategy — LIVE (pulsing, heartbeat < 3 min) / SILENT / NOT REGISTERED — with magic, account, host + pid, heartbeat age, trade stats |
| `/analytics` | Win rate, profit factor, avg win/loss, avg hold, net P/L — **broken down by strategy, by symbol, by strategy × symbol, and by account** (tabbed), with strategy + symbol filters scoping the whole report, Net-P/L bar charts, **equity curves** from recorded snapshots, and the **Ops panel** |
| `/backtests` | Upload backtest CSVs and candle sets; compare against live results |

### Ops layer (dashboard-side — no strategy changes needed)

Configured from the **Ops panel** on `/analytics`; both features ship **disabled**:

- **Telegram alerts** — account up/down transitions, trade `OPEN/CLOSED/REJECTED/ERROR` (including signals MT5 rejected), **strategy heartbeat lost/back**, and drawdown alerts when floating loss exceeds a configurable % of balance. Includes a "Send test" button and an action log.
- **Auto-heal** — when a registered account's worker returns HTTP 500 (wedged terminal), the dashboard restarts it via the bridge automatically, with a per-worker cooldown.
- **Equity recorder** — always on; snapshots every account's balance/equity to `equity_snapshots` each minute, building real equity curves over time.

---

## 🔌 MT5 Bridge API Reference

Every MT5 account added to the orchestrator is exposed via its own bridge URL.

- **Base URL:** `https://exness-bridge-mt5.pickleballify.com/<login>/<account_type>`
- **Auth:** header `X-Api-Key: ak_...` (per-account key)

| # | Method & path | Purpose |
|---|---|---|
| 1 | `GET /<login>/<type>` | Account info: balance, equity, margin, status |
| 2 | `GET /<login>/<type>/positions` | Open positions |
| 3 | `POST /<login>/<type>/trade` | Place a market/pending order |
| 4 | `POST /<login>/<type>/close` | Close a position (full or partial) |
| 5 | `POST /<login>/<type>/modify` | Modify SL/TP of an open position |
| 6 | `GET /<login>/<type>/history?days=N` | Deal history |
| 7 | `GET /<login>/<type>/market/candles/<symbol>?timeframe=M5&count=800` | OHLC candles |
| 8 | `POST /<login>/<type>/restart` | Restart the account's worker + terminal |
| 9 | `POST /<login>/<type>/headless` | Show/hide the terminal window |
| 10 | `GET /accounts` | List all registered accounts (any valid key) |
| 11 | `GET /logs?which=main\|worker&login=N&lines=200` | Bridge / worker logs remotely |

### 1. Get account information

```bash
curl -H "X-Api-Key: ak_..." "https://exness-bridge-mt5.pickleballify.com/279637220/demo"
```

```json
{
  "login": 279637220,
  "server": "Exness-MT5Trial8",
  "currency": "USD",
  "balance": 100000.0,
  "equity": 100000.0,
  "profit": 0.0,
  "margin_free": 100000.0,
  "account_type": "demo",
  "is_real": false,
  "status": "running",
  "name": "sumittechmero",
  "timestamp": 1781495778
}
```

### 2. Get open positions

```bash
curl -H "X-Api-Key: ak_..." ".../279637220/demo/positions"
```

```json
[
  {
    "ticket": 1234567,
    "symbol": "EURUSD",
    "type": 1,
    "volume": 0.1,
    "price_open": 1.0850,
    "price_current": 1.0855,
    "profit": 5.00,
    "sl": 0.0,
    "tp": 1.0900,
    "magic": 17001,
    "time": 1781495000
  }
]
```

`type`: `0` = Buy, `1` = Sell.

### 3. Place a trade

`POST /<login>/<type>/trade`

```json
{
  "action": 1,        // 1 = DEAL (market), 5 = PENDING
  "symbol": "EURUSD",
  "volume": 0.1,
  "type": 1,          // 0 = Buy, 1 = Sell
  "price": 0.0,       // required for pending orders; 0.0 for market
  "sl": 1.0900,
  "tp": 1.0700,
  "magic": 17001,
  "comment": "Bot"
}
```

```json
{ "order_id": 1234568, "result": 10009, "comment": "Request executed" }
```

`result` `10009` = `TRADE_RETCODE_DONE` (success).

### 4. Close a position

`POST /<login>/<type>/close`

```json
{ "ticket": 1234567, "volume": 0.1 }   // omit volume to close fully
```

### 5. Modify SL/TP

`POST /<login>/<type>/modify`

```json
{ "ticket": 1234567, "sl": 1.0850, "tp": 1.0950 }
```

### 6. Deal history

```bash
curl -H "X-Api-Key: ak_..." ".../279637220/demo/history?days=3"
```

Returns deals with `ticket`, `position_id`, `symbol`, `type`, `entry` (`0` = in, `1` = out), `volume`, `price`, `profit`, `commission`, `swap`, `magic`, `comment`, `time`.

### 8. Restart a worker

```bash
curl -X POST -H "X-Api-Key: ak_..." ".../415891589/demo/restart"
```

Kills and relaunches the account's worker + terminal — used by the dashboard's auto-heal to recover wedged workers.

### 11. Remote logs

```bash
curl -H "X-Api-Key: ak_..." ".../logs?which=worker&login=415891589&lines=200"
```

`which=main` → the bridge's own stdout/stderr; `which=list` → available log files. Any valid account key works (single-tenant deployment).

---

## 🚀 Execution & Deployment

### Deployment topology

| Component | Where | How |
|---|---|---|
| MT5 Bridge + workers | Windows VPS | pm2 (`pm2.config.cjs`), Cloudflare tunnel → `exness-bridge-mt5.pickleballify.com` |
| Strategies (`Live/*.py`) | Strategy host | **Cronicle**, one Python process per strategy. Not PM2 — see [Why not PM2](#why-not-pm2) |
| Trade dashboard | Local machine | uvicorn on `:8600` |
| Trade database | CockroachDB (managed) | shared by all components |

### Why not PM2

Strategies run under **Cronicle**, and nothing in this repo should start them
with PM2. The repo used to carry `ecosystem.config.cjs`, declaring a PM2 app per
strategy. Those apps were stopped when the fleet moved to Cronicle, but stayed
*registered* — and `pm2 startOrRestart` starts stopped apps. Running it, whether
from CI or by hand, would have launched a second copy of every S17/S21 strategy
alongside the Cronicle ones: two processes, same magic number, same account,
duplicate orders on live money.

The CI step that did this was removed first, then the file itself, so the trap
cannot be re-armed. If a PM2 app for a strategy still lingers on the host,
`pm2 delete` it and `pm2 save` — deleting the file here does not touch PM2's own
saved process list.

PM2 is still correct for the **bridge** (`mt5-orchestrator/pm2.config.cjs`),
which is a long-running server rather than a scheduled job.

### Run a live strategy

Each strategy reads its sibling `.json` config plus the shared `Live/engine.json`.
Make sure the bridge is reachable, then run it by path from the repo root:

```bash
python "Live/Strategy 17/s17_m3m2_v1_btcusd_sell.py"
python "Live/Strategy 21/s21_btcusd.py"
python "Live/Strategy 18.1/1H-5min/Bridge-Strategy-18.1-1H-5min-XAUUSD-Live.py"
```

The strategy puts the repo root on `sys.path` itself, so `engine/` and
`trade_db.py` resolve wherever it is started from — see
[How a strategy reaches the engine](#how-a-strategy-reaches-the-engine). If
`psycopg2-binary` is missing, persistence logs a warning and disables itself;
trading continues.

A strategy **refuses to start** if another process is already running it. Two
guards, deliberately independent: the registry check (is anything heartbeating
for this `strategy_id`?), which works across hosts but trusts the database, and
`engine/singleton.py`, which scans `/proc` for another Python process whose
config declares the same `MAGIC`. The second exists because a strategy with
broken persistence does not heartbeat at all and so looks long dead to the
first. Pass `--allow-duplicate` for a deliberate hand-off — or set
`"ALLOW_DUPLICATE": true` in the sidecar, which is how a shadow runs beside its
production twin. A database outage fails open and trades.

### Dependencies

```bash
pip install numpy pandas requests talib psycopg2-binary
```

> [!NOTE]
> TA-Lib requires its C library — see the [TA-Lib installation guide](https://github.com/ta-lib/ta-lib-python#installation). `psycopg2-binary` is only needed for trade persistence.

---

## 🏗️ Version 2 — the shared engine

Every strategy used to be a self-contained script of ~3,000 lines, about half
of which was the same infrastructure copied verbatim. Six of the S17 variants
differed from one another by as little as **nine lines out of three thousand**,
so one logical change meant six or seven near-identical edits — the uncapped-
trail fix touched 67, 67, 67, 70, 67, 67 and 45 lines across seven files to say
one thing once.

Version 2 splits that in two. `engine/` holds everything shared; a strategy
file declares what it is and how to scan.

### How a strategy reaches the engine

There is no package install and no `PYTHONPATH` set anywhere. Cronicle starts a
strategy **by path** (`python "/home/kaushal/strategy-engine/Live/Strategy 17/s17_m1m2_v1_forex_sell.py"`),
so Python puts only that script's own directory on `sys.path`. Every strategy
therefore bootstraps its own imports, in this exact shape:

```python
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))     # repo root
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "Live")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from s17_core import Spec, make_strategy    # sibling core, via _HERE
from engine import Strategy, Signal         # shared runtime, via _ROOT
```

Three entries, each earning its place: `_HERE` finds the sibling core module,
`_ROOT` finds `engine/`, and `_ROOT/Live` finds `trade_db`. **The number of
`dirname` calls is load-bearing** — a strategy two levels below `Live/` (the
Strategy 18.1 layout, `Live/Strategy 18.1/1H-5min/`) needs a different count
than one at `Live/Strategy 17/`. Getting it wrong does not fail at import; it
fails later, when something the engine needs is missing. This was one of the
three bugs found by running the scripts on the host rather than trusting them.

`engine/__init__.py` re-exports the whole public surface, so a declaration
imports from `engine` and never from `engine.runtime` or `engine.bridge`
directly:

```python
from engine import Strategy, Signal, ScanContext, Config, Position, Book, Telegram, log
```

### What each file in `engine/` does

| File | Responsibility | Reached from |
|---|---|---|
| `__init__.py` | The public surface: re-exports `Strategy`, `Signal`, `ScanContext`, `Config`, `Position`, `Book`, `Telegram`, `log` | every declaration |
| `runtime.py` | **The API and the two threads.** `Strategy` (registry, heartbeat, `@strategy.scan`, `.cli()`), `Signal`, `ScanContext` | the declaration; drives everything below |
| `config.py` | `Config` — env → strategy JSON → `Live/engine.json` → code default. Explicit typed reads (`cfg.get("MAGIC", int)`) so a missing key fails at startup by name | `runtime`, every module needing a setting |
| `candles.py` | `CandleCache` — rolling per-symbol cache, parallel prefetch of all timeframes, derived timeframes. Keyed by symbol *and* timeframe (the USOIL trail incident was a cross-symbol cache collision) | `ScanContext.candles()` |
| `bridge.py` | `Bridge` — the MT5 Bridge REST client: place, modify, close, positions, deal history | `execution`, `trailing`, `recovery` |
| `execution.py` | `Executor` — place → find the fill → correct the stop from the real fill price → build the ratio ladder → register the position | `runtime`, once per accepted `Signal` |
| `trailing.py` | `StopManager` plus the ladder maths (`target`, `reached_rung`, `anchor_for`, `improves`, `sane_extreme`) and the pre-close flatten | the stop-management thread |
| `recovery.py` | `recover()` — re-adopt OPEN positions after a restart, matching on the strategy's own magic | `runtime`, at startup |
| `positions.py` | `Position`, `Book` — the strategy's own view of what it holds | `execution`, `trailing`, `recovery` |
| `journal.py` | `Journal` — CSV merge-by-Event-ID and `_fired_events.json`, the ledger of setups already traded | `runtime`, `execution` |
| `db.py` | `TradeDB` — persistence as an object the runtime is handed, delegating to `Live/trade_db.py` by name | `runtime` |
| `indicators.py` | The indicator stack: MACD cycles, KAMA, EMA/KAMA, Bollinger, Heikin-Ashi, Fibonacci, volume windows. **Proven identical to both V1 originals** | `s17_core`, `s21_core`, the 18.1 scripts |
| `sizing.py` | Contract sizes, price digits, lot rounding, and `fixed_risk_sl` — `qty = RISK_PER_TRADE / SL distance` | `execution` |
| `sessions.py` | `is_market_closed`, `flatten_due` — market hours and the flatten window, NY wall clock, DST-proof | `trailing` |
| `notify.py` | `Telegram`, `NullTelegram`, `log`, `stamped`. `NullTelegram` is why a host with no token degrades instead of crashing | everywhere |
| `fmt.py` | `vkv`, `vblocks` — the "Additional info" blocks in journal rows and Telegram messages. **Output is part of the journal format**; changing it changes files people diff against backtests | `journal`, `s17_core`, `s21_core` |
| `singleton.py` | Host-level double-start guard: scans `/proc` for another Python process whose config declares the same `MAGIC`. Linux-only by design | `runtime`, at startup |

**Why `singleton.py` exists as well as the registry check.** The engine's first
guard asks the database whether another process is heartbeating for the same
`strategy_id`. That works across hosts, but it trusts the database — and a
strategy whose persistence is broken does not heartbeat at all. On 2026-08-17
an invalid root certificate did exactly that to three live strategies: they
kept trading, recorded nothing, and to the registry looked long dead. A
heartbeat-only guard would have started a second copy of each. So the second
guard asks the operating system, keyed on **magic** — what actually ends up
stamped on the orders, so two processes sharing one are two processes claiming
the same trades, whichever generation they belong to.

### What each file in `Live/` does

| Path | Responsibility |
|---|---|
| `engine.json` | Fleet-wide config — **gitignored**, deployed to the host, seeded from the `ENGINE_CONFIG_JSON` secret. Holds `BOT_TOKEN`, `CHAT_ID`, `TRADE_DB_URL`, `MT5_API_KEY` and the shared flatten/trail defaults |
| `engine.example.json` | Redacted template, committed. What a new host needs to fill in |
| `trade_db.py` | Persistence and registry: `init`, `record_signal`, `record_execution`, `record_trail`, `record_close`, `load_open_trades`, `live_elsewhere`, plus the 60 s heartbeat. Used by **both** generations — V2 through `engine/db.py`, V1 scripts directly |
| `repair_trail_state.py` | One-shot trail-state reconciliation, companion to [TRAIL_SL_FIX.md](TRAIL_SL_FIX.md) |
| `Strategy 17/s17_core.py` | The S17 logic (2,103 lines) shared by all six variants — cycle maps, 5m setup scan, M1/M2/M3 entry search, hard SL from the 5m window, exit formatting |
| `Strategy 17/s17_*.py` (+ `.json`) | The six V2 declarations. Each is ~35 lines: a `Spec(...)` and `strategy.cli()` |
| `Strategy 21/s21_core.py` | The S21 logic (1,354 lines) on the V2 engine — 15m wick setups, 3m EMA50 entries, Fibonacci hard SL, soft SL, Stage-2 exits |
| `Strategy 21/s21_btcusd.py` (+ `.json`) | The S21 V2 declaration |
| `Strategy 18.1/<tf>/Bridge-*.py` (+ `.json`) | Six **self-contained V1-style** scripts, run under V2 machinery but not migrated onto the engine — see below |
| `Strategy */Bridge-*.py` | The seven V1 originals, kept so parity tests can drive them. **They point at live accounts** — see the `MANAGED` flag below for what stops the sync script starting them |

### Writing a new strategy

```python
from engine import Strategy, Signal, indicators as ind

strategy = Strategy(
    id="S22-MYIDEA-XAUUSD",
    label="S22 My Idea · XAUUSD",
    symbols=["XAUUSD"],
    timeframes={"M5": 2500, "M1": 6000},
    log_dir="./bridge/Strategy 22 Logs",
)

@strategy.scan
def scan(ctx):
    df5 = ctx.candles("M5")               # already fetched, cached, in parallel
    ...
    return [Signal(symbol=ctx.symbol, side="buy",
                   event_id=ctx.event_id("buy", ctx.symbol, fcc_ts),
                   entry_price=px, entry_dt=dt, hard_sl=sl, qty=q, row=row)]

if __name__ == "__main__":
    strategy.cli()
```

Plus a config sidecar holding only what genuinely differs — its magic, its
account, its risk:

```json
{ "MAGIC": 22001,
  "MT5_BRIDGE_URL": "https://exness-bridge-mt5.pickleballify.com/<login>/<type>",
  "RISK_PER_TRADE": 100 }
```

The engine does the rest: registry and heartbeat, candle fetching, order
placement, post-entry stop correction, indefinite trailing, market-close
flatten, crash recovery, CSV journalling, Telegram, and both threads.

### Config layering

Resolution order, most specific first:

1. **environment variable** — deploy-time override, never committed
2. **the strategy's own JSON** — `Live/<family>/<script>.json`
3. **`Live/engine.json`** — fleet-wide, gitignored
4. **the code default**

The strategy layer is checked before the shared one, so a pre-Version-2 config
that still carries `BOT_TOKEN` or `TRADE_DB_URL` keeps working — it simply
wins. This is what took the live database password out of seven committed
files.

**Every read of a shared key must tolerate its absence.** `Live/engine.json` is
gitignored, so it exists on the host and on a developer's machine but **not in
CI** — a bare `_config["BOT_TOKEN"]` passes locally and fails on every CI probe,
which is exactly how the first six 18.1 scripts shipped in a state where they
could never have started. `test/test_shared_config_optional.py` reads the
source and fails any strategy that subscripts a shared key instead of using
`.get()`.

### Running Version 1 scripts under the same machinery

Strategy 18.1 is **not** migrated onto the engine. Its six scripts are still
self-contained V1-style files that call `trade_db.init(...)` themselves; they
are only scheduled, deployed and shadowed by the same tooling. `sync_cronicle`
recognises both shapes:

| | declared (V2) | self-contained (V1-style) |
|---|---|---|
| detected by | `make_strategy(` + `strategy.cli()` | `trade_db.init(` |
| strategy id from | `id="..."` in the file or its core | the `trade_db.init("...")` argument |
| managed | always | **only with `"MANAGED": true`** |

That opt-in flag is load-bearing. The repo also holds the seven **Version 1
originals**, kept for parity tests, and they point at *live* accounts. Without
`MANAGED`, discovery found 20 strategies instead of 13 and would have started
seven duplicates against real money.

### Shadow deployment

Version 2 runs **beside** Version 1 so the two can be compared. Sharing a
`strategy_id` would not have made them look alike — it would have corrupted the
live records. `trades` is `UNIQUE (strategy_id, event_id)` and both generations
derive the *same* deterministic `event_id` for the same setup (that determinism
is what lets backtest parity join a replay to live rows). With one id the
shadow's `record_signal` is silently dropped by `ON CONFLICT DO NOTHING`, and
its `record_execution` then **updates the Version 1 row**, writing a ticket,
entry price and stop from a different account over a live trade's record.

A shadow is separated on every axis that matters, all from config:

| Key | Effect |
|---|---|
| `"STRATEGY_ID_SUFFIX": "-V2"` | its own `strategy_id`, so its trades and registry row are its own |
| `MAGIC` in the `+100` shadow block | its own magic — orders attributable in raw history, and neither registration refused (`strategy_registry.magic` is `UNIQUE`) |
| account `463858748` | the "CI/CD Account Test" demo account |
| `log_dir` suffixed `-V2` | its own `_fired_events.json` — two instances sharing one would each skip whatever the other claimed first, and each would trade roughly half the signals |
| `"ALLOW_DUPLICATE": true` | permits the V1 twin to keep running on the same host |

### Auto-registration with Cronicle

`scripts/sync_cronicle.py` runs after each deploy and reconciles Cronicle's
schedule with what is in the repo:

* **new strategy** → event created and started. It has never run, so it holds
  no positions and starting it disturbs nothing.
* **changed strategy** → event definition updated, flagged **restart pending**
  in the dashboard. It is *not* restarted: it may be holding open positions,
  and whoever merges is not necessarily whoever is watching the account.
* **existing event, not running** → started.
* **orphaned event** → reported, never deleted.

Events are matched by a `[strategy:<id>]` marker in the event notes, so the
dashboard no longer has to guess the link from a filename.

Placement and ownership are set explicitly, because Cronicle category ids are
opaque and differ per install:

```bash
CRONICLE_CATEGORY_TITLE='Sumit Jobs'   # resolved to an id at run time
CRONICLE_USERNAME=sumit                # who owns the event
STRATEGY_DIR=/home/kaushal/strategy-engine
```

Generated events carry **`detached: 1`**. Without it a Cronicle daemon restart
aborts the job — all 13 shadows died at once with `Aborted Job: Server shut
down unexpectedly` while 35 hand-made events, every one of them detached,
survived.

Two subtleties worth knowing before trusting a sync:

* **A running job snapshots its event definition at launch.** Category, owner
  and `detached` change on the *event* immediately but on the *job* only at the
  next restart. Verifying the event and concluding the job is fixed is the
  trap here; it caught us three times running.
* **`version_of()` fingerprints everything that decides behaviour** — the entry
  file, the sibling `*_core.py`, the config sidecar, and every `engine/*.py` —
  not just the entry file. A V2 declaration is a short stub; hashing it alone
  made a shared-engine fix invisible. S21 spent its whole first deployment
  raising `TypeError` on every scan, and the fix would have been reported as
  "unchanged".

Changes are detected across every managed field — `title`, `category`,
`target`, `plugin`, `enabled`, `timing`, `catch_up`, `max_children`, `timeout`,
`detached`, `retries`, `username` — with bool/0-1 normalisation, because
Cronicle round-trips some as booleans and some as integers. An earlier check
compared only script, title, marker and version, which is why the first
`detached` fix shipped but never reached the host.

Two things stop any of this from double-starting a strategy: the event's
`max_children: 1`, and the engine's startup check, which refuses to run when
the registry shows a live heartbeat from another process — backed by the
`/proc` scan in `engine/singleton.py` for when persistence itself is broken.

```bash
python scripts/sync_cronicle.py --dry-run    # report, touch nothing
python scripts/sync_cronicle.py              # reconcile
```

### Behaviour resolved during the migration

The two Version 1 engines disagreed in four places. Each is resolved towards
whichever was safer, and the loser is named here so the change is not a
surprise:

| | Version 1 S17 | Version 1 S21 | Version 2 |
|---|---|---|---|
| fill lookup fails | falls back to signal price, still corrects | **leaves the placeholder stop on a live position** | always corrects |
| stop modify fails | retries x3, alerts | fire-and-forget | retries x3, alerts |
| order rejected | records the retcode | **row stays SIGNAL forever** | records the retcode |
| ratio ladder measured from | the corrected stop | **the raw signal stop** | the corrected stop |

The last one changes S21's rung prices relative to Version 1. That is the
intended fix: 1R now means one unit of the money actually at risk.

### Indefinite trailing

Rung prices are computed from entry and R rather than looked up in a table that
stopped at 1:10, so there is no last rung. A runner keeps ratcheting until the
stop is taken out. Every move is recorded as a `TRAIL_MOVE` event.

### The migration was verified, not asserted

| Test | What it pins |
|---|---|
| `test_migration_parity.py` | Drives each V1 script's **own** functions and the V2 pipeline over identical candles, comparing every column both produce — 40+ per setup |
| `test_indicators.py` | Loads the indicator functions back out of the original files and pins them against `engine/indicators.py` |
| `test_frame_prep.py` | Drives each strategy's own `prepare_*_data()` — the **call sites**, not the engine's internal usage |
| `test_engine.py` | The engine end to end against a faked broker |
| `test_singleton.py` | The `/proc` double-start guard |
| `test_shared_config_optional.py` | AST check: no strategy subscripts a `Live/engine.json` key |
| `test_sync_cronicle.py` | Discovery, `MANAGED` gating, fingerprinting, field comparison |
| `test_live_execution.py` | The CI gate |

`test_frame_prep.py` exists because of a specific hole. `engine/indicators.py`
lost the `length` argument on `calc_sma_kama`, which killed S21 on every scan —
and `test_indicators.py` could not see it, because it calls both
implementations *the engine's way*. The defect was at the strategy's call site.
The rule that came out of it: **a new test must be shown to fail against the
old code before it is trusted.**

`scripts/gen_s17_core.py` and `scripts/gen_s21_core.py` regenerate the shared
logic from the Version 1 scripts, so "byte-identical" stays checkable rather
than being a claim in a commit message.

---

## 🧪 Testing

See **[TESTING.md](TESTING.md)** for the full local-testing guide (MT5 for Mac via bundled Wine + embedded Python). A dedicated **demo** account is available for paper-money test trades:

| Server | Login | Password |
|--------|-------|----------|
| `Exness-MT5Trial14` | `415979703` | `1Techmero@100` |

**MT5 Bridge API Test Credentials:**
- **Bridge URL**: `https://exness-bridge-mt5.pickleballify.com/415979703/demo`
- **API Key**: `ak_Toblo1aIMZo0Xv6BJl4mjw_JasfLSsJ4-NAoQ2H-j9o`

### HFT stress-test driver

`Test Strategies/HFT-S17-M3-M2-V1-BTCUSDT-Sell-Live.py` replaces signal logic with a deterministic 1-minute SELL loop to stress-test the bridge, position tracking, and exit plumbing. It mirrors the live strategy's trailing stops, Telegram messaging, and CSV logging, and runs under magic `17999` so test trades are distinguishable from live ones (`17001`).

Safety switches (environment variables):

```bash
export HALT_HFT=1            # stop firing new trades
export HFT_DRY_RUN=1         # log operations without executing
export MAX_TRADES_PER_HOUR=60  # sliding-window trade cap (default 60)

python "Test Strategies/HFT-S17-M3-M2-V1-BTCUSDT-Sell-Live.py"
```

### Local test scripts (`scripts/`)

- `place_local_trade.py` — place a trade on the locally installed MT5 terminal (Wine).
- `place_test_trade.py` — place a trade through the hosted bridge API.
- `hft_test_strategy.py` — scripted HFT loop for end-to-end verification.

---

## 💬 Notifications & Logs

- **Telegram (strategies):** entries, exits, errors, and trailing-stop updates broadcast to the configured channel per strategy config.
- **Telegram (dashboard ops):** optional account up/down, trade-event, and drawdown alerts — configured on `/analytics`, off by default.
- **CSV logs:** real-time state and trade outcomes appended under `./output/`.
- **Remote logs:** bridge and per-worker logs are viewable in the dashboard (account page → *Bridge / worker logs*) or via `GET /logs` on the bridge.

---

## 🔗 Related Repositories

| Repo | Contents |
|---|---|
| [strategy-engine](https://github.com/sumittechmero/strategy-engine) (this repo) | Strategies, configs, trade persistence, test tooling |
| [mt5-orchestrator](https://github.com/sumittechmero/mt5-orchestrator) | MT5 bridge (Windows VPS), per-account workers, trade sync, trade dashboard |
