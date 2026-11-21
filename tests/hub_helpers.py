"""Shared by the hub tests: a clock that is moved by hand, a hub on a temp file, and two approved machines."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan import hubproto as hp
from sampan.hub.core import HubCore, Principal

START = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
KEY = b"o" * 32
KEYS = {"k1": KEY}


class Clock:
    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def principal(name):
    return Principal(f"client-{name}", f"sub-{name}")


def hello_payload(name, published=("proj-a",), **kw):
    return {"machine_id": f"id-{name}", "name": name, "version": "0.26.0", "proto_min": 1, "proto_max": 1,
            "published": [{"session": s, "alias": "", "status": "idle"} for s in published], **kw}


def new_hub(clock=None, limits=None, path=None):
    clock = clock or Clock()
    path = path or Path(tempfile.mkdtemp()) / "hub.db"
    return HubCore(path, clock=clock, limits=limits), clock, path


def hub_with_machines(names=("laptop", "desktop"), clock=None, limits=None, published=("proj-a",)):
    hub, clock, path = new_hub(clock, limits)
    for n in names:
        hub.hello(principal(n), hello_payload(n, published))
        hub.approve(n)
        hub.hello(principal(n), hello_payload(n, published))  # published is only kept once active
    return hub, clock, path


def message(clock, to="desktop", session="proj-a", body="hello", sender="laptop", **kw):
    env = hp.new_envelope("message", f"id-{to}", session, body, now=clock(), from_session="proj-b", **kw)
    return hp.sign(env, KEYS, "k1", f"id-{sender}")


class LocalTransport:
    """The client's transport, wired straight to a HubCore (no sockets), with faults that can be switched on:
    fail(call, n): the next n calls of that kind raise TransportError before reaching the hub;
    lose_reply(call, n): the call reaches the hub and takes effect, but the answer is lost (TransportError);
    extra_poll: envelopes handed out one more time in the next poll (a duplicate delivery)."""

    def __init__(self, hub, who):
        from sampan.connect import TransportError
        self.hub, self.who, self.TransportError = hub, who, TransportError
        self.faults, self.lost, self.extra_poll, self.calls = {}, {}, [], []

    def fail(self, call, n=1):
        self.faults[call] = n

    def lose_reply(self, call, n=1):
        self.lost[call] = n

    def _go(self, call, fn):
        self.calls.append(call)
        if self.faults.get(call, 0) > 0:
            self.faults[call] -= 1
            raise self.TransportError(f"{call}: connection refused")
        out = fn()
        if self.lost.get(call, 0) > 0:
            self.lost[call] -= 1
            raise self.TransportError(f"{call}: timed out")
        return out

    def hello(self, payload):
        return self._go("hello", lambda: self.hub.hello(self.who, payload))

    def send(self, env):
        return self._go("send", lambda: self.hub.send(self.who, env))

    def poll(self, wait=0):
        def go():
            items = self.hub.fetch(self.who)
            items = items + self.extra_poll
            self.extra_poll = []
            return items
        return self._go("poll", go)

    def ack(self, ids, result, reason=""):
        return self._go("ack", lambda: self.hub.ack(self.who, ids, result, reason))

    def cancel(self, env_id):
        return self._go("cancel", lambda: self.hub.cancel(self.who, env_id))

    def peers(self):
        return self._go("peers", lambda: self.hub.peers(self.who))


class StubIdp:
    """A stand-in for the identity provider's token endpoint (OAuth client credentials), on a loopback port. Hands out
    `stub.<client>.<sub>` tokens (what hub.auth.StubVerifier accepts) and records how it was called."""

    def __init__(self, clients, expires_in=300):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.clients, self.expires_in, self.requests, self.mode = dict(clients), expires_in, [], "ok"
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                from urllib.parse import parse_qs
                form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                idp.requests.append({"ua": self.headers.get("User-Agent"), "headers": dict(self.headers.items()),
                                     "client": (form.get("client_id") or [""])[0]})
                if idp.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://login.example.com/")
                    self.end_headers()
                    return
                if idp.mode == "html":
                    body = b"<html>Sign in</html>"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                client, secret = (form.get("client_id") or [""])[0], (form.get("client_secret") or [""])[0]
                if idp.clients.get(client) != secret:
                    body = json.dumps({"error": "unauthorized_client"}).encode()
                    self.send_response(401)
                else:
                    body = json.dumps({"access_token": f"stub.{client}.sub-{client}", "expires_in": idp.expires_in,
                                       "token_type": "Bearer"}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/token"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
