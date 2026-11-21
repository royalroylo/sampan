"""The hub protocol, version 1: the envelope, its signature, addressing and the limits both sides share.

Standard library only: `sampan connect` (the client) imports this, and sampan itself has no third-party dependency. The hub
(`sampan[hub]`) uses the same module, so the two cannot disagree about what a signed envelope is.

An envelope is a plain dict (JSON on the wire):

    v, id, kind (message | reply | receipt), to {machine, session}, from_session (what the sender calls itself),
    thread_id, in_reply_to, created, expires, body, body_len,
    status / reason (a reply: done or failed), phase / ref (a receipt), sig {kid, alg, mac}, stamp {…}

`stamp` is written by the hub alone, from the token that sent the envelope (never from the envelope): a client's own
`stamp` or `from` is dropped. `sig` is an HMAC-SHA256 over the fields that matter, with the owner key every one of the
owner's machines holds (put there by hand, never through the hub), so a hub that is taken over cannot make a message
look as if it came from another of the owner's machines.
"""
import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

PROTO = 1
PROTO_MIN = 1
USER_AGENT_PREFIX = "sampan-connect"
KINDS = ("message", "reply", "receipt")
REPLY_STATUS = ("done", "failed")
RECEIPT_PHASES = ("taken", "sent", "rejected", "expired")  # taken, rejected, expired: from the hub; sent: from the target machine
MAX_BODY = 8_000  # characters of one message (the existing batching limit)
MAX_REQUEST = 64 * 1024  # bytes of one request body
MAX_FROM_SESSION = 100
POLL_BATCH = 20
POLL_WAIT = 30  # seconds a poll waits for something before answering "nothing"
VISIBILITY_TIMEOUT = 60  # seconds before an envelope that was handed out and not acked is handed out again
EXPIRY_SELF = timedelta(days=7)  # an envelope between the owner's own machines
EXPIRY_PERSON = timedelta(hours=48)  # across people (second phase)
BODY_KEEP = timedelta(days=14)  # how long the text stays after the target took it
TOMBSTONE_KEEP = timedelta(days=90)  # who sent what to whom, without the text
AUDIT_KEEP = timedelta(days=365)
SEEN_KEEP = timedelta(days=16)  # the receiver's list of ids it has taken: longer than BODY_KEEP plus a margin
MAX_AGE = timedelta(days=7)  # an envelope created longer ago than this is refused (replay)
SKEW_WARN = timedelta(minutes=5)
SKEW_REJECT = timedelta(minutes=15)
DEFAULT_MAILBOX_CAP = 200  # pending envelopes per machine
DEFAULT_MAILBOX_BYTES = 1_000_000
DEFAULT_DAILY_MESSAGES = 500
DEFAULT_DAILY_BYTES = 2_000_000

# error codes of the hub (the HTTP status is the first field)
ERRORS = {
    "bad-request": (400, "請求格式不對"),
    "bad-envelope": (400, "信封格式不對"),
    "too-large": (413, "訊息太長"),
    "unauthorized": (401, "token 無效或過期"),
    "pending-approval": (403, "這台機器還沒被核准（在 hub 主機上 sampan hub machines approve）"),
    "revoked": (403, "這台機器已被撤銷"),
    "machine-mismatch": (409, "這個 client 已綁定另一個機器 id"),
    "name-taken": (409, "機器名稱已被別台使用（sampan machine --name 改名）"),
    "unknown-target": (404, "找不到這個目標（機器沒登記、或 session 沒發布）"),
    "unsupported-target": (400, "這個版本只支援自己裝置之間（機器/session）"),
    "expired": (410, "訊息已過期"),
    "too-old": (400, "訊息建立時間太久以前，當成重放拒絕"),
    "clock-skew": (400, "兩邊時鐘差太多"),
    "mailbox-full": (429, "對方信箱已滿"),
    "rate-limited": (429, "超過額度"),
    "upgrade-required": (426, "需要更新 sampan（協定版本太舊）"),
    "superseded": (409, "有另一條長輪詢取代了這條"),
    "not-found": (404, "找不到"),
}


class ProtoError(Exception):
    """A problem with a request or envelope; `code` is a key of ERRORS, `detail` an optional extra sentence."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail

    @property
    def status(self):
        return ERRORS[self.code][0]

    @property
    def message(self):
        text = ERRORS[self.code][1]
        return f"{text}（{self.detail}）" if self.detail else text


def user_agent(version):
    return f"{USER_AGENT_PREFIX}/{version} (proto {PROTO})"


# ---------- time ----------

def iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    """A datetime (UTC) from an ISO-8601 string ending in Z or an offset; None for anything else."""
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.astimezone(timezone.utc) if when.tzinfo else None


def new_id():
    return str(uuid.uuid4())


_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_id(text):
    return isinstance(text, str) and _ID_RE.fullmatch(text) is not None


# ---------- canonical form and signature ----------

def canonical(obj):
    """The bytes that are signed: keys sorted, no spaces, UTF-8 (not \\u escapes), so every implementation agrees."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def body_hash(body):
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def signed_core(env, sender_machine):
    """What the signature covers. The sender machine is the hub's stamp of who sent it, so a signature made for one
    machine does not verify as another's."""
    to = env.get("to") or {}
    return {
        "v": env.get("v"), "id": env.get("id"), "kind": env.get("kind"),
        "to": {"machine": to.get("machine"), "session": to.get("session")},
        "from_session": env.get("from_session") or "",
        "thread_id": env.get("thread_id"), "in_reply_to": env.get("in_reply_to"),
        "created": env.get("created"), "expires": env.get("expires"),
        "body_sha256": body_hash(env.get("body")),
        "status": env.get("status"), "reason": env.get("reason") or "", "phase": env.get("phase"),
        "ref": env.get("ref"), "sender_machine": sender_machine,
    }


def new_key():
    """A fresh owner key: 32 random bytes as base64url text (what goes into the key file)."""
    return base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).decode("ascii").rstrip("=")


def decode_key(text):
    pad = "=" * (-len(text.strip()) % 4)
    return base64.urlsafe_b64decode(text.strip() + pad)


def sign(env, keys, kid, sender_machine):
    """Put `sig` on env (a copy): keys is {kid: key bytes}."""
    out = dict(env)
    mac = hmac.new(keys[kid], canonical(signed_core(out, sender_machine)), hashlib.sha256).digest()
    out["sig"] = {"kid": kid, "alg": "HMAC-SHA256", "mac": base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")}
    return out


def verify(env, keys, sender_machine):
    """True when env's signature is valid for sender_machine under one of keys ({kid: key bytes})."""
    sig = env.get("sig")
    if not isinstance(sig, dict) or sig.get("alg") != "HMAC-SHA256" or sig.get("kid") not in keys:
        return False
    mac = sig.get("mac")
    if not isinstance(mac, str):
        return False
    want = hmac.new(keys[sig["kid"]], canonical(signed_core(env, sender_machine)), hashlib.sha256).digest()
    try:
        got = base64.urlsafe_b64decode(mac + "=" * (-len(mac) % 4))
    except ValueError:
        return False
    return hmac.compare_digest(want, got)


# ---------- the envelope ----------

CLIENT_FIELDS = ("v", "id", "kind", "to", "from_session", "thread_id", "in_reply_to", "created", "expires", "body",
                 "body_len", "status", "reason", "phase", "ref", "sig")  # what a client may send; anything else is dropped


def new_envelope(kind, to_machine, to_session, body, *, now, expires_in=EXPIRY_SELF, from_session="", thread_id=None,
                 in_reply_to=None, status=None, reason="", phase=None, ref=None, env_id=None):
    env = {
        "v": PROTO, "id": env_id or new_id(), "kind": kind, "to": {"machine": to_machine, "session": to_session},
        "from_session": from_session, "thread_id": thread_id, "in_reply_to": in_reply_to,
        "created": iso(now), "expires": iso(now + expires_in), "body": body, "body_len": len(body or ""),
    }
    if kind == "reply":
        env["status"], env["reason"] = status or "done", reason or ""
    if kind == "receipt":
        env["phase"], env["ref"] = phase, ref
    return env


def client_fields(env):
    """env with only the fields a client may set: its own `stamp` or `from` never reach the hub's logic."""
    return {k: env[k] for k in CLIENT_FIELDS if k in env}


def check_envelope(env, *, now, max_body=MAX_BODY):
    """The problem with a client's envelope as a ProtoError, or None. Time checks (too old, expired, skew) included."""
    if not isinstance(env, dict):
        return ProtoError("bad-envelope", "不是物件")
    if env.get("v") != PROTO:
        return ProtoError("upgrade-required", f"信封版本 {env.get('v')!r}")
    if not is_id(env.get("id")):
        return ProtoError("bad-envelope", "id 不是 uuid")
    kind = env.get("kind")
    if kind not in KINDS:
        return ProtoError("bad-envelope", f"kind {kind!r}")
    to = env.get("to")
    if not isinstance(to, dict):
        return ProtoError("bad-envelope", "沒有 to")
    if "person" in to:
        return ProtoError("unsupported-target")
    if not all(isinstance(to.get(k), str) and to.get(k) for k in ("machine", "session")):
        return ProtoError("bad-envelope", "to 要有 machine 與 session")
    body = env.get("body")
    if kind in ("message", "reply"):
        if not isinstance(body, str):
            return ProtoError("bad-envelope", "body 要是字串")
        if len(body) > max_body:
            return ProtoError("too-large", f"{len(body)} 字，上限 {max_body}")
    elif body not in (None, ""):
        return ProtoError("bad-envelope", "receipt 不帶 body")
    if kind == "message" and not body.strip():
        return ProtoError("bad-envelope", "訊息是空的")
    if kind == "reply" and env.get("status") not in REPLY_STATUS:
        return ProtoError("bad-envelope", "reply 要有 status")
    if kind == "receipt" and env.get("phase") not in RECEIPT_PHASES:
        return ProtoError("bad-envelope", "receipt 要有 phase")
    if kind in ("reply", "receipt") and not is_id(env.get("in_reply_to") if kind == "reply" else env.get("ref")):
        return ProtoError("bad-envelope", "reply／receipt 要指向原信封")
    fs = env.get("from_session", "")
    if not isinstance(fs, str) or len(fs) > MAX_FROM_SESSION or any(ord(c) < 32 for c in fs):
        return ProtoError("bad-envelope", "from_session 不合規")
    created, expires = parse_time(env.get("created")), parse_time(env.get("expires"))
    if created is None or expires is None or expires <= created:
        return ProtoError("bad-envelope", "created／expires 不合規")
    if created - now > SKEW_REJECT:
        return ProtoError("clock-skew", "created 在未來太遠")
    if now - created > MAX_AGE:
        return ProtoError("too-old")
    if expires <= now:
        return ProtoError("expired")
    return None


# ---------- addressing ----------

NAME_MAX = 64


def valid_name(text):
    """A machine or person name: not empty, no slash, no control characters, at most NAME_MAX characters."""
    return (isinstance(text, str) and 0 < len(text.strip()) <= NAME_MAX and text == text.strip()
            and "/" not in text and not any(ord(c) < 32 or ord(c) == 127 for c in text))


def parse_target(text):
    """("device", machine, session) for `machine/session`, ("person", name) for `@name`, None for anything else (a plain
    word is the caller's business: a local session first, then a person)."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith("@"):
        name = text[1:]
        return ("person", name) if valid_name(name) else None
    if "/" in text:
        machine, _, session = text.partition("/")
        if valid_name(machine) and session and session == session.strip() and not any(ord(c) < 32 for c in session):
            return ("device", machine, session)
    return None


def resolve_target(text, local_names, person_names=()):
    """What `text` addresses: ("local", name), ("device", machine, session), ("person", name) or None. A `/` is always a
    remote machine; `@name` is always a person; a plain word is a local session first, then a person (local wins)."""
    parsed = parse_target(text)
    if parsed is not None:
        return parsed
    if "/" in (text or "") or (text or "").startswith("@"):
        return None
    word = (text or "").strip()
    if word in local_names:
        return ("local", word)
    if word in person_names:
        return ("person", word)
    return None


def expires_for(owner_is_self, now):
    return now + (EXPIRY_SELF if owner_is_self else EXPIRY_PERSON)
