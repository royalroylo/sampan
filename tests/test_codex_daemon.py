import base64
import hashlib
import json
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sampan import codex_daemon as cd
from tests.fakes import FakeRunner, cp

NOW = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

RATE_LIMITS = {
    "rateLimits": {"primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1792888099},
                   "secondary": {"usedPercent": 73, "windowDurationMins": 10080, "resetsAt": 1793382353},
                   "rateLimitReachedType": "rate_limit_reached", "planType": "plus"},
    "ordinaryUsageAllowed": False, "accountId": "acct-redacted"}
THREADS = {"data": [
    {"id": "t1", "name": "", "preview": "fix the parser", "cwd": "/w/a", "originator": "codex-tui", "source": "cli",
     "parentThreadId": None, "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
     "updatedAt": 1792888000, "path": "/h/rollout-t1.jsonl"},
    {"id": "t2", "name": "docs", "preview": "p", "cwd": "/w/b", "originator": "Codex Desktop",
     "source": {"subAgent": {"x": 1}}, "parentThreadId": "t1", "status": {"type": "idle"}, "updatedAt": 1792887000}],
    "nextCursor": None}
RESULTS = {"initialize": {"userAgent": "x"}, "account/rateLimits/read": RATE_LIMITS, "thread/list": THREADS,
           "thread/loaded/list": {"data": ["t1"], "nextCursor": None}}


def ws_frame(text, opcode=1, fin=True):
    """An unmasked server frame, with the extended length forms when needed."""
    data = text.encode() if isinstance(text, str) else text
    head = bytes([(0x80 if fin else 0) | opcode])
    if len(data) < 126:
        return head + bytes([len(data)]) + data
    if len(data) < 1 << 16:
        return head + bytes([126]) + struct.pack(">H", len(data)) + data
    return head + bytes([127]) + struct.pack(">Q", len(data)) + data


class FakeDaemon:
    """A WebSocket JSON-RPC server on a unix socket in a short temp dir, serving one connection on a thread.
    Options: results (method → result), notify (push a no-id frame before each reply), silent_after (stop replying
    once this method arrives), drop_after (close after answering this method), upgrade (answer the handshake with 101),
    split (send replies as continuation fragments), pings (ping before each reply), errors (methods answered with a
    JSON-RPC error)."""

    def __init__(self, results=None, notify=False, silent_after=None, drop_after=None, upgrade=True, split=False,
                 pings=False, errors=()):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.path = Path(self.dir) / "d.sock"
        self.results = dict(RESULTS if results is None else results)
        self.notify, self.silent_after, self.drop_after = notify, silent_after, drop_after
        self.upgrade, self.split, self.pings, self.errors = upgrade, split, pings, set(errors)
        self.methods, self.unmasked, self.raw_first = [], 0, b""
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self.server.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _read_frame(self, conn, buf):
        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = conn.recv(65536)
                if not chunk:
                    raise EOFError
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out
        b0, b1 = need(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", need(2))[0]
        if not b1 & 0x80:
            self.unmasked += 1
        key = need(4)
        data = bytes(b ^ key[i % 4] for i, b in enumerate(need(n)))
        return b0 & 0x0F, data, buf

    def _serve(self):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, _, buf = buf.partition(b"\r\n\r\n")
            self.raw_first = head
            if not self.upgrade:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                try:  # a client that sent JSON anyway would show up here
                    conn.settimeout(0.3)
                    self.methods.append(("raw", conn.recv(4096)))
                except OSError:
                    pass
                return
            key = next(ln.split(":", 1)[1].strip() for ln in head.decode().split("\r\n") if ln.lower().startswith("sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            try:
                while True:
                    op, data, buf = self._read_frame(conn, buf)
                    if op == 8:
                        return
                    if op == 10:
                        continue
                    msg = json.loads(data)
                    self.methods.append(msg["method"])
                    if "id" not in msg:
                        continue
                    method = msg["method"]
                    if self.silent_after == method:
                        time.sleep(5)
                        return
                    if self.notify:
                        conn.sendall(ws_frame(json.dumps({"jsonrpc": "2.0", "method": "thread/status/changed"})))
                    if self.pings:
                        conn.sendall(ws_frame(b"hi", opcode=9))
                    if method in self.errors:
                        reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "no"}}
                    else:
                        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": self.results.get(method)}
                    text = json.dumps(reply)
                    if self.split:
                        mid = len(text) // 2
                        conn.sendall(ws_frame(text[:mid], fin=False) + ws_frame(text[mid:].encode(), opcode=0))
                    else:
                        conn.sendall(ws_frame(text))
                    if self.drop_after == method:
                        return
            except (EOFError, OSError):
                return


class DaemonCase(unittest.TestCase):
    def daemon(self, **kw):
        d = FakeDaemon(**kw)
        self.addCleanup(d.close)
        return d

    def snap(self, d, timeout=2.0):
        return cd.snapshot(Path(d.dir) / "home", FakeRunner(strict=True).on(
            ["codex", "app-server", "daemon", "version"], cp(out=json.dumps({"socketPath": str(d.path)}))),
            now=NOW, timeout=timeout)


class SnapshotTests(DaemonCase):
    def check_full(self, snap):
        self.assertEqual(snap.usage.five_hour.used, 100.0)
        self.assertEqual(snap.usage.week.used, 73.0)
        self.assertTrue(snap.usage.reached)
        self.assertEqual(len(snap.threads), 2)
        self.assertEqual(snap.loaded, frozenset({"t1"}))
        self.assertEqual(snap.at, NOW)

    def test_snapshot_reads_usage_threads_and_loaded(self):
        d = self.daemon()
        snap = self.snap(d)
        self.check_full(snap)
        t1, t2 = snap.threads
        self.assertEqual((t1.name, t1.status, t1.flags, t1.source, t1.parent_id, t1.path),
                         ("fix the parser", "active", ("waitingOnApproval",), "cli", None, "/h/rollout-t1.jsonl"))
        self.assertEqual((t2.name, t2.status, t2.source, t2.parent_id, t2.path),
                         ("docs", "idle", "subAgent", "t1", None))
        self.assertEqual(t1.updated_at, datetime.fromtimestamp(1792888000, timezone.utc))
        self.assertEqual(d.unmasked, 0)
        self.assertNotIn("acct", repr(snap))

    def test_requests_only_allowed_methods(self):
        d = self.daemon()
        self.snap(d)
        self.assertEqual(d.methods, ["initialize", "initialized", "account/rateLimits/read", "thread/list",
                                     "thread/loaded/list"])
        self.assertLessEqual(set(d.methods), cd.ALLOWED)

    def test_request_refuses_other_methods(self):
        d = self.daemon()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.connect(str(d.path))
        conn = cd._Conn(sock, time.monotonic() + 2)
        cd._handshake(conn)
        with self.assertRaises(ValueError):
            cd._request(conn, "turn/start", {})
        with self.assertRaises(ValueError):
            cd._notify(conn, "thread/resume")
        time.sleep(0.1)
        self.assertEqual(d.methods, [])

    def test_notifications_between_responses_are_skipped(self):
        self.check_full(self.snap(self.daemon(notify=True)))

    def test_pings_are_answered_and_fragments_joined(self):
        self.check_full(self.snap(self.daemon(pings=True, split=True)))

    def test_long_replies_use_extended_lengths(self):
        threads = {"data": [{"id": f"t{i}", "name": "n" * 40, "updatedAt": 1792888000} for i in range(50)]}
        d = self.daemon(results={**RESULTS, "thread/list": threads})
        snap = self.snap(d)
        self.assertEqual(len(snap.threads), 50)  # > 125 bytes: 16-bit length form
        big = {"data": [{"id": f"t{i}", "name": "n" * 1000, "updatedAt": 1792888000} for i in range(100)]}
        d = self.daemon(results={**RESULTS, "thread/list": big})
        self.assertEqual(len(self.snap(d).threads), 100)  # > 64 KiB: 64-bit length form

    def test_silent_server_times_out_within_budget(self):
        d = self.daemon(silent_after="initialize")
        t = time.monotonic()
        self.assertIsNone(self.snap(d, timeout=0.3))
        self.assertLess(time.monotonic() - t, 1.0)

    def test_dropped_connection_returns_none(self):
        self.assertIsNone(self.snap(self.daemon(drop_after="initialize")))

    def test_plain_json_without_upgrade_is_not_sent(self):
        d = self.daemon(upgrade=False)
        self.assertIsNone(self.snap(d))
        self.assertEqual(d.methods, [])
        self.assertTrue(d.raw_first.startswith(b"GET / HTTP/1.1"))

    def test_rpc_error_on_required_method_returns_none(self):
        self.assertIsNone(self.snap(self.daemon(errors=["thread/list"])))
        self.assertIsNone(self.snap(self.daemon(errors=["account/rateLimits/read"])))

    def test_failed_loaded_list_leaves_loaded_empty(self):
        snap = self.snap(self.daemon(errors=["thread/loaded/list"]))
        self.assertEqual(snap.loaded, frozenset())
        self.assertEqual(len(snap.threads), 2)

    def test_no_socket_returns_none(self):
        home = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        self.assertIsNone(cd.snapshot(home, FakeRunner().on(["codex"], cp(rc=1)), now=NOW))


class ParserTests(unittest.TestCase):
    def test_malformed_fields_are_skipped(self):
        threads = cd.parse_threads({"data": [
            {"id": "a", "status": "idle", "updatedAt": 1792888000},
            {"id": "b", "status": {"type": "idle"}, "updatedAt": "x"},
            {"id": "c", "updatedAt": True}, {"id": 5, "updatedAt": 1}, "junk"]})
        self.assertEqual([(t.id, t.status) for t in threads], [("a", "unknown")])
        self.assertEqual(cd.parse_threads(None), ())
        self.assertIsNone(cd.parse_rate_limits({"rateLimits": None}, NOW))
        self.assertIsNone(cd.parse_rate_limits("x", NOW))
        u = cd.parse_rate_limits({"rateLimits": {
            "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 10 ** 20},
            "secondary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1793382353}}}, NOW)
        self.assertIsNone(u.five_hour)
        self.assertEqual(u.week.used, 7.0)
        self.assertFalse(u.reached)

    def test_reached_flags(self):
        win = {"primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1792888099}}
        self.assertFalse(cd.parse_rate_limits({"rateLimits": {**win, "rateLimitReachedType": None}}, NOW).reached)
        self.assertTrue(cd.parse_rate_limits({"rateLimits": win, "ordinaryUsageAllowed": False}, NOW).reached)
        self.assertTrue(cd.parse_rate_limits({"rateLimits": {"rateLimitReachedType": "x"}}, NOW).reached)

    def test_windows_are_told_apart_by_duration_not_position(self):
        u = cd.parse_rate_limits({"rateLimits": {
            "primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1793382353}}}, NOW)
        self.assertIsNone(u.five_hour)
        self.assertEqual(u.week.used, 40.0)


class SocketPathTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_socket_path_prefers_default_then_version(self):
        default = self.home / cd.SOCKET_REL
        default.parent.mkdir(parents=True)
        default.touch()
        run = FakeRunner(strict=True)
        self.assertEqual(cd.socket_path(self.home, run), default)
        self.assertEqual(run.calls, [])
        default.unlink()
        other = self.home / "x.sock"
        other.touch()
        run.on(["codex", "app-server", "daemon", "version"], cp(out=json.dumps({"socketPath": str(other)})))
        self.assertEqual(cd.socket_path(self.home, run), other)
        call = run.calls[0]
        self.assertEqual(call["env"]["CODEX_HOME"], str(self.home))
        self.assertIsNotNone(call["timeout"])

    def test_socket_path_failures_are_none(self):
        base = ["codex", "app-server", "daemon", "version"]
        self.assertIsNone(cd.socket_path(self.home, FakeRunner(strict=True).on(base, cp(rc=1))))
        self.assertIsNone(cd.socket_path(self.home, FakeRunner(strict=True).on(base, cp(out="not json"))))
        gone = json.dumps({"socketPath": str(self.home / "gone.sock")})
        self.assertIsNone(cd.socket_path(self.home, FakeRunner(strict=True).on(base, cp(out=gone))))


if __name__ == "__main__":
    unittest.main()
