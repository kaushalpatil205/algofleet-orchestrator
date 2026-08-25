"""Cover the Cronicle reconciliation without a Cronicle.

The server is not reachable from a developer machine, so the API is faked and
the decisions are checked: what gets discovered, what an event looks like, and
— the part that matters — that a code change never restarts a running strategy
by itself.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import sync_cronicle as SC       # noqa: E402


class FakeCronicle:
    def __init__(self, schedule=None, jobs=None):
        self.schedule = schedule or []
        self.jobs = jobs or {}
        self.calls = []

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if path == "/api/app/get_schedule/v1":
            return {"rows": self.schedule}
        if path == "/api/app/get_active_jobs/v1":
            return {"jobs": self.jobs}
        if path == "/api/app/create_event/v1":
            return {"id": "evt_" + payload["title"][-6:]}
        return {}

    def paths(self):
        return [p for p, _ in self.calls]


@pytest.fixture
def fake(monkeypatch):
    f = FakeCronicle()
    monkeypatch.setattr(SC, "api", f)
    monkeypatch.setattr(SC, "API_KEY", "test-key")
    return f


def test_discovers_every_managed_strategy():
    found = SC.discover()
    ids = {s["id"] for s in found}
    assert "S21-BTCUSD-LIVE-V2" in ids
    assert sum(1 for i in ids if i.startswith("S17-")) == 6
    assert sum(1 for i in ids if i.startswith("S18.1-")) == 6
    for s in found:
        assert s["magic"], f"{s['id']} has no magic in its config"


def test_the_id_is_the_one_the_process_reports():
    """The event marker joins to the registry row. A shadow reports its base
    id plus the config suffix, so the marker has to carry the suffix too —
    otherwise the shadow's event matches the row of the generation it was
    copied from, and the dashboard shows one strategy's data under another."""
    for s in SC.discover():
        suffix = SC._suffix_of(s["config"])
        assert s["id"] == s["base_id"] + suffix
        if suffix:
            assert s["id"].endswith(suffix)


def test_self_contained_scripts_need_explicit_opt_in(tmp_path, monkeypatch):
    """The repo keeps the original Version 1 S17/S21 scripts so the parity
    tests can load them, and those point at live accounts. Managing anything
    that merely looks like a strategy would start duplicates on real money."""
    rels = {s["rel"] for s in SC.discover()}
    for original in ("Live/Strategy 17/Bridge-S17-M2-M3-V4-XAUUSD-Buy-Live.py",
                     "Live/Strategy 17/Bridge-S17-M1-M2-V1-Forex-Live-Sell.py",
                     "Live/Strategy 21/Bridge-S21-1_10-Ratios-BTCUSD-Live.py"):
        assert os.path.exists(os.path.join(SC.HERE, original)), original
        assert original not in rels, f"live Version 1 script picked up: {original}"


def test_removing_the_managed_flag_drops_a_self_contained_strategy():
    cfg = next(s["config"] for s in SC.discover() if s["kind"] == "self-contained")
    original = open(cfg, "rb").read()
    try:
        d = json.loads(original)
        del d["MANAGED"]
        open(cfg, "w").write(json.dumps(d))
        assert cfg not in {s["config"] for s in SC.discover()}
    finally:
        with open(cfg, "wb") as f:
            f.write(original)
    assert cfg in {s["config"] for s in SC.discover()}


def test_declared_strategies_do_not_need_the_flag():
    for s in SC.discover():
        if s["kind"] == "declared":
            assert not SC._managed_flag(s["config"])


def test_magics_are_unique():
    """A duplicated magic means two strategies claiming the same orders."""
    found = SC.discover()
    magics = [s["magic"] for s in found]
    assert len(set(magics)) == len(magics), f"duplicate magic numbers: {magics}"





def test_event_carries_a_matchable_marker():
    s = SC.discover()[0]
    ev = SC.event_for(s)
    assert ev["timeout"] == 31536000, "a live strategy must not be timed out"
    assert SC.marker_of(ev) == s["id"], "the event must identify its strategy"
    assert ev["max_children"] == 1, "Cronicle must refuse an obvious second copy"
    assert ev["timing"] is False, "a live strategy is long-running, not scheduled"
    assert os.path.basename(s["rel"]) in ev["params"]["script"]


def test_event_runs_from_the_strategys_own_directory():
    """The working directory is load-bearing, not cosmetic.

    A strategy's log_dir is relative ("./bridge/<name> Logs"), and that
    directory holds the fired-event ledger recording which setups have already
    been traded. Starting from the repository root instead would point every
    strategy at an empty ledger, so a setup traded minutes before a restart
    could be traded again.
    """
    for s in SC.discover():
        script = SC.event_for(s)["params"]["script"]
        expected = os.path.join(SC.DEPLOY_DIR, os.path.dirname(s["rel"]))
        assert f"cd '{expected}'" in script, (
            f"{s['id']} must run from its own directory, got: {script}")
        # and the interpreter must be an absolute path, since cwd is not the repo
        assert "\nexec /" in script, f"{s['id']} needs an absolute interpreter"


def test_new_strategies_are_created_and_started(fake):
    SC.sync(dry_run=False, start_new=True)
    created = [p for p, _ in fake.calls if p == "/api/app/create_event/v1"]
    started = [p for p, _ in fake.calls if p == "/api/app/run_event/v1"]
    n = len(SC.discover())
    assert len(created) == n, "every strategy should be created on an empty schedule"
    assert len(started) == n, "a brand-new strategy holds no positions — start it"


def test_no_start_flag_creates_without_running(fake):
    SC.sync(dry_run=False, start_new=False)
    assert "/api/app/run_event/v1" not in fake.paths()


READS = {"/api/app/get_schedule/v1", "/api/app/get_active_jobs/v1"}


def test_dry_run_writes_nothing(fake):
    SC.sync(dry_run=True)
    assert set(fake.paths()) <= READS, \
        f"a dry run must only read, saw {fake.paths()}"


def test_changed_code_updates_but_never_restarts(fake, capsys):
    """The whole point. A running strategy may hold open positions, so a
    deploy updates the definition and flags it — a human restarts it."""
    strategies = SC.discover()
    # every strategy already has an event, but recorded at an older version
    fake.schedule = [
        dict(SC.event_for(s), id=f"evt{i}",
             notes=SC.event_for(s)["notes"].replace(
                 SC.version_of(s["path"]), "0000000000"))
        for i, s in enumerate(strategies)
    ]
    SC.sync(dry_run=False)
    paths = fake.paths()
    assert paths.count("/api/app/update_event/v1") == len(strategies), \
        "definitions should update"
    assert "/api/app/run_event/v1" not in paths, \
        "a code change must NEVER restart a running strategy automatically"
    assert "/api/app/create_event/v1" not in paths
    assert f"restart pending  {len(strategies)}" in capsys.readouterr().out


def test_unchanged_strategies_are_left_alone(fake, capsys):
    strategies = SC.discover()
    fake.schedule = [dict(SC.event_for(s), id=f"evt{i}")
                     for i, s in enumerate(strategies)]
    SC.sync(dry_run=False)
    assert "/api/app/update_event/v1" not in fake.paths()
    assert f"unchanged        {len(strategies)}" in capsys.readouterr().out


def test_orphaned_events_are_reported_not_deleted(fake, capsys):
    fake.schedule = [{"id": "evt_gone", "title": "Strategy: S99-RETIRED",
                      "notes": "[strategy:S99-RETIRED] magic=99999",
                      "params": {"script": "x"}}]
    SC.sync(dry_run=False)
    out = capsys.readouterr().out
    assert "ORPHANED" in out and "S99-RETIRED" in out
    assert "/api/app/delete_event/v1" not in fake.paths(), \
        "sync must never delete an event on its own"


# --- the cutover preflight ----------------------------------------------------

def test_a_magic_already_in_use_creates_but_does_not_start(fake, monkeypatch, capsys):
    """The cutover case. During it the Version 1 script is still running, and
    two processes on one magic place duplicate orders on a live account."""
    monkeypatch.setattr(
        SC.singleton, "others_with_magic",
        lambda magic, exclude_pid=None: [(4242, f"/live/Bridge-Old-{magic}.py")])
    SC.sync(dry_run=False, start_new=True)
    paths = fake.paths()
    assert paths.count("/api/app/create_event/v1") == len(SC.discover()), \
        "events should still be created"
    assert "/api/app/run_event/v1" not in paths, \
        "nothing may be started while its magic is held by another process"
    out = capsys.readouterr().out
    assert "created but NOT started" in out
    assert "Stop the old process" in out


def test_a_free_magic_starts_normally(fake, monkeypatch):
    monkeypatch.setattr(SC.singleton, "others_with_magic",
                        lambda magic, exclude_pid=None: [])
    SC.sync(dry_run=False, start_new=True)
    assert fake.paths().count("/api/app/run_event/v1") == len(SC.discover())


def test_preflight_is_reported_in_a_dry_run(fake, monkeypatch, capsys):
    monkeypatch.setattr(
        SC.singleton, "others_with_magic",
        lambda magic, exclude_pid=None: [(99, "/live/old.py")])
    SC.sync(dry_run=True)
    assert "would create but NOT start" in capsys.readouterr().out
    assert set(fake.paths()) <= READS, "a dry run must only read"


def test_category_is_taken_from_the_hosts_existing_events():
    """Cronicle categories are opaque ids, so a hardcoded default is always
    wrong on someone else's install."""
    sched = [
        {"category": "cmi2sv17i01", "notes": "", "params":
         {"script": "cd '/x/Live/Strategy 17' && python Bridge-S17-a.py"}},
        {"category": "cmi2sv17i01", "notes": "", "params":
         {"script": "cd '/x/Live/Strategy 17' && python Bridge-S17-b.py"}},
        {"category": "other", "notes": "", "params": {"script": "backup.sh"}},
    ]
    assert SC.discover_category(sched) == "cmi2sv17i01"


def test_no_recognisable_events_leaves_the_category_empty():
    assert SC.discover_category([{"category": "misc", "notes": "",
                                  "params": {"script": "backup.sh"}}]) == ""


def test_allow_duplicate_starts_despite_a_held_magic(fake, monkeypatch):
    """Deliberately shadowing a Version 1 copy, to compare the two side by
    side. Only sane on demo accounts trading DIFFERENT logins."""
    monkeypatch.setattr(
        SC.singleton, "others_with_magic",
        lambda magic, exclude_pid=None: [(4242, "/live/Bridge-Old.py")])
    SC.sync(dry_run=False, start_new=True, allow_duplicate=True)
    assert fake.paths().count("/api/app/run_event/v1") == len(SC.discover())


def test_missing_preflight_fails_closed(fake, monkeypatch):
    """If the magic cannot be checked, do not start. Refusing is recoverable;
    a second process on a live magic is not."""
    monkeypatch.setattr(SC, "singleton", None)
    SC.sync(dry_run=False, start_new=True)
    assert "/api/app/run_event/v1" not in fake.paths()


def test_event_points_libpq_at_a_usable_ca_bundle():
    """~/.postgresql/root.crt on the strategy host is not a readable
    certificate, so libpq picking it up leaves a strategy trading with
    persistence silently disabled — no trades recorded, no heartbeat."""
    s = SC.discover()[0]
    script = SC.event_for(s)["params"]["script"]
    assert "PGSSLROOTCERT=" in script, \
        "an event must not inherit the host's broken root certificate"
    assert script.index("PGSSLROOTCERT=") < script.index("exec "), \
        "the variable must be exported before the interpreter starts"


def test_start_stopped_brings_a_dead_event_back_up(fake, capsys):
    """A host reboot leaves every event present but nothing running. Starting
    those disturbs nothing — there is no process and no managed position."""
    strategies = SC.discover()
    fake.schedule = [dict(SC.event_for(s), id=f"evt{i}")
                     for i, s in enumerate(strategies)]
    fake.jobs = {}                        # nothing running
    SC.sync(dry_run=False, start_stopped=True, allow_duplicate=True)
    assert fake.paths().count("/api/app/run_event/v1") == len(SC.discover())
    assert f"started         {len(strategies)}" in capsys.readouterr().out


def test_start_stopped_leaves_running_events_alone(fake):
    strategies = SC.discover()
    fake.schedule = [dict(SC.event_for(s), id=f"evt{i}")
                     for i, s in enumerate(strategies)]
    fake.jobs = {f"j{i}": {"event": f"evt{i}"} for i in range(len(strategies))}
    SC.sync(dry_run=False, start_stopped=True, allow_duplicate=True)
    assert "/api/app/run_event/v1" not in fake.paths(), \
        "a running strategy must never be restarted as a side effect"


# --- naming the category ------------------------------------------------------

_OTHERS_CATEGORY = [
    {"category": "cmi2sv17i01", "notes": "", "params":
     {"script": "cd '/x/Live/Strategy 17' && python Bridge-S17-a.py"}},
]


def test_a_named_category_wins_over_the_neighbours(monkeypatch):
    """The whole point: without a name the sync inherits whatever category the
    events next to it happen to use, which put the first V2 batch under
    another person's jobs."""
    monkeypatch.setattr(SC, "CATEGORY_TITLE", "Sumit Jobs")
    monkeypatch.setattr(SC, "api", lambda p, _b: {"rows": [
        {"id": "cmi2sv17i01", "title": "Kaushal Jobs"},
        {"id": "cmt71div60o", "title": "Sumit Jobs"},
    ]})
    assert SC.discover_category(_OTHERS_CATEGORY) == "cmt71div60o"


def test_category_title_match_ignores_case_and_padding(monkeypatch):
    monkeypatch.setattr(SC, "CATEGORY_TITLE", "  sumit JOBS ")
    monkeypatch.setattr(SC, "api", lambda p, _b: {"rows": [
        {"id": "cmt71div60o", "title": "Sumit Jobs"}]})
    assert SC.discover_category([]) == "cmt71div60o"


def test_an_explicit_id_still_beats_a_title(monkeypatch):
    monkeypatch.setattr(SC, "CATEGORY", "cmforced0001")
    monkeypatch.setattr(SC, "CATEGORY_TITLE", "Sumit Jobs")
    monkeypatch.setattr(SC, "api", _boom)
    assert SC.discover_category(_OTHERS_CATEGORY) == "cmforced0001"


def test_a_missing_category_falls_back_instead_of_failing(monkeypatch, capsys):
    """The deploy key cannot create categories, so a typo must not take the
    deploy down — it warns and behaves as it did before."""
    monkeypatch.setattr(SC, "CATEGORY_TITLE", "Nobody Jobs")
    monkeypatch.setattr(SC, "api", lambda p, _b: {"rows": [
        {"id": "cmi2sv17i01", "title": "Kaushal Jobs"}]})
    assert SC.discover_category(_OTHERS_CATEGORY) == "cmi2sv17i01"
    assert "Nobody Jobs" in capsys.readouterr().out


def test_an_unreachable_category_list_falls_back(monkeypatch, capsys):
    monkeypatch.setattr(SC, "CATEGORY_TITLE", "Sumit Jobs")
    monkeypatch.setattr(SC, "api", _boom)
    assert SC.discover_category(_OTHERS_CATEGORY) == "cmi2sv17i01"
    assert "could not list categories" in capsys.readouterr().out


def _boom(_p, _b):
    raise SC.CronicleError("nope")


def test_events_carry_the_configured_owner(monkeypatch):
    """Events created through an API key are ownerless, so Cronicle's per-user
    view does not show them to the person who owns the strategies."""
    monkeypatch.setattr(SC, "USERNAME", "sumit")
    assert SC.event_for(SC.discover()[0])["username"] == "sumit"


def test_no_owner_configured_leaves_the_field_off(monkeypatch):
    monkeypatch.setattr(SC, "USERNAME", "")
    assert "username" not in SC.event_for(SC.discover()[0])


# --- what counts as a code change ---------------------------------------------

def _v(sc):
    return sc.version_of(sc.discover()[0]["path"])


def test_editing_the_shared_engine_counts_as_a_change(tmp_path, monkeypatch):
    """The reason this exists: S21's whole first deployment was broken by a
    shared-engine signature, and the fix for it hashed as "unchanged"."""
    before = _v(SC)
    target = os.path.join(SC.HERE, "engine", "indicators.py")
    original = open(target, "rb").read()
    try:
        with open(target, "ab") as f:
            f.write(b"\n# touched by a test\n")
        assert _v(SC) != before
    finally:
        with open(target, "wb") as f:
            f.write(original)
    assert _v(SC) == before


def test_editing_the_sidecar_config_counts_as_a_change():
    """Magic, account and risk live in the config. A process that read the old
    one is as stale as one running old code."""
    before = _v(SC)
    cfg = os.path.splitext(SC.discover()[0]["path"])[0] + ".json"
    original = open(cfg, "rb").read()
    try:
        d = json.loads(original)
        d["RISK_PER_TRADE"] = float(d.get("RISK_PER_TRADE") or 1) + 1
        open(cfg, "w").write(json.dumps(d))
        assert _v(SC) != before
    finally:
        with open(cfg, "wb") as f:
            f.write(original)
    assert _v(SC) == before


def test_the_fingerprint_is_stable_when_nothing_moves():
    assert _v(SC) == _v(SC)


# --- adopting events stamped before the marker carried the suffix -------------

def test_an_event_marked_with_the_pre_suffix_id_is_adopted(fake, capsys):
    """The seven shadow events on the host were created when the marker held
    the base id. Once the marker gained the suffix they must be RE-STAMPED,
    not duplicated — a second event beside a running strategy would place
    duplicate orders on the account."""
    strategies = [s for s in SC.discover() if s["id"] != s["base_id"]]
    assert strategies, "expected at least one suffixed strategy"
    # Shaped like the events actually on the host: title and marker both
    # carry the base id, everything else already current.
    fake.schedule = [
        dict(SC.event_for(s), id=f"evt{i}",
             title=SC.event_for(s)["title"].replace(s["id"], s["base_id"]),
             notes=SC.event_for(s)["notes"].replace(s["id"], s["base_id"]))
        for i, s in enumerate(strategies)
    ]
    SC.sync(dry_run=False)
    paths = fake.paths()
    assert "/api/app/create_event/v1" not in paths, \
        "adopting must not create a second event for a running strategy"
    assert paths.count("/api/app/update_event/v1") == len(strategies)
    assert "orphan" not in capsys.readouterr().out.lower()


def test_the_restamped_event_carries_the_suffixed_marker():
    s = next(x for x in SC.discover() if x["id"] != x["base_id"])
    ev = SC.event_for(s)
    assert SC.marker_of(ev) == s["id"]
    assert s["id"].endswith("-V2") and ev["title"].endswith("-V2")


def test_a_stale_marker_alone_is_enough_to_trigger_an_update():
    """Belt and braces: even if title and script already matched, a marker
    that no longer names the strategy must still be corrected."""
    s = next(x for x in SC.discover() if x["id"] != x["base_id"])
    want = SC.event_for(s)
    have = dict(want, id="evt0",
                notes=want["notes"].replace(s["id"], s["base_id"]))
    assert SC.marker_of(have) != SC.marker_of(want)


# --- surviving a Cronicle restart --------------------------------------------

def test_events_are_detached():
    """Without this Cronicle aborts the job when the daemon restarts, and
    nothing brings a live strategy back. All thirteen shadows died this way
    on 2026-08-25 — "Server ... shut down unexpectedly" — while thirty-five
    hand-made events carried on, every one of them created with Detached
    ticked in the UI."""
    for s in SC.discover():
        ev = SC.event_for(s)
        assert ev.get("detached") == 1, f"{s['id']} would not survive a restart"


def test_events_do_not_retry():
    """A live strategy runs until stopped, so a retry after it exits would
    start a second copy rather than recover the first."""
    assert SC.event_for(SC.discover()[0]).get("retries") == 0


def test_a_long_running_event_carries_no_schedule():
    """Guard for the pair: detached plus timing means Cronicle would keep
    launching new copies of something already running."""
    ev = SC.event_for(SC.discover()[0])
    assert ev["timing"] is False
    assert ev["max_children"] == 1


def test_a_changed_setting_is_detected_even_when_the_script_matches(fake, capsys):
    """The reason _settings_differ exists. `detached` was added to event_for
    and never reached the host: the old check compared the script and the
    title, so a deploy called thirteen undetached strategies "unchanged" and
    they stayed one Cronicle restart from dying."""
    strategies = SC.discover()
    fake.schedule = [
        # identical to what we want, except missing the one field
        {k: v for k, v in dict(SC.event_for(s), id=f"evt{i}").items()
         if k != "detached"}
        for i, s in enumerate(strategies)
    ]
    SC.sync(dry_run=False)
    paths = fake.paths()
    assert paths.count("/api/app/update_event/v1") == len(strategies), \
        "a missing managed field must count as changed"
    assert "/api/app/run_event/v1" not in paths, \
        "correcting a setting must not restart a running strategy"


@pytest.mark.parametrize("field,value", [
    ("timeout", 60), ("max_children", 5), ("enabled", 0),
    ("category", "somewhere-else"), ("retries", 3),
])
def test_each_managed_field_is_compared(fake, monkeypatch, field, value):
    # Pin the category: with none configured, discover_category deliberately
    # INHERITS whatever the host's events use, so a drifted category is not
    # drift — it is the documented fallback, and the sync would rightly
    # decide nothing changed.
    monkeypatch.setattr(SC, "CATEGORY", "cmpinned0001")
    strategies = SC.discover()
    fake.schedule = [dict(SC.event_for(s), id=f"evt{i}", **{field: value})
                     for i, s in enumerate(strategies)]
    SC.sync(dry_run=False)
    assert fake.paths().count("/api/app/update_event/v1") == len(strategies), \
        f"{field} drifting must be detected"


def test_an_untouched_event_is_still_reported_unchanged(fake, capsys):
    """The comparison must not become so eager that every deploy rewrites
    every event — that is how a real change stops standing out."""
    strategies = SC.discover()
    fake.schedule = [dict(SC.event_for(s), id=f"evt{i}")
                     for i, s in enumerate(strategies)]
    SC.sync(dry_run=False)
    assert "/api/app/update_event/v1" not in fake.paths()
    assert f"unchanged        {len(strategies)}" in capsys.readouterr().out
