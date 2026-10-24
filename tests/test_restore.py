import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan.registry import Row, write_rows
from sampan.restore import Candidate, candidates, cmd_restore
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
CLAUDE_TRUST = "Accessing workspace:\n\n /Users/u/proj\n\n Quick safety check: Is this a project you created or one you trust?\n ❯ 1. Yes, I trust this folder\n   2. No, exit\n"


def dead(pid, sig):
    raise ProcessLookupError(pid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.cwd = self.tmp / "repo-proj"
        self.cwd.mkdir()
        self.msgs = []
        self.events = []  # send-keys and sleep calls, in order
        self.panes = []
        self.procs = []
        self.screen = ""
        self.server = True
        self.on_send = None

    def transcript(self, sid, days_ago=1, profile="alice"):
        d = self.cfg.profiles[profile].projects_dir / "-x"
        d.mkdir(parents=True, exist_ok=True)
        ts = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
        (d / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "timestamp": ts}) + "\n")

    def rollout(self, tid, days_ago=1):
        p = self.cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{tid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
        p.write_text(json.dumps({"timestamp": ts, "type": "session_meta"}) + "\n")
        return p

    def registry(self, rows):
        write_rows(self.cfg.registry_path, rows, "t", self.cfg.history_dir)

    def runner(self):
        r = FakeRunner(strict=True)

        def list_panes(_a):
            return cp(out="\n".join(self.panes) + "\n") if self.server else cp(rc=1, err="no server running")

        def send(a):
            self.events.append(("keys", a[4:]))
            if self.on_send:
                self.on_send(a[4:])
            return cp()

        made = []

        def new_session(a):
            self.server = True
            made.append(a)
            return cp(out=f"%{49 + len(made)}\n")

        r.on(["tmux", "-u", "list-panes"], list_panes)
        r.on(["tmux", "-u", "list-sessions"], lambda a: cp() if self.server else cp(rc=1))
        r.on(["tmux", "-u", "new-session"], new_session)
        r.on(["tmux", "-u", "send-keys"], send)
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=self.screen))
        r.on(["ps", "-A"], lambda a: cp(out="\n".join(self.procs) + "\n"))
        r.on(["ps", "eww"], cp())  # no environment readable
        return r

    def sleep(self, s):
        self.events.append(("sleep", s))

    def restore(self, r, answers=("",), **kw):
        answers = list(answers)
        kw.setdefault("timeout", 0)
        kw.setdefault("alive", dead)
        return cmd_restore(self.cfg, r, self.msgs.append, lambda _p: answers.pop(0) if answers else None,
                           sleep=self.sleep, now_fn=lambda: NOW, **kw)

    def keys(self):
        return [e[1] for e in self.events if e[0] == "keys"]

    def sleeps(self):
        return [e[1] for e in self.events if e[0] == "sleep"]

    def text(self):
        return "\n".join(self.msgs)


class CandidatesTest(Base):
    def test_default_selection_is_recent_active_only(self):
        self.transcript(A, days_ago=3)
        self.transcript(B, days_ago=10)
        self.transcript(C, days_ago=1)
        self.registry([Row("recent", "claude", "alice", A, str(self.cwd)),
                       Row("stale", "claude", "alice", B, str(self.cwd)),
                       Row("old", "claude", "alice", C, str(self.cwd), "retired")])
        cands = candidates(self.cfg, self.runner(), _rows(self.cfg), NOW)
        self.assertEqual([(c.row.tmux, c.selected, c.skip) for c in cands],
                         [("recent", True, None), ("stale", False, None)])
        self.assertIsInstance(cands[0], Candidate)
        self.assertEqual(cands[0].last, NOW - timedelta(days=3))

    def test_session_with_running_agent_not_listed(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        self.panes = [pane_line("x", "%7", 100, "claude", str(self.cwd))]
        self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(200, 100, "claude", "claude")]
        self.assertEqual(candidates(self.cfg, self.runner(), _rows(self.cfg), NOW), [])

    def test_skip_reasons(self):
        self.transcript(A)
        self.rollout(TID)
        self.registry([Row("nodir", "claude", "alice", A, "/no/such/dir"),
                       Row("notx", "claude", "alice", B, str(self.cwd)),
                       Row("noprof", "claude", "gone", A, str(self.cwd)),
                       Row("cx-noid", "codex", "", "", str(self.cwd)),
                       Row("cx-badid", "codex", "", "*", str(self.cwd)),
                       Row("cl-noid", "claude", "alice", "", str(self.cwd)),
                       Row("cl-badid", "claude", "alice", "*", str(self.cwd)),
                       Row("wrongprof", "claude", "work", A, str(self.cwd)),
                       Row("cx-norollout", "codex", "", C, str(self.cwd)),
                       Row("cx-ok", "codex", "", TID, str(self.cwd))])
        skips = {c.row.tmux: c.skip for c in candidates(self.cfg, self.runner(), _rows(self.cfg), NOW)}
        self.assertIn("目錄不存在", skips["nodir"])
        self.assertIn("找不到逐字稿", skips["notx"])
        self.assertIn("profile gone", skips["noprof"])
        for name in ("cx-noid", "cl-noid"):
            self.assertIn("沒有對話 id", skips[name])
        for name in ("cx-badid", "cl-badid"):
            self.assertIn("不是 UUID", skips[name])
        self.assertIn("逐字稿在 profile alice，不是 work", skips["wrongprof"])
        self.assertIn("找不到 rollout", skips["cx-norollout"])
        self.assertIsNone(skips["cx-ok"])


def _rows(cfg):
    from sampan.registry import load_rows
    return load_rows(cfg.registry_path)


class RestoreTest(Base):
    def test_missing_transcript_skipped_not_new_conversation(self):
        self.transcript(A)
        self.rollout(TID)
        self.registry([Row("has", "claude", "alice", A, str(self.cwd)),
                       Row("lost", "claude", "alice", B, str(self.cwd)),
                       Row("cx-lost", "codex", "", C, str(self.cwd)),
                       Row("cx-has", "codex", "", TID, str(self.cwd))])
        self.restore(self.runner(), select_all=True)
        lines = [k[1] for k in self.keys() if len(k) > 1 and k[1] not in ("Enter", "C-u")]
        self.assertEqual(len(lines), 2)
        for line in lines:
            if "claude" in line:
                self.assertIn(f"--resume {A}", line)
            else:
                self.assertIn(f"codex resume {TID}", line)
        self.assertRegex(self.text(), r"lost.*找不到逐字稿")
        self.assertRegex(self.text(), r"cx-lost.*找不到 rollout")

    def test_already_running_elsewhere_skipped(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        self.panes = [pane_line("other", "%9", 100, "claude", "/c")]
        self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(200, 100, "claude", "claude")]
        write_session(self.cfg, "alice", 200, sid=A, tmux="other:@1.%9")
        r = self.runner()
        self.restore(r, select_all=True)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertEqual(self.keys(), [])
        self.assertRegex(self.text(), r"x.*已在 other 執行")

    def test_old_pid_still_alive_skipped(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd), pid=4242)])
        probed = []
        r = self.runner()
        self.restore(r, select_all=True, alive=lambda pid, sig: probed.append((pid, sig)))
        self.assertIn((4242, 0), probed)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertEqual(self.keys(), [])
        self.assertIn("4242", self.text())

    def test_old_pid_seen_as_agent_in_ps_skipped(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd), pid=4242)])
        self.procs = [ps_line(1, 0, "launchd", "launchd"), ps_line(4242, 1, "claude", "claude")]
        r = self.runner()
        self.restore(r, select_all=True, alive=lambda pid, sig: None)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertIn("pid 4242", self.text())

    def test_reused_pid_of_unrelated_process_does_not_block(self):
        # pid reused by another process (common after a reboot): os.kill says it is alive, but in ps it is not an agent
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd), pid=4242)])
        self.procs = [ps_line(1, 0, "launchd", "launchd"), ps_line(4242, 1, "zsh", "-zsh")]
        r = self.runner()
        self.restore(r, select_all=True, alive=lambda pid, sig: None)
        self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1)

    def test_rows_sharing_an_id_launch_once(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd)), Row("y", "claude", "alice", A, str(self.cwd))])
        r = self.runner()
        self.restore(r, select_all=True)
        self.assertEqual([c["args"][-3] for c in r.called(["tmux", "-u", "new-session"])], ["x"])
        self.assertEqual(len(self.keys()), 1)
        self.assertRegex(self.text(), r"y.*本次已在 x 接回")

    def test_duplicate_picker_names_launch_once(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        r = self.runner()
        self.restore(r, picker_fn=lambda cands: ["x", "x"])
        self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1)
        self.assertEqual(len(self.keys()), 1)
        self.assertEqual(self.sleeps(), [])

    def test_interval_comes_before_the_fresh_check(self):
        self.transcript(A)
        self.transcript(B)
        self.registry([Row("a", "claude", "alice", A, str(self.cwd)), Row("b", "claude", "alice", B, str(self.cwd))])
        r = self.runner()
        orig = self.sleep

        def sleep_then_b_starts_elsewhere(s):
            orig(s)
            if s == 3:  # the user opened b's conversation elsewhere while waiting
                self.panes = [pane_line("other", "%9", 100, "claude", "/c")]
                self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(200, 100, "claude", "claude")]
                write_session(self.cfg, "alice", 200, sid=B, tmux="other:@1.%9")
        self.sleep = sleep_then_b_starts_elsewhere
        self.restore(r)
        self.assertEqual([c["args"][-3] for c in r.called(["tmux", "-u", "new-session"])], ["a"])
        self.assertRegex(self.text(), r"b.*已在 other 執行")

    def test_old_pid_of_another_user_is_not_ours(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd), pid=4242)])

        def other_user(pid, sig):
            raise PermissionError(pid)
        r = self.runner()
        self.restore(r, select_all=True, alive=other_user)
        self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1)

    def test_restore_without_tmux_server_creates_sessions(self):
        # Review Focus 5: tmux server isn't up yet after a reboot
        self.server = False
        self.transcript(A)
        self.transcript(B, profile="work")
        self.registry([Row("003-帳務", "claude", "work", B, str(self.cwd)), Row("a", "claude", "alice", A, str(self.cwd))])
        r = self.runner()
        self.restore(r)
        made = [c["args"][-4:] for c in r.called(["tmux", "-u", "new-session"])]
        self.assertEqual(made, [["-s", "003-帳務", "-c", str(self.cwd)], ["-s", "a", "-c", str(self.cwd)]])
        self.assertEqual(self.keys()[0],
                         ["%50", f"env CLAUDE_CONFIG_DIR={self.cfg.profiles['work'].config_dir} claude --resume {B}", "Enter"])

    def test_reuses_shell_pane_of_existing_session(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        self.panes = [pane_line("x", "%7", 100, "zsh", "/Users/u")]
        self.procs = [ps_line(100, 1, "-zsh", "-zsh")]
        r = self.runner()
        self.restore(r)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertEqual(self.keys(), [["%7", "C-u"], ["%7", f"cd {self.cwd} && env -u CLAUDE_CONFIG_DIR claude --resume {A}", "Enter"]])

    def test_session_without_shell_pane_skipped(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        self.panes = [pane_line("x", "%7", 100, "vim", "/c")]
        self.procs = [ps_line(100, 1, "vim", "vim")]
        r = self.runner()
        self.assertEqual(self.restore(r, select_all=True), 1)
        self.assertEqual(self.keys(), [])
        self.assertIn("沒有 shell pane", self.text())

    def test_codex_enter_sent_separately(self):
        self.rollout(TID)
        self.registry([Row("cx", "codex", "", TID, str(self.cwd))])
        self.restore(self.runner())
        self.assertEqual(self.events[:3], [("keys", ["%50", f"codex resume {TID}"]), ("sleep", 1), ("keys", ["%50", "Enter"])])

    def test_confirmed_resume_is_reported(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])

        def started(keys):
            if len(keys) > 1 and "claude --resume" in keys[1]:
                self.panes = [pane_line("x", "%50", 100, "claude", str(self.cwd))]
                self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(200, 100, "claude", f"claude --resume {A}")]
                write_session(self.cfg, "alice", 200, sid=A, cwd=str(self.cwd), tmux="x:@1.%50")
        self.on_send = started
        self.assertEqual(self.restore(self.runner(), timeout=10), 0)
        self.assertIn("接回 1", self.text())
        self.assertNotIn(("sleep", 1), self.events)  # seen on the first check

    def test_trust_prompt_not_pressed(self):
        from tests.test_screens import CLAUDE_TRUST as CL_NOW
        codex = (FIXTURES / "codex_trust.txt").read_text()
        # the older footer-less Claude screen and the current one (2.1.287); each agent's own screen
        for claude in (CLAUDE_TRUST, CL_NOW.format(path="/Users/u/proj")):
            with self.subTest(screen=claude[:20]):
                self.setUp()
                self.two_rows()
                self.assertEqual(self.restore(self.per_pane({"%50": claude, "%51": codex}), timeout=10), 1)
                sent = [k[1:] for k in self.keys()]
                self.assertEqual(sent, [[f"env -u CLAUDE_CONFIG_DIR claude --resume {A}", "Enter"],
                                        [f"codex resume {TID}"], ["Enter"]])
                self.assertIn("停在信任提示 2", self.text())
                self.assertIn("請到視窗確認", self.text())

    def test_trust_text_in_scrollback_is_not_a_prompt_without_relay(self):
        from tests.test_screens import COMPOSER
        self.two_rows()
        stale = {"%50": CLAUDE_TRUST + COMPOSER, "%51": (FIXTURES / "codex_trust.txt").read_text() + COMPOSER}
        self.assertEqual(self.restore(self.per_pane(stale), timeout=2), 1)
        self.assertIn("停在信任提示 0", self.text())
        self.assertIn("未確認 2", self.text())

    def test_other_menu_is_not_called_a_trust_prompt(self):
        from tests.test_prompts import CODEX_UPDATE
        self.two_rows()
        self.assertEqual(self.restore(self.per_pane({"%50": "", "%51": CODEX_UPDATE}), timeout=2), 1)
        self.assertIn("停在信任提示 0", self.text())

    def test_not_live_in_time_reported_without_pressing(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        self.screen = "Update available! Press enter to continue\n"
        self.assertEqual(self.restore(self.runner(), timeout=10), 1)
        self.assertEqual(len(self.keys()), 1)
        self.assertEqual(self.sleeps(), [1] * 10)
        self.assertIn("停在提示或尚未接回，請到視窗確認", self.text())

    def two_rows(self):
        self.transcript(A)
        self.rollout(TID)
        self.registry([Row("cl", "claude", "alice", A, str(self.cwd)), Row("cx", "codex", "", TID, str(self.cwd))])

    def per_pane(self, screens):
        """capture-pane answers per pane id (%50 = cl, %51 = cx)."""
        r = self.runner()
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=screens.get(a[a.index("-t") + 1], "")))
        return r

    def test_prompts_are_relayed_and_skipped_ones_listed(self):
        from sampan.prompts import SKIP
        from tests.test_prompts import CLAUDE_TRUST as CL
        from tests.test_prompts import CODEX_UPDATE
        self.two_rows()
        screens = {"%50": CL, "%51": CODEX_UPDATE}

        def on_send(keys):
            if keys == ["%51", "2"]:
                screens["%51"] = ""
        self.on_send = on_send
        asked = []
        rc = self.restore(self.per_pane(screens), timeout=3,
                          on_prompt=lambda n, a, p: asked.append(n) or (SKIP if n == "cl" else 1))
        self.assertEqual(rc, 1)
        self.assertEqual(asked, ["cl", "cx"])  # cl was set to "ignore for now": not asked again
        self.assertEqual(self.keys()[-1], ["%51", "2"])
        self.assertIn("停在提示 1", self.text())
        self.assertIn("  ⚠ cl 停在提示，進去處理", self.msgs)
        self.assertIn("  ⚠ cx：停在提示或尚未接回，請到視窗確認", self.msgs)  # answered but not resumed

    def test_attach_stops_watching_the_others(self):
        from sampan.prompts import ATTACH
        from tests.test_prompts import CLAUDE_TRUST as CL
        from tests.test_prompts import CODEX_UPDATE
        self.two_rows()
        asked = []
        rc = self.restore(self.per_pane({"%50": CL, "%51": CODEX_UPDATE}), timeout=3,
                          on_prompt=lambda n, a, p: asked.append(n) or ATTACH)
        self.assertEqual(rc, 1)
        self.assertEqual(asked, ["cl"])
        self.assertEqual(len(self.keys()), 3)  # only the two launch commands (codex's Enter is sent separately)
        self.assertIn("進去 cl；其他 session 已送出接回指令，不再等待", self.text())
        self.assertIn("  ⚠ cl 停在提示，進去處理", self.msgs)

    def test_trust_text_in_scrollback_is_not_a_prompt_with_relay(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        self.screen = CLAUDE_TRUST + "\n> hi\n\n  ? for shortcuts\n"
        asked = []
        self.restore(self.runner(), timeout=2, on_prompt=lambda *a: asked.append(a) or 0)
        self.assertEqual(asked, [])
        self.assertNotIn("停在信任提示 1", self.text())

    def test_cancel_does_nothing(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        r = self.runner()
        self.assertEqual(self.restore(r, answers=["q"]), 0)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]) + r.called(["tmux", "-u", "send-keys"]), [])
        self.assertIn("已取消", self.text())
        r = self.runner()
        self.restore(r, picker_fn=lambda cands: None)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]) + r.called(["tmux", "-u", "send-keys"]), [])

    def test_y_or_yes_accepts_the_defaults(self):
        for answer in ("y", "YES", " Yes "):
            self.transcript(A)
            self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
            self.events.clear()
            r = self.runner()
            self.server = False
            self.restore(r, answers=[answer])
            self.assertEqual(len(r.called(["tmux", "-u", "new-session"])), 1, answer)

    def test_eof_and_ctrl_c_at_prompt_cancel(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        for exc in (EOFError, KeyboardInterrupt):
            r = self.runner()

            def boom(_p, exc=exc):
                raise exc
            rc = cmd_restore(self.cfg, r, self.msgs.append, boom, sleep=self.sleep, now_fn=lambda: NOW,
                             timeout=0, alive=dead)
            self.assertEqual(rc, 0)
            self.assertEqual(r.called(["tmux", "-u", "new-session"]) + r.called(["tmux", "-u", "send-keys"]), [])
        self.assertIn("已取消", self.text())

    def test_numbers_change_selection(self):
        self.transcript(A, days_ago=1)
        self.transcript(B, days_ago=30)
        self.registry([Row("a", "claude", "alice", A, str(self.cwd)), Row("b", "claude", "alice", B, str(self.cwd))])
        r = self.runner()
        self.restore(r, answers=["2"])
        self.assertEqual([c["args"][-3] for c in r.called(["tmux", "-u", "new-session"])], ["b"])

    def test_picker_result_is_used(self):
        self.transcript(A)
        self.transcript(B)
        self.registry([Row("a", "claude", "alice", A, str(self.cwd)), Row("b", "claude", "alice", B, str(self.cwd))])
        seen = []
        r = self.runner()
        self.restore(r, picker_fn=lambda cands: seen.extend(c.row.tmux for c in cands) or ["b"])
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual([c["args"][-3] for c in r.called(["tmux", "-u", "new-session"])], ["b"])

    def test_no_confirm_skips_question_and_reports_opened(self):
        self.transcript(B)
        self.registry([Row("b", "claude", "alice", B, str(self.cwd))])
        asked, opened = [], []
        r = self.runner()
        cmd_restore(self.cfg, r, self.msgs.append, lambda p: asked.append(p) or "", names=("b",), confirm=False,
                    opened=opened, sleep=self.sleep, now_fn=lambda: NOW, timeout=0, alive=dead)
        self.assertEqual(asked, [])
        self.assertEqual(opened, ["b"])

    def test_no_wait_sends_resume_and_returns_without_watching(self):
        self.transcript(B)
        self.registry([Row("b", "claude", "alice", B, str(self.cwd))])
        opened, slept = [], []
        r = self.runner()
        rc = cmd_restore(self.cfg, r, self.msgs.append, lambda p: "", names=("b",), confirm=False, opened=opened,
                         sleep=slept.append, now_fn=lambda: NOW, timeout=10, alive=dead, wait=False)
        self.assertEqual(rc, 0)
        self.assertEqual(opened, ["b"])
        self.assertEqual(r.called(["tmux", "-u", "capture-pane"]), [])
        self.assertTrue(any("已送出接回指令 1" in m for m in self.msgs))

    def test_names_limit_rows_but_still_confirm(self):
        self.transcript(A)
        self.transcript(B, days_ago=30)
        self.registry([Row("a", "claude", "alice", A, str(self.cwd)), Row("b", "claude", "alice", B, str(self.cwd))])
        asked = []
        r = self.runner()
        cmd_restore(self.cfg, r, self.msgs.append, lambda p: asked.append(p) or "", names=("b", "nope"),
                    sleep=self.sleep, now_fn=lambda: NOW, timeout=0, alive=dead)
        self.assertEqual(len(asked), 1)
        self.assertEqual([c["args"][-3] for c in r.called(["tmux", "-u", "new-session"])], ["b"])
        self.assertIn("登記表沒有 nope", self.text())

    def test_interval_between_launches(self):
        for sid in (A, B, C):
            self.transcript(sid)
        self.registry([Row(n, "claude", "alice", sid, str(self.cwd)) for n, sid in (("a", A), ("b", B), ("c", C))])
        self.restore(self.runner())
        self.assertEqual(self.sleeps(), [3, 3])

    def test_dry_run_sends_nothing(self):
        self.transcript(A)
        self.registry([Row("x", "claude", "alice", A, str(self.cwd))])
        r = self.runner()
        self.assertEqual(self.restore(r, dry_run=True), 0)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]) + r.called(["tmux", "-u", "send-keys"]), [])
        self.assertIn("[dry-run]", self.text())

    def test_does_not_ask_when_nothing_can_be_restored(self):
        self.registry([Row("lost", "claude", "alice", B, str(self.cwd))])
        asked = []
        r = self.runner()
        rc = cmd_restore(self.cfg, r, self.msgs.append, lambda p: asked.append(p) or "",
                         sleep=self.sleep, now_fn=lambda: NOW, timeout=0, alive=dead)
        self.assertEqual((rc, asked, self.keys()), (1, [], []))
        self.assertRegex(self.text(), r"跳過 1")

    def test_nothing_to_restore(self):
        self.registry([])
        self.assertEqual(self.restore(self.runner()), 0)
        self.assertIn("沒有要接回", self.text())

    def test_restore_named_retired_row_says_activate_first(self):
        self.registry([Row("old", "claude", "alice", B, str(self.cwd), "retired")])
        r = self.runner()
        self.assertEqual(self.restore(r, names=["old"]), 0)
        self.assertIn("old 是 retired；先 sampan activate old", self.msgs)
        self.assertNotIn("沒有要接回", self.text())
        self.assertEqual(self.keys(), [])
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])

    def test_restore_names_nothing_keeps_old_message_with_retired_rows(self):
        self.registry([Row("old", "claude", "alice", B, str(self.cwd), "retired")])
        self.assertEqual(self.restore(self.runner()), 0)
        self.assertIn("沒有要接回", self.text())

    def test_corrupt_registry(self):
        self.cfg.registry_path.parent.mkdir(parents=True)
        self.cfg.registry_path.write_text("{")
        self.assertEqual(self.restore(self.runner()), 2)


class IsolationTest(unittest.TestCase):
    def test_save_module_does_not_import_restore(self):
        code = "import sys, sampan.save, sampan.cli; print('sampan.restore' in sys.modules)"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        self.assertEqual(r.stdout.strip(), "False", r.stderr)


class CliTest(unittest.TestCase):
    def test_start_is_renamed(self):
        from sampan.cli import main
        tmp = Path(tempfile.mkdtemp())
        make_env(tmp)
        msgs = []
        env = {"SAMPAN_CONFIG": str(tmp / "config.toml"), "SAMPAN_STATE_DIR": str(tmp / "state")}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            from unittest import mock
            with mock.patch("sampan.cli.out", msgs.append):
                self.assertEqual(main(["start", "--profile", "x"]), 2)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertIn("已改名為 sampan restore", "\n".join(msgs))


if __name__ == "__main__":
    unittest.main()
