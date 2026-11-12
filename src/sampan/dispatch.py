"""Dispatch records: one message sent from a caller session (or the terminal) to a target session.

They live in the messages table of dispatches.db (see store.py), guarded by dispatches.lock, plus outbox/<id>.txt for the
text to deliver. Delivering, reading the reply and advancing the state machine live elsewhere and do it inside `transaction`.
"""
import fcntl
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .clock import parse_iso
from .fsutil import PRIVATE_FILE, atomic_write, private_dir, try_chmod

STATES = ("queued", "sent", "done", "failed", "cancelled")
KINDS = ("terminal", "unregistered", "session")  # who sent it; only "session" may be matched by from_name
TERMINAL_NAME = "終端機"
FINISHED = ("done", "failed", "cancelled")
DB_VERSION_STUB = 3  # the "version" of the stub dispatches.json a 0.17 sampan leaves behind (moved_to dispatches.db)
OLD_VERSIONS = (1, 2)  # a dispatches.json of these versions still holds the data: `sampan migrate` moves it to the db
KEEP_DAYS = 90  # default of [dispatches] keep_days: finished messages older than this lose their bodies
PURGE_EVERY = timedelta(hours=1)
# what `status` is when a Dispatch leaves it empty: derived from state
STATUS_OF_STATE = {"queued": "queued", "sent": "pasted", "done": "replied", "failed": "failed", "cancelled": "cancelled"}


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
    startup: bool = False  # the startup prompt of `sampan new --message`: the session may not be registered yet
    raw: bool = False  # a direct send: text is the bare message (no source line), a slash command for the target itself
    batch: str | None = None  # id of the batch send this dispatch belongs to
    batch_skipped: int = 0  # on the first item of a batch only: how many sessions the batch skipped
    to_id: str = ""  # registry id of the target: how it is found again after a rename ("" = an old dispatch: by name)
    from_id: str = ""  # registry id of the sending session ("" = not a session, or an old dispatch)
    machine: str = ""  # id of the machine that created it
    status: str = ""  # finer than state (queued, rung, taken, partial, pasted, replied, unanswered, failed, cancelled); "" = derived from state
    in_reply_to: str | None = None  # reserved: the message this one answers
    thread: str | None = None  # reserved: the conversation thread this one belongs to
    # keys of the JSON object this sampan does not know (written by a newer sampan); written back as they were
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self):
        if self.from_kind is None:
            self.from_kind = "terminal" if self.from_name == TERMINAL_NAME else "session"


def new_id(existing):
    """Four lowercase hex digits not in existing. Inside a transaction pass `items.ids`: it also holds the ids of
    messages whose text was purged."""
    while True:
        i = secrets.token_hex(2)
        if i not in existing:
            return i


COLUMNS = tuple(f.name for f in fields(Dispatch))  # the last one, extra, is stored as JSON
BOOLS = frozenset(("raw", "notify", "notified", "startup"))
OPTIONAL = frozenset(("sent_at", "done_at", "reply", "thread_id", "from_kind", "batch", "in_reply_to", "thread"))
STATUS_AT = COLUMNS.index("status")


def _row(d, snap_state=None, snap_status=None):
    """The values of one Dispatch in COLUMNS order, then text_len and reply_len. A status the caller left alone while
    the state moved (snap_*: how it was loaded) is derived again from the new state."""
    values = []
    for name in COLUMNS:
        v = getattr(d, name)
        if name == "status" and (not v or (v == snap_status and d.state != snap_state)):
            v = STATUS_OF_STATE.get(d.state, d.state)
        elif name == "extra":
            v = json.dumps(v, ensure_ascii=False, sort_keys=True) if v else None
        elif name in BOOLS:
            v = int(bool(v))
        values.append(v)
    return tuple(values) + (len(d.text) if d.text is not None else None, len(d.reply) if d.reply is not None else None)


def _from_row(row):
    kw = {}
    for name in COLUMNS:
        v = row[name]
        if name == "extra":
            v = json.loads(v) if v else {}
        elif name in BOOLS or name == "batch_skipped":
            v = int(v or 0) if name == "batch_skipped" else bool(v)
        elif v is None and name not in OPTIONAL:
            v = ""
        kw[name] = v
    if kw["status"] == STATUS_OF_STATE.get(kw["state"]):
        kw["status"] = ""  # no finer than the state says: stays derived, so a later change of state cannot leave it stale
    return Dispatch(**kw)


class Items(list):
    """The dispatches of one transaction, plus what it knows: `conn` is the open sqlite connection (inside the lock;
    what a caller writes through it commits or rolls back with the dispatches) and `ids` every message id of the table,
    purged ones included."""

    def __init__(self, conn=None, rows=(), taken=()):
        super().__init__(rows)
        self.conn = conn
        self.taken = set(taken)
        self.snapshot = {d.id: (_row(d), d.state, d.status) for d in rows}  # id -> as loaded or last written

    @property
    def ids(self):
        return self.taken | {d.id for d in self}

    def flush(self):
        """Upsert every dispatch that is new or changed since it was loaded (or last flushed); no commit."""
        names = ", ".join(COLUMNS + ("text_len", "reply_len"))
        sets = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS[1:] + ("text_len", "reply_len"))
        sql = (f"INSERT INTO messages ({names}) VALUES ({', '.join('?' * (len(COLUMNS) + 2))}) "
               f"ON CONFLICT(id) DO UPDATE SET {sets}")
        for d in self:
            old = self.snapshot.get(d.id)
            row = _row(d, old[1], old[2]) if old else _row(d)
            if old is not None and old[0] == row:
                continue
            self.conn.execute(sql, row)
            self.snapshot[d.id] = (row, d.state, row[STATUS_AT])


def _check(d):
    if d.state not in STATES:
        raise DispatchError(f"{store.DB_FILE}：派工 {d.id} 的 state 是 {d.state!r}，只能是 {STATES}")
    if d.from_kind not in KINDS:
        raise DispatchError(f"{store.DB_FILE}：派工 {d.id} 的 from_kind 是 {d.from_kind!r}，只能是 {KINDS}")


def _old_file_problem(state_dir):
    """Why a state dir without a db is not a fresh install: a dispatches.json is there. None when it is not."""
    path = Path(state_dir) / "dispatches.json"
    if not path.exists():
        return None
    try:
        version = json.loads(path.read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return f"{path} 無法解析（{e}）。不會覆蓋它。"
    if version in OLD_VERSIONS:
        return "派工資料還沒升級到 0.17（sampan migrate）"
    if version == DB_VERSION_STUB:
        return f"找不到 {Path(state_dir) / store.DB_FILE}；派工資料已搬進資料庫，沒有資料庫就無法繼續"
    return f"{path} 是未知的版本 {version!r}（只認得 {'、'.join(map(str, OLD_VERSIONS + (DB_VERSION_STUB,)))}）。不會覆蓋它。"


def _open(state_dir, *, fresh_ok):
    """The connection to the db. With no db yet and no dispatches.json either (a fresh install) it is created when
    fresh_ok, else None."""
    exists = (Path(state_dir) / store.DB_FILE).exists()
    problem = None if exists else _old_file_problem(state_dir)
    if problem:
        raise DispatchError(problem)
    if not exists and not fresh_ok:
        return None
    try:
        return store.connect(state_dir, create=not exists)
    except store.StoreError as e:
        raise DispatchError(str(e)) from e


def _read(conn):
    """The unpurged messages by created, and every message id."""
    items = [_from_row(r) for r in conn.execute("SELECT * FROM messages WHERE purged=0 ORDER BY created, rowid")]
    for d in items:
        _check(d)
    return items, {r["id"] for r in conn.execute("SELECT id FROM messages")}


def load(state_dir):
    """Every message whose text is still kept, by created. [] when nothing was ever stored."""
    conn = _open(state_dir, fresh_ok=False)
    if conn is None:
        return []
    try:
        return _read(conn)[0]
    except sqlite3.Error as e:
        raise DispatchError(f"{Path(state_dir) / store.DB_FILE} 讀取失敗（{e}）。不會覆蓋它。") from e
    finally:
        conn.close()


@contextmanager
def _sqlite_errors(state_dir):
    """A sqlite error (locked, corrupt, schema) is a DispatchError, which the callers report instead of crashing."""
    try:
        yield
    except sqlite3.Error as e:
        raise DispatchError(f"{Path(state_dir) / store.DB_FILE} 無法使用（{e}）。不會覆蓋它。") from e


def checkpoint(state_dir, items):
    """Commit items now, in the middle of a transaction (the caller holds the lock): a state that must survive a crash
    of this process before the transaction ends ("being sent" before any key is pressed)."""
    with _sqlite_errors(state_dir):
        items.flush()
        items.conn.commit()


def purge(conn, now, keep_days):
    """Clear text, reply and extra of finished messages whose done_at (created when there is none) is more than
    keep_days before now; their rows, ids and statistics stay (purged=1). The delivery and reply-part texts of those
    messages go too. Returns the ids purged. No commit."""
    limit = now - timedelta(days=keep_days)
    old = []
    for r in conn.execute("SELECT id, done_at, created FROM messages WHERE purged=0 AND state IN (?,?,?)", FINISHED):
        when = parse_iso(r["done_at"] or r["created"])
        if when is not None and when < limit:
            old.append(r["id"])
    for did in old:
        conn.execute("UPDATE messages SET text=NULL, reply=NULL, extra=NULL, purged=1 WHERE id=?", (did,))
        conn.execute("UPDATE deliveries SET text=NULL WHERE id IN (SELECT delivery FROM delivery_messages WHERE message=?)", (did,))
        conn.execute("UPDATE reply_parts SET text=NULL WHERE message=?", (did,))
    return old


def _purge_if_due(conn, state_dir, keep_days):
    now = datetime.now(timezone.utc)
    row = conn.execute("SELECT value FROM meta WHERE key='purged_at'").fetchone()
    last = parse_iso(row["value"]) if row else None
    if last is not None and now - last < PURGE_EVERY:
        return
    gone = purge(conn, now, keep_days)
    names = [n for did in gone for n in (did, f"{did}-notice")]  # the outbox of the message and of its failure notice
    names += sorted({r["delivery"] for did in gone  # and of a group paste it was in
                     for r in conn.execute("SELECT delivery FROM delivery_messages WHERE message=?", (did,))})
    conn.execute("INSERT INTO meta (key, value) VALUES ('purged_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (now.isoformat(timespec="seconds"),))
    conn.commit()
    for name in names:
        try:
            outbox_path(state_dir, name).unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def transaction(state_dir, *, wait_s=None, keep_days=KEEP_DAYS):
    """Hold the lock across load, caller changes and write-back; yields the mutable list (an `Items`, with the open
    connection as .conn). Changed and new dispatches are upserted on a clean exit; one removed from the list is not
    deleted. An exception rolls back and writes nothing (but for what a checkpoint committed).
    wait_s None blocks until the lock is free; a number tries for at most that many seconds, then raises DispatchError
    (for callers that must not stall, like the curses ui). At most once an hour, finished messages older than keep_days
    days lose their bodies (purge)."""
    state_dir = private_dir(state_dir)
    with os.fdopen(os.open(state_dir / "dispatches.lock", os.O_WRONLY | os.O_CREAT, PRIVATE_FILE), "w") as fh:
        try_chmod(fh.fileno(), PRIVATE_FILE)
        if wait_s is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            end = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= end:
                        raise DispatchError("正在送出，稍後再試") from None
                    time.sleep(0.02)
        conn = None
        try:
            with _sqlite_errors(state_dir):
                conn = _open(state_dir, fresh_ok=True)
                _purge_if_due(conn, state_dir, keep_days)
                rows, taken = _read(conn)
            items = Items(conn, rows, taken)
            yield items  # whatever the caller's block raises passes through as itself
            with _sqlite_errors(state_dir):
                items.flush()
                conn.commit()
        except BaseException:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if conn is not None:
                conn.close()
            fcntl.flock(fh, fcntl.LOCK_UN)


def outbox_path(state_dir, did):
    return Path(state_dir) / "outbox" / f"{did}.txt"


def write_outbox(state_dir, did, text):
    private_dir(Path(state_dir) / "outbox")
    path = outbox_path(state_dir, did)
    atomic_write(path, text, mode=PRIVATE_FILE)
    return path
