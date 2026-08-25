"""
Postgres persistence layer for the live strategy scripts.

Records the full signal -> execution -> trailing -> close lifecycle so a
restarted strategy can rebuild its in-memory trailing state (_ticket_map)
and resume managing open positions.

Design rule: the database must NEVER block or break trading. Every public
function swallows its own exceptions, uses short timeouts, and degrades to
a no-op (with a console warning) when the database is unreachable or
init() was never called.

Usage from a strategy script:

    import trade_db
    trade_db.init("S17-M3M2-V1-BTCUSDT-SELL",
                  _config.get("TRADE_DB_URL", ""), magic=MAGIC,
                  bridge_url=MT5_BRIDGE_URL)

init() also self-registers the strategy in the shared strategy_registry
table (strategy_id, magic, account, host, pid, script version hash) and
starts a 60s heartbeat that bumps last_seen — so the dashboard can
attribute trades by magic and show liveness with zero manual configuration.
Each process run additionally opens a strategy_sessions row (restart /
uptime / deploy history) and resolves the server's public IP in the
background, so the dashboard can show exactly where and which build of a
strategy is running.

Connection URL resolution order: TRADE_DB_URL environment variable, then
the db_url argument (normally the script's JSON config), then a gitignored
`trade_db_url.local` file next to this module — so the credential never
has to be committed to the repo.
"""

import atexit
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_OK = True
except ImportError:
    psycopg2 = None
    _PSYCOPG2_OK = False

_CONNECT_TIMEOUT = 4   # seconds; Neon cold starts are usually < 2s
_lock = threading.Lock()

# Hardcoded Neon connection string — the guaranteed fallback so DB persistence
# works even when a strategy's JSON config, the TRADE_DB_URL env var, and the
# gitignored trade_db_url.local file are all absent. Private repo, single
# source of truth for every strategy. Env / config still take precedence
# (see init) so it can be pointed elsewhere without editing code.
_HARDCODED_DB_URL = (
    "postgresql://floyd:4NOc9B_RfdRuvNiCoU3A4w@mt5-strategy-engine-30775.j77.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb?sslmode=require"
)

_strategy_id = None
_magic = None
_db_url = None
_conn = None
_warned_disabled = False
_session_id = None    # strategy_sessions row for THIS process run
_public_ip = None     # resolved async by _public_ip_thread
_version = None       # sha1[:10] of the running script — deploy fingerprint

# event_id -> trades.id cache so hot-path writes skip a lookup query
_trade_id_cache = {}


def _say(msg):
    print(f"[trade_db] {msg}", flush=True)


def _local_url_file():
    """Gitignored trade_db_url.local next to this module (keeps the
    credential out of the committed JSON configs)."""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_db_url.local")
        if os.path.exists(p):
            with open(p) as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def init(strategy_id, db_url=None, magic=None, bridge_url=None, label=None,
         timeframe=None, extra=None, symbols=None):
    """Configure the module. Safe to call even with no URL (disables DB).
    bridge_url/label/timeframe/extra feed the strategy registry (optional)."""
    global _strategy_id, _magic, _db_url
    _strategy_id = strategy_id
    _magic = magic
    _db_url = (os.environ.get("TRADE_DB_URL") or (db_url or "")
               or _local_url_file() or _HARDCODED_DB_URL)
    if not _PSYCOPG2_OK:
        _say("psycopg2 not installed — DB persistence DISABLED (pip install psycopg2-binary)")
        return
    if not _db_url:
        _say("no TRADE_DB_URL configured — DB persistence DISABLED")
        return
    if _connect():
        _say(f"connected — strategy_id={strategy_id}")
        _register(bridge_url=bridge_url, label=label, timeframe=timeframe,
                  extra=extra, symbols=symbols)


def enabled():
    return _PSYCOPG2_OK and bool(_db_url) and _strategy_id is not None


def _connect():
    global _conn
    try:
        _conn = psycopg2.connect(_db_url, connect_timeout=_CONNECT_TIMEOUT)
        _conn.autocommit = True
        return True
    except Exception as e:
        _conn = None
        _say(f"connect failed: {e}")
        return False


# ── strategy registry ────────────────────────────────────────────
# Every strategy publishes itself here on init: its magic number (stamped on
# all its MT5 orders), the account it trades, and where it runs. A daemon
# heartbeat bumps last_seen every 60s so the dashboard can show liveness and
# alert when a strategy goes silent. All best-effort — registry problems
# never block trading.

_HEARTBEAT_SEC = 60
_hb_started = False

# Public-IP echo services tried in order at startup (plain-text responses).
# Knowing the public IP pins a strategy to the actual server it runs on —
# hostnames alone are ambiguous across VPSes.
_IP_SERVICES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
)


def _parse_bridge_url(bridge_url):
    """(login, account_type) from a .../<login>/<type> bridge URL."""
    try:
        parts = (bridge_url or "").rstrip("/").split("/")
        return int(parts[-2]), parts[-1]
    except (ValueError, IndexError):
        return None, ""


def _script_fingerprint():
    """(sha1[:10], abs path) of the entry-point script. Registered alongside
    each run so the dashboard can tell WHICH code generation runs where —
    a stale or partial deploy shows up as a mismatched version hash."""
    try:
        p = os.path.abspath(sys.argv[0])
        with open(p, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:10], p
    except Exception:
        return None, None


def _fetch_public_ip():
    for url in _IP_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read(64).decode("ascii", "ignore").strip()
                if ip and len(ip) <= 45 and all(c in "0123456789abcdef.:" for c in ip.lower()):
                    return ip
        except Exception:
            continue
    return None


def _public_ip_thread():
    """Resolve the server's public IP off-thread (never delays trading) and
    stamp it on the registry row and this run's session row. A few retries
    cover networks still coming up at boot."""
    global _public_ip
    for attempt in range(3):
        ip = _fetch_public_ip()
        if ip:
            _public_ip = ip
            _exec("UPDATE strategy_registry SET public_ip=%s WHERE strategy_id=%s",
                  (ip, _strategy_id))
            if _session_id:
                _exec("UPDATE strategy_sessions SET public_ip=%s WHERE id=%s",
                      (ip, _session_id))
            _say(f"public ip {ip}")
            return
        time.sleep(30)


def _session_end(reason="process_exit"):
    """atexit hook: mark this run's session row ended. A session that is
    stale but never ended = the process died without cleanup (crash/kill)."""
    if _session_id:
        _exec("UPDATE strategy_sessions SET ended_at=now(), last_seen=now(), "
              "end_reason=%s WHERE id=%s AND ended_at IS NULL",
              (reason, _session_id))


def _register(bridge_url=None, label=None, timeframe=None, extra=None,
              symbols=None):
    global _hb_started, _session_id, _version
    try:
        _exec("""CREATE TABLE IF NOT EXISTS strategy_registry (
                     strategy_id  TEXT PRIMARY KEY,
                     magic        INTEGER UNIQUE,
                     label        TEXT,
                     symbols      JSONB NOT NULL DEFAULT '[]'::jsonb,
                     timeframe    TEXT,
                     account      BIGINT,
                     account_type TEXT,
                     host         TEXT,
                     pid          INTEGER,
                     started_at   TIMESTAMPTZ,
                     last_seen    TIMESTAMPTZ,
                     extra        JSONB NOT NULL DEFAULT '{}'::jsonb)""")
        # in-place upgrades for databases created before these columns existed
        _exec("ALTER TABLE strategy_registry ADD COLUMN IF NOT EXISTS public_ip TEXT")
        _exec("ALTER TABLE strategy_registry ADD COLUMN IF NOT EXISTS version TEXT")
        # one row per process run — restarts, uptime timeline and deploy
        # history all fall out of this table (heartbeat bumps last_seen;
        # gaps between sessions are the offline periods)
        _exec("""CREATE TABLE IF NOT EXISTS strategy_sessions (
                     id          BIGSERIAL PRIMARY KEY,
                     strategy_id TEXT NOT NULL,
                     magic       INTEGER,
                     host        TEXT,
                     pid         INTEGER,
                     public_ip   TEXT,
                     version     TEXT,
                     script_path TEXT,
                     started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                     last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
                     ended_at    TIMESTAMPTZ,
                     end_reason  TEXT)""")
        _exec("""CREATE INDEX IF NOT EXISTS idx_sessions_sid_started
                 ON strategy_sessions (strategy_id, started_at DESC)""")
        magic = _magic
        if magic is not None:
            # a magic must belong to exactly one strategy; on a collision keep
            # trading but register without it and shout so it gets fixed
            row = _exec("SELECT strategy_id FROM strategy_registry "
                        "WHERE magic=%s AND strategy_id<>%s",
                        (int(magic), _strategy_id), fetch="one")
            if row:
                _say(f"WARNING: magic {magic} already registered to "
                     f"{row['strategy_id']} — registering without magic")
                magic = None
        login, atype = _parse_bridge_url(bridge_url)
        _version, script_path = _script_fingerprint()
        _exec(
            """INSERT INTO strategy_registry
                   (strategy_id, magic, label, timeframe, account, account_type,
                    host, pid, started_at, last_seen, extra, version, symbols)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s, %s)
               ON CONFLICT (strategy_id) DO UPDATE SET
                   magic=COALESCE(EXCLUDED.magic, strategy_registry.magic),
                   label=EXCLUDED.label,
                   symbols=EXCLUDED.symbols,
                   timeframe=COALESCE(EXCLUDED.timeframe, strategy_registry.timeframe),
                   account=COALESCE(EXCLUDED.account, strategy_registry.account),
                   account_type=COALESCE(NULLIF(EXCLUDED.account_type, ''),
                                         strategy_registry.account_type),
                   host=EXCLUDED.host, pid=EXCLUDED.pid,
                   started_at=now(), last_seen=now(),
                   extra=EXCLUDED.extra, version=EXCLUDED.version""",
            (_strategy_id, int(magic) if magic is not None else None,
             label or _strategy_id, timeframe, login, atype or None,
             socket.gethostname(), os.getpid(), _jsonb(extra or {}), _version,
             _jsonb(list(symbols or []))))
        row = _exec(
            """INSERT INTO strategy_sessions
                   (strategy_id, magic, host, pid, version, script_path)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (_strategy_id, int(magic) if magic is not None else None,
             socket.gethostname(), os.getpid(), _version, script_path),
            fetch="one")
        if row:
            _session_id = row["id"]
            atexit.register(_session_end)
        _say(f"registered — magic={magic} account={login} "
             f"host={socket.gethostname()} version={_version} session={_session_id}")
        if not _hb_started:
            _hb_started = True
            threading.Thread(target=_heartbeat_loop, daemon=True,
                             name="trade_db-heartbeat").start()
            threading.Thread(target=_public_ip_thread, daemon=True,
                             name="trade_db-public-ip").start()
    except Exception as e:
        _say(f"registry error (ignored): {e}")


def _heartbeat_loop():
    while True:
        time.sleep(_HEARTBEAT_SEC)
        try:
            _exec("UPDATE strategy_registry SET last_seen=now() WHERE strategy_id=%s",
                  (_strategy_id,))
            if _session_id:
                _exec("UPDATE strategy_sessions SET last_seen=now() WHERE id=%s",
                      (_session_id,))
        except Exception:
            pass


def _exec(sql, params=(), fetch=None):
    """Run one statement; reconnect once on connection errors.
    Returns fetched rows / row / rowcount, or None on failure."""
    global _warned_disabled
    if not enabled():
        if not _warned_disabled:
            _say("persistence disabled — skipping DB writes")
            _warned_disabled = True
        return None
    with _lock:
        for attempt in (1, 2):
            try:
                if _conn is None or _conn.closed:
                    if not _connect():
                        return None
                with _conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    if fetch == "one":
                        return cur.fetchone()
                    if fetch == "all":
                        return cur.fetchall()
                    return cur.rowcount
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                if attempt == 2:
                    _say(f"query failed after reconnect: {e}")
                    return None
                try:
                    _conn.close()
                except Exception:
                    pass
                if not _connect():
                    return None
            except Exception as e:
                _say(f"query error: {e}")
                return None
    return None


def _jsonb(obj):
    return psycopg2.extras.Json(obj, dumps=lambda o: json.dumps(o, default=str))


def _num(x):
    """Coerce script values ('123.4', '', 'None', NaN, None) to float or None."""
    try:
        v = float(x)
        return v if v == v else None   # NaN check
    except (TypeError, ValueError):
        return None


def _trade_id(event_id=None, ticket=None):
    if event_id and event_id in _trade_id_cache:
        return _trade_id_cache[event_id]
    if event_id is not None:
        row = _exec("SELECT id FROM trades WHERE strategy_id=%s AND event_id=%s",
                    (_strategy_id, event_id), fetch="one")
    elif ticket is not None:
        row = _exec("SELECT id, event_id FROM trades WHERE strategy_id=%s AND ticket=%s "
                    "ORDER BY id DESC LIMIT 1", (_strategy_id, ticket), fetch="one")
    else:
        return None
    if row:
        if event_id:
            _trade_id_cache[event_id] = row["id"]
        return row["id"]
    return None


def _event(trade_id, event_type, payload=None):
    if trade_id is None:
        return
    _exec("INSERT INTO trade_events (trade_id, event_type, payload) VALUES (%s, %s, %s)",
          (trade_id, event_type, _jsonb(payload or {})))


# ── lifecycle writers ────────────────────────────────────────────


def record_signal(event_id, symbol, side, signal_price=None, qty=None,
                  hard_sl=None, extra=None):
    """New signal detected. Idempotent on (strategy_id, event_id)."""
    try:
        row = _exec(
            """INSERT INTO trades (strategy_id, event_id, symbol, side, magic,
                                   status, signal_price, qty, hard_sl)
               VALUES (%s, %s, %s, %s, %s, 'SIGNAL', %s, %s, %s)
               ON CONFLICT (strategy_id, event_id) DO NOTHING
               RETURNING id""",
            (_strategy_id, event_id, symbol, side, _magic,
             _num(signal_price), _num(qty), _num(hard_sl)), fetch="one")
        if row:
            _trade_id_cache[event_id] = row["id"]
            _event(row["id"], "SIGNAL", {"signal_price": _num(signal_price), "qty": _num(qty),
                                         "hard_sl": _num(hard_sl), **(extra or {})})
    except Exception as e:
        _say(f"record_signal error: {e}")


def record_execution(event_id, ticket, retcode, entry_price=None, qty=None,
                     hard_sl=None, targets=None, error=None):
    """MT5 responded to the order. retcode 10009 + ticket => OPEN, else REJECTED."""
    try:
        ok = bool(ticket) and int(ticket) > 0 and int(retcode or 0) == 10009
        status = "OPEN" if ok else "REJECTED"
        _exec(
            """UPDATE trades SET status=%s, ticket=%s, retcode=%s, entry_price=%s,
                   qty=COALESCE(%s, qty), hard_sl=COALESCE(%s, hard_sl),
                   current_sl=COALESCE(%s, current_sl), targets=COALESCE(%s, targets),
                   error=%s, opened_at=CASE WHEN %s THEN now() ELSE opened_at END,
                   updated_at=now()
               WHERE strategy_id=%s AND event_id=%s""",
            (status, ticket if ok else None, retcode, _num(entry_price), _num(qty),
             _num(hard_sl), _num(hard_sl), _jsonb(targets) if targets else None,
             error, ok, _strategy_id, event_id))
        _event(_trade_id(event_id=event_id), "MT5_RESULT",
               {"ticket": ticket, "retcode": retcode, "entry_price": _num(entry_price),
                "hard_sl": _num(hard_sl), "status": status, "error": error})
    except Exception as e:
        _say(f"record_execution error: {e}")


def record_trail(ticket, new_sl, ratio, executed=True):
    """Trailing logic moved (or tried to move) the SL for an open ticket.

    Only an executed move updates current_sl/trail_hits — the dashboard shows
    current_sl as THE stop, so a modify MT5 rejected must not drift the DB
    away from the broker's truth (see TRAIL_SL_FIX.md). Failed attempts are
    still recorded as TRAIL_MOVE events (executed=false) for audit."""
    try:
        if executed:
            _exec(
                """UPDATE trades SET current_sl=%s,
                       trail_hits=(SELECT to_jsonb(ARRAY(
                           SELECT DISTINCT e FROM jsonb_array_elements(trail_hits || %s) AS e))),
                       updated_at=now()
                   WHERE strategy_id=%s AND ticket=%s AND status='OPEN'""",
                (_num(new_sl), _jsonb([ratio]), _strategy_id, ticket))
        _event(_trade_id(ticket=ticket), "TRAIL_MOVE",
               {"new_sl": _num(new_sl), "ratio": ratio, "executed": bool(executed)})
    except Exception as e:
        _say(f"record_trail error: {e}")


def record_close(ticket, reason="not_in_positions", pnl=None, exit_price=None):
    """Position no longer open on MT5 (or found closed during recovery).

    exit_price is the broker's own close price. It cannot be recovered from
    pnl afterwards — MT5 P/L is money, not price distance, so dividing it back
    out needs the per-symbol contract size and was putting XAUUSD exit markers
    100x too far from entry. COALESCE keeps an already-recorded price if this
    call could not determine one."""
    try:
        _exec(
            """UPDATE trades SET status='CLOSED', close_reason=%s,
                   pnl=COALESCE(%s, pnl),
                   exit_price=COALESCE(%s, exit_price),
                   closed_at=now(), updated_at=now()
               WHERE strategy_id=%s AND ticket=%s AND status='OPEN'""",
            (reason, _num(pnl), _num(exit_price), _strategy_id, ticket))
        _event(_trade_id(ticket=ticket), "CLOSE_DETECTED",
               {"reason": reason, "pnl": _num(pnl), "exit_price": _num(exit_price)})
    except Exception as e:
        _say(f"record_close error: {e}")


def mark_telegram_sent(event_id):
    try:
        _exec("UPDATE trades SET telegram_sent=TRUE, updated_at=now() "
              "WHERE strategy_id=%s AND event_id=%s", (_strategy_id, event_id))
    except Exception as e:
        _say(f"mark_telegram_sent error: {e}")


def record_recovery_event(ticket, note):
    try:
        _event(_trade_id(ticket=ticket), "RECOVERY", {"note": note})
    except Exception as e:
        _say(f"record_recovery_event error: {e}")


# ── crash recovery ───────────────────────────────────────────────


def load_open_trades():
    """Rows with status OPEN for this strategy, with JSONB fields converted
    back to the in-memory shapes the scripts use (float-keyed targets dict,
    trail-hit set). Returns [] when disabled or on failure."""
    try:
        rows = _exec(
            """SELECT event_id, symbol, side, ticket, entry_price, qty,
                      hard_sl, current_sl, targets, trail_hits
               FROM trades WHERE strategy_id=%s AND status='OPEN' AND ticket IS NOT NULL
               ORDER BY opened_at""", (_strategy_id,), fetch="all")
        out = []
        for r in rows or []:
            targets = {}
            for k, v in (r.get("targets") or {}).items():
                try:
                    targets[float(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            trail_hit = set()
            for h in (r.get("trail_hits") or []):
                try:
                    trail_hit.add(int(h) if float(h) == int(h) else float(h))
                except (TypeError, ValueError):
                    continue
            out.append({
                "event_id": r["event_id"],
                "symbol": r["symbol"],
                "side": r["side"],
                "ticket": int(r["ticket"]),
                "entry_price": float(r["entry_price"] or 0),
                "qty": float(r["qty"] or 0),
                "hard_sl": float(r["hard_sl"] or 0),
                "current_sl": float(r["current_sl"] or r["hard_sl"] or 0),
                "targets": targets,
                "trail_hit": trail_hit,
            })
        return out
    except Exception as e:
        _say(f"load_open_trades error: {e}")
        return []


# ── singleton guard ──────────────────────────────────────────────


def live_elsewhere(max_age_sec=180):
    """Is another process already running this strategy?

    Returns (True, description) when the registry shows a heartbeat from a
    DIFFERENT pid or host within max_age_sec. The heartbeat is written every
    60s, so a gap beyond 180s means the other process is gone.

    This exists because Version 2 lets CI create and start Cronicle events.
    Two processes on the same magic and account would both scan, both fire on
    the same setup, and place duplicate orders on live money — the failure the
    deploy pipeline was deliberately written to avoid. Rather than trusting
    every scheduler never to double-start, a strategy asks first.

    Fails OPEN: if the database cannot be reached the answer is (False, ...),
    because refusing to trade on a database outage is the worse failure.
    """
    if not enabled():
        return False, "persistence disabled — cannot check"
    try:
        row = _exec(
            """SELECT host, pid, public_ip,
                      EXTRACT(EPOCH FROM (now() - last_seen)) AS age
               FROM strategy_registry WHERE strategy_id=%s""",
            (_strategy_id,), fetch="one")
        if not row or row.get("age") is None:
            return False, "no previous registration"
        age = float(row["age"])
        if age > max_age_sec:
            return False, f"last heartbeat {int(age)}s ago — stale"
        same_process = (row.get("host") == socket.gethostname()
                        and int(row.get("pid") or 0) == os.getpid())
        if same_process:
            return False, "that heartbeat is this process"
        where = f"{row.get('host')}"
        if row.get("public_ip"):
            where += f" ({row['public_ip']})"
        return True, f"pid {row.get('pid')} on {where}, heartbeat {int(age)}s ago"
    except Exception as e:
        _say(f"live_elsewhere check failed: {e}")
        return False, f"check failed: {e}"
