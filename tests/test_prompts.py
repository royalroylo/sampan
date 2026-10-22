"""Startup prompts (Codex update/trust/hooks review, Claude trust-folder): parse the screen, relay to the user for an answer, send the choice."""
import re
import unittest
from pathlib import Path

from sampan.prompts import ATTACH, SKIP, Relay, plain_prompt, prompt_lines, recording
from sampan.screens import Prompt, parse_prompt

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Real-machine screen from codex-cli 0.147.0 (capture-pane -p, blank lines removed)
CODEX_UPDATE = """\
  ✨ Update available! 0.147.0 -> 0.163.0
  Release notes: https://github.com/openai/codex/releases/latest
› 1. Update now (runs `npm install -g @openai/codex`)
  2. Skip
  3. Skip until next version
  Press enter to continue
"""
CODEX_TRUST = """\
> You are in /private/tmp/…/scratchpad
  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt
  injection. Trusting the directory allows project-local config, hooks, and exec policies to load.
› 1. Yes, continue
  2. No, quit
  Press enter to continue
"""
CODEX_HOOKS = """\
  Hooks need review
  2 hooks are new or changed.
  Hooks can run outside the sandbox after you trust them.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
  Press enter to confirm or esc to go back
"""
# Claude Code 2.1.287
CLAUDE_TRUST = """\
 Accessing workspace:
 /private/tmp/…/scratchpad/claude-trust-probe
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source
 project, or work from your team). If not, take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""
IDLE = (FIXTURES / "codex_idle.txt").read_text()


OPTION = re.compile(r"^(› |  \d\. | ❯ |   \S)")


def with_blanks(screen):
    """Real captures have blank lines between paragraphs (none between options)."""
    out = []
    for line in screen.splitlines():
        if out and not (OPTION.match(line) and OPTION.match(out[-1])):
            out.append("")
        out.append(line)
    return "\n".join(out) + "\n"


class ParseCodexTest(unittest.TestCase):
    def test_update(self):
        p = parse_prompt(CODEX_UPDATE, "codex")
        self.assertEqual(p.question, "✨ Update available! 0.147.0 -> 0.163.0")
        self.assertEqual(p.options, ("Update now (runs `npm install -g @openai/codex`)", "Skip", "Skip until next version"))
        self.assertEqual(p.current, 0)
        self.assertEqual(p.keys_for(1), [["2"]])
        self.assertIn("Release notes: https://github.com/openai/codex/releases/latest", p.detail)
        self.assertIsNone(p.trust_path)

    def test_trust(self):
        p = parse_prompt(CODEX_TRUST, "codex")
        self.assertEqual(p.question, "Do you trust the contents of this directory?")
        self.assertEqual(p.options, ("Yes, continue", "No, quit"))
        self.assertEqual(p.keys_for(0), [["1"]])
        self.assertEqual(p.trust_path, "/private/tmp/…/scratchpad")
        self.assertEqual(p.detail, ("目錄：/private/tmp/…/scratchpad",))

    def test_hooks(self):
        p = parse_prompt(CODEX_HOOKS, "codex")
        self.assertEqual(p.question, "Hooks need review")
        self.assertEqual(p.options, ("Review hooks", "Trust all and continue", "Continue without trusting (hooks won't run)"))
        self.assertEqual(p.keys_for(2), [["3"]])
        self.assertIn("2 hooks are new or changed.", p.detail)

    def test_same_screens_with_blank_lines(self):
        for screen in (CODEX_UPDATE, CODEX_TRUST, CODEX_HOOKS):
            with self.subTest(screen=screen[:20]):
                a, b = parse_prompt(screen, "codex"), parse_prompt(with_blanks(screen), "codex")
                self.assertEqual((a.question, a.options, a.trust_path), (b.question, b.options, b.trust_path))
        self.assertEqual(parse_prompt((FIXTURES / "codex_trust.txt").read_text(), "codex").trust_path, "/Users/u/proj")

    def test_wrapped_trust_path(self):
        screen = ("> You are in /Users/u/我的 專案/a-very-long-directory-name-that-wraps-at-the-pane-\n"
                  "  edge/sub\n\n  Do you trust the contents of this directory? Working with untrusted contents\n\n"
                  "› 1. Yes, continue\n  2. No, quit\n\n  Press enter to continue\n")
        p = parse_prompt(screen, "codex")
        self.assertEqual(p.trust_path, "/Users/u/我的 專案/a-very-long-directory-name-that-wraps-at-the-pane-edge/sub")
        self.assertEqual(p.question, "Do you trust the contents of this directory?")

    def test_approval_prompt_question_and_wrapped_option(self):
        p = parse_prompt((FIXTURES / "codex_waiting.txt").read_text(), "codex")
        self.assertEqual(len(p.options), 3)
        self.assertTrue(p.question.endswith("?"))
        wrapped = ("  Hooks need review\n\n› 1. Review hooks\n  2. Trust all and continue, this option is long and\n"
                   "     wraps onto a second line\n  3. Continue\n\n  Press enter to continue\n")
        p = parse_prompt(wrapped, "codex")
        self.assertEqual(p.options[1], "Trust all and continue, this option is long and wraps onto a second line")
        self.assertEqual(p.keys_for(2), [["3"]])

    def test_menu_in_scrollback_followed_by_composer_is_none(self):
        self.assertIsNone(parse_prompt(CODEX_TRUST + "\n" + IDLE, "codex"))
        self.assertIsNone(parse_prompt(CODEX_UPDATE + "\n› Write tests for @filename\n\n  codex-model · /x\n", "codex"))

    def test_idle_composer_is_none(self):
        self.assertIsNone(parse_prompt(IDLE, "codex"))
        self.assertIsNone(parse_prompt((FIXTURES / "codex_working.txt").read_text(), "codex"))
        self.assertIsNone(parse_prompt("", "codex"))

    def test_marker_without_options_is_unknown(self):
        p = parse_prompt("  Something new happened\n  Some text\n  Press enter to continue\n", "codex")
        self.assertEqual(p.options, ())
        self.assertIn("Some text", p.detail)


class ParseClaudeTest(unittest.TestCase):
    def test_trust(self):
        p = parse_prompt(CLAUDE_TRUST, "claude")
        self.assertEqual(p.question, "Quick safety check: Is this a project you created or one you trust?")
        self.assertEqual(p.options, ("No, exit", "Yes, I trust this folder"))
        self.assertEqual(p.current, 0)
        self.assertEqual(p.keys_for(1), [["Down"], ["Enter"]])
        self.assertEqual(p.keys_for(0), [["Enter"]])
        self.assertEqual(p.trust_path, "/private/tmp/…/scratchpad/claude-trust-probe")
        self.assertEqual(parse_prompt(with_blanks(CLAUDE_TRUST), "claude").options, p.options)

    def test_cursor_on_second_option(self):
        screen = CLAUDE_TRUST.replace(" ❯ No, exit\n   Yes,", "   No, exit\n ❯ Yes,")
        p = parse_prompt(screen, "claude")
        self.assertEqual(p.current, 1)
        self.assertEqual(p.keys_for(0), [["Up"], ["Enter"]])
        self.assertEqual(p.keys_for(1), [["Enter"]])

    def test_wrapped_path(self):
        long = "/Users/u/Projects/" + "a-very-long-directory-name/" * 5 + "end"
        wrapped = f" {long[:60]}\n {long[60:]}"
        screen = CLAUDE_TRUST.replace(" /private/tmp/…/scratchpad/claude-trust-probe", wrapped)
        p = parse_prompt(with_blanks(screen).replace(f"{long[:60]}\n\n", f"{long[:60]}\n"), "claude")
        self.assertEqual(p.trust_path, long)
        self.assertEqual(p.question, "Quick safety check: Is this a project you created or one you trust?")
        self.assertEqual(parse_prompt(screen, "claude").trust_path, long)

    def test_scrollback_and_idle_are_none(self):
        self.assertIsNone(parse_prompt(CLAUDE_TRUST + "\n> hello\n\n⏺ Hi!\n\n> \n  ? for shortcuts\n", "claude"))
        self.assertIsNone(parse_prompt("> \n  ? for shortcuts\n", "claude"))

    def test_marker_without_options_is_unknown(self):
        p = parse_prompt(" Something\n Enter to confirm · Esc to cancel\n", "claude")
        self.assertEqual(p.options, ())


class FakeTmux:
    def __init__(self, screens):
        self.screens = list(screens)  # the screen after each key sent
        self.sent = []

    def send_keys(self, target, *keys):
        self.sent.append((target, list(keys)))

    def capture(self, target):
        return self.screens.pop(0) if self.screens else ""


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.msgs, self.sleeps, self.asked = [], [], []

    def relay(self, answers, screens):
        answers = list(answers)
        tmux = FakeTmux(screens)

        def on_prompt(name, agent, p):
            self.asked.append(p)
            return answers.pop(0)
        return Relay(on_prompt, tmux, self.msgs.append, self.sleeps.append), tmux

    def test_no_prompt_asks_nothing(self):
        r, tmux = self.relay([], [])
        self.assertIsNone(r.check("x", "codex", "%1", IDLE))
        self.assertEqual(self.asked, [])

    def test_three_codex_menus_in_a_row(self):
        r, tmux = self.relay([2, 0, 2], [CODEX_TRUST, CODEX_HOOKS, IDLE])
        self.assertEqual(r.check("x", "codex", "%1", CODEX_UPDATE), "answered")
        self.assertEqual(tmux.sent, [("%1", ["3"]), ("%1", ["1"]), ("%1", ["3"])])
        self.assertEqual([p.question for p in self.asked],
                         ["✨ Update available! 0.147.0 -> 0.163.0", "Do you trust the contents of this directory?",
                          "Hooks need review"])
        self.assertEqual(self.sleeps, [1, 1, 1])

    def test_claude_arrow_keys_are_separate_with_a_gap(self):
        r, tmux = self.relay([1], [""])
        self.assertEqual(r.check("x", "claude", "%1", CLAUDE_TRUST), "answered")
        self.assertEqual(tmux.sent, [("%1", ["Down"]), ("%1", ["Enter"])])
        self.assertEqual(self.sleeps, [0.3, 1])

    def test_same_menu_after_sending_offers_only_attach_or_skip(self):
        r, tmux = self.relay([0, SKIP], [CODEX_TRUST])
        self.assertEqual(r.check("x", "codex", "%1", CODEX_TRUST), SKIP)
        self.assertIn("x：送出後畫面沒變，請進去處理", "\n".join(self.msgs))
        self.assertEqual(self.asked[1].options, ())
        self.assertEqual(self.asked[1].question, self.asked[0].question)
        self.assertEqual(len(tmux.sent), 1)

    def test_skip_stops_asking_for_that_session(self):
        r, tmux = self.relay([SKIP], [])
        self.assertEqual(r.check("x", "codex", "%1", CODEX_TRUST), SKIP)
        self.assertIsNone(r.check("x", "codex", "%1", CODEX_TRUST))
        self.assertEqual(r.skipped, ["x"])
        self.assertEqual(tmux.sent, [])

    def test_attach_is_recorded(self):
        r, tmux = self.relay([ATTACH], [])
        self.assertEqual(r.check("x", "codex", "%1", CODEX_TRUST), ATTACH)
        self.assertEqual(r.attach, "x")
        self.assertEqual(tmux.sent, [])

    def test_unknown_prompt_or_bad_choice_sends_nothing(self):
        r, tmux = self.relay([0], [])
        self.assertEqual(r.check("x", "codex", "%1", "  Odd\n  Press enter to continue\n"), SKIP)
        r, tmux = self.relay([True], [])
        self.assertEqual(r.check("y", "codex", "%1", CODEX_TRUST), SKIP)
        self.assertEqual(tmux.sent, [])


class TextTest(unittest.TestCase):
    def test_prompt_lines(self):
        p = parse_prompt(CODEX_TRUST, "codex")
        self.assertEqual(prompt_lines("001-proj", "codex", p),
                         ["? 001-proj（codex）在問：Do you trust the contents of this directory?",
                          "  目錄：/private/tmp/…/scratchpad"])
        unknown = parse_prompt("  Odd thing\n  Press enter to continue\n", "codex")
        self.assertIn("認不出", prompt_lines("x", "codex", unknown)[0])
        self.assertIn("  Odd thing", prompt_lines("x", "codex", unknown))

    def test_plain_prompt(self):
        out, answers = [], ["9", "2"]
        fn = plain_prompt(out.append, lambda _p: answers.pop(0))
        self.assertEqual(fn("x", "codex", parse_prompt(CODEX_TRUST, "codex")), 1)
        text = "\n".join(out)
        self.assertIn("a. 進去自己操作", text)
        self.assertIn("1. Yes, continue", text)
        self.assertIn("s. 先不管", text)
        self.assertIn("無效選擇：9", text)
        for raw, want in (("", ATTACH), ("a", ATTACH), ("s", SKIP), (None, SKIP)):
            self.assertEqual(plain_prompt(out.append, lambda _p: raw)("x", "codex", parse_prompt(CODEX_TRUST, "codex")),
                             want)

        def eof(_p):
            raise EOFError
        self.assertEqual(plain_prompt(out.append, eof)("x", "codex", parse_prompt(CODEX_TRUST, "codex")), SKIP)

    def test_unknown_plain_prompt_has_no_numbers(self):
        out, answers = [], ["1", "s"]
        fn = plain_prompt(out.append, lambda _p: answers.pop(0))
        self.assertEqual(fn("x", "codex", Prompt("codex", "q")), SKIP)
        self.assertNotIn("1. ", "\n".join(out))

    def test_recording(self):
        fn, want = recording(lambda name, agent, p: ATTACH if name == "b" else 0)
        self.assertEqual(fn("a", "codex", None), 0)
        self.assertEqual(fn("b", "codex", None), ATTACH)
        self.assertEqual(want, ["b"])


if __name__ == "__main__":
    unittest.main()
