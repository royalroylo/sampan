import tempfile
import unittest
from pathlib import Path

from sampan import actionlog
from sampan.registry import Row, load_rows, write_rows
from sampan.session_run import cmd_move, cmd_switch
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CODEX_UPDATE = ("  ✨ Update available! 0.147.0 -> 0.163.0\n\n"
                "› 1. Update now (runs `npm install -g @openai/codex`)\n  2. Skip\n  3. Skip until next version\n\n"
                "  Press enter to continue\n")
CODEX_HOOKS = ("  Hooks need review\n  2 hooks are new or changed.\n\n› 1. Review hooks\n  2. Trust all and continue\n"
               "  3. Continue without trusting (hooks won't run)\n\n  Press enter to confirm or esc to go back\n")
NO_PROC = Path("/nonexistent-proc-root")
TRUST_YES = (" Accessing workspace:\n\n {path}\n\n Quick safety check: Is this a project you created or one you trust?\n"
             "   No, exit\n ❯ Yes, I trust this folder\n Enter to confirm · Esc to cancel\n")
TRUST = (" Accessing workspace:\n\n {path}\n\n Quick safety check: Is this a project you created or one you trust?\n"
         " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm · Esc to cancel\n")


class MoveFixture(unittest.TestCase):
    """State machine of one pane (%1, shell pid 100): /exit -> shell; launch command sent -> trust prompt or straight up; Down+Enter -> up."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.old = self.tmp / "old"
        self.old.mkdir()
        self.dest = (self.tmp / "new" / "proj").resolve()
        d = self.cfg.default_profile.projects_dir / "-old"
        d.mkdir(parents=True)
        (d / f"{A}.jsonl").write_text("")
        write_rows(self.cfg.registry_path, [Row("994-monitor", "claude", "alice", A, str(self.old))], "t", self.cfg.history_dir)
        self.msgs, self.keys = [], []
        self.claude = 200          # pid of claude in the pane (None = shell only)
        self.has_file = True       # whether claude has a session file
        self.status = "idle"
        self.cwd = str(self.old)
        self.screen = ""
        self.trust_shows = None    # path shown by the trust prompt after launch; None = no prompt appears
        self.exits_on_exit = True
        self.started = False
        self.screen_prefix = ""
        self.cursor_on_yes = False  # trust prompt starts with ❯ already on "Yes, I trust this folder"
        self.live_profile = "alice"  # profile of the claude in the pane (a relaunch with CLAUDE_CONFIG_DIR=… makes it work)
        self.relaunch_cwd = None   # where a relaunched claude runs (None = self.dest, i.e. a move)
        self.sync_files()

    def sync_files(self):
        for prof in self.cfg.profiles.values():
            d = prof.config_dir / "sessions"
            if d.exists():
                for f in d.iterdir():
                    f.unlink()
        if self.claude and self.has_file:
            write_session(self.cfg, self.live_profile, self.claude, sid=A, cwd=self.cwd, status=self.status, tmux="994-monitor:@1.%1")

    def ps(self, _a):
        if self.claude == 100:  # claude is the pane's own process (`tmux new-session … claude`), no shell
            return cp(out=ps_line(100, 1, "claude", f"claude --resume {A}") + "\n")
        lines = [ps_line(100, 1, "-zsh", "-zsh")]
        if self.claude:
            lines.append(ps_line(self.claude, 100, "claude", f"claude --resume {A}"))
        return cp(out="\n".join(lines) + "\n")

    def panes(self, _a):
        cmd = "claude" if self.claude else "zsh"
        return cp(out=pane_line("994-monitor", "%1", 100, cmd, self.cwd) + "\n")

    def send(self, args):
        keys = args[5:]
        self.keys.append(keys)
        if keys == ["/exit", "Enter"] and self.exits_on_exit and self.has_file:
            self.claude = None
        elif keys == ["Escape"] and self.claude and not self.has_file:
            self.claude, self.screen = None, ""
        elif len(keys) == 2 and keys[1] == "Enter" and "--resume" in keys[0]:
            self.claude, self.started = 300, True
            self.cwd = self.relaunch_cwd or str(self.dest)
            self.live_profile = "alice" if "-u CLAUDE_CONFIG_DIR" in keys[0] else "work"
            if self.trust_shows:
                self.has_file, self.screen = False, self.screen_prefix + (TRUST_YES if self.cursor_on_yes else TRUST).format(
                    path=self.trust_shows)
            else:
                self.has_file = True
        elif keys == ["Enter"] and self.screen and (self.keys[-2:-1] == [["Down"]] or self.cursor_on_yes):
            self.has_file, self.screen = True, ""
        self.sync_files()
        return cp()

    def runner(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="srv\n") if "-F" in a else cp())
        r.on(["tmux", "-u", "list-panes"], self.panes)
        r.on(["tmux", "-u", "has-session"], cp())
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=self.screen))
        r.on(["tmux", "-u", "send-keys"], self.send)
        r.on(["ps", "-A"], self.ps)
        r.on(["ps", "eww"], cp())  # no environment readable
        r.on(["ps", "-o", "etime="], cp())
        r.on(["lsof"], cp())
        return r

    def move(self, dest=None, **kw):
        kw.setdefault("own_pane", "%9")
        return cmd_move(self.cfg, self.runner(), self.msgs.append, "994-monitor", str(dest or self.dest),
                        sleep=lambda _: None, proc_root=NO_PROC, **kw)

    def row(self):
        return {r.tmux: r for r in load_rows(self.cfg.registry_path)}["994-monitor"]

    def launch(self):
        return [f"cd {self.dest} && env -u CLAUDE_CONFIG_DIR claude --resume {A}", "Enter"]


class MoveTest(MoveFixture):
    def test_idle_moves_creates_dir_accepts_trust_and_saves(self):
        self.trust_shows = str(self.dest)
        self.assertEqual(self.move(), 0)
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(self.keys, [["C-u"], ["/exit", "Enter"], ["C-u"], self.launch(), ["Down"], ["Enter"]])
        self.assertEqual(self.row().cwd, str(self.dest))
        self.assertEqual(self.row().session_id, A)

    def test_typed_command_containing_dest_does_not_count_as_trust_path(self):
        self.trust_shows = "/Users/u"
        self.screen_prefix = f"cd {self.dest} && sampan claude alice\n"
        self.assertEqual(self.move(timeout=3), 1)
        self.assertNotIn(["Down"], self.keys)

    def test_unreadable_trust_path_keeps_waiting_without_pressing(self):
        self.trust_shows = str(self.dest)
        self.screen_prefix = ""
        rc_screen = " Yes, I trust this folder\n"  # prompt only half drawn: no Accessing workspace
        orig = self.send

        def half(args):
            r = orig(args)
            if self.screen:
                self.screen = rc_screen
            return r
        self.send = half
        self.assertEqual(self.move(timeout=3), 1)
        self.assertNotIn(["Down"], self.keys)
        self.assertIn("沒看到 claude 接回", "\n".join(self.msgs))

    def test_no_trust_prompt_when_dir_already_trusted(self):
        self.assertEqual(self.move(), 0)
        self.assertNotIn(["Down"], self.keys)
        self.assertEqual(self.row().cwd, str(self.dest))

    def test_relay_accepts_matching_claude_trust_without_asking(self):
        self.trust_shows = str(self.dest)
        asked = []
        self.assertEqual(self.move(on_prompt=lambda *a: asked.append(a) or 0), 0)
        self.assertEqual(asked, [])
        self.assertEqual(self.keys[-2:], [["Down"], ["Enter"]])

    def test_relay_asks_for_claude_trust_of_other_path(self):
        from sampan.prompts import SKIP
        self.trust_shows = "/Users/u"
        asked = []
        self.assertEqual(self.move(timeout=3, on_prompt=lambda n, a, p: asked.append(p.trust_path) or SKIP), 1)
        self.assertEqual(asked, ["/Users/u"])
        self.assertNotIn(["Down"], self.keys)

    def test_trust_prompt_for_other_path_is_not_accepted(self):
        self.trust_shows = "/Users/u"
        self.assertEqual(self.move(timeout=3), 1)
        self.assertNotIn(["Down"], self.keys)
        self.assertEqual(self.keys[-1], self.launch())  # nothing was pressed after the launch line
        text = "\n".join(self.msgs)
        self.assertIn("信任", text)
        self.assertIn("/Users/u", text)
        self.assertIn("tmux attach -t 994-monitor", text)

    def test_space_inside_the_path_must_match(self):
        # the prompt shows the path without the space: a different directory, so no keys
        spaced = self.tmp / "new" / "my dir"
        self.trust_shows = str(spaced).replace("my dir", "mydir")
        self.assertEqual(self.move(dest=spaced, timeout=3), 1)
        self.assertNotIn(["Down"], self.keys)
        self.assertNotIn(["Enter"], self.keys)
        self.assertIn("信任提示顯示的是", "\n".join(self.msgs))

    def test_wrapped_long_path_still_matches(self):
        long = self.tmp / "new" / ("a-long-directory-name-" * 6 + "end")
        shown = str(long.resolve())
        self.trust_shows = f"{shown[:70]}\n {shown[70:]}"  # the terminal wrapped it; the second line is indented
        self.assertEqual(self.move(dest=long), 0)
        self.assertEqual(self.keys[-2:], [["Down"], ["Enter"]])

    def test_cursor_already_on_yes_sends_only_enter(self):
        # the key sequence follows where ❯ is, not a fixed Down
        self.trust_shows = str(self.dest)
        self.cursor_on_yes = True
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys, [["C-u"], ["/exit", "Enter"], ["C-u"], self.launch(), ["Enter"]])

    def test_cursor_on_no_sends_down_then_enter(self):
        self.trust_shows = str(self.dest)
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys[-2:], [["Down"], ["Enter"]])

    def test_claude_trust_is_pressed_once(self):
        self.trust_shows = str(self.dest)
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys.count(["Enter"]) + self.keys.count(["Down"]), 2)  # one Down, one Enter

    def _slow_redraw(self, rounds):
        """The trust keys are accepted but the screen only redraws after `rounds` polls."""
        orig_send, orig_panes, state = self.send, self.panes, {"n": None}

        def send(args):
            saved = self.screen
            r = orig_send(args)
            if args[5:] == ["Enter"] and saved:
                self.screen, state["n"] = saved, 0  # keep showing the old prompt
                self.has_file = False
                self.sync_files()
            return r

        def panes(a):
            if state["n"] is not None:
                state["n"] += 1
                if state["n"] > rounds:
                    self.screen, self.has_file, state["n"] = "", True, None
                    self.sync_files()
            return orig_panes(a)
        self.send, self.panes = send, panes

    def test_prompt_still_shown_after_our_answer_waits_and_succeeds(self):
        self.trust_shows = str(self.dest)
        self._slow_redraw(3)
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys.count(["Enter"]) + self.keys.count(["Down"]), 2)  # answered exactly once
        self.assertNotIn("沒有按任何鍵", "\n".join(self.msgs))

    def test_prompt_never_redrawn_reports_truthfully_without_pressing_again(self):
        self.trust_shows = str(self.dest)
        self._slow_redraw(10_000)
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys.count(["Enter"]) + self.keys.count(["Down"]), 2)
        text = "\n".join(self.msgs)
        self.assertNotIn("沒有按任何鍵", text)
        self.assertNotIn("不是", text)
        self.assertIn("已按下信任", text)
        self.assertIn("tmux attach -t 994-monitor", text)

    def _redraw_then_late_agent(self, clear_after, agent_after):
        """After our keys: the old prompt stays for `clear_after` polls, then the screen is empty but the agent only shows up after `agent_after`."""
        orig_send, orig_panes, state = self.send, self.panes, {"n": None}

        def send(args):
            saved = self.screen
            r = orig_send(args)
            if args[5:] == ["Enter"] and saved:
                self.screen, state["n"] = saved, 0
                self.has_file = False
                self.sync_files()
            return r

        def panes(a):
            if state["n"] is not None:
                state["n"] += 1
                if state["n"] > clear_after:
                    self.screen = ""
                if state["n"] > agent_after:
                    self.has_file, state["n"] = True, None
                    self.sync_files()
            return orig_panes(a)
        self.send, self.panes = send, panes

    def test_prompt_gone_early_but_agent_detected_late_still_succeeds(self):
        self.trust_shows = str(self.dest)
        self._redraw_then_late_agent(2, 8)
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys.count(["Enter"]) + self.keys.count(["Down"]), 2)
        self.assertNotIn("已按下信任", "\n".join(self.msgs))

    def test_other_prompt_after_trust_left_by_user_is_asked_once_and_stops(self):
        from sampan.prompts import SKIP
        self.trust_shows = str(self.dest)
        orig, asked = self.send, []

        def send(args):
            saved = self.screen
            r = orig(args)
            if args[5:] == ["Enter"] and saved:  # after our trust a different prompt (another path) appears
                self.has_file, self.screen = False, TRUST.format(path="/Users/u")
                self.sync_files()
            return r
        self.send = send
        self.assertEqual(self.move(on_prompt=lambda n, a, p: asked.append(p.trust_path) or SKIP), 1)
        self.assertEqual(asked, ["/Users/u"])
        self.assertEqual(self.keys.count(["Enter"]) + self.keys.count(["Down"]), 2)
        text = "\n".join(self.msgs)
        self.assertIn("停在提示", text)
        self.assertNotIn("已按下信任", text)

    def test_slow_redraw_wait_does_not_print_screen_unchanged_warning(self):
        self.trust_shows = str(self.dest)
        self._slow_redraw(10_000)
        self.assertEqual(self.move(), 1)
        text = "\n".join(self.msgs)
        self.assertNotIn("送出後畫面沒變", text)
        self.assertIn("已按下信任，但畫面還沒更新", text)

    def test_busy_is_refused_untouched(self):
        self.status = "busy"
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])
        self.assertFalse(self.dest.exists())
        self.assertIn("忙碌", "\n".join(self.msgs))

    def test_worktree_is_refused(self):
        self.cwd = str(self.tmp / "repo" / ".claude" / "worktrees" / "x")
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])

    def test_own_pane_is_refused(self):
        self.assertEqual(self.move(own_pane="%1"), 1)
        self.assertEqual(self.keys, [])

    def test_agent_as_pane_root_is_refused_untouched(self):
        # the pane closes when that agent exits, so there is no shell to relaunch in
        self.claude = 100
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])
        self.assertFalse(self.dest.exists())
        text = "\n".join(self.msgs)
        self.assertIn("後面沒有 shell", text)
        self.assertIn("sampan stop 994-monitor、sampan restore 994-monitor", text)

    def test_stuck_at_old_trust_prompt_is_escaped(self):
        self.has_file, self.screen = False, TRUST.format(path=self.old)
        self.sync_files()
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys[0], ["Escape"])
        self.assertEqual(self.row().cwd, str(self.dest))

    def test_old_trust_prompt_only_in_scrollback_is_not_escaped(self):
        self.has_file = False
        self.screen = TRUST.format(path=self.old) + "".join(f"output line {i}\n" for i in range(10))
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])
        self.assertIn("判斷不出狀態，不動", "\n".join(self.msgs))

    def test_old_trust_prompt_for_other_path_is_not_escaped(self):
        self.has_file, self.screen = False, TRUST.format(path="/Users/u/elsewhere")
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])
        self.assertIn("判斷不出狀態，不動", "\n".join(self.msgs))

    def test_old_trust_prompt_is_not_escaped_for_a_codex_row(self):
        write_rows(self.cfg.registry_path, [Row("994-monitor", "codex", "", TID, str(self.old))], "t", self.cfg.history_dir)
        rollout = self.cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
        self.has_file, self.screen = False, TRUST.format(path=self.old)
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])

    def test_row_without_session_id_is_refused(self):
        for sid in ("", "*"):
            write_rows(self.cfg.registry_path, [Row("994-monitor", "claude", "alice", sid, str(self.old))], "t",
                       self.cfg.history_dir)
            self.claude = None
            self.sync_files()
            self.assertEqual(self.move(), 1)
            self.assertEqual(self.keys, [])
            self.assertFalse(self.dest.exists())
        text = "\n".join(self.msgs)
        self.assertIn("沒有對話 id", text)
        self.assertIn("不是 UUID", text)

    def test_conversation_running_in_another_pane_is_refused(self):
        self.claude = None
        self.sync_files()
        orig_panes, orig_ps = self.panes, self.ps
        self.panes = lambda a: cp(out=orig_panes(a).stdout + pane_line("other", "%9", 101, "claude", "/c") + "\n")
        self.ps = lambda a: cp(out=orig_ps(a).stdout + ps_line(101, 1, "-zsh", "-zsh") + "\n"
                               + ps_line(400, 101, "claude", "claude") + "\n")
        write_session(self.cfg, "alice", 400, sid=A, tmux="other:@2.%9")
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])
        self.assertIn("這個對話已在 other 執行，不重複開", "\n".join(self.msgs))

    def test_live_profile_other_than_registered_is_refused(self):
        write_rows(self.cfg.registry_path, [Row("994-monitor", "claude", "work", A, str(self.old))], "t", self.cfg.history_dir)
        self.assertEqual(self.move(), 1)  # the session file is in alice, the registered profile is work
        self.assertEqual(self.keys, [])
        self.assertIn("alice", "\n".join(self.msgs))

    def test_transcript_of_other_profile_is_refused(self):
        write_rows(self.cfg.registry_path, [Row("994-monitor", "claude", "work", A, str(self.old))], "t", self.cfg.history_dir)
        self.claude = None
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])
        self.assertIn("逐字稿在 profile alice，不是 work", "\n".join(self.msgs))

    def test_claude_without_session_file_and_no_prompt_is_refused(self):
        self.has_file = False
        self.sync_files()
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])

    def test_shell_pane_is_launched_directly(self):
        self.claude = None
        self.sync_files()
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys[:2], [["C-u"], self.launch()])

    def test_exit_timeout_does_not_launch(self):
        self.exits_on_exit = False
        self.assertEqual(self.move(timeout=4), 1)
        self.assertFalse(self.started)

    def test_unknown_name_and_same_dir_and_file_dest(self):
        r = cmd_move(self.cfg, self.runner(), self.msgs.append, "nope", str(self.dest), sleep=lambda _: None)
        self.assertEqual(r, 2)
        self.assertEqual(self.move(dest=self.old), 0)
        self.assertEqual(self.keys, [])
        f = self.tmp / "afile"
        f.write_text("")
        self.assertEqual(self.move(dest=f), 2)

    def test_move_to_dir_with_space_and_cjk(self):
        # Review Focus 2: target ~/我的 專案; a trust-prompt path containing a space and Chinese characters still counts as a match
        self.dest = (self.cfg.home / "我的 專案").resolve()
        self.trust_shows = str(self.dest)
        self.assertEqual(self.move(), 0)
        self.assertEqual(self.keys[3], [f"cd '{self.dest}' && env -u CLAUDE_CONFIG_DIR claude --resume {A}", "Enter"])
        self.assertEqual(self.keys[4:], [["Down"], ["Enter"]])
        self.assertEqual(self.row().cwd, str(self.dest))

    def test_multi_agent_session_is_refused(self):
        orig = self.panes

        def two(a):
            return cp(out=orig(a).stdout + pane_line("994-monitor", "%2", 101, "codex", self.cwd) + "\n")
        self.panes = two
        orig_ps = self.ps
        self.ps = lambda a: cp(out=orig_ps(a).stdout + ps_line(101, 1, "-zsh", "-zsh") + "\n"
                               + ps_line(301, 101, "codex", "codex") + "\n")
        self.assertEqual(self.move(), 1)
        self.assertEqual(self.keys, [])
        self.assertIn("拆成各自的 tmux session", "\n".join(self.msgs))

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.move(dry_run=True), 0)
        self.assertEqual(self.keys, [])
        self.assertFalse(self.dest.exists())
        self.assertIn("將建立", "\n".join(self.msgs))



class CodexMoveTest(unittest.TestCase):
    """Codex in pane %1 (shell 100): /quit+Enter -> shell; launch command+Enter -> trust prompt / other prompt / straight up; Enter -> up."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.old = self.tmp / "a"
        self.old.mkdir()
        self.dest = (self.tmp / "b").resolve()
        rollout = self.cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
        self.lsof_out = f"p200\nf10\nn{rollout}\n"
        write_rows(self.cfg.registry_path, [Row("zz-cx", "codex", "", TID, str(self.old))], "t", self.cfg.history_dir)
        self.msgs, self.keys, self.events = [], [], []
        self.codex = 200
        self.ready = True  # codex has loaded the conversation (lsof can see the rollout)
        self.cwd = str(self.old)
        self.idle = (FIXTURES / "codex_idle.txt").read_text()
        self.screen = self.idle
        self.startup = None  # screen after launching in the new directory; None = starts straight up

    def ps(self, _a):
        lines = [ps_line(100, 1, "-zsh", "-zsh")]
        if self.codex:
            lines.append(ps_line(self.codex, 100, "codex", "codex"))
        return cp(out="\n".join(lines) + "\n")

    def panes(self, _a):
        return cp(out=pane_line("zz-cx", "%1", 100, "codex" if self.codex else "zsh", self.cwd) + "\n")

    def send(self, args):
        keys = args[5:]
        self.keys.append(keys)
        self.events.append(keys)
        prev = self.events[:-1][-2:]
        if keys == ["Enter"] and prev == [["/quit"], ("sleep", 1)] and self.codex:
            self.codex, self.screen = None, "% "
        elif keys == ["Enter"] and prev[-1:] == [("sleep", 1)] and "codex resume" in str(prev[0]):
            self.codex, self.cwd = 300, str(self.dest)
            self.ready = self.startup is None
            self.screen = self.idle if self.startup is None else self.startup
        elif keys == ["1"] and self.codex and not self.ready and "Do you trust" in self.screen:
            self.ready, self.screen = True, self.idle
        return cp()

    def runner(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="srv\n") if "-F" in a else cp())
        r.on(["tmux", "-u", "list-panes"], self.panes)
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=self.screen))
        r.on(["tmux", "-u", "send-keys"], self.send)
        r.on(["ps", "-A"], self.ps)
        r.on(["ps", "eww"], cp())  # no environment readable
        r.on(["ps", "-o", "etime="], cp(rc=1))
        r.on(["lsof"], lambda a: cp(out=self.lsof_out.replace("p200", f"p{a[2]}") if self.ready else f"p{a[2]}\n"))
        return r

    def move(self, **kw):
        kw.setdefault("own_pane", "%9")
        return cmd_move(self.cfg, self.runner(), self.msgs.append, "zz-cx", str(self.dest),
                        sleep=lambda s: self.events.append(("sleep", s)), proc_root=NO_PROC, **kw)

    def row(self):
        return {r.tmux: r for r in load_rows(self.cfg.registry_path)}["zz-cx"]

    def trust(self, path):
        return (FIXTURES / "codex_trust.txt").read_text().replace("/Users/u/proj", path)

    def test_move_codex_accepts_matching_trust_with_its_digit(self):
        self.startup = self.trust(str(self.dest))
        self.assertEqual(self.move(), 0)
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(self.keys, [["C-u"], ["/quit"], ["Enter"], ["C-u"],
                                     [f"cd {self.dest} && codex resume {TID} -C {self.dest}"], ["Enter"], ["1"]])
        self.assertNotIn(["Down"], self.keys)
        self.assertEqual((self.row().cwd, self.row().session_id), (str(self.dest), TID))

    def test_move_codex_refuses_other_path(self):
        self.startup = self.trust("/Users/u")
        self.assertEqual(self.move(timeout=3), 1)
        self.assertEqual(self.keys[-1], ["Enter"])  # the last one is the launch command's Enter; nothing was pressed for the trust prompt
        self.assertEqual(len(self.keys), 6)
        self.assertIn("/Users/u", "\n".join(self.msgs))
        self.assertIn("tmux attach -t zz-cx", "\n".join(self.msgs))

    def test_move_codex_refuses_truncated_path(self):
        # codex 0.147 on a real machine: a path wider than the pane is truncated, not wrapped -> equality can't be confirmed, so nothing is pressed
        self.startup = self.trust(str(self.dest)[:-3])
        self.assertEqual(self.move(timeout=3), 1)
        self.assertEqual(len(self.keys), 6)
        self.assertIn("截斷", "\n".join(self.msgs))
        self.assertIn("tmux attach -t zz-cx", "\n".join(self.msgs))

    def test_move_codex_never_answers_update_prompt(self):
        self.startup = CODEX_UPDATE
        self.assertEqual(self.move(timeout=3), 1)
        self.assertEqual(len(self.keys), 6)  # nothing after the launch line: never "1" (Update now)
        self.assertNotIn(["1"], self.keys)
        text = "\n".join(self.msgs)
        self.assertIn("Update available", text)
        self.assertIn("停在 codex 的提示", text)
        self.assertIn("tmux attach -t zz-cx", text)

    def test_move_codex_never_answers_hooks_review(self):
        self.startup = CODEX_HOOKS
        self.assertEqual(self.move(timeout=3), 1)
        self.assertEqual(len(self.keys), 6)
        self.assertIn("Hooks need review", "\n".join(self.msgs))
        self.assertIn("tmux attach -t zz-cx", "\n".join(self.msgs))

    def test_move_codex_unreadable_menu_gets_no_keys(self):
        self.startup = "  Something new happened\n  Press enter to continue\n"
        self.assertEqual(self.move(timeout=3), 1)
        self.assertEqual(len(self.keys), 6)
        self.assertIn("tmux attach -t zz-cx", "\n".join(self.msgs))

    def relayed(self, screens):
        """digits sent to the pane step through `screens`; the last one is the idle composer with the thread loaded."""
        orig, steps = self.send, list(screens)

        def send(args):
            r = orig(args)
            keys = args[5:]
            if len(keys) == 1 and keys[0].isdigit():
                self.screen = steps.pop(0)
                if not steps:
                    self.ready = True
            return r
        self.send = send

    def test_move_codex_relays_update_then_accepts_matching_trust(self):
        self.startup = CODEX_UPDATE
        self.relayed([self.trust(str(self.dest)), self.idle])
        asked = []
        rc = self.move(on_prompt=lambda name, agent, p: asked.append(p.question) or 1)
        self.assertEqual(rc, 0)
        self.assertEqual(asked, ["✨ Update available! 0.147.0 -> 0.163.0"])  # trusting the new directory needs no question
        self.assertEqual(self.keys[-2:], [["2"], ["1"]])
        self.assertEqual(self.row().cwd, str(self.dest))

    def test_move_codex_trust_for_other_path_is_asked_not_pressed(self):
        from sampan.prompts import SKIP
        self.startup = self.trust("/Users/u")
        asked = []
        rc = self.move(timeout=3, on_prompt=lambda name, agent, p: asked.append(p.detail) or SKIP)
        self.assertEqual(rc, 1)
        self.assertEqual(asked, [("目錄：/Users/u",)])
        self.assertEqual(len(self.keys), 6)
        self.assertIn("⚠ zz-cx 停在提示，進去處理", self.msgs)

    def test_move_codex_attach_ends_the_wait(self):
        from sampan.prompts import ATTACH
        self.startup = CODEX_UPDATE
        self.assertEqual(self.move(timeout=3, on_prompt=lambda *_a: ATTACH), 1)
        self.assertEqual(len(self.keys), 6)
        self.assertIn("→ zz-cx：進去處理提示", self.msgs)

    def test_move_codex_without_trust_prompt(self):
        self.assertEqual(self.move(), 0)
        self.assertEqual(len(self.keys), 6)
        self.assertEqual(self.row().cwd, str(self.dest))

    def test_move_codex_busy_or_unknown_is_refused(self):
        for name in ("codex_working.txt", "codex_waiting.txt"):
            self.screen = (FIXTURES / name).read_text()
            self.keys = []
            self.assertEqual(self.move(), 1)
            self.assertEqual(self.keys, [])
            self.assertFalse(self.dest.exists())



class SwitchTest(MoveFixture):
    """sampan switch: the move flow in the session's own directory, under another profile."""

    def setUp(self):
        super().setUp()
        self.relaunch_cwd = str(self.old)
        self.side = self.cfg.default_profile.projects_dir / "-old" / A
        (self.side / "subagents").mkdir(parents=True)
        (self.side / "subagents" / "x.jsonl").write_text("s")

    def switch(self, profile="work", **kw):
        kw.setdefault("own_pane", "%9")
        return cmd_switch(self.cfg, self.runner(), self.msgs.append, "994-monitor", profile,
                          sleep=lambda _: None, proc_root=NO_PROC, **kw)

    def old_transcript(self):
        return self.cfg.default_profile.projects_dir / "-old" / f"{A}.jsonl"

    def new_transcript(self):
        return self.cfg.profiles["work"].projects_dir / "-old" / f"{A}.jsonl"

    def test_running_moves_transcript_and_relaunches_under_new_profile(self):
        self.assertEqual(self.switch(), 0)
        work = self.cfg.profiles["work"].config_dir
        self.assertIn([f"cd {self.old} && env CLAUDE_CONFIG_DIR={work} claude --resume {A}", "Enter"], self.keys)
        self.assertFalse(self.old_transcript().exists())  # moved, never copied: one copy only
        self.assertTrue(self.new_transcript().exists())
        self.assertTrue((self.new_transcript().with_suffix("") / "subagents" / "x.jsonl").exists())
        self.assertFalse(self.side.exists())
        self.assertEqual(self.row().profile, "work")
        self.assertEqual(self.row().cwd, str(self.old))
        text = "\n".join(self.msgs)
        self.assertIn("已把逐字稿搬到 work", text)
        self.assertIn("✔ 994-monitor 已換到 work", text)
        log = actionlog.log_path(self.cfg).read_text()
        self.assertIn("[switch 994-monitor → work] ✔ 994-monitor 已換到 work", log)

    def test_shared_projects_moves_nothing(self):
        work = self.cfg.profiles["work"].config_dir
        (work / "projects").symlink_to(self.cfg.default_profile.projects_dir)
        self.assertEqual(self.switch(), 0)
        self.assertTrue(self.old_transcript().exists())
        self.assertNotIn("已把逐字稿搬到", "\n".join(self.msgs))
        self.assertEqual(self.row().profile, "work")

    def test_plugin_gap_is_shown_but_does_not_block(self):
        self.assertEqual(self.switch(), 0)
        self.assertIn("⚠ work 沒有裝：superpowers@claude-plugins-official", "\n".join(self.msgs))

    def test_busy_is_refused_and_nothing_moves(self):
        self.status = "busy"
        self.sync_files()
        self.assertEqual(self.switch(), 1)
        self.assertEqual(self.keys, [])
        self.assertTrue(self.old_transcript().exists())
        self.assertIn("等它閒置再換", "\n".join(self.msgs))

    def test_transcript_already_in_new_profile_is_refused_untouched(self):
        self.new_transcript().parent.mkdir(parents=True)
        self.new_transcript().write_text("other")
        self.assertEqual(self.switch(), 1)
        self.assertEqual(self.keys, [])
        self.assertTrue(self.old_transcript().exists())
        self.assertIn("兩份逐字稿", "\n".join(self.msgs))

    def test_exit_timeout_gives_quota_menu_hint_and_moves_nothing(self):
        self.exits_on_exit = False
        self.assertEqual(self.switch(timeout=3), 1)
        self.assertTrue(self.old_transcript().exists())
        text = "\n".join(self.msgs)
        self.assertIn("沒有用 work 啟動", text)
        self.assertIn("按 Esc 再打 /exit", text)

    def test_same_profile_unknown_profile_and_codex(self):
        self.assertEqual(self.switch("alice"), 0)
        self.assertIn("已經是 alice", self.msgs[-1])
        self.assertEqual(self.switch("nope"), 2)
        write_rows(self.cfg.registry_path, [Row("994-monitor", "codex", "", A, str(self.old))], "t", self.cfg.history_dir)
        self.assertEqual(self.switch(), 2)
        self.assertIn("只有 Claude session 能換 profile", self.msgs[-1])
        self.assertEqual(self.keys, [])

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.switch(dry_run=True), 0)
        self.assertEqual(self.keys, [])
        self.assertTrue(self.old_transcript().exists())
        self.assertEqual(self.row().profile, "alice")
        self.assertIn("搬逐字稿到 work", "\n".join(self.msgs))

    def test_stopped_session_changes_registry_moves_transcript_and_restores(self):
        self.claude = None
        calls = []
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("other", "%5", 500, "zsh", "/x") + "\n"))
        rc = cmd_switch(self.cfg, r, self.msgs.append, "994-monitor", "work", sleep=lambda _: None,
                        proc_root=NO_PROC, restore_fn=lambda *a, **k: calls.append(k) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["names"], ("994-monitor",))
        self.assertFalse(calls[0]["confirm"])
        self.assertEqual(self.row().profile, "work")
        self.assertTrue(self.new_transcript().exists())
        self.assertFalse(self.old_transcript().exists())
        self.assertEqual(self.keys, [])

    def test_interrupted_earlier_switch_is_completed(self):
        # an earlier switch moved the transcript, then the relaunch failed: the row may still say alice
        self.new_transcript().parent.mkdir(parents=True)
        self.old_transcript().rename(self.new_transcript())
        self.assertEqual(self.switch(), 0)
        self.assertEqual(self.row().profile, "work")
        self.assertNotIn("已把逐字稿搬到", "\n".join(self.msgs))

    def test_registry_follows_the_transcript_even_if_the_relaunch_fails(self):
        real_send = self.send

        def no_relaunch(args):  # the new account never comes up (e.g. not logged in)
            if "--resume" in " ".join(args):
                self.keys.append(args[5:])
                return cp()
            return real_send(args)
        self.send = no_relaunch
        self.assertEqual(self.switch(timeout=3), 1)
        self.assertTrue(self.new_transcript().exists())
        self.assertEqual(self.row().profile, "work")  # restore / switch still find it

    def test_other_conversation_in_the_pane_is_not_success(self):
        real_send = self.send

        def wrong_id(args):
            res = real_send(args)
            if "--resume" in " ".join(args):  # something else came up: a fresh conversation id
                write_session(self.cfg, "work", self.claude, sid="bbbbbbbb-0000-4000-8000-000000000002",
                              cwd=str(self.old), status="idle", tmux="994-monitor:@1.%1")
            return res
        self.send = wrong_id
        self.assertEqual(self.switch(timeout=3), 1)
        self.assertNotIn("✔ 994-monitor 已換到", "\n".join(self.msgs))

    def test_move_failure_puts_everything_back(self):
        import os
        from unittest import mock

        real = os.rename
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 2:  # the sidecar directory fails after the .jsonl moved
                raise OSError("boom")
            return real(src, dst)
        with mock.patch("sampan.claude_state.os.rename", side_effect=flaky):
            self.assertEqual(self.switch(), 1)
        self.assertTrue(self.old_transcript().exists())
        self.assertTrue(self.side.is_dir())
        self.assertFalse(self.new_transcript().exists())
        self.assertIn("逐字稿搬不過去（boom）", "\n".join(self.msgs))
        self.assertEqual(self.row().profile, "alice")

    def test_sidecar_already_in_new_profile_is_refused(self):
        (self.cfg.profiles["work"].projects_dir / "-old" / A).mkdir(parents=True)
        self.assertEqual(self.switch(), 1)
        self.assertEqual(self.keys, [])
        self.assertIn("附屬資料夾", "\n".join(self.msgs))

    def test_copy_in_another_project_folder_is_refused(self):
        other = self.cfg.profiles["work"].projects_dir / "-elsewhere"
        other.mkdir(parents=True)
        (other / f"{A}.jsonl").write_text("x")
        self.assertEqual(self.switch(), 1)
        self.assertIn("兩份逐字稿", "\n".join(self.msgs))

    def stopped_switch(self, **kw):
        self.claude = None
        self.calls = []
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("other", "%5", 500, "zsh", "/x") + "\n"))
        return cmd_switch(self.cfg, r, self.msgs.append, "994-monitor", "work", sleep=lambda _: None,
                          proc_root=NO_PROC, restore_fn=lambda *a, **k: self.calls.append(k) or 0, **kw)

    def test_stopped_retired_or_missing_dir_is_refused_untouched(self):
        write_rows(self.cfg.registry_path, [Row("994-monitor", "claude", "alice", A, str(self.old), "retired")], "t",
                   self.cfg.history_dir)
        self.assertEqual(self.stopped_switch(), 2)
        self.assertIn("sampan activate 994-monitor", self.msgs[-1])
        write_rows(self.cfg.registry_path, [Row("994-monitor", "claude", "alice", A, str(self.tmp / "gone"))], "t",
                   self.cfg.history_dir)
        self.assertEqual(self.stopped_switch(), 2)
        self.assertIn("目錄不存在", self.msgs[-1])
        self.assertEqual(self.calls, [])
        self.assertTrue(self.old_transcript().exists())

    def test_stopped_but_conversation_live_elsewhere_is_refused(self):
        self.claude = None
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("other", "%5", 100, "claude", str(self.old)) + "\n"))
        r.on(["ps", "-A"], cp(out=ps_line(100, 1, "-zsh", "-zsh") + "\n" + ps_line(400, 100, "claude", "claude") + "\n"))
        write_session(self.cfg, "alice", 400, sid=A, cwd=str(self.old), status="idle", tmux="other:@5.%5")
        calls = []
        rc = cmd_switch(self.cfg, r, self.msgs.append, "994-monitor", "work", sleep=lambda _: None,
                        proc_root=NO_PROC, restore_fn=lambda *a, **k: calls.append(k) or 0)
        self.assertEqual(rc, 1)
        self.assertIn("已在 other 執行", self.msgs[-1])
        self.assertEqual(calls, [])
        self.assertTrue(self.old_transcript().exists())
        self.assertEqual(self.row().profile, "alice")


if __name__ == "__main__":
    unittest.main()
