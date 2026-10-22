import unittest

from sampan.deps import BREW_INSTALL, check, ensure

NONE = lambda _p: False  # noqa: E731  don't depend on whether this machine really has /opt/homebrew/bin/brew


def finder(*present):
    return lambda name: f"/opt/homebrew/bin/{name}" if name in present else None


class CheckTest(unittest.TestCase):
    def levels(self, which, path="/h/.local/bin:/usr/bin"):
        return {d.name: d.level for d in check(which, {"PATH": path}, "/h", exists=NONE, platform="darwin")}

    def test_all_present(self):
        self.assertEqual(self.levels(finder("brew", "tmux", "claude", "codex")),
                         {"Homebrew": "ok", "tmux": "ok", "claude": "ok", "codex": "ok", "PATH": "ok"})

    def test_missing_levels(self):
        lv = self.levels(finder(), path="/usr/bin")
        self.assertEqual(lv, {"Homebrew": "warn", "tmux": "fail", "claude": "fail", "codex": "info", "PATH": "warn"})

    def test_brew_found_outside_path(self):
        deps = check(finder(), {"PATH": "/usr/bin"}, "/h", exists=lambda p: p == "/opt/homebrew/bin/brew", platform="darwin")
        self.assertEqual(next(d for d in deps if d.name == "Homebrew").level, "ok")

    def test_hints(self):
        deps = {d.name: d for d in check(finder(), {"PATH": "/usr/bin"}, "/h", exists=NONE, platform="darwin")}
        self.assertIn(BREW_INSTALL, deps["Homebrew"].hint)
        self.assertIn("brew install tmux", deps["tmux"].hint)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', deps["PATH"].hint)


class NonMacTest(unittest.TestCase):
    def test_no_homebrew_check_and_tmux_hint_is_generic(self):
        deps = {d.name: d for d in check(finder(), {"PATH": "/usr/bin"}, "/h", exists=lambda p: True, platform="linux")}
        self.assertNotIn("Homebrew", deps)
        self.assertEqual(deps["tmux"].level, "fail")
        self.assertNotIn("brew", deps["tmux"].hint)

    def test_ensure_never_offers_brew_install(self):
        asked, installed = [], []
        ok = ensure(lambda _l: None, lambda p: asked.append(p) or "y", which=finder("claude"),
                    env={"PATH": "/h/.local/bin"}, home="/h", exists=lambda p: True, platform="linux",
                    install=lambda a: installed.append(a) or 0)
        self.assertFalse(ok)
        self.assertEqual((asked, installed), ([], []))


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.lines, self.installed = [], []

    def ensure(self, present, answers=("",), **kw):
        answers = iter(answers)
        found = set(present)

        def install(args):
            self.installed.append(args)
            found.add("tmux")
            return 0
        return ensure(self.lines.append, lambda _p: next(answers), which=lambda n: finder(*found)(n),
                      env={"PATH": "/h/.local/bin"}, home="/h", install=install, exists=NONE, platform="darwin", **kw)

    def text(self):
        return "\n".join(self.lines)

    def test_all_present_passes_quietly(self):
        self.assertTrue(self.ensure({"brew", "tmux", "claude"}))
        self.assertEqual(self.installed, [])
        self.assertIn("✔ tmux", self.text())

    def test_offers_brew_install_tmux_and_rechecks(self):
        self.assertTrue(self.ensure({"brew", "claude"}, answers=[""]))
        self.assertEqual(self.installed, [["/opt/homebrew/bin/brew", "install", "tmux"]])

    def test_declined_tmux_install_stops(self):
        self.assertFalse(self.ensure({"brew", "claude"}, answers=["n"]))
        self.assertEqual(self.installed, [])
        self.assertIn("brew install tmux", self.text())

    def test_no_brew_no_tmux_stops_with_brew_hint(self):
        self.assertFalse(self.ensure({"claude"}))
        self.assertIn(BREW_INSTALL, self.text())
        self.assertEqual(self.installed, [])

    def test_tmux_without_brew_is_fine(self):
        self.assertTrue(self.ensure({"tmux", "claude"}))

    def test_missing_claude_stops(self):
        self.assertFalse(self.ensure({"brew", "tmux"}))
        self.assertIn("claude.ai/install.sh", self.text())

    def test_missing_codex_only_informs(self):
        self.assertTrue(self.ensure({"brew", "tmux", "claude"}))
        self.assertIn("codex", self.text())

    def test_failed_install_stops(self):
        def install(args):
            self.installed.append(args)
            return 1
        ok = ensure(self.lines.append, lambda _p: "", which=finder("brew", "claude"), env={"PATH": "/h/.local/bin"},
                    home="/h", install=install, exists=NONE, platform="darwin")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
