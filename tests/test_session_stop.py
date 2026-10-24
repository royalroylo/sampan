import signal
import tempfile
import unittest
from pathlib import Path

from sampan.session_run import cmd_stop
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
NO_PROC = Path("/nonexistent-proc-root")
PANE_OF = {200: 100, 201: 101, 202: 102, 203: 103}


class StopTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.alive = {200, 201, 202}
        self.reparented = set()  # pid replaced by another process (tests re-verification before force)
        self.exit_on_keys = True
        self.msgs, self.killed = [], []
        self.panes = [pane_line("a", "%1", 100, "claude", "/c"), pane_line("b", "%2", 101, "claude", "/c"),
                      pane_line("me", "%3", 102, "claude", "/c")]
        write_session(self.cfg, "alice", 200, sid=A, tmux="a:@1.%1", status="idle")
        write_session(self.cfg, "work", 201, sid=B, tmux="b:@2.%2", status="busy")
        write_session(self.cfg, "alice", 202, sid=C, tmux="me:@3.%3", status="idle")

    def ps(self, _args):
        lines = [ps_line(pp, 1, "-zsh", "-zsh") for pp in (100, 101, 102, 103)]
        for pid in sorted(self.alive):
            if pid in self.reparented:
                lines.append(ps_line(pid, 1, "node", "node other"))
            else:
                lines.append(ps_line(pid, PANE_OF[pid], "claude", "claude"))
        return cp(out="\n".join(lines) + "\n")

    def runner(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="srv\n") if "-F" in a else cp())
        r.on(["tmux", "-u", "list-panes"], lambda a: cp(out="\n".join(self.panes) + "\n"))
        r.on(["ps", "-A"], self.ps)
        r.on(["ps", "eww"], cp())  # no environment readable
        r.on(["tmux", "-u", "kill-session"], cp())

        def send_keys(args):
            if self.exit_on_keys and args[-2:] == ["/exit", "Enter"] and args[4] == "%1":
                self.alive.discard(200)
            return cp()
        r.on(["tmux", "-u", "send-keys"], send_keys)
        return r

    def stop(self, r, **kw):
        kw.setdefault("own_pane", "%3")
        return cmd_stop(self.cfg, r, self.msgs.append, sleep=lambda _: None, proc_root=NO_PROC,
                        kill=lambda pid, sig: self.killed.append((pid, sig)), **kw)

    def sent(self, r):
        return [c["args"][4:] for c in r.called(["tmux", "-u", "send-keys"])]

    def test_idle_gets_exit_busy_and_self_skipped(self):
        # Review Focus 5
        r = self.runner()
        rc = self.stop(r)
        self.assertEqual(self.sent(r), [["%1", "C-u"], ["%1", "/exit", "Enter"]])
        self.assertEqual(rc, 1)  # b is busy
        text = "\n".join(self.msgs)
        self.assertIn("b（busy）", text)
        self.assertIn("自己", text)
        self.assertTrue(self.cfg.registry_path.exists())  # save first

    def test_cmd_stop_fills_stopped_and_left(self):
        r = self.runner()
        stopped, left = [], []
        self.stop(r, stopped=stopped, left=left)
        self.assertEqual(stopped, ["a"])
        self.assertEqual(sorted(left), ["b", "me"])

    def killed_sessions(self, r):
        return [c["args"][3:] for c in r.called(["tmux", "-u", "kill-session"])]

    def test_exited_single_pane_session_is_closed(self):
        r = self.runner()
        self.stop(r, names=("a",))
        self.assertEqual(self.killed_sessions(r), [["-t", "=a"]])
        self.assertIn("tmux session 已關閉", "\n".join(self.msgs))

    def test_agent_as_pane_root_session_gone_counts_as_closed(self):
        # `tmux new-session -s a claude`: /exit ends the pane's own process and tmux closes the session itself
        self.panes[0] = pane_line("a", "%1", 200, "claude", "/c")
        r = self.runner()
        r.on(["ps", "-A"], lambda a: cp(out=self.ps(a).stdout.replace(ps_line(200, 100, "claude", "claude"),
                                                                      ps_line(200, 1, "claude", "claude"))))
        r.on(["tmux", "-u", "list-panes"],
             lambda a: cp(out="\n".join(p for p in self.panes if 200 in self.alive or not p.startswith("a\t")) + "\n"))
        self.stop(r, names=("a",))
        self.assertEqual(self.sent(r), [["%1", "C-u"], ["%1", "/exit", "Enter"]])
        self.assertEqual(self.killed_sessions(r), [])  # nothing left to kill
        text = "\n".join(self.msgs)
        self.assertIn("a：已結束，tmux session 已關閉", text)
        self.assertNotIn("保留 session", text)

    def test_closed_session_stays_in_registry_for_restore(self):
        # Regression: since v0.3.2 stop closes tmux, and the next save treated it as closed by the user and deleted that row
        from sampan.registry import load_rows, merge
        self.stop(self.runner(), names=("a",))
        rows = load_rows(self.cfg.registry_path)
        res = merge(rows, [], {"b", "me"}, "2026-10-21T00:00:00+08:00", "srv")
        self.assertIn("a", res.pending)
        self.assertIn("a", [r.tmux for r in res.rows])

    def test_progress_is_reported_while_waiting(self):
        self.exit_on_keys = False
        self.stop(self.runner(), names=("a",), timeout=10)
        text = "\n".join(self.msgs)
        self.assertIn("▶ a：已送出 /exit，等待結束…", text)
        self.assertIn("… 還在等 a（10／10 秒）", text)

    def test_finished_is_reported_as_it_happens(self):
        self.stop(self.runner(), names=("a",))
        self.assertIn("· a 已結束（2 秒）", "\n".join(self.msgs))

    def test_session_with_other_panes_is_kept(self):
        self.panes.append(pane_line("a", "%9", 109, "npm", "/c"))
        r = self.runner()
        self.stop(r, names=("a",))
        self.assertEqual(self.killed_sessions(r), [])
        self.assertIn("a：已結束；tmux 裡還有其他視窗或 pane，保留 session", "\n".join(self.msgs))

    def test_pane_opened_while_waiting_keeps_session(self):
        r = self.runner()

        def panes(_a):  # after /exit is sent, the user opened another pane in a
            extra = [] if 200 in self.alive else [pane_line("a", "%9", 109, "zsh", "/c")]
            return cp(out="\n".join(self.panes + extra) + "\n")
        r.on(["tmux", "-u", "list-panes"], panes)
        self.stop(r, names=("a",))
        self.assertEqual(self.killed_sessions(r), [])

    def test_keep_tmux_leaves_session(self):
        r = self.runner()
        self.stop(r, names=("a",), keep_tmux=True)
        self.assertEqual(self.killed_sessions(r), [])

    def test_timed_out_session_is_not_closed(self):
        self.exit_on_keys = False
        r = self.runner()
        self.stop(r, names=("a",), timeout=4)
        self.assertEqual(self.killed_sessions(r), [])

    def test_timeout_and_force(self):
        self.exit_on_keys = False
        self.assertEqual(self.stop(self.runner(), names=("a",), timeout=4), 1)
        self.assertEqual(self.killed, [])
        self.stop(self.runner(), names=("a",), timeout=4, force=True)
        self.assertEqual(self.killed, [(200, signal.SIGTERM)])

    def test_force_reverifies_pid_before_kill(self):
        # Review R2: pid reused by another process while waiting -> must not kill
        self.exit_on_keys = False
        r = self.runner()

        def send_keys(args):
            if args[-2:] == ["/exit", "Enter"]:
                self.reparented.add(200)
            return cp()
        r.on(["tmux", "-u", "send-keys"], send_keys)
        self.stop(r, names=("a",), timeout=4, force=True)
        self.assertEqual(self.killed, [])

    def test_claude_without_session_file_is_reported_not_ignored(self):
        # Review R6
        self.alive.add(203)
        self.panes.append(pane_line("nofile", "%4", 103, "claude", "/c"))
        r = self.runner()
        rc = self.stop(r, names=("nofile",))
        self.assertEqual(rc, 1)
        self.assertEqual(self.sent(r), [])
        self.assertTrue(any("nofile" in m and "沒有 session 檔" in m for m in self.msgs))

    def test_profile_filter_and_dry_run(self):
        r = self.runner()
        self.assertEqual(self.stop(r, profile="alice", dry_run=True), 0)
        self.assertEqual(self.sent(r), [])
        self.assertTrue(any("[dry-run]" in m and "a" in m for m in self.msgs))

    def test_no_own_pane_stops_everything_idle(self):
        r = self.runner()
        self.stop(r, own_pane=None)
        self.assertEqual({a[0] for a in self.sent(r)}, {"%1", "%3"})

    def test_worktree_session_is_not_sent_exit(self):
        # Task 14 real-machine test: a --worktree session receiving /exit pops up a Keep/Remove worktree confirmation
        write_session(self.cfg, "alice", 200, sid=A, tmux="a:@1.%1", status="idle",
                      cwd="/Users/u/proj/.claude/worktrees/feature-schedule")
        r = self.runner()
        rc = self.stop(r, names=("a",))
        self.assertEqual(self.sent(r), [])
        self.assertEqual(rc, 1)
        self.assertTrue(any("worktree" in m and "Keep worktree" in m for m in self.msgs))


TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CodexStopTest(unittest.TestCase):
    """Pane %1 (shell 100) runs codex 300; the screen is determined by self.screen."""

    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.msgs, self.events = [], []
        self.codex_alive = True
        self.screen = (FIXTURES / "codex_idle.txt").read_text()
        self.panes = [pane_line("cx", "%1", 100, "codex", "/c")]
        self.extra_ps = []

    def ps(self, _a):
        lines = [ps_line(100, 1, "-zsh", "-zsh"), *self.extra_ps]
        if self.codex_alive:
            lines.append(ps_line(300, 100, "codex", "codex"))
        return cp(out="\n".join(lines) + "\n")

    def runner(self):
        rollout = self.cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="srv\n") if "-F" in a else cp())
        r.on(["tmux", "-u", "list-panes"], lambda a: cp(out="\n".join(self.panes) + "\n"))
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=self.screen))
        r.on(["ps", "-A"], self.ps)
        r.on(["ps", "eww"], cp())  # no environment readable
        r.on(["tmux", "-u", "kill-session"], cp())
        r.on(["lsof"], cp(out=f"p300\nf10\nn{rollout}\n"))

        def send_keys(args):
            self.events.append(args[4:])
            if args[4:] == ["%1", "Enter"] and self.events[-3:-1] == [["%1", "/quit"], ("sleep", 1)]:
                self.codex_alive = False
            return cp()
        r.on(["tmux", "-u", "send-keys"], send_keys)
        return r

    def stop(self, r, **kw):
        return cmd_stop(self.cfg, r, self.msgs.append, sleep=lambda s: self.events.append(("sleep", s)),
                        proc_root=NO_PROC, kill=lambda pid, sig: None, **kw)

    def test_stop_codex_idle_sends_quit_then_enter_separately(self):
        r = self.runner()
        self.assertEqual(self.stop(r, names=("cx",)), 0)
        keys = [e for e in self.events if e != ("sleep", 2)]
        self.assertEqual(keys, [["%1", "C-u"], ["%1", "/quit"], ("sleep", 1), ["%1", "Enter"]])
        self.assertIn("sampan stop：已結束 1", "\n".join(self.msgs))

    def test_stop_codex_unknown_is_left_alone(self):
        self.screen = (FIXTURES / "codex_waiting.txt").read_text()  # waiting for approval: judged unknown
        r = self.runner()
        self.assertEqual(self.stop(r, names=("cx",)), 1)
        self.assertEqual([e for e in self.events if isinstance(e, list)], [])
        self.assertIn("cx（unknown）", "\n".join(self.msgs))

    def test_stop_codex_working_is_left_alone(self):
        self.screen = (FIXTURES / "codex_working.txt").read_text()
        r = self.runner()
        self.assertEqual(self.stop(r, names=("cx",)), 1)
        self.assertEqual([e for e in self.events if isinstance(e, list)], [])

    def test_stop_refuses_multi_agent_session(self):
        self.panes.append(pane_line("cx", "%2", 101, "claude", "/c"))
        self.extra_ps = [ps_line(101, 1, "-zsh", "-zsh"), ps_line(201, 101, "claude", "claude")]
        write_session(self.cfg, "alice", 201, sid=A, tmux="cx:@1.%2", status="idle")
        r = self.runner()
        self.assertEqual(self.stop(r, names=("cx",)), 1)
        self.assertEqual([e for e in self.events if isinstance(e, list)], [])
        self.assertIn("拆成各自的 tmux session", "\n".join(self.msgs))

    def test_stop_counts_codex_even_when_codex_disabled(self):
        import dataclasses
        self.cfg = dataclasses.replace(self.cfg, codex=dataclasses.replace(self.cfg.codex, enabled=False))
        self.panes.append(pane_line("cx", "%2", 101, "claude", "/c"))
        self.extra_ps = [ps_line(101, 1, "-zsh", "-zsh"), ps_line(201, 101, "claude", "claude")]
        write_session(self.cfg, "alice", 201, sid=A, tmux="cx:@1.%2", status="idle")
        r = self.runner()
        self.assertEqual(self.stop(r, names=("cx",)), 1)
        self.assertEqual([e for e in self.events if isinstance(e, list)], [])
        self.assertIn("拆成各自的 tmux session", "\n".join(self.msgs))
