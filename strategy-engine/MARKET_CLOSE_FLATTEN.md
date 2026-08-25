# Market-close flatten — plan & implementation

> Ships on branch [`fix/spurious-trail-hits`](https://github.com/sumittechmero/strategy-engine/tree/fix/spurious-trail-hits) (PR [#24](https://github.com/sumittechmero/strategy-engine/pull/24), unmerged) together with the trail-SL fixes — same motivation, same deploy.

## Why

On 2026-07-12 four USOIL positions holding breakeven stops through the weekend **gap-filled at the
Sunday reopen for −10.79 each (−43.16 total)** — see `TRAIL_SL_FIX.md`. Independent of that bug,
any position held across a session close is exposed to the reopening gap with **no working stop**
while the market is closed. This provision closes a strategy's own positions shortly *before* the
relevant symbol's market closes.

## Design

- **Runs inside each strategy** (the always-on Linux process that owns the positions) — not the
  dashboard, which may be offline at close time.
- **Session times in New York wall clock** (`zoneinfo("America/New_York")`), because oil/gold/forex
  sessions key off 17:00 ET; this makes the logic DST-proof (17:00 ET = 21:00 UTC in summer,
  22:00 UTC in winter — exactly the shift that makes UTC hardcoding wrong half the year).

| Symbol | Weekend close (Fri 17:00 ET) | Daily break (17:00–18:00 ET Mon–Thu) |
|---|---|---|
| USOIL | flatten (default **on**) | flatten (default **off**) |
| XAUUSD | flatten (default **on**) | flatten (default **off**) |
| USDJPY / EURUSD | flatten (default **on**) | n/a (24/5) |
| BTCUSD | never (24/7) | never |

- **Lead window**: positions are closed inside the final `FLATTEN_LEAD_MIN` (default 10) minutes
  before the close, i.e. 16:50–17:00 ET. A failed close logs and retries on the next loop pass
  within the window.
- **Daily-break flatten defaults OFF** deliberately: closing oil/gold every day at 17:00 ET changes
  strategy behavior and pays the spread daily; the 1-hour break gap risk is far smaller than the
  weekend's. Enable per strategy via config when wanted.
- Closed positions are recorded to Postgres (`record_close`, reason `flatten_weekend_close` /
  `flatten_daily_break`, floating P/L captured), announced on Telegram
  (`🛑 MARKET-CLOSE FLATTEN`), and skipped by the same pass's trailing logic.
- Only positions **owned by the strategy** are touched (ticket in `_ticket_map`, matching symbol
  and magic) — manual trades and other strategies' positions are never closed.

## Configuration (per strategy `.json`)

```json
{
    "FLATTEN_BEFORE_WEEKEND":     true,
    "FLATTEN_BEFORE_DAILY_BREAK": false,
    "FLATTEN_LEAD_MIN":           10
}
```

All keys optional; the values above are the code defaults. All six Live configs carry them
explicitly for discoverability.

## Implementation

In every `Live/Bridge-*.py`:

- `_MC_SESSIONS` — per-symbol session traits (weekend / daily-break / 24-7);
- `market_close_flatten_due(symbol, _now=None)` → `(due, reason)`; `_now` injectable for tests;
- `mt5_bridge_close_ticket(ticket)` — bridge `POST /close`, success = retcode 10009;
- `flatten_for_market_close(positions, symbol)` — closes owned positions in the window, returns
  the closed tickets, which the caller removes from the list before the trailing pass.

## Verified

- `py_compile` clean, all six scripts.
- Window logic exercised with injected timestamps: Fri 16:55 ET → weekend flatten (oil & EURUSD);
  Fri 16:49 / 17:00 → outside window; Wed 16:55 → off by default, `daily_break` when enabled
  (oil only, never EURUSD); BTCUSD and Sunday → never. 10/10 cases pass.

## Known limits / future work

- New entries are **not blocked** during the lead window — a signal firing at 16:56 ET Friday would
  open and be flattened moments later (costs one spread). Blocking entries in the window is a
  possible follow-up.
- Exness session schedules are assumed to follow the standard NY-anchored hours observed on the
  live feed (oil's last candle 20:58 UTC in July = 16:58 ED T). If a broker holiday closes a market
  early, the flatten won't anticipate it.
