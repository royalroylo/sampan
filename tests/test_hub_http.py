import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sampan import hubproto as hp
from sampan.connect import Connector, Hooks, TransportError
from sampan.hub.auth import StubVerifier
from sampan.hub.server import HubServer
from sampan.hubhttp import (
    HttpTransport,
    TokenSource,
    access_headers,
    check_url,
    load_credentials,
    request_json,
    transport_from_credentials,
)
from sampan.remote_store import RemoteStore
from tests.hub_helpers import KEYS, Clock, StubIdp, hello_payload, new_hub, principal

UA = hp.user_agent("0.26.0")


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hub, _, _ = new_hub(self.clock)
        self.logs = []
        self.server = HubServer(("127.0.0.1", 0), self.hub, StubVerifier(), max_poll_wait=0.3, log=self.logs.append)
        self.server.start()
        self.idp = StubIdp({"client-laptop": "s1", "client-desktop": "s2"})
        self.addCleanup(self.server.stop)
        self.addCleanup(self.idp.close)

    def transport(self, name, **kw):
        token = TokenSource(self.idp.url, f"client-{name}", {"laptop": "s1", "desktop": "s2"}[name], clock=self.clock, ua=UA)
        return HttpTransport(self.server.url, token, ua=UA, **kw)

    def raw(self, path, *, method="GET", body=None, headers=None):
        req = urllib.request.Request(self.server.url + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def approve_both(self):
        for n in ("laptop", "desktop"):
            t = self.transport(n)
            t.hello(hello_payload(n))
            self.hub.approve(n)
            t.hello(hello_payload(n))


class EndpointTest(HttpCase):
    def test_health_needs_no_login_and_says_nothing_else(self):
        status, body = self.raw("/health")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "proto": 1}))

    def test_no_token_or_a_bad_one_is_401(self):
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
            status, body = self.raw("/v1/peers", headers={"User-Agent": UA, **headers})
            self.assertEqual((status, json.loads(body)["error"]), (401, "unauthorized"), headers)

    def test_the_wrong_user_agent_is_refused(self):
        status, body = self.raw("/v1/peers", headers={"User-Agent": "Python-urllib/3.12", "Authorization": "Bearer stub.client-laptop.x"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "bad-request")

    def test_unknown_paths_and_methods(self):
        h = {"User-Agent": UA, "Authorization": "Bearer stub.client-laptop.x"}
        self.assertEqual(self.raw("/v1/nothing", headers=h)[0], 404)
        self.assertEqual(self.raw("/v1/send", headers=h)[0], 404)  # a GET to a POST endpoint

    def test_bad_bodies(self):
        h = {"User-Agent": UA, "Authorization": "Bearer stub.client-laptop.x"}
        self.assertEqual(self.raw("/v1/hello", method="POST", body=b"{not json", headers=h)[0], 400)
        self.assertEqual(self.raw("/v1/hello", method="POST", body=b"x" * (hp.MAX_REQUEST + 1), headers=h)[0], 413)
        status, body = self.raw("/v1/hello", method="POST", body=b"[]", headers=h)
        self.assertEqual((status, json.loads(body)["error"]), (400, "bad-request"))

    def test_an_internal_error_says_only_that_it_happened(self):
        self.hub.fetch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SECRET-DETAIL /Users/x/hub.db"))
        self.approve_both()
        status, body = self.raw("/v1/poll?wait=0", headers={"User-Agent": UA, "Authorization": "Bearer stub.client-desktop.x"})
        self.assertEqual((status, json.loads(body)), (500, {"error": "internal"}))
        self.assertFalse(any("SECRET" in line for line in self.logs))

    def test_the_log_has_no_tokens_bodies_or_headers(self):
        t = self.transport("laptop")
        t.hello(hello_payload("laptop"))
        with self.assertRaises(hp.ProtoError):
            t.send({"v": 1, "id": hp.new_id(), "kind": "message", "body": "PRIVATE-BODY-TEXT"})
        joined = "\n".join(self.logs)
        for needle in ("stub.client-laptop", "PRIVATE", "Bearer", UA, "127.0.0.1"):
            self.assertNotIn(needle, joined)
        self.assertTrue(all(line.split()[0] in ("GET", "POST") for line in self.logs))


class HardeningTest(unittest.TestCase):
    def setUp(self):
        self.hub, self.clock, _ = new_hub(Clock())
        self.servers = []

    def server(self, **kw):
        srv = HubServer(("127.0.0.1", 0), self.hub, StubVerifier(), max_poll_wait=0.2, **kw)
        srv.start()
        self.servers.append(srv)
        self.addCleanup(srv.stop)
        return srv

    def raw(self, srv, data, *, read=True, wait=2.0):
        s = socket.create_connection(srv.server_address[:2], timeout=wait)
        s.sendall(data)
        out = b""
        if read:
            try:
                while chunk := s.recv(4096):
                    out += chunk
            except (socket.timeout, ConnectionError):
                pass
        s.close()
        return out

    def test_a_negative_content_length_is_refused_at_once(self):
        srv = self.server()
        out = self.raw(srv, b"POST /v1/hello HTTP/1.0\r\nUser-Agent: " + UA.encode() + b"\r\nAuthorization: Bearer stub.c.s\r\n"
                       b"Content-Length: -1\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.0 400"), out[:40])

    def test_a_client_that_never_finishes_its_request_is_cut_off(self):
        srv = self.server(request_timeout=0.4)
        started = time.monotonic()
        out = self.raw(srv, b"POST /v1/hello HTTP/1.0\r\nUser-Agent: x\r\n", wait=5)  # headers never end
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(out, b"")  # closed without an answer

    def test_a_client_that_drips_a_byte_at_a_time_is_cut_off_by_the_total_deadline(self):
        # each read is quick (a byte every 0.15 s, under the per-read timeout), but the request as a whole is not
        srv = self.server(request_timeout=0.6)
        sock = socket.create_connection(srv.server_address[:2], timeout=5)
        started = time.monotonic()
        closed = False
        try:
            for ch in b"POST /v1/hello HTTP/1.0\r\nUser-Agent: x\r\nX-Pad: " + b"a" * 60:
                sock.sendall(bytes([ch]))
                time.sleep(0.15)
                if time.monotonic() - started > 4:
                    break
        except OSError:
            closed = True
        finally:
            sock.close()
        elapsed = time.monotonic() - started
        self.assertTrue(closed or elapsed < 4, "the connection was never cut")
        self.assertLess(elapsed, 3.5)

    def test_the_slot_of_a_slow_client_comes_back(self):
        srv = self.server(max_connections=1, request_timeout=0.4)
        slow = socket.create_connection(srv.server_address[:2])
        time.sleep(0.1)
        self.assertTrue(self.raw(srv, b"GET /health HTTP/1.0\r\n\r\n").startswith(b"HTTP/1.0 503"))
        time.sleep(0.8)  # the watchdog has closed the slow one
        slow.close()
        self.assertTrue(self.raw(srv, b"GET /health HTTP/1.0\r\n\r\n").startswith(b"HTTP/1.0 200"))

    def test_a_body_that_never_arrives_is_cut_off(self):
        srv = self.server(request_timeout=0.4)
        started = time.monotonic()
        self.raw(srv, b"POST /v1/hello HTTP/1.0\r\nUser-Agent: " + UA.encode() + b"\r\nAuthorization: Bearer stub.c.s\r\n"
                 b"Content-Length: 100\r\n\r\n{", wait=5)
        self.assertLess(time.monotonic() - started, 3)

    def test_connections_past_the_limit_get_503_and_the_server_keeps_serving(self):
        srv = self.server(max_connections=2, request_timeout=5)
        idle = [socket.create_connection(srv.server_address[:2]) for _ in range(2)]  # two connections that say nothing
        try:
            time.sleep(0.2)
            out = self.raw(srv, b"GET /health HTTP/1.0\r\n\r\n")
            self.assertTrue(out.startswith(b"HTTP/1.0 503"), out[:40])
        finally:
            for s in idle:
                s.close()
        time.sleep(0.3)
        self.assertTrue(self.raw(srv, b"GET /health HTTP/1.0\r\n\r\n").startswith(b"HTTP/1.0 200"))

    def test_a_slow_client_does_not_hold_a_long_poll_hostage(self):
        # the read timeout covers the request only: a poll that waits for mail is not cut by it
        srv = self.server(request_timeout=0.3)
        for n in ("laptop",):
            self.hub.hello(principal(n), hello_payload(n))
            self.hub.approve(n)
        req = (b"GET /v1/poll?wait=1 HTTP/1.0\r\nUser-Agent: " + UA.encode() + b"\r\nAuthorization: Bearer stub.client-laptop.s\r\n\r\n")
        out = self.raw(srv, req, wait=5)
        self.assertTrue(out.startswith(b"HTTP/1.0 204"), out[:40])

    def test_dev_mode_refuses_anything_that_came_through_a_proxy(self):
        srv = self.server(reject_proxied=True)
        for header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Ray"):
            req = (f"GET /v1/peers HTTP/1.0\r\nUser-Agent: {UA}\r\nAuthorization: Bearer stub.client-laptop.s\r\n{header}: 1.2.3.4\r\n\r\n").encode()
            out = self.raw(srv, req)
            self.assertTrue(out.startswith(b"HTTP/1.0 401"), (header, out[:40]))
            self.assertIn(b"--dev", out)
        direct = self.raw(srv, "GET /health HTTP/1.0\r\nCF-Connecting-IP: 1.1.1.1\r\n\r\n".encode())
        self.assertTrue(direct.startswith(b"HTTP/1.0 200"))  # /health says nothing, whoever asks

    def test_without_dev_mode_proxy_headers_are_normal(self):
        srv = self.server(reject_proxied=False)
        req = (f"GET /v1/peers HTTP/1.0\r\nUser-Agent: {UA}\r\nAuthorization: Bearer stub.client-laptop.s\r\nCF-Connecting-IP: 1.2.3.4\r\n\r\n").encode()
        self.assertNotIn(b"--dev", self.raw(srv, req))


class TransportTest(HttpCase):
    def test_a_message_goes_through_over_http(self):
        self.approve_both()
        a, b = self.transport("laptop"), self.transport("desktop")
        env = hp.sign(hp.new_envelope("message", "id-desktop", "proj-a", "héllo 讀書", now=self.clock(), from_session="proj-b"),
                      KEYS, "k1", "id-laptop")
        self.assertEqual(a.send(env)["duplicate"], False)
        self.assertEqual(a.send(env)["duplicate"], True)
        [got] = b.poll(0)
        self.assertEqual((got["body"], got["stamp"]["sender_machine_name"]), ("héllo 讀書", "laptop"))
        self.assertEqual(b.ack([got["id"]], "taken"), {"acked": 1})
        [receipt] = a.poll(0)
        self.assertEqual((receipt["kind"], receipt["phase"]), ("receipt", "taken"))
        self.assertEqual(a.cancel(env["id"])["state"], "taken")
        self.assertEqual([p["name"] for p in a.peers()], ["desktop"])

    def test_poll_waits_then_answers_nothing(self):
        self.approve_both()
        self.assertEqual(self.transport("desktop").poll(5), [])  # the server caps the wait at 0.3 s

    def test_a_long_poll_is_woken_by_a_send(self):
        self.approve_both()
        got = []
        t = threading.Thread(target=lambda: got.extend(self.transport("desktop").poll(5)))
        t.start()
        for _ in range(200):
            if self.hub._pollers:
                break
            threading.Event().wait(0.01)
        env = hp.sign(hp.new_envelope("message", "id-desktop", "proj-a", "wake up", now=self.clock()), KEYS, "k1", "id-laptop")
        self.transport("laptop").send(env)
        t.join(5)
        self.assertEqual([e["body"] for e in got], ["wake up"])

    def test_errors_from_the_hub_become_proto_errors(self):
        t = self.transport("laptop")
        with self.assertRaises(hp.ProtoError) as e:
            t.peers()  # never said hello
        self.assertEqual(e.exception.code, "pending-approval")
        t.hello(hello_payload("laptop"))
        with self.assertRaises(hp.ProtoError) as e:
            t.peers()
        self.assertEqual(e.exception.code, "pending-approval")

    def test_a_bad_token_is_unauthorized_not_a_transport_error(self):
        t = HttpTransport(self.server.url, lambda: "garbage", ua=UA)
        with self.assertRaises(hp.ProtoError) as e:
            t.hello(hello_payload("laptop"))
        self.assertEqual(e.exception.code, "unauthorized")

    def test_every_request_has_the_custom_user_agent_including_the_token_endpoint(self):
        self.approve_both()
        self.transport("laptop").poll(0)
        self.assertTrue(self.idp.requests)
        self.assertTrue(all(r["ua"] == UA for r in self.idp.requests))
        self.assertTrue(all("Python-urllib" not in r["ua"] for r in self.idp.requests))

    def test_the_access_service_token_headers_go_to_the_token_endpoint_and_the_hub(self):
        creds = {"hub_url": self.server.url, "token_url": self.idp.url, "client_id": "client-laptop", "client_secret": "s1",
                 "access_client_id": "abc.access", "access_client_secret": "shh"}
        self.assertEqual(access_headers(creds), {"CF-Access-Client-Id": "abc.access", "CF-Access-Client-Secret": "shh"})
        t = transport_from_credentials(creds, clock=self.clock, version="0.26.0")
        t.hello(hello_payload("laptop"))
        sent = {k.lower(): v for k, v in self.idp.requests[0]["headers"].items()}
        self.assertEqual((sent.get("cf-access-client-id"), sent.get("cf-access-client-secret")), ("abc.access", "shh"))
        self.assertEqual(access_headers({"client_id": "x"}), {})

    def test_the_token_is_reused_until_shortly_before_it_runs_out(self):
        self.idp.expires_in = 900
        t = self.transport("laptop")
        t.hello(hello_payload("laptop"))
        t.hello(hello_payload("laptop"))
        self.assertEqual(len(self.idp.requests), 1)
        self.clock.advance(minutes=12)
        t.hello(hello_payload("laptop"))
        self.assertEqual(len(self.idp.requests), 1)  # 3 minutes left: still fine
        self.clock.advance(minutes=1, seconds=30)
        t.hello(hello_payload("laptop"))
        self.assertEqual(len(self.idp.requests), 2)  # under 2 minutes left: renewed

    def test_a_wrong_secret_is_a_transport_error_with_no_secret_in_it(self):
        token = TokenSource(self.idp.url, "client-laptop", "WRONG-SECRET", clock=self.clock, ua=UA)
        with self.assertRaises(TransportError) as e:
            token()
        self.assertNotIn("WRONG-SECRET", str(e.exception))

    def test_a_redirect_or_a_login_page_is_a_transport_error(self):
        for mode in ("redirect", "html"):
            self.idp.mode = mode
            with self.assertRaises(TransportError, msg=mode):
                TokenSource(self.idp.url, "client-laptop", "s1", clock=self.clock, ua=UA)()

    def test_a_server_error_or_nothing_listening_is_a_transport_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        t = HttpTransport(f"http://127.0.0.1:{port}", lambda: "stub.client-laptop.x", ua=UA, timeout=2)
        with self.assertRaises(TransportError):
            t.hello(hello_payload("laptop"))

    def test_url_rules(self):
        self.assertEqual(check_url("https://hub.example.com"), "https://hub.example.com")
        self.assertEqual(check_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(check_url("http://localhost:1"), "http://localhost:1")
        for bad in ("http://hub.example.com", "ftp://x", "hub.example.com", "https://", ""):
            with self.assertRaises(ValueError, msg=bad):
                check_url(bad)
        with self.assertRaises(ValueError):
            HttpTransport("http://hub.example.com", lambda: "x", ua=UA)
        with self.assertRaises(ValueError):
            TokenSource("http://idp.example.com/token", "c", "s", clock=self.clock, ua=UA)

    def test_request_json_does_not_follow_redirects(self):
        self.idp.mode = "redirect"
        with self.assertRaises(TransportError) as e:
            request_json(self.idp.url, method="POST", body=b"x=1")
        self.assertIn("Cloudflare Access", str(e.exception))


class CredentialsTest(unittest.TestCase):
    def write(self, text, mode=0o600):
        path = Path(tempfile.mkdtemp()) / "credentials.toml"
        path.write_text(text)
        path.chmod(mode)
        return path

    GOOD = 'hub_url = "https://hub.example.com"\ntoken_url = "https://idp.example.com/token"\nclient_id = "c"\nclient_secret = "s"\n'

    def test_good_file(self):
        creds = load_credentials(self.write(self.GOOD))
        self.assertEqual((creds["client_id"], creds["hub_url"]), ("c", "https://hub.example.com"))

    def test_problems_are_strings_naming_the_file_never_the_secret(self):
        loose = load_credentials(self.write(self.GOOD, 0o644))
        self.assertIsInstance(loose, str)
        self.assertIn("644", loose)
        self.assertNotIn("client_secret", loose)
        self.assertIn("缺少", load_credentials(self.write('hub_url = "https://x"\n')))
        self.assertIn("格式不對", load_credentials(self.write("not = [toml")))
        self.assertIsInstance(load_credentials(Path(tempfile.mkdtemp()) / "missing"), (str, dict))


class EndToEndOverHttpTest(HttpCase):
    """Two Connectors, each with its own store and an HTTP transport, through the real hub server."""

    def node(self, name):
        store = RemoteStore(Path(tempfile.mkdtemp()) / "remote.db", clock=self.clock, rng=lambda: 0.5)
        delivered, receipts, replies = [], [], []
        hooks = Hooks(deliver_local=lambda env: delivered.append(env) or f"local-{len(delivered)}",
                      apply_receipt=receipts.append, apply_reply=replies.append,
                      published_status=lambda: [{"session": "proj-a", "alias": "", "status": "idle"}])
        conn = Connector(store, self.transport(name), hooks, machine_id=f"id-{name}", machine_name=name, version="0.26.0",
                         keys=KEYS, clock=self.clock)
        return conn, store, delivered, receipts, replies

    def test_send_receive_receipt_and_reply(self):
        a, astore, _, areceipts, areplies = self.node("laptop")
        b, bstore, bdelivered, _, _ = self.node("desktop")
        self.assertEqual((a.tick()["state"], b.tick()["state"]), ("pending", "pending"))
        self.hub.approve("laptop")
        self.hub.approve("desktop")
        a.tick()
        b.tick()
        env = a.sign(hp.new_envelope("message", "id-desktop", "proj-a", "do it", now=self.clock(), from_session="proj-b"))
        astore.outbox_add(env, "d1")
        self.assertEqual(a.tick()["send"]["sent"], 1)
        self.assertEqual(b.tick()["pull"]["taken"], 1)
        self.assertEqual([e["body"] for e in bdelivered], ["do it"])
        reply = b.sign(hp.new_envelope("reply", "id-laptop", "proj-b", "done", now=self.clock(), in_reply_to=env["id"], status="done"))
        bstore.outbox_add(reply, "")
        b.tick()
        a.tick()
        self.assertEqual([(r["phase"]) for r in areceipts], ["taken"])
        self.assertEqual([(r["body"], r["status"]) for r in areplies], [("done", "done")])

    def test_revoke_and_expiry_over_http(self):
        a, astore, _, areceipts, _ = self.node("laptop")
        b, _, bdelivered, _, _ = self.node("desktop")
        for n in (a, b):
            n.tick()
        self.hub.approve("laptop")
        self.hub.approve("desktop")
        a.tick()
        b.tick()
        astore.outbox_add(a.sign(hp.new_envelope("message", "id-desktop", "proj-a", "late", now=self.clock())), "d1")
        a.tick()
        self.clock.advance(days=7, seconds=5)
        self.hub.tick()
        a.tick()
        self.assertEqual([r["phase"] for r in areceipts], ["expired"])
        self.hub.revoke("desktop")
        self.assertEqual(b.tick()["error"], hp.ProtoError("revoked").message)
        self.assertEqual(b.state, "revoked")
        self.assertEqual(bdelivered, [])

    def test_the_hub_going_away_and_coming_back_loses_nothing(self):
        a, astore, _, _, _ = self.node("laptop")
        b, _, bdelivered, _, _ = self.node("desktop")
        a.tick()
        b.tick()
        self.hub.approve("laptop")
        self.hub.approve("desktop")
        a.tick()
        b.tick()
        self.server.stop()  # the hub goes away
        astore.outbox_add(a.sign(hp.new_envelope("message", "id-desktop", "proj-a", "while down", now=self.clock())), "d1")
        out = a.tick()
        self.assertIn("hub 連不上", out["error"])
        self.assertEqual(len(astore.outbox_pending()), 1)
        self.server = HubServer(("127.0.0.1", 0), self.hub, StubVerifier(), max_poll_wait=0.3)
        self.server.start()
        for n in (a, b):
            n.transport.base = self.server.url
        self.clock.advance(minutes=1)
        a.tick()
        b.tick()
        self.assertEqual([e["body"] for e in bdelivered], ["while down"])
        self.addCleanup(self.server.stop)


if __name__ == "__main__":
    unittest.main()
