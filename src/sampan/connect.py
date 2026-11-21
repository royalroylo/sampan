"""`sampan connect`: this machine's side of the hub. The core is plain functions over a transport, a clock and a set of
hooks, so it runs (and is tested with faults) without sockets; the HTTP transport and the daemon loop are added on top.

What it does, in one pass (tick): say hello (registers the machine, refreshes what it publishes), send what the outbox holds,
take what the hub has for this machine. Everything is at least once: an envelope is sent until the hub accepts it (the hub
answers a repeat with the same answer), and taken until it is acked (a repeat is recognised by id and not applied twice).
"""
import os
import stat
from dataclasses import dataclass
from typing import Callable

from . import hubproto as hp

TRANSIENT = ("mailbox-full", "rate-limited", "pending-approval", "superseded", "unauthorized")  # try again later
SLOW = ("mailbox-full", "rate-limited")  # and not too soon
HUB_PHASES = ("taken", "rejected", "expired")  # receipts the hub may send without an owner signature
SLOW_FLOOR = 30
HELLO_EVERY = 60  # seconds between two hellos (they carry what the machine publishes and its busy/idle)
KEY_FILE = "owner.key"
KEY_KID = "k1"


class TransportError(Exception):
    """The hub could not be reached or answered with a server error: try again later."""


class Rejected(Exception):
    """Raised by a hook for an envelope that can never be applied here (the session is gone): it is acked as rejected."""


@dataclass
class Hooks:
    """What the core needs from the rest of sampan. The two that change dispatches must be idempotent by envelope id: a crash
    between applying and recording the id makes the envelope come again."""
    deliver_local: Callable  # (env) -> local message id; the target session gets it like any message
    apply_receipt: Callable  # (env): a receipt (taken, sent, rejected, expired) for a message this machine sent
    apply_reply: Callable  # (env): the answer to a message this machine sent
    accepted: Callable = lambda dispatch_id, env_id, resp: None  # the hub took an outbox envelope
    failed: Callable = lambda dispatch_id, env_id, code, message: None  # an outbox envelope can never be sent
    published_status: Callable = lambda: []  # () -> [{session, alias, status}] of what is published right now
    log: Callable = lambda line: None


def load_keys(path):
    """{kid: key bytes} from the owner key file: one `kid:base64url` per line (a bare line is kid k1). Empty when the file
    is missing or unreadable (nothing can be signed or verified then)."""
    keys = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return keys
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kid, _, raw = line.partition(":") if ":" in line else (KEY_KID, "", line)
        try:
            keys[kid.strip()] = hp.decode_key(raw)
        except ValueError:
            continue
    return {k: v for k, v in keys.items() if len(v) >= 16}


def write_key(path, key_text=None, kid=KEY_KID):
    """Create the owner key file (0600) with a fresh key; the text to copy to the owner's other machines is returned.
    Refuses to overwrite an existing file."""
    key_text = key_text or hp.new_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"{kid}:{key_text}\n")
    return f"{kid}:{key_text}"


def file_problem(path):
    """Why a secret file is not private enough (it should be a regular file only its owner can read), or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return f"{path} 不是一般檔案"
    if st.st_mode & 0o077:
        return f"{path} 的權限是 {st.st_mode & 0o777:o}，別人讀得到（要 600）"
    return None


class Connector:
    def __init__(self, store, transport, hooks, *, machine_id, machine_name, version, keys, kid=KEY_KID, clock):
        self.store, self.transport, self.hooks = store, transport, hooks
        self.machine_id, self.machine_name, self.version = machine_id, machine_name, version
        self.keys, self.kid, self.clock = keys, kid, clock
        self.state = store.get("machine_state", "unknown")  # unknown | pending | active | revoked | unsupported
        self.code = store.get("verification_code", "")
        self._last_hello = None

    # ---------- signing ----------

    def sign(self, env):
        """The envelope with this machine's signature (the owner key); a machine without a key cannot send."""
        if self.kid not in self.keys:
            raise Rejected("沒有 owner key（~/.config/sampan/hub/owner.key），不能簽章")
        return hp.sign(env, self.keys, self.kid, self.machine_id)

    # ---------- hello ----------

    def hello(self):
        """Register or refresh this machine at the hub. The state it comes back with decides what else happens."""
        payload = {"machine_id": self.machine_id, "name": self.machine_name, "version": self.version,
                   "proto_min": hp.PROTO_MIN, "proto_max": hp.PROTO, "published": self.hooks.published_status()}
        try:
            resp = self.transport.hello(payload)
        except hp.ProtoError as e:
            if e.code == "revoked":
                self._set_state("revoked")
            elif e.code == "upgrade-required":
                self._set_state("unsupported")
            self.hooks.log(f"hello 被拒：{e.message}")
            raise
        if resp.get("proto_min", 1) > hp.PROTO or resp.get("proto", hp.PROTO) < hp.PROTO_MIN:
            self._set_state("unsupported")
            self.hooks.log("hub 的協定版本和這個 sampan 不相容，需要更新其中一邊")
            raise hp.ProtoError("upgrade-required", "hub 的協定版本不相容")
        if not resp.get("require_sig", True):
            self.hooks.log("hub 說不用簽章：忽略（簽章不能被降級）")
        self.code = resp.get("code", "")
        self.store.put("verification_code", self.code)
        self._set_state(resp.get("state", "pending"))
        self._last_hello = self.clock()
        return resp

    def _set_state(self, state):
        if state != self.state:
            self.hooks.log(f"機器狀態：{self.state} → {state}")
        self.state = state
        self.store.put("machine_state", state)

    def hello_due(self):
        return self._last_hello is None or (self.clock() - self._last_hello).total_seconds() >= HELLO_EVERY

    # ---------- outbox ----------

    def send_outbox(self):
        """Send what is due. Stops at the first network failure (everything after it would fail the same way)."""
        sent = failed = retried = 0
        now = self.clock()
        for row in self.store.outbox_due():
            env, env_id, did = row["envelope"], row["id"], row["dispatch_id"]
            expires = hp.parse_time(env.get("expires"))
            if expires is not None and now >= expires:
                self.store.outbox_failed(env_id, "expired")
                self.hooks.failed(did, env_id, "expired", "對方一直沒上線，已過期")
                failed += 1
                continue
            try:
                resp = self.transport.send(env)
            except TransportError as e:
                self.store.outbox_retry(env_id, f"hub 連不上：{e}")
                retried += 1
                break
            except hp.ProtoError as e:
                if e.code in TRANSIENT:
                    self.store.outbox_retry(env_id, e.message, floor=SLOW_FLOOR if e.code in SLOW else 0)
                    retried += 1
                    if e.code in ("pending-approval", "unauthorized"):
                        break
                    continue
                if e.code == "revoked":  # this machine, not the envelope: keep everything for when it is approved again
                    self._set_state("revoked")
                    self.store.outbox_retry(env_id, e.message, floor=SLOW_FLOOR)
                    retried += 1
                    break
                self.store.outbox_failed(env_id, e.message)
                self.hooks.failed(did, env_id, e.code, e.message)
                failed += 1
                continue
            self.store.outbox_sent(env_id)
            self.hooks.accepted(did, env_id, resp)
            sent += 1
        return {"sent": sent, "failed": failed, "retried": retried}

    # ---------- inbound ----------

    def _check_inbound(self, env):
        """None when env may be applied, else the reason (a short code) it is refused."""
        if not isinstance(env, dict) or env.get("v") != hp.PROTO or not hp.is_id(env.get("id")):
            return "bad-envelope"
        stamp = env.get("stamp") or {}
        if (env.get("to") or {}).get("machine") != self.machine_id:
            return "wrong-machine"
        now = self.clock()
        created, expires = hp.parse_time(env.get("created")), hp.parse_time(env.get("expires"))
        if created is None or expires is None:
            return "bad-envelope"
        if now >= expires:
            return "expired"
        if now - created > hp.MAX_AGE:
            return "too-old"
        if created - now > hp.SKEW_REJECT:
            return "clock-skew"  # from the future: it could be replayed once the ids it was taken under are forgotten
        if expires - created > hp.EXPIRY_SELF:
            return "bad-envelope"
        for text in (env.get("body"), env.get("reason")):
            if isinstance(text, str) and hp.unsafe_char(text):
                return "unsafe-characters"  # never reaches a session: an ESC could end a bracketed paste early
        for text in (env.get("from_session"), stamp.get("sender_machine_name"), stamp.get("sender_sub")):
            if isinstance(text, str) and hp.unsafe_name(text):
                return "unsafe-characters"  # names are pasted on the first line of a message: no newline either
        if env.get("kind") == "receipt" and stamp.get("hub") and not env.get("sig"):
            # the hub's own receipt: it says it itself, no owner signature; but only these phases, about a real envelope.
            # `sent` (pasted into the session) is the other machine's word and needs the owner signature.
            return None if env.get("phase") in HUB_PHASES and hp.is_id(env.get("ref")) else "bad-envelope"
        if env.get("kind") not in hp.KINDS or not stamp.get("sender_machine"):
            return "bad-envelope"
        if not hp.verify(env, self.keys, stamp["sender_machine"]):
            return "bad-signature"
        return None

    def pull(self, wait=0):
        """Take what the hub has for this machine and apply it. Returns counts. An envelope whose hook raises is left
        unacked (the hub hands it out again), never applied half."""
        items = self.transport.poll(wait)
        taken, rejected, repeats, errors = [], [], 0, 0
        for env in items:
            env_id = env.get("id") if isinstance(env, dict) else None
            if not hp.is_id(env_id):
                continue
            if self.store.seen(env_id) is not None:
                taken.append(env_id)  # already applied, the ack was lost
                repeats += 1
                continue
            problem = self._check_inbound(env)
            if problem is not None:
                rejected.append((env_id, problem))
                self.hooks.log(f"拒收信封 {env_id[:8]}：{problem}")
                continue
            try:
                local = self._apply(env)
                self.store.seen_add(env_id, local or "")  # applied, then recorded: a crash in between replays the id
            except Rejected as e:
                rejected.append((env_id, str(e)[:100]))
                self.hooks.log(f"拒收信封 {env_id[:8]}：{e}")
                continue
            except Exception as e:  # noqa: BLE001  not acked: the hub hands it out again, and applying is idempotent by id
                errors += 1
                self.hooks.log(f"處理信封 {env_id[:8]} 失敗，稍後會再送：{type(e).__name__}: {e}")
                continue
            taken.append(env_id)
        if taken:
            self.transport.ack(taken, "taken")
        by_reason = {}
        for env_id, reason in rejected:
            by_reason.setdefault(reason, []).append(env_id)
        for reason, ids in by_reason.items():
            self.transport.ack(ids, "rejected", reason)
        return {"taken": len(taken) - repeats, "repeats": repeats, "rejected": len(rejected), "errors": errors}

    def _apply(self, env):
        kind = env["kind"]
        if kind == "message":
            return self.hooks.deliver_local(env)
        if kind == "reply":
            self.hooks.apply_reply(env)
        else:
            self.hooks.apply_receipt(env)
        return ""

    # ---------- one pass ----------

    def tick(self, wait=0):
        """hello (when due), send, take. Nothing is sent or taken until the hub says this machine is approved."""
        out = {"state": self.state}
        if self.hello_due() or self.state != "active":
            try:
                self.hello()
            except hp.ProtoError as e:
                out["state"], out["error"] = self.state, e.message
                return out
            except TransportError as e:
                out["error"] = f"hub 連不上：{e}"
                return out
            out["state"] = self.state
            if self.state == "active":
                self._refresh_peers(out)
        if self.state != "active":
            return out
        try:
            out["send"] = self.send_outbox()
            out["pull"] = self.pull(wait)
        except TransportError as e:
            out["error"] = f"hub 連不上：{e}"
        except hp.ProtoError as e:
            if e.code == "revoked":
                self._set_state("revoked")
            out["error"] = e.message
        return out

    def _refresh_peers(self, out):
        """The owner's other machines and what they publish, kept for `machine/session` addressing (with each hello)."""
        try:
            self.store.peers_set(self.transport.peers())
        except (TransportError, hp.ProtoError) as e:
            out["peers_error"] = str(e)

    def maintenance(self):
        """Forget what is old: the ids taken (16 days), finished outbox rows."""
        return {"seen": self.store.seen_purge(), "outbox": self.store.outbox_purge()}
