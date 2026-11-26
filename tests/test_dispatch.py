import dataclasses
import json
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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

    def test_db_still_being_created_reads_as_empty(self):
        self.dir.mkdir(parents=True)
        sqlite3.connect(self.dir / store.DB_FILE).close()  # the file is there before its schema (another process is creating it)
        self.assertEqual(dispatch.load(self.dir), [])

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

    def test_sqlite_errors_surface_as_dispatch_error_and_write_nothing(self):
        self.put(mk("aaaa"))
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        conn.execute("CREATE TRIGGER no_update BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT, 'no'); END")
        conn.commit()
        with self.assertRaises(DispatchError):  # the upsert fails
            with dispatch.transaction(self.dir) as cur:
                cur[0].reason = "x"
        with self.assertRaises(DispatchError):  # and so does a checkpoint
            with dispatch.transaction(self.dir) as cur:
                cur[0].reason = "x"
                dispatch.checkpoint(self.dir, cur)
        conn.execute("DROP TRIGGER no_update")
        conn.execute("DROP TABLE meta")  # the purge on the way in fails
        conn.commit()
        with self.assertRaises(DispatchError):
            with dispatch.transaction(self.dir):
                pass
        self.assertEqual(dispatch.load(self.dir)[0].reason, "")

    def test_callers_own_sqlite_error_is_not_wrapped(self):
        self.put(mk("aaaa"))
        with self.assertRaises(sqlite3.OperationalError):
            with dispatch.transaction(self.dir) as cur:
                cur.conn.execute("SELECT * FROM no_such_table")

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

    def test_reverted_or_unmigrated_data_is_refused_with_or_without_a_db(self):
        cases = ((dispatch.REVERT_MARK, "", dispatch.REVERTED_NOTE),
                 (dispatch.MIGRATE_JOURNAL, "{}", dispatch.NOT_MIGRATED),
                 ("dispatches.json", '{"version": 1, "dispatches": []}', dispatch.NOT_MIGRATED),
                 ("dispatches.json", '{"version": 2, "dispatches": []}', dispatch.NOT_MIGRATED))
        for with_db in (False, True):
            for name, content, why in cases:
                with self.subTest(name=name, content=content, with_db=with_db):
                    with tempfile.TemporaryDirectory() as t:
                        sd = Path(t) / "state"
                        if with_db:
                            with dispatch.transaction(sd) as cur:
                                cur.append(mk("aaaa"))
                        else:
                            sd.mkdir()
                        (sd / name).write_text(content)
                        for use in (lambda: dispatch.transaction(sd).__enter__(), lambda: dispatch.load(sd)):
                            with self.assertRaises(DispatchError) as ctx:
                                use()
                            self.assertEqual(str(ctx.exception), why)
                        self.assertEqual((sd / store.DB_FILE).exists(), with_db)  # never created

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
        self.assertEqual(sorted(d.id for d in dispatch.load(self.dir)), ["a002", "a003", "a005"])
        with dispatch.transaction(self.dir, ids=["a001", "a002"]) as cur:
            self.assertEqual(sorted(d.id for d in cur), ["a002", "a003"])  # a purged one is never loaded
            self.assertIn("a001", cur.ids)

    def test_purge_keeps_owed_notices_and_unfinished_batches(self):
        when = dict(created=ago(3), done_at=ago(2))
        conn = self.seed(mk("a001", "done", notify=True, **when),  # its notice is still owed
                         mk("a002", "done", notify=True, notified=True, **when),
                         mk("a003", "cancelled", notify=True, **when),  # a cancelled one is never told
                         mk("a004", "done", batch="b0001", **when), mk("a005", "queued", batch="b0001"),
                         mk("a006", "done", batch="b0002", notify=True, notified=True, **when),
                         mk("a007", "failed", batch="b0002", notify=True, notified=True, **when),
                         mk("a008", "done", batch="b0003", notify=True, **when),  # the batch notice is owed
                         mk("a009", "failed", batch="b0003", notify=True, **when))
        self.assertEqual(sorted(dispatch.purge(conn, NOW, 0)), ["a002", "a003", "a006", "a007"])

    def test_transaction_purges_at_most_once_an_hour(self):
        self.seed(mk("a001", "done", created=ago(95), done_at=ago(91)))
        conn = store.connect(self.dir)
        self.addCleanup(conn.close)
        stamp = conn.execute("SELECT value FROM meta WHERE key='purged_at'").fetchone()[0]
        self.assertIsNotNone(stamp)
        self.assertEqual(conn.execute("SELECT purged FROM messages").fetchone()[0], 0)  # first run came before the row existed
        with dispatch.transaction(self.dir, ids=["a001"]) as cur:
            self.assertEqual(len(cur), 1)  # an hour has not passed
        conn.execute("UPDATE meta SET value=?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
        conn.commit()
        with dispatch.transaction(self.dir, ids=["a001"]) as cur:
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


class RingOutboxPurgeTest(unittest.TestCase):
    def test_purge_removes_the_outbox_of_old_rings(self):
        with tempfile.TemporaryDirectory() as t:
            sd = Path(t) / "state"
            with dispatch.transaction(sd) as cur:
                cur.conn.execute("INSERT INTO rings (id, at) VALUES ('0a0a', ?), ('0b0b', ?)",
                                 ((datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
                                  (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()))
                cur.conn.execute("DELETE FROM meta WHERE key='purged_at'")
            for code in ("0a0a", "0b0b"):
                dispatch.write_outbox(sd, f"ring-{code}", "ring")
            with dispatch.transaction(sd, keep_days=5):
                pass
            self.assertFalse(dispatch.outbox_path(sd, "ring-0a0a").exists())
            self.assertTrue(dispatch.outbox_path(sd, "ring-0b0b").exists())


class BoundedLoadTest(unittest.TestCase):
    """A transaction (and the watchers' reads) load only the live messages, plus the ones the caller names and their
    batch mates, never every finished one; a row that was not loaded is never written."""

    FINISHED_N = 60

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "state"
        old = [mk(f"f{n:03x}", "done", created=ago(2), done_at=ago(1), reply="r") for n in range(self.FINISHED_N)]
        live = [mk("aq01", "queued"), mk("as01", "sent"),
                mk("an01", "done", done_at=ago(0), notify=True),  # its notice is still owed
                mk("ac01", "cancelled", done_at=ago(0), notify=True),  # a cancelled one never owes a notice
                mk("ab01", "done", done_at=ago(0), batch="b0001", notify=True, notified=True),
                mk("ab02", "sent", batch="b0001"),  # live: its finished batch mate ab01 comes with it
                mk("ax01", "done", done_at=ago(0), batch="b0002")]  # a finished batch: only when named
        with dispatch.transaction(self.dir) as cur:
            cur.extend(old + live)
        self.live = {"aq01", "as01", "an01", "ab01", "ab02"}

    def counted(self):
        """Patch the row decoder to count how many message rows are read; the list it appends to."""
        seen = []
        real = dispatch._from_row

        def count(row):
            seen.append(row["id"])
            return real(row)
        patcher = mock.patch.object(dispatch, "_from_row", count)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def table(self):
        conn = store.connect(self.dir)
        try:
            return {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM messages")}
        finally:
            conn.close()

    def test_transaction_loads_only_live_rows_and_knows_every_id(self):
        seen = self.counted()
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual({d.id for d in cur}, self.live)
            self.assertEqual(len(cur.ids), self.FINISHED_N + 7)  # new ids still avoid unloaded ones
        self.assertEqual(sorted(seen), sorted(self.live))

    def test_named_ids_and_their_batch_mates_are_loaded(self):
        seen = self.counted()
        with dispatch.transaction(self.dir, ids=["f001", "b0002", "ac01"]) as cur:
            self.assertEqual({d.id for d in cur}, self.live | {"f001", "ax01", "ac01"})
        self.assertEqual(len(seen), len(self.live) + 3)

    def test_need_loads_more_once(self):
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual([d.id for d in cur.need(["f002", "aq01"])], ["aq01", "f002"])
            cur.need(["f002"])
            self.assertEqual(sum(d.id == "f002" for d in cur), 1)
            cur[-1].reason = "changed"
        self.assertEqual(next(d for d in dispatch.load(self.dir) if d.id == "f002").reason, "changed")

    def test_load_with_ids_is_bounded_and_without_is_everything(self):
        seen = self.counted()
        self.assertEqual({d.id for d in dispatch.load(self.dir, ids=())}, self.live)
        self.assertEqual(len(seen), len(self.live))
        self.assertEqual(len(dispatch.load(self.dir)), self.FINISHED_N + 7)

    def test_send_pending_reads_only_live_rows(self):
        from sampan import send
        seen = self.counted()
        cfg = mock.Mock(state_dir=self.dir)
        self.assertEqual(sorted(send.pending(cfg)), ["ab02", "an01", "aq01", "as01"])
        self.assertEqual(len(seen), len(self.live))

    def test_flush_never_touches_unloaded_rows(self):
        before = self.table()
        with dispatch.transaction(self.dir) as cur:
            for d in cur:
                d.reason = "touched"
        after = self.table()
        untouched = set(before) - self.live
        self.assertEqual({i: after[i] for i in untouched}, {i: before[i] for i in untouched})
        self.assertTrue(all(after[i] != before[i] for i in self.live))

    def test_a_new_row_with_an_unloaded_id_fails_instead_of_overwriting(self):
        before = self.table()
        with self.assertRaises(DispatchError):
            with dispatch.transaction(self.dir) as cur:
                cur.append(mk("f000", "queued", reason="overwrite"))
        self.assertEqual(self.table(), before)


class IdAndOutboxTest(unittest.TestCase):
    def test_new_id_shape_and_avoids_existing(self):
        i = dispatch.new_id(set())
        self.assertRegex(i, r"^[0-9a-f]{4}$")
        existing = {f"{n:04x}" for n in range(0x10000) if n != 0xbeef}
        self.assertEqual(dispatch.new_id(existing), "beef")

    def test_new_id_is_bounded_when_nearly_full_and_raises_when_full(self):
        existing = {f"{n:04x}" for n in range(0x10000) if n != 0x0042}
        calls = []

        def taken(_n):
            calls.append(1)
            return "beef"
        with mock.patch.object(dispatch.secrets, "token_hex", side_effect=taken):
            self.assertEqual(dispatch.new_id(existing), "0042")
            self.assertEqual(len(calls), store.ID_TRIES)
            with self.assertRaises(DispatchError) as ctx:
                dispatch.new_id(existing | {"0042"})
        self.assertIn("id 用完了", str(ctx.exception))

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
