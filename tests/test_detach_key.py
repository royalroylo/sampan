import dataclasses
import tempfile
import unittest
from pathlib import Path

from sampan import detach_key as dk
from sampan.tmux import TMUX
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env

LIST = [*TMUX, "list-keys", "-T", "root", "C-q"]
OURS = "bind-key -T root C-q if-shell -F \"#{@sampan}\" { ... }\n"


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def runner(self, existing=""):
        r = FakeRunner(strict=True)
        r.on(LIST, cp(rc=0 if existing else 1, out=existing))
        r.on([*TMUX, "bind-key"], cp())
        r.on([*TMUX, "unbind-key"], cp())
        r.on([*TMUX, "set-option"], cp())
        return r

    def test_default_key_is_bound_and_sessions_marked(self):
        self.assertEqual(self.cfg.detach_key, "C-q")
        r = self.runner()
        self.assertIsNone(dk.ensure(self.cfg, r, ["a", "b"]))
        bind = r.called([*TMUX, "bind-key"])[0]["args"]
        self.assertEqual(bind, [*TMUX, *dk.binding("C-q")])
        self.assertEqual(bind[2:6], ["bind-key", "-n", "C-q", "if-shell"])
        self.assertIn("#{@sampan}", bind)
        self.assertEqual(bind[-1], "send-keys C-q")  # not a sampan session: the key goes to the program
        self.assertIn("switch-client -l", bind[-2])
        self.assertIn("detach-client", bind[-2])
        marks = [c["args"] for c in r.called([*TMUX, "set-option"])]
        self.assertEqual(marks, [[*TMUX, "set-option", "-t", "=a", "@sampan", "1"],
                                 [*TMUX, "set-option", "-t", "=b", "@sampan", "1"]])

    def test_rebinding_our_own_binding_is_fine(self):
        r = self.runner(OURS)
        self.assertIsNone(dk.ensure(self.cfg, r, []))
        self.assertTrue(r.called([*TMUX, "bind-key"]))

    def test_a_users_own_binding_is_never_overwritten(self):
        r = self.runner("bind-key -T root C-q send-keys -X cancel\n")
        warning = dk.ensure(self.cfg, r, ["a"])
        self.assertIn("C-q", warning)
        self.assertFalse(r.called([*TMUX, "bind-key"]))
        self.assertFalse(r.called([*TMUX, "set-option"]))

    def test_disabled_removes_only_our_binding(self):
        cfg = dataclasses.replace(self.cfg, detach_key="")
        r = self.runner(OURS)
        self.assertIsNone(dk.ensure(cfg, r, ["a"]))
        self.assertEqual(r.called([*TMUX, "unbind-key"])[0]["args"], [*TMUX, "unbind-key", "-n", "C-q"])
        self.assertFalse(r.called([*TMUX, "bind-key"]))
        r = self.runner("bind-key -T root C-q send-keys -X cancel\n")
        dk.ensure(cfg, r, [])
        self.assertFalse(r.called([*TMUX, "unbind-key"]))

    def test_another_key_frees_our_old_default(self):
        cfg = dataclasses.replace(self.cfg, detach_key="C-x")
        r = self.runner(OURS)
        r.on([*TMUX, "list-keys", "-T", "root", "C-x"], cp(rc=1))
        self.assertIsNone(dk.ensure(cfg, r, []))
        self.assertEqual(r.called([*TMUX, "unbind-key"])[0]["args"], [*TMUX, "unbind-key", "-n", "C-q"])
        self.assertEqual(r.called([*TMUX, "bind-key"])[0]["args"][4], "C-x")

    def test_tmux_failure_is_a_warning_not_an_error(self):
        r = self.runner()
        r.on([*TMUX, "bind-key"], cp(rc=1, err="unknown key: C-q"))
        self.assertIn("unknown key", dk.ensure(self.cfg, r, []))

    def test_conflict_reads_only(self):
        r = self.runner("bind-key -T root C-q send-keys -X cancel\n")
        self.assertIn("C-q", dk.conflict(self.cfg, r))
        self.assertEqual([c["args"][2] for c in r.calls], ["list-keys"])
        self.assertIsNone(dk.conflict(self.cfg, self.runner(OURS)))
        self.assertIsNone(dk.conflict(dataclasses.replace(self.cfg, detach_key=""), self.runner("x\n")))


if __name__ == "__main__":
    unittest.main()
