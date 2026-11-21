"""state_dir/remote.db: what `sampan connect` keeps between runs (outbox, ids it has taken, what the hub told it). A
separate file from dispatches.db on purpose: the dispatch data and its schema version are not touched by the hub work, so
upgrading, downgrading or deleting this file never costs a dispatch."""
import json
import os
import random
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

from . import hubproto as hp

DB_FILE = "remote.db"
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE outbox (
    id TEXT PRIMARY KEY, dispatch_id TEXT, envelope TEXT NOT NULL, state TEXT NOT NULL, attempts INT DEFAULT 0,
    next_try TEXT, last_error TEXT, created TEXT, expires TEXT
);
CREATE INDEX outbox_due ON outbox(state, next_try);
CREATE TABLE seen (id TEXT PRIMARY KEY, at TEXT, local_message TEXT);
CREATE TABLE peers (machine_id TEXT PRIMARY KEY, name TEXT, sessions TEXT, updated TEXT);
CREATE TABLE published (session TEXT PRIMARY KEY, alias TEXT);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
"""
BACKOFF_MAX = 300  # seconds between two tries of one envelope, at most


class RemoteStore:
    def __init__(self, path, *, clock, rng=random.random):
        self.path, self.clock, self.rng = str(path), clock, rng
        self._lock = threading.RLock()
        fresh = not os.path.exists(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
        old = os.umask(0o077)
        try:
            self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        finally:
            os.umask(old)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        if fresh or self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @classmethod
    def at(cls, state_dir, **kw):
        return cls(Path(state_dir) / DB_FILE, **kw)

    def close(self):
        with self._lock:
            self.db.close()

    def _t(self, when=None):
        return hp.iso(when or self.clock())

    # ---------- state ----------

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key, value):
        with self._lock:
            self.db.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))
            self.db.commit()

    # ---------- outbox ----------

    def outbox_add(self, env, dispatch_id=""):
        """Queue a signed envelope to be sent. Adding the same id again changes nothing."""
        with self._lock:
            now = self._t()
            self.db.execute("INSERT OR IGNORE INTO outbox(id,dispatch_id,envelope,state,attempts,next_try,created,expires) VALUES(?,?,?,?,?,?,?,?)",
                            (env["id"], dispatch_id, json.dumps(env), "pending", 0, now, now, env.get("expires")))
            self.db.commit()

    def outbox_due(self, limit=50):
        now = self._t()
        rows = self.db.execute("SELECT * FROM outbox WHERE state='pending' AND next_try<=? ORDER BY created, rowid LIMIT ?", (now, limit)).fetchall()
        return [dict(r, envelope=json.loads(r["envelope"])) for r in rows]

    def outbox_get(self, env_id):
        r = self.db.execute("SELECT * FROM outbox WHERE id=?", (env_id,)).fetchone()
        return dict(r, envelope=json.loads(r["envelope"])) if r else None

    def outbox_pending(self):
        return [dict(r, envelope=json.loads(r["envelope"])) for r in self.db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY created")]

    def outbox_sent(self, env_id):
        with self._lock:
            self.db.execute("UPDATE outbox SET state='sent', last_error=NULL WHERE id=?", (env_id,))
            self.db.commit()

    def outbox_failed(self, env_id, error):
        with self._lock:
            self.db.execute("UPDATE outbox SET state='failed', last_error=? WHERE id=?", (str(error)[:300], env_id))
            self.db.commit()

    def outbox_retry(self, env_id, error, *, floor=0):
        """Try again later: exponential backoff with jitter, at least `floor` seconds, at most BACKOFF_MAX."""
        with self._lock:
            row = self.db.execute("SELECT attempts FROM outbox WHERE id=?", (env_id,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            delay = min(BACKOFF_MAX, max(floor, 2 ** min(attempts, 10)))
            delay = delay * (0.75 + 0.5 * self.rng())
            self.db.execute("UPDATE outbox SET attempts=?, next_try=?, last_error=? WHERE id=?",
                            (attempts, self._t(self.clock() + timedelta(seconds=delay)), str(error)[:300], env_id))
            self.db.commit()
            return delay

    def outbox_withdraw(self, env_id):
        """Drop a pending envelope that was never sent (a cancelled dispatch). True if it was still pending."""
        with self._lock:
            n = self.db.execute("DELETE FROM outbox WHERE id=? AND state='pending'", (env_id,)).rowcount
            self.db.commit()
            return n > 0

    def outbox_purge(self, keep=hp.SEEN_KEEP):
        with self._lock:
            cut = self._t(self.clock() - keep)
            n = self.db.execute("DELETE FROM outbox WHERE state IN ('sent','failed') AND created<=?", (cut,)).rowcount
            self.db.commit()
            return n

    # ---------- ids already taken ----------

    def seen(self, env_id):
        row = self.db.execute("SELECT local_message FROM seen WHERE id=?", (env_id,)).fetchone()
        return None if row is None else (row["local_message"] or "")

    def seen_add(self, env_id, local_message=""):
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO seen(id,at,local_message) VALUES(?,?,?)", (env_id, self._t(), local_message))
            self.db.commit()

    def seen_purge(self, keep=hp.SEEN_KEEP):
        with self._lock:
            n = self.db.execute("DELETE FROM seen WHERE at<=?", (self._t(self.clock() - keep),)).rowcount
            self.db.commit()
            return n

    # ---------- what the hub told us / what we publish ----------

    def peers_set(self, peers):
        with self._lock:
            now = self._t()
            self.db.execute("DELETE FROM peers")
            for p in peers:
                self.db.execute("INSERT OR REPLACE INTO peers VALUES(?,?,?,?)",
                                (p["machine_id"], p["name"], json.dumps(p.get("sessions") or []), now))
            self.db.commit()

    def peers_get(self):
        return [{"machine_id": r["machine_id"], "name": r["name"], "sessions": json.loads(r["sessions"]), "updated": r["updated"]}
                for r in self.db.execute("SELECT * FROM peers ORDER BY name")]

    def publish(self, session, alias=""):
        with self._lock:
            self.db.execute("INSERT INTO published(session,alias) VALUES(?,?) ON CONFLICT(session) DO UPDATE SET alias=excluded.alias",
                            (session, alias))
            self.db.commit()

    def unpublish(self, session):
        with self._lock:
            n = self.db.execute("DELETE FROM published WHERE session=?", (session,)).rowcount
            self.db.commit()
            return n > 0

    def published(self):
        return [{"session": r["session"], "alias": r["alias"]} for r in self.db.execute("SELECT * FROM published ORDER BY session")]
