# Fix plan: spurious trail-SL ladders on USOIL (cross-symbol candle-cache contamination)

> **Status: implemented on branch [`fix/spurious-trail-hits`](https://github.com/sumittechmero/strategy-engine/tree/fix/spurious-trail-hits), PR [#24](https://github.com/sumittechmero/strategy-engine/pull/24) — NOT merged, parked for review.**
> Two incidents have now occurred (2026-07-09 and 2026-07-10). The second one caused a **realized
> loss of −43.16** across four trades. Until this branch is merged **and deployed to the strategy
> server**, every open USOIL position of the multi-symbol forex strategies remains exposed to the
> same failure.

## Incidents

**#1 — 2026-07-09 20:59 UTC.** Two open USOIL sells (`S17-M1M2-V1-FOREX-SELL`) recorded the whole
1:2→1:7 trail ladder in ~20 s; all six modifies rejected by MT5. No broker-side damage (stop stayed
at hard SL).

**#2 — 2026-07-10 16:47–16:49 and 20:26 UTC.** Five USOIL positions laddered the same way. This
time each position's **1:2 move (SL → breakeven) was VALID and executed on the broker**, because a
breakeven stop sits above market for an underwater/flat sell. Four of those positions gap-filled at
the Sunday reopen (`[sl 71.690]` filled at 73.181 etc.) — **−10.79 each, −43.16 total** — trades
that should still have been running on their original hard stops. The fifth (`#3451945022`) stayed
open with every trail ratio burned.

## Root cause (revised after incident #2)

**The rolling candle cache is keyed by timeframe only** — `_candle_cache[timeframe]` — while the
forex scripts loop `INSTRUMENTS = ["USDJPY", "EURUSD", "USOIL"]` through the *same module-level
cache*. Every symbol's fetch is merged into one dataframe, deduplicated by `datetime` alone. The
trailing pass then reads `df1_raw["low"].iloc[-1]` — **the newest row of a mixed-symbol frame**.

Whenever USOIL's fetch fails, lags a minute, or the oil market stops printing candles while forex
keeps trading (its daily 21:00 UTC break — which is what made incident #1 look like a
"market-close" problem), the newest row belongs to **EURUSD at ≈ 1.14**. For a USOIL sell with
targets at 51.69 / 41.69 / … a "low" of 1.14 satisfies *every* ratio simultaneously → the whole
ladder fires:

- ratio 1:2 (SL = entry) is a *placeable* stop → **executes**, silently moving the real stop to
  breakeven on fictional data;
- ratios 1:3+ request stops below market → MT5 rejects them (*Invalid stops* — the only safety net
  that held).

Evidence: the bridge's stored USOIL history for both windows is pristine (min low 70.686 over
3000 M1 candles — nothing within $17 of the nearest target); contamination is only ever
EURUSD→USOIL because a 71.x low sits *above* USDJPY/EURUSD targets; single-symbol scripts
(BTCUSD/XAUUSD) never fired. Definitive confirmation available in the strategy's own log:

```bash
grep "\[TRAIL\]" <job log> | grep -E "current_px=(0|1)\."   # EURUSD-priced USOIL passes
```

Secondary bookkeeping bugs (also fixed by this branch): ratios were marked "hit" before the
modify succeeded (burning them forever, surviving restarts via crash recovery); `trade_db`
persisted `current_sl`/`trail_hits` even for rejected moves (dashboard showed stops the broker
never accepted); Telegram announced "SL TRAILED" for moves that never happened.

## Fixes on the branch (all six `Live/Bridge-*.py` + `trade_db.py`)

1. **Cache keyed by `(symbol, timeframe)`** — the root fix. Each symbol gets its own rolling frame;
   cross-symbol contamination becomes impossible. (Harmless in single-symbol scripts, applied
   everywhere for uniformity.)
2. **Per-position price-sanity guard** in `trail_conservative_positions`: if `current_px` deviates
   more than ±50 % from the position's *own open price*, the frame cannot belong to this symbol —
   skip the position and log `[TRAIL] px sanity: …`. (Defense-in-depth: catches any future
   contamination path even when the frame is internally consistent.)
3. **Degenerate-candle guard**: no/zero close → skip the pass; a low/high deviating > ±20 % from
   its own candle's close is discarded (logged), falling back to the close.
4. **Failed modifies retry instead of burning the level**: on rejection the ratio is un-marked,
   `cur_sl` keeps broker truth, no Telegram post; a later candle retries naturally.
5. **`trade_db.record_trail` truth-keeping**: `executed=False` records the audit event only;
   `current_sl`/`trail_hits` advance exclusively on executed moves.
6. **Market-close flatten** (companion feature, same branch): strategies close their own
   positions in the final minutes before a symbol's session close — weekend always (default),
   oil/gold daily break optional — so no position ever sits through a reopening gap again.
   Full design: [`MARKET_CLOSE_FLATTEN.md`](MARKET_CLOSE_FLATTEN.md).
7. **`Live/repair_trail_state.py`** — idempotent reconciler: rebuilds `trail_hits`/`current_sl` for
   every OPEN trade of every strategy/symbol from executed `TRAIL_MOVE` events only.

## Data repairs executed so far

| When | What |
|---|---|
| 2026-07-10 | Incident-#1 trades (7438, 7441) reset to hard SL, ratios cleared |
| 2026-07-13 | Incident-#2 open trade **7474** (`#3451945022`): `trail_hits [2…7] → []`, `current_sl 21.158 → 81.158` (= hard SL, broker truth); plus ordering normalization on 7451 |

Closed trades keep their historical rows as-is (the `executed:false` events remain the audit trail).
**Note:** a *running* strategy process keeps its burned in-memory trail set — the DB repair takes
effect at the next restart, when crash recovery reloads clean state.

## Deployment (after merge — currently NOT merged)

1. Merge PR #24.
2. Copy to the strategy server (`/home/kaushal/S17`): the six strategy scripts (or re-apply the
   same edits to the server's per-symbol variants), `trade_db.py`, `repair_trail_state.py`.
   **If the server runs per-symbol single-instrument copies, the cache-key fix is prophylactic
   there — but guards 2–5 are still essential.**
3. Run `python3 repair_trail_state.py` once more (catches anything polluted since).
4. Restart the Cronicle jobs one at a time; watch for `[trade_db] connected` + `registered`.
5. Confirm on the dashboard: `/trades` trail histories show `executed:true` moves only (or visibly
   retried failures), and no ladder ever fires without price actually at the target.

## Verification done on the branch

- `py_compile` clean across all six scripts + `trade_db.py` + repair script.
- Live Neon round-trip of the `record_trail` gate (temp rows, cleaned up).
- Repair script: dry-run → apply → idempotent re-run, against production data.
- Root-cause evidence: DB event timeline (5 laddered positions), bridge candle history (clean),
  MT5 deal history (breakeven stop gap-filled at Sunday reopen), code inspection of
  `_candle_cache` keying and the `INSTRUMENTS` loop.

## Out of scope / unchanged

- Dashboard: no code change needed — it shows whatever Postgres holds, and the trail-history
  expansion rendering `executed:false` in red is exactly what surfaced both incidents.
- MT5-side behavior untouched — invalid-stop rejection remains the last line of defense.
