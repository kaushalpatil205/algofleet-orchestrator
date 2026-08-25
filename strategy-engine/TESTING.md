# Testing

## MT5 demo test account

A dedicated **demo** account for end-to-end testing (paper money only — no real
funds; safe to place and close trades on).

| Field | Value |
|-------|-------|
| Server | `Exness-MT5Trial14` |
| Login | `415979703` |
| Password | `1Techmero@100` |

> ⚠️ Demo/testing account only. It shares the `Exness-MT5Trial14` server with the
> existing demo terminals, so it can trade the same symbols (BTCUSD, XAUUSD, …).

### Running the terminal locally (macOS)

The official **MetaTrader 5 for Mac** is installed (it ships its own bundled
Wine):

```
/Users/apple/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/terminal64.exe
```

Launch it, log into the account above, then **Tools → Options → Expert Advisors →
"Allow Algo Trading"** (and toolbar **Algo Trading** button) so orders can be
placed. Wine is also available standalone at `/opt/homebrew/bin/wine`.

### Placing programmatic trades

**Locally, against the Mac terminal (no bridge / VPS needed) — recommended.**
The MT5-for-Mac app ships an embedded Windows Python (`C:\PythonEmbed`) with the
`MetaTrader5` package in the same Wine prefix as the terminal, so orders can be
placed straight from macOS. `scripts/place_local_trade.py` logs the terminal
into the account, opens → trails the SL → closes a tiny position, and records
the whole lifecycle through `Live/trade_db.py` so it shows up in the visualizer:

```bash
# any python with psycopg2 + requests works; the trade-dashboard venv from the
# mt5-orchestrator repo has both
PY=/path/to/mt5-orchestrator/trade-dashboard/.venv/bin/python
$PY scripts/place_local_trade.py \
    --login 415979703 --password '1Techmero@100' --server Exness-MT5Trial14 \
    --symbol BTCUSD --side buy --strategy-id LOCAL-TEST
```

Just make sure MetaTrader 5 is running with **Algo Trading enabled** (toolbar
button). Add `--no-close` to leave the position open. It drives MT5 through the
bundled Wine via `scripts/_mt5_exec.py`.

**Via the hosted bridge (VPS).** Orders can also go through the MT5 **bridge**
(repo `mt5-orchestrator`), which fronts each terminal with a per-account worker:

- `POST /{login}/{type}/trade` / `/modify` / `/close`
- `GET  /{login}/{type}/positions`, `/history`, `/market/candles/{symbol}`

The hosted bridge is `https://exness-bridge-mt5.pickleballify.com/{login}/demo`;
every call needs an `X-Api-Key` header. For the test account to be reachable
there it must first be **registered on the bridge** and issued an API key
(orchestrator dashboard → add account). `scripts/place_test_trade.py` runs the
same open→trail→close lifecycle against a bridge URL + API key.

### HFT test strategy (fast live activity)

Real strategies signal slowly, so `scripts/hft_test_strategy.py` fires a trade
every N seconds, trails its stop, and closes it — streaming activity into the
dashboard to exercise the persistence / trailing / aggregation logic:

```bash
# sim (default): DB-only, live-seeded prices, no real orders
$PY scripts/hft_test_strategy.py --interval 10 --duration 120

# live: real demo orders on the local terminal
$PY scripts/hft_test_strategy.py --live \
    --login 415979703 --password '1Techmero@100' --server Exness-MT5Trial14
```

Bound it with `--max-trades` / `--duration`, or stop with Ctrl-C (open trades
are closed out first). Trades land under strategy id `HFT-TEST`.

## Visualizer / dashboard

The dashboard lives in the **mt5-orchestrator** repo at `trade-dashboard/`
(moved there from this repo's `visualizer/`):

```bash
cd /path/to/mt5-orchestrator/trade-dashboard
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export TRADE_DB_URL="postgresql://…"                     # or Live/trade_db_url.local
export LIVE_CONFIG_DIR="/path/to/strategy-engine/Live"   # this repo's Live/ folder
./.venv/bin/uvicorn server:app --port 8600
```

Open http://localhost:8600 — `/` dashboard, `/charts`, `/accounts`, `/trades`,
`/backtests`. See `trade-dashboard/README.md` there for the full page/feature
list and the backtest upload formats.
