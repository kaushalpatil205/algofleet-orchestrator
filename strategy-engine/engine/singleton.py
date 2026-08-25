"""Host-level detection of another strategy already running on a magic.

The engine's first guard asks the registry whether another process is
heartbeating for the same strategy_id. That is the right question most of the
time, and it works across hosts — but it trusts the database, and a strategy
whose persistence is broken does not heartbeat at all. On 2026-08-17 an
invalid root certificate on the strategy host did exactly that to three live
strategies: they kept trading, recorded nothing, and to the registry looked
long dead. Anything relying only on heartbeats would happily have started a
second copy of each.

So this asks the operating system instead. It scans /proc for other Python
processes running a strategy script, resolves each one's config sidecar, and
reports any that declares the same MAGIC. The magic is the right key: it is
what actually ends up stamped on the orders, so two processes sharing one are
two processes claiming the same trades, whatever they are called or which
generation they belong to. That is what makes it catch a Version 1 script and
a Version 2 declaration colliding during a cutover.

Linux-only by design — it is a guard for the strategy host. Everywhere else it
returns nothing and the registry check stands alone.
"""

import json
import os
import re

PROC = "/proc"


def _cmdline(pid):
    try:
        with open(f"{PROC}/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace")
                    for a in f.read().split(b"\0") if a]
    except (OSError, IOError):
        return []


def _cwd(pid):
    try:
        return os.readlink(f"{PROC}/{pid}/cwd")
    except (OSError, IOError):
        return None


def _script_of(args, cwd):
    """The .py a process is running, as an absolute path."""
    for a in args[1:]:
        if not a.endswith(".py"):
            continue
        if os.path.isabs(a):
            return a if os.path.exists(a) else None
        if cwd:
            p = os.path.join(cwd, a)
            return p if os.path.exists(p) else None
    return None


def _magic_of(script):
    """MAGIC from the script's config sidecar, or from the source as a
    fallback — Version 1 scripts default it inline when the key is absent."""
    cfg = script[:-3] + ".json"
    try:
        with open(cfg) as f:
            m = json.load(f).get("MAGIC")
        if m is not None:
            return int(m)
    except Exception:
        pass
    try:
        with open(script, encoding="utf-8", errors="ignore") as f:
            src = f.read(8000)
        m = re.search(r'MAGIC\s*=\s*int\(_config\.get\("MAGIC",\s*(\d+)\)', src)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def others_with_magic(magic, exclude_pid=None):
    """[(pid, script)] of other live processes stamping this magic.

    Empty on any platform without /proc, and never raises — a guard that
    crashes a startup is worse than one that abstains.
    """
    try:
        magic = int(magic)
    except (TypeError, ValueError):
        return []
    if not os.path.isdir(PROC):
        return []

    me = exclude_pid or os.getpid()
    found = []
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return []

    for pid_s in pids:
        pid = int(pid_s)
        if pid == me:
            continue
        args = _cmdline(pid)
        if not args or "python" not in os.path.basename(args[0]).lower():
            continue
        script = _script_of(args, _cwd(pid))
        if not script:
            continue
        try:
            if _magic_of(script) == magic:
                found.append((pid, script))
        except Exception:
            continue
    return found


def describe(others):
    return ", ".join(f"pid {p} running {os.path.basename(s)}" for p, s in others)
