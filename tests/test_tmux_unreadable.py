"""When tmux is unreadable (list-panes fails but not with "no server"), all sessions must not be treated as stopped."""
import tempfile
import unittest
from pathlib import Path

from sampan.doctor import run_checks
from sampan.registry import Row, load_rows, write_rows
from sampan.restore import cmd_restore
from sampan.save import cmd_save
from sampan.session_run import cmd_move, cmd_stop
from sampan.status import State, cmd_status, status_rows
from sampan.tmux import Tmux
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env, status_row

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
PERM = "error connecting to /private/tmp/tmux-501/default (Permission denied)"
NO_SERVER = ["no server running on /private/tmp/tmux-501/default",
             "error connecting to /private/tmp/tmux-501/default (No such file or directory)",
             "error connecting to /private/tmp/tmux-501/default (Connection refused)"]
DESTRUCTIVE = ("send-keys", "kill-session", "new-session", "rename-session")


def runner(err=PERM, rc=1):
    r = FakeRunner()
    r.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="srv\n") if "-F" in a else cp())
    r.on(["tmux", "-u", "list-panes"], cp(rc=rc, err=err))
    return r


def tmux_actions(r):
    return [c["args"] for c in r.calls if c["args"][:2] == ["tmux", "-u"] and c["args"][2] in DESTRUCTIVE]


class PanesTest(unittest.TestCase):
    def test_no_server_is_empty_list(self):
        for msg in NO_SERVER:
            with self.subTest(msg=msg):
                self.assertEqual(Tmux(runner(msg)).panes(), [])

    def test_other_failures_are_none_with_reason(self):
        for rc, err in ((1, PERM), (1, "protocol version mismatch\nsecond line"), (127, ""), (124, "")):
            with self.subTest(err=err):
                t = Tmux(runner(err, rc))
                self.assertIsNone(t.panes())
                self.assertIsNotNone(t.unreadable_reason)

    def test_reason_is_first_stderr_line(self):
        t = Tmux(runner("protocol version mismatch\nsecond line"))
        t.panes()
        self.assertEqual(t.unreadable_reason, "protocol version mismatch")


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(self.cfg.registry_path, [Row("one", "claude", "alice", A, "/c"),
                                            Row("old", "claude", "alice", B, "/c", "retired")],
                   "t", self.cfg.history_dir)
        self.msgs = []

    def registry(self):
        return self.cfg.registry_path.read_bytes()


class StatusTest(Base):
    def test_rows_are_not_stopped_and_warning_is_attached(self):
        rows = status_rows(self.cfg, runner())
        states = {r.name: r.state for r in rows}
        self.assertNotEqual(states["one"], State.STOPPED)
        self.assertEqual(states["one"], State.UNKNOWN)
        self.assertEqual(states["old"], State.RETIRED)
        self.assertIn("讀不到 tmux", rows.warning)
        self.assertIn("Permission denied", rows.warning)

    def test_no_server_still_reports_stopped(self):
        rows = status_rows(self.cfg, runner(NO_SERVER[0]))
        self.assertEqual({r.name: r.state for r in rows}["one"], State.STOPPED)
        self.assertIsNone(rows.warning)

    def test_cmd_status_prints_warning_and_returns_1(self):
        rc = cmd_status(self.cfg, runner(), self.msgs.append)
        self.assertEqual(rc, 1)
        self.assertIn("狀態無法判斷", self.msgs[0])
        self.assertFalse(any("該跑沒跑" in m for m in self.msgs))


class UiTest(Base):
    def test_enter_on_unknown_row_does_not_resume(self):
        from sampan.ui import cmd_ui
        calls = []
        r = runner()
        rc = cmd_ui(self.cfg, r, self.msgs.append, lambda _p="": "",
                    picker_fn=lambda rows, cur: ("attach", ["one"]),
                    restore_fn=lambda *a, **k: calls.append("restore") or 0,
                    exec_fn=lambda *a: calls.append("exec"))
        self.assertEqual(calls, [])
        self.assertEqual(rc, 1)
        self.assertTrue(any("讀不到 tmux" in m for m in self.msgs))

    def test_card_has_no_resume_key(self):
        from sampan.ui import card_lines
        row = status_row("one", State.UNKNOWN, sid=A, agent_status="-")
        text = repr(card_lines(self.cfg, row, {"panes": 0}, None))
        self.assertNotIn("接回", text)
        self.assertNotIn("已停止", text)


class RestoreTest(Base):
    def test_stops_and_creates_nothing(self):
        r = runner()
        rc = cmd_restore(self.cfg, r, self.msgs.append, lambda _p="": "y", select_all=True)
        self.assertEqual(rc, 1)
        self.assertIn("讀不到 tmux", "\n".join(self.msgs))
        self.assertEqual(tmux_actions(r), [])

    def test_unreadable_at_the_pre_launch_recheck_skips_that_session(self):
        n = {"panes": 0}
        cwd = self.cfg.home / "proj"
        cwd.mkdir()
        d = self.cfg.profiles["alice"].projects_dir / "-proj"
        d.mkdir(parents=True)
        (d / f"{A}.jsonl").write_text("")
        write_rows(self.cfg.registry_path, [Row("one", "claude", "alice", A, str(cwd))], "t", self.cfg.history_dir)

        def panes(_a):
            n["panes"] += 1
            return cp(out="") if n["panes"] == 1 else cp(rc=1, err=PERM)  # readable for the candidate list only
        r = runner()
        r.on(["tmux", "-u", "list-panes"], panes)
        rc = cmd_restore(self.cfg, r, self.msgs.append, lambda _p="": "y", select_all=True, sleep=lambda _: None)
        self.assertEqual(rc, 1)
        text = "\n".join(self.msgs)
        self.assertIn("讀不到 tmux", text)
        self.assertIn("one", text)
        self.assertEqual(tmux_actions(r), [])

    def test_no_server_still_proceeds_to_candidates(self):
        r = runner(NO_SERVER[0])
        cmd_restore(self.cfg, r, self.msgs.append, lambda _p="": "q", dry_run=True)
        self.assertNotIn("讀不到 tmux", "\n".join(self.msgs))
        self.assertTrue(any("one" in m for m in self.msgs))


class StopMoveTest(Base):
    def test_stop_does_nothing(self):
        r = runner()
        before = self.registry()
        rc = cmd_stop(self.cfg, r, self.msgs.append, sleep=lambda _: None, proc_root=Path("/nonexistent"))
        self.assertEqual(rc, 2)
        self.assertIn("讀不到 tmux", "\n".join(self.msgs))
        self.assertEqual(tmux_actions(r), [])
        self.assertEqual(self.registry(), before)

    def test_move_does_nothing(self):
        d = Path(tempfile.mkdtemp())
        r = runner()
        rc = cmd_move(self.cfg, r, self.msgs.append, "one", str(d), sleep=lambda _: None,
                      proc_root=Path("/nonexistent"))
        self.assertEqual(rc, 2)
        self.assertIn("讀不到 tmux", "\n".join(self.msgs))
        self.assertEqual(tmux_actions(r), [])


class SaveDoctorTest(Base):
    def test_save_does_not_write(self):
        before = self.registry()
        rc = cmd_save(self.cfg, runner(), self.msgs.append)
        self.assertEqual(rc, 1)
        self.assertEqual(self.registry(), before)
        self.assertIn("Permission denied", "\n".join(self.msgs))
        self.assertEqual(load_rows(self.cfg.registry_path)[0].state, "active")

    def test_doctor_reports_check(self):
        from datetime import datetime, timezone
        checks = run_checks(self.cfg, runner(), now=datetime.now(timezone.utc), uid=0,
                            sampan_path="/x/sampan", which=lambda n: f"/bin/{n}", env={"PATH": "/usr/bin"})
        hits = [c for c in checks if "讀不到 tmux" in c.title]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "warn")
        self.assertIn("Permission denied", hits[0].title)
