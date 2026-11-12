"""Deliveries and reply parts in dispatches.db: one time messages were put in front of a target, and the parts of the
answer each message got.

Every function takes the connection of an open dispatch.transaction (`items.conn`): the caller holds dispatches.lock,
and what is written here commits or rolls back with the dispatches. No commit here.
"""
import re
import sqlite3
from datetime import timezone

from . import store

ACTIVE, ENDED = "active", "ended"
KINDS = ("inbox", "paste", "raw")
HOWS = ("tool", "marker", "whole", "fill")
MARKER = re.compile(r"^[ \t]*〔回覆 ([^〕\s]+)〕[ \t]*", re.MULTILINE)  # 〔回覆 <派工 id>〕 at the start of a line


class ActiveDelivery(Exception):
    """The target already has an active delivery (one at a time per target)."""


def open_delivery(conn, *, kind, to_id, to_name, messages, text, conversation, cursor, now):
    """Record an active delivery of messages (ids, in the order they were put together) to to_id. Its id; raises
    ActiveDelivery when to_id already has one."""
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r}")
    did = store.new_delivery_id(conn)
    at = _iso(now)
    try:
        conn.execute("INSERT INTO deliveries (id, kind, to_id, to_name, text, conversation, cursor, created, sent_at, "
                     "state, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
                     (did, kind, to_id, to_name, text, conversation, cursor, at, at, ACTIVE))
    except sqlite3.IntegrityError as e:
        raise ActiveDelivery(to_id) from e
    conn.executemany("INSERT INTO delivery_messages (delivery, message, position) VALUES (?, ?, ?)",
                     [(did, m, i) for i, m in enumerate(messages)])
    return did


def drop_delivery(conn, delivery_id):
    """Forget a delivery that never reached the target (nothing was pasted or queued)."""
    conn.execute("DELETE FROM delivery_messages WHERE delivery=?", (delivery_id,))
    conn.execute("DELETE FROM deliveries WHERE id=?", (delivery_id,))


def active_delivery(conn, to_id):
    return conn.execute("SELECT * FROM deliveries WHERE to_id=? AND state=?", (to_id, ACTIVE)).fetchone()


def delivery_of(conn, message):
    """The latest delivery message is (still) part of, or None (never delivered, or a startup prompt)."""
    return conn.execute("SELECT d.* FROM deliveries d JOIN delivery_messages dm ON dm.delivery=d.id "
                        "WHERE dm.message=? AND dm.archived=0 ORDER BY d.rowid DESC LIMIT 1", (message,)).fetchone()


def members(conn, delivery_id, *, archived=False):
    """The message ids of a delivery by position; those whose link was archived (cancelled) only with archived=True."""
    sql = "SELECT message FROM delivery_messages WHERE delivery=?" + ("" if archived else " AND archived=0")
    return [r["message"] for r in conn.execute(sql + " ORDER BY position", (delivery_id,))]


def end_delivery(conn, delivery_id, now):
    conn.execute("UPDATE deliveries SET state=?, ended_at=? WHERE id=? AND state=?",
                 (ENDED, _iso(now), delivery_id, ACTIVE))


def archive_member(conn, delivery_id, message):
    """message no longer waits for this delivery (cancelled); the others of it are not affected."""
    conn.execute("UPDATE delivery_messages SET archived=1 WHERE delivery=? AND message=?", (delivery_id, message))


def add_part(conn, *, delivery_id, message, text, how, now, late=False):
    """Record a part of the reply to message within delivery_id (its replies row is made with the first part). Unless
    late, the message's reply / reply_len become this part (the latest). The part id."""
    if how not in HOWS:
        raise ValueError(f"how {how!r}")
    at = _iso(now)
    row = conn.execute("SELECT id FROM replies WHERE delivery=?", (delivery_id,)).fetchone()
    if row is None:
        reply_id = "r" + delivery_id[1:]  # one reply per delivery: its id follows the delivery's (as migrate does)
        conn.execute("INSERT INTO replies (id, delivery, first_at, source) VALUES (?, ?, ?, ?)",
                     (reply_id, delivery_id, at, "tool" if how == "tool" else "transcript"))
    else:
        reply_id = row["id"]
    pid = store.new_part_id(conn)
    conn.execute("INSERT INTO reply_parts (id, reply, message, text, how, at, text_len, late) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (pid, reply_id, message, text, how, at, len(text) if text is not None else None, int(bool(late))))
    if not late:
        conn.execute("UPDATE messages SET reply=?, reply_len=? WHERE id=?",
                     (text, len(text) if text is not None else None, message))
    return pid


def parts(conn, message):
    """Every reply part of message, oldest first: id, how, text (None once purged), at, late, latest."""
    rows = conn.execute("SELECT id, how, text, at, late FROM reply_parts WHERE message=? ORDER BY at, id", (message,))
    out = [{"id": r["id"], "how": r["how"], "text": r["text"], "at": r["at"], "late": bool(r["late"])} for r in rows]
    latest = latest_part(conn, message)
    for p in out:
        p["latest"] = latest is not None and p["id"] == latest["id"]
    return out


def latest_part(conn, message):
    """The part that stands for message's reply: the newest at that is not late, the larger id on the same at. None
    when it has none."""
    r = conn.execute("SELECT id, how, text, at, late FROM reply_parts WHERE message=? AND late=0 ORDER BY at DESC, id DESC "
                     "LIMIT 1", (message,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "how": r["how"], "text": r["text"], "at": r["at"], "late": bool(r["late"])}


def split_markers(reply_text, ids):
    """The sections of reply_text that start with 〔回覆 <id>〕 at the start of a line, for the ids given (a marker in
    the middle of a line, or with an id not in ids, is plain text). {id: section}, only for ids that have a non-empty
    section; an id marked twice gets both sections. The caller gives the others the whole reply."""
    wanted = set(ids)
    found = [m for m in MARKER.finditer(reply_text or "") if m.group(1) in wanted]
    out = {}
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(reply_text)
        section = reply_text[m.end():end].strip()
        if section:
            out[m.group(1)] = f"{out[m.group(1)]}\n\n{section}" if m.group(1) in out else section
    return out


def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")
