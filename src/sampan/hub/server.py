"""The hub over HTTP: a thin layer. The wire is JSON; every answer is JSON or empty; nothing here decides anything the core
does not (it only turns requests into core calls and ProtoErrors into status codes).

http.server on the standard library, one thread per request (a long poll holds its thread). It listens where it is told
(127.0.0.1 behind the tunnel). It never logs a header, a body or a token: a line is the method, the path and the status.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .. import hubproto as hp

REQUEST_TIMEOUT = 15
MAX_CONNECTIONS = 64
PROXY_HEADERS = ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Ray", "X-Forwarded-Host")


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, core, verifier, *, max_poll_wait=hp.POLL_WAIT, require_ua=True, log=None,
                 request_timeout=REQUEST_TIMEOUT, max_connections=MAX_CONNECTIONS, reject_proxied=False):
        super().__init__(address, _Handler)
        self.core, self.verifier, self.max_poll_wait = core, verifier, max_poll_wait
        self.require_ua, self.log = require_ua, log or (lambda line: None)
        self.request_timeout = request_timeout  # seconds a client has to send its request (reading); a long poll waits elsewhere
        self.reject_proxied = reject_proxied  # --dev: a test-token hub must never sit behind a proxy or tunnel
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        """One thread per connection, at most max_connections at once: past that the answer is 503 and the connection
        closes at once, so idle or slow clients cannot use up the threads."""
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def start(self):
        """Serve in a background thread; the thread (stop() ends it)."""
        t = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        t.start()
        self._thread = t
        return t

    def stop(self):
        self.shutdown()
        self.server_close()
        t = getattr(self, "_thread", None)
        if t is not None:
            t.join(5)

    @property
    def url(self):
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class _Handler(BaseHTTPRequestHandler):
    server_version = "sampan-hub"
    sys_version = ""

    def setup(self):
        self.timeout = self.server.request_timeout  # one read waits at most this long...
        super().setup()
        # ...and the whole request (line, headers, body) has this long in total: a client that sends a byte just before
        # each read would give up would otherwise hold its connection, and so one of the few slots, for ever
        self._watchdog = threading.Timer(self.server.request_timeout, self._cut_off)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _cut_off(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _request_read(self):
        """The request has been read in full: stop the clock (a long poll that waits for mail is not a slow client)."""
        self._watchdog.cancel()

    def finish(self):
        self._watchdog.cancel()
        super().finish()

    def log_message(self, *args):  # the default would print the client address and the request line
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError, ConnectionError):
            self.close_connection = True  # a client too slow to finish its request

    # ---------- plumbing ----------

    def _send(self, status, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)
        self.server.log(f"{self.command} {urlsplit(self.path).path} {status}")

    def _error(self, err):
        self._send(err.status, {"error": err.code, "message": err.message, "detail": err.detail},
                   {"WWW-Authenticate": "Bearer"} if err.code == "unauthorized" else None)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise hp.ProtoError("bad-request", "Content-Length") from None
        if length < 0:
            raise hp.ProtoError("bad-request", "Content-Length 不能是負的")
        if length > hp.MAX_REQUEST:
            raise hp.ProtoError("too-large", "請求本文太大")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            raise hp.ProtoError("bad-request", "不是 JSON") from None
        return data

    def _principal(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or len(auth) > 8192:
            raise hp.ProtoError("unauthorized")
        return self.server.verifier(auth[7:].strip())

    def _route(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path == "/health" and self.command == "GET":
            return self._send(200, {"ok": True, "proto": hp.PROTO})
        try:
            if self.server.reject_proxied and any(self.headers.get(h) for h in PROXY_HEADERS):
                raise hp.ProtoError("unauthorized", "--dev 的 hub（測試 token）不能放在代理或 Tunnel 後面")
            if self.server.require_ua and not (self.headers.get("User-Agent") or "").startswith(hp.USER_AGENT_PREFIX + "/"):
                raise hp.ProtoError("bad-request", "User-Agent 不是 sampan-connect")
            handler = ROUTES.get((self.command, path))
            if handler is None:
                raise hp.ProtoError("not-found")
            who = self._principal()
            body = self._body() if self.command == "POST" else {}
            self._request_read()
            result = handler(self.server, who, body, query)
        except hp.ProtoError as e:
            return self._error(e)
        except Exception:  # noqa: BLE001  never leak what went wrong, only that it did
            self.server.log(f"{self.command} {path} 500")
            return self._send(500, {"error": "internal"})
        if result is None:
            return self._send(204)
        return self._send(202 if path == "/v1/send" else 200, result)

    do_GET = do_POST = _route


def _hello(server, who, body, query):
    return server.core.hello(who, body)


def _send(server, who, body, query):
    return server.core.send(who, body)


def _poll(server, who, body, query):
    try:
        wait = float((query.get("wait") or ["0"])[0])
    except ValueError:
        raise hp.ProtoError("bad-request", "wait") from None
    items = server.core.poll(who, min(max(wait, 0.0), server.max_poll_wait))
    return {"items": items} if items else None


def _ack(server, who, body, query):
    if not isinstance(body, dict):
        raise hp.ProtoError("bad-request")
    return server.core.ack(who, body.get("ids"), body.get("result"), str(body.get("reason") or ""))


def _cancel(server, who, body, query):
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise hp.ProtoError("bad-request")
    return server.core.cancel(who, body["id"])


def _peers(server, who, body, query):
    return {"peers": server.core.peers(who)}


ROUTES = {
    ("POST", "/v1/hello"): _hello, ("POST", "/v1/send"): _send, ("GET", "/v1/poll"): _poll,
    ("POST", "/v1/ack"): _ack, ("POST", "/v1/cancel"): _cancel, ("GET", "/v1/peers"): _peers,
}
