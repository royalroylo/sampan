"""Deliveries and reply parts in dispatches.db, the 〔回覆 id〕 split and the text of a group paste."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan import deliver, deliveries, dispatch, store
from sampan.clock import fmt_local, parse_iso

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def message(did, created=NOW, **kw):
    fields = dict(from_name="alpha-pm", from_profile="alice", from_agent="claude", to_name="beta-dev",
                  to_agent="claude", to_profile="alice", text=deliver.compose("alpha-pm", "alice", did, f"body {did}"),
                  state="queued", created=created.isoformat(timespec="seconds"), to_id="T1")
    fields.update(kw)
    return dispatch.Dispatch(did, **fields)


class StoreBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.conn = store.connect(self.state, create=True)
        self.addCleanup(self.conn.close)

    def put(self, *ds):
        items = dispatch.Items(self.conn)
        items.extend(ds)
        items.flush()

    def open(self, ids, to_id="T1", kind="paste"):
        return deliveries.open_delivery(self.conn, kind=kind, to_id=to_id, to_name="beta-dev", messages=list(ids),
                                        text="t", conversation="c", cursor=None, now=NOW)


class DeliveryTest(StoreBase):
    def test_one_active_delivery_per_target(self):
        did = self.open(["a1a1", "b2b2"])
        self.assertTrue(did.startswith("d") and len(did) == 7)
        self.assertEqual(deliveries.active_delivery(self.conn, "T1")["id"], did)
        with self.assertRaises(deliveries.ActiveDelivery):
            self.open(["c3c3"])
        self.open(["c3c3"], to_id="T2")  # another target is fine
        deliveries.end_delivery(self.conn, did, NOW)
        self.assertIsNone(deliveries.active_delivery(self.conn, "T1"))
        self.open(["c3c3"])

    def test_members_by_position_without_archived(self):
        did = self.open(["b2b2", "a1a1", "c3c3"])
        deliveries.archive_member(self.conn, did, "a1a1")
        self.assertEqual(deliveries.members(self.conn, did), ["b2b2", "c3c3"])
        self.assertEqual(deliveries.members(self.conn, did, archived=True), ["b2b2", "a1a1", "c3c3"])
        self.assertIsNone(deliveries.delivery_of(self.conn, "a1a1"))
        self.assertEqual(deliveries.delivery_of(self.conn, "b2b2")["id"], did)

    def test_add_part_makes_one_reply_and_caches_the_latest(self):
        self.put(message("a1a1", state="sent"))
        did = self.open(["a1a1"])
        p1 = deliveries.add_part(self.conn, delivery_id=did, message="a1a1", text="first", how="whole", now=NOW)
        p2 = deliveries.add_part(self.conn, delivery_id=did, message="a1a1", text="late one", how="tool",
                                 now=NOW + timedelta(seconds=9), late=True)
        self.assertTrue(p1.startswith("p") and len(p1) == 9)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 1)
        row = self.conn.execute("SELECT reply, reply_len FROM messages WHERE id='a1a1'").fetchone()
        self.assertEqual(tuple(row), ("first", 5))  # a late part is not the reply
        got = deliveries.parts(self.conn, "a1a1")
        self.assertEqual([(p["id"], p["how"], p["late"], p["latest"]) for p in got],
                         [(p1, "whole", False, True), (p2, "tool", True, False)])

    def test_latest_part_rules(self):
        self.put(message("a1a1", state="sent"))
        did = self.open(["a1a1"])
        self.assertIsNone(deliveries.latest_part(self.conn, "a1a1"))
        parts = [deliveries.add_part(self.conn, delivery_id=did, message="a1a1", text=t, how="whole", now=NOW)
                 for t in ("x", "y")]
        self.assertEqual(deliveries.latest_part(self.conn, "a1a1")["id"], max(parts))  # same time: the larger id
        deliveries.add_part(self.conn, delivery_id=did, message="a1a1", text="late", how="tool",
                            now=NOW + timedelta(seconds=5), late=True)
        self.assertEqual(deliveries.latest_part(self.conn, "a1a1")["id"], max(parts))  # late never counts
        newer = deliveries.add_part(self.conn, delivery_id=did, message="a1a1", text="z", how="tool",
                                    now=NOW + timedelta(seconds=6))
        self.assertEqual(deliveries.latest_part(self.conn, "a1a1")["id"], newer)


class SplitMarkersTest(unittest.TestCase):
    def test_markers_at_line_start_of_members_only(self):
        reply = ("intro\n〔回覆 aaaa〕\nfor a\nmore a\n看到〔回覆 bbbb〕在行中\n〔回覆 zzzz〕 not a member\n"
                 "〔回覆 cccc〕for c")
        self.assertEqual(deliveries.split_markers(reply, ["aaaa", "bbbb", "cccc"]),
                         {"aaaa": "for a\nmore a\n看到〔回覆 bbbb〕在行中\n〔回覆 zzzz〕 not a member", "cccc": "for c"})

    def test_no_markers_is_empty(self):
        self.assertEqual(deliveries.split_markers("just text", ["aaaa"]), {})
        self.assertEqual(deliveries.split_markers(None, ["aaaa"]), {})

    def test_empty_section_counts_as_missing(self):
        self.assertEqual(deliveries.split_markers("〔回覆 aaaa〕\n〔回覆 bbbb〕b", ["aaaa", "bbbb"]), {"bbbb": "b"})


class ComposeGroupTest(unittest.TestCase):
    def test_header_and_numbered_items_without_their_source_lines(self):
        a = message("a1a1", text=deliver.compose("alpha-pm", "alice", "a1a1", "first\nline two"))
        b = message("b2b2", created=NOW + timedelta(minutes=1), from_name="終端機", from_profile="",
                    text=deliver.compose(None, "", "b2b2", "second"))
        when = fmt_local(parse_iso(a.created))
        when_b = fmt_local(parse_iso(b.created))
        self.assertEqual(deliver.compose_group([a, b]), (
            "〔sampan 轉送｜2 則，依時間順序；前後衝突時以後面的為準｜每則請用〔回覆 <派工id>〕開頭分段回覆，sampan 會帶回〕\n\n"
            f"【1／2｜派工 a1a1｜來自 alpha-pm（alice）｜{when}】\nfirst\nline two\n\n"
            f"【2／2｜派工 b2b2｜來自 終端機｜{when_b}】\nsecond"))


if __name__ == "__main__":
    unittest.main()
