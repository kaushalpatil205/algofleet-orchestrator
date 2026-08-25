# Plan: continuous delivery of strategies into Cronicle

**Status:** proposal, not built. Nothing here is implemented yet.

## Context

`deploy-strategies.yml` ships code and stops. It scp's the repo to
`/home/kaushal/strategy-engine`, installs dependencies, and prints
*"Restart affected strategies from /jobs when ready."* Getting new code actually
running is a manual step in Cronicle.

That gap is deliberate — the PM2 restart step was removed because
`pm2 startOrRestart` starts *stopped* apps, which would have launched a second
copy of every strategy alongside the Cronicle ones. But the cost is that a
merged fix sits inert until someone remembers to restart, and adding a strategy
means hand-building a Cronicle event.

The goal is to close that gap **without** re-creating the hazard the removal
avoided: a restart is not free. A strategy may be holding open positions, and
whoever merges is not necessarily whoever is watching the account.

## Goals

- A new `Live/**/Bridge-*.py` + sidecar produces a Cronicle event automatically.
- A modified strategy gets restarted onto the new code, when that is safe.
- A strategy holding open positions is never restarted silently.
- Every action is idempotent — re-running the sync changes nothing.
- Nothing runs twice. One strategy, one process, one magic number.

## Non-goals

- Deciding *whether* a new strategy should trade. Creation is automatic;
  going live stays a human decision.
- Replacing Cronicle's UI. `/jobs` remains where people act.
- Touching the bridge, which correctly stays on PM2.

## What already exists

| Piece | Where | Note |
|---|---|---|
| Code deploy | `.github/workflows/deploy-strategies.yml` | scp + pip, no restart |
| Cronicle client | `mt5-orchestrator/trade-dashboard/cronicle_client.py` | `get_schedule`, `get_active_jobs`, `run_event`, `abort_job`, `get_categories`, `health` |
| Jobs UI | dashboard `/jobs`, `/api/jobs`, `/api/jobs/{id}/run`, `/api/jobs/{id}/abort` | |
| Live-execution gate | `test/test_live_execution.py` | blocks a strategy that cannot trade or survive a restart |
| Self-registration | `Live/trade_db.py` `_register()` | writes magic, account, host, pid and a sha1 script fingerprint to `strategy_registry`, heartbeats every 60s |

**The client cannot create or modify events yet.** Cronicle's API supports
`create_event`, `update_event`, `delete_event` and `get_event`; only the read
and run calls are wrapped today. Those four wrappers are the first thing to add.

## Design

### Where it runs

Inside the **existing SSH step** of `deploy-strategies.yml`, calling a script
shipped in this repo.

Cronicle listens on `127.0.0.1:3012`, so a GitHub runner cannot reach it and
should not be able to. Running the sync on the host keeps the API bound to
localhost, reuses the deploy key already in `secrets.DEPLOY_SSH_KEY`, and means
the code being synced is the code already on disk — no version skew between
what CI thinks it deployed and what Cronicle is pointed at.

```
scp  ──▶  /home/kaushal/strategy-engine
ssh  ──▶  venv/bin/pip install -r requirements.txt
     ──▶  venv/bin/python scripts/sync_cronicle.py --changed "<file list>"
                     │
                     └──▶ http://127.0.0.1:3012/api/app/...
```

### Identity

A strategy's Cronicle event must be findable again on the next deploy, without
a database.

Key on the **repo-relative script path**, stored in the event's `notes` field as
a machine-readable line:

```
managed-by: sync_cronicle
strategy-path: Live/Strategy 17/Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live.py
```

Path, not title: titles are edited by humans, and a rename should read as
*delete + create* rather than silently orphaning an event. Events without that
marker are never touched — anything hand-made in Cronicle stays hand-made.

All managed events live in a dedicated Cronicle **category**, so a single
`get_schedule` call plus a category filter enumerates them.

### Change detection

CI computes the changed set and passes it through:

```bash
git diff --name-only ${{ github.event.before }} ${{ github.sha }} -- Live/
```

Mapping a changed file to affected strategies:

| Changed | Affects |
|---|---|
| `Live/<...>/Bridge-X.py` | strategy X |
| `Live/<...>/Bridge-X.json` | strategy X (config only — still needs a restart to take effect) |
| `Live/trade_db.py` | **every** strategy — it is imported by all of them |
| anything else under `Live/` | nothing, but log it |

That third row is the one that gets forgotten. A `trade_db.py` fix reaches no
running process until every strategy restarts.

### The decision table

This is the heart of it. What the sync does depends on the change *and* on
whether money is currently at risk.

| Situation | Action |
|---|---|
| New strategy, no event exists | **Create the event, disabled.** Notify. A human enables it. |
| Modified strategy, event exists, **no open positions** | Update the event, then restart it. |
| Modified strategy, event exists, **open positions** | **Do not restart.** Update the event definition so the next start picks up new code, and raise a notification naming the tickets. |
| Event exists, strategy file deleted | **Disable, do not delete.** Notify. Deleting loses run history. |
| Event exists, strategy unchanged | Nothing. |
| Strategy fails the live-execution gate | Nothing — the merge should not have happened; fail loudly. |

Creating a new strategy **disabled** is the deliberate choice. Everything else
here is recoverable; a strategy that starts trading an account nobody chose is
not. It also matches how S18.1 arrived — with a paper-trading flag, an unset
account, and `RISK_PER_TRADE` 10x its siblings.

### Deciding "open positions"

Two sources, and they answer different questions:

- **`strategy_registry` + `trades`** in CockroachDB — rows with `status='OPEN'`
  for that `strategy_id`. Cheap, no bridge dependency.
- **The bridge `/positions`** filtered by `magic` — the broker's truth.

Use the bridge as the authority and the DB as the fallback, because the DB can
hold stale `OPEN` rows when a strategy died without reconciling — exactly the
S18.1 failure. Treating a stale row as "positions open" is the safe direction:
it defers a restart rather than performing one.

### Cronicle event shape

```jsonc
{
  "title":    "S17 · BTCUSDT Sell (M3-M2-V1)",
  "category":  "<managed strategies category id>",
  "plugin":    "shellplug",
  "target":    "<server group>",
  "enabled":   false,          // true only after a human enables it
  "catch_up":  false,          // never backfill a missed trading window
  "max_children": 1,           // one process per strategy, always
  "timeout":   0,              // long-running; do not kill it
  "params": {
    "script": "cd '/home/kaushal/strategy-engine/Live/Strategy 17' && exec ../../venv/bin/python3 'Bridge-S17-M3-M2-V1-BTCUSDT-Sell-Live.py'"
  },
  "notes": "managed-by: sync_cronicle\nstrategy-path: Live/Strategy 17/Bridge-...py"
}
```

`max_children: 1` and `catch_up: false` are the two that matter. Together they
are what stops a second copy of a strategy ever existing — the same guarantee
the PM2 removal was protecting, expressed as configuration rather than as a
missing CI step.

`cd` into the script's directory is required: the strategies resolve their
config sidecar and their `./bridge/...` log directory relative to cwd.

> **Confirm against the live instance before building.** The existing events
> were made by hand, and this plan has not been checked against them — the
> plugin id, target group, category id, and how a long-running job is currently
> kept alive all need reading off a real event first via `get_event`. Adopting
> the existing convention matters more than the shape sketched above.

### Safety gates

Ordered, each blocking:

1. **The live-execution gate passes.** `test/test_live_execution.py` already
   runs on PRs to `main`; the sync refuses to act on a strategy that fails it.
2. **Magic uniqueness.** Refuse to create an event whose config `MAGIC`
   collides with another strategy's. A duplicate magic silently merges two
   strategies' trades in attribution and trailing.
3. **Config sidecar exists and parses**, and names a bridge URL and account.
4. **No duplicate event** for the same `strategy-path`.
5. **`--dry-run` by default in the workflow for the first weeks**, printing the
   plan without applying it, so the decision table can be watched before it is
   trusted.

### Verifying a restart actually took

`trade_db._register()` writes a sha1[:10] fingerprint of the running script to
`strategy_registry`. After a restart, poll that row until the fingerprint
changes to the deployed file's hash, with a timeout.

This is the difference between "we asked Cronicle to restart it" and "the new
code is running", and it costs one query.

## Components to build

| # | Component | Notes |
|---|---|---|
| 1 | `create_event` / `update_event` / `delete_event` / `get_event` in `cronicle_client.py` | orchestrator repo; the only missing API surface |
| 2 | `scripts/sync_cronicle.py` | this repo; the decision table, `--dry-run`, `--changed` |
| 3 | Discovery: `Live/**/Bridge-*.py` with a sidecar | reuse `test/test_live_execution.py::strategies()` — same definition of "a strategy", so the gate and the deployer cannot disagree |
| 4 | Open-position probe | bridge `/positions` by magic, DB fallback |
| 5 | Fingerprint wait | poll `strategy_registry` after a restart |
| 6 | Workflow step | one line in the existing SSH block |
| 7 | Notifications | reuse the dashboard's existing Telegram ops alerts |

## Rollout

**Phase 1 — visibility.** Build 1–3. Run with `--dry-run` on every deploy.
It prints what it *would* do. Change nothing for two weeks and read the output.

**Phase 2 — create only.** Allow creating disabled events for new strategies.
The riskiest action stays behind a human toggle, and the common case (a new
strategy appearing) stops being manual.

**Phase 3 — restart when safe.** Enable restarts for modified strategies with
no open positions. Keep the open-positions case as a notification.

**Phase 4 — assisted restart.** Offer a one-click "restart when flat" from
`/jobs`: watch until the strategy has no open positions, then restart. This is
where the awkward case finally gets automated, and only once the machinery
underneath has a track record.

## Verification

- **Unit:** decision table as a pure function over (change kind, event exists,
  positions open) → action. Every row asserted, no Cronicle needed.
- **Integration against a scratch Cronicle category:** create, update, disable,
  re-run sync and assert nothing changes the second time.
- **Idempotency:** run the sync twice on an unchanged repo; the second run must
  make zero write calls.
- **Safety:** with a fake open position, assert no restart is issued.
- **End to end on one strategy:** change a comment in an S17 file, merge, watch
  the fingerprint in `strategy_registry` change and the trade log resume.

## Risks

| Risk | Mitigation |
|---|---|
| A restart orphans open positions mid-trail | Never restart with positions open (decision table); `recover_open_trades()` resumes trailing on start, and PR #38 makes that survivable for S18.1 |
| Two processes for one strategy | `max_children: 1`, plus the duplicate-event check |
| A bad sync disables working strategies | `--dry-run` first, never delete, disable is reversible |
| Cronicle unreachable during deploy | Fail the step loudly; code is already on disk, so nothing is half-applied |
| Magic collision from a copy-pasted config | Gate 2 refuses |
| Config-only change looks harmless | Sidecar changes are treated exactly like code changes — they need a restart too |

## Open questions

1. What plugin, target group and category do the current events use? Read one
   with `get_event` before writing any of this.
2. How is a long-running strategy currently kept alive — a repeating schedule
   with `max_children: 1`, or something else? Match it.
3. Should a `trade_db.py` change really restart all seven strategies at once, or
   roll them one at a time with a gap?
4. Who receives the "needs a human" notifications — the existing ops Telegram
   chat, or somewhere separate?
5. Is `/jobs` the right place for the Phase 4 "restart when flat" button?
