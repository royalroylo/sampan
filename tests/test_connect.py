import os
import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sampan import hubproto as hp
from sampan.connect import Connector, Hooks, Rejected, TransportError, file_problem, load_keys, write_key
from sampan.hub.core import Limits
from sampan.remote_store import RemoteStore
from tests.hub_helpers import KEYS, LocalTransport, hub_with_machines, new_hub, principal


class Node:
    """One machine: a store, a connector to the shared hub, and hooks that record what happened."""

    def __init__(self, hub, clock, name, *, keys=KEYS, deliver=None):
        self.name = name
        self.store = RemoteStore(Path(tempfile.mkdtemp()) / "remote.db", clock=clock, rng=lambda: 0.5)
        self.transport = LocalTransport(hub, principal(name))
        self.delivered, self.receipts, self.replies, self.accepted, self.failed, self.logs = [], [], [], [], [], []
        self.local = {}  # envelope id → local message id: what an idempotent deliver_local keeps in dispatches.db
        self.hooks = Hooks(
            deliver_local=deliver or self._deliver, apply_receipt=self.receipts.append, apply_reply=self.replies.append,
            accepted=lambda d, i, r: self.accepted.append((d, i)),
            failed=lambda d, i, c, m: self.failed.append((d, i, c)),
            published_status=lambda: [{"session": "proj-a", "alias": "", "status": "idle"}],
            log=self.logs.append)
        self.conn = Connector(self.store, self.transport, self.hooks, machine_id=f"id-{name}", machine_name=name,
                              version="0.26.0", keys=keys, clock=clock)

    def _deliver(self, env):
        if env["id"] not in self.local:
            self.local[env["id"]] = f"local-{len(self.local) + 1}"
            self.delivered.append(env)
        return self.local[env["id"]]

    def send(self, to, body="hello", dispatch="d1", **kw):
        env = self.conn.sign(hp.new_envelope("message", f"id-{to}", "proj-a", body, now=self.conn.clock(), from_session="proj-b", **kw))
        self.store.outbox_add(env, dispatch)
        return env


def two_nodes(**kw):
    hub, clock, _ = hub_with_machines(**kw)
    return hub, clock, Node(hub, clock, "laptop"), Node(hub, clock, "desktop")


class FlowTest(unittest.TestCase):
    def test_a_message_goes_through_and_both_ends_learn_what_happened(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        self.assertEqual(a.conn.tick()["send"], {"sent": 1, "failed": 0, "retried": 0})
        self.assertEqual(a.accepted, [("d1", env["id"])])
        self.assertEqual(b.conn.tick()["pull"]["taken"], 1)
        self.assertEqual([e["id"] for e in b.delivered], [env["id"]])
        self.assertEqual(b.delivered[0]["stamp"]["sender_machine"], "id-laptop")
        a.conn.tick()
        [receipt] = a.receipts  # the hub's receipt: the target machine has it
        self.assertEqual((receipt["phase"], receipt["ref"]), ("taken", env["id"]))
        self.assertTrue(receipt["stamp"]["hub"])

    def test_a_reply_and_a_sent_receipt_come_back_signed(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        a.conn.tick()
        b.conn.tick()
        sent = b.conn.sign(hp.new_envelope("receipt", "id-laptop", "proj-b", "", now=clock(), phase="sent", ref=env["id"]))
        reply = b.conn.sign(hp.new_envelope("reply", "id-laptop", "proj-b", "done: ok", now=clock(), in_reply_to=env["id"], status="done"))
        b.store.outbox_add(sent, "")
        b.store.outbox_add(reply, "")
        b.conn.tick()
        a.conn.tick()
        self.assertEqual([r["phase"] for r in a.receipts], ["taken", "sent"])
        self.assertEqual([(r["body"], r["status"]) for r in a.replies], [("done: ok", "done")])

    def test_nothing_moves_until_the_machine_is_approved(self):
        hub, clock, _ = new_hub()
        a, b = Node(hub, clock, "laptop"), Node(hub, clock, "desktop")
        b.conn.hello()
        env = a.send("desktop")
        out = a.conn.tick()
        self.assertEqual((out["state"], "send" in out), ("pending", False))
        self.assertEqual(a.conn.code, next(m["code"] for m in hub.machines() if m["name"] == "laptop"))
        self.assertEqual(len(a.store.outbox_pending()), 1)  # it waits
        hub.approve("laptop")
        hub.approve("desktop")
        b.conn.tick()
        a.conn.tick()
        a.conn.tick()
        b.conn.tick()
        self.assertEqual([e["id"] for e in b.delivered], [env["id"]])

    def test_revoked_stops_everything_but_keeps_the_outbox(self):
        hub, clock, a, b = two_nodes()
        a.conn.tick()
        hub.revoke("laptop")
        a.send("desktop")
        out = a.conn.tick()
        self.assertEqual(a.conn.state, "revoked")
        self.assertEqual(out["error"], hp.ProtoError("revoked").message)
        self.assertEqual(len(a.store.outbox_pending()), 1)
        self.assertEqual(a.store.get("machine_state"), "revoked")


class FaultTest(unittest.TestCase):
    def test_hub_down_keeps_the_outbox_and_retries_with_backoff_without_duplicates(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        a.conn.tick()
        a.transport.fail("send", 1)
        env2 = a.send("desktop", body="two", dispatch="d2")
        out = a.conn.tick()
        self.assertEqual(out["send"], {"sent": 0, "failed": 0, "retried": 1})
        [pending] = a.store.outbox_pending()
        self.assertGreater(pending["attempts"], 0)
        a.conn.tick()
        self.assertEqual(len(a.store.outbox_pending()), 1)  # not due yet
        clock.advance(seconds=10)
        a.conn.tick()
        self.assertEqual(a.store.outbox_pending(), [])
        b.conn.tick()
        self.assertEqual(sorted(e["id"] for e in b.delivered), sorted([env["id"], env2["id"]]))

    def test_the_answer_to_a_send_is_lost_and_the_envelope_is_sent_again_once_at_the_hub(self):
        hub, clock, a, b = two_nodes()
        a.send("desktop")
        a.conn.tick()
        env2 = a.send("desktop", body="two", dispatch="d2")
        a.transport.lose_reply("send", 1)
        a.conn.tick()
        clock.advance(seconds=10)
        a.conn.tick()
        self.assertEqual(hub.db.execute("SELECT COUNT(*) FROM envelopes WHERE id=?", (env2["id"],)).fetchone()[0], 1)
        b.conn.tick()
        self.assertEqual(len(b.delivered), 2)

    def test_a_lost_ack_comes_again_and_is_not_applied_twice(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        a.conn.tick()
        b.transport.fail("ack", 1)  # the ack never reaches the hub
        with self.assertRaises(TransportError):
            b.conn.pull()
        self.assertEqual(len(b.delivered), 1)
        self.assertIsNotNone(b.store.seen(env["id"]))
        clock.advance(seconds=hp.VISIBILITY_TIMEOUT)
        out = b.conn.pull()
        self.assertEqual((out["taken"], out["repeats"]), (0, 1))
        self.assertEqual(len(b.delivered), 1)  # applied once
        a.conn.tick()
        self.assertEqual([r["phase"] for r in a.receipts], ["taken"])

    def test_an_ack_whose_answer_is_lost_is_harmless(self):
        hub, clock, a, b = two_nodes()
        a.send("desktop")
        a.conn.tick()
        b.transport.lose_reply("ack", 1)
        with self.assertRaises(TransportError):
            b.conn.pull()
        clock.advance(seconds=hp.VISIBILITY_TIMEOUT)
        self.assertEqual(b.conn.pull(), {"taken": 0, "repeats": 0, "rejected": 0, "errors": 0})  # the hub has it as taken
        self.assertEqual(len(b.delivered), 1)

    def test_a_duplicate_delivery_in_one_batch_applies_once(self):
        hub, clock, a, b = two_nodes()
        a.send("desktop")
        a.conn.tick()
        b.transport.extra_poll = [dict(e) for e in hub.fetch(principal("desktop"))]  # the same envelope twice
        b.conn.pull()
        self.assertEqual(len(b.delivered), 1)

    def test_a_crash_between_applying_and_recording_is_safe_because_applying_is_idempotent_by_id(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        a.conn.tick()
        real = b.store.seen_add
        b.store.seen_add = lambda *x: (_ for _ in ()).throw(OSError("disk full"))
        out = b.conn.pull()  # applied, then the id could not be recorded: not acked, so it comes again
        self.assertEqual(out["errors"], 1)
        b.store.seen_add = real
        clock.advance(seconds=hp.VISIBILITY_TIMEOUT)
        b.conn.pull()
        self.assertEqual(len(b.delivered), 1)  # the hook saw the id twice and made one message
        self.assertEqual(b.local[env["id"]], "local-1")
        self.assertEqual(b.store.seen(env["id"]), "local-1")

    def test_a_hook_that_fails_leaves_the_envelope_unacked_and_unrecorded(self):
        hub, clock, a, _ = two_nodes()
        boom = Node(hub, clock, "desktop", deliver=lambda env: (_ for _ in ()).throw(RuntimeError("tmux gone")))
        env = a.send("desktop")
        a.conn.tick()
        out = boom.conn.pull()
        self.assertEqual((out["errors"], out["taken"]), (1, 0))
        self.assertIsNone(boom.store.seen(env["id"]))
        self.assertTrue(any("稍後會再送" in line for line in boom.logs))

    def test_a_hook_can_refuse_for_good(self):
        hub, clock, a, _ = two_nodes()
        gone = Node(hub, clock, "desktop", deliver=lambda env: (_ for _ in ()).throw(Rejected("session 不存在")))
        a.send("desktop")
        a.conn.tick()
        self.assertEqual(gone.conn.pull()["rejected"], 1)
        a.conn.tick()
        self.assertEqual([(r["phase"], r["reason"]) for r in a.receipts], [("rejected", "session 不存在")])

    def test_the_outbox_survives_a_restart_of_the_client(self):
        hub, clock, a, b = two_nodes()
        a.send("desktop")
        path = a.store.path
        a.store.close()
        again = RemoteStore(path, clock=clock)
        self.assertEqual(len(again.outbox_pending()), 1)
        self.assertIsNone(again.get("machine_state"))  # nothing but the outbox was written


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.hub, self.clock, self.a, self.b = two_nodes()

    def inject(self, env):
        self.b.transport.extra_poll = [env]
        return self.b.conn.pull()

    def stamped(self, env, sender="id-laptop", **stamp):
        return {**env, "stamp": {"sender_machine": sender, "sender_machine_name": "laptop", "owner": "self", "hub": False, **stamp}}

    def good(self, **kw):
        return self.a.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "hi", now=self.clock(), **kw))

    def test_a_tampered_body_is_refused_and_the_sender_is_told(self):
        env = self.stamped(self.good())
        out = self.inject({**env, "body": "something else"})
        self.assertEqual((out["rejected"], self.b.delivered), (1, []))
        self.assertTrue(any("bad-signature" in line for line in self.b.logs))

    def test_a_missing_or_foreign_signature_is_refused(self):
        env = self.stamped(self.good())
        for bad in ({k: v for k, v in env.items() if k != "sig"}, {**env, "sig": {**env["sig"], "kid": "k9"}}):
            self.assertEqual(self.inject(bad)["rejected"], 1)
        other = Node(self.hub, self.clock, "x", keys={"k1": b"z" * 32})
        wrong_key = self.stamped(other.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "hi", now=self.clock())))
        self.assertEqual(self.inject(wrong_key)["rejected"], 1)
        self.assertEqual(self.b.delivered, [])

    def test_a_signature_for_one_machine_does_not_work_as_another(self):
        env = self.stamped(self.good(), sender="id-nas")  # the hub's stamp says another machine sent it
        self.assertEqual(self.inject(env)["rejected"], 1)

    def test_replay_of_an_old_envelope_and_expired_ones_are_refused(self):
        env = self.stamped(self.good())
        self.clock.advance(days=8)
        self.assertEqual(self.inject(env)["rejected"], 1)
        fresh = self.stamped(self.a.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "hi", now=self.clock(),
                                                              expires_in=hp.timedelta(hours=1))))
        self.clock.advance(hours=2)
        self.assertEqual(self.inject(fresh)["rejected"], 1)
        self.assertEqual(self.b.delivered, [])

    def test_an_envelope_for_another_machine_is_refused(self):
        env = self.stamped(self.a.conn.sign(hp.new_envelope("message", "id-nas", "proj-a", "hi", now=self.clock())))
        self.assertEqual(self.inject(env)["rejected"], 1)

    def test_the_hub_cannot_turn_off_signatures(self):
        original = self.b.transport.hub.hello
        self.b.transport.hub.hello = lambda who, payload: {**original(who, payload), "require_sig": False}
        self.b.conn.hello()
        self.assertTrue(any("簽章不能被降級" in line for line in self.b.logs))
        unsigned = self.stamped({k: v for k, v in self.good().items() if k != "sig"})
        self.assertEqual(self.inject(unsigned)["rejected"], 1)

    def test_only_the_hubs_own_receipt_may_go_without_a_signature(self):
        rec = hp.new_envelope("receipt", "id-desktop", "proj-a", "", now=self.clock(), phase="taken", ref=hp.new_id())
        self.assertEqual(self.inject(self.stamped(rec, hub=True, sender_machine=None))["rejected"], 0)
        self.assertEqual(len(self.b.receipts), 1)
        peer_unsigned = self.stamped(hp.new_envelope("receipt", "id-desktop", "proj-a", "", now=self.clock(), phase="sent", ref=hp.new_id()))
        self.assertEqual(self.inject(peer_unsigned)["rejected"], 1)  # a peer's receipt has to be signed

    def test_an_envelope_from_the_future_is_refused(self):
        future = self.stamped(self.a.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "hi",
                                                               now=self.clock() + hp.SKEW_REJECT + timedelta(minutes=1))))
        self.assertEqual(self.inject(future)["rejected"], 1)
        self.assertTrue(any("clock-skew" in line for line in self.b.logs))
        self.assertEqual(self.b.delivered, [])

    def test_an_expiry_further_out_than_seven_days_is_refused_here_too(self):
        far = self.stamped(self.a.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "hi", now=self.clock(),
                                                            expires_in=timedelta(days=30))))
        self.assertEqual(self.inject(far)["rejected"], 1)
        self.assertEqual(self.b.delivered, [])

    def test_text_with_an_escape_or_other_control_character_never_reaches_the_hook(self):
        for body in ("a\x1b[201~\rINJECT", "a\u2028b", "a\u200bb"):
            env = self.stamped(self.a.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", body, now=self.clock())))
            self.assertEqual(self.inject(env)["rejected"], 1, repr(body))
        reply = self.stamped(self.a.conn.sign(hp.new_envelope("reply", "id-desktop", "proj-a", "ok\x1b[2J", now=self.clock(),
                                                               in_reply_to=hp.new_id(), status="done")))
        self.assertEqual(self.inject(reply)["rejected"], 1)
        self.assertEqual((self.b.delivered, self.b.replies), ([], []))
        self.assertTrue(any("unsafe-characters" in line for line in self.b.logs))

    def test_a_sender_name_the_hub_stamped_with_a_control_character_is_refused(self):
        env = self.a.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "hi", now=self.clock()))
        for name in ("laptop\x1b[201~", "lap\u202etop", "lap\ntop"):
            bad = self.stamped(env, sender_machine_name=name)
            self.assertEqual(self.inject(bad)["rejected"], 1, repr(name))
        self.assertEqual(self.b.delivered, [])
        newline = self.a.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "hi", now=self.clock(), from_session="a b"))
        self.assertEqual(self.inject(self.stamped({**newline, "from_session": "a\nb"}))["rejected"], 1)

    def test_the_hubs_unsigned_receipt_is_limited_to_its_own_phases_about_a_real_envelope(self):
        for phase, ref in (("sent", hp.new_id()), ("taken", "not-an-id"), ("taken", None)):
            rec = {**hp.new_envelope("receipt", "id-desktop", "proj-a", "", now=self.clock(), phase="taken", ref=hp.new_id()),
                   "phase": phase, "ref": ref}
            self.assertEqual(self.inject(self.stamped(rec, hub=True, sender_machine=None))["rejected"], 1, (phase, ref))
        self.assertEqual(self.b.receipts, [])
        for phase in ("rejected", "expired"):
            rec = hp.new_envelope("receipt", "id-desktop", "proj-a", "", now=self.clock(), phase=phase, ref=hp.new_id())
            self.assertEqual(self.inject(self.stamped(rec, hub=True, sender_machine=None))["rejected"], 0, phase)

    def test_a_sent_receipt_needs_the_owner_signature_even_with_a_hub_stamp(self):
        rec = hp.new_envelope("receipt", "id-desktop", "proj-a", "", now=self.clock(), phase="sent", ref=hp.new_id())
        self.assertEqual(self.inject(self.stamped(rec, hub=True, sender_machine=None))["rejected"], 1)
        signed = self.a.conn.sign(rec)
        self.assertEqual(self.inject(self.stamped(signed))["rejected"], 0)
        self.assertEqual([r["phase"] for r in self.b.receipts], ["sent"])

    def test_malformed_input_never_crashes_the_pass(self):
        self.b.transport.extra_poll = [None, "x", {"id": "nope"}, {"id": hp.new_id(), "v": 99}, {"id": hp.new_id(), "v": 1}]
        out = self.b.conn.pull()
        self.assertEqual(out["taken"], 0)
        self.assertEqual(self.b.delivered, [])


class OutboxTest(unittest.TestCase):
    def test_permanent_errors_fail_the_dispatch_and_say_why(self):
        hub, clock, a, b = two_nodes()
        env = hp.new_envelope("message", "id-ghost", "proj-a", "hi", now=clock())
        a.store.outbox_add(a.conn.sign(env), "d9")
        self.assertEqual(a.conn.tick()["send"]["failed"], 1)
        self.assertEqual(a.failed, [("d9", env["id"], "unknown-target")])
        self.assertEqual(a.store.outbox_pending(), [])
        self.assertEqual(a.store.outbox_get(env["id"])["state"], "failed")

    def test_a_full_mailbox_is_tried_again_later_not_failed(self):
        hub, clock, a, b = two_nodes(limits=Limits(mailbox_cap=1))
        a.send("desktop")
        a.send("desktop", body="two", dispatch="d2")
        out = a.conn.tick()
        self.assertEqual(out["send"], {"sent": 1, "failed": 0, "retried": 1})
        b.conn.tick()
        clock.advance(seconds=60)
        self.assertEqual(a.conn.tick()["send"]["sent"], 1)

    def test_an_envelope_that_expired_in_the_outbox_fails_without_asking_the_hub(self):
        hub, clock, a, b = two_nodes()
        a.conn.tick()
        a.send("desktop")
        a.transport.calls.clear()
        clock.advance(days=8)
        a.conn.send_outbox()
        self.assertNotIn("send", a.transport.calls)
        self.assertEqual(a.failed[0][2], "expired")

    def test_withdraw_drops_a_pending_one_only(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        self.assertTrue(a.store.outbox_withdraw(env["id"]))
        self.assertFalse(a.store.outbox_withdraw(env["id"]))
        self.assertEqual(a.store.outbox_pending(), [])

    def test_add_twice_changes_nothing(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        a.store.outbox_add(env, "other")
        self.assertEqual(len(a.store.outbox_pending()), 1)
        self.assertEqual(a.store.outbox_get(env["id"])["dispatch_id"], "d1")

    def test_backoff_grows_has_jitter_and_is_capped(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        delays = [a.store.outbox_retry(env["id"], "down") for _ in range(12)]
        self.assertEqual(delays[0], 2)
        self.assertEqual(delays[3], 16)
        self.assertEqual(delays[-1], 300)
        self.assertEqual(a.store.outbox_retry(env["id"], "x", floor=500), 300)
        self.assertEqual(a.store.outbox_retry(env["id"], "x", floor=30), 300)

    def test_seen_and_finished_outbox_rows_are_forgotten_after_16_days(self):
        hub, clock, a, b = two_nodes()
        env = a.send("desktop")
        a.conn.tick()
        b.conn.tick()
        self.assertIsNotNone(b.store.seen(env["id"]))
        clock.advance(days=15)
        b.conn.maintenance()
        self.assertIsNotNone(b.store.seen(env["id"]))
        clock.advance(days=2)
        self.assertEqual(b.conn.maintenance(), {"seen": 1, "outbox": 0})
        self.assertEqual(a.conn.maintenance()["outbox"], 1)
        self.assertIsNone(b.store.seen(env["id"]))


class ProtocolTest(unittest.TestCase):
    def test_a_hub_that_needs_a_newer_client_makes_the_machine_unsupported(self):
        hub, clock, a, b = two_nodes()
        orig = a.transport.hub.hello
        a.transport.hub.hello = lambda who, payload: {**orig(who, payload), "proto_min": 2, "proto": 2}
        out = a.conn.tick()
        self.assertEqual(out["state"], "unsupported")
        self.assertTrue(any("不相容" in line for line in a.logs))

    def test_the_hub_refusing_an_old_client_is_shown(self):
        hub, clock, a, b = two_nodes()
        hub.set_meta("min_client_proto", 2)
        self.assertEqual(a.conn.tick()["state"], "unsupported")

    def test_hello_carries_what_is_published(self):
        hub, clock, a, b = two_nodes()
        a.conn.hello()
        self.assertEqual([s["session"] for s in hub.peers(principal("desktop"))[0]["sessions"]], ["proj-a"])


class KeyFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_write_key_is_private_and_never_overwrites(self):
        path = self.dir / "owner.key"
        line = write_key(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIsNone(file_problem(path))
        self.assertEqual(set(load_keys(path)), {"k1"})
        self.assertEqual(len(load_keys(path)["k1"]), 32)
        self.assertTrue(line.startswith("k1:"))
        with self.assertRaises(FileExistsError):
            write_key(path)

    def test_load_keys_formats_comments_and_garbage(self):
        k2 = hp.new_key()
        (self.dir / "owner.key").write_text(f"# comment\n\n{hp.new_key()}\nk2:{k2}\nk3:!!!\nk4:abc\n", encoding="utf-8")
        keys = load_keys(self.dir / "owner.key")
        self.assertEqual(sorted(keys), ["k1", "k2"])  # a bare line is k1; short and broken ones are dropped
        self.assertEqual(keys["k2"], hp.decode_key(k2))
        self.assertEqual(load_keys(self.dir / "missing"), {})

    def test_file_problem_names_loose_permissions(self):
        path = self.dir / "k"
        path.write_text("x")
        os.chmod(path, 0o644)
        self.assertIn("644", file_problem(path))
        self.assertIsNone(file_problem(self.dir / "nope"))

    def test_a_machine_without_a_key_cannot_sign(self):
        hub, clock, _ = hub_with_machines()
        n = Node(hub, clock, "laptop", keys={})
        with self.assertRaises(Rejected):
            n.conn.sign(hp.new_envelope("message", "id-desktop", "proj-a", "x", now=clock()))


if __name__ == "__main__":
    unittest.main()
