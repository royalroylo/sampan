"""One machine sends to a session on another: two sampan state folders, a real hub server on a loopback port, the real HTTP
transport, the real delivery to the target (send.advance pastes into a fake tmux). Only the identity provider is a stub."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan import dispatch, hubcmd, machine, remote, send
from sampan import hubproto as hp
from sampan.connect import write_key
from sampan.hub.auth import StubVerifier
from sampan.hub.core import HubCore
from sampan.hub.server import HubServer
from sampan.hubhttp import HttpTransport, TokenSource
from sampan.send import Caller
from sampan.status import State
from tests import test_send as ts
from tests.helpers import make_env, status_row
from tests.hub_helpers import Clock, StubIdp

CALLER = Caller("proj-b", "alice", "claude", "%1", id="aaaaaaaa-0000-4000-8000-000000000001")
UA = hp.user_agent("0.26.0")


class Machine:
    def __init__(self, test, name, sessions, key_text):
        self.test, self.name = test, name
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        machine.rename(self.cfg.path, name)
        remote.hub_dir(self.cfg).mkdir(parents=True)
        write_key(remote.key_path(self.cfg), key_text)
        self.rows = [status_row(n, State.RUNNING, agent_status="idle", pane=f"%{i}", id=f"id-{n}", sid=ts.SID_B)
                     for i, n in enumerate(sessions, 1)]
        self.store = remote.open_store(self.cfg, clock=test.clock)
        token = TokenSource(test.idp.url, f"client-{name}", "secret", clock=test.clock, ua=UA)
        self.transport = HttpTransport(test.server.url, token, ua=UA)
        self.run = ts.tmux_runner()
        self.conn, why = hubcmd.build_connector(self.cfg, self.run, self.store, clock=test.clock, transport=self.transport,
                                                version="0.26.0", status_fn=self.status_fn)
        assert why is None, why

    def status_fn(self, cfg, run, snapshot_fn=None):
        return list(self.rows)

    def tick(self):
        out = self.conn.tick()
        remote.pump_inbound(self.cfg, self.run, self.store, self.conn, sleep=lambda s: None, now_fn=self.test.clock,
                            status_fn=self.status_fn)
        if self.conn.state == "active" and self.store.outbox_pending():
            self.conn.send_outbox()  # what the daemon's loop does after the pump
        return out

    def dispatch(self, did):
        return dispatch.load(self.cfg.state_dir, ids=[did])[0]


class E2E(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(ts.NOW)
        self.hub = HubCore(Path(tempfile.mkdtemp()) / "hub.db", clock=self.clock)
        self.server = HubServer(("127.0.0.1", 0), self.hub, StubVerifier(), max_poll_wait=0.2)
        self.server.start()
        self.idp = StubIdp({"client-laptop": "secret", "client-desktop": "secret"})
        self.addCleanup(self.server.stop)
        self.addCleanup(self.idp.close)
        key = hp.new_key()
        self.laptop = Machine(self, "laptop", ("proj-b",), key)
        self.desktop = Machine(self, "desktop", ("proj-a",), key)
        self.desktop.store.publish("proj-a")
        self.laptop.tick()
        self.desktop.tick()
        self.hub.approve("laptop")
        self.hub.approve("desktop")
        for _ in range(2):
            self.laptop.tick()
            self.desktop.tick()
        self.clock.advance(seconds=61)
        self.laptop.tick()
        self.desktop.tick()

    def submit(self, message="please run the tests"):
        return remote.submit_remote(self.laptop.cfg, CALLER, "desktop/proj-a", message, notify=False, now=self.clock(),
                                    store=self.laptop.store)

    def test_send_arrive_paste_reply_and_dispatch_result(self):
        d = self.submit()
        self.assertEqual(d.reason, remote.PHASES["outbox"])
        self.laptop.tick()
        self.assertEqual(remote.info(self.laptop.dispatch(d.id))["phase"], "at-hub")
        self.desktop.tick()  # takes it: a local message for proj-a, pasted into its (fake) pane by the ordinary delivery
        self.laptop.tick()
        self.assertEqual(remote.info(self.laptop.dispatch(d.id))["phase"], "sent")  # taken, then pasted: the receipt came back
        writes = ts.tmux_writes(self.desktop.run)
        self.assertTrue(writes, "nothing was pasted into the target")
        [inbound] = [x for x in dispatch.load(self.desktop.cfg.state_dir, ids=()) if remote.info(x)]
        self.assertIn("please run the tests", inbound.text)
        self.assertIn("laptop/proj-b", inbound.text)
        replies = iter([send.Reply(True, True, "all tests pass")])
        with mock.patch.object(send, "_reply", lambda cfg, row, text, start=None: next(replies)):
            self.desktop.tick()
        self.desktop.tick()
        self.laptop.tick()
        final = self.laptop.dispatch(d.id)
        self.assertEqual((final.state, final.reply), ("done", "all tests pass"))
        self.assertEqual(remote.summary(final)["phase"], "replied")

    def test_revoking_the_target_machine_fails_the_dispatch_with_the_hubs_reason(self):
        d = self.submit()
        self.hub.revoke("desktop")
        self.laptop.tick()
        final = self.laptop.dispatch(d.id)
        self.assertEqual(final.state, "failed")
        self.assertIn("hub 拒收", final.reason)
        self.assertEqual(self.desktop.tick()["error"], hp.ProtoError("revoked").message)
        self.assertEqual([x for x in dispatch.load(self.desktop.cfg.state_dir, ids=()) if remote.info(x)], [])

    def test_a_revoked_sender_cannot_get_a_queued_message_through(self):
        self.submit()
        self.laptop.tick()
        self.hub.revoke("laptop")
        self.desktop.tick()
        self.assertEqual([x for x in dispatch.load(self.desktop.cfg.state_dir, ids=()) if remote.info(x)], [])

    def test_a_message_nobody_takes_expires_and_the_sender_is_told(self):
        d = self.submit()
        self.laptop.tick()
        self.clock.advance(days=7, seconds=5)
        self.hub.tick()
        self.laptop.tick()
        final = self.laptop.dispatch(d.id)
        self.assertEqual((final.state, final.reason), ("failed", remote.EXPIRED_TEXT))

    def test_the_hub_going_down_keeps_the_dispatch_waiting_and_it_goes_through_afterwards(self):
        d = self.submit()
        self.server.stop()
        out = self.laptop.tick()
        self.assertIn("hub 連不上", out.get("error", "") or str(out))
        self.assertEqual(remote.info(self.laptop.dispatch(d.id))["phase"], "outbox")
        self.server = HubServer(("127.0.0.1", 0), self.hub, StubVerifier(), max_poll_wait=0.2)
        self.server.start()
        for m in (self.laptop, self.desktop):
            m.transport.base = self.server.url
        self.addCleanup(self.server.stop)
        self.clock.advance(minutes=6)
        self.laptop.tick()
        self.desktop.tick()
        self.laptop.tick()
        self.assertEqual(remote.info(self.laptop.dispatch(d.id))["phase"], "sent")

    def test_a_tampered_envelope_never_reaches_the_target(self):
        d = self.submit()
        self.laptop.tick()
        self.hub.db.execute("UPDATE envelopes SET body='something else' WHERE state='queued'")  # a hub that was taken over
        self.hub.db.commit()
        self.desktop.tick()
        self.assertEqual([x for x in dispatch.load(self.desktop.cfg.state_dir, ids=()) if remote.info(x)], [])
        self.laptop.tick()
        final = self.laptop.dispatch(d.id)
        self.assertEqual(final.state, "failed")
        self.assertIn("bad-signature", final.reason)

    def test_a_hostile_hub_cannot_slip_a_paste_escape_through(self):
        # the sender refuses it at the source; here the hub itself is the attacker and rewrites a stored message
        d = self.submit()
        self.laptop.tick()
        self.hub.db.execute("UPDATE envelopes SET body=? WHERE state='queued'", ("x\x1b[201~\rINJECT",))
        self.hub.db.commit()
        self.desktop.tick()
        self.assertEqual([x for x in dispatch.load(self.desktop.cfg.state_dir, ids=()) if remote.info(x)], [])
        self.assertEqual(ts.tmux_writes(self.desktop.run), [])  # nothing was pasted or typed into the target
        self.laptop.tick()
        self.assertEqual(self.laptop.dispatch(d.id).state, "failed")

    def test_a_sender_that_is_itself_compromised_cannot_get_an_escape_through_the_hub(self):
        env = self.laptop.conn.sign(hp.new_envelope("message", f"{self.desktop.conn.machine_id}", "proj-a", "ok\x1b[201~\rINJECT",
                                                    now=self.clock()))
        self.laptop.store.outbox_add(env, "d-evil")
        out = self.laptop.conn.send_outbox()
        self.assertEqual(out["failed"], 1)  # the hub refuses it (bad-envelope), never stores it
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM envelopes WHERE body LIKE ?", ("%INJECT%",)).fetchone()[0], 0)
        self.desktop.tick()
        self.assertEqual(ts.tmux_writes(self.desktop.run), [])

    def test_no_wait_dispatch_does_not_notify_a_remote_session_that_does_not_exist(self):
        d = self.submit()
        self.assertFalse(self.laptop.dispatch(d.id).notify)  # the terminal-less caller asked for no notice; nothing to deliver


if __name__ == "__main__":
    unittest.main()
