import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan.adopt import cmd_adopt, default_name, outside_agents
from sampan.registry import Row, load_rows, write_rows
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
NOW = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
NO_PROC = Path("/nonexistent-proc-root")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.cwd = self.cfg.home / "proj.v2"
        self.cwd.mkdir(parents=True)
        self.msgs = []
        self.events = []  # send-keys and sleep calls, in order
        self.panes = []
        self.procs = [ps_line(1, 0, "launchd", "launchd")]
        self.env = {}  # pid -> ps eww output
        self.lsof = {}  # pid -> rollout path
        self.cwds = {}  # pid → cwd
        self.on_send = None
        self.on_sleep = None
        self.screen = ""

    # ── scenarios ──
    def iterm_claude(self, pid, sid=A, profile="alice", cwd=None, write=True, days_ago=0, text="幫我修 sampan 的 bug"):
        cwd = str(cwd or self.cwd)
        self.procs += [ps_line(pid - 1, 1, "-zsh", "-zsh"), ps_line(pid, pid - 1, "claude", "claude", stat="S+")]
        if write:
            write_session(self.cfg, profile, pid, sid=sid, cwd=cwd, tmux="")
        if sid:
            self.transcript(sid, profile, days_ago, text)
        self.cwds[pid] = cwd

    def iterm_codex(self, pid, tid=TID, cwd=None):
        cwd = str(cwd or self.cwd)
        self.procs += [ps_line(pid - 1, 1, "-zsh", "-zsh"),
                       ps_line(pid, pid - 1, "node", "node /opt/homebrew/bin/codex", stat="S+"),
                       ps_line(pid + 1, pid, "codex", "/opt/homebrew/lib/codex/codex", stat="S+")]
        if tid:
            self.lsof[pid + 1] = self.rollout(tid, cwd)
        self.cwds[pid] = self.cwds[pid + 1] = cwd

    def transcript(self, sid, profile="alice", days_ago=0, text="hi"):
        d = self.cfg.profiles[profile].projects_dir / "-x"
        d.mkdir(parents=True, exist_ok=True)
        ts = (NOW - timedelta(minutes=3, days=days_ago)).isoformat().replace("+00:00", "Z")
        recs = [{"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}},
                {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "x"}]}},
                {"type": "user", "isMeta": True, "timestamp": ts, "message": {"role": "user", "content": "meta"}},
                {"type": "user", "timestamp": ts, "message": {"role": "user", "content": "<command-name>/clear"}}]
        (d / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))

    def rollout(self, tid, cwd):
        p = self.cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{tid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        recs = [{"timestamp": ts, "type": "session_meta", "payload": {"id": tid, "cwd": cwd}},
                {"timestamp": ts, "type": "event_msg", "payload": {"type": "user_message", "message": "整理 README"}}]
        p.write_text("".join(json.dumps(r) + "\n" for r in recs))
        return p

    def in_tmux_claude(self, session, pane_pid, pid, sid=B):
        self.panes.append(pane_line(session, f"%{pane_pid}", pane_pid, "claude", str(self.cwd)))
        self.procs += [ps_line(pane_pid, 1, "-zsh", "-zsh"), ps_line(pid, pane_pid, "claude", "claude")]
        write_session(self.cfg, "alice", pid, sid=sid, cwd=str(self.cwd), tmux=f"{session}:@1.%{pane_pid}")

    def registry(self, rows):
        write_rows(self.cfg.registry_path, rows, "t", self.cfg.history_dir)

    def gone(self, *pids):
        self.procs = [l for l in self.procs if int(l.split()[0]) not in pids]

    def runner(self):
        r = FakeRunner(strict=True)

        def send(a):
            self.events.append(("keys", a[4:]))
            if self.on_send:
                self.on_send(a[4:])
            return cp()

        def lsof(a):
            if "-d" in a:  # cwd
                pid = int(a[a.index("-p") + 1])
                return cp(out=f"p{pid}\nfcwd\nn{self.cwds[pid]}\n") if pid in self.cwds else cp(rc=1)
            pid = int(a[2])
            return cp(out=f"p{pid}\nfcwd\nn/x\nf10\nn{self.lsof[pid]}\n") if pid in self.lsof else cp(out=f"p{pid}\n")

        def eww(a):
            pid = int(a[-1])
            return cp(out=self.env[pid]) if pid in self.env else cp(rc=1)

        r.on(["tmux", "-u", "list-panes"], lambda a: cp(rc=1, err="error connecting to /tmp/x (Permission denied)")
             if getattr(self, "panes_fail", False) else cp(out="\n".join(self.panes) + "\n"))
        r.on(["tmux", "-u", "list-sessions"], cp(out="1-2\n"))
        r.on(["tmux", "-u", "has-session"], lambda a: cp(rc=0 if any(
            p.split("\t")[0] == a[-1][1:] for p in self.panes) else 1))
        r.on(["tmux", "-u", "new-session"], cp(out="%50\n"))
        r.on(["tmux", "-u", "send-keys"], send)
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=self.screen))
        r.on(["ps", "-A"], lambda a: cp(out="\n".join(self.procs) + "\n"))
        r.on(["ps", "eww"], eww)
        r.on(["ps", "-o", "etime="], cp(rc=1))
        r.on(["lsof"], lsof)
        return r

    def sleep(self, s):
        self.events.append(("sleep", s))
        if self.on_sleep:
            self.on_sleep()

    def adopt(self, r, answers=(), **kw):
        answers = list(answers)

        def ask(prompt):
            self.events.append(("ask", prompt))
            if not answers:
                return ""
            a = answers.pop(0)
            if isinstance(a, BaseException) or (isinstance(a, type) and issubclass(a, BaseException)):
                raise a
            return a
        kw.setdefault("picker_fn", lambda cands: [c.key for c in cands if c.selected])
        kw.setdefault("timeout", 0)
        return cmd_adopt(self.cfg, r, self.msgs.append, ask, sleep=self.sleep, now_fn=lambda: NOW,
                         proc_root=NO_PROC, own_pid=99999, **kw)

    def keys(self):
        return [e[1] for e in self.events if e[0] == "keys"]

    def text(self):
        return "\n".join(self.msgs)

    def found(self, **kw):
        return outside_agents(self.cfg, self.runner(), NOW, proc_root=NO_PROC, own_pid=99999, **kw)


TID2 = "01a1568b-81c2-71f1-99f7-b7a590f4ed97"
TID3 = "01a1568b-81c2-71f1-99f7-b7a590f4ed98"


class ResumedCodexTest(Base):
    """Codex 0.164 TUI attached to the shared daemon: no rollout open (lsof), and a resumed thread's threads row was
    created long before the process started. The daemon's loaded threads are the last way to tell (WSL report)."""

    def setUp(self):
        super().setUp()
        self.snaps = []

    def snap(self, *threads, loaded=None):
        from sampan.codex_daemon import CodexThread, DaemonSnapshot
        ts = [CodexThread(tid, "", cwd, "codex-tui", "vscode", None, "idle", (), updated, None)
              for tid, cwd, updated in threads]
        snap = DaemonSnapshot(self.cfg.codex.home, None, tuple(ts),
                              frozenset(loaded if loaded is not None else [t[0] for t in threads]), NOW)

        def fn(home, run, now):
            self.snaps.append(home)
            return snap
        return fn

    def runner(self):
        r = super().runner()
        r.on(["ps", "-o", "etime="], cp(out="10:00\n"))  # every process started 10 minutes ago
        return r

    def resumed(self, tid, cwd=None):
        self.iterm_codex(700, tid=None, cwd=cwd)
        self.rollout(tid, str(cwd or self.cwd))
        return NOW - timedelta(minutes=2)  # used after the process started

    def test_resumed_codex_is_found_through_the_daemon(self):
        used = self.resumed(TID)
        [c] = self.found(snapshot_fn=self.snap((TID, str(self.cwd), used),
                                               (TID2, str(self.cwd), NOW - timedelta(days=3))))  # stale, not this one
        self.assertEqual((c.session_id, c.skip), (TID, None))
        self.assertEqual(c.text, "整理 README")

    def test_thread_of_a_codex_in_tmux_is_not_taken(self):
        used = self.resumed(TID)
        self.panes.append(pane_line("cx", "%5", 500, "codex", str(self.cwd)))
        self.procs += [ps_line(500, 1, "-zsh", "-zsh"), ps_line(501, 500, "codex", "codex")]
        self.lsof[501] = self.rollout(TID2, str(self.cwd))
        [c] = self.found(snapshot_fn=self.snap((TID, str(self.cwd), used), (TID2, str(self.cwd), used)))
        self.assertEqual((c.session_id, c.skip), (TID, None))

    def test_two_candidates_are_not_guessed(self):
        used = self.resumed(TID)
        self.rollout(TID2, str(self.cwd))
        [c] = self.found(snapshot_fn=self.snap((TID, str(self.cwd), used), (TID2, str(self.cwd), used)))
        self.assertIsNone(c.session_id)
        self.assertIn("判斷不出", c.skip)

    def test_resumed_but_not_used_yet_says_send_a_message_first(self):
        self.resumed(TID)
        [c] = self.found(snapshot_fn=self.snap((TID, str(self.cwd), NOW - timedelta(days=3))))
        self.assertIsNone(c.session_id)
        self.assertIn("剛 resume", c.skip)
        self.assertIn("送一則訊息", c.skip)

    def test_no_daemon_call_when_lsof_found_it(self):
        self.iterm_codex(700)
        [c] = self.found(snapshot_fn=self.snap())
        self.assertEqual((c.session_id, self.snaps), (TID, []))

    def test_no_daemon_is_the_same_skip(self):
        self.resumed(TID)
        [c] = self.found(snapshot_fn=lambda home, run, now: None)
        self.assertIn("剛 resume", c.skip)

    def test_adopt_passes_the_daemon_reader_on(self):
        used = self.resumed(TID)
        self.adopt(self.runner(), dry_run=True, snapshot_fn=self.snap((TID, str(self.cwd), used)))
        self.assertIn("codex resume", self.text())
        self.assertIn(TID, self.text())


class ListTest(Base):
    def test_lists_only_top_level_agents_outside_tmux(self):
        self.iterm_claude(600)
        self.procs.append(ps_line(610, 600, "claude", "claude -p sub"))  # subagent: not a separate session
        self.in_tmux_claude("work", 100, 200)
        self.iterm_codex(700)
        cands = self.found()
        self.assertEqual([(c.agent, c.pid, c.pids, c.session_id, c.skip) for c in cands],
                         [("claude", 600, (600,), A, None), ("codex", 700, (700, 701), TID, None)])
        self.assertTrue(all(c.selected for c in cands))
        c = cands[0]
        self.assertEqual((c.profile, c.cwd, c.where, c.ago), ("alice", str(self.cwd), "~/proj.v2", "3 分鐘前"))
        self.assertEqual(c.text, "幫我修 sampan 的 bug")
        self.assertEqual(cands[1].text, "整理 README")

    def test_agents_without_a_terminal_are_not_listed(self):
        # e.g. a codex app-server started in the background by another program: no window for the user to /quit
        self.procs += [ps_line(44837, 1, "node", "node gateway"),
                       ps_line(45508, 44837, "codex", "codex app-server --listen stdio://", stat="S"),
                       ps_line(46000, 1, "claude", "claude -p x", stat="Ss")]
        self.iterm_claude(600)
        self.assertEqual([c.pid for c in self.found()], [600])

    def test_codex_child_of_claude_not_listed(self):
        self.iterm_claude(600)
        self.procs.append(ps_line(620, 600, "codex", "codex exec x"))
        self.assertEqual([c.pid for c in self.found()], [600])

    def test_list_screen_shows_agent_profile_cwd_age_and_message(self):
        self.iterm_claude(600)
        self.adopt(self.runner(), dry_run=True)
        self.assertRegex(self.text(), r"\[x\] +1\) claude +alice +~/proj\.v2 +3 分鐘前 +「幫我修 sampan 的 bug」")

    def test_unknown_id_or_profile_is_skipped_not_selectable(self):
        self.iterm_claude(600, sid=None, write=False)  # no session file, profile known
        self.env[600] = f"claude PATH=/bin HOME=/h CLAUDE_CONFIG_DIR={self.cfg.profiles['work'].config_dir}"
        self.iterm_claude(800, sid=None, write=False)
        self.env[800] = "claude PATH=/bin HOME=/h CLAUDE_CONFIG_DIR=/elsewhere/.claude"
        self.iterm_claude(900, sid=B, text="x")
        (self.cfg.profiles["alice"].projects_dir / "-x" / f"{B}.jsonl").unlink()  # no transcript
        self.iterm_codex(700, tid=None)
        skips = {c.pid: c.skip for c in self.found()}
        self.assertIn("判斷不出對話 id", skips[600])
        self.assertIn("不在設定檔", skips[800])
        self.assertIn("找不到逐字稿", skips[900])
        self.assertIn("判斷不出這個 Codex 在哪段對話", skips[700])
        self.assertFalse(any(c.selected for c in self.found()))

    def test_missing_cwd_is_skipped(self):
        self.iterm_claude(600, cwd="/no/such/dir")
        self.assertIn("讀不到工作目錄", self.found()[0].skip)

    def test_skipped_rows_are_never_launched(self):
        self.iterm_claude(800, sid=None, write=False)
        self.env[800] = "claude PATH=/bin HOME=/h CLAUDE_CONFIG_DIR=/elsewhere/.claude"
        r = self.runner()
        rc = self.adopt(r, picker_fn=lambda cands: [c.key for c in cands])
        self.assertEqual(rc, 1)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertIn("↷ 跳過", self.text())

    def test_agent_running_sampan_itself_is_skipped(self):
        self.iterm_claude(600)
        self.procs.append(ps_line(650, 600, "sampan", "python sampan adopt"))
        cands = outside_agents(self.cfg, self.runner(), NOW, proc_root=NO_PROC, own_pid=650)
        self.assertIn("自己", cands[0].skip)

    def test_nothing_outside(self):
        self.in_tmux_claude("work", 100, 200)
        self.assertEqual(self.adopt(self.runner()), 0)
        self.assertIn("沒有在 tmux 外執行的", self.text())


class NameTest(unittest.TestCase):
    def test_default_name_is_tmux_safe_and_unique(self):
        self.assertEqual(default_name("/Users/u/proj.v2", set()), "proj-v2")
        self.assertEqual(default_name("/Users/u/a:b", set()), "a-b")
        self.assertEqual(default_name("/Users/u/proj", {"proj", "proj-2"}), "proj-3")
        self.assertEqual(default_name("/", set()), "adopt")


class FlowTest(Base):
    def come_up(self, sid, pid=300):
        """After send-keys sends the resume command, claude shows up in pane %50 (so the resume can be confirmed)."""
        def on_send(keys):
            if len(keys) > 1 and "--resume" in keys[1]:
                self.panes.append(pane_line("proj-v2", "%50", 290, "claude", str(self.cwd)))
                self.procs += [ps_line(290, 1, "-zsh", "-zsh"), ps_line(pid, 290, "claude", "claude")]
                write_session(self.cfg, "alice", pid, sid=sid, cwd=str(self.cwd), tmux="proj-v2:@1.%50")
        self.on_send = on_send

    def exit_after(self, n, *pids):
        count = [0]

        def on_sleep():
            count[0] += 1
            if count[0] == n:
                self.gone(*pids)
        self.on_sleep = on_sleep

    def test_waits_until_gone_then_resumes_same_conversation_and_registers(self):
        self.iterm_claude(600)
        self.exit_after(3, 600)
        self.come_up(A)
        r = self.runner()
        rc = self.adopt(r, answers=[""], timeout=5)
        self.assertEqual(rc, 0)
        # nothing is created and no keys are sent before the outside process exits
        first_new = next(i for i, c in enumerate(r.calls) if c["args"][:3] == ["tmux", "-u", "new-session"])
        sleeps_before = sum(1 for e in self.events if e[0] == "sleep")
        self.assertGreaterEqual(sleeps_before, 3)
        self.assertTrue(all(c["args"][:3] != ["tmux", "-u", "send-keys"] for c in r.calls[:first_new]))
        self.assertEqual(r.called(["tmux", "-u", "new-session"])[0]["args"][-4:], ["-s", "proj-v2", "-c", str(self.cwd)])
        self.assertEqual(self.keys(), [["%50", f"env -u CLAUDE_CONFIG_DIR claude --resume {A}", "Enter"]])
        self.assertEqual([(x.tmux, x.agent, x.profile, x.session_id, x.cwd, x.state)
                          for x in load_rows(self.cfg.registry_path)],
                         [("proj-v2", "claude", "alice", A, str(self.cwd), "active")])
        t = self.text()
        self.assertIn("▶ 請到原本的終端機視窗（tmux 外）裡「~/proj.v2」那個 Claude 輸入 /exit", t)
        self.assertIn("  等待它結束…（按 Ctrl-C 取消，什麼都不會改）", t)
        self.assertLess(t.index("✔ 已結束"), t.index("✔ 開好 tmux session「proj-v2」，接回同一段對話（alice）"))

    def test_reports_opened_sessions(self):
        self.iterm_claude(600)
        self.exit_after(1, 600)
        self.come_up(A)
        opened = []
        self.adopt(self.runner(), answers=[""], timeout=5, opened=opened)
        self.assertEqual(opened, ["proj-v2"])

    def test_skipped_row_is_not_reported_opened(self):
        self.iterm_claude(600)
        self.exit_after(1, 600)
        opened = []
        r = self.runner()
        r.on(["tmux", "-u", "new-session"], cp(rc=1))
        self.adopt(r, answers=[""], opened=opened)
        self.assertEqual(opened, [])

    def test_never_signals_or_types_into_the_outside_process(self):
        self.iterm_claude(600)
        self.exit_after(1, 600)
        r = self.runner()
        self.adopt(r)
        self.assertEqual(r.called(["kill"]) + r.called(["osascript"]), [])
        self.assertTrue(all(k[0] == "%50" for k in self.keys()))

    def test_ctrl_c_while_waiting_changes_nothing(self):
        self.iterm_claude(600)

        def interrupt():
            raise KeyboardInterrupt
        self.on_sleep = interrupt
        r = self.runner()
        self.assertEqual(self.adopt(r), 130)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]) + r.called(["tmux", "-u", "send-keys"]), [])
        self.assertFalse(self.cfg.registry_path.exists())
        self.assertIn("已取消", self.text())

    def test_ctrl_c_on_second_row_keeps_first(self):
        self.iterm_claude(600)
        self.iterm_claude(610, sid=B, cwd=self.tmp)
        self.exit_after(1, 600)
        r = self.runner()
        self.assertEqual(self.adopt(r, answers=["first", KeyboardInterrupt]), 130)
        self.assertEqual([x.tmux for x in load_rows(self.cfg.registry_path)], ["first"])
        self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1)

    def test_ctrl_c_while_verifying_keeps_the_new_session(self):
        self.iterm_claude(600)
        self.iterm_claude(610, sid=B, cwd=self.tmp)
        self.exit_after(1, 600)
        r = self.runner()
        r.on(["tmux", "-u", "capture-pane"], lambda a: (_ for _ in ()).throw(KeyboardInterrupt))
        self.assertEqual(self.adopt(r, timeout=3), 130)
        self.assertEqual([x.tmux for x in load_rows(self.cfg.registry_path)], ["proj-v2"])
        self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1)
        self.assertIn("之後的都沒有動", self.text())

    def test_ps_failure_is_not_taken_as_exited(self):
        self.iterm_claude(600)
        r = self.runner()
        polls = [0]

        def ps(a):
            polls[0] += 1
            if polls[0] in (2, 3):
                return cp(rc=1)
            if polls[0] >= 4:
                self.gone(600)
            return cp(out="\n".join(self.procs) + "\n")
        r.on(["ps", "-A"], ps)
        self.adopt(r)
        self.assertGreaterEqual(polls[0], 4)
        self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1)

    def test_conversation_switched_in_the_window_resumes_the_new_one(self):
        self.iterm_claude(600)
        self.transcript(B)
        count = [0]

        def on_sleep():
            count[0] += 1
            if count[0] == 1:  # the user /resume'd another conversation in the original window
                write_session(self.cfg, "alice", 600, sid=B, cwd=str(self.cwd), tmux="")
            if count[0] == 2:
                self.gone(600)
        self.on_sleep = on_sleep
        self.adopt(self.runner())
        self.assertIn(f"claude --resume {B}", self.keys()[0][1])

    def test_name_is_asked_with_default_and_validated(self):
        self.iterm_claude(600)
        self.registry([Row("taken", "claude", "alice", B, "/c")])
        self.panes.append(pane_line("live", "%1", 100, "zsh", "/c"))
        self.procs.append(ps_line(100, 1, "-zsh", "-zsh"))
        self.exit_after(1, 600)
        r = self.runner()
        self.adopt(r, answers=["a.b", "taken", "live", "ok-name"])
        asks = [e[1] for e in self.events if e[0] == "ask"]
        self.assertEqual(len(asks), 4)
        self.assertIn("proj-v2", asks[0])
        self.assertEqual(r.called(["tmux", "-u", "new-session"])[0]["args"][-3], "ok-name")
        self.assertIn("不能含 . 或 :", self.text())

    def test_default_name_avoids_registry_and_tmux(self):
        self.iterm_claude(600)
        self.registry([Row("proj-v2", "claude", "alice", B, "/c")])
        self.exit_after(1, 600)
        r = self.runner()
        self.adopt(r, answers=[""])
        self.assertEqual(r.called(["tmux", "-u", "new-session"])[0]["args"][-3], "proj-v2-2")

    def test_dry_run_changes_nothing_and_does_not_wait(self):
        self.iterm_claude(600)
        self.iterm_codex(700)
        r = self.runner()
        self.assertEqual(self.adopt(r, dry_run=True), 0)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]) + r.called(["tmux", "-u", "send-keys"]), [])
        self.assertEqual([e for e in self.events if e[0] in ("sleep", "ask")], [])
        self.assertFalse(self.cfg.registry_path.exists())
        self.assertIn(f"[dry-run] ~/proj.v2：結束後建立 tmux session proj-v2 → env -u CLAUDE_CONFIG_DIR claude --resume {A}",
                      self.text())
        self.assertIn(f"codex resume {TID} -C {self.cwd}", self.text())
        self.assertIn("proj-v2-2", self.text())  # second row in the same directory gets a distinct name

    def test_codex_uses_quit_and_resume_with_cwd(self):
        self.iterm_codex(700)
        self.exit_after(2, 700, 701)
        r = self.runner()
        self.adopt(r)
        self.assertIn("▶ 請到原本的終端機視窗（tmux 外）裡「~/proj.v2」那個 Codex 輸入 /quit", self.text())
        self.assertEqual(self.keys(), [["%50", f"cd {self.cwd} && codex resume {TID} -C {self.cwd}"], ["%50", "Enter"]])
        rows = load_rows(self.cfg.registry_path)
        self.assertEqual([(x.agent, x.profile, x.session_id) for x in rows], [("codex", self.cfg.codex.profile, TID)])

    def test_codex_waits_for_both_wrapper_and_native(self):
        self.iterm_codex(700)
        count = [0]

        def on_sleep():
            count[0] += 1
            if count[0] == 1:
                self.gone(700)  # the node wrapper exits first, the native process is still running
            if count[0] == 3:
                self.gone(701)
        self.on_sleep = on_sleep
        r = self.runner()
        self.adopt(r)
        self.assertGreaterEqual(count[0], 3)
        self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1)

    def test_conversation_started_elsewhere_meanwhile_is_not_opened_twice(self):
        self.iterm_claude(600)

        def on_sleep():
            self.gone(600)
            self.in_tmux_claude("other", 100, 200, sid=A)  # the user resumed in tmux by themselves while waiting
        self.on_sleep = on_sleep
        r = self.runner()
        self.assertEqual(self.adopt(r), 1)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertIn("已在 other 執行", self.text())

    def test_tmux_unreadable_after_exit_skips_without_traceback_or_keys(self):
        self.iterm_claude(600)

        def on_sleep():
            self.gone(600)
            self.panes_fail = True  # tmux becomes unreadable while we waited
        self.on_sleep = on_sleep
        r = self.runner()
        self.assertEqual(self.adopt(r), 1)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertEqual(self.keys(), [])
        self.assertIn("讀不到 tmux", self.text())

    def test_unconfirmed_launch_is_reported_without_pressing(self):
        self.iterm_claude(600)
        self.exit_after(1, 600)
        self.screen = "Accessing workspace:\n\n /x\n\n Quick safety check\n ❯ 1. Yes, I trust this folder\n"
        self.assertEqual(self.adopt(self.runner(), timeout=3), 1)
        self.assertEqual(len(self.keys()), 1)
        self.assertIn("信任提示", self.text())

    def test_picker_cancel_does_nothing(self):
        self.iterm_claude(600)
        r = self.runner()
        self.assertEqual(self.adopt(r, picker_fn=lambda cands: None), 0)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertIn("已取消，什麼都沒做", self.text())

    def test_text_picker_used_without_picker_fn(self):
        self.iterm_claude(600)
        r = self.runner()
        self.assertEqual(self.adopt(r, answers=["q"], picker_fn=None), 0)
        self.assertIn("接手", next(e[1] for e in self.events if e[0] == "ask"))


if __name__ == "__main__":
    unittest.main()
