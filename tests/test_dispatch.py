import dataclasses
import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan import dispatch, store
from sampan.dispatch import Dispatch, DispatchError
from sampan.send import REASON_SENDING

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def mk(did="ab12", state="queued", created=None, **kw):
    return Dispatch(id=did, from_name="a", from_profile="p", from_agent="claude", to_name="b", to_agent="codex",
                    to_profile="q", text="hi", state=state, created=created or NOW.isoformat(timespec="seconds"), **kw)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "state"

    def put(self, *items):
        with dispatch.transaction(self.dir) as cur:
            cur.extend(items)

    def sql(self, query, *args):
        conn = store.connect(self.dir)
        try:
            return conn.execute(query, args).fetchall()
        finally:
            conn.close()

    def test_missing_db_and_file_is_empty(self):
        self.assertEqual(dispatch.load(self.dir), [])
        self.assertFalse((self.dir / store.DB_FILE).exists())

    def test_round_trip_and_permissions(self):
        items = [mk("aaaa", reply="x", notify=True, thread_id="t1"), mk("bbbb", "done", sent_at=ago(1), done_at=ago(0))]
        self.put(*items)
        self.assertEqual(dispatch.load(self.dir), items)
        self.assertEqual(stat.S_IMODE((self.dir / store.DB_FILE).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.dir / "dispatches.lock").stat().st_mode), 0o600)

    def test_roundtrip_keeps_fields_and_extra(self):
        d = mk("aaaa", raw=True, batch="b1", batch_skipped=2, startup=True, to_id="s1", from_id="s2", machine="m1",
               in_reply_to="cccc", thread="t9", status="rung", extra={"future": 1})
        self.put(d)
        [back] = dispatch.load(self.dir)
        self.assertEqual(back, d)
        self.assertEqual(back.extra, {"future": 1})
        self.assertEqual(self.sql("SELECT extra, text_len, reply_len FROM messages")[0][:], ('{"future": 1}', 2, None))

    def test_status_is_derived_from_state_until_set(self):
        self.put(mk("aaaa"))
        self.assertEqual(self.sql("SELECT status FROM messages")[0][0], "queued")
        with dispatch.transaction(self.dir) as cur:
            cur[0].state = "sent"
        self.assertEqual(self.sql("SELECT status FROM messages")[0][0], "pasted")
        with dispatch.transaction(self.dir) as cur:
            cur[0].state, cur[0].status = "sent", "taken"
        self.assertEqual(self.sql("SELECT status FROM messages")[0][0], "taken")
        self.assertEqual(dispatch.load(self.dir)[0].status, "taken")
        with dispatch.transaction(self.dir) as cur:
            cur[0].state = "done"  # status left alone while the state moved: derived again
        self.assertEqual(self.sql("SELECT status FROM messages")[0][0], "replied")

    def test_unknown_key_survives_a_change_to_the_same_dispatch_and_replace(self):
        self.put(mk("aaaa", extra={"future_field": {"n": 1}}), mk("bbbb"))
        with dispatch.transaction(self.dir) as cur:
            cur[0] = dataclasses.replace(cur[0], state="sent")
            cur[1].state = "sent"
        by_id = {d.id: d for d in dispatch.load(self.dir)}
        self.assertEqual(by_id["aaaa"].extra, {"future_field": {"n": 1}})
        self.assertEqual((by_id["aaaa"].state, by_id["bbbb"].state, by_id["bbbb"].extra), ("sent", "sent", {}))

    def test_transaction_writes_back_only_changes_and_new_items(self):
        self.put(mk("aaaa"), mk("bbbb"))
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        with dispatch.transaction(self.dir) as cur:
            cur[0].reason = "changed"
            cur.append(mk("cccc"))
            # a row nobody changed must not be written: this other connection's edit (made inside the lock) survives
            cur.conn.execute("UPDATE messages SET reason='by sql' WHERE id='bbbb'")
        rows = {r[0]: r[1] for r in conn.execute("SELECT id, reason FROM messages")}
        self.assertEqual(rows, {"aaaa": "changed", "bbbb": "by sql", "cccc": ""})

    def test_removing_from_the_list_deletes_nothing(self):
        self.put(mk("aaaa"), mk("bbbb"))
        with dispatch.transaction(self.dir) as cur:
            del cur[0]
        self.assertEqual([d.id for d in dispatch.load(self.dir)], ["aaaa", "bbbb"])

    def test_items_carry_the_connection_and_every_id(self):
        self.put(mk("aaaa", "done", created=ago(100), done_at=ago(100)))
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        conn.execute("DELETE FROM meta")
        conn.commit()
        with dispatch.transaction(self.dir, keep_days=90) as cur:
            self.assertEqual(cur, [])  # purged on the way in
            self.assertEqual(cur.ids, {"aaaa"})
            cur.append(mk("bbbb"))
            self.assertEqual(cur.ids, {"aaaa", "bbbb"})
            self.assertEqual(cur.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)  # the new one is written on exit

    def test_from_kind_round_trips_and_is_inferred(self):
        self.put(mk("aaaa", from_kind="unregistered"), mk("bbbb"), dataclasses.replace(mk("cccc"), from_name="終端機", from_kind=None))
        self.assertEqual([d.from_kind for d in dispatch.load(self.dir)], ["unregistered", "session", "terminal"])

    def test_bad_state_and_from_kind_raise(self):
        self.put(mk("aaaa"))
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        for col, val in (("state", "weird"), ("from_kind", "admin")):
            conn.execute(f"UPDATE messages SET {col}=?", (val,))
            conn.commit()
            with self.assertRaises(DispatchError):
                dispatch.load(self.dir)
            conn.execute("UPDATE messages SET state='queued', from_kind='session'")
            conn.commit()

    def test_old_json_without_db_asks_for_migrate(self):
        self.dir.mkdir(parents=True)
        path = self.dir / "dispatches.json"
        for version in (1, 2):
            path.write_text(json.dumps({"version": version, "dispatches": []}))
            with self.assertRaises(DispatchError) as cm:
                dispatch.load(self.dir)
            self.assertIn("sampan migrate", str(cm.exception))
            with self.assertRaises(DispatchError):
                self.put(mk())
        self.assertFalse((self.dir / store.DB_FILE).exists())
        self.assertEqual(json.loads(path.read_text())["version"], 2)

    def test_stub_or_garbage_json_without_db_is_an_error_and_untouched(self):
        self.dir.mkdir(parents=True)
        path = self.dir / "dispatches.json"
        for text in ('{"version": 3, "dispatches": [], "moved_to": "dispatches.db"}', "{not json", '{"version": 9}'):
            path.write_text(text)
            with self.assertRaises(DispatchError):
                dispatch.load(self.dir)
            with self.assertRaises(DispatchError):
                self.put(mk())
            self.assertEqual(path.read_text(), text)

    def test_checkpoint_commits_before_the_block_ends(self):
        self.put(mk("aaaa"))
        with dispatch.transaction(self.dir) as cur:
            cur[0].state, cur[0].reason = "sent", REASON_SENDING
            dispatch.checkpoint(self.dir, cur)
            self.assertEqual(self.sql("SELECT reason FROM messages")[0][0], REASON_SENDING)  # another connection sees it
        with self.assertRaises(KeyboardInterrupt):
            with dispatch.transaction(self.dir) as cur:
                cur[0].state = "done"
                dispatch.checkpoint(self.dir, cur)
                cur[0].reply = "later"
                raise KeyboardInterrupt  # killed while delivering
        [d] = dispatch.load(self.dir)
        self.assertEqual((d.state, d.reply), ("done", None))

    def test_exception_in_transaction_writes_nothing(self):
        self.put(mk("aaaa"))
        with self.assertRaises(RuntimeError):
            with dispatch.transaction(self.dir) as cur:
                cur.append(mk("bbbb"))
                cur[0].state = "sent"
                raise RuntimeError("boom")
        [d] = dispatch.load(self.dir)
        self.assertEqual((d.id, d.state), ("aaaa", "queued"))

    def test_second_transaction_sees_first(self):
        self.put(mk("aaaa"))
        with dispatch.transaction(self.dir) as cur:
            cur[0].state = "sent"
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual(cur[0].state, "sent")
            cur[0].state = "done"
        self.assertEqual(dispatch.load(self.dir)[0].state, "done")

    def test_no_200_cap(self):
        self.put(*[mk(f"{i:04x}", created=(NOW - timedelta(minutes=i)).isoformat(timespec="seconds")) for i in range(250)])
        self.assertEqual(len(dispatch.load(self.dir)), 250)
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual(len(cur), 250)
        self.assertEqual(len(dispatch.load(self.dir)), 250)

    def test_load_is_by_created(self):
        self.put(mk("bbbb", created=ago(1)), mk("aaaa", created=ago(2)))
        self.assertEqual([d.id for d in dispatch.load(self.dir)], ["aaaa", "bbbb"])


class PurgeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "state"

    def seed(self, *items):
        with dispatch.transaction(self.dir) as cur:
            cur.extend(items)
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        return conn

    def test_purge_clears_bodies_after_keep_days_and_keeps_stats(self):
        conn = self.seed(mk("a001", "done", created=ago(95), done_at=ago(91), reply="the reply", extra={"k": 1}),
                         mk("a002", "done", created=ago(95), done_at=ago(89), reply="fresh"),
                         mk("a003", "queued", created=ago(200)), mk("a004", "failed", created=ago(91)),
                         mk("a005", "done", created=ago(100), done_at="garbage"))
        conn.execute("INSERT INTO deliveries (id, text) VALUES ('d1', 'body')")
        conn.execute("INSERT INTO deliveries (id, text) VALUES ('d2', 'body2')")
        conn.execute("INSERT INTO delivery_messages (delivery, message, position) VALUES ('d1', 'a001', 0)")
        conn.execute("INSERT INTO delivery_messages (delivery, message, position) VALUES ('d2', 'a002', 0)")
        conn.execute("INSERT INTO reply_parts (id, reply, message, text, text_len) VALUES ('p1', 'r', 'a001', 'part', 4)")
        conn.commit()
        self.assertEqual(sorted(dispatch.purge(conn, NOW, 90)), ["a001", "a004"])
        conn.commit()
        row = conn.execute("SELECT * FROM messages WHERE id='a001'").fetchone()
        self.assertEqual((row["text"], row["reply"], row["extra"], row["purged"]), (None, None, None, 1))
        self.assertEqual((row["text_len"], row["reply_len"], row["state"], row["done_at"]), (2, 9, "done", ago(91)))
        self.assertEqual(conn.execute("SELECT text FROM deliveries WHERE id='d1'").fetchone()[0], None)
        self.assertEqual(conn.execute("SELECT text FROM deliveries WHERE id='d2'").fetchone()[0], "body2")
        self.assertEqual(conn.execute("SELECT text FROM reply_parts").fetchone()[0], None)
        self.assertEqual(conn.execute("SELECT text FROM messages WHERE id='a002'").fetchone()[0], "hi")
        self.assertEqual(conn.execute("SELECT purged FROM messages WHERE id IN ('a003','a005')").fetchall()[0][0], 0)
        # the id stays taken, the row is no longer listed
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual(sorted(d.id for d in cur), ["a002", "a003", "a005"])
            self.assertIn("a001", cur.ids)

    def test_transaction_purges_at_most_once_an_hour(self):
        self.seed(mk("a001", "done", created=ago(95), done_at=ago(91)))
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        stamp = conn.execute("SELECT value FROM meta WHERE key='purged_at'").fetchone()[0]
        self.assertIsNotNone(stamp)
        self.assertEqual(conn.execute("SELECT purged FROM messages").fetchone()[0], 0)  # first run came before the row existed
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual(len(cur), 1)  # an hour has not passed
        conn.execute("UPDATE meta SET value=?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
        conn.commit()
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual(cur, [])
        self.assertEqual(conn.execute("SELECT purged FROM messages").fetchone()[0], 1)

    def test_keep_days_argument_and_outbox_removal(self):
        self.seed(mk("a001", "done", created=ago(10), done_at=ago(10)))
        dispatch.write_outbox(self.dir, "a001", "body")
        dispatch.write_outbox(self.dir, "a001-notice", "n")
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        conn.execute("DELETE FROM meta")
        conn.commit()
        with dispatch.transaction(self.dir, keep_days=5) as cur:
            self.assertEqual(cur, [])
        self.assertFalse(dispatch.outbox_path(self.dir, "a001").exists())
        self.assertFalse(dispatch.outbox_path(self.dir, "a001-notice").exists())


class IdAndOutboxTest(unittest.TestCase):
    def test_new_id_shape_and_avoids_existing(self):
        i = dispatch.new_id(set())
        self.assertRegex(i, r"^[0-9a-f]{4}$")
        existing = {f"{n:04x}" for n in range(0x10000) if n != 0xbeef}
        self.assertEqual(dispatch.new_id(existing), "beef")

    def test_outbox(self):
        with tempfile.TemporaryDirectory() as t:
            sd = Path(t) / "state"
            self.assertEqual(dispatch.outbox_path(sd, "ab12"), sd / "outbox" / "ab12.txt")
            p = dispatch.write_outbox(sd, "ab12", "多行\n文字")
            self.assertEqual(p.read_text(), "多行\n文字")
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
