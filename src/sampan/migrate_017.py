"""Moving the dispatches from dispatches.json (version 1 or 2, sampan 0.16 and older) into dispatches.db, and back.

`upgrade` runs under dispatches.lock and writes a journal (state/migrate-0.17.json) as it goes:
  1. take the lock;
  2. copy dispatches.json into state/backup-0.17-<stamp>/ (journal: backed_up);
  3. write dispatches.db.tmp, every old dispatch mapped (see `map_old`), and read every one back (db_written);
  4. rename it to dispatches.db (db_ready);
  5. re-read dispatches.json: if a 0.16 changed it meanwhile, drop the db and start again from 2 (at most 3 times);
     otherwise put the version-3 stub in its place with one atomic rename (stub_written);
  6. delete the journal.
Every step can be redone, so a run cut short anywhere is finished by the next one: what is on disk says where it
stopped (a db.tmp is always rebuilt, a db is checked against dispatches.json and kept or rebuilt, a stub only needs the
journal removed). dispatches.json is at every moment either the complete original or the complete stub: 0.16 keeps
using the first and refuses the second. A failure (not a kill) rolls back to the original and raises MigrateError.

`revert` (`sampan migrate --revert`) writes the messages that still have their text back into a version-2
dispatches.json (only when the db is the live copy: a version 1/2 dispatches.json is kept as it is), renames the db to dispatches.db.reverted-<stamp> and leaves a mark (state/migrate-0.17.reverted): while
it is there nothing migrates them again and the commands that use dispatches refuse; `sampan migrate` removes it.
"""
import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path

from . import deliveries, dispatch, store
from .clock import now_iso
from .dispatch import Dispatch, DispatchError
from .fsutil import PRIVATE_DIR, PRIVATE_FILE, private_dir, try_chmod
from .migrate import _raw, _write_bytes

JOURNAL = dispatch.MIGRATE_JOURNAL
REVERTED = dispatch.REVERT_MARK
BACKUP_PREFIX = "backup-0.17-"
JSON_FILE = "dispatches.json"
TMP_FILE = store.DB_FILE + ".tmp"
SIDE_FILES = ("-wal", "-shm", "-journal")  # what sqlite keeps beside a db file
STUB = {"version": dispatch.DB_VERSION_STUB, "dispatches": [], "moved_to": store.DB_FILE}
TRIES = 3  # dispatches.json changed while migrating: start again from the backup at most this many times
REVERTED_NOTE = dispatch.REVERTED_NOTE
REASON_IN_FLIGHT = "降版時還在處理，請重送"  # a sent message 0.16 could never judge (a group paste, an inbox take)
REINSTALL_016 = "uv tool install --reinstall git+https://github.com/royalroylo/sampan@v0.16.1"
# the fields of 0.16's Dispatch: a key of an old record that is not one of these is kept in `extra`, as 0.16 did
OLD_FIELDS = tuple(f.name for f in fields(Dispatch) if f.name not in ("status", "in_reply_to", "thread", "extra"))
KEY_FIELDS = ("id", "state", "text", "reply", "created", "sent_at", "done_at", "reason")
STATUS_OF_OLD = {"queued": "queued", "sent": "pasted", "done": "replied", "failed": "failed", "cancelled": "cancelled"}


class MigrateError(Exception):
    pass


def _json_version(raw):
    """The "version" of dispatches.json's bytes, None when missing or unreadable."""
    if raw is None:
        return None
    try:
        v = json.loads(raw).get("version")
    except (ValueError, AttributeError):
        return None
    return v if type(v) is int else None


def reverted(cfg):
    return (Path(cfg.state_dir) / REVERTED).exists()


def pending(cfg):
    """A migration is unfinished (journal) or dispatches.json still holds the data (version 1 or 2). The revert mark
    does not count here."""
    state_dir = Path(cfg.state_dir)
    return (state_dir / JOURNAL).exists() or _json_version(_raw(state_dir / JSON_FILE)) in dispatch.OLD_VERSIONS


def needed(cfg):
    """True when the next command should migrate: not reverted, and a journal or a version 1/2 dispatches.json."""
    return not reverted(cfg) and pending(cfg)


@contextmanager
def _lock(state_dir):
    """dispatches.lock, the same lock every dispatch transaction (also 0.16's) takes."""
    with os.fdopen(os.open(Path(state_dir) / "dispatches.lock", os.O_WRONLY | os.O_CREAT, PRIVATE_FILE), "w") as fh:
        try_chmod(fh.fileno(), PRIVATE_FILE)
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- one old record -> rows

def _old_dispatch(d):
    """An old record read the way 0.16 read it. MigrateError for one 0.16 could not read either."""
    if not isinstance(d, dict):
        raise MigrateError(f"dispatches.json 裡有一筆不是物件：{d!r}")
    known = set(OLD_FIELDS) | {"extra"}
    try:
        old = Dispatch(**{k: v for k, v in d.items() if k in known and k != "extra"},
                       extra={k: v for k, v in d.items() if k not in known})
    except TypeError as e:
        raise MigrateError(f"dispatches.json 的派工 {d.get('id', '?')} 讀不懂（{e}）") from e
    if old.state not in dispatch.STATES:
        raise MigrateError(f"dispatches.json 的派工 {old.id} 的 state 是 {old.state!r}，只能是 {dispatch.STATES}")
    if old.from_kind not in dispatch.KINDS:
        raise MigrateError(f"dispatches.json 的派工 {old.id} 的 from_kind 是 {old.from_kind!r}，只能是 {dispatch.KINDS}")
    return old


def map_old(d):
    """(message row, delivery row or None, reply part row or None) of one 0.16 record, as dicts by column (ids of the
    delivery and the part are given when they are inserted). The delivery row also has link_archived: the value of
    its delivery_messages row (a cancelled message no longer waits for that delivery).
      queued                 -> queued, no delivery
      sent (also 送出中)      -> pasted, an active paste delivery with the reason as it was
      done                   -> replied, an ended paste delivery, its reply as one whole part
      failed                 -> failed, a delivery only when it was sent, a part only when it has a reply
      cancelled              -> cancelled, a delivery only when it was sent, no part
    A raw dispatch (a slash command) is delivered as kind raw instead of paste."""
    old = _old_dispatch(d)
    old.status = STATUS_OF_OLD[old.state]
    message = dict(zip(dispatch.COLUMNS + ("text_len", "reply_len"), dispatch._row(old)))
    sent = old.state in ("sent", "done") or (old.state in ("failed", "cancelled") and old.sent_at)
    if not sent:
        return message, None, None
    delivery = {"kind": "raw" if old.raw else "paste", "to_id": old.to_id, "to_name": old.to_name, "text": old.text,
                "conversation": old.thread_id, "cursor": None, "created": old.sent_at or old.created,
                "sent_at": old.sent_at, "state": "active" if old.state == "sent" else "ended", "reason": old.reason,
                "ended_at": None if old.state == "sent" else old.done_at, "archived": 0,
                "link_archived": 1 if old.state == "cancelled" else 0}
    part = None
    if old.state in ("done", "failed") and old.reply is not None:
        part = {"message": old.id, "text": old.reply, "how": "whole", "at": old.done_at or old.sent_at or old.created,
                "text_len": len(old.reply), "late": 0}
    return message, delivery, part


def _insert(conn, olds):
    """Write the rows of every old record. Of several "sent" ones to the same target only the newest keeps an active
    delivery (one active delivery per target); the others' end, the message itself stays pasted."""
    rows = [map_old(d) for d in olds]
    newest = {}
    for message, delivery, _part in rows:
        if delivery is not None and delivery["state"] == "active":
            when = delivery["sent_at"] or delivery["created"] or ""
            if delivery["to_id"] not in newest or when >= newest[delivery["to_id"]][0]:
                newest[delivery["to_id"]] = (when, message["id"])
    for message, delivery, part in rows:
        conn.execute(f"INSERT INTO messages ({', '.join(message)}) VALUES ({', '.join('?' * len(message))})",
                     tuple(message.values()))
        if delivery is None:
            continue
        delivery = dict(delivery)
        link_archived = delivery.pop("link_archived")
        if delivery["state"] == "active" and newest[delivery["to_id"]][1] != message["id"]:
            delivery["state"] = "ended"
        delivery = {"id": store.new_delivery_id(conn), **delivery}
        conn.execute(f"INSERT INTO deliveries ({', '.join(delivery)}) VALUES ({', '.join('?' * len(delivery))})",
                     tuple(delivery.values()))
        conn.execute("INSERT INTO delivery_messages (delivery, message, position, archived) VALUES (?, ?, 0, ?)",
                     (delivery["id"], message["id"], link_archived))
        if part is None:
            continue
        reply_id = "r" + delivery["id"][1:]  # one reply per delivery: its id follows the delivery's
        conn.execute("INSERT INTO replies (id, delivery, first_at, source) VALUES (?, ?, ?, 'transcript')",
                     (reply_id, delivery["id"], part["at"]))
        part = {"id": store.new_part_id(conn), "reply": reply_id, **part}
        conn.execute(f"INSERT INTO reply_parts ({', '.join(part)}) VALUES ({', '.join('?' * len(part))})",
                     tuple(part.values()))


def _same(name, a, b):
    if name not in dispatch.OPTIONAL and a in (None, "") and b in (None, ""):
        return True  # the store reads a missing text, reason… back as "", as dispatch.load always has
    return a == b


def _verify(conn, olds):
    """Every old record read back from the db: [] when all are there as they were, else the lines saying what is not."""
    try:
        items, ids = dispatch._read(conn)
    except (DispatchError, sqlite3.Error) as e:
        return [f"寫完讀不回來：{e}"]
    got = {d.id: d for d in items}
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM messages")}
    kinds = {}
    for r in conn.execute("SELECT dm.message, d.kind FROM delivery_messages dm JOIN deliveries d ON d.id=dm.delivery"):
        kinds.setdefault(r["message"], []).append(r["kind"])
    problems = []
    if len(ids) != len(olds):
        problems.append(f"資料庫有 {len(ids)} 則，dispatches.json 有 {len(olds)} 則")
    for d in olds:
        message, delivery, part = map_old(d)
        new = got.get(message["id"])
        if new is None:
            problems.append(f"派工 {message['id']}：資料庫裡沒有")
            continue
        want = dispatch._from_row(message)
        bad = [n for n in OLD_FIELDS if not _same(n, getattr(new, n), getattr(want, n))]
        bad += [n for n in KEY_FIELDS if not _same(n, getattr(new, n), d.get(n, getattr(want, n)))]
        if new.extra != want.extra:
            bad.append("extra")
        if status.get(new.id) != message["status"]:
            bad.append("status")
        if kinds.get(new.id, []) != ([delivery["kind"]] if delivery else []):
            bad.append("送達")
        latest = conn.execute("SELECT text FROM reply_parts WHERE message=? AND late=0 ORDER BY at DESC, id DESC LIMIT 1",
                              (new.id,)).fetchone()
        if (latest["text"] if latest else None) != (part["text"] if part else None):
            bad.append("回覆段落")
        if bad:
            problems.append(f"派工 {new.id}：{'、'.join(dict.fromkeys(bad))} 不一致")
    return problems


# --- the steps

def _parse(raw):
    """The records of dispatches.json's bytes (version 1 or 2), each checked by map_old."""
    try:
        data = json.loads(raw)
        records = data["dispatches"]
        if data["version"] not in dispatch.OLD_VERSIONS or not isinstance(records, list):
            raise ValueError(f"版本 {data['version']!r}")
    except (ValueError, KeyError, TypeError) as e:
        raise MigrateError(f"dispatches.json 無法解析（{e}）") from e
    for d in records:
        map_old(d)
    return records


def _backup(state_dir, raw, now):
    """A state/backup-0.17-<stamp>/ holding these bytes of dispatches.json: one that already does (a run retried), or a
    new directory (never an existing one)."""
    for d in sorted(state_dir.glob(BACKUP_PREFIX + "*"), reverse=True):
        if d.is_dir() and _raw(d / JSON_FILE) == raw:
            return d
    base = f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M%S}"
    n = 1
    while True:
        d = state_dir / (base if n == 1 else f"{base}-{n}")
        try:
            d.mkdir(mode=PRIVATE_DIR)
            break
        except FileExistsError:
            n += 1
    _write_bytes(d / JSON_FILE, raw)
    return d


def _mark(state_dir, step, backup):
    _write_bytes(state_dir / JOURNAL, (json.dumps({"step": step, "at": now_iso(), "backup": str(backup)}) + "\n").encode())


def _drop_journal(state_dir):
    (state_dir / JOURNAL).unlink(missing_ok=True)


def _remove(path):
    for p in [path] + [path.with_name(path.name + s) for s in SIDE_FILES]:
        p.unlink(missing_ok=True)


def _build(state_dir, olds):
    """Step 3: dispatches.db.tmp with every old record, read back and compared. A rollback journal, not WAL, so the
    finished file is complete by itself (nothing beside it to carry along when it is renamed)."""
    tmp = state_dir / TMP_FILE
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(str(tmp))  # a plain path opens read-write-create (mode=rwc)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=DELETE")
            store.create_schema(conn)
            _insert(conn, olds)
            conn.commit()
            problems = _verify(conn, olds)
        finally:
            conn.close()
    finally:
        os.umask(old_umask)
    try_chmod(tmp, PRIVATE_FILE)
    if problems:
        raise MigrateError("寫完核對不一致：" + "；".join(problems[:10]))


def _install(state_dir):
    """Step 4: dispatches.db.tmp becomes dispatches.db. A -wal or -shm left from an earlier db that is not there any
    more would be read as part of the new one: removed first."""
    db = state_dir / store.DB_FILE
    for s in SIDE_FILES:
        db.with_name(db.name + s).unlink(missing_ok=True)
    os.replace(state_dir / TMP_FILE, db)


def _db_problems(state_dir, olds):
    try:
        conn = store.connect(state_dir)
    except store.StoreError as e:
        return [str(e)]
    try:
        return _verify(conn, olds)
    finally:
        conn.close()


def _write_stub(state_dir):
    """Step 5: the version-3 stub in place of dispatches.json, by one atomic rename."""
    _write_bytes(state_dir / JSON_FILE, (json.dumps(STUB) + "\n").encode())


def _run(state_dir, now):
    json_path, db = state_dir / JSON_FILE, state_dir / store.DB_FILE
    version = _json_version(_raw(json_path))
    if version == dispatch.DB_VERSION_STUB and db.exists():
        if not (state_dir / JOURNAL).exists():
            return []  # another sampan finished it while we waited for the lock
        _drop_journal(state_dir)  # cut short after step 5
        return [f"已做完上次沒做完的派工遷移（{store.DB_FILE}）"]
    if version not in dispatch.OLD_VERSIONS:
        if not (state_dir / JOURNAL).exists():
            return []
        raise MigrateError(f"遷移紀錄 {JOURNAL} 還在，但 dispatches.json 不是舊格式、也沒有 {store.DB_FILE}；"
                           f"不會動它（備份在 {state_dir}/{BACKUP_PREFIX}*）")
    if db.exists() and not (state_dir / JOURNAL).exists():
        raise MigrateError(f"{store.DB_FILE} 和舊格式的 dispatches.json 同時存在、又沒有遷移紀錄，不知道哪一份比較新，"
                           "兩個都不會動。降版沒做完的話請先跑 sampan migrate --revert")
    for _attempt in range(TRIES):
        raw = _raw(json_path)
        olds = _parse(raw)
        bk = _backup(state_dir, raw, now)
        _mark(state_dir, "backed_up", bk)
        if db.exists() and _db_problems(state_dir, olds):  # cut short after step 4 with a db that does not match
            _remove(db)
        if not db.exists():
            _remove(state_dir / TMP_FILE)  # a half-written one (and its -wal, -shm, -journal) from a run cut short
            _build(state_dir, olds)
            _mark(state_dir, "db_written", bk)
            _install(state_dir)
            _mark(state_dir, "db_ready", bk)
        if _raw(json_path) != _raw(bk / JSON_FILE):  # a 0.16 wrote meanwhile: its write would be lost
            _remove(db)
            continue
        _write_stub(state_dir)
        _mark(state_dir, "stub_written", bk)
        _drop_journal(state_dir)
        return [f"已把 {len(olds)} 則派工搬進 {store.DB_FILE}（dispatches.json 的備份在 {bk}）"]
    _remove(db)
    _drop_journal(state_dir)
    raise MigrateError(f"遷移時 dispatches.json 一直被改（試了 {TRIES} 次），先停下")


def _roll_back(state_dir):
    """After a failure: back to the original dispatches.json alone. Only while it still is the original (version 1/2)
    and a journal says the db is this migration's own; the backup stays. Returns a note when the clean-up failed."""
    if not (state_dir / JOURNAL).exists() or _json_version(_raw(state_dir / JSON_FILE)) not in dispatch.OLD_VERSIONS:
        return ""
    try:
        _remove(state_dir / TMP_FILE)
        _remove(state_dir / store.DB_FILE)
        _drop_journal(state_dir)
    except OSError as e:
        return f"（清理也失敗：{e}；下一個指令會再試）"
    return ""


def upgrade(cfg, *, now):
    """Migrate (or finish a migration cut short), under dispatches.lock. Returns the lines to print ([] when there was
    nothing to do). MigrateError when it failed: dispatches.json is then the original."""
    state_dir = private_dir(cfg.state_dir)
    with _lock(state_dir):
        try:
            return _run(state_dir, now)
        except Exception as e:
            why = str(e) if isinstance(e, MigrateError) else f"{type(e).__name__}: {e}"
            raise MigrateError(why + _roll_back(state_dir)) from e


def dry_run(cfg):
    """The migration tried on a copy of dispatches.json; nothing under the state dir is written. The lines to print;
    MigrateError when it would fail."""
    state_dir = Path(cfg.state_dir)
    raw = _raw(state_dir / JSON_FILE)
    if _json_version(raw) not in dispatch.OLD_VERSIONS:
        return ["派工：上次的遷移沒做完，下一個指令會接著做完"] if (state_dir / JOURNAL).exists() else []
    olds = _parse(raw)
    counts = Counter(d["state"] for d in olds)
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copyfile(state_dir / JSON_FILE, Path(tmp) / JSON_FILE)
        upgrade(replace(cfg, state_dir=Path(tmp)), now=datetime.now())
    by_state = "、".join(f"{s} {counts[s]}" for s in dispatch.STATES if counts[s])
    return [f"派工：{len(olds)} 則（{by_state}）會搬進 {store.DB_FILE}", "  派工逐筆核對：沒有差異"]


# --- back to 0.16

def _in_flight(conn, did):
    """Whether sent message did waits in an active delivery 0.16 cannot judge: an inbox take (0.16 never pasted it)
    or a group paste (0.16 looks for the message's own text, and the target got the group's)."""
    for r in conn.execute("SELECT d.id, d.kind FROM deliveries d JOIN delivery_messages dm ON dm.delivery=d.id "
                          "WHERE dm.message=? AND dm.archived=0 AND d.state=?", (did, deliveries.ACTIVE)):
        if r["kind"] == "inbox" or conn.execute("SELECT COUNT(*) FROM delivery_messages WHERE delivery=?",
                                                (r["id"],)).fetchone()[0] > 1:
            return True
    return False


def _export(state_dir):
    """The version-2 records of every message whose text is kept: its fields as 0.16 knows them, the time of its last
    delivery and its newest reply part (else the reply the message row holds). A sent message in an active inbox
    take or group paste is written as failed (REASON_IN_FLIGHT): 0.16 would wait for a text it can never find."""
    conn = store.connect(state_dir)
    try:
        items, _ids = dispatch._read(conn)
        records = []
        for d in items:
            rec = {name: getattr(d, name) for name in OLD_FIELDS}
            if d.state == "sent" and _in_flight(conn, d.id):
                rec.update(state="failed", reason=REASON_IN_FLIGHT, done_at=now_iso())
            part = conn.execute("SELECT text FROM reply_parts WHERE message=? AND late=0 ORDER BY at DESC, id DESC "
                                "LIMIT 1", (d.id,)).fetchone()
            if part is not None and part["text"] is not None:
                rec["reply"] = part["text"]
            last = conn.execute("SELECT MAX(d.sent_at) FROM deliveries d JOIN delivery_messages dm ON dm.delivery=d.id "
                                "WHERE dm.message=?", (d.id,)).fetchone()[0]
            if last:
                rec["sent_at"] = last
            records.append({**d.extra, **rec})
        return records
    finally:
        conn.close()


def _retire_db(state_dir, now):
    """dispatches.db (with a -wal or -shm still beside it) renamed to dispatches.db.reverted-<stamp>; returns that path."""
    conn = store.connect(state_dir)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    db = state_dir / store.DB_FILE
    base = f"{store.DB_FILE}.reverted-{now:%Y%m%d-%H%M%S}"
    n = 1
    while (state_dir / (base if n == 1 else f"{base}-{n}")).exists():
        n += 1
    target = state_dir / (base if n == 1 else f"{base}-{n}")
    os.replace(db, target)
    for s in ("-wal", "-shm"):
        side = db.with_name(db.name + s)
        if side.exists():
            os.replace(side, target.with_name(target.name + s))
    return target


def revert(cfg, *, now):
    """`sampan migrate --revert`, under dispatches.lock: mark first (so nothing migrates them back), write the messages
    back into a version-2 dispatches.json, rename the db, drop what an unfinished migration left. The messages come
    from the db only when it is the live copy (dispatches.json is the stub); a dispatches.json still at version 1/2 is
    kept as it is and the db only renamed. Run again after being
    cut short, it finishes. Returns the lines to print; MigrateError when it could not (files as they were)."""
    state_dir = private_dir(cfg.state_dir)
    json_path, db = state_dir / JSON_FILE, state_dir / store.DB_FILE
    lines = []
    with _lock(state_dir):
        marker = state_dir / REVERTED
        marked_now = not marker.exists()
        before = _raw(json_path)
        if not db.exists() and _json_version(before) == dispatch.DB_VERSION_STUB:
            raise MigrateError(f"dispatches.json 是 0.17 的空殼，卻找不到 {store.DB_FILE}；"
                               f"舊資料在 {state_dir}/{BACKUP_PREFIX}* 裡")
        if marked_now:
            _write_bytes(marker, (json.dumps({"at": now_iso()}) + "\n").encode())
        if db.exists() and _json_version(before) in dispatch.OLD_VERSIONS:
            # the stub was never written (a migration cut short, or a revert cut short after its export): the v1/v2
            # is the live copy 0.16 keeps using. It wins, byte for byte; the db is only set aside.
            gone = _retire_db(state_dir, now)
            lines.append(f"dispatches.json 還是 0.16 的格式，原檔保留沒動；資料庫改名為 {gone.name}")
        elif db.exists():
            try:
                records = _export(state_dir)
                body = json.dumps({"version": 2, "dispatches": records}, ensure_ascii=False, indent=2) + "\n"
                _write_bytes(json_path, body.encode())
                if json.loads(json_path.read_text()) != json.loads(body):
                    raise MigrateError("寫回的 dispatches.json 讀回來不一致")
            except Exception as e:
                if before is not None:
                    _write_bytes(json_path, before)
                if marked_now:
                    marker.unlink(missing_ok=True)
                why = str(e) if isinstance(e, MigrateError) else f"{type(e).__name__}: {e}"
                raise MigrateError(f"沒有降版（檔案都還原了）：{why}") from e
            gone = _retire_db(state_dir, now)
            lines.append(f"已把 {len(records)} 則派工寫回 0.16 的格式（dispatches.json 版本 2）；資料庫改名為 {gone.name}")
        else:
            lines.append("沒有派工資料庫，dispatches.json 不用寫回")
        _remove(state_dir / TMP_FILE)
        _drop_journal(state_dir)
    return lines + [
        f"已加上降版標記 {REVERTED}：0.17 不會再自動升級派工資料，送訊息、派工相關的指令會停用；要再升級請跑 sampan migrate",
        "0.16 下一次寫入派工時，會照它的規則刪掉完成超過 7 天的派工（最多留 200 筆）",
        f"重裝 0.16：{REINSTALL_016}",
    ]


def unmark(cfg):
    (Path(cfg.state_dir) / REVERTED).unlink(missing_ok=True)
