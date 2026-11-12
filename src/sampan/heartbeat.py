"""Heartbeats: each `sampan mcp serve` says which session and conversation it serves, so a sender can tell whether
the target can take its inbox through MCP (the `inboxes` table in dispatches.db). Writers hold dispatches.lock."""
import os
import sqlite3

from . import procs
from .clock import parse_iso

HEARTBEAT_S = 120  # a heartbeat older than this no longer counts
MIN_VERSION = (0, 17)


def _iso(now):
    return now.isoformat(timespec="seconds")


def beat(conn, *, session_id, agent, conversation, nonce, pid, started, version, now):
    """Record that instance `nonce` serves session_id. An instance that started before another live row of the same
    session does not overwrite it (returns False); rows of older instances of the session are replaced."""
    for other, other_started in conn.execute(
            "SELECT nonce, started FROM inboxes WHERE session_id = ? AND nonce != ?", (session_id, nonce)).fetchall():
        if (other_started or "") > (started or ""):
            return False
    conn.execute("DELETE FROM inboxes WHERE session_id = ? AND nonce != ?", (session_id, nonce))
    conn.execute("INSERT OR REPLACE INTO inboxes (session_id, nonce, agent, conversation, pid, started, version, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (session_id, nonce, agent, conversation, pid, started, version, _iso(now)))
    return True


def forget(conn, nonce):
    conn.execute("DELETE FROM inboxes WHERE nonce = ?", (nonce,))


def _version_ok(v):
    try:
        return tuple(int(x) for x in (v or "").split(".")[:2]) >= MIN_VERSION
    except ValueError:
        return False


def _pid_alive(alive, pid):
    try:
        alive(pid, 0)
    except PermissionError:
        return True  # exists, owned by someone else
    except (OSError, TypeError, ValueError):
        return False
    return True


def valid(conn, *, session_id, conversation, now, alive=os.kill, start_of=procs.start_time):
    """The newest heartbeat of session_id that is fresh (within HEARTBEAT_S), whose process is alive with the recorded
    start time, that serves `conversation` (what the registry sees now) and comes from sampan 0.17 or later; else None."""
    try:
        rows = conn.execute("SELECT * FROM inboxes WHERE session_id = ? ORDER BY at DESC", (session_id,)).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        try:
            age = (now - parse_iso(row["at"])).total_seconds()
        except (TypeError, ValueError):
            continue
        if age < 0 or age > HEARTBEAT_S or row["conversation"] != conversation or not _version_ok(row["version"]):
            continue
        if not _pid_alive(alive, row["pid"]):
            continue
        if not row["started"] or start_of(row["pid"]) != row["started"]:
            continue
        return row
    return None
