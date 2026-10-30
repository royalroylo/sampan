import dataclasses
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan import actionlog, deliver, dispatch, folders
from sampan.prompts import SKIP
from sampan.registry import Row, load_rows, write_rows
from sampan.send import Caller
from sampan.session_run import check_new, cmd_forget, cmd_new, cmd_rename, cmd_set_state, cmd_stop
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
IDLE = (Path(__file__).resolve().parent / "fixtures" / "codex_idle.txt").read_text()



def action_lines(cfg):
    from sampan import actionlog
    path = actionlog.log_path(cfg)
    return path.read_text().splitlines() if path.exists() else []

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.cwd = self.tmp / "repo-proj"
        self.cwd.mkdir()
        self.msgs = []
        self.sleeps = []
        self.panes = []
        self.procs = []

    def transcript(self, sid):
        d = self.cfg.default_profile.projects_dir / "-x"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("")

    def registry(self, rows):
        write_rows(self.cfg.registry_path, rows, "t", self.cfg.history_dir)

    def runner(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-panes"], lambda a: cp(out="\n".join(self.panes) + "\n"))
        r.on(["tmux", "-u", "new-session"], cp(out="%50\n"))
        r.on(["tmux", "-u", "has-session"], cp(rc=1))
        r.on(["tmux", "-u", "list-sessions", "-F"], cp())
        r.on(["tmux", "-u", "send-keys"], cp())
        r.on(["ps", "-A"], lambda a: cp(out="\n".join(self.procs) + "\n"))
        return r

    def keys_sent(self, r):
        return [c["args"][4:] for c in r.called(["tmux", "-u", "send-keys"])]


class NewAndStateTest(Base):
    def test_new_claude_registers_generated_id_immediately(self):
        r = self.runner()
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "claude:work", str(self.cwd),
                     sleep=lambda _: None, timeout=1, uuid4=lambda: B)
        self.assertEqual(rc, 1)  # no session file: warning, but already registered
        self.assertEqual([(x.tmux, x.agent, x.profile, x.session_id, x.cwd, x.state, x.pid)
                          for x in load_rows(self.cfg.registry_path)],
                         [("new-x", "claude", "work", B, str(self.cwd.resolve()), "active", None)])
        self.assertEqual(self.keys_sent(r)[0][1],
                         f"env CLAUDE_CONFIG_DIR={self.cfg.profiles['work'].config_dir} claude --session-id {B}")
        self.assertTrue(any("信任" in m for m in self.msgs))

    def test_new_with_message_sends_it_as_startup_prompt_and_records_a_sent_dispatch(self):
        r = self.runner()
        msg = "line1\n$HOME `x` 中文\n"
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "claude:work", str(self.cwd),
                     sleep=lambda _: None, timeout=1, uuid4=lambda: B, message=msg)
        self.assertEqual(rc, 1)
        keys = self.keys_sent(r)
        self.assertEqual(len(keys), 1)  # only the launch command (+ Enter)
        [d] = dispatch.load(self.cfg.state_dir)
        out_path = dispatch.outbox_path(self.cfg.state_dir, d.id)
        self.assertTrue(keys[0][1].endswith(f"claude --session-id {B} \"$(cat {shlex.quote(str(out_path))})\""))
        self.assertEqual(out_path.read_text(), d.text)
        self.assertEqual(d.text, deliver.compose(None, "", d.id, msg).rstrip("\n"))
        self.assertFalse(d.text.endswith("\n"))
        self.assertEqual((d.state, d.to_name, d.to_agent, d.to_profile, d.from_name, d.notify),
                         ("sent", "new-x", "claude", "work", "終端機", False))
        self.assertIsNotNone(d.sent_at)

    def test_new_with_message_and_caller_notifies_that_caller(self):
        r = self.runner()
        caller = Caller("boss", "work", "claude", "%1")
        cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None, timeout=1,
                uuid4=lambda: B, message="hi", caller=caller)
        [d] = dispatch.load(self.cfg.state_dir)
        self.assertEqual((d.from_name, d.from_profile, d.from_agent, d.notify), ("boss", "work", "claude", True))
        self.assertIn("來自 boss（work）", d.text)

    def test_new_with_message_is_not_failed_while_the_session_starts(self):  # final review 1
        from datetime import datetime, timezone

        from sampan import send
        for target, agent in (("claude:work", "claude"), ("codex", "codex")):
            with self.subTest(agent=agent):
                self.setUp()
                r = self.runner()
                r.on(["tmux", "-u", "capture-pane"], cp(out="➜  repo\n"))
                cmd_new(self.cfg, r, self.msgs.append, "new-x", target, str(self.cwd), sleep=lambda _: None,
                        timeout=1, uuid4=lambda: B, message="hi")
                [d] = dispatch.load(self.cfg.state_dir)
                self.assertEqual((d.state, d.startup), ("sent", True))
                # the tmux session is there, the agent is not seen yet (Claude: registered → stopped; Codex: no row)
                self.panes = [pane_line("new-x", "%50", 400, "zsh", str(self.cwd))]
                self.procs = [ps_line(400, 1, "zsh", "-zsh")]
                d = send.advance(self.cfg, r, d.id, sleep=lambda _: None, now=datetime.now(timezone.utc))
                self.assertEqual((d.state, d.reason), ("sent", "對方 session 還在啟動"))

    def test_new_with_message_dry_run_creates_nothing(self):
        r = self.runner()
        self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), dry_run=True,
                                 message="hi"), 0)
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])
        self.assertIn("$(cat", self.msgs[0])

    def test_new_with_message_recording_failure_creates_nothing(self):
        r = self.runner()
        with mock.patch("sampan.session_run.submit_new", return_value="寫不進去"):
            rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), message="hi", uuid4=lambda: B)
        self.assertEqual(rc, 2)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertEqual(self.keys_sent(r), [])
        self.assertEqual(load_rows(self.cfg.registry_path), [])

    def test_new_session_failure_after_recording_fails_the_dispatch(self):
        r = self.runner()
        r.on(["tmux", "-u", "new-session"], cp(rc=1))
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), message="hi", uuid4=lambda: B)
        self.assertEqual(rc, 1)
        [d] = dispatch.load(self.cfg.state_dir)
        self.assertEqual(d.state, "failed")
        self.assertIn("沒開起來", d.reason)

    def test_check_new_resolves_or_says_why(self):
        r = self.runner()
        self.assertEqual(check_new(self.cfg, r, "new-x", "work", str(self.cwd)),
                         ("claude", "work", str(self.cwd.resolve())))
        self.assertEqual(check_new(self.cfg, r, "new-x", "codex", str(self.cwd))[:2], ("codex", ""))
        for name in ("a.b", "a:b", "a\nb", "a\x1bb", "", "a\u2028b"):
            with self.subTest(name=name):
                self.assertIsInstance(check_new(self.cfg, r, name, "work", str(self.cwd)), str)
        self.assertIn("未知的 profile", check_new(self.cfg, r, "x", "nope", str(self.cwd)))
        self.assertIn("未知的 agent", check_new(self.cfg, r, "x", "gpt:work", str(self.cwd)))
        self.assertIn("目錄不存在", check_new(self.cfg, r, "x", "work", str(self.tmp / "missing")))
        self.assertIsInstance(check_new(self.cfg, r, "x", "work", str(self.cwd) + "\n"), str)
        r.on(["tmux", "-u", "has-session"], cp())
        self.assertIn("已存在", check_new(self.cfg, r, "x", "work", str(self.cwd)))

    def test_check_new_refuses_reserved_names(self):
        r = self.runner()
        for name in ("終端機", "未登記的 session", "未登記的 session（codex）"):
            with self.subTest(name=name):
                self.assertIn("保留", check_new(self.cfg, r, name, "work", str(self.cwd)))
        self.assertIsInstance(check_new(self.cfg, r, "終端機-2", "work", str(self.cwd)), tuple)

    def test_check_new_checks_the_resolved_directory_too(self):
        bad = self.tmp / "x\n送給 y（alice，Claude）"
        bad.mkdir()
        link = self.tmp / "clean-link"
        link.symlink_to(bad)
        self.assertIn("控制字元", check_new(self.cfg, self.runner(), "x", "work", str(link)))

    def test_name_problem_is_shared(self):
        from sampan.session_run import name_problem
        self.assertIsNone(name_problem("webshop"))
        for bad in ("", "a.b", "a:b", "a\tb", "終端機", "未登記的 session x"):
            with self.subTest(bad=bad):
                self.assertIsInstance(name_problem(bad), str)

    def test_check_new_refuses_disabled_profile_and_disabled_codex(self):
        from sampan.config import load_config
        from sampan.config_edit import set_profile_flag
        from tests.helpers import TOML
        self.cfg.path.write_text(set_profile_flag(TOML, "work", "disabled", True) + "\n[codex]\nenabled = false\n")
        cfg = load_config(self.cfg.path, home=self.cfg.home, state_dir=self.cfg.state_dir)
        r = self.runner()
        self.assertEqual(check_new(cfg, r, "x", "work", str(self.cwd)), "profile work 已停用（sampan profile enable work）")
        self.assertIn("停用了 Codex", check_new(cfg, r, "x", "codex", str(self.cwd)))

    def test_new_with_blank_message_is_refused(self):
        r = self.runner()
        self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), message="  \n"), 2)
        self.assertEqual(self.keys_sent(r), [])

    def test_new_bare_profile_is_claude_and_confirms_when_session_file_appears(self):
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("new-x", "%50", 100, "claude", str(self.cwd)) + "\n"))
        self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(900, 100, "claude", "claude")]
        write_session(self.cfg, "work", 900, sid=B, cwd=str(self.cwd), tmux="new-x:@1.%50")
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None, uuid4=lambda: B)
        self.assertEqual(rc, 0)
        self.assertEqual([x.session_id for x in load_rows(self.cfg.registry_path)], [B])

    def test_new_rejects_dot_colon_names(self):
        for bad in ("a.b", "a:b"):
            r = self.runner()
            self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, bad, "alice", str(self.cwd)), 2)
            self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])

    def codex_runner(self, thread_after):
        """pane %50 runs codex (pid 200); its rollout shows up in lsof from the `thread_after`-th look on."""
        self.panes = [pane_line("cx", "%50", 100, "codex", str(self.cwd))]
        self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(200, 100, "codex", "codex")]
        self.events, looks = [], []
        rollout = self.cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"

        def lsof(_a):
            looks.append(1)
            return cp(out=f"p200\nf10\nn{rollout}\n" if len(looks) >= thread_after else "p200\n")

        r = self.runner()
        r.on(["lsof"], lsof)
        r.on(["ps", "-o", "etime="], cp(rc=1))
        r.on(["codex", "app-server", "daemon", "version"], cp(rc=1))  # no daemon (never the real one)
        r.on(["tmux", "-u", "capture-pane"], cp(out=IDLE))
        r.on(["tmux", "-u", "send-keys"], lambda a: self.events.append(a[4:]) or cp())
        return r

    def test_new_codex_waits_for_thread(self):
        r = self.codex_runner(thread_after=3)
        r.on(["tmux", "-u", "capture-pane"], cp(out=""))  # not yet at the input box (e.g. still handling the first prompt)
        rc = cmd_new(self.cfg, r, self.msgs.append, "cx", "codex", str(self.cwd),
                     sleep=lambda s: self.events.append(("sleep", s)), timeout=5)
        self.assertEqual(rc, 0)
        # The launch command and Enter are sent separately with a 1 s pause between (Codex swallows an Enter sent in the same send-keys)
        self.assertEqual(self.events[:3], [["%50", "codex"], ("sleep", 1), ["%50", "Enter"]])
        self.assertEqual([(x.tmux, x.agent, x.profile, x.session_id, x.cwd, x.pid)
                          for x in load_rows(self.cfg.registry_path)],
                         [("cx", "codex", self.cfg.codex.profile, TID, str(self.cwd.resolve()), 200)])

    def test_new_without_wait_returns_right_after_launch(self):
        r = self.codex_runner(thread_after=99)
        slept = []
        rc = cmd_new(self.cfg, r, self.msgs.append, "cx", "codex", str(self.cwd), sleep=slept.append, wait=False)
        self.assertEqual(rc, 0)
        self.assertEqual(r.called(["tmux", "-u", "capture-pane"]), [])
        self.assertEqual(self.events, [["%50", "codex"], ["%50", "Enter"]])  # only the launch command, no key pressed on your behalf

    def test_new_codex_idle_at_composer_stops_waiting(self):
        # Codex only creates the conversation once the first prompt is sent: after waiting two seconds at the input box, stop waiting and leave it to a later sampan save to register
        r = self.codex_runner(thread_after=99)
        looks = []
        rc = cmd_new(self.cfg, r, self.msgs.append, "cx", "codex", str(self.cwd),
                     sleep=lambda s: looks.append(s), timeout=15)
        self.assertEqual(rc, 1)
        self.assertLess(looks.count(1), 5)
        self.assertEqual(load_rows(self.cfg.registry_path), [])
        self.assertTrue(any("已就緒" in m for m in self.msgs))

    def test_new_codex_without_thread_warns_and_does_not_register(self):
        r = self.codex_runner(thread_after=99)
        r.on(["tmux", "-u", "capture-pane"], cp(out=""))
        rc = cmd_new(self.cfg, r, self.msgs.append, "cx", "codex", str(self.cwd), sleep=lambda _: None, timeout=3)
        self.assertEqual(rc, 1)
        self.assertEqual(load_rows(self.cfg.registry_path), [])
        self.assertTrue(any(m.startswith("⚠ cx") and "save" in m for m in self.msgs))

    def prompt_runner(self, first, after, thread_after=99):
        """codex_runner whose screen is `first` until a digit is sent, then `after`."""
        r = self.codex_runner(thread_after=thread_after)
        screen = [first]

        def send(a):
            self.events.append(a[4:])
            if a[5:] and a[5].isdigit():
                screen[0] = after
            return cp()
        r.on(["tmux", "-u", "send-keys"], send)
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=screen[0]))
        return r

    def test_new_codex_relays_startup_menu_and_sends_only_the_choice(self):
        from tests.test_prompts import CODEX_TRUST
        r = self.prompt_runner(CODEX_TRUST, IDLE, thread_after=2)
        asked = []
        rc = cmd_new(self.cfg, r, self.msgs.append, "cx", "codex", str(self.cwd), sleep=lambda _: None, timeout=8,
                     on_prompt=lambda name, agent, p: asked.append((name, agent, p.question)) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(asked, [("cx", "codex", "Do you trust the contents of this directory?")])
        self.assertEqual(self.events, [["%50", "codex"], ["%50", "Enter"], ["%50", "1"]])
        self.assertEqual([x.session_id for x in load_rows(self.cfg.registry_path)], [TID])

    def test_new_attach_or_skip_ends_the_wait(self):
        from sampan.prompts import ATTACH, SKIP
        from tests.test_prompts import CODEX_UPDATE
        for choice, text in ((ATTACH, "→ cx：進去處理提示"), (SKIP, "⚠ cx 停在提示，進去處理")):
            with self.subTest(choice=choice):
                self.msgs = []
                r = self.prompt_runner(CODEX_UPDATE, IDLE)
                rc = cmd_new(self.cfg, r, self.msgs.append, "cx", "codex", str(self.cwd), sleep=lambda _: None,
                             timeout=8, on_prompt=lambda *_a: choice)
                self.assertEqual(rc, 1)
                self.assertIn(text, self.msgs)
                self.assertEqual(self.events, [["%50", "codex"], ["%50", "Enter"]])

    def test_new_claude_trust_is_relayed_with_arrow_keys(self):
        from tests.test_prompts import CLAUDE_TRUST
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], lambda a: cp(out="\n".join(self.panes) + "\n"))
        screen, sent = [CLAUDE_TRUST], []

        def send(a):
            sent.append(a[4:])
            if a[5:] == ["Enter"] and sent[-2:-1] == [["%50", "Down"]]:
                screen[0] = ""
                self.panes = [pane_line("new-x", "%50", 100, "claude", str(self.cwd))]
                self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(900, 100, "claude", "claude")]
                write_session(self.cfg, "work", 900, sid=B, cwd=str(self.cwd), tmux="new-x:@1.%50")
            return cp()
        r.on(["tmux", "-u", "send-keys"], send)
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=screen[0]))
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None,
                     uuid4=lambda: B, timeout=5, on_prompt=lambda *_a: 1)
        self.assertEqual(rc, 0)
        self.assertEqual([k[1:] for k in sent[1:]], [["Down"], ["Enter"]])

    def trust_runner(self, shown):
        """A Claude that stops at the trust prompt for `shown`; Down+Enter trusts it and the session comes up."""
        from tests.test_prompts import CLAUDE_TRUST
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], lambda a: cp(out="\n".join(self.panes) + "\n"))
        screen, sent = [CLAUDE_TRUST.replace("/private/tmp/…/scratchpad/claude-trust-probe", shown)], []

        def send(a):
            sent.append(a[4:])
            if a[5:] == ["Enter"] and sent[-2:-1] == [["%50", "Down"]]:
                screen[0] = ""
                self.panes = [pane_line("new-x", "%50", 100, "claude", str(self.cwd))]
                self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(900, 100, "claude", "claude")]
                write_session(self.cfg, "work", 900, sid=B, cwd=str(self.cwd), tmux="new-x:@1.%50")
            return cp()
        r.on(["tmux", "-u", "send-keys"], send)
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=screen[0]))
        return r, sent

    def test_new_with_trust_answers_only_the_prompt_for_its_own_directory(self):
        # MCP new_session with confirm: the user approved this cwd in the tool's permission prompt
        asked = []
        r, sent = self.trust_runner(str(self.cwd.resolve()))
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None,
                     uuid4=lambda: B, timeout=5, trust=True, on_prompt=lambda *a: asked.append(a) or SKIP)
        self.assertEqual((rc, asked), (0, []))
        self.assertEqual([k[1:] for k in sent[1:]], [["Down"], ["Enter"]])
        self.assertIn(f"  已信任 {self.cwd.resolve()}", self.msgs)
        for shown in ("/somewhere/else", str(self.cwd.resolve().parent)):  # another directory: never answered
            with self.subTest(shown=shown):
                self.panes, self.procs, asked = [], [ps_line(1, 0, "launchd", "launchd")], []
                r, sent = self.trust_runner(shown)
                rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None,
                             uuid4=lambda: B, timeout=5, trust=True, on_prompt=lambda *a: asked.append(a) or SKIP)
                self.assertEqual((rc, len(asked)), (1, 1))
                self.assertEqual(sent[1:], [])

    def test_new_trust_presses_once_through_a_slow_redraw(self):
        from tests.test_prompts import CLAUDE_TRUST
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], lambda a: cp(out="\n".join(self.panes) + "\n"))
        trust = CLAUDE_TRUST.replace("/private/tmp/…/scratchpad/claude-trust-probe", str(self.cwd.resolve()))
        sent, captures = [], [0]

        def send(a):
            sent.append(a[4:])
            return cp()

        def capture(a):
            captures[0] += 1
            if [k for k in sent[1:] if k[1:] == ["Enter"]] and captures[0] >= 5:  # the screen catches up late
                self.panes = [pane_line("new-x", "%50", 100, "claude", str(self.cwd))]
                self.procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(900, 100, "claude", "claude")]
                write_session(self.cfg, "work", 900, sid=B, cwd=str(self.cwd), tmux="new-x:@1.%50")
                return cp(out="")
            return cp(out=trust)
        r.on(["tmux", "-u", "send-keys"], send)
        r.on(["tmux", "-u", "capture-pane"], capture)
        asked = []
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None,
                     uuid4=lambda: B, timeout=8, trust=True, on_prompt=lambda *a: asked.append(a) or SKIP)
        self.assertEqual((rc, asked), (0, []))
        self.assertEqual([k[1:] for k in sent[1:]], [["Down"], ["Enter"]])  # never pressed a second time

    def test_new_trust_then_another_prompt_goes_to_on_prompt(self):
        from tests.test_prompts import CLAUDE_TRUST
        r, sent = self.trust_runner(str(self.cwd.resolve()))
        other = CLAUDE_TRUST.replace("/private/tmp/…/scratchpad/claude-trust-probe", "/another/dir")
        r.on(["tmux", "-u", "send-keys"], lambda a: sent.append(a[4:]) or cp())
        screens = [CLAUDE_TRUST.replace("/private/tmp/…/scratchpad/claude-trust-probe", str(self.cwd.resolve())), other]
        r.on(["tmux", "-u", "capture-pane"], lambda a: cp(out=screens[0] if not sent[1:] else screens[1]))
        asked = []
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None,
                     uuid4=lambda: B, timeout=5, trust=True, on_prompt=lambda n, a, p: asked.append(p.trust_path) or SKIP)
        self.assertEqual((rc, asked), (1, ["/another/dir"]))
        self.assertEqual([k[1:] for k in sent[1:]], [["Down"], ["Enter"]])

    def test_new_is_logged_with_its_prompt_answers_and_a_dry_run_is_not(self):
        r, _ = self.trust_runner(str(self.cwd.resolve()))
        cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), dry_run=True)
        self.assertEqual(action_lines(self.cfg), [])
        cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None,
                uuid4=lambda: B, timeout=5, on_prompt=lambda *_a: 1)
        lines = action_lines(self.cfg)
        self.assertTrue(lines and all("[new new-x]" in x for x in lines), lines)
        self.assertTrue(any("已登記" in x for x in lines), lines)
        self.assertTrue(any("提示" in x and "→" in x for x in lines), lines)  # the trust answer the user gave

    def test_stop_and_move_are_logged(self):
        cmd_stop(self.cfg, self.runner(), self.msgs.append, names=("nope",), dry_run=True)
        self.assertEqual(action_lines(self.cfg), [])
        cmd_stop(self.cfg, self.runner(), self.msgs.append, names=("nope",))
        self.assertTrue(any("[stop]" in x for x in action_lines(self.cfg)), action_lines(self.cfg))
        from sampan.session_run import cmd_move
        cmd_move(self.cfg, self.runner(), self.msgs.append, "nope", str(self.cwd))
        self.assertTrue(any("[move nope]" in x for x in action_lines(self.cfg)), action_lines(self.cfg))

    def test_new_without_trust_relays_the_trust_prompt(self):
        r, sent = self.trust_runner(str(self.cwd.resolve()))
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", "work", str(self.cwd), sleep=lambda _: None,
                     uuid4=lambda: B, timeout=5, on_prompt=lambda *a: SKIP)
        self.assertEqual((rc, sent[1:]), (1, []))

    def test_new_without_on_prompt_never_presses(self):
        from tests.test_prompts import CODEX_TRUST
        r = self.prompt_runner(CODEX_TRUST, IDLE)
        self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, "cx", "codex", str(self.cwd), sleep=lambda _: None,
                                 timeout=3), 1)
        self.assertEqual(self.events, [["%50", "codex"], ["%50", "Enter"]])

    def test_new_codex_disabled(self):
        cfg = dataclasses.replace(self.cfg, codex=dataclasses.replace(self.cfg.codex, enabled=False))
        r = self.runner()
        self.assertEqual(cmd_new(cfg, r, self.msgs.append, "cx", "codex", str(self.cwd)), 2)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])

    def test_new_with_corrupt_registry_creates_no_tmux_session(self):
        self.cfg.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.registry_path.write_text("{not json")
        for target in ("alice", "codex"):
            r = self.runner()
            self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, "x", target, str(self.cwd)), 2)
            self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])

    def test_new_rejects_existing_and_unknown_profile(self):
        r = self.runner().on(["tmux", "-u", "has-session"], cp(rc=0))
        self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, "x", "alice", str(self.cwd)), 2)
        self.assertEqual(cmd_new(self.cfg, self.runner(), self.msgs.append, "x", "claude:nobody", str(self.cwd)), 2)

    def test_new_timeout_warns(self):
        rc = cmd_new(self.cfg, self.runner(), self.msgs.append, "x", "alice", str(self.cwd),
                     sleep=lambda _: None, timeout=2)
        self.assertEqual(rc, 1)
        self.assertEqual(len(load_rows(self.cfg.registry_path)), 1)  # registered immediately, just a warning

    def test_new_dry_run(self):
        # Review R8
        r = self.runner()
        self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, "x", "alice", str(self.cwd), dry_run=True), 0)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])
        self.assertTrue(any("[dry-run]" in m for m in self.msgs))

    def test_retire_and_activate(self):
        self.registry([Row("x", "claude", "alice", A, "/c")])
        self.assertEqual(cmd_set_state(self.cfg, self.msgs.append, "x", "retired"), 0)
        self.assertEqual(load_rows(self.cfg.registry_path)[0].state, "retired")
        self.assertEqual(cmd_set_state(self.cfg, self.msgs.append, "nope", "retired"), 2)

    def test_set_state_refuses_an_unknown_state_and_leaves_the_registry_alone(self):
        self.registry([Row("x", "claude", "alice", A, "/c")])
        before = self.cfg.registry_path.read_text()
        self.assertEqual(cmd_set_state(self.cfg, self.msgs.append, "x", "bogus"), 2)
        self.assertEqual(self.cfg.registry_path.read_text(), before)
        self.assertTrue(any("bogus" in m for m in self.msgs))



class ForgetTest(Base):
    def setUp(self):
        super().setUp()
        self.live = set()
        self.registry([Row("u", "claude", "alice", A, str(self.cwd), "retired", "t", "srv", None),
                       Row("other", "claude", "alice", B, str(self.cwd), "active", "t", "srv", None)])

    def runner(self):
        r = super().runner()
        r.on(["tmux", "-u", "has-session"], lambda a: cp(rc=0 if a[-1].lstrip("=") in self.live else 1))
        return r

    def test_forget_removes_row_keeps_history_and_prints_resume_line(self):
        r = self.runner()
        self.assertEqual(cmd_forget(self.cfg, r, self.msgs.append, "u"), 0)
        self.assertEqual([x.tmux for x in load_rows(self.cfg.registry_path)], ["other"])
        self.assertTrue(any("u" in [x.tmux for x in load_rows(f)] for f in self.cfg.history_dir.iterdir()))
        self.assertEqual(self.msgs[0], "✔ 已從登記表移除 u")
        self.assertIn("  對話還在：", self.msgs[1])
        self.assertIn(f"--resume {A}", self.msgs[1])
        self.assertEqual(r.called(["tmux", "-u", "kill-session"]), [])
        log = actionlog.log_path(self.cfg).read_text()
        self.assertIn("[forget u] ✔ 已從登記表移除 u", log)
        self.assertIn("[forget u]   對話還在：", log)

    def test_forget_dry_run_logs_nothing(self):
        self.assertEqual(cmd_forget(self.cfg, self.runner(), self.msgs.append, "u", dry_run=True), 0)
        self.assertFalse(actionlog.log_path(self.cfg).exists())

    def test_forget_prints_id_when_it_cannot_be_resumed(self):
        self.registry([Row("u", "claude", "gone", "not-a-uuid", str(self.cwd), "retired", "t", "srv", None)])
        self.assertEqual(cmd_forget(self.cfg, self.runner(), self.msgs.append, "u"), 0)
        self.assertEqual(self.msgs[1], "  對話 id not-a-uuid")

    def test_forget_refuses_running_and_missing(self):
        before = self.cfg.registry_path.read_text()
        self.live = {"u"}
        self.assertEqual(cmd_forget(self.cfg, self.runner(), self.msgs.append, "u"), 1)
        self.assertEqual(self.msgs[-1], "❌ u 還在 tmux 裡；先 sampan stop u（只剩 shell 的話：tmux kill-session -t =u）")
        self.assertEqual(cmd_forget(self.cfg, self.runner(), self.msgs.append, "nope"), 2)
        self.assertEqual(self.msgs[-1], "❌ 登記表沒有 nope")
        self.assertEqual(self.cfg.registry_path.read_text(), before)

    def test_forget_refuses_when_tmux_unreadable(self):
        before = self.cfg.registry_path.read_text()
        r = self.runner()
        r.on(["tmux", "-u", "list-panes"], cp(rc=1, err="boom"))
        self.assertEqual(cmd_forget(self.cfg, r, self.msgs.append, "u"), 2)
        self.assertEqual(self.cfg.registry_path.read_text(), before)

    def test_forget_dry_run_changes_nothing(self):
        before = self.cfg.registry_path.read_text()
        self.assertEqual(cmd_forget(self.cfg, self.runner(), self.msgs.append, "u", dry_run=True), 0)
        self.assertEqual(self.cfg.registry_path.read_text(), before)
        self.assertIn("u", self.msgs[0])
        self.assertNotIn("✔", self.msgs[0])


class FolderSyncTest(Base):
    def setUp(self):
        super().setUp()
        self.live = set()
        self.registry([Row("a1", "claude", "alice", A, str(self.cwd), "active", "t", "srv", None),
                       Row("u", "claude", "alice", B, str(self.cwd), "retired", "t", "srv", None)])
        folders.edit(self.cfg.state_dir, lambda t: (folders.place_session(t, "a1", "客戶"),
                                                    folders.place_session(t, "u", "客戶")))

    def runner(self):
        r = super().runner()
        r.on(["tmux", "-u", "has-session"], lambda a: cp(rc=0 if a[-1].lstrip("=") in self.live else 1))
        r.on(["tmux", "-u", "rename-session"], cp())
        return r

    def test_rename_syncs_folder(self):
        self.assertEqual(cmd_rename(self.cfg, self.runner(), self.msgs.append, "a1", "a2"), 0)
        self.assertEqual(folders.load(self.cfg.state_dir).sessions, {"a2": "客戶", "u": "客戶"})

    def test_forget_syncs_folder(self):
        self.assertEqual(cmd_forget(self.cfg, self.runner(), self.msgs.append, "u"), 0)
        self.assertEqual(folders.load(self.cfg.state_dir).sessions, {"a1": "客戶"})

    def test_forget_dry_run_keeps_folder(self):
        cmd_forget(self.cfg, self.runner(), self.msgs.append, "u", dry_run=True)
        self.assertEqual(folders.load(self.cfg.state_dir).sessions, {"a1": "客戶", "u": "客戶"})

    def test_rename_still_ok_when_folders_broken(self):
        (self.cfg.state_dir / "folders.json").write_text("{")
        self.assertEqual(cmd_rename(self.cfg, self.runner(), self.msgs.append, "a1", "a2"), 0)
        self.assertEqual(sum(m.startswith("⚠ 資料夾設定沒同步") for m in self.msgs), 1)
        self.assertEqual((self.cfg.state_dir / "folders.json").read_text(), "{")
        self.assertIn("a2", {x.tmux for x in load_rows(self.cfg.registry_path)})

    def test_new_with_folder_places_after_success(self):
        rc = cmd_new(self.cfg, self.runner(), self.msgs.append, "n1", "claude:work", str(self.cwd), folder="開發/甲",
                     sleep=lambda _: None, timeout=1, uuid4=lambda: A, wait=False)
        self.assertEqual(rc, 0)
        t = folders.load(self.cfg.state_dir)
        self.assertEqual(t.sessions["n1"], "開發/甲")
        self.assertIn("開發", t.folders)

    def test_new_failure_does_not_write_folder(self):
        before = (self.cfg.state_dir / "folders.json").read_bytes()
        rc = cmd_new(self.cfg, self.runner(), self.msgs.append, "n1", "claude:work", str(self.cwd / "nope"),
                     folder="開發", sleep=lambda _: None, timeout=1)
        self.assertEqual(rc, 2)
        rc = cmd_new(self.cfg, self.runner(), self.msgs.append, "n2", "claude:work", str(self.cwd), folder="開發",
                     sleep=lambda _: None, timeout=1, uuid4=lambda: A)  # no session file: rc 1
        self.assertEqual(rc, 1)
        self.assertEqual((self.cfg.state_dir / "folders.json").read_bytes(), before)

    def test_new_with_bad_folder_refused_before_anything_starts(self):
        r = self.runner()
        self.assertEqual(cmd_new(self.cfg, r, self.msgs.append, "n1", "claude:work", str(self.cwd), folder="a//b"), 2)
        self.assertEqual(r.called(["tmux", "-u", "new-session"]), [])

    def test_new_rc1_with_tmux_session_places_folder(self):
        # Codex returns 1 when it is ready but not registered yet: the tmux session exists, so it is placed
        from unittest import mock
        self.live.add("n1")
        with mock.patch("sampan.session_run._cmd_new", return_value=1):
            rc = cmd_new(self.cfg, self.runner(), self.msgs.append, "n1", "codex", str(self.cwd), folder="開發")
        self.assertEqual(rc, 1)
        self.assertEqual(folders.load(self.cfg.state_dir).sessions["n1"], "開發")

    def test_new_rc1_without_tmux_session_does_not_place(self):
        from unittest import mock
        with mock.patch("sampan.session_run._cmd_new", return_value=1):
            rc = cmd_new(self.cfg, self.runner(), self.msgs.append, "n1", "codex", str(self.cwd), folder="開發")
        self.assertEqual(rc, 1)
        self.assertNotIn("n1", folders.load(self.cfg.state_dir).sessions)

    def test_new_folder_write_failure_only_warns(self):
        (self.cfg.state_dir / "folders.json").write_text("{")
        rc = cmd_new(self.cfg, self.runner(), self.msgs.append, "n1", "claude:work", str(self.cwd), folder="開發",
                     sleep=lambda _: None, timeout=1, uuid4=lambda: A, wait=False)
        self.assertEqual(rc, 0)
        self.assertTrue(any(m.startswith("⚠ 資料夾設定沒同步") for m in self.msgs))


class RenameTest(Base):
    def setUp(self):
        super().setUp()
        self.live = {"u"}
        self.registry([Row("u", "claude", "alice", A, str(self.cwd), "active", "t", "srv", None),
                       Row("other", "claude", "alice", B, str(self.cwd), "active", "t", "srv", None)])

    def runner(self):
        r = super().runner()
        r.on(["tmux", "-u", "has-session"], lambda a: cp(rc=0 if a[-1].lstrip("=") in self.live else 1))
        r.on(["tmux", "-u", "rename-session"], cp())
        return r

    def renamed(self, r):
        return [c["args"][2:] for c in r.called(["tmux", "-u", "rename-session"])]

    def test_renames_tmux_session_and_registry_row(self):
        r = self.runner()
        self.assertEqual(cmd_rename(self.cfg, r, self.msgs.append, "u", "webshop"), 0)
        self.assertEqual(self.renamed(r), [["rename-session", "-t", "=u", "webshop"]])
        rows = {x.tmux: x for x in load_rows(self.cfg.registry_path)}
        self.assertEqual(set(rows), {"webshop", "other"})
        self.assertEqual(rows["webshop"].session_id, A)

    def test_registry_only_when_tmux_session_not_running(self):
        self.live = set()
        r = self.runner()
        self.assertEqual(cmd_rename(self.cfg, r, self.msgs.append, "u", "new-name"), 0)
        self.assertEqual(self.renamed(r), [])
        self.assertIn("new-name", {x.tmux for x in load_rows(self.cfg.registry_path)})

    def test_tmux_only_session_is_renamed(self):
        self.live = {"loose"}
        r = self.runner()
        self.assertEqual(cmd_rename(self.cfg, r, self.msgs.append, "loose", "tidy"), 0)
        self.assertEqual(self.renamed(r), [["rename-session", "-t", "=loose", "tidy"]])
        self.assertNotIn("tidy", {x.tmux for x in load_rows(self.cfg.registry_path)})

    def test_rejects_bad_or_taken_names_and_changes_nothing(self):
        before = self.cfg.registry_path.read_text()
        self.live = {"u", "busy"}
        for new in ("a.b", "a:b", "", "other", "busy", "u", "a\nb", "a\x1b[2Jb", "終端機", "未登記的 session（claude）"):
            r = self.runner()
            self.assertEqual(cmd_rename(self.cfg, r, self.msgs.append, "u", new), 2, new)
            self.assertEqual(self.renamed(r), [], new)
        self.assertEqual(self.cfg.registry_path.read_text(), before)

    def test_unknown_session(self):
        r = self.runner()
        self.assertEqual(cmd_rename(self.cfg, r, self.msgs.append, "nope", "x"), 2)
        self.assertTrue(any("nope" in m for m in self.msgs))

    def test_tmux_failure_leaves_registry_alone(self):
        before = self.cfg.registry_path.read_text()
        r = self.runner()
        r.on(["tmux", "-u", "rename-session"], cp(rc=1, err="boom"))
        self.assertEqual(cmd_rename(self.cfg, r, self.msgs.append, "u", "fresh"), 1)
        self.assertEqual(self.cfg.registry_path.read_text(), before)
