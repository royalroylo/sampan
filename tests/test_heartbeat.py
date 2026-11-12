"""Heartbeats in dispatches.db: validity rules, no overwrite by an older instance, forget on shutdown."""
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sampan import dispatch, heartbeat, mcp_server, procs, store
from sampan.send import Caller
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env, status_row

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
START = "2026-11-12T08:00:00"
LATER = "2026-11-12T09:00:00"


class HeartbeatBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.conn = store.connect(self.state, create=True)
        self.addCleanup(self.conn.close)

    def beat(self, nonce="n1", **kw):
        args = dict(session_id="S1", agent="claude", conversation="c1", nonce=nonce, pid=4242, started=START,
                    version="0.17.0", now=NOW)
        args.update(kw)
        return heartbeat.beat(self.conn, **args)

    def valid(self, now=NOW, conversation="c1", start=START, alive_pids=(4242,)):
        def alive(pid, sig):
            if pid not in alive_pids:
                raise ProcessLookupError
        return heartbeat.valid(self.conn, session_id="S1", conversation=conversation, now=now, alive=alive,
                               start_of=lambda pid: start)


class ValidityTest(HeartbeatBase):
    def test_valid_within_120s_and_expired_after(self):
        self.beat()
        self.assertEqual(self.valid(NOW + timedelta(seconds=120))["nonce"], "n1")
        self.assertIsNone(self.valid(NOW + timedelta(seconds=121)))

    def test_dead_pid_or_wrong_start_time_is_invalid(self):
        self.beat()
        self.assertIsNone(self.valid(alive_pids=()))
        self.assertIsNone(self.valid(start="2026-11-12T10:00:00"))
        self.assertIsNone(self.valid(start=None))

    def test_other_conversation_is_invalid(self):
        self.beat()
        self.assertIsNone(self.valid(conversation="c2"))

    def test_version_0_16_is_invalid(self):
        self.beat(version="0.16.3")
        self.assertIsNone(self.valid())

    def test_dev_and_unparseable_versions(self):
        self.beat(version="0.17.0.dev3+gabc")
        self.assertIsNotNone(self.valid())
        self.beat(version="unknown")
        self.assertIsNone(self.valid())


class WriteTest(HeartbeatBase):
    def test_older_instance_cannot_overwrite_a_newer_one(self):
        self.assertTrue(self.beat("new", started=LATER))
        self.assertFalse(self.beat("old", started=START))
        rows = self.conn.execute("SELECT nonce FROM inboxes").fetchall()
        self.assertEqual([r["nonce"] for r in rows], ["new"])

    def test_newer_instance_replaces_the_older_row_and_a_repeat_updates_in_place(self):
        self.beat("old", started=START)
        self.assertTrue(self.beat("new", started=LATER))
        self.assertTrue(self.beat("new", started=LATER, now=NOW + timedelta(seconds=30)))
        rows = self.conn.execute("SELECT nonce, at FROM inboxes").fetchall()
        self.assertEqual([r["nonce"] for r in rows], ["new"])

    def test_forget_removes_only_that_instance(self):
        self.beat("n1")
        self.beat("n2", session_id="S2")
        heartbeat.forget(self.conn, "n1")
        rows = self.conn.execute("SELECT nonce FROM inboxes").fetchall()
        self.assertEqual([r["nonce"] for r in rows], ["n2"])


    def test_dead_or_expired_newer_row_does_not_block_a_live_older_instance(self):
        self.beat("crashed", started=LATER, pid=999)
        alive = lambda pid, sig: None if pid == 4242 else (_ for _ in ()).throw(ProcessLookupError())  # noqa: E731
        self.assertTrue(self.beat("old", started=START, alive=alive, start_of=lambda pid: START))
        self.assertEqual([r["nonce"] for r in self.conn.execute("SELECT nonce FROM inboxes")], ["old"])
        self.beat("expired", session_id="S3", started=LATER, now=NOW - timedelta(seconds=500))
        self.assertTrue(self.beat("mine", session_id="S3", started=START))
        self.assertEqual([r["nonce"] for r in self.conn.execute("SELECT nonce FROM inboxes WHERE session_id = 'S3'")],
                         ["mine"])

    def test_live_newer_row_still_blocks(self):
        self.beat("new", started=LATER)
        self.assertFalse(self.beat("old", started=START, alive=lambda pid, sig: None, start_of=lambda pid: LATER))


class StartTimeTest(unittest.TestCase):
    def test_parses_ps_lstart(self):
        run = FakeRunner(strict=True)
        run.on(["ps", "-o", "lstart=", "-p", "4242"], cp(0, "Thu Nov 12  8:00:01 2026\n"))
        self.assertEqual(procs.start_time(4242, run), "2026-11-12T08:00:01")

    def test_gone_process_is_none(self):
        run = FakeRunner(strict=True)
        run.on(["ps", "-o", "lstart=", "-p", "4242"], cp(1, ""))
        self.assertIsNone(procs.start_time(4242, run))


CALLER = Caller("alpha-pm", "alice", "claude", "%1", id="S1")


class ServerHeartbeatTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = make_env(Path(tmp.name))
        self.rows = [status_row("alpha-pm", sid="conv-1", pane="%1", id="S1")]

    def server(self, caller=CALLER, start_of=lambda pid: START, clock=lambda: 0.0):
        return mcp_server.Server(self.cfg, FakeRunner(strict=True), io.StringIO(), io.StringIO(), caller=caller,
                                 status_fn=lambda cfg, run, snapshot_fn=None: self.rows, now_fn=lambda: NOW,
                                 start_of=start_of, clock=clock)

    def inboxes(self):
        conn = store.connect(self.cfg.state_dir, create=True)
        try:
            return conn.execute("SELECT * FROM inboxes").fetchall()
        finally:
            conn.close()

    def test_watch_pass_beats_with_the_registry_conversation(self):
        s = self.server()
        s.watch_once()
        (row,) = self.inboxes()
        self.assertEqual((row["session_id"], row["agent"], row["conversation"], row["started"]),
                         ("S1", "claude", "conv-1", START))
        self.assertEqual(row["nonce"], s.nonce)

    def test_forget_on_shutdown(self):
        s = self.server()
        s.watch_once()
        self.assertEqual(len(self.inboxes()), 1)
        self.assertEqual(s.serve(None), 0)  # stdin is empty: serve beats once, then shuts down
        self.assertEqual(self.inboxes(), [])

    def test_no_fresh_db_while_reverted_or_not_migrated(self):
        sd = Path(self.cfg.state_dir)
        sd.mkdir(parents=True, exist_ok=True)
        for name, content in ((dispatch.REVERT_MARK, ""), (dispatch.MIGRATE_JOURNAL, "{}"),
                              ("dispatches.json", '{"version": 2, "dispatches": []}')):
            with self.subTest(name):
                (sd / name).write_text(content)
                with mock.patch.object(mcp_server, "_log") as log:
                    self.server().watch_once()
                self.assertIn(dispatch.DispatchError.__name__, log.call_args_list[0].args[0])
                self.assertFalse((sd / store.DB_FILE).exists())
                (sd / name).unlink()

    def test_a_pass_that_raises_does_not_end_the_watcher(self):
        t = [0.0]
        s = self.server(clock=lambda: t[0])
        passes, beats, logged = [], [], []
        real_load, real_beat = dispatch.load, heartbeat.beat

        def load(*a, **k):
            passes.append(1)
            if len(passes) == 1:
                raise RuntimeError("unexpected")
            s._stop.set()  # the second pass is the last
            return real_load(*a, **k)

        def beat(*a, **k):
            beats.append(1)
            t[0] += 60  # the next beat is due on the next pass
            return real_beat(*a, **k)
        for target, name, fake in ((dispatch, "load", load), (heartbeat, "beat", beat), (mcp_server, "_log", logged.append)):
            self.addCleanup(setattr, target, name, getattr(target, name))
            setattr(target, name, fake)
        s._watch(0.001)
        self.assertEqual((len(passes), len(beats)), (2, 2))
        self.assertIn("unexpected", logged[0])

    def test_unknown_caller_writes_nothing(self):
        s = self.server(caller=Caller(None, "", "", None, kind="unregistered"))
        s.watch_once()
        self.assertEqual(self.inboxes(), [])

    def test_no_conversation_yet_writes_nothing(self):
        self.rows = [status_row("alpha-pm", sid="", pane="%1", id="S1")]
        self.server().watch_once()
        self.assertEqual(self.inboxes(), [])

    def test_beats_are_throttled_to_one_per_30s(self):
        t = [0.0]
        s = self.server(clock=lambda: t[0])
        writes = []
        orig = heartbeat.beat
        self.addCleanup(setattr, heartbeat, "beat", orig)
        heartbeat.beat = lambda *a, **k: writes.append(1) or orig(*a, **k)
        for t[0] in (0.0, 3.0, 29.9, 30.0, 33.0, 60.0):
            s.watch_once()
        self.assertEqual(len(writes), 3)

    def test_sqlite_error_inside_the_transaction_keeps_the_server_running(self):
        s = self.server()
        orig = heartbeat.beat

        def boom(*a, **k):
            raise sqlite3.OperationalError("no such table: inboxes")
        heartbeat.beat = boom
        self.addCleanup(setattr, heartbeat, "beat", orig)
        self.assertEqual(s.serve(None), 0)  # the startup beat fails; serve still runs and shuts down
        s.watch_once()  # and so does a watcher pass
        orig_forget = heartbeat.forget
        heartbeat.forget = boom
        self.addCleanup(setattr, heartbeat, "forget", orig_forget)
        s.forget()

    def test_a_failing_status_read_does_not_break_the_server(self):
        s = self.server()
        s.status_fn = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))
        s.watch_once()

    def test_a_failing_write_does_not_break_the_server(self):
        s = self.server()
        orig = dispatch.transaction

        def boom(*a, **k):
            raise dispatch.DispatchError("busy")
        dispatch.transaction = boom
        self.addCleanup(setattr, dispatch, "transaction", orig)
        s.watch_once()  # must not raise


if __name__ == "__main__":
    unittest.main()
