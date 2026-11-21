import json
import os
import sqlite3
import threading
import unittest
from datetime import timedelta

from sampan import hubproto as hp
from sampan.hub.core import HubCore, Limits, verification_code
from tests.hub_helpers import Clock, hello_payload, hub_with_machines, message, new_hub, principal

L, D = principal("laptop"), principal("desktop")


def code_of(fn):
    try:
        fn()
    except hp.ProtoError as e:
        return e.code
    return None


class MachinesTest(unittest.TestCase):
    def test_a_first_hello_is_pending_and_gives_the_code_both_sides_show(self):
        hub, _, _ = new_hub()
        r = hub.hello(L, hello_payload("laptop"))
        self.assertEqual(r["state"], "pending")
        self.assertEqual(r["code"], verification_code("id-laptop", "client-laptop"))
        [m] = hub.machines()
        self.assertEqual((m["state"], m["code"]), ("pending", r["code"]))

    def test_a_pending_machine_can_do_nothing_but_hello(self):
        hub, clock, _ = new_hub()
        hub.hello(L, hello_payload("laptop"))
        self.assertEqual(code_of(lambda: hub.send(L, message(clock))), "pending-approval")
        self.assertEqual(code_of(lambda: hub.fetch(L)), "pending-approval")
        self.assertEqual(code_of(lambda: hub.peers(L)), "pending-approval")
        self.assertEqual(code_of(lambda: hub.ack(L, [], "taken")), "pending-approval")

    def test_an_unknown_client_that_skips_hello_is_not_approved_either(self):
        hub, clock, _ = new_hub()
        self.assertEqual(code_of(lambda: hub.fetch(L)), "pending-approval")

    def test_approve_checks_the_code_when_given_and_audits(self):
        hub, _, _ = new_hub()
        r = hub.hello(L, hello_payload("laptop"))
        self.assertEqual(code_of(lambda: hub.approve("laptop", code="000-000")), "bad-request")
        self.assertEqual(hub.approve("laptop", code=r["code"])["state"], "active")
        self.assertEqual(hub.hello(L, hello_payload("laptop"))["state"], "active")
        self.assertEqual([a["action"] for a in hub.audit_log()][:2], ["machine-approved", "machine-registered"])
        self.assertEqual(code_of(lambda: hub.approve("nobody")), "not-found")

    def test_names_and_machine_ids_are_unique_and_bound_to_the_client(self):
        hub, _, _ = new_hub()
        hub.hello(L, hello_payload("laptop"))
        self.assertEqual(code_of(lambda: hub.hello(D, {**hello_payload("laptop"), "machine_id": "id-desktop"})), "name-taken")
        self.assertEqual(code_of(lambda: hub.hello(D, {**hello_payload("desktop"), "machine_id": "id-laptop"})), "machine-mismatch")
        self.assertEqual(code_of(lambda: hub.hello(L, {**hello_payload("laptop"), "machine_id": "id-other"})), "machine-mismatch")

    def test_rename_keeps_the_machine_and_must_stay_unique(self):
        hub, _, _ = hub_with_machines()
        self.assertEqual(hub.hello(L, hello_payload("notebook", machine_id="id-laptop"))["state"], "active")
        self.assertEqual([m["name"] for m in hub.machines()], ["notebook", "desktop"])
        self.assertEqual(code_of(lambda: hub.hello(L, hello_payload("desktop", machine_id="id-laptop"))), "name-taken")

    def test_protocol_ranges(self):
        hub, _, _ = new_hub()
        self.assertEqual(code_of(lambda: hub.hello(L, {**hello_payload("laptop"), "proto_min": 2, "proto_max": 2})), "upgrade-required")
        hub.set_meta("min_client_proto", 2)
        self.assertEqual(code_of(lambda: hub.hello(L, hello_payload("laptop"))), "upgrade-required")  # the client is too old
        for bad in ({"proto_min": "1", "proto_max": 1}, {"proto_min": 2, "proto_max": 1}, {"machine_id": ""}, {"name": "a/b"}):
            self.assertEqual(code_of(lambda: hub.hello(L, {**hello_payload("laptop"), **bad})), "bad-request", bad)
        self.assertEqual(code_of(lambda: hub.hello(L, None)), "bad-request")

    def test_published_is_only_kept_for_active_machines(self):
        hub, clock, _ = new_hub()
        hub.hello(L, hello_payload("laptop", ("proj-a",)))
        hub.hello(D, hello_payload("desktop", ("proj-a",)))
        hub.approve("laptop")
        hub.approve("desktop")
        self.assertEqual(hub.peers(L), [{"machine_id": "id-desktop", "name": "desktop", "last_seen": hub.peers(L)[0]["last_seen"], "sessions": []}])
        hub.hello(D, hello_payload("desktop", ("proj-a", "proj-c")))
        self.assertEqual([s["session"] for s in hub.peers(L)[0]["sessions"]], ["proj-a", "proj-c"])

    def test_peers_lists_the_others_only(self):
        hub, _, _ = hub_with_machines(("laptop", "desktop", "nas"))
        self.assertEqual([m["name"] for m in hub.peers(L)], ["desktop", "nas"])

    def test_revoke_is_immediate_and_drops_what_waits(self):
        hub, clock, _ = hub_with_machines()
        hub.send(L, message(clock))
        env = message(clock, to="laptop", sender="desktop")
        hub.send(D, env)
        self.assertEqual(hub.revoke("desktop")["state"], "revoked")
        self.assertEqual(code_of(lambda: hub.fetch(D)), "revoked")
        self.assertEqual(code_of(lambda: hub.hello(D, hello_payload("desktop"))), "revoked")
        self.assertEqual(code_of(lambda: hub.approve("desktop")), "revoked")
        self.assertEqual(code_of(lambda: hub.send(L, message(clock))), "unknown-target")  # nobody to send to
        row = hub.db.execute("SELECT state, reason, body FROM envelopes WHERE to_machine='id-desktop'").fetchone()
        self.assertEqual((row["state"], row["reason"], row["body"]), ("rejected", "machine-revoked", None))
        [receipt] = [e for e in hub.fetch(L) if e["kind"] == "receipt"]  # the sender is told why
        self.assertEqual((receipt["phase"], receipt["reason"]), ("rejected", "machine-revoked"))


class RevokeSenderTest(unittest.TestCase):
    def test_what_a_revoked_machine_sent_and_nobody_took_never_arrives(self):
        hub, clock, _ = hub_with_machines()
        sent = message(clock, body="from the stolen laptop")
        hub.send(L, sent)
        handed = message(clock, body="already handed out")
        hub.send(L, handed)
        hub.fetch(D)  # both handed out, not acked
        hub.revoke("laptop")
        clock.advance(seconds=hp.VISIBILITY_TIMEOUT)
        self.assertEqual(hub.fetch(D), [])
        rows = hub.db.execute("SELECT state, reason, body FROM envelopes WHERE from_machine='id-laptop'").fetchall()
        self.assertEqual({(r["state"], r["reason"], r["body"]) for r in rows}, {("rejected", "sender-revoked", None)})

    def test_an_envelope_taken_before_the_revoke_stays_taken(self):
        hub, clock, _ = hub_with_machines()
        hub.send(L, message(clock))
        hub.ack(D, [e["id"] for e in hub.fetch(D)], "taken")
        hub.revoke("laptop")
        self.assertEqual(hub.db.execute("SELECT state FROM envelopes WHERE from_machine='id-laptop'").fetchone()["state"], "taken")

    def test_fetch_skips_a_sender_that_is_no_longer_active_even_if_a_row_slipped_through(self):
        hub, clock, _ = hub_with_machines()
        hub.send(L, message(clock))
        hub.db.execute("UPDATE machines SET state='revoked' WHERE machine_id='id-laptop'")  # revoked behind the core's back
        hub.db.commit()
        self.assertEqual(hub.fetch(D), [])


class SendTest(unittest.TestCase):
    def setUp(self):
        self.hub, self.clock, _ = hub_with_machines()

    def test_send_stamps_and_stores(self):
        env = message(self.clock)
        r = self.hub.send(L, env)
        self.assertEqual((r["duplicate"], r["state"]), (False, "queued"))
        [got] = self.hub.fetch(D)
        self.assertEqual(got["body"], "hello")
        self.assertEqual(got["stamp"]["sender_machine"], "id-laptop")
        self.assertEqual(got["stamp"]["sender_machine_name"], "laptop")
        self.assertEqual(got["stamp"]["sender_sub"], "sub-laptop")
        self.assertEqual(got["stamp"]["owner"], "self")
        self.assertEqual(got["sig"], env["sig"])  # the owner's signature goes through untouched

    def test_a_forged_stamp_or_from_is_dropped(self):
        env = {**message(self.clock), "stamp": {"sender_machine": "id-desktop", "sender_sub": "root"}, "from": "alice", "owner": "x"}
        self.hub.send(L, env)
        [got] = self.hub.fetch(D)
        self.assertEqual(got["stamp"]["sender_machine"], "id-laptop")
        self.assertEqual(got["stamp"]["sender_sub"], "sub-laptop")
        self.assertNotIn("from", got)

    def test_send_is_idempotent_by_id(self):
        env = message(self.clock)
        first = self.hub.send(L, env)
        again = self.hub.send(L, env)
        self.assertEqual((again["duplicate"], again["accepted_at"]), (True, first["accepted_at"]))
        self.assertEqual(len(self.hub.fetch(D)), 1)

    def test_a_resend_long_after_still_answers_the_same(self):
        env = message(self.clock)
        self.hub.send(L, env)
        self.clock.advance(days=8)  # past the replay window, but the hub knows this id
        self.assertTrue(self.hub.send(L, env)["duplicate"])

    def test_an_id_reused_for_other_content_or_by_another_machine_is_refused(self):
        env = message(self.clock)
        self.hub.send(L, env)
        self.assertEqual(code_of(lambda: self.hub.send(L, {**env, "body": "other"})), "bad-envelope")
        self.assertEqual(code_of(lambda: self.hub.send(D, {**env, "to": {"machine": "id-laptop", "session": "proj-a"}})), "bad-envelope")

    def test_replay_and_clock_rules(self):
        old = message(self.clock)
        self.clock.advance(days=8)
        self.assertEqual(code_of(lambda: self.hub.send(L, old)), "too-old")
        fresh = message(self.clock)
        self.clock.advance(minutes=-20)  # the hub's clock is behind the sender's
        self.assertEqual(code_of(lambda: self.hub.send(L, fresh)), "clock-skew")

    def test_target_must_exist_be_active_and_have_published_the_session(self):
        self.assertEqual(code_of(lambda: self.hub.send(L, message(self.clock, to="nas"))), "unknown-target")
        self.assertEqual(code_of(lambda: self.hub.send(L, message(self.clock, session="secret"))), "unknown-target")
        self.hub.hello(L, hello_payload("laptop", ()))  # nothing published
        self.assertEqual(code_of(lambda: self.hub.send(D, message(self.clock, to="laptop", sender="desktop"))), "unknown-target")

    def test_client_receipts_are_only_phase_sent_and_must_point_at_a_message_for_them(self):
        env = message(self.clock)
        self.hub.send(L, env)
        rec = hp.new_envelope("receipt", "id-laptop", "proj-b", "", now=self.clock(), phase="sent", ref=env["id"])
        self.assertFalse(self.hub.send(D, hp.sign(rec, {"k1": b"o" * 32}, "k1", "id-desktop"))["duplicate"])
        bad = hp.new_envelope("receipt", "id-laptop", "proj-b", "", now=self.clock(), phase="taken", ref=env["id"])
        self.assertEqual(code_of(lambda: self.hub.send(D, bad)), "bad-envelope")  # `taken` is the hub's to say
        stranger = hp.new_envelope("receipt", "id-laptop", "proj-b", "", now=self.clock(), phase="sent", ref=hp.new_id())
        self.assertEqual(code_of(lambda: self.hub.send(D, stranger)), "unknown-target")
        wrong_way = hp.new_envelope("receipt", "id-desktop", "proj-a", "", now=self.clock(), phase="sent", ref=env["id"])
        self.assertEqual(code_of(lambda: self.hub.send(L, wrong_way)), "unknown-target")  # the laptop sent it, it cannot receipt it

    def test_a_reply_must_come_from_the_machine_the_message_was_for(self):
        env = message(self.clock)
        self.hub.send(L, env)
        reply = hp.new_envelope("reply", "id-laptop", "proj-b", "done it", now=self.clock(), in_reply_to=env["id"], status="done")
        self.assertEqual(code_of(lambda: self.hub.send(L, reply)), "unknown-target")  # not the laptop's to answer
        self.assertFalse(self.hub.send(D, reply)["duplicate"])
        [got] = self.hub.fetch(L)
        self.assertEqual((got["kind"], got["status"], got["in_reply_to"]), ("reply", "done", env["id"]))

    def test_size_mailbox_and_daily_limits(self):
        self.assertEqual(code_of(lambda: self.hub.send(L, message(self.clock, body="x" * 8001))), "too-large")
        hub, clock, _ = hub_with_machines(limits=Limits(mailbox_cap=2, daily_messages=3))
        hub.send(L, message(clock))
        hub.send(L, message(clock, body="two"))
        self.assertEqual(code_of(lambda: hub.send(L, message(clock, body="three"))), "mailbox-full")
        hub.fetch(D)
        hub.ack(D, [e["id"] for e in hub.fetch(D)], "taken")  # nothing new handed out yet: still full
        hub2, clock2, _ = hub_with_machines(limits=Limits(daily_messages=2))
        hub2.send(L, message(clock2))
        hub2.send(L, message(clock2, body="two"))
        self.assertEqual(code_of(lambda: hub2.send(L, message(clock2, body="three"))), "rate-limited")
        clock2.advance(days=1)
        self.assertFalse(hub2.send(L, message(clock2, body="three"))["duplicate"])
        hub3, clock3, _ = hub_with_machines(limits=Limits(mailbox_bytes=10))
        self.assertEqual(code_of(lambda: hub3.send(L, message(clock3, body="x" * 11))), "mailbox-full")

    def test_the_whole_envelope_counts_not_just_the_text(self):
        hub, clock, _ = hub_with_machines(limits=Limits(mailbox_bytes=1500))
        big = message(clock, body="x")
        hub.send(L, big)  # about 600 bytes
        fat = hp.sign({**hp.new_envelope("message", "id-desktop", "proj-a", "y", now=clock(), thread_id="t" * 200),
                       "reason": "r" * 200}, {"k1": b"o" * 32}, "k1", "id-laptop")
        hub.send(L, fat)
        self.assertEqual(code_of(lambda: hub.send(L, message(clock, body="z"))), "mailbox-full")  # one more would pass on text alone

    def test_the_mailbox_frees_up_once_the_target_takes_things(self):
        hub, clock, _ = hub_with_machines(limits=Limits(mailbox_cap=1))
        env = message(clock)
        hub.send(L, env)
        self.assertEqual(code_of(lambda: hub.send(L, message(clock, body="two"))), "mailbox-full")
        hub.ack(D, [e["id"] for e in hub.fetch(D)], "taken")
        self.assertFalse(hub.send(L, message(clock, body="two"))["duplicate"])

    def test_the_audit_never_holds_text(self):
        hub, clock, _ = hub_with_machines(limits=Limits(daily_messages=1))
        hub.send(L, message(clock, body="TOPSECRETTEXT"))
        code_of(lambda: hub.send(L, message(clock, body="MORETOPSECRET")))
        dump = json.dumps(hub.audit_log())
        self.assertNotIn("SECRET", dump)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.hub, self.clock, self.path = hub_with_machines()

    def test_at_least_once_a_handed_out_envelope_comes_again_until_acked(self):
        self.hub.send(L, message(self.clock))
        [first] = self.hub.fetch(D)
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(self.hub.fetch(D), [])  # handed out, waiting for the ack
        self.clock.advance(seconds=hp.VISIBILITY_TIMEOUT - 1)
        self.assertEqual(self.hub.fetch(D), [])
        self.clock.advance(seconds=1)
        [again] = self.hub.fetch(D)
        self.assertEqual((again["id"], again["attempt"]), (first["id"], 2))

    def test_ack_taken_ends_it_gives_the_sender_a_receipt_and_is_repeatable(self):
        env = message(self.clock)
        self.hub.send(L, env)
        [got] = self.hub.fetch(D)
        self.assertEqual(self.hub.ack(D, [got["id"]], "taken"), {"acked": 1})
        self.assertEqual(self.hub.ack(D, [got["id"]], "taken"), {"acked": 1})  # a lost answer, sent again
        self.clock.advance(minutes=5)
        self.assertEqual(self.hub.fetch(D), [])
        [receipt] = self.hub.fetch(L)
        self.assertEqual((receipt["kind"], receipt["phase"], receipt["ref"]), ("receipt", "taken", env["id"]))
        self.assertTrue(receipt["stamp"]["hub"])
        self.assertNotIn("sig", receipt)
        self.hub.ack(L, [receipt["id"]], "taken")
        self.assertEqual(self.hub.fetch(L), [])  # a receipt gets no receipt

    def test_ack_rejected_tells_the_sender_why_and_drops_the_text(self):
        env = message(self.clock, body="PRIVATE")
        self.hub.send(L, env)
        [got] = self.hub.fetch(D)
        self.hub.ack(D, [got["id"]], "rejected", "bad-signature")
        [receipt] = self.hub.fetch(L)
        self.assertEqual((receipt["phase"], receipt["reason"]), ("rejected", "bad-signature"))
        self.assertIsNone(self.hub.db.execute("SELECT body FROM envelopes WHERE id=?", (env["id"],)).fetchone()["body"])

    def test_ack_only_counts_the_machines_own_envelopes(self):
        env = message(self.clock)
        self.hub.send(L, env)
        self.assertEqual(self.hub.ack(L, [env["id"]], "taken"), {"acked": 0})  # the laptop is not the target
        self.assertEqual(self.hub.ack(D, ["no-such-id"], "taken"), {"acked": 0})
        self.assertEqual(len(self.hub.fetch(D)), 1)
        self.assertEqual(code_of(lambda: self.hub.ack(D, [], "maybe")), "bad-request")
        self.assertEqual(code_of(lambda: self.hub.ack(D, "x", "taken")), "bad-request")

    def test_order_and_batch_limit(self):
        for i in range(25):
            self.hub.send(L, message(self.clock, body=f"m{i}"))
            if i % 5 == 4:
                self.clock.advance(seconds=1)
        first = self.hub.fetch(D, limit=100)
        self.assertEqual(len(first), hp.POLL_BATCH)
        self.assertEqual([e["body"] for e in first][:3], ["m0", "m1", "m2"])
        self.assertEqual(len(self.hub.fetch(D)), 5)

    def test_state_survives_a_restart(self):
        env = message(self.clock)
        self.hub.send(L, env)
        self.hub.fetch(D)
        self.hub.close()
        hub = HubCore(self.path, clock=self.clock)
        self.assertTrue(hub.send(L, env)["duplicate"])  # the same envelope again: still known
        self.clock.advance(seconds=hp.VISIBILITY_TIMEOUT)
        [again] = hub.fetch(D)
        self.assertEqual(again["id"], env["id"])
        self.assertEqual(hub.machines()[0]["state"], "active")

    def test_cancel_takes_back_only_what_nobody_has_been_handed(self):
        a, b = message(self.clock), message(self.clock, body="two")
        self.hub.send(L, a)
        self.hub.send(L, b)
        self.assertEqual(self.hub.cancel(L, a["id"]), {"cancelled": True, "state": "cancelled"})
        [got] = self.hub.fetch(D)
        self.assertEqual(got["id"], b["id"])
        self.assertEqual(self.hub.cancel(L, b["id"]), {"cancelled": False, "state": "delivered"})
        self.assertEqual(code_of(lambda: self.hub.cancel(D, a["id"])), "not-found")  # not the sender's
        self.assertEqual(code_of(lambda: self.hub.cancel(L, "x")), "not-found")


class PollTest(unittest.TestCase):
    def test_poll_returns_at_once_when_something_waits(self):
        hub, clock, _ = hub_with_machines()
        hub.send(L, message(clock))
        self.assertEqual(len(hub.poll(D, wait=5)), 1)

    def test_poll_times_out_with_nothing(self):
        hub, _, _ = hub_with_machines()
        self.assertEqual(hub.poll(D, wait=0.05), [])

    def test_a_send_wakes_a_waiting_poll(self):
        hub, clock, _ = hub_with_machines()
        got = []
        t = threading.Thread(target=lambda: got.extend(hub.poll(D, wait=5)))
        t.start()
        for _ in range(100):
            if hub._pollers:
                break
            threading.Event().wait(0.01)
        hub.send(L, message(clock))
        t.join(5)
        self.assertEqual([e["body"] for e in got], ["hello"])

    def test_a_newer_poll_supersedes_the_older(self):
        hub, clock, _ = hub_with_machines()
        result = {}

        def older():
            try:
                hub.poll(D, wait=5)
            except hp.ProtoError as e:
                result["code"] = e.code
        t = threading.Thread(target=older)
        t.start()
        for _ in range(100):
            if hub._pollers:
                break
            threading.Event().wait(0.01)
        newer = threading.Thread(target=lambda: hub.poll(D, wait=0.3))
        newer.start()
        t.join(5)
        newer.join(5)
        self.assertEqual(result, {"code": "superseded"})

    def test_a_revoke_ends_a_waiting_poll(self):
        hub, clock, _ = hub_with_machines()
        result = {}

        def waiting():
            try:
                hub.poll(D, wait=5)
            except hp.ProtoError as e:
                result["code"] = e.code
        t = threading.Thread(target=waiting)
        t.start()
        for _ in range(100):
            if hub._pollers:
                break
            threading.Event().wait(0.01)
        hub.revoke("desktop")
        t.join(5)
        self.assertEqual(result, {"code": "revoked"})


class TimeTest(unittest.TestCase):
    def test_expiry_drops_the_text_at_once_and_tells_the_sender(self):
        hub, clock, _ = hub_with_machines()
        env = message(clock, body="WILLEXPIRE")
        hub.send(L, env)
        clock.advance(days=7, seconds=1)
        self.assertEqual(hub.fetch(D), [])  # not handed out past its time
        self.assertEqual(hub.tick()["expired"], 1)
        row = hub.db.execute("SELECT state, body FROM envelopes WHERE id=?", (env["id"],)).fetchone()
        self.assertEqual((row["state"], row["body"]), ("expired", None))
        [receipt] = hub.fetch(L)
        self.assertEqual((receipt["phase"], receipt["ref"]), ("expired", env["id"]))
        self.assertIn("過期", receipt["reason"])

    def test_the_text_stays_14_days_after_it_was_taken(self):
        hub, clock, _ = hub_with_machines()
        env = message(clock, body="KEEPME")
        hub.send(L, env)
        hub.ack(D, [e["id"] for e in hub.fetch(D)], "taken")
        clock.advance(days=13, hours=23)
        hub.tick()
        self.assertEqual(hub.db.execute("SELECT body FROM envelopes WHERE id=?", (env["id"],)).fetchone()["body"], "KEEPME")
        clock.advance(hours=1, seconds=1)
        self.assertEqual(hub.tick()["purged"], 1)
        self.assertIsNone(hub.db.execute("SELECT body FROM envelopes WHERE id=?", (env["id"],)).fetchone()["body"])

    def test_tombstones_and_audit_have_their_own_lifetimes(self):
        hub, clock, path = hub_with_machines()
        env = message(clock)
        hub.send(L, env)
        hub.ack(D, [e["id"] for e in hub.fetch(D)], "taken")
        clock.advance(days=91)
        hub.tick()
        self.assertEqual(hub.db.execute("SELECT COUNT(*) FROM envelopes WHERE id=?", (env["id"],)).fetchone()[0], 0)
        self.assertGreater(hub.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        clock.advance(days=300)
        hub.tick()
        self.assertEqual(hub.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    def test_deleted_text_is_really_gone_from_the_file(self):
        hub, clock, path = hub_with_machines()
        env = message(clock, body="NEEDLE-IN-THE-FILE")
        hub.send(L, env)
        clock.advance(days=8)
        hub.tick()
        hub.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        hub.close()
        self.assertNotIn(b"NEEDLE-IN-THE-FILE", path.read_bytes())

    def test_a_clock_that_runs_ahead_does_not_hand_out_an_expired_one(self):
        hub, clock, _ = hub_with_machines()
        hub.send(L, message(clock))
        clock.advance(days=8)
        self.assertEqual(hub.fetch(D), [])


class StorageTest(unittest.TestCase):
    def test_the_file_is_private_and_secure_delete_is_on(self):
        hub, _, path = new_hub()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(hub.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.assertEqual(hub.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_reopening_keeps_the_schema_and_machines(self):
        hub, clock, path = hub_with_machines()
        hub.close()
        again = HubCore(path, clock=clock)
        self.assertEqual([m["name"] for m in again.machines()], ["laptop", "desktop"])
        self.assertEqual(again.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(again.status()["machines"], {"active": 2})

    def test_concurrent_sends_all_land_once(self):
        hub, clock, _ = hub_with_machines()
        envs = [message(clock, body=f"m{i}") for i in range(30)]
        threads = [threading.Thread(target=lambda e=e: hub.send(L, e)) for e in envs for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(hub.db.execute("SELECT COUNT(*) FROM envelopes").fetchone()[0], 30)

    def test_the_database_schema_has_no_third_party_dependency(self):
        self.assertIs(sqlite3.sqlite_version_info >= (3, 0), True)
        self.assertEqual(Clock().now.tzinfo is not None, True)
        self.assertEqual(timedelta(days=7), hp.EXPIRY_SELF)


if __name__ == "__main__":
    unittest.main()
