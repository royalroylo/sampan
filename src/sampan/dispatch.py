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
MIGRATE_JOURNAL = "migrate-0.17.json"  # an unfinished migration to 0.17 (migrate_017)
REVERT_MARK = "migrate-0.17.reverted"  # the dispatches were moved back to 0.16 (`sampan migrate --revert`)
NOT_MIGRATED = "派工資料還沒升級到 0.17（sampan migrate）"
REVERTED_NOTE = "已降回 0.16 格式；要再升級請跑 sampan migrate"
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
    messages whose text was purged. After store.ID_TRIES random ones that are all taken, one of the free ones;
    DispatchError when there is none."""
    for _ in range(store.ID_TRIES):
        i = secrets.token_hex(2)
        if i not in existing:
            return i
    free = sorted({f"{n:04x}" for n in range(0x10000)} - set(existing))
    if not free:
        raise DispatchError(store.IDS_EXHAUSTED.format(what="派工", n=0x10000))
    return secrets.choice(free)


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
    purged and unloaded ones included. A transaction loads only the live messages (see _read); a row it did not load
    is never written."""

    def __init__(self, conn=None, rows=(), taken=()):
        super().__init__(rows)
        self.conn = conn
        self.taken = set(taken)
        self.snapshot = {d.id: (_row(d), d.state, d.status) for d in rows}  # id -> as loaded or last written

    @property
    def ids(self):
        return self.taken | {d.id for d in self}

    def need(self, ids):
        """Load the unpurged messages of ids (message or batch ids) that this transaction has not loaded yet, with
        their batch mates, at the end of the list. The dispatches of ids that are loaded now."""
        ids = set(ids)
        have = {d.id for d in self}
        more = [d for d in _query(self.conn, *_named(ids)) if d.id not in have]
        self.extend(more)
        self.snapshot.update({d.id: (_row(d), d.state, d.status) for d in more})
        return [d for d in self if d.id in ids or d.batch in ids]

    def flush(self):
        """Write every dispatch that is new or changed since it was loaded (or last flushed); no commit. Only loaded
        rows are updated; a new one is inserted, and one whose id is already in the table (a row this transaction did
        not load) fails the transaction instead of overwriting it."""
        names = ", ".join(COLUMNS + ("text_len", "reply_len"))
        insert = f"INSERT INTO messages ({names}) VALUES ({', '.join('?' * (len(COLUMNS) + 2))})"
        update = f"UPDATE messages SET {', '.join(f'{c}=?' for c in COLUMNS[1:] + ('text_len', 'reply_len'))} WHERE id=?"
        for d in self:
            old = self.snapshot.get(d.id)
            row = _row(d, old[1], old[2]) if old else _row(d)
            if old is not None and old[0] == row:
                continue
            if old is None:
                self.conn.execute(insert, row)
            else:
                self.conn.execute(update, row[1:] + row[:1])
            self.snapshot[d.id] = (row, d.state, row[STATUS_AT])


def _check(d):
    if d.state not in STATES:
        raise DispatchError(f"{store.DB_FILE}：派工 {d.id} 的 state 是 {d.state!r}，只能是 {STATES}")
    if d.from_kind not in KINDS:
        raise DispatchError(f"{store.DB_FILE}：派工 {d.id} 的 from_kind 是 {d.from_kind!r}，只能是 {KINDS}")


def _old_file_problem(state_dir, db_exists):
    """Why the db must not be opened (or, with no db, why this is not a fresh install), None when it may: a 0.16
    revert mark or an unfinished 0.17 migration, a dispatches.json that still holds the data (version 1 or 2), or,
    with no db, any other dispatches.json."""
    state_dir = Path(state_dir)
    if (state_dir / REVERT_MARK).exists():
        return REVERTED_NOTE
    if (state_dir / MIGRATE_JOURNAL).exists():
        return NOT_MIGRATED
    path = state_dir / "dispatches.json"
    if not path.exists():
        return None
    try:
        version = json.loads(path.read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return None if db_exists else f"{path} 無法解析（{e}）。不會覆蓋它。"
    if version in OLD_VERSIONS:
        return NOT_MIGRATED
    if db_exists:
        return None
    if version == DB_VERSION_STUB:
        return f"找不到 {state_dir / store.DB_FILE}；派工資料已搬進資料庫，沒有資料庫就無法繼續"
    return f"{path} 是未知的版本 {version!r}（只認得 {'、'.join(map(str, OLD_VERSIONS + (DB_VERSION_STUB,)))}）。不會覆蓋它。"


def _open(state_dir, *, fresh_ok):
    """The connection to the db. With no db yet and no dispatches.json either (a fresh install) it is created when
    fresh_ok, else None. DispatchError, db or not, while the data is reverted to 0.16 or not migrated to 0.17 yet: a
    heartbeat or a send then never creates or writes a db beside the data 0.16 uses."""
    exists = (Path(state_dir) / store.DB_FILE).exists()
    problem = _old_file_problem(state_dir, exists)
    if problem:
        raise DispatchError(problem)
    if not exists and not fresh_ok:
        return None
    try:
        conn = store.connect(state_dir, create=not exists)
    except store.StoreError as e:
        raise DispatchError(str(e)) from e
    if not fresh_ok and conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.close()  # being created right now by another process (the file is there before its schema): nothing yet
        return None
    return conn


# a message still moving (queued, sent) or whose caller is still owed its notice (see send._owes_notice: a plain one
# done or failed; a batch item, whose batch's first item owes the one batch notice)
OWED = "(notify=1 AND notified=0 AND (batch IS NOT NULL OR state IN ('done','failed')))"
LIVE = f"(state IN ('queued','sent') OR {OWED})"


def _named(ids):
    """The SQL condition for the messages named in ids (message or batch ids), and its arguments."""
    ids = sorted(ids)
    if not ids:
        return "0", ()
    marks = ", ".join("?" * len(ids))
    return f"(id IN ({marks}) OR batch IN ({marks}))", tuple(ids) * 2


def _query(conn, where, args):
    """The unpurged messages matching where, plus every unpurged batch mate of them, by created."""
    sql = (f"SELECT * FROM messages WHERE purged=0 AND ({where} OR batch IN "
           f"(SELECT batch FROM messages WHERE batch IS NOT NULL AND {where})) ORDER BY created, rowid")
    items = [_from_row(r) for r in conn.execute(sql, args * 2)]
    for d in items:
        _check(d)
    return items


def _read(conn, ids=None):
    """The unpurged messages by created, and every message id (purged ones included). ids None: every message;
    else only the live ones (LIVE), the ones named in ids (message or batch ids), and the batch mates of those."""
    if ids is None:
        items = _query(conn, "1", ())
    else:
        named, args = _named(ids)
        items = _query(conn, f"({LIVE} OR {named})", args)
    return items, {r["id"] for r in conn.execute("SELECT id FROM messages")}


def load(state_dir, ids=None):
    """The messages whose text is still kept, by created: every one (ids None), or only the live ones plus the ones
    named in ids (see _read). [] when nothing was ever stored."""
    conn = _open(state_dir, fresh_ok=False)
    if conn is None:
        return []
    try:
        return _read(conn, ids)[0]
    except sqlite3.Error as e:
        raise DispatchError(f"{Path(state_dir) / store.DB_FILE} 讀取失敗（{e}）。不會覆蓋它。") from e
    finally:
        conn.close()


@contextmanager
def _sqlite_errors(state_dir):
    """A sqlite error (locked, corrupt, schema) or a StoreError is a DispatchError, which the callers report instead
    of crashing."""
    try:
        yield
    except sqlite3.Error as e:
        raise DispatchError(f"{Path(state_dir) / store.DB_FILE} 無法使用（{e}）。不會覆蓋它。") from e
    except store.StoreError as e:  # e.g. the ids of a table are used up
        raise DispatchError(str(e)) from e


def checkpoint(state_dir, items):
    """Commit items now, in the middle of a transaction (the caller holds the lock): a state that must survive a crash
    of this process before the transaction ends ("being sent" before any key is pressed)."""
    with _sqlite_errors(state_dir):
        items.flush()
        items.conn.commit()


def purge(conn, now, keep_days):
    """Clear text, reply and extra of finished messages whose done_at (created when there is none) is more than
    keep_days before now; their rows, ids and statistics stay (purged=1). Never one whose caller is still owed its
    notice, nor an item of a batch that is not finished yet (the batch notice counts every item). The delivery and
    reply-part texts of those messages go too. Returns the ids purged. No commit."""
    limit = now - timedelta(days=keep_days)
    old = []
    for r in conn.execute(f"SELECT id, done_at, created FROM messages AS m WHERE purged=0 AND state IN (?,?,?) "
                          f"AND NOT {OWED} AND (batch IS NULL OR NOT EXISTS (SELECT 1 FROM messages WHERE "
                          f"batch=m.batch AND state IN ('queued','sent')))", FINISHED):
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
    names += _old_rings(conn, now, keep_days)
    conn.execute("INSERT INTO meta (key, value) VALUES ('purged_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (now.isoformat(timespec="seconds"),))
    conn.commit()
    for name in names:
        try:
            outbox_path(state_dir, name).unlink(missing_ok=True)
        except OSError:
            pass


def _old_rings(conn, now, keep_days):
    """The outbox names (ring-<code>) of the rings rung more than keep_days before now."""
    limit = now - timedelta(days=keep_days)
    return [f"ring-{r['id']}" for r in conn.execute("SELECT id, at FROM rings")
            if (at := parse_iso(r["at"])) is not None and at < limit]


@contextmanager
def transaction(state_dir, *, wait_s=None, keep_days=KEEP_DAYS, ids=()):
    """Hold the lock across load, caller changes and write-back; yields the mutable list (an `Items`, with the open
    connection as .conn). It holds the live messages (queued, sent, notice owed), the ones named in ids (message or
    batch ids: one the caller looks up, finished or not) and their batch mates; Items.need loads more. Changed and new
    dispatches are written on a clean exit; one removed from the list is not deleted. An exception rolls back and writes nothing (but for what a checkpoint committed).
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
                rows, taken = _read(conn, ids)
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
