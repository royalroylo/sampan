"""Read-only client for the Codex app-server daemon (`codex app-server daemon`): 額度 and thread list, nothing else.
The daemon's control socket speaks JSON-RPC 2.0 in WebSocket text frames over a unix socket; this module is a minimal
stdlib WebSocket client for it. Only ALLOWED methods are ever sent (no turn/start, no thread/resume …), the whole
exchange is bounded by one deadline, and every failure is None — a missing or stuck daemon must never block sampan.
Never reads credentials; the account id the daemon returns is neither logged nor kept."""
import base64
import hashlib
import json
import os
import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .update import current_version
from .usage import CODEX_WINDOWS, Usage, _window

SOCKET_REL = Path("app-server-control/app-server-control.sock")
ALLOWED = frozenset({"initialize", "initialized", "thread/list", "thread/loaded/list", "account/rateLimits/read"})
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 handshake constant
_MAX_FRAME = 8 * 1024 * 1024  # a reply larger than this is not from a sane daemon


@dataclass(frozen=True)
class CodexThread:
    id: str
    name: str  # name, else preview
    cwd: str
    originator: str  # "Codex Desktop" | "codex-tui" | "Claude Code" …
    source: str  # "vscode" / "cli" …; an object source is reduced to its first key
    parent_id: str | None
    status: str  # notLoaded | idle | systemError | active; "unknown" when absent
    flags: tuple[str, ...]  # activeFlags: waitingOnApproval / waitingOnUserInput
    updated_at: datetime
    path: str | None  # rollout path


@dataclass(frozen=True)
class DaemonSnapshot:
    home: Path
    usage: Usage | None
    threads: tuple[CodexThread, ...]
    loaded: frozenset[str]  # ids of threads the daemon has in memory
    at: datetime


# ---------- parsers (pure) ----------

def parse_rate_limits(result, at):
    """Usage from an account/rateLimits/read result; None when rateLimits is absent or holds nothing usable.
    Windows are told apart by windowDurationMins (never by primary/secondary position)."""
    limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        return None
    found = {}
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if not isinstance(w, dict):
            continue
        mins = w.get("windowDurationMins")
        field = CODEX_WINDOWS.get(mins) if isinstance(mins, int) and not isinstance(mins, bool) else None
        win = _window(w.get("usedPercent"), w.get("resetsAt"))
        if field and win and field not in found:
            found[field] = win
    reached = limits.get("rateLimitReachedType") is not None or result.get("ordinaryUsageAllowed") is False
    if not found and not reached:
        return None
    return Usage(found.get("five_hour"), found.get("week"), at, reached)


def _str(value):
    return value if isinstance(value, str) else ""


def _thread(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    ts = raw.get("updatedAt")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        updated = datetime.fromtimestamp(ts, timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None
    status = raw.get("status")
    kind = status.get("type") if isinstance(status, dict) else None
    flags = status.get("activeFlags") if isinstance(status, dict) else None
    source = raw.get("source")
    if isinstance(source, dict):
        source = next(iter(source), "")
    parent, path = raw.get("parentThreadId"), raw.get("path")
    return CodexThread(
        id=raw["id"], name=_str(raw.get("name")) or _str(raw.get("preview")), cwd=_str(raw.get("cwd")),
        originator=_str(raw.get("originator")), source=_str(source),
        parent_id=parent if isinstance(parent, str) else None,
        status=kind if isinstance(kind, str) and kind else "unknown",
        flags=tuple(f for f in flags if isinstance(f, str)) if isinstance(flags, list) else (),
        updated_at=updated, path=path if isinstance(path, str) else None)


def parse_threads(result):
    """CodexThread tuple from a thread/list result; an entry without an id or a numeric updatedAt (seconds) is skipped."""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        return ()
    return tuple(t for t in map(_thread, data) if t is not None)


def _loaded_ids(result):
    data = result.get("data") if isinstance(result, dict) else None
    return frozenset(i for i in data if isinstance(i, str)) if isinstance(data, list) else frozenset()


# ---------- socket location ----------

def socket_path(codex_home, run, timeout=2.0):
    """The daemon's control socket: <codex_home>/app-server-control/app-server-control.sock when it exists, else the
    socketPath `codex app-server daemon version` reports (if that file exists); None when neither. The version
    command gets at most `timeout` seconds, and is not run at all when none are left."""
    default = Path(codex_home) / SOCKET_REL
    if default.exists():
        return default
    if timeout <= 0:
        return None
    try:
        r = run(["codex", "app-server", "daemon", "version"], env={**os.environ, "CODEX_HOME": str(codex_home)},
                timeout=timeout)
        if r.returncode != 0:
            return None
        path = json.loads(r.stdout).get("socketPath")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(path) if isinstance(path, str) and path and Path(path).exists() else None


# ---------- minimal WebSocket client ----------

class _Conn:
    """A connected socket plus a receive buffer, all reads and writes bounded by one absolute deadline."""

    def __init__(self, sock, deadline):
        self.sock, self.deadline, self.buf, self.next_id = sock, deadline, b"", 0

    def _arm(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline")
        self.sock.settimeout(left)

    def send(self, data):
        self._arm()
        self.sock.sendall(data)

    def recv_until(self, marker):
        while marker not in self.buf:
            self._fill()
        head, _, self.buf = self.buf.partition(marker)
        return head

    def recv_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _fill(self):
        self._arm()
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed")
        self.buf += chunk
        if len(self.buf) > _MAX_FRAME:
            raise ValueError("reply too large")


def _frame(opcode, payload):
    """One masked, final client frame (RFC 6455 requires clients to mask)."""
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _handshake(conn):
    key = base64.b64encode(os.urandom(16)).decode()
    conn.send(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    lines = conn.recv_until(b"\r\n\r\n").decode("latin-1").split("\r\n")
    if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
        raise ConnectionError("no websocket upgrade")
    headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    if headers.get("sec-websocket-accept") != want:
        raise ConnectionError("bad handshake")


def _recv_message(conn):
    """The next complete text message (continuation frames joined); answers ping, raises ConnectionError on close."""
    parts = []
    while True:
        b0, b1 = conn.recv_exact(2)
        opcode, n = b0 & 0x0F, b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", conn.recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", conn.recv_exact(8))[0]
        if n > _MAX_FRAME:
            raise ValueError("frame too large")
        key = conn.recv_exact(4) if b1 & 0x80 else None
        payload = conn.recv_exact(n)
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if opcode == 8:
            raise ConnectionError("closed by server")
        if opcode == 9:
            conn.send(_frame(10, payload))
            continue
        if opcode == 10:
            continue
        parts.append(payload)
        if b0 & 0x80:
            return b"".join(parts).decode()


def _send_json(conn, message):
    conn.send(_frame(1, json.dumps(message, separators=(",", ":")).encode()))


def _request(conn, method, params):
    """Send one allow-listed JSON-RPC request and return its result, skipping frames without an id (server
    notifications); ValueError for a method outside ALLOWED (nothing is sent) or a JSON-RPC error reply."""
    if method not in ALLOWED:
        raise ValueError(f"method not allowed: {method}")
    conn.next_id += 1
    rid = conn.next_id
    _send_json(conn, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        msg = json.loads(_recv_message(conn))
        if not isinstance(msg, dict) or msg.get("id") != rid:
            continue  # notification, or a reply to something we did not ask
        if "error" in msg:
            raise ValueError(f"{method} failed")
        return msg.get("result")


def _notify(conn, method):
    if method not in ALLOWED:
        raise ValueError(f"method not allowed: {method}")
    _send_json(conn, {"jsonrpc": "2.0", "method": method})


def _connect_unix(path, timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(path))
    except BaseException:
        sock.close()
        raise
    return sock


def snapshot(codex_home, run, *, now, timeout=3.0, connect=None):
    """One read of the daemon: 額度, thread list and loaded ids, within `timeout` seconds in all. None when there is
    no daemon or initialize / rateLimits / thread/list fails (a failed thread/loaded/list only leaves `loaded` empty).
    `connect(path, timeout)` returns a connected socket (tests inject their own)."""
    deadline = time.monotonic() + timeout
    path = socket_path(codex_home, run, min(2.0, max(0.0, deadline - time.monotonic())))
    if path is None:
        return None
    try:
        sock = (connect or _connect_unix)(path, max(0.0, deadline - time.monotonic()))
    except OSError:
        return None
    try:
        conn = _Conn(sock, deadline)
        _handshake(conn)
        _request(conn, "initialize", {"clientInfo": {"name": "sampan", "version": current_version()}})
        _notify(conn, "initialized")
        limits = _request(conn, "account/rateLimits/read", {})
        threads = _request(conn, "thread/list", {"limit": 50, "sortKey": "updated_at"})
        try:
            loaded = _loaded_ids(_request(conn, "thread/loaded/list", {}))
        except (OSError, ValueError):
            loaded = frozenset()
        return DaemonSnapshot(Path(codex_home), parse_rate_limits(limits, now), parse_threads(threads), loaded, now)
    except (OSError, ValueError):  # ConnectionError / TimeoutError are OSError; bad JSON / UnicodeDecodeError / RPC error are ValueError
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass
