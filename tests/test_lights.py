import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sampan import lights
from sampan.dispatch import Dispatch
from sampan.status import State
from sampan.tmux import Tmux
from tests.fakes import FakeRunner, cp
from tests.helpers import status_row

T = [datetime(2026, 10, 31, 9, m, tzinfo=timezone.utc) for m in range(0, 60, 10)]  # 09:00 .. 09:50


def light(row, **kw):
    args = dict(attached=0, last_attached=None, seen=None, replied=None)
    args.update(kw)
    return lights.light(row, **args)


class LightTest(unittest.TestCase):
    def test_busy_is_running(self):
        self.assertEqual(light(status_row("a", agent_status="busy")), lights.RUNNING)

    def test_codex_working_is_running(self):
        self.assertEqual(light(status_row("a", agent="codex", agent_status="working")), lights.RUNNING)

    def test_claude_shell_is_running(self):  # the turn is over but a background shell task runs
        self.assertEqual(light(status_row("a", agent_status="shell", last=T[3])), lights.RUNNING)

    def test_claude_waiting_is_waiting(self):
        self.assertEqual(light(status_row("a", agent_status="waiting")), lights.WAITING)

    def test_idle_without_new_output_is_idle(self):
        self.assertEqual(light(status_row("a", last=T[1]), last_attached=T[2]), lights.IDLE)
        self.assertEqual(light(status_row("a", last=None), seen=T[0]), lights.IDLE)

    def test_idle_with_output_after_every_look_is_unseen(self):
        row = status_row("a", last=T[3])
        self.assertEqual(light(row), lights.UNSEEN)
        self.assertEqual(light(row, last_attached=T[0], seen=T[1], replied=T[2]), lights.UNSEEN)

    def test_look_after_output_is_idle(self):
        row = status_row("a", last=T[2])
        self.assertEqual(light(row, last_attached=T[3], seen=T[0], replied=T[0]), lights.IDLE)
        self.assertEqual(light(row, last_attached=T[0], seen=T[3], replied=T[0]), lights.IDLE)
        self.assertEqual(light(row, last_attached=T[0], seen=T[0], replied=T[3]), lights.IDLE)

    def test_attached_now_is_never_unseen(self):
        self.assertEqual(light(status_row("a", last=T[3]), attached=1), lights.IDLE)

    def test_not_running_has_no_light(self):
        for st in (State.STOPPED, State.RETIRED):
            self.assertIsNone(light(status_row("a", st, agent_status="busy")))

    def test_unrecognised_status_has_no_light(self):
        for s in ("unknown", "-"):
            self.assertIsNone(light(status_row("a", agent_status=s, last=T[3])))

    def test_is_waiting(self):
        self.assertTrue(lights.is_waiting(lights.WAITING))
        self.assertTrue(lights.is_waiting(lights.UNSEEN))
        for x in (lights.RUNNING, lights.IDLE, None):
            self.assertFalse(lights.is_waiting(x))


class SeenTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_seen_file_missing_or_broken_is_empty(self):
        self.assertEqual(lights.load_seen(self.d), {})
        for text in ("{not json", "[1]", '{"a": 5, "b": "nope"}'):
            (self.d / "seen.json").write_text(text)
            self.assertEqual(lights.load_seen(self.d), {})

    def test_mark_seen_round_trip(self):
        lights.mark_seen(self.d, "a", T[0])
        lights.mark_seen(self.d, "b", T[1])
        lights.mark_seen(self.d, "a", T[2])
        self.assertEqual(lights.load_seen(self.d), {"a": T[2], "b": T[1]})

    def test_mark_seen_failure_is_silent(self):
        (self.d / "seen.json").mkdir()  # cannot be replaced by a file
        lights.mark_seen(self.d, "a", T[0])
        lights.mark_seen(self.d / "missing" / "x" / "\0", "a", T[0])


def disp(to, state, done_at):
    return Dispatch("d", "f", "p", "claude", to, "claude", "p", "t", state, "2026-10-31T00:00:00+00:00",
                    done_at=done_at)


class RepliedTest(unittest.TestCase):
    def test_latest_done_per_target(self):
        items = [disp("a", "done", T[0].isoformat()), disp("a", "done", T[2].isoformat()),
                 disp("a", "failed", T[4].isoformat()), disp("b", "sent", None), disp("c", "done", None)]
        self.assertEqual(lights.replied_at(items), {"a": T[2]})


class AttachTimesTest(unittest.TestCase):
    def test_parses_and_handles_empty(self):
        out = "one\t1\t1761901200\ntwo\t0\t\n中文\t2\t1761901260\nbad line\n"
        r = FakeRunner().on(["tmux", "-u", "list-sessions"], cp(out=out))
        got = Tmux(r).attach_times()
        self.assertEqual(got["one"], (1, datetime.fromtimestamp(1761901200, timezone.utc)))
        self.assertEqual(got["two"], (0, None))
        self.assertEqual(got["中文"][0], 2)
        self.assertNotIn("bad line", got)

    def test_unreadable_is_none_and_no_server_is_empty(self):
        r = FakeRunner().on(["tmux", "-u", "list-sessions"], cp(rc=1, err="error: permission denied"))
        self.assertIsNone(Tmux(r).attach_times())
        r = FakeRunner().on(["tmux", "-u", "list-sessions"], cp(rc=1, err="no server running on /tmp/x"))
        self.assertEqual(Tmux(r).attach_times(), {})


if __name__ == "__main__":
    unittest.main()
