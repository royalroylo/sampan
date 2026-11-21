"""The hub's behaviour: machines, envelopes, delivery, expiry, quotas. No HTTP, no token checking: callers hand in a
Principal (who the token says is calling) and get dicts or a hubproto.ProtoError back.

State of an envelope: queued → (handed out) → taken, or expired / rejected / cancelled. Handing out is at least once: an
envelope that was handed out and not acked within VISIBILITY_TIMEOUT is handed out again; the receiver tells repeats apart
by id. The text is dropped when the envelope expires (at once) or BODY_KEEP after it was taken.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import hubproto as hp

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE machines (
    client_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE, owner TEXT NOT NULL DEFAULT 'self',
    sub TEXT, state TEXT NOT NULL, proto_min INT, proto_max INT, version TEXT, first_seen TEXT, last_seen TEXT,
    approved_at TEXT, approved_by TEXT, revoked_at TEXT, note TEXT
);
CREATE TABLE published (machine_id TEXT, session TEXT, alias TEXT, status TEXT, updated TEXT, PRIMARY KEY (machine_id, session));
CREATE TABLE envelopes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    from_machine TEXT, from_name TEXT, from_sub TEXT, from_session TEXT, owner TEXT,
    to_machine TEXT NOT NULL, to_session TEXT, thread_id TEXT, in_reply_to TEXT, created TEXT NOT NULL, expires TEXT NOT NULL,
    state TEXT NOT NULL, reason TEXT, attempts INT DEFAULT 0, last_attempt TEXT, taken_at TEXT,
    body TEXT, body_len INT, size INT DEFAULT 0, body_purge_at TEXT, status TEXT, phase TEXT, ref TEXT, sig TEXT, digest TEXT, stamped_at TEXT, hub INT DEFAULT 0
);
CREATE INDEX envelopes_to ON envelopes(to_machine, state, seq);
CREATE INDEX envelopes_from ON envelopes(from_machine, created);
CREATE TABLE quota (client_id TEXT, day TEXT, count INT, bytes INT, PRIMARY KEY (client_id, day));
CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT, action TEXT, subject TEXT, detail TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
LIVE = ("queued", "delivered")  # envelope states that still wait for the target
VACUUM_EVERY_DAYS = 1


def utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Principal:
    """Who is calling, as the token says: the OAuth client (one per machine) and its subject."""
    client_id: str
    sub: str = ""


@dataclass
class Limits:
    mailbox_cap: int = hp.DEFAULT_MAILBOX_CAP
    mailbox_bytes: int = hp.DEFAULT_MAILBOX_BYTES
    daily_messages: int = hp.DEFAULT_DAILY_MESSAGES
    daily_bytes: int = hp.DEFAULT_DAILY_BYTES
    visibility_timeout: int = hp.VISIBILITY_TIMEOUT
    max_pending: int = 20  # machines waiting for approval at once: a client the identity provider accepts cannot fill the table


class HubCore:
    def __init__(self, path, *, clock=utcnow, limits=None):
        self.path, self.clock, self.limits = str(path), clock, limits or Limits()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._pollers = {}  # machine id → the poll that is waiting for it (a newer one replaces it)
        self._open()

    # ---------- storage ----------

    def _open(self):
        fresh = not os.path.exists(self.path)
        old = os.umask(0o077)  # the db, its -wal and -shm are owner only
        try:
            self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        finally:
            os.umask(old)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA foreign_keys=ON")
        if fresh or self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self):
        with self._lock:
            self.db.close()

    def _now(self):
        return self.clock()

    def _t(self, when=None):
        return hp.iso(when or self._now())

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self._lock:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, str(value)))
            self.db.commit()

    @property
    def min_client_proto(self):
        return int(self.meta("min_client_proto", hp.PROTO_MIN))

    def audit(self, actor, action, subject="", detail=""):
        """One line in the audit trail. Never the text of a message, a token or a secret."""
        self.db.execute("INSERT INTO audit(at,actor,action,subject,detail) VALUES(?,?,?,?,?)",
                        (self._t(), actor, action, subject, detail[:500]))

    # ---------- machines ----------

    def _machine(self, p):
        row = self.db.execute("SELECT * FROM machines WHERE client_id=?", (p.client_id,)).fetchone()
        return row

    def _active(self, p):
        """The machine row of an active caller, or ProtoError (unknown → pending-approval after hello, pending, revoked)."""
        m = self._machine(p)
        if m is None:
            raise hp.ProtoError("pending-approval", "先送 hello")
        if m["state"] == "revoked":
            raise hp.ProtoError("revoked")
        if m["state"] != "active":
            raise hp.ProtoError("pending-approval")
        self.db.execute("UPDATE machines SET last_seen=? WHERE client_id=?", (self._t(), p.client_id))
        return m

    def hello(self, p, payload):
        """Register or refresh a machine. A first hello makes a `pending` machine; the owner approves it on the hub host."""
        if not isinstance(payload, dict):
            raise hp.ProtoError("bad-request")
        machine_id, name = payload.get("machine_id"), payload.get("name")
        if not isinstance(machine_id, str) or not machine_id or not hp.valid_name(name):
            raise hp.ProtoError("bad-request", "機器 id 或名稱不合規")
        lo, hi = payload.get("proto_min"), payload.get("proto_max")
        if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi:
            raise hp.ProtoError("bad-request", "protocol 範圍")
        if hi < self.min_client_proto or hi < hp.PROTO_MIN:
            raise hp.ProtoError("upgrade-required", f"hub 最低要求協定 {self.min_client_proto}")
        if lo > hp.PROTO:
            raise hp.ProtoError("upgrade-required", "hub 比這個客戶端舊")
        with self._lock:
            now = self._t()
            m = self._machine(p)
            if m is None:
                if self.db.execute("SELECT COUNT(*) FROM machines WHERE state='pending'").fetchone()[0] >= self.limits.max_pending:
                    raise hp.ProtoError("rate-limited", "等核准的機器太多了：先核准或撤銷一些")
                if self.db.execute("SELECT 1 FROM machines WHERE name=?", (name,)).fetchone():
                    raise hp.ProtoError("name-taken")
                if self.db.execute("SELECT 1 FROM machines WHERE machine_id=?", (machine_id,)).fetchone():
                    raise hp.ProtoError("machine-mismatch", "這個機器 id 已被另一個 client 使用")
                self.db.execute(
                    "INSERT INTO machines(client_id,machine_id,name,sub,state,proto_min,proto_max,version,first_seen,last_seen)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (p.client_id, machine_id, name, p.sub, "pending", lo, hi, str(payload.get("version") or ""), now, now))
                self.audit(p.client_id, "machine-registered", name, machine_id)
                m = self._machine(p)
            else:
                if m["machine_id"] != machine_id:
                    raise hp.ProtoError("machine-mismatch")
                if m["state"] == "revoked":
                    raise hp.ProtoError("revoked")
                if name != m["name"]:
                    if self.db.execute("SELECT 1 FROM machines WHERE name=? AND client_id<>?", (name, p.client_id)).fetchone():
                        raise hp.ProtoError("name-taken")
                    self.db.execute("UPDATE machines SET name=? WHERE client_id=?", (name, p.client_id))
                self.db.execute("UPDATE machines SET last_seen=?, proto_min=?, proto_max=?, version=? WHERE client_id=?",
                                (now, lo, hi, str(payload.get("version") or ""), p.client_id))
            if m["state"] == "active":
                self._set_published(machine_id, payload.get("published"), now)
            self.db.commit()
            m = self._machine(p)
            return {"state": m["state"], "machine_id": machine_id, "proto": hp.PROTO, "proto_min": hp.PROTO_MIN,
                    "min_client_proto": self.min_client_proto, "require_sig": True, "server_time": now,
                    "code": self._verification_code(m)}

    @staticmethod
    def _verification_code(m):
        return verification_code(m["machine_id"], m["client_id"])

    def _set_published(self, machine_id, sessions, now):
        self.db.execute("DELETE FROM published WHERE machine_id=?", (machine_id,))
        if not isinstance(sessions, list):
            return
        for s in sessions[:200]:
            if isinstance(s, dict) and isinstance(s.get("session"), str) and s["session"]:
                self.db.execute("INSERT OR REPLACE INTO published VALUES(?,?,?,?,?)",
                                (machine_id, s["session"][:100], str(s.get("alias") or "")[:100],
                                 str(s.get("status") or "")[:20], now))

    def peers(self, p):
        """The owner's other machines and what they published (session names, aliases and busy/idle)."""
        with self._lock:
            me = self._active(p)
            out = []
            for m in self.db.execute("SELECT * FROM machines WHERE state='active' AND owner=? AND machine_id<>? ORDER BY name",
                                     (me["owner"], me["machine_id"])):
                sessions = [{"session": r["session"], "alias": r["alias"], "status": r["status"]}
                            for r in self.db.execute("SELECT * FROM published WHERE machine_id=? ORDER BY session",
                                                     (m["machine_id"],))]
                out.append({"machine_id": m["machine_id"], "name": m["name"], "last_seen": m["last_seen"],
                            "sessions": sessions})
            self.db.commit()
            return out

    # ---------- send ----------

    def _row_envelope(self, r):
        env = {"v": hp.PROTO, "id": r["id"], "kind": r["kind"], "to": {"machine": r["to_machine"], "session": r["to_session"]},
               "from_session": r["from_session"] or "", "thread_id": r["thread_id"], "in_reply_to": r["in_reply_to"],
               "created": r["created"], "expires": r["expires"], "body": r["body"] or "", "body_len": r["body_len"] or 0,
               "stamp": {"sender_machine": r["from_machine"], "sender_machine_name": r["from_name"],
                         "sender_sub": r["from_sub"], "owner": r["owner"], "at": r["stamped_at"], "hub": bool(r["hub"])}}
        if r["kind"] == "reply":
            env["status"], env["reason"] = r["status"], r["reason"] or ""
        if r["kind"] == "receipt":
            env["phase"], env["ref"], env["reason"] = r["phase"], r["ref"], r["reason"] or ""
        if r["sig"]:
            env["sig"] = json.loads(r["sig"])
        return env

    def _digest(self, env):
        return hp.body_hash(hp.canonical(hp.client_fields(env)).decode("utf-8"))

    def _quota(self, m, size):
        day = self._now().strftime("%Y-%m-%d")
        row = self.db.execute("SELECT count, bytes FROM quota WHERE client_id=? AND day=?", (m["client_id"], day)).fetchone()
        count, used = (row["count"], row["bytes"]) if row else (0, 0)
        if count + 1 > self.limits.daily_messages or used + size > self.limits.daily_bytes:
            key = f"quota-audited:{m['client_id']}"
            if self.meta(key) != day:  # one line a day: the trail is kept a year and must not be fillable by hitting the limit
                self.audit(m["client_id"], "quota", m["name"], f"{count} messages, {used} bytes today")
                self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, day))
            self.db.commit()
            raise hp.ProtoError("rate-limited", "今天的額度用完了")
        self.db.execute("INSERT INTO quota VALUES(?,?,?,?) ON CONFLICT(client_id,day) DO UPDATE SET count=count+1, bytes=bytes+?",
                        (m["client_id"], day, 1, size, size))

    def _mailbox_full(self, to_machine, size):
        row = self.db.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM envelopes WHERE to_machine=? AND state IN ('queued','delivered')",
                              (to_machine,)).fetchone()
        return row["n"] + 1 > self.limits.mailbox_cap or row["b"] + size > self.limits.mailbox_bytes

    def send(self, p, env):
        """Accept an envelope from an active machine: validated, stamped by the hub, stored. Idempotent by id: the same
        envelope again answers the same. Returns {id, accepted_at, duplicate}."""
        with self._lock:
            m = self._active(p)
            now = self._now()
            fields = hp.client_fields(env) if isinstance(env, dict) else env
            problem = hp.check_envelope(fields, now=now)
            if problem is not None and not (problem.code in ("too-old", "expired") and self._known(fields)):
                raise problem
            digest = self._digest(fields)
            known = self.db.execute("SELECT * FROM envelopes WHERE id=?", (fields["id"],)).fetchone()
            if known is not None:
                if known["from_machine"] != m["machine_id"] or known["digest"] != digest:
                    raise hp.ProtoError("bad-envelope", "這個 id 已用在別的信封")
                return {"id": fields["id"], "accepted_at": known["stamped_at"], "duplicate": True, "state": known["state"]}
            if fields["kind"] == "receipt" and fields.get("phase") != "sent":
                raise hp.ProtoError("bad-envelope", "client 只能送 phase=sent 的 receipt")
            to = fields["to"]
            target = self.db.execute("SELECT * FROM machines WHERE machine_id=? AND owner=? AND state='active'",
                                     (to["machine"], m["owner"])).fetchone()
            if target is None:
                raise hp.ProtoError("unknown-target", "機器沒登記、還沒核准或已撤銷")
            if fields["kind"] == "message":
                if not self.db.execute("SELECT 1 FROM published WHERE machine_id=? AND session=?", (to["machine"], to["session"])).fetchone():
                    raise hp.ProtoError("unknown-target", f"{target['name']} 沒有發布 {to['session']}")
            else:
                orig_id = fields.get("in_reply_to") if fields["kind"] == "reply" else fields.get("ref")
                orig = self.db.execute("SELECT * FROM envelopes WHERE id=?", (orig_id,)).fetchone()
                if orig is None or orig["kind"] != "message" or orig["from_machine"] != to["machine"] or orig["to_machine"] != m["machine_id"]:
                    raise hp.ProtoError("unknown-target", "回覆或回條要指向送給你的訊息")
            size = hp.envelope_size(fields)  # the whole envelope counts, not just the text
            if self._mailbox_full(to["machine"], size):
                raise hp.ProtoError("mailbox-full")
            self._quota(m, size)
            stamped = self._t(now)
            self.db.execute(
                "INSERT INTO envelopes(id,kind,from_machine,from_name,from_sub,from_session,owner,to_machine,to_session,thread_id,"
                "in_reply_to,created,expires,state,body,body_len,size,status,phase,ref,reason,sig,digest,stamped_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fields["id"], fields["kind"], m["machine_id"], m["name"], p.sub, fields.get("from_session") or "", m["owner"],
                 to["machine"], to["session"], fields.get("thread_id"), fields.get("in_reply_to"), fields["created"],
                 fields["expires"], "queued", fields.get("body") or "", len(fields.get("body") or ""), size, fields.get("status"),
                 fields.get("phase"), fields.get("ref"), fields.get("reason") or "",
                 json.dumps(fields["sig"]) if isinstance(fields.get("sig"), dict) else None, digest, stamped))
            self.db.commit()
            self._cond.notify_all()
            return {"id": fields["id"], "accepted_at": stamped, "duplicate": False, "state": "queued"}

    def _known(self, fields):
        return isinstance(fields, dict) and self.db.execute("SELECT 1 FROM envelopes WHERE id=?", (fields.get("id"),)).fetchone()

    # ---------- receive ----------

    def fetch(self, p, limit=hp.POLL_BATCH):
        """Hand out what is waiting for this machine (not yet handed out, or handed out and not acked in time)."""
        with self._lock:
            m = self._active(p)
            now = self._now()
            stale = self._t(now - timedelta(seconds=self.limits.visibility_timeout))
            rows = self.db.execute(
                "SELECT * FROM envelopes WHERE to_machine=? AND expires>? AND (state='queued' OR (state='delivered' AND last_attempt<=?))"
                " AND (from_machine IS NULL OR from_machine IN (SELECT machine_id FROM machines WHERE state='active'))"  # not from a revoked machine
                " ORDER BY seq LIMIT ?", (m["machine_id"], self._t(now), stale, min(limit, hp.POLL_BATCH))).fetchall()
            for r in rows:
                self.db.execute("UPDATE envelopes SET state='delivered', attempts=attempts+1, last_attempt=? WHERE id=?",
                                (self._t(now), r["id"]))
            self.db.commit()
            return [dict(self._row_envelope(r), attempt=r["attempts"] + 1) for r in rows]

    def poll(self, p, wait=hp.POLL_WAIT):
        """fetch, but when nothing is waiting, wait up to `wait` seconds for something. Only one poll per machine waits:
        a newer one replaces the older (which gets `superseded`)."""
        deadline = time.monotonic() + max(0.0, wait)
        with self._lock:
            m = self._active(p)
            token = object()
            self._pollers[m["machine_id"]] = token
            try:
                while True:
                    items = self.fetch(p)
                    if items:
                        return items
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return []
                    self._cond.wait(min(left, 0.5))
                    if self._pollers.get(m["machine_id"]) is not token:
                        raise hp.ProtoError("superseded")
                    self._active(p)  # revoked while waiting
            finally:
                if self._pollers.get(m["machine_id"]) is token:
                    del self._pollers[m["machine_id"]]

    def wake(self):
        with self._lock:
            self._cond.notify_all()

    def ack(self, p, ids, result, reason=""):
        """The machine took (or refused) these envelopes. Taken starts the text's 14 days, and the sender gets the hub's
        receipt `taken` (or `rejected` with the reason)."""
        if result not in ("taken", "rejected") or not isinstance(ids, list):
            raise hp.ProtoError("bad-request")
        with self._lock:
            m = self._active(p)
            now = self._now()
            done = 0
            for i in ids[:hp.POLL_BATCH * 5]:
                r = self.db.execute("SELECT * FROM envelopes WHERE id=? AND to_machine=?", (i, m["machine_id"])).fetchone()
                if r is None:
                    continue
                if r["state"] in ("taken", "rejected", "expired", "cancelled"):
                    done += 1  # an ack that arrives again
                    continue
                if result == "taken":
                    self.db.execute("UPDATE envelopes SET state='taken', taken_at=?, body_purge_at=? WHERE id=?",
                                    (self._t(now), self._t(now + hp.BODY_KEEP), i))
                else:
                    self.db.execute("UPDATE envelopes SET state='rejected', reason=?, body=NULL WHERE id=?", (str(reason)[:200], i))
                if r["kind"] == "message":
                    self._hub_receipt(r, "taken" if result == "taken" else "rejected", str(reason)[:200], now)
                done += 1
            self.db.commit()
            return {"acked": done}

    def _hub_receipt(self, orig, phase, reason, now):
        """The hub's own receipt for a message, to the machine that sent it (no signature: the hub says it, not the owner)."""
        if not orig["from_machine"]:
            return
        rid = hp.new_id()
        self.db.execute(
            "INSERT INTO envelopes(id,kind,from_machine,from_name,from_sub,from_session,owner,to_machine,to_session,created,expires,"
            "state,body,body_len,phase,ref,reason,stamped_at,hub) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (rid, "receipt", None, "hub", None, "", orig["owner"], orig["from_machine"], orig["from_session"], self._t(now),
             self._t(now + hp.EXPIRY_SELF), "queued", "", 0, phase, orig["id"], reason, self._t(now)))
        self._cond.notify_all()

    def cancel(self, p, env_id):
        """Take back an envelope the target has not been handed. {cancelled, state}."""
        with self._lock:
            m = self._active(p)
            r = self.db.execute("SELECT * FROM envelopes WHERE id=? AND from_machine=?", (env_id, m["machine_id"])).fetchone()
            if r is None:
                raise hp.ProtoError("not-found")
            if r["state"] == "queued":
                self.db.execute("UPDATE envelopes SET state='cancelled', body=NULL WHERE id=?", (env_id,))
                self.db.commit()
                return {"cancelled": True, "state": "cancelled"}
            return {"cancelled": r["state"] == "cancelled", "state": r["state"]}

    # ---------- time ----------

    def tick(self):
        """Expiry and cleaning; run about once a minute. Returns counts."""
        with self._lock:
            now = self._now()
            t = self._t(now)
            expired = purged = 0
            for r in self.db.execute("SELECT * FROM envelopes WHERE state IN ('queued','delivered') AND expires<=?", (t,)).fetchall():
                self.db.execute("UPDATE envelopes SET state='expired', body=NULL WHERE id=?", (r["id"],))
                if r["kind"] == "message":
                    self._hub_receipt(r, "expired", "對方一直沒上線，已過期", now)
                expired += 1
            purged += self.db.execute("UPDATE envelopes SET body=NULL WHERE body IS NOT NULL AND body_purge_at IS NOT NULL AND body_purge_at<=?",
                                      (t,)).rowcount
            cut = self._t(now - hp.TOMBSTONE_KEEP)
            gone = self.db.execute("DELETE FROM envelopes WHERE state IN ('taken','expired','rejected','cancelled') AND created<=?",
                                   (cut,)).rowcount
            self.db.execute("DELETE FROM audit WHERE at<=?", (self._t(now - hp.AUDIT_KEEP),))
            self.db.execute("DELETE FROM quota WHERE day<?", ((now - timedelta(days=3)).strftime("%Y-%m-%d"),))
            self.db.execute("DELETE FROM meta WHERE key LIKE 'quota-audited:%' AND value<?", ((now - timedelta(days=3)).strftime("%Y-%m-%d"),))
            self.db.commit()
            last = hp.parse_time(self.meta("last_vacuum"))
            if last is None or (now - last).days >= VACUUM_EVERY_DAYS:
                self.db.execute("VACUUM")
                self.set_meta("last_vacuum", t)
            # secure_delete blanks the page, but the old text may still sit in the write-ahead log: empty it (last, so
            # nothing written after it puts anything back)
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {"expired": expired, "purged": purged, "deleted": gone}

    # ---------- admin (the hub host's own commands) ----------

    def machines(self):
        with self._lock:
            return [dict(r, code=verification_code(r["machine_id"], r["client_id"]))
                    for r in self.db.execute("SELECT * FROM machines ORDER BY first_seen")]

    def _by_name(self, name):
        r = self.db.execute("SELECT * FROM machines WHERE name=?", (name,)).fetchone()
        if r is None:
            raise hp.ProtoError("not-found", f"沒有機器 {name}")
        return r

    def approve(self, name, actor="admin", code=None):
        """Approve a pending machine. `code` (the one the machine printed) is checked when given."""
        with self._lock:
            r = self._by_name(name)
            if r["state"] == "revoked":
                raise hp.ProtoError("revoked", "已撤銷的不能核准")
            if code is not None and code != verification_code(r["machine_id"], r["client_id"]):
                raise hp.ProtoError("bad-request", "驗證碼不符")
            self.db.execute("UPDATE machines SET state='active', approved_at=?, approved_by=? WHERE name=?", (self._t(), actor, name))
            self.audit(actor, "machine-approved", name)
            self.db.commit()
            return {"name": name, "state": "active"}

    def revoke(self, name, actor="admin"):
        """Refuse this machine from now on (at once, whatever its token's lifetime) and drop what waits for it."""
        with self._lock:
            r = self._by_name(name)
            now = self._now()
            self.db.execute("UPDATE machines SET state='revoked', revoked_at=? WHERE name=?", (self._t(now), name))
            self.db.execute("DELETE FROM published WHERE machine_id=?", (r["machine_id"],))
            for e in self.db.execute("SELECT * FROM envelopes WHERE to_machine=? AND state IN ('queued','delivered')",
                                     (r["machine_id"],)).fetchall():
                self.db.execute("UPDATE envelopes SET state='rejected', reason='machine-revoked', body=NULL WHERE id=?", (e["id"],))
                if e["kind"] == "message":
                    self._hub_receipt(e, "rejected", "machine-revoked", now)
            # what it sent and nobody has taken yet goes too (a stolen machine's queued messages must not arrive later)
            self.db.execute("UPDATE envelopes SET state='rejected', reason='sender-revoked', body=NULL WHERE from_machine=? AND state IN ('queued','delivered')",
                            (r["machine_id"],))
            self.audit(actor, "machine-revoked", name)
            self.db.commit()
            self._cond.notify_all()
            return {"name": name, "state": "revoked"}

    def audit_log(self, limit=100):
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    def status(self):
        with self._lock:
            counts = {r["state"]: r["n"] for r in self.db.execute("SELECT state, COUNT(*) n FROM machines GROUP BY state")}
            env = {r["state"]: r["n"] for r in self.db.execute("SELECT state, COUNT(*) n FROM envelopes GROUP BY state")}
            return {"machines": counts, "envelopes": env, "proto": hp.PROTO, "min_client_proto": self.min_client_proto}


def verification_code(machine_id, client_id):
    """Six characters from the machine id and client id, the same on both sides, so the owner can match the machine
    asking in front of them with the one the hub lists as pending."""
    digest = hashlib.sha256(f"{machine_id}\n{client_id}".encode("utf-8")).hexdigest()
    return f"{digest[:3]}-{digest[3:6]}".upper()
