"""The light of a running session: 執行中 / 等人 / 待看 / 閒置. Pure classification plus its small data sources.

待看 (UNSEEN) is a finished session nobody has looked at since it last produced output; it counts as 等人.
"What looking is" comes from three places, the newest of which wins: tmux's session_last_attached, the time the sampan
screen previewed the row for SEEN_DWELL_S (seen.json), and when the last dispatch to it was collected.
"""
import fcntl
import json
import os
from datetime import timezone

from .clock import parse_iso
from .fsutil import PRIVATE_FILE, atomic_write, private_dir, try_chmod
from .status import State

RUNNING, WAITING, UNSEEN, IDLE = "執行中", "等人", "待看", "閒置"
SEEN_DWELL_S = 3  # a preview shown this long counts as looked at


def light(row, *, attached, last_attached, seen, replied):
    """The row's light; None when it is not running or the agent's own status is not one we recognise."""
    if row.state != State.RUNNING:
        return None
    status = row.agent_status
    if status in ("busy", "working", "shell"):  # shell: Claude's turn is over but a background shell task runs
        return RUNNING
    if status == "waiting":
        return WAITING
    if status != "idle":
        return None
    looks = [t for t in (last_attached, seen, replied) if t is not None]
    if attached == 0 and row.last is not None and all(row.last > t for t in looks):
        return UNSEEN
    return IDLE


def is_waiting(label):
    return label in (WAITING, UNSEEN)


def _seen_path(state_dir):
    return state_dir / "seen.json"


def load_seen(state_dir):
    """{session name: when the sampan screen last saw it}; a missing or broken file is {} (nothing seen)."""
    try:
        data = json.loads(_seen_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, ts in data.items():
        dt = parse_iso(ts)
        if dt is not None:
            out[name] = dt
    return out


def mark_seen(state_dir, name, now):
    """Record that `name` was seen at `now`. Never raises: a seen mark that cannot be written is just not there."""
    try:
        private_dir(state_dir)
        with os.fdopen(os.open(state_dir / "seen.lock", os.O_WRONLY | os.O_CREAT, PRIVATE_FILE), "w") as fh:
            try_chmod(fh.fileno(), PRIVATE_FILE)
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                data = {k: v.astimezone(timezone.utc).isoformat(timespec="seconds")
                        for k, v in load_seen(state_dir).items()}
                data[name] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
                atomic_write(_seen_path(state_dir), json.dumps(data, ensure_ascii=False), mode=PRIVATE_FILE,
                             encoding="utf-8")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except (OSError, ValueError, TypeError):
        pass


def replied_at(items):
    """{target session: done_at of its latest done dispatch, from any sender}."""
    out = {}
    for d in items:
        if d.state != "done":
            continue
        at = parse_iso(d.done_at)
        if at is not None and (d.to_name not in out or at > out[d.to_name]):
            out[d.to_name] = at
    return out

