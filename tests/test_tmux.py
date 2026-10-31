import unittest

from sampan.tmux import Tmux
from tests.fakes import FakeRunner, cp


class SessionIdTest(unittest.TestCase):
    def test_set_and_read_session_id(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "set-option"], cp())
        r.on(["tmux", "-u", "list-sessions"], cp(out="終端 機\tx-id\nplain\t\n"))
        t = Tmux(r)
        self.assertTrue(t.set_session_id("終端 機", "x-id"))
        self.assertEqual(r.calls[0]["args"], ["tmux", "-u", "set-option", "-t", "=終端 機", "@sampan_id", "x-id"])
        self.assertEqual(t.session_ids(), {"終端 機": "x-id", "plain": ""})
        self.assertEqual(r.calls[1]["args"][-1], "#{session_name}\t#{@sampan_id}")

    def test_failures_are_quiet(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "set-option"], cp(rc=1, err="no such session"))
        self.assertFalse(Tmux(r).set_session_id("x", "i"))
        self.assertIsNone(Tmux(FakeRunner().on(["tmux", "-u", "list-sessions"], cp(rc=1, err="permission denied"))).session_ids())
        self.assertEqual(Tmux(FakeRunner().on(["tmux", "-u", "list-sessions"], cp(rc=1, err="no server running"))).session_ids(), {})


if __name__ == "__main__":
    unittest.main()
