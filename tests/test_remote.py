"""Sending to another machine's session and taking what comes, with two sampan state folders and one hub (in process)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan import dispatch, machine, remote
from sampan import hubproto as hp
from sampan.connect import Connector, write_key
from sampan.hub.core import HubCore, Principal
from sampan.send import Caller
from sampan.status import State
from tests.helpers import make_env, status_row
from tests.hub_helpers import LocalTransport

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
CALLER = Caller("proj-b", "alice", "claude", "%1", id="aaaaaaaa-0000-4000-8000-000000000001")


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Box:
    """One machine: its own config and state folders, a remote.db, a connector to the shared hub."""

    def __init__(self, hub, clock, name, sessions, key_text):
        self.name, self.clock, self.hub = name, clock, hub
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        machine.rename(self.cfg.path, name)
        self.machine = machine.load_or_create(self.cfg.path)
        remote.hub_dir(self.cfg).mkdir(parents=True)
        write_key(remote.key_path(self.cfg), key_text)
        self.rows = [status_row(n, State.RUNNING, agent_status="idle", pane=f"%{i}", id=f"id-{n}") for i, n in enumerate(sessions, 1)]
        self.store = remote.open_store(self.cfg, clock=clock)
        self.transport = LocalTransport(hub, Principal(f"client-{name}", f"sub-{name}"))
        self.logs = []
        self.hooks = remote.make_hooks(self.cfg, None, self.store, log=self.logs.append, now_fn=clock,
                                       status_fn=lambda cfg, run, **kw: self.rows)
        from sampan.connect import load_keys
        self.conn = Connector(self.store, self.transport, self.hooks, machine_id=self.machine.id, machine_name=name,
                              version="0.26.0", keys=load_keys(remote.key_path(self.cfg)), clock=clock)
        self.advanced = []

    def dispatch(self, did):
        return dispatch.load(self.cfg.state_dir, ids=[did])[0]

    def pump(self, fake_advance=None):
        return remote.pump_inbound(self.cfg, None, self.store, self.conn, sleep=lambda s: None,
                                   advance=fake_advance or self.advance, now_fn=self.clock)

    def advance(self, cfg, run, did, *, sleep):
        self.advanced.append(did)
        return self.dispatch(did)

    def finish(self, did, reply="done: ok", state="done", reason=""):
        with dispatch.transaction(self.cfg.state_dir, ids=[did]) as items:
            d = next(x for x in items if x.id == did)
            d.state, d.status, d.reply, d.reason = state, "replied" if state == "done" else "failed", reply, reason

    def paste(self, did):
        with dispatch.transaction(self.cfg.state_dir, ids=[did]) as items:
            d = next(x for x in items if x.id == did)
            d.state, d.status = "sent", "pasted"


class Setup(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hub = HubCore(Path(tempfile.mkdtemp()) / "hub.db", clock=self.clock)
        key = hp.new_key()
        self.a = Box(self.hub, self.clock, "laptop", ("proj-b",), key)
        self.b = Box(self.hub, self.clock, "desktop", ("proj-a", "private"), key)
        self.b.store.publish("proj-a", "main")
        for box in (self.a, self.b):
            box.conn.tick()
        self.hub.approve("laptop")
        self.hub.approve("desktop")
        for _ in range(2):
            self.a.conn.tick()
            self.b.conn.tick()
        self.clock.advance(seconds=61)  # the next hello (every minute) brings what the other machine publishes
        self.a.conn.tick()
        self.b.conn.tick()

    def send(self, to="desktop/proj-a", message="do the thing", **kw):
        return remote.submit_remote(self.a.cfg, CALLER, to, message, notify=False, now=self.clock(), store=self.a.store, **kw)


class SendTest(Setup):
    def test_a_queued_dispatch_with_a_signed_envelope_in_the_outbox(self):
        d = self.send()
        self.assertEqual((d.state, d.status, d.to_name), ("queued", "queued", "desktop/proj-a"))
        r = remote.info(d)
        self.assertEqual((r["role"], r["phase"], r["machine_name"], r["session"]), ("out", "outbox", "desktop", "proj-a"))
        [row] = self.a.store.outbox_pending()
        self.assertEqual((row["dispatch_id"], row["id"]), (d.id, r["id"]))
        env = row["envelope"]
        self.assertEqual((env["body"], env["to"], env["from_session"]), ("do the thing", {"machine": self.b.machine.id, "session": "proj-a"}, "proj-b"))
        self.assertIn("sig", env)
        self.assertEqual(d.reason, remote.PHASES["outbox"])

    def test_the_session_can_be_named_by_its_alias(self):
        self.assertEqual(self.send("desktop/main").extra[remote.REMOTE]["session"], "proj-a")

    def test_problems_come_back_as_sentences(self):
        for to, needle in (("nas/proj-a", "沒有叫「nas」"), ("desktop/private", "沒有發布「private」"), ("laptop/proj-b", "就是這台機器")):
            got = self.send(to)
            self.assertIsInstance(got, str, to)
            self.assertIn(needle, got)
        self.assertIn("沒發布" if False else "機器/session", self.send("plain"))
        self.assertIn("raw", self.send(raw=True))
        self.assertIn("restore", self.send(restore=True))
        self.assertIn("restore", self.send(profile="work"))
        self.assertIn("空的", self.send(message="  "))
        self.assertIn("太長", self.send(message="x" * (hp.MAX_BODY + 1)))

    def test_no_peer_list_yet_and_no_key(self):
        empty = remote.open_store(make_env(Path(tempfile.mkdtemp())), clock=self.clock)
        self.assertIn("還沒有遠端機器的清單", remote.submit_remote(self.a.cfg, CALLER, "desktop/proj-a", "x", notify=False, now=NOW, store=empty))
        remote.key_path(self.a.cfg).unlink()
        self.assertIn("owner key", self.send())

    def test_nothing_is_created_when_it_is_refused(self):
        self.send(raw=True)
        self.send("nas/x")
        self.assertEqual(dispatch.load(self.a.cfg.state_dir, ids=()), [])
        self.assertEqual(self.a.store.outbox_pending(), [])


class FlowTest(Setup):
    def test_the_whole_way_there_and_back(self):
        d = self.send()
        self.a.conn.tick()
        self.assertEqual(remote.info(self.a.dispatch(d.id))["phase"], "at-hub")
        self.b.conn.tick()
        [inbound] = [x for x in dispatch.load(self.b.cfg.state_dir, ids=()) if remote.info(x)]
        self.assertEqual((inbound.from_kind, inbound.from_name, inbound.to_name, inbound.state), ("unregistered", "laptop/proj-b", "proj-a", "queued"))
        self.assertIn("do the thing", inbound.text)
        self.assertIn("laptop/proj-b", inbound.text)
        self.assertFalse(inbound.notify)
        self.a.conn.tick()  # the hub's receipt: taken
        mid = self.a.dispatch(d.id)
        self.assertEqual((remote.info(mid)["phase"], mid.status, mid.state), ("taken", "taken", "queued"))
        self.b.paste(inbound.id)
        self.assertEqual(self.b.pump(), 1)  # the `sent` receipt
        self.b.conn.tick()
        self.a.conn.tick()
        mid = self.a.dispatch(d.id)
        from sampan import send
        self.assertEqual((remote.info(mid)["phase"], mid.state, send.status_of(mid)), ("sent", "sent", "pasted"))
        self.b.finish(inbound.id, "all done")
        self.assertEqual(self.b.pump(), 1)  # the reply
        self.b.conn.tick()
        self.a.conn.tick()
        final = self.a.dispatch(d.id)
        self.assertEqual((final.state, send.status_of(final), final.reply, remote.info(final)["phase"]), ("done", "replied", "all done", "replied"))
        self.assertEqual(remote.summary(final)["phase"], "replied")
        self.assertEqual(self.b.pump(), 0)  # nothing left to say
        self.assertEqual(self.b.store.get(remote.OPEN), [])

    def test_a_failed_reply_fails_the_senders_dispatch_with_the_reason(self):
        d = self.send()
        self.a.conn.tick()
        self.b.conn.tick()
        [inbound] = [x for x in dispatch.load(self.b.cfg.state_dir, ids=()) if remote.info(x)]
        self.b.finish(inbound.id, "", state="failed", reason="Claude 沒在執行")
        self.b.pump()
        self.b.conn.tick()
        self.a.conn.tick()
        final = self.a.dispatch(d.id)
        self.assertEqual(final.state, "failed")
        self.assertIn("Claude 沒在執行", final.reason)

    def test_the_receiver_advances_what_it_took(self):
        self.send()
        self.a.conn.tick()
        self.b.conn.tick()
        self.b.pump()
        self.assertEqual(len(self.b.advanced), 1)

    def test_the_same_envelope_twice_makes_one_message(self):
        self.send()
        self.a.conn.tick()
        env = self.hub.fetch(Principal("client-desktop", "sub-desktop"))[0]
        deliver = self.b.hooks.deliver_local
        first, second = deliver(env), deliver(env)
        self.assertEqual(first, second)
        self.assertEqual(len([x for x in dispatch.load(self.b.cfg.state_dir, ids=()) if remote.info(x)]), 1)

    def test_a_session_that_is_not_published_or_not_running_is_refused(self):
        from sampan.connect import Rejected
        self.send()
        self.a.conn.tick()
        env = self.hub.fetch(Principal("client-desktop", "sub-desktop"))[0]
        self.b.store.unpublish("proj-a")
        with self.assertRaises(Rejected):
            self.b.hooks.deliver_local(env)
        self.b.store.publish("proj-a")
        self.b.rows[0] = status_row("proj-a", State.STOPPED)
        with self.assertRaises(Rejected) as e:
            self.b.hooks.deliver_local(env)
        self.assertIn("沒在執行", str(e.exception))

    def test_a_refused_message_fails_the_senders_dispatch(self):
        d = self.send()
        self.b.store.unpublish("proj-a")
        self.a.conn.tick()
        self.b.conn.tick()
        self.a.conn.tick()
        final = self.a.dispatch(d.id)
        self.assertEqual(final.state, "failed")
        self.assertIn("對方機器拒收", final.reason)

    def test_the_hub_refusing_the_send_fails_it_with_the_hubs_words(self):
        d = self.send()
        self.hub.revoke("desktop")
        self.a.conn.tick()
        final = self.a.dispatch(d.id)
        self.assertEqual(final.state, "failed")
        self.assertIn("hub 拒收", final.reason)

    def test_expired_at_the_hub_fails_it_as_expired(self):
        d = self.send()
        self.a.conn.tick()
        self.clock.advance(days=7, seconds=5)
        self.hub.tick()
        self.a.conn.hello()
        self.a.conn.pull()
        final = self.a.dispatch(d.id)
        self.assertEqual((final.state, final.reason), ("failed", remote.EXPIRED_TEXT))

    def test_published_status_says_idle_busy_or_offline(self):
        self.b.store.publish("private")
        self.b.rows[1] = status_row("private", State.RUNNING, agent_status="busy")
        got = {s["session"]: s["status"] for s in remote.published_status(self.b.cfg, None, self.b.store, lambda cfg, run, **kw: self.b.rows)}
        self.assertEqual(got, {"private": "busy", "proj-a": "idle"})
        self.b.rows[0] = status_row("proj-a", State.STOPPED)
        got = {s["session"]: s["status"] for s in remote.published_status(self.b.cfg, None, self.b.store, lambda cfg, run, **kw: self.b.rows)}
        self.assertEqual(got["proj-a"], "offline")


class LocalRulesTest(Setup):
    def test_advance_leaves_a_remote_dispatch_alone_until_its_time(self):
        from sampan import send
        d = self.send()
        out = send.advance(self.a.cfg, None, d.id, sleep=lambda s: None, now=self.clock(), status_fn=lambda cfg, run, **kw: [])
        self.assertEqual((out.state, remote.info(out)["phase"]), ("queued", "outbox"))
        out = send.advance(self.a.cfg, None, d.id, sleep=lambda s: None, now=self.clock() + timedelta(days=7, seconds=1),
                           status_fn=lambda cfg, run, **kw: [])
        self.assertEqual((out.state, out.reason), ("failed", remote.EXPIRED_TEXT))

    def test_the_30_minute_queue_limit_does_not_apply(self):
        from sampan import send
        d = self.send()
        out = send.advance(self.a.cfg, None, d.id, sleep=lambda s: None, now=self.clock() + timedelta(hours=2),
                           status_fn=lambda cfg, run, **kw: [])
        self.assertEqual(out.state, "queued")

    def test_a_lost_outbox_row_is_rebuilt_by_advance(self):
        from sampan import send
        d = self.send()
        self.a.store.outbox_withdraw(remote.info(d)["id"])
        self.assertEqual(self.a.store.outbox_pending(), [])
        send.advance(self.a.cfg, None, d.id, sleep=lambda s: None, now=self.clock(), status_fn=lambda cfg, run, **kw: [])
        # advance opens the real remote.db of the state folder: the same file this test's store uses
        self.assertEqual(len(self.a.store.outbox_pending()), 1)

    def test_cancel_in_the_outbox_withdraws_it(self):
        from sampan import send
        d = self.send()
        out = send.cancel(self.a.cfg, d.id, now=self.clock())
        self.assertEqual(out.state, "cancelled")
        self.assertEqual(self.a.store.outbox_pending(), [])

    def test_cancel_at_the_hub_asks_the_hub_at_the_next_pass(self):
        from sampan import send
        d = self.send()
        self.a.conn.tick()  # at the hub
        self.assertEqual(send.cancel(self.a.cfg, d.id, now=self.clock()).state, "cancelled")
        self.assertEqual(self.a.store.get(remote.CANCELS), [remote.info(self.a.dispatch(d.id))["id"]])
        remote.process_cancels(self.a.store, self.a.transport)
        self.assertEqual(self.a.store.get(remote.CANCELS), [])
        self.b.conn.tick()
        self.assertEqual([x for x in dispatch.load(self.b.cfg.state_dir, ids=()) if remote.info(x)], [])  # never arrived

    def test_cancel_with_the_hub_down_keeps_the_request(self):
        from sampan import send
        d = self.send()
        self.a.conn.tick()
        send.cancel(self.a.cfg, d.id, now=self.clock())
        self.a.transport.fail("cancel", 1)
        remote.process_cancels(self.a.store, self.a.transport)
        self.assertEqual(len(self.a.store.get(remote.CANCELS)), 1)

    def test_resend_is_refused_for_a_remote_dispatch(self):
        from sampan import send
        d = self.send()
        send.cancel(self.a.cfg, d.id, now=self.clock())
        self.assertEqual(send.resend(self.a.cfg, d.id), remote.RESEND_REFUSAL)

    def test_a_remote_dispatch_looks_remote_to_the_summary_only(self):
        d = self.send()
        self.assertEqual(remote.summary(d)["machine"], "desktop")
        plain = dispatch.Dispatch("ab12", "x", "", "claude", "y", "claude", "", "t", "queued", NOW.isoformat())
        self.assertIsNone(remote.summary(plain))
        self.assertIsNone(remote.info(plain))


if __name__ == "__main__":
    unittest.main()
