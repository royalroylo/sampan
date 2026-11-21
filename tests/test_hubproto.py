import unittest
from datetime import datetime, timedelta, timezone

from sampan import hubproto as hp

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
KEY = b"k" * 32
KEYS = {"k1": KEY}
M = "m-laptop"


def msg(**kw):
    env = hp.new_envelope("message", "m-desktop", "proj-a", kw.pop("body", "hello"), now=NOW, from_session="proj-b", **kw)
    return env


class CanonicalTest(unittest.TestCase):
    def test_canonical_sorts_keys_and_keeps_unicode(self):
        self.assertEqual(hp.canonical({"b": 1, "a": "讀書"}), '{"a":"讀書","b":1}'.encode("utf-8"))
        self.assertEqual(hp.canonical({"a": [1, {"d": 2, "c": 3}]}), b'{"a":[1,{"c":3,"d":2}]}')

    def test_fixed_test_vector(self):
        # a vector other implementations can check: the signature of a fixed envelope under a fixed key
        env = {"v": 1, "id": "11111111-1111-4111-8111-111111111111", "kind": "message",
               "to": {"machine": "m-desktop", "session": "proj-a"}, "from_session": "proj-b", "thread_id": None,
               "in_reply_to": None, "created": "2026-11-21T12:00:00Z", "expires": "2026-11-28T12:00:00Z", "body": "héllo 讀書"}
        self.assertEqual(hp.canonical(hp.signed_core(env, M)).decode("utf-8"),
                         '{"body_sha256":"af25925cfa644f48e8069ddd13326a040a2ac3658ce313b61ec8148b48c71779",'
                         '"created":"2026-11-21T12:00:00Z","expires":"2026-11-28T12:00:00Z","from_session":"proj-b",'
                         '"id":"11111111-1111-4111-8111-111111111111","in_reply_to":null,"kind":"message","phase":null,'
                         '"reason":"","ref":null,"sender_machine":"m-laptop","status":null,"thread_id":null,'
                         '"to":{"machine":"m-desktop","session":"proj-a"},"v":1}')
        signed = hp.sign(env, KEYS, "k1", M)
        # checked against `openssl dgst -sha256 -hmac` over the canonical bytes above, with a key of 32 "k"
        self.assertEqual(signed["sig"]["mac"], "oLSdw4miU6VuRr_K4PiKmNqKFAAPERKYjnRqUU1o9cc")
        self.assertTrue(hp.verify(signed, KEYS, M))


class SignatureTest(unittest.TestCase):
    def test_roundtrip(self):
        env = hp.sign(msg(), KEYS, "k1", M)
        self.assertTrue(hp.verify(env, KEYS, M))

    def test_a_changed_field_breaks_it(self):
        env = hp.sign(msg(), KEYS, "k1", M)
        for field, value in (("body", "hello!"), ("created", "2026-11-21T12:00:01Z"), ("expires", "2027-01-01T00:00:00Z"),
                             ("from_session", "other"), ("thread_id", "x"), ("kind", "reply"), ("id", hp.new_id())):
            self.assertFalse(hp.verify({**env, field: value}, KEYS, M), field)
        self.assertFalse(hp.verify({**env, "to": {"machine": "m-other", "session": "proj-a"}}, KEYS, M))
        self.assertFalse(hp.verify({**env, "to": {"machine": "m-desktop", "session": "x"}}, KEYS, M))

    def test_a_signature_made_for_one_machine_does_not_verify_as_another(self):
        env = hp.sign(msg(), KEYS, "k1", M)
        self.assertFalse(hp.verify(env, KEYS, "m-other"))

    def test_wrong_key_unknown_kid_and_malformed_sig(self):
        env = hp.sign(msg(), KEYS, "k1", M)
        self.assertFalse(hp.verify(env, {"k1": b"x" * 32}, M))
        self.assertFalse(hp.verify({**env, "sig": {**env["sig"], "kid": "k9"}}, KEYS, M))
        for bad in (None, "x", {}, {"kid": "k1", "alg": "none", "mac": "x"}, {"kid": "k1", "alg": "HMAC-SHA256", "mac": 5},
                    {"kid": "k1", "alg": "HMAC-SHA256", "mac": "!!!"}):
            self.assertFalse(hp.verify({**env, "sig": bad}, KEYS, M), bad)
        env.pop("sig")
        self.assertFalse(hp.verify(env, KEYS, M))

    def test_a_rotated_key_still_verifies_by_kid(self):
        old = hp.sign(msg(), {"k1": KEY}, "k1", M)
        self.assertTrue(hp.verify(old, {"k1": KEY, "k2": b"n" * 32}, M))

    def test_reply_and_receipt_fields_are_signed(self):
        rid = hp.new_id()
        reply = hp.sign(hp.new_envelope("reply", "m-desktop", "proj-a", "ok", now=NOW, in_reply_to=rid, status="done"),
                        KEYS, "k1", M)
        self.assertTrue(hp.verify(reply, KEYS, M))
        self.assertFalse(hp.verify({**reply, "status": "failed"}, KEYS, M))
        self.assertFalse(hp.verify({**reply, "in_reply_to": hp.new_id()}, KEYS, M))
        receipt = hp.sign(hp.new_envelope("receipt", "m-desktop", "proj-a", "", now=NOW, phase="sent", ref=rid), KEYS, "k1", M)
        self.assertFalse(hp.verify({**receipt, "phase": "taken"}, KEYS, M))

    def test_new_key_roundtrip(self):
        key = hp.new_key()
        self.assertEqual(len(hp.decode_key(key)), 32)
        self.assertNotEqual(hp.new_key(), key)


class EnvelopeTest(unittest.TestCase):
    def check(self, env, **kw):
        err = hp.check_envelope(env, now=kw.pop("now", NOW), **kw)
        return err.code if err else None

    def test_a_good_message_passes(self):
        self.assertIsNone(self.check(msg()))

    def test_bad_shapes(self):
        self.assertEqual(self.check(None), "bad-envelope")
        self.assertEqual(self.check({**msg(), "v": 2}), "upgrade-required")
        self.assertEqual(self.check({**msg(), "id": "nope"}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "kind": "x"}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "to": {"machine": "m"}}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "to": {"person": "alice"}}), "unsupported-target")
        self.assertEqual(self.check({**msg(), "body": 5}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "body": "   "}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "from_session": "x\ny"}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "from_session": "x" * 101}), "bad-envelope")

    def test_too_large(self):
        self.assertEqual(self.check(msg(body="x" * (hp.MAX_BODY + 1))), "too-large")
        self.assertIsNone(self.check(msg(body="x" * hp.MAX_BODY)))

    def test_time_rules(self):
        self.assertEqual(self.check(msg(), now=NOW + hp.EXPIRY_SELF + timedelta(seconds=1)), "too-old")  # replay
        self.assertEqual(self.check(msg(), now=NOW - hp.SKEW_REJECT - timedelta(seconds=1)), "clock-skew")
        self.assertIsNone(self.check(msg(), now=NOW - timedelta(minutes=14)))
        short = hp.new_envelope("message", "m", "s", "x", now=NOW, expires_in=timedelta(hours=1))
        self.assertEqual(self.check(short, now=NOW + timedelta(hours=2)), "expired")
        self.assertEqual(self.check({**msg(), "expires": "2026-11-21T11:00:00Z"}), "bad-envelope")  # before created
        self.assertEqual(self.check({**msg(), "created": "yesterday"}), "bad-envelope")

    def test_reply_and_receipt_need_their_fields(self):
        rid = hp.new_id()
        good_reply = hp.new_envelope("reply", "m", "s", "ok", now=NOW, in_reply_to=rid, status="done")
        self.assertIsNone(self.check(good_reply))
        self.assertEqual(self.check({**good_reply, "status": "maybe"}), "bad-envelope")
        self.assertEqual(self.check({**good_reply, "in_reply_to": None}), "bad-envelope")
        good_receipt = hp.new_envelope("receipt", "m", "s", "", now=NOW, phase="sent", ref=rid)
        self.assertIsNone(self.check(good_receipt))
        self.assertEqual(self.check({**good_receipt, "phase": "read"}), "bad-envelope")
        self.assertEqual(self.check({**good_receipt, "body": "text"}), "bad-envelope")

    def test_free_text_fields_are_bounded(self):
        for field in ("thread_id", "in_reply_to", "reason"):
            self.assertEqual(self.check({**msg(), field: "x" * 201}), "bad-envelope", field)
            self.assertEqual(self.check({**msg(), field: 5}), "bad-envelope", field)
        self.assertEqual(self.check({**msg(), "to": {"machine": "m" * 201, "session": "s"}}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "to": {"machine": "m", "session": "s" * 201}}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "sig": {"kid": "k1", "alg": "HMAC-SHA256", "mac": "x" * 101}}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "sig": {"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"}}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "sig": "text"}), "bad-envelope")
        self.assertIsNone(self.check({**msg(), "thread_id": "x" * 200}))

    def test_control_and_format_characters_never_pass(self):
        # an ESC in the text could end a bracketed paste early so the rest is typed as keys (checked with tmux -L)
        for text in ("a\x1b[201~\rINJECT", "x\ry", "x\x00y", "x\x7fy", "a\u2028b", "a\u2029b", "a\u202eb", "a\u200bb",
                     "a\ufeffb", "a\u200db", "a\x85b", "a\x9bb"):
            self.assertEqual(self.check(msg(body=text)), "bad-envelope", repr(text))
            self.assertIsNotNone(hp.unsafe_char(text), repr(text))
        for field in ("reason", "thread_id"):
            self.assertEqual(self.check({**msg(), field: "a\x1bb"}), "bad-envelope", field)
        self.assertEqual(self.check({**msg(), "from_session": "a\u200bb"}), "bad-envelope")
        self.assertEqual(self.check({**msg(), "to": {"machine": "m\x1b", "session": "s"}}), "bad-envelope")

    def test_newline_tab_and_ordinary_text_pass(self):
        for text in ("two\nlines", "tab\there", "讀書 PM — ✓", "emoji 🙂", "  indented\n    more"):
            self.assertIsNone(self.check(msg(body=text)), repr(text))
            self.assertIsNone(hp.unsafe_char(text))

    def test_clean_newlines_makes_crlf_and_cr_newlines_only(self):
        self.assertEqual(hp.clean_newlines("a\r\nb\rc\nd"), "a\nb\nc\nd")
        self.assertEqual(hp.clean_newlines(None), "")

    def test_expires_cannot_be_further_out_than_the_kind_allows(self):
        far = hp.new_envelope("message", "m", "s", "x", now=NOW, expires_in=hp.EXPIRY_SELF + timedelta(seconds=1))
        self.assertEqual(self.check(far), "bad-envelope")
        edge = hp.new_envelope("message", "m", "s", "x", now=NOW, expires_in=hp.EXPIRY_SELF)
        self.assertIsNone(self.check(edge))
        self.assertEqual(hp.check_envelope(edge, now=NOW, max_lifetime=hp.EXPIRY_PERSON).code, "bad-envelope")  # across people: 48 h

    def test_a_whole_envelope_has_a_size_limit(self):
        self.assertLess(hp.envelope_size(msg()), 1000)
        self.assertGreater(hp.envelope_size(msg(body="é" * 8000)), 8000)  # bytes, not characters
        self.assertGreater(hp.envelope_size({"x": object()}), hp.MAX_ENVELOPE)

    def test_client_fields_drops_stamp_and_from(self):
        env = {**msg(), "stamp": {"sender_machine": "forged"}, "from": "alice", "owner": "self"}
        out = hp.client_fields(env)
        self.assertNotIn("stamp", out)
        self.assertNotIn("from", out)
        self.assertNotIn("owner", out)
        self.assertEqual(out["id"], env["id"])

    def test_parse_time_needs_a_timezone(self):
        self.assertEqual(hp.parse_time("2026-11-21T12:00:00Z"), NOW)
        self.assertEqual(hp.parse_time("2026-11-21T20:00:00+08:00"), NOW)
        self.assertIsNone(hp.parse_time("2026-11-21T12:00:00"))
        self.assertIsNone(hp.parse_time(None))


class AddressingTest(unittest.TestCase):
    def test_parse_target(self):
        self.assertEqual(hp.parse_target("laptop/proj-a"), ("device", "laptop", "proj-a"))
        self.assertEqual(hp.parse_target("@alice"), ("person", "alice"))
        self.assertIsNone(hp.parse_target("plain"))
        self.assertIsNone(hp.parse_target("/proj-a"))
        self.assertIsNone(hp.parse_target("laptop/"))
        self.assertIsNone(hp.parse_target("@"))
        self.assertIsNone(hp.parse_target("la\x00ptop/proj"))
        self.assertIsNone(hp.parse_target(None))

    def test_resolution_order_local_first(self):
        local, people = {"alice", "proj-a"}, {"alice", "bob"}
        self.assertEqual(hp.resolve_target("alice", local, people), ("local", "alice"))  # same name: local wins
        self.assertEqual(hp.resolve_target("@alice", local, people), ("person", "alice"))  # @ forces the person
        self.assertEqual(hp.resolve_target("bob", local, people), ("person", "bob"))
        self.assertEqual(hp.resolve_target("laptop/proj-a", local, people), ("device", "laptop", "proj-a"))
        self.assertIsNone(hp.resolve_target("nobody", local, people))
        self.assertIsNone(hp.resolve_target("/x", local, people))

    def test_names_cannot_carry_control_format_or_separator_characters(self):
        for bad in ("la\x9bptop", "lap\u202etop", "lap\u200btop", "lap\u2028top", "lap\ntop", "lap\ttop", "lap\x1btop", "lap\ufefftop"):
            self.assertFalse(hp.valid_name(bad), repr(bad))
            self.assertIsNotNone(hp.unsafe_name(bad), repr(bad))
        self.assertIsNone(hp.unsafe_name("讀書 PM-1"))
        self.assertEqual(hp.check_envelope({**msg(), "from_session": "a\nb"}, now=NOW).code, "bad-envelope")
        self.assertEqual(hp.check_envelope({**msg(), "from_session": "a\u202eb"}, now=NOW).code, "bad-envelope")

    def test_valid_name(self):
        for ok in ("laptop", "desktop-mini", "讀書", "a" * 64):
            self.assertTrue(hp.valid_name(ok), ok)
        for bad in ("", " x", "x ", "a/b", "a\nb", "a" * 65, None):
            self.assertFalse(hp.valid_name(bad), bad)

    def test_user_agent_is_never_the_python_default(self):
        ua = hp.user_agent("0.26.0")
        self.assertTrue(ua.startswith("sampan-connect/"))
        self.assertNotIn("Python-urllib", ua)


if __name__ == "__main__":
    unittest.main()
