"""The sqlite store behind dispatches: messages, deliveries, replies, rings and inbox heartbeats (state_dir/dispatches.db).

This module only opens the database, creates the schema and hands out ids. Processes still serialise their writes with
dispatches.lock; sqlite's own locking (WAL, busy_timeout) only covers the short gaps in between.
"""
import os
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .fsutil import PRIVATE_FILE, private_dir, try_chmod

DB_FILE = "dispatches.db"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE messages (
    id TEXT PRIMARY KEY, from_name TEXT, from_profile TEXT, from_agent TEXT, from_kind TEXT, from_id TEXT,
    to_name TEXT, to_id TEXT, to_agent TEXT, to_profile TEXT, text TEXT, raw INT, created TEXT, notify INT,
    notified INT, startup INT, batch TEXT, batch_skipped INT, machine TEXT, extra TEXT, state TEXT, status TEXT,
    reason TEXT, sent_at TEXT, done_at TEXT, reply TEXT, thread_id TEXT, in_reply_to TEXT, thread TEXT,
    text_len INT, reply_len INT, purged INT DEFAULT 0
);
CREATE INDEX messages_state ON messages(state);
CREATE INDEX messages_to_state ON messages(to_id, state, created);
CREATE INDEX messages_from ON messages(from_id, created);
CREATE INDEX messages_created ON messages(created);
CREATE TABLE deliveries (
    id TEXT PRIMARY KEY, kind TEXT, to_id TEXT, to_name TEXT, text TEXT, conversation TEXT, cursor INT, created TEXT,
    sent_at TEXT, state TEXT, reason TEXT, ended_at TEXT, archived INT DEFAULT 0
);
CREATE UNIQUE INDEX one_active ON deliveries(to_id) WHERE state='active';
CREATE TABLE delivery_messages (
    delivery TEXT, message TEXT, position INT, archived INT DEFAULT 0, PRIMARY KEY(delivery, message)
);
CREATE TABLE rings (id TEXT PRIMARY KEY, to_id TEXT, conversation TEXT, messages TEXT, at TEXT, result TEXT, delivery TEXT);
CREATE TABLE replies (id TEXT PRIMARY KEY, delivery TEXT UNIQUE, first_at TEXT, source TEXT);
CREATE TABLE reply_parts (
    id TEXT PRIMARY KEY, reply TEXT, message TEXT, text TEXT, how TEXT, at TEXT, text_len INT, late INT DEFAULT 0
);
CREATE TABLE inboxes (
    session_id TEXT, nonce TEXT PRIMARY KEY, agent TEXT, conversation TEXT, pid INT, started TEXT, version TEXT, at TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


class StoreError(Exception):
    pass


def connect(state_dir, *, create=False):
    """Open state_dir/dispatches.db. Without create a missing file is an error (never a silent empty db); create=True
    (migration only) makes the file and its schema. Rows come back as sqlite3.Row; the caller closes the connection."""
    path = Path(state_dir) / DB_FILE
    if not create and not path.exists():
        raise StoreError(f"找不到 {path}；派工資料還沒升級或已降版（sampan migrate）")
    if create:
        private_dir(state_dir)
    old_umask = os.umask(0o077)  # the db, its -wal and -shm are created while this is set: owner only
    try:
        try:
            # quoted: a #, ? or % in the path would otherwise end or change the file name in the URI
            conn = sqlite3.connect(f"file:{quote(str(path))}?mode={'rwc' if create else 'rw'}", uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            raise StoreError(f"打不開 {path}：{exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            if create and conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                create_schema(conn)
        except sqlite3.Error as exc:
            conn.close()
            raise StoreError(f"打不開 {path}：{exc}") from exc
    finally:
        os.umask(old_umask)
    try_chmod(path, PRIVATE_FILE)  # a db an older run left with looser bits
    return conn


def create_schema(conn):
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


ID_TRIES = 1000  # random ids tried before giving up: the space is (nearly) full
ID_NAMES = {"messages": "派工", "reply_parts": "回覆段落", "rings": "門鈴", "deliveries": "送達"}
IDS_EXHAUSTED = "{what} id 用完了（試了 {n} 個都已被占用，已清除本文的也占著 id）；之後的 0.17.x 會加長 id"


def _fresh_id(conn, table, make):
    """An id from make() that no row of table has (also rows whose text was purged keep their id). StoreError after
    ID_TRIES taken ones."""
    for _ in range(ID_TRIES):
        candidate = make()
        if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (candidate,)).fetchone() is None:
            return candidate
    raise StoreError(IDS_EXHAUSTED.format(what=ID_NAMES.get(table, table), n=ID_TRIES))


def new_message_id(conn):
    return _fresh_id(conn, "messages", lambda: secrets.token_hex(2))


def new_part_id(conn):
    return _fresh_id(conn, "reply_parts", lambda: "p" + secrets.token_hex(4))


def new_ring_id(conn):
    return _fresh_id(conn, "rings", lambda: secrets.token_hex(2))


def new_delivery_id(conn):
    return _fresh_id(conn, "deliveries", lambda: "d" + secrets.token_hex(3))


def mnt_problem(state_dir):
    """Why state_dir cannot hold the db (sqlite locking is unreliable on a Windows drive mounted under WSL), or None."""
    real = os.path.realpath(state_dir)
    if real == "/mnt" or real.startswith("/mnt/"):
        return f"{real} 在 /mnt 底下（Windows 磁碟），sqlite 的鎖在那裡不可靠；請把 state 目錄放在 Linux 檔案系統上"
    return None
