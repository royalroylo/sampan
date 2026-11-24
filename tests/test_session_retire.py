import tempfile
import unittest
from pathlib import Path

from sampan.registry import Row, load_rows, write_rows
from sampan.session_run import cmd_activate, cmd_retire
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
D = "dddddddd-0000-4000-8000-000000000004"
NO_PROC = Path("/nonexistent-proc-root")
PANE_OF = {200: 100, 201: 101, 202: 102}


class Base(unittest.TestCase):
    """retire = stop first, mark only when the stop worked (a: idle, b: busy, me: our own pane, gone: no tmux, sh: only a shell)."""

    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(self.cfg.registry_path, [
            Row("a", "claude", "alice", A, "/c"), Row("b", "claude", "alice", B, "/c"),
            Row("me", "claude", "alice", C, "/c"), Row("gone", "claude", "alice", D, "/c"),
            Row("sh", "claude", "alice", "eeeeeeee-0000-4000-8000-000000000005", "/c")], "t", self.cfg.history_dir)
        self.alive = {200, 201, 202}
        self.exit_on_keys = True
        self.unreadable = False
        self.sigterm_works = True
        self.unreadable_after_sigterm = False
        self.extra_ps = []
        self.sigtermed = []
        self.msgs = []
        self.panes = [pane_line("a", "%1", 100, "claude", "/c"), pane_line("b", "%2", 101, "claude", "/c"),
                      pane_line("me", "%3", 102, "claude", "/c"), pane_line("sh", "%4", 103, "zsh", "/c")]
        write_session(self.cfg, "alice", 200, sid=A, tmux="a:@1.%1", status="idle")
        write_session(self.cfg, "alice", 201, sid=B, tmux="b:@2.%2", status="busy")
        write_session(self.cfg, "alice", 202, sid=C, tmux="me:@3.%3", status="idle")

    def ps(self, _args):
        lines = [ps_line(pp, 1, "-zsh", "-zsh") for pp in (100, 101, 102, 103)]
        lines += [ps_line(pid, PANE_OF[pid], "claude", "claude") for pid in sorted(self.alive)]
        lines += self.extra_ps
        return cp(out="\n".join(lines) + "\n")

    def runner(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="srv\n") if "-F" in a else cp())
        r.on(["tmux", "-u", "set-option"], cp())
        r.on(["tmux", "-u", "list-panes"],
             lambda a: cp(rc=1, err="error connecting to /private/tmp/tmux-501/default (Permission denied)")
             if self.unreadable else cp(out="\n".join(self.panes) + "\n"))
        r.on(["ps", "-A"], self.ps)
        r.on(["ps", "eww"], cp())
        r.on(["tmux", "-u", "kill-session"], cp())
        r.on(["tmux", "-u", "capture-pane"], cp(out="➜  ~\n"))

        def send_keys(args):
            if self.exit_on_keys and args[-2:] == ["/exit", "Enter"] and args[4] == "%1":
                self.alive.discard(200)
            return cp()
        r.on(["tmux", "-u", "send-keys"], send_keys)
        return r

    def retire(self, r, name, **kw):
        kw.setdefault("own_pane", "%3")
        return cmd_retire(self.cfg, r, self.msgs.append, name, sleep=lambda _: None, proc_root=NO_PROC,
                          kill=self.kill, **kw)

    def kill(self, pid, sig):
        self.sigtermed.append((pid, sig))
        if self.unreadable_after_sigterm:
            self.unreadable = True
        if self.sigterm_works:
            self.alive.discard(pid)

    def state(self, name):
        return next(x.state for x in load_rows(self.cfg.registry_path) if x.tmux == name)

    def sent(self, r):
        return [c["args"][4:] for c in r.called(["tmux", "-u", "send-keys"])]

    def killed(self, r):
        return [c["args"] for c in r.called(["tmux", "-u", "kill-session"])]

    def text(self):
        return "\n".join(self.msgs)


class RetireTest(Base):
    def test_stopped_session_is_only_marked(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "gone"), 0)
        self.assertEqual(self.state("gone"), "retired")
        self.assertEqual((self.sent(r), self.killed(r)), ([], []))

    def test_idle_running_session_is_stopped_then_marked(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "a"), 0)
        self.assertEqual(self.sent(r), [["%1", "C-u"], ["%1", "/exit", "Enter"]])
        self.assertEqual(self.killed(r), [["tmux", "-u", "kill-session", "-t", "=a"]])
        self.assertEqual(self.state("a"), "retired")
        self.assertEqual(self.state("b"), "active")

    def test_only_the_named_session_is_touched(self):
        r = self.runner()
        self.retire(r, "a")
        self.assertTrue(all(k[0] == "%1" for k in self.sent(r)))

    def test_busy_session_is_refused_and_not_marked(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "b"), 1)
        self.assertEqual(self.state("b"), "active")
        self.assertEqual(self.sent(r), [])
        self.assertIn("b（busy）", self.text())
        self.assertIn("--no-stop", self.text())

    def test_own_pane_is_refused(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "me"), 1)
        self.assertEqual(self.state("me"), "active")
        self.assertEqual(self.sent(r), [])
        self.assertIn("--no-stop", self.text())

    def test_a_stop_that_times_out_does_not_mark(self):
        self.exit_on_keys = False
        r = self.runner()
        self.assertEqual(self.retire(r, "a", timeout=4), 1)
        self.assertEqual(self.state("a"), "active")
        self.assertEqual(self.killed(r), [])

    def test_force_sends_sigterm_when_the_stop_times_out_and_marks_once_the_agent_is_gone(self):
        self.exit_on_keys = False
        r = self.runner()
        self.assertEqual(self.retire(r, "a", timeout=4, force=True), 0)
        self.assertEqual(self.sigtermed, [(200, 15)])
        self.assertEqual(self.state("a"), "retired")
        self.assertEqual(self.killed(r), [["tmux", "-u", "kill-session", "-t", "=a"]])
        self.assertIn("SIGTERM", self.text())

    def test_force_that_does_not_end_the_agent_does_not_mark(self):
        self.exit_on_keys = False
        self.sigterm_works = False
        r = self.runner()
        self.assertEqual(self.retire(r, "a", timeout=4, force=True), 1)
        self.assertEqual(self.sigtermed, [(200, 15)])
        self.assertEqual(self.state("a"), "active")
        self.assertEqual(self.killed(r), [])

    def test_tmux_unreadable_after_the_sigterm_is_not_a_traceback_and_does_not_mark(self):
        self.exit_on_keys = False
        self.unreadable_after_sigterm = True
        r = self.runner()
        self.assertEqual(self.retire(r, "a", timeout=4, force=True), 1)
        self.assertEqual(self.sigtermed, [(200, 15)])
        self.assertEqual(self.state("a"), "active")
        self.assertIn("SIGTERM 已送給 a", self.text())
        self.assertIn("sampan status", self.text())

    def test_a_codex_that_is_still_closing_counts_as_an_agent(self):
        from sampan.session_run import _agent_in
        r = self.runner()
        self.panes.append(pane_line("cx", "%5", 105, "zsh", "/c"))
        self.extra_ps = [ps_line(105, 1, "-zsh", "-zsh"), ps_line(205, 105, "codex", "codex")]
        from unittest import mock
        with mock.patch("sampan.session_run.live_by_pane", return_value=({}, [])):  # codex not identified (closing down)
            self.assertTrue(_agent_in(self.cfg, r, "cx", NO_PROC))
            self.assertFalse(_agent_in(self.cfg, r, "sh", NO_PROC))

    def test_a_claude_without_a_session_file_is_refused_with_a_way_through_the_trust_prompt(self):
        import os
        for f in (self.cfg.profiles["alice"].config_dir / "sessions").glob("200.*"):
            os.unlink(f)  # a claude with no session file (at the trust prompt)
        r = self.runner()
        self.assertEqual(self.retire(r, "a"), 1)
        self.assertEqual((self.state("a"), self.sent(r)), ("active", []))
        self.assertIn("信任提示", self.text())
        self.assertIn("tmux attach -t a", self.text())
        self.assertNotIn("等它閒了", self.text())

    def test_force_does_not_override_a_busy_session(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "b", force=True), 1)
        self.assertEqual((self.sigtermed, self.state("b")), ([], "active"))

    def test_without_force_a_timeout_sends_no_sigterm(self):
        self.exit_on_keys = False
        r = self.runner()
        self.assertEqual(self.retire(r, "a", timeout=4), 1)
        self.assertEqual(self.sigtermed, [])

    def test_dry_run_with_only_a_shell_says_it_would_only_mark(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "sh", dry_run=True), 0)
        self.assertEqual(self.state("sh"), "active")
        self.assertIn("標成 retired", self.text())
        self.assertNotIn("停掉", self.text())

    def test_dry_run_with_an_idle_agent_says_it_would_stop(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "a", dry_run=True), 0)
        self.assertIn("會停掉 a", self.text())

    def test_already_retired_with_only_a_shell_returns_at_once_without_a_save_or_a_log_line(self):
        r = self.runner()
        self.retire(r, "sh")
        self.msgs.clear()
        seen = self.cfg.registry_path.read_bytes()
        calls = len(r.calls)
        log = (self.cfg.state_dir / "actions.log").read_text() if (self.cfg.state_dir / "actions.log").exists() else ""
        self.assertEqual(self.retire(r, "sh"), 0)
        self.assertIn("已經是 retired", self.text())
        self.assertEqual(self.cfg.registry_path.read_bytes(), seen)  # no save rewrote it
        self.assertNotIn("stop", [c["args"][2] for c in r.calls[calls:] if len(c["args"]) > 2 and c["args"][2] == "send-keys"])
        new_log = (self.cfg.state_dir / "actions.log").read_text()
        self.assertNotIn("[stop", new_log[len(log):])

    def test_no_stop_with_unreadable_tmux_warns_that_it_may_still_run(self):
        self.unreadable = True
        r = self.runner()
        self.assertEqual(self.retire(r, "a", no_stop=True), 0)
        self.assertIn("tmux 讀不到，不知道 a 有沒有在跑", self.text())
        self.assertIn("sampan stop a", self.text())

    def test_no_stop_marks_a_running_session_and_warns(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "a", no_stop=True), 0)
        self.assertEqual(self.state("a"), "retired")
        self.assertEqual((self.sent(r), self.killed(r)), ([], []))
        self.assertIn("a 仍在執行：retired 但還在跑；sampan stop a 下線，或 sampan activate a 還原", self.text())

    def test_no_stop_also_marks_a_busy_session(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "b", no_stop=True), 0)
        self.assertEqual(self.state("b"), "retired")

    def test_unreadable_tmux_refuses_and_changes_nothing(self):
        self.unreadable = True
        r = self.runner()
        self.assertEqual(self.retire(r, "a"), 2)
        self.assertEqual(self.state("a"), "active")
        self.assertIn("不知道 a 有沒有在跑，什麼都沒改", self.text())

    def test_unreadable_tmux_with_no_stop_marks(self):
        self.unreadable = True
        r = self.runner()
        self.assertEqual(self.retire(r, "a", no_stop=True), 0)
        self.assertEqual(self.state("a"), "retired")

    def test_a_tmux_session_with_only_a_shell_is_marked_and_not_closed(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "sh"), 0)
        self.assertEqual(self.state("sh"), "retired")
        self.assertEqual((self.sent(r), self.killed(r)), ([], []))

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "a", dry_run=True), 0)
        self.assertEqual(self.state("a"), "active")
        self.assertEqual((self.sent(r), self.killed(r)), ([], []))
        self.assertIn("dry-run", self.text())
        self.assertIn("retired", self.text())

    def test_dry_run_of_a_busy_session_says_it_would_refuse(self):
        r = self.runner()
        self.assertEqual(self.retire(r, "b", dry_run=True), 1)
        self.assertEqual(self.state("b"), "active")

    def test_unknown_name(self):
        self.assertEqual(self.retire(self.runner(), "nope"), 2)
        self.assertIn("登記表沒有 nope", self.text())

    def test_already_retired_and_stopped_is_a_no_op(self):
        r = self.runner()
        self.retire(r, "gone")
        self.msgs.clear()
        self.assertEqual(self.retire(r, "gone"), 0)
        self.assertIn("已經是 retired", self.text())

    def test_retired_but_running_is_finished_by_running_retire_again(self):
        r = self.runner()
        self.retire(r, "a", no_stop=True)  # retired, still running
        self.msgs.clear()
        self.assertEqual(self.retire(r, "a"), 0)
        self.assertEqual(self.sent(r), [["%1", "C-u"], ["%1", "/exit", "Enter"]])
        self.assertEqual(self.state("a"), "retired")

    def test_the_stop_is_saved_before_so_the_registry_keeps_the_other_rows(self):
        r = self.runner()
        self.retire(r, "a")
        self.assertEqual({x.tmux for x in load_rows(self.cfg.registry_path)}, {"a", "b", "me", "gone", "sh"})


class ActivateTest(Base):
    """activate flips the flag only, and says what to do next (it never reattaches)."""

    def activate(self, r, name):
        return cmd_activate(self.cfg, r, self.msgs.append, name)

    def test_stopped_session_is_told_to_restore(self):
        r = self.runner()
        self.retire(r, "gone")
        self.msgs.clear()
        self.assertEqual(self.activate(r, "gone"), 0)
        self.assertEqual(self.state("gone"), "active")
        self.assertIn("sampan restore gone 接回", self.text())
        self.assertEqual((self.sent(r), self.killed(r)), ([], []))

    def test_running_session_needs_nothing_else(self):
        r = self.runner()
        self.retire(r, "a", no_stop=True)
        self.msgs.clear()
        self.assertEqual(self.activate(r, "a"), 0)
        self.assertEqual(self.state("a"), "active")
        self.assertIn("已恢復，不需其他動作", self.text())

    def test_a_disabled_profile_is_mentioned(self):
        import dataclasses
        r = self.runner()
        self.retire(r, "gone")
        self.msgs.clear()
        profiles = dict(self.cfg.all_profiles)
        profiles["alice"] = dataclasses.replace(profiles["alice"], disabled=True)
        self.cfg = dataclasses.replace(self.cfg, all_profiles=profiles)
        self.activate(r, "gone")
        self.assertIn("sampan profile enable alice", self.text())

    def test_unknown_name(self):
        self.assertEqual(self.activate(self.runner(), "nope"), 2)


if __name__ == "__main__":
    unittest.main()
