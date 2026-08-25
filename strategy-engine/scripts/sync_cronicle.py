#!/usr/bin/env python3
"""Reconcile Cronicle's schedule with the strategies in this repository.

Run after a deploy. For every Version 2 strategy it finds on disk:

  * no Cronicle event yet  -> create one, and start it. A brand-new strategy
    holds no positions, so there is nothing to disturb.
  * event exists           -> update the definition (command, category, notes)
    and, if the code changed, mark it as needing a restart. It is NOT
    restarted here: a running strategy may be holding open positions, and
    whoever merges is not necessarily whoever is watching the account. The
    dashboard surfaces the pending restart with an open-position count.
  * event with no strategy -> reported as orphaned, never deleted automatically.

Matching is by a `[strategy:<id>]` marker written into the event's notes, not
by guessing from the script filename. The dashboard used to infer the link by
stripping separators from a filename and testing for a substring; an earlier,
looser version of that attributed three different strategies to one account.

Safety: starting a strategy that is already running would place duplicate
orders. Two things prevent that here — Cronicle's own max-concurrent limit,
and the engine's startup check, which refuses to become a second copy when the
registry shows a live heartbeat from another process.

    export CRONICLE_URL=http://127.0.0.1:3012
    export CRONICLE_API_KEY=...
    python scripts/sync_cronicle.py --dry-run
    python scripts/sync_cronicle.py
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _load_singleton():
    """Load engine/singleton.py without importing the engine package.

    The package __init__ pulls in the runtime, which imports trade_db, which
    needs psycopg2 — none of which this script has any use for. Loading the
    one module by path keeps the preflight working on a CI runner with only
    the standard library installed.
    """
    import importlib.util
    path = os.path.join(HERE, "engine", "singleton.py")
    try:
        spec = importlib.util.spec_from_file_location("engine_singleton", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"  WARN could not load the magic preflight ({e}) — "
              f"events will be created but never auto-started")
        return None


singleton = _load_singleton()

LIVE = os.path.join(HERE, "Live")

CRONICLE_URL = os.environ.get("CRONICLE_URL", "http://127.0.0.1:3012")
API_KEY = os.environ.get("CRONICLE_API_KEY", "")
# Cronicle categories are opaque ids ("cmi2sv17i01"), not names, so a
# hardcoded default is always wrong on someone else's install. Discovered from
# the categories the existing strategy events already use; the env var
# overrides, and "general" is only the last resort.
CATEGORY = os.environ.get("CRONICLE_CATEGORY", "")
# Category ids are opaque and differ per install ("cmt71div60o"), so the
# deploy names the category instead and this resolves it. Without one, the
# sync inherits whatever the neighbouring strategy events use, which is how
# the first V2 batch landed in someone else's category.
CATEGORY_TITLE = os.environ.get("CRONICLE_CATEGORY_TITLE", "")
# Cronicle shows an event's owner in the UI and filters by it. Events created
# through an API key are otherwise ownerless.
USERNAME = os.environ.get("CRONICLE_USERNAME", "")
TARGET = os.environ.get("CRONICLE_TARGET", "allgrp")
PLUGIN = os.environ.get("CRONICLE_PLUGIN", "shellplug")
DEPLOY_DIR = os.environ.get("STRATEGY_DIR", "/home/kaushal/strategy-engine")
PYTHON = os.environ.get("STRATEGY_PYTHON",
                        "/home/kaushal/strategy-engine/venv/bin/python3")

# libpq picks up ~/.postgresql/root.crt automatically. On this host that file
# exists under /root and is not a readable certificate, so every strategy
# Cronicle starts as root fails to reach CockroachDB and silently runs with
# persistence disabled — recording no trades and never heartbeating. Pointing
# at the system CA bundle is the correct fix rather than a dodge: the database
# is CockroachDB Cloud, which presents a publicly trusted certificate, so this
# verifies it properly instead of skipping verification.
SSL_ROOT_CERT = os.environ.get("STRATEGY_SSLROOTCERT",
                               "/etc/ssl/certs/ca-certificates.crt")

MARKER = re.compile(r"\[strategy:([A-Za-z0-9._-]+)\]")
UA = "strategy-engine-sync/2.0"


class CronicleError(RuntimeError):
    pass


def api(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        CRONICLE_URL.rstrip("/") + path, data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "X-API-Key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode(), strict=False)
    except urllib.error.HTTPError as e:
        raise CronicleError(f"HTTP {e.code} from {path}") from e
    except Exception as e:
        raise CronicleError(f"cannot reach Cronicle at {CRONICLE_URL}: {e}") from e
    if data.get("code"):
        raise CronicleError(f"{path}: {data.get('description') or data['code']}")
    return data


# --- what is on disk ----------------------------------------------------------

def discover():
    """Strategies this script manages: a .py under Live/ with a sibling .json.

    Two shapes qualify. A Version 2 strategy is a short declaration built by
    `make_strategy()` and run through `strategy.cli()`. A Version 1 script is
    a self-contained program that registers itself with `trade_db.init()` —
    Strategy 18.1 is one, at ~2600 lines apiece, and rewriting those as
    declarations would risk changing signals for no operational gain. What
    both have in common is the thing that matters here: a sidecar config, and
    an id they report to the registry.

    A .py with neither marker is a helper module, not a strategy.

    Self-contained scripts must opt in with `"MANAGED": true` in their config.
    They are not safe to detect by shape alone: the repository also holds the
    original Version 1 S17 and S21 scripts, kept so the parity tests can load
    them, and those point at live accounts. Auto-managing anything that merely
    looks like a strategy would have started seven duplicates on real money.
    Declared strategies need no flag — a declaration only exists to be run.
    """
    found = []
    for root, _dirs, files in os.walk(LIVE):
        for fn in sorted(files):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(root, fn)
            src = open(path, encoding="utf-8", errors="ignore").read()
            declared = "make_strategy(" in src and "strategy.cli()" in src
            self_contained = "trade_db.init(" in src
            if not (declared or self_contained):
                continue
            cfg = path[:-3] + ".json"
            if not os.path.exists(cfg):
                if declared:
                    print(f"  WARN {fn}: no config sidecar, skipping")
                continue
            if not declared and not _managed_flag(cfg):
                continue
            base = _strategy_id_of(path, src, cfg)
            if not base:
                print(f"  WARN {fn}: could not determine a strategy id, skipping")
                continue
            # The id the process actually reports is the declared one plus the
            # config's suffix. Match on that, so a shadow deployment's event
            # lines up with the shadow's registry row instead of the row
            # belonging to the generation it was copied from.
            suffix = _suffix_of(cfg)
            rel = os.path.relpath(path, HERE)
            found.append({"id": base + suffix, "base_id": base, "path": path,
                          "rel": rel, "config": cfg, "magic": _magic_of(cfg),
                          "kind": "declared" if declared else "self-contained"})
    return found


def _managed_flag(cfg):
    """Whether a self-contained script's config opts it into management."""
    try:
        return bool(json.load(open(cfg)).get("MANAGED", False))
    except Exception:
        return False


def _suffix_of(cfg):
    try:
        return str(json.load(open(cfg)).get("STRATEGY_ID_SUFFIX", "") or "")
    except Exception:
        return ""


def _strategy_id_of(path, src, cfg):
    m = re.search(r"""\bid\s*=\s*["']([A-Za-z0-9._-]+)["']""", src)
    if m:
        return m.group(1)
    # A self-contained Version 1 script names itself when it registers.
    m = re.search(r"""trade_db\.init\(\s*["']([A-Za-z0-9._-]+)["']""", src)
    if m:
        return m.group(1)
    # S21 declares its id inside its core module rather than the entry file
    core = os.path.join(os.path.dirname(path), "s21_core.py")
    if os.path.exists(core):
        m = re.search(r"""id\s*=\s*["']([A-Za-z0-9._-]+)["']""",
                      open(core, encoding="utf-8").read())
        if m:
            return m.group(1)
    return None


def _magic_of(cfg):
    try:
        return json.load(open(cfg)).get("MAGIC")
    except Exception:
        return None


def version_of(path):
    """Fingerprint of everything that decides how this strategy behaves.

    Not just the entry file. A Version 2 strategy is a short declaration; the
    behaviour lives in the shared engine and in the sibling core module, and
    the settings live in the sidecar config. Hashing only the entry file made
    a shared-engine fix invisible: S21 spent its whole first deployment
    raising TypeError on every scan, and the fix for it would have been
    reported as "unchanged" — nothing would have suggested a restart.

    The first sync after this lands will report every strategy as changed,
    because the fingerprint now covers more than it used to. That is accurate:
    those processes really are running code from before the engine moved.
    """
    import hashlib
    h = hashlib.sha1()
    parts = [path]
    stem = os.path.dirname(path)
    for sibling in ("s17_core.py", "s21_core.py"):
        cand = os.path.join(stem, sibling)
        if os.path.exists(cand):
            parts.append(cand)
    cfg = os.path.splitext(path)[0] + ".json"
    if os.path.exists(cfg):
        parts.append(cfg)
    engine_dir = os.path.join(HERE, "engine")
    if os.path.isdir(engine_dir):
        parts += [os.path.join(engine_dir, f)
                  for f in sorted(os.listdir(engine_dir)) if f.endswith(".py")]
    for f in parts:
        # The name goes in too, so moving code between modules registers as a
        # change even when the bytes are simply relocated.
        h.update(os.path.basename(f).encode())
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:10]


# --- event shape --------------------------------------------------------------

def _category_by_title(title):
    """Look up a category id by its display title, case-insensitively.

    Returns "" when the host has no such category. Creating one is not
    attempted: the deploy key deliberately carries edit_categories=0, so a
    missing category is a signal to go add it in the UI, not something to
    paper over by inventing an id.
    """
    try:
        cats = api("/api/app/get_categories/v1", {"limit": 100}).get("rows", [])
    except CronicleError as e:
        print(f"  WARN could not list categories ({e})")
        return ""
    want = title.strip().lower()
    for c in cats:
        if (c.get("title") or "").strip().lower() == want:
            return c.get("id") or ""
    return ""


def discover_category(schedule):
    """The category the host's existing strategy events use.

    Returns "" when nothing recognisable is scheduled yet, which leaves the
    event uncategorised rather than inventing a category id that does not
    exist on this install.
    """
    if CATEGORY:
        return CATEGORY
    if CATEGORY_TITLE:
        found = _category_by_title(CATEGORY_TITLE)
        if found:
            return found
        print(f"  WARN no category titled {CATEGORY_TITLE!r} on this host — "
              f"falling back to whatever the existing strategy events use")
    counts = {}
    for ev in schedule:
        script = (ev.get("params") or {}).get("script", "")
        if "Live/Strategy" in script or "Bridge-S" in script or marker_of(ev):
            cat = ev.get("category")
            if cat:
                counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def _maybe_start(strategy, event_id, dry_run, allow_duplicate, blocked, started):
    """Start a stopped event, unless its magic is already held."""
    if allow_duplicate:
        occupied = []
    else:
        occupied = (singleton.others_with_magic(strategy["magic"]) if singleton
                    else [(0, "preflight unavailable")])
    if occupied:
        why = (singleton.describe(occupied) if singleton
               else "magic preflight unavailable")
        blocked.append((strategy["id"], why))
        print(f"         stopped, and NOT started — {why}")
        return
    print(f"         stopped -> starting")
    if not dry_run:
        api("/api/app/run_event/v1", {"id": event_id})
    started.append(strategy["id"])


# Every field event_for() sets, other than the two compared separately
# (notes carries the version and the marker). Comparing the whole set rather
# than a hand-picked few: `detached` was added to event_for and simply never
# reached the host, because the old check looked at the script and the title
# and nothing else called it wrong. Thirteen strategies stayed undetached
# through a deploy that reported them "unchanged".
_MANAGED_FIELDS = ("title", "category", "target", "plugin", "enabled",
                   "timing", "catch_up", "max_children", "timeout",
                   "detached", "retries", "username")


def _settings_differ(have, want):
    """True when any field this script manages does not match the host's."""
    if (have.get("params") or {}).get("script") != want["params"]["script"]:
        return True
    for f in _MANAGED_FIELDS:
        if f not in want:
            continue
        mine, theirs = want[f], have.get(f)
        # Cronicle round-trips some of these as 0/1 and some as booleans.
        if isinstance(mine, bool) or isinstance(theirs, bool):
            if bool(mine) != bool(theirs):
                return True
        elif mine != theirs:
            return True
    return False


def event_for(strategy, category=None):
    """The Cronicle event for one strategy.

    The command deliberately matches the shape the existing events already use:
    cd into the strategy's OWN directory, then exec the interpreter on the bare
    filename. The working directory is load-bearing — a strategy's log_dir is
    relative ("./bridge/<name> Logs"), and that directory holds the fired-event
    ledger recording which setups have already been traded. Running from the
    repository root instead would point every strategy at an empty ledger.
    """
    workdir = os.path.join(DEPLOY_DIR, os.path.dirname(strategy["rel"]))
    env = f"export PGSSLROOTCERT='{SSL_ROOT_CERT}'\n" if SSL_ROOT_CERT else ""
    script = (f"cd '{workdir}'\n{env}"
              f"exec {PYTHON} '{os.path.basename(strategy['rel'])}'")
    return {
        "title": f"Strategy: {strategy['id']}",
        "category": category if category is not None else CATEGORY,
        "target": TARGET,
        "plugin": PLUGIN,
        "enabled": 1,
        **({"username": USERNAME} if USERNAME else {}),
        "params": {"script": script, "annotate": 0, "json": 0},
        # A live strategy is a long-running process, not a periodic task, so it
        # carries no schedule: Cronicle starts it and it stays up. `timing:
        # false` is what the fleet already uses.
        "timing": False,
        "catch_up": 0,
        # The engine's own startup check is the real guard against a second
        # copy; this makes Cronicle refuse the obvious case too.
        "max_children": 1,
        # Survive a Cronicle restart. Without this the daemon aborts the job
        # when it restarts, and nothing brings a live strategy back: all
        # thirteen shadows died on 2026-08-25 with
        #   "Aborted Job: Server ... shut down unexpectedly"
        # while thirty-five hand-made events carried on, because every one of
        # those was created through the UI with Detached ticked.
        "detached": 1,
        # Matches the existing fleet. A live strategy is a long-running
        # process, so a retry after it exits would start a second copy rather
        # than recover the first.
        "retries": 0,
        # A live strategy runs until it is stopped. The existing events use a
        # one-year timeout rather than 0, whose meaning differs between
        # Cronicle versions; match what is known to work on this host.
        "timeout": 31536000,
        "notes": (f"[strategy:{strategy['id']}] magic={strategy['magic']}\n"
                  f"Managed by scripts/sync_cronicle.py — edit the strategy, "
                  f"not this event.\nSource: {strategy['rel']}\n"
                  f"version={version_of(strategy['path'])}"),
    }


def marker_of(ev):
    m = MARKER.search(ev.get("notes") or "")
    return m.group(1) if m else None


# --- reconcile ----------------------------------------------------------------

def sync(dry_run=False, start_new=True, allow_duplicate=False,
         start_stopped=False):
    strategies = discover()
    if not strategies:
        print("no Version 2 strategies found under Live/ — nothing to do")
        return 0

    # /v1 endpoints — the same ones the dashboard's cronicle_client uses
    # against this instance.
    if allow_duplicate:
        print("  ALLOW_DUPLICATE is set — strategies will be started even where")
        print("  another process already stamps the same magic.")
    schedule = api("/api/app/get_schedule/v1", {"limit": 500}).get("rows", [])
    active = api("/api/app/get_active_jobs/v1", {}).get("jobs", {}) or {}
    running_events = {j.get("event") for j in active.values()}
    category = discover_category(schedule)
    if category:
        if CATEGORY:
            why = "set explicitly"
        elif CATEGORY_TITLE and category == _category_by_title(CATEGORY_TITLE):
            why = f"{CATEGORY_TITLE!r}"
        else:
            why = "inherited from the host's existing events"
        print(f"  using category {category} ({why})")
    if USERNAME:
        print(f"  events owned by {USERNAME}")
    by_marker = {}
    for ev in schedule:
        sid = marker_of(ev)
        if sid:
            by_marker[sid] = ev

    created, updated, restart_pending, unchanged = [], [], [], []
    blocked, restarted = [], []

    for s in strategies:
        want = event_for(s, category=category)
        have = by_marker.pop(s["id"], None)
        if have is None and s.get("base_id") and s["base_id"] != s["id"]:
            # Events created before the marker carried the suffix. Adopt and
            # re-stamp them rather than creating a second event beside a
            # running strategy.
            have = by_marker.pop(s["base_id"], None)

        if have is None:
            # Preflight: is anything on this host already stamping this magic?
            # During a cutover the Version 1 script is usually still running,
            # and two processes on one magic place duplicate orders. This asks
            # the operating system rather than the registry, because a
            # strategy with broken persistence does not heartbeat and would
            # otherwise look dead.
            # No preflight available means we cannot prove the magic is free,
            # so we do not start. Refusing to start is recoverable; starting a
            # second process on a live magic is not.
            if allow_duplicate:
                occupied = []          # deliberately shadowing another copy
            else:
                # No preflight available means we cannot prove the magic is
                # free, so we do not start. Refusing to start is recoverable;
                # starting a second process on a live magic is not.
                occupied = (singleton.others_with_magic(s["magic"]) if singleton
                            else [(0, "preflight unavailable")])
            print(f"  CREATE {s['id']}  (magic {s['magic']})")
            if not dry_run:
                r = api("/api/app/create_event/v1", want)
                ev_id = r.get("id")
                if start_new and ev_id and occupied:
                    why = (singleton.describe(occupied) if singleton
                           else "magic preflight unavailable")
                    blocked.append((s["id"], why))
                    print(f"         created but NOT started — {why}")
                elif start_new and ev_id:
                    # Nothing else holds this magic, so starting places no
                    # duplicate order.
                    api("/api/app/run_event/v1", {"id": ev_id})
                    print(f"         started (event {ev_id})")
            elif occupied:
                why = (singleton.describe(occupied) if singleton
                       else "magic preflight unavailable")
                blocked.append((s["id"], why))
                print(f"         would create but NOT start — {why}")
            created.append(s["id"])
            continue

        is_running = have["id"] in running_events

        changed = (
            _settings_differ(have, want)
            # The marker is what the dashboard joins on, so a stale one is a
            # real difference even when everything else matches — that is how
            # an adopted pre-suffix event gets re-stamped.
            or marker_of(have) != marker_of(want)
            or (have.get("notes") or "").split("version=")[-1].strip()
               != want["notes"].split("version=")[-1].strip()
        )
        if not changed:
            unchanged.append(s["id"])
            if start_stopped and not is_running:
                _maybe_start(s, have["id"], dry_run, allow_duplicate,
                             blocked, restarted)
            continue

        code_changed = ((have.get("notes") or "").split("version=")[-1].strip()
                        != want["notes"].split("version=")[-1].strip())
        print(f"  UPDATE {s['id']}" + ("  (code changed)" if code_changed else ""))
        if not dry_run:
            payload = dict(want)
            payload["id"] = have["id"]
            api("/api/app/update_event/v1", payload)
        updated.append(s["id"])
        if start_stopped and not is_running:
            # The event exists but nothing is running it, so there is no
            # process to disturb and no position being managed — the same
            # argument that makes starting a brand-new event safe. This is
            # also what closes the gap where a host reboot leaves every
            # strategy down until somebody notices.
            _maybe_start(s, have["id"], dry_run, allow_duplicate,
                         blocked, restarted)
            continue
        if code_changed:
            # Deliberately NOT restarted here. The strategy may be holding open
            # positions; restarting is a decision someone makes while watching
            # the account, from the dashboard.
            restart_pending.append(s["id"])

    orphans = sorted(by_marker)

    print()
    print(f"  created          {len(created)}")
    print(f"  updated          {len(updated)}")
    print(f"  unchanged        {len(unchanged)}")
    print(f"  started         {len(restarted)}"
          + (f"  -> {', '.join(restarted)}" if restarted else ""))
    print(f"  restart pending  {len(restart_pending)}"
          + (f"  -> {', '.join(restart_pending)}" if restart_pending else ""))
    if orphans:
        print(f"  ORPHANED events (no strategy on disk): {', '.join(orphans)}")
        print("    left alone — delete them by hand once you are sure.")
    if restart_pending:
        print()
        print("  Those strategies are running older code. Restart them from the")
        print("  dashboard Jobs page once you have checked open positions.")
    if blocked:
        print()
        print(f"  {len(blocked)} event(s) created but NOT started — their magic is")
        print("  already in use on this host:")
        for sid, detail in blocked:
            print(f"    {sid}: {detail}")
        print()
        print("  This is the cutover, and it is deliberate: two processes on one")
        print("  magic place duplicate orders. Stop the old process, then start")
        print("  the new event from the dashboard Jobs page. Each strategy also")
        print("  refuses to start on its own if the magic is still held, so a")
        print("  mistake here fails safe rather than double-trading.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, touch nothing")
    ap.add_argument("--no-start", action="store_true",
                    help="create new events but do not start them")
    ap.add_argument("--start-stopped",
                    action="store_true",
                    default=os.environ.get("START_STOPPED", "") not in ("", "0", "false"),
                    help="also start events that exist but are not running "
                         "(START_STOPPED=1). Nothing is running them, so there "
                         "is no process to disturb — this is what brings a "
                         "fleet back up after a host reboot.")
    ap.add_argument("--allow-duplicate",
                    action="store_true",
                    default=os.environ.get("ALLOW_DUPLICATE", "") not in ("", "0", "false"),
                    help="start a strategy even when another process on this "
                         "host already stamps its magic (ALLOW_DUPLICATE=1). "
                         "Only sane on demo accounts, and only when the two "
                         "copies trade DIFFERENT accounts.")
    args = ap.parse_args()

    if not API_KEY and not args.dry_run:
        print("CRONICLE_API_KEY is not set — refusing to write", file=sys.stderr)
        return 2
    try:
        return sync(dry_run=args.dry_run, start_new=not args.no_start,
                    allow_duplicate=args.allow_duplicate,
                    start_stopped=args.start_stopped)
    except CronicleError as e:
        print(f"cronicle: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
