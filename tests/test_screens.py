"""Screen readers shared by every caller (verified on claude 2.1.287 / codex-cli 0.147.0)."""
import unittest
from pathlib import Path

from sampan.screens import at_trust_prompt, bottom_trust_path, claude_input_state, codex_activity, trust_path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text()


class ActivityTest(unittest.TestCase):
    def test_activity_from_fixtures(self):
        self.assertEqual(codex_activity(fixture("codex_idle.txt")), "idle")
        self.assertEqual(codex_activity(fixture("codex_working.txt")), "working")
        # an approval prompt is recognised as "not idle": never mistaken for idle
        self.assertEqual(codex_activity(fixture("codex_waiting.txt")), "unknown")
        self.assertEqual(codex_activity(fixture("codex_trust.txt")), "unknown")
        self.assertEqual(codex_activity("@@#\x1b[0m garbage ~~~ 1234\n▒▒▒▒\n"), "unknown")
        self.assertEqual(codex_activity(""), "unknown")


class TrustPathTest(unittest.TestCase):
    def test_codex(self):
        self.assertEqual(trust_path(fixture("codex_trust.txt"), "codex"), "/Users/u/proj")
        self.assertIsNone(trust_path(fixture("codex_idle.txt"), "codex"))

    def test_codex_takes_last_prompt(self):
        stale = fixture("codex_trust.txt").replace("/Users/u/proj", "/Users/u/old")
        self.assertEqual(trust_path(stale + "% codex\n" + fixture("codex_trust.txt"), "codex"), "/Users/u/proj")

    def test_codex_wrapped(self):
        screen = ("> You are in /Users/u/我的 專案/a-very-long-directory-name-that-wraps-at-the-pane-\n"
                  "  edge/sub\n"
                  "\n"
                  "  Do you trust the contents of this directory? Working with untrusted contents\n"
                  "› 1. Yes, continue\n")
        self.assertEqual(trust_path(screen, "codex"),
                         "/Users/u/我的 專案/a-very-long-directory-name-that-wraps-at-the-pane-edge/sub")

    def test_claude_survives_line_wrapping(self):
        shown = "/private/tmp/new/proj/a-long-directory-name"
        wrapped = f" {shown[:20]}\n {shown[20:]}"
        screen = f"cd {shown} && sampan claude alice\n Accessing workspace:\n\n{wrapped}\n\n Quick safety check: Is this\n"
        self.assertEqual(trust_path(screen, "claude"), shown)
        self.assertIsNone(trust_path(f"cd {shown}\n", "claude"))


CLAUDE_TRUST = (" Accessing workspace:\n\n {path}\n\n Quick safety check: Is this a project you created or one you trust?\n"
                " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm · Esc to cancel\n")
# older Claude screen (no footer, numbered, cursor on Yes): still recognised at the bottom
CLAUDE_TRUST_LEGACY = ("Accessing workspace:\n\n {path}\n\n Quick safety check: Is this a project you created or one you trust?\n"
                       " ❯ 1. Yes, I trust this folder\n   2. No, exit\n")
COMPOSER = "\n> hi\n\n  ? for shortcuts\n"


class BottomTest(unittest.TestCase):
    def test_bottom_trust_path_only_when_the_prompt_is_last(self):
        for tmpl in (CLAUDE_TRUST, CLAUDE_TRUST_LEGACY):
            with self.subTest(tmpl[:30]):
                self.assertEqual(bottom_trust_path(tmpl.format(path="/Users/u/proj")), "/Users/u/proj")
                self.assertIsNone(bottom_trust_path(tmpl.format(path="/Users/u/proj") + COMPOSER))
                self.assertIsNone(bottom_trust_path(tmpl.format(path="/Users/u/proj") + "".join(f"out {i}\n" for i in range(10))))
        self.assertIsNone(bottom_trust_path(" Yes, I trust this folder\n"))  # half drawn: no "Accessing workspace:"

    def test_at_trust_prompt_is_agent_specific_and_bottom_only(self):
        codex = fixture("codex_trust.txt")
        self.assertTrue(at_trust_prompt(CLAUDE_TRUST.format(path="/x"), "claude"))
        self.assertTrue(at_trust_prompt(CLAUDE_TRUST_LEGACY.format(path="/x"), "claude"))
        self.assertTrue(at_trust_prompt(codex, "codex"))
        self.assertFalse(at_trust_prompt(codex, "claude"))
        self.assertFalse(at_trust_prompt(CLAUDE_TRUST.format(path="/x"), "codex"))
        # a prompt left in the scrollback under a composer is not a prompt
        self.assertFalse(at_trust_prompt(CLAUDE_TRUST.format(path="/x") + COMPOSER, "claude"))
        self.assertFalse(at_trust_prompt(codex + fixture("codex_idle.txt"), "codex"))
        # other menus are prompts but not trust prompts
        self.assertFalse(at_trust_prompt("  Update available\n› 1. Update now\n  2. Skip\n\n  Press enter to continue\n", "codex"))
        self.assertFalse(at_trust_prompt("", "claude"))


if __name__ == "__main__":
    unittest.main()


class ClaudeInputStateTest(unittest.TestCase):
    def test_real_screens(self):
        for name, want in (("claude_empty_placeholder.ansi", "empty"), ("claude_empty_after_reply.ansi", "empty"),
                           ("claude_draft.ansi", "draft"), ("claude_pasted.ansi", "draft"),
                           ("claude_slash_menu.ansi", "blocked"), ("claude_trust.ansi", "blocked")):
            with self.subTest(name):
                self.assertEqual(claude_input_state(fixture(name)), want)

    def test_dim_slash_command_suggestion_is_empty(self):
        # Claude suggests the last slash command again as dim text in an empty box: nobody typed it
        screen = fixture("claude_slash_suggestion.ansi")
        self.assertIn("\x1b[2m/context-tools:summary\x1b[0m", screen)
        self.assertEqual(claude_input_state(screen), "empty")
        self.assertEqual(claude_input_state(screen.replace("\x1b[2m", "")), "blocked")  # typed: a slash menu
        typed = screen.replace("summary\x1b[0m", "summary\x1b[0m x")
        self.assertNotEqual(claude_input_state(typed), "empty")  # suggestion plus typed text: never pasted into

    def test_dim_numbered_option_is_still_blocked(self):
        rule = "─" * 40
        self.assertEqual(claude_input_state(f"{rule}\n❯\xa0\x1b[2m1. Yes\x1b[0m\n{rule}\n  footer"), "blocked")

    def test_placeholder_text_in_normal_intensity_is_a_draft(self):
        screen = fixture("claude_empty_placeholder.ansi").replace("\x1b[2m", "")
        self.assertIn("Try", screen)
        self.assertEqual(claude_input_state(screen), "draft")

    def test_unknown_screens_are_never_empty(self):
        rule = "─" * 40
        for screen in ("", "\n\n", "$ ls\nfoo", "no box here ❯\xa0", f"{rule}\n❯\xa0\n",  # one rule only
                       f"{rule}\n❯\xa01. Yes\n  2. No\n{rule}\nEsc to cancel",  # permission dialog
                       f"{rule}\n❯ \n{rule}\n  footer"):  # plain space, not NBSP
            with self.subTest(screen=screen):
                self.assertEqual(claude_input_state(screen), "blocked")

    def test_prompt_box_scrolled_away_from_the_bottom_is_blocked(self):
        rule = "─" * 40
        screen = f"{rule}\n❯\xa0\n{rule}\n" + "\n".join(f"output {i}" for i in range(8))
        self.assertEqual(claude_input_state(screen), "blocked")

    def test_minimal_empty(self):
        rule = "─" * 40
        self.assertEqual(claude_input_state(f"{rule}\n❯\xa0\n{rule}\n  footer"), "empty")

    def test_other_nonblank_line_inside_the_box_is_a_draft(self):
        lines = fixture("claude_empty_placeholder.ansi").split("\n")
        i = next(n for n, ln in enumerate(lines) if "❯" in ln)
        for extra in ("second line", "\x1b[2mdim line\x1b[0m"):
            with self.subTest(extra=extra):
                self.assertEqual(claude_input_state("\n".join(lines[:i] + [extra] + lines[i:])), "draft")  # blank-first draft: text above ❯ ...
                self.assertEqual(claude_input_state("\n".join(lines[:i + 1] + [extra] + lines[i + 1:])), "draft")

    def test_dim_placeholder_followed_by_normal_text_is_a_draft(self):
        screen = fixture("claude_empty_placeholder.ansi")
        self.assertIn('\x1b[0m', screen)
        i = screen.index('to..."\x1b[0m') + len('to..."\x1b[0m')
        self.assertEqual(claude_input_state(screen[:i] + "typed" + screen[i:]), "draft")


class ClaudeTitledBoxTest(unittest.TestCase):
    """Claude 2.1.3xx writes the session title into the top rule of the input box (「──── <title> ─」)."""

    def screen(self, title):
        top = f"\x1b[38;5;244m{'─' * 60}\x1b[39m \x1b[38;5;244m{title}\x1b[39m \x1b[38;5;244m─"
        return "\n".join(["⏺ done", "", top, "\x1b[39m❯\xa0\x1b[2m先查一下\x1b[0m",
                          "\x1b[38;5;244m" + "─" * 81, "  Opus · ~/proj · context 0%",
                          "  ⏵⏵ auto mode on (shift+tab to cycle)",
                          " " * 32 + "Update available! Run: brew upgrade claude-code"])

    def test_titled_top_rule_with_placeholder_is_empty(self):
        for title in ("工作標題", "new task? /clear to save 266.7k tokens"):
            with self.subTest(title=title):
                self.assertEqual(claude_input_state(self.screen(title)), "empty")

    def test_titled_top_rule_with_typed_text_is_a_draft(self):
        self.assertEqual(claude_input_state(self.screen("工作標題").replace("\x1b[2m", "")), "draft")

    def test_text_line_ending_in_a_rule_char_is_not_a_rule(self):
        rule = "─" * 40
        self.assertEqual(claude_input_state(f"see ─\n❯\xa0\n{rule}\n  footer"), "blocked")
