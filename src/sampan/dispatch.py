"""Dispatch records: one message sent from a caller session (or the terminal) to a target session.

This module only stores them (dispatches.json, guarded by dispatches.lock, plus outbox/<id>.txt for the text to deliver).
Delivering, reading the reply and advancing the state machine live elsewhere and do it inside `transaction`.
"""
import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from .clock import parse_iso
from .fsutil import PRIVATE_FILE, atomic_write, private_dir, try_chmod

STATES = ("queued", "sent", "done", "failed", "cancelled")
KINDS = ("terminal", "unregistered", "session")  # who sent it; only "session" may be matched by from_name
TERMINAL_NAME = "終端機"
FINISHED = ("done", "failed", "cancelled")
VERSION = 1
KEEP_DAYS = 7
KEEP_MAX = 200


class DispatchError(Exception):
    pass


@dataclass
class Dispatch:
    id: str
    from_name: str
    from_profile: str
    from_agent: str
    to_name: str
    to_agent: str
    to_profile: str
    text: str  # the full text sent, including the source line
    state: str
    created: str  # UTC ISO
    sent_at: str | None = None
    done_at: str | None = None
    reply: str | None = None
    reason: str = ""
    notify: bool = False
    notified: bool = False
    thread_id: str | None = None
    from_kind: str | None = None  # KINDS; None (an old record without it) is inferred from from_name

    def __post_init__(self):
        if self.from_kind is None:
            self.from_kind = "terminal" if self.from_name == TERMINAL_NAME else "session"


def new_id(existing):
    """Four lowercase hex digits not in existing."""
    while True:
        i = secrets.token_hex(2)
        if i not in existing:
            return i


def _path(state_dir):
    return Path(state_dir) / "dispatches.json"


def load(state_dir):
    path = _path(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if data["version"] != VERSION:
            raise DispatchError(f"{path} 是未知的版本 {data['version']!r}（只認得 {VERSION}）。不會覆蓋它。")
        items = [Dispatch(**d) for d in data["dispatches"]]
    except DispatchError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise DispatchError(f"{path} 無法解析（{e}）。不會覆蓋它。") from e
    for d in items:
        if d.state not in STATES:
            raise DispatchError(f"{path}：派工 {d.id} 的 state 是 {d.state!r}，只能是 {STATES}")
        if d.from_kind not in KINDS:
            raise DispatchError(f"{path}：派工 {d.id} 的 from_kind 是 {d.from_kind!r}，只能是 {KINDS}")
    return items


def _write(state_dir, items):
    text = json.dumps({"version": VERSION, "dispatches": [asdict(d) for d in items]}, ensure_ascii=False, indent=2) + "\n"
    atomic_write(_path(state_dir), text, mode=PRIVATE_FILE)


@contextmanager
def transaction(state_dir):
    """Hold the lock across load, caller changes and write-back; yields the mutable list. An exception writes nothing."""
    state_dir = private_dir(state_dir)
    with os.fdopen(os.open(state_dir / "dispatches.lock", os.O_WRONLY | os.O_CREAT, PRIVATE_FILE), "w") as fh:
        try_chmod(fh.fileno(), PRIVATE_FILE)
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            items = load(state_dir)
            yield items
            _write(state_dir, items)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def prune(items, now):
    """Drop finished dispatches older than 7 days, then keep only the newest 200 (input order is kept)."""
    limit = now - timedelta(days=KEEP_DAYS)

    def when(d):
        return parse_iso(d.created)

    kept = [d for d in items if not (d.state in FINISHED and (when(d) is not None and when(d) < limit))]
    if len(kept) > KEEP_MAX:
        order = sorted(range(len(kept)), key=lambda i: (when(kept[i]) is not None, when(kept[i]) or limit), reverse=True)
        keep = set(order[:KEEP_MAX])
        kept = [d for i, d in enumerate(kept) if i in keep]
    return kept


def outbox_path(state_dir, did):
    return Path(state_dir) / "outbox" / f"{did}.txt"


def write_outbox(state_dir, did, text):
    private_dir(Path(state_dir) / "outbox")
    path = outbox_path(state_dir, did)
    atomic_write(path, text, mode=PRIVATE_FILE)
    return path
