import os
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan import store
from sampan.store import StoreError


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "state"
        self.dir.mkdir()

    def make(self):
        conn = store.connect(self.dir, create=True)
        self.addCleanup(conn.close)
        return conn

    def delivery(self, conn, did, to_id, state):
        conn.execute("INSERT INTO deliveries(id, kind, to_id, state) VALUES (?, 'ring', ?, ?)", (did, to_id, state))

    def test_connect_without_create_refuses_a_missing_db(self):
        with self.assertRaises(StoreError) as ctx:
            store.connect(self.dir)
        self.assertIn("sampan migrate", str(ctx.exception))
        self.assertFalse((self.dir / store.DB_FILE).exists())

    def test_create_makes_schema_and_private_files(self):
        conn = self.make()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")
        conn.commit()
        for name in (store.DB_FILE, store.DB_FILE + "-wal"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600, name)
        again = store.connect(self.dir)  # an existing db opens without create
        self.addCleanup(again.close)
        self.assertEqual(again.execute("SELECT value FROM meta").fetchone()["value"], "v")

    def test_one_active_delivery_per_target(self):
        conn = self.make()
        self.delivery(conn, "d000001", "s1", "active")
        with self.assertRaises(sqlite3.IntegrityError):
            self.delivery(conn, "d000002", "s1", "active")
        self.delivery(conn, "d000003", "s2", "active")
        self.delivery(conn, "d000004", "s1", "ended")
        self.delivery(conn, "d000005", "s1", "ended")

    def test_ids_avoid_every_existing_id(self):
        conn = self.make()
        conn.execute("INSERT INTO messages(id, purged) VALUES ('aaaa', 1)")
        with mock.patch.object(secrets, "token_hex", side_effect=["aaaa", "bbbb"]):
            self.assertEqual(store.new_message_id(conn), "bbbb")
        conn.execute("INSERT INTO rings(id) VALUES ('cccc')")
        with mock.patch.object(secrets, "token_hex", side_effect=["cccc", "dddd"]):
            self.assertEqual(store.new_ring_id(conn), "dddd")
        self.delivery(conn, "d0000aa", "s1", "ended")
        with mock.patch.object(secrets, "token_hex", side_effect=["0000aa", "0000bb"]):
            self.assertEqual(store.new_delivery_id(conn), "d0000bb")
        self.assertRegex(store.new_part_id(conn), r"^p[0-9a-f]{8}$")
        self.assertRegex(store.new_message_id(conn), r"^[0-9a-f]{4}$")

    def test_a_state_dir_with_uri_characters_opens_the_right_file(self):
        odd = Path(self._tmp.name) / "a#b%c?d"
        odd.mkdir()
        conn = store.connect(odd, create=True)
        conn.execute("INSERT INTO meta(key, value) VALUES ('k', 'odd')")
        conn.commit()
        conn.close()
        self.assertTrue((odd / store.DB_FILE).exists())
        self.assertEqual(sorted(p.name for p in Path(self._tmp.name).iterdir()), ["a#b%c?d", "state"])  # nothing beside it
        again = store.connect(odd)
        self.addCleanup(again.close)
        self.assertEqual(again.execute("SELECT value FROM meta").fetchone()["value"], "odd")

    def test_fresh_id_gives_up_after_bounded_tries(self):
        conn = self.make()
        conn.execute("INSERT INTO rings(id) VALUES ('cccc')")
        calls = []

        def taken(_n):
            calls.append(1)
            return "cccc"
        with mock.patch.object(secrets, "token_hex", side_effect=taken):
            with self.assertRaises(StoreError) as ctx:
                store.new_ring_id(conn)
        self.assertEqual(len(calls), store.ID_TRIES)
        self.assertIn("id 用完了", str(ctx.exception))

    def test_mnt_problem(self):
        self.assertTrue(store.mnt_problem("/mnt/c/x"))
        self.assertIsNone(store.mnt_problem(self.dir))
        link = self.dir / "link"
        os.symlink("/mnt", link)
        with mock.patch("os.path.realpath", return_value="/mnt/c/state"):
            self.assertTrue(store.mnt_problem(link))


if __name__ == "__main__":
    unittest.main()
