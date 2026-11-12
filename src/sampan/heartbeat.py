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


def _fresh(row, now):
    try:
        age = (now - parse_iso(row["at"])).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= age <= HEARTBEAT_S


def _live(row, now, alive, start_of):
    """Whether a row is a running instance: fresh, and (when alive / start_of are given) its pid exists with the
    recorded start time."""
    if not _fresh(row, now):
        return False
    if alive is not None and not _pid_alive(alive, row["pid"]):
        return False
    return start_of is None or (bool(row["started"]) and start_of(row["pid"]) == row["started"])


def beat(conn, *, session_id, agent, conversation, nonce, pid, started, version, now, alive=None, start_of=None):
    """Record that instance `nonce` serves session_id. An instance that started before another live row of the same
    session does not overwrite it (returns False); rows of older instances, and rows that are expired, whose pid is
    dead or whose start time does not match (an instance that died without forget), are deleted."""
    others = conn.execute("SELECT * FROM inboxes WHERE session_id = ? AND nonce != ?", (session_id, nonce)).fetchall()
    for other in others:
        if not _live(other, now, alive, start_of):
            conn.execute("DELETE FROM inboxes WHERE nonce = ?", (other["nonce"],))
        elif (other["started"] or "") > (started or ""):
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
        if row["conversation"] != conversation or not _version_ok(row["version"]):
            continue
        if _live(row, now, alive, start_of) and row["started"]:
            return row
    return None
