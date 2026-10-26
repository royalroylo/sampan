"""Delivering a dispatch: Claude by tmux buffer paste (only into an empty prompt), Codex by `codex queue`."""
import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path

from sampan.deliver import Pasted, Unsure, compose, source_line, to_claude, to_codex
from sampan.dispatch import write_outbox
from sampan.tmux import Tmux
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TID = "11111111-2222-3333-4444-555555555555"


def screen(name):
    return (FIXTURES / name).read_text()


def tmux_with(screen_text, *, buffer_rc=0):
    run = FakeRunner(strict=True)
    run.on(["tmux", "-u", "capture-pane"], cp(0, screen_text))
    run.on(["tmux", "-u", "display-message"], cp(0, "0\n"))
    run.on(["tmux", "-u", "load-buffer"], cp(buffer_rc))
    run.on(["tmux", "-u", "paste-buffer"], cp())
    run.on(["tmux", "-u", "send-keys"], cp())
    return run, Tmux(run)


def writes(run):
    return [c["args"] for c in run.calls if not {"capture-pane", "display-message"} & set(c["args"])]


class SourceLineTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(source_line(None, "", "ab12"), "〔sampan 轉送｜來自 終端機｜派工 ab12｜直接回覆文字即可，sampan 會帶回〕")
        self.assertEqual(source_line("api", "work", "ab12"), "〔sampan 轉送｜來自 api（work）｜派工 ab12｜直接回覆文字即可，sampan 會帶回〕")
        self.assertEqual(source_line("x", "codex", "ab12"), "〔sampan 轉送｜來自 x（codex）｜派工 ab12｜直接回覆文字即可，sampan 會帶回〕")
        self.assertEqual(compose(None, "", "ab12", "hi"), "〔sampan 轉送｜來自 終端機｜派工 ab12｜直接回覆文字即可，sampan 會帶回〕\nhi")

    def test_a_label_without_profile_has_no_empty_brackets(self):
        self.assertEqual(source_line("未登記的 session（claude）", "", "ab12"),
                         "〔sampan 轉送｜來自 未登記的 session（claude）｜派工 ab12｜直接回覆文字即可，sampan 會帶回〕")


class ToClaudeTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.outbox = write_outbox(self.state, "ab12", "hello")
        self.sleeps = []

    def deliver(self, tmux):
        return to_claude(tmux, "%3", self.outbox, "ab12", self.sleeps.append)

    def test_empty_prompt_loads_pastes_then_enters(self):
        run, tmux = tmux_with(screen("claude_empty_placeholder.ansi"))
        self.assertIsNone(self.deliver(tmux))
        cap = run.calls[0]["args"]
        self.assertIn("-e", cap)  # attributes are needed to tell a placeholder from a draft
        self.assertEqual(writes(run), [
            ["tmux", "-u", "load-buffer", "-b", "sampan-ab12", str(self.outbox)],
            ["tmux", "-u", "paste-buffer", "-p", "-d", "-b", "sampan-ab12", "-t", "%3"],
            ["tmux", "-u", "send-keys", "-t", "%3", "Enter"],
        ])
        self.assertEqual(self.sleeps, [0.5])

    def test_draft_blocked_and_unknown_send_nothing(self):
        for name, reason in (("claude_draft.ansi", "對方輸入框有未送出的字"),
                             ("claude_pasted.ansi", "對方輸入框有未送出的字"),
                             ("claude_slash_menu.ansi", "對方畫面停在選單或提示"),
                             ("claude_trust.ansi", "對方畫面停在選單或提示")):
            with self.subTest(name):
                run, tmux = tmux_with(screen(name))
                self.assertEqual(self.deliver(tmux), reason)
                self.assertEqual(writes(run), [])
        run, tmux = tmux_with("")  # unreadable pane
        self.assertEqual(self.deliver(tmux), "對方畫面停在選單或提示")
        self.assertEqual(writes(run), [])

    def test_load_failure_is_reported_and_nothing_is_pasted(self):
        run = FakeRunner(strict=True)
        run.on(["tmux", "-u", "capture-pane"], cp(0, screen("claude_empty_placeholder.ansi")))
        run.on(["tmux", "-u", "display-message"], cp(0, "0\n"))
        run.on(["tmux", "-u", "load-buffer"], cp(1, "", "no such file\nmore"))
        reason = to_claude(Tmux(run), "%3", self.outbox, "ab12", self.sleeps.append)
        self.assertIn("no such file", reason)
        self.assertEqual(len(writes(run)), 1)

    def test_failed_enter_is_reported(self):
        run = FakeRunner(strict=True)
        run.on(["tmux", "-u", "capture-pane"], cp(0, screen("claude_empty_placeholder.ansi")))
        run.on(["tmux", "-u", "display-message"], cp(0, "0\n"))
        run.on(["tmux", "-u", "load-buffer"], cp()).on(["tmux", "-u", "paste-buffer"], cp())
        run.on(["tmux", "-u", "send-keys"], cp(1, "", "can't find pane\nx"))
        r = self.deliver(Tmux(run))
        self.assertEqual(r, "can't find pane")
        self.assertIsInstance(r, Pasted)  # the text is already in the box: the caller must not paste it again

    def test_copy_mode_or_unreadable_mode_sends_nothing(self):  # final review 8
        for out, rc, reason in (("1\n", 0, "對方畫面在捲動模式（tmux copy mode）"), ("", 1, "對方畫面停在選單或提示"),
                                ("?\n", 0, "對方畫面停在選單或提示")):
            with self.subTest(out=out, rc=rc):
                run, tmux = tmux_with(screen("claude_empty_placeholder.ansi"))
                run.on(["tmux", "-u", "display-message"], cp(rc, out))
                self.assertEqual(self.deliver(tmux), reason)
                self.assertEqual(writes(run), [])
                self.assertEqual(run.called(["tmux", "-u", "display-message"])[0]["args"][-3:],
                                 ["-t", "%3", "#{pane_in_mode}"])

    def test_failed_paste_deletes_the_buffer(self):
        run, tmux = tmux_with(screen("claude_empty_placeholder.ansi"))
        run.on(["tmux", "-u", "paste-buffer"], cp(1, "", "pane gone"))
        run.on(["tmux", "-u", "delete-buffer"], cp())
        r = self.deliver(tmux)
        self.assertEqual(r, "pane gone")
        self.assertNotIsInstance(r, Pasted)
        self.assertEqual(writes(run)[-1], ["tmux", "-u", "delete-buffer", "-b", "sampan-ab12"])
        self.assertEqual(run.called(["tmux", "-u", "send-keys"]), [])

    def test_refusals_are_not_pasted(self):
        run, tmux = tmux_with(screen("claude_draft.ansi"))
        self.assertNotIsInstance(self.deliver(tmux), Pasted)
        run, tmux = tmux_with(screen("claude_empty_placeholder.ansi"), buffer_rc=1)
        self.assertNotIsInstance(self.deliver(tmux), Pasted)

    def test_message_is_byte_exact_and_never_in_argv(self):
        msg = 'line1\n「中文」 "q" $HOME `x` \\n ' + "字" * 5000
        text = compose("api", "work", "ab12", msg)
        outbox = write_outbox(self.state, "ff00", text)
        self.assertEqual(outbox.read_bytes(), text.encode())
        run, tmux = tmux_with(screen("claude_empty_placeholder.ansi"))
        self.assertIsNone(to_claude(tmux, "%3", outbox, "ff00", lambda s: None))
        for c in run.calls:
            for a in c["args"]:
                self.assertNotIn("HOME", a)
                self.assertLess(len(a), 200)


class ToCodexTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_argv_env_and_default_home(self):
        run = FakeRunner(strict=True).on(["codex"], cp())
        text = 'a\nb "c" $HOME `x`'
        self.assertIsNone(to_codex(self.cfg, run, TID, text))
        c = run.calls[0]
        self.assertEqual(c["args"], ["codex", "queue", "--thread", TID, "--message", text])
        self.assertFalse(c["env"] and "CODEX_HOME" in c["env"])
        self.assertEqual(c["timeout"], 30)
        self.assertEqual(c["stdin"], subprocess.DEVNULL)

    def test_custom_codex_home_is_passed(self):
        cfg = dataclasses.replace(self.cfg, codex=dataclasses.replace(self.cfg.codex, home=Path("/x/codex-home")))
        run = FakeRunner(strict=True).on(["codex"], cp())
        to_codex(cfg, run, TID, "m")
        self.assertEqual(run.calls[0]["env"]["CODEX_HOME"], "/x/codex-home")

    def test_failures(self):
        run = FakeRunner(strict=True).on(["codex"], cp(1, "", "\n  boom: bad thread \nsecond"))
        self.assertEqual(to_codex(self.cfg, run, TID, "m"), "boom: bad thread")
        run = FakeRunner(strict=True).on(["codex"], cp(127, "", "[Errno 2] No such file"))
        self.assertEqual(to_codex(self.cfg, run, TID, "m"), "找不到 codex 指令")
        run = FakeRunner(strict=True).on(["codex"], cp(2))
        self.assertEqual(to_codex(self.cfg, run, TID, "m"), "結束碼 2")

    def test_timeout_is_unsure_never_a_plain_reason(self):  # final review 3
        run = FakeRunner(strict=True).on(["codex"], cp(124, "", "timeout"))
        r = to_codex(self.cfg, run, TID, "m")
        self.assertIsInstance(r, Unsure)
        self.assertNotIsInstance(r, Pasted)
        self.assertIn("可能已送出", r)


if __name__ == "__main__":
    unittest.main()
