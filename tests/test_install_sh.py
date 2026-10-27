"""install.sh: syntax and the options that exit before doing anything. The real test is scripts/linux-smoke/install-test.sh."""
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"


def sh(*args):
    # a minimal environment with a throwaway HOME: nothing of the real machine is visible to the script
    with tempfile.TemporaryDirectory() as home:
        return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "HOME": home})


class InstallShTest(unittest.TestCase):
    def test_syntax(self):
        r = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_everything_runs_from_main_on_the_last_line(self):
        lines = SCRIPT.read_text().rstrip("\n").split("\n")
        self.assertEqual(lines[-1], 'main "$@"')
        self.assertTrue(lines[0].startswith("#!/bin/sh"))

    def test_help_exits_before_doing_anything(self):
        r = sh("--help")
        self.assertEqual(r.returncode, 0)
        for word in ("--yes", "--no-init", "--version"):
            self.assertIn(word, r.stdout)

    def test_unknown_option_fails(self):
        r = sh("--no-such-option")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown option", r.stderr)

    def test_version_needs_a_value(self):
        r = sh("--version")
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs a value", r.stderr)

    def test_path_hint_tests_the_original_path_and_init_runs_with_it(self):
        text = SCRIPT.read_text()
        self.assertIn("ORIG_PATH=\"$PATH\"", text)
        self.assertIn('case ":$ORIG_PATH:" in', text)
        self.assertIn('PATH="$ORIG_PATH" "$BIN_DIR/sampan" $init_args', text)
        self.assertNotIn('PATH="$BIN_DIR:$PATH"\n    export PATH', text)  # the adjusted PATH must not leak into the hint

    def test_installer_never_forces_over_foreign_executables(self):
        text = SCRIPT.read_text()
        self.assertIn("tool install --reinstall", text)
        self.assertNotIn("install --force", text)

    def test_version_without_v_is_normalised(self):
        self.assertIn('case "$VERSION" in "" | v*) ;; *) VERSION="v$VERSION" ;; esac', SCRIPT.read_text())

    def test_macos_checks_xcode_tools_and_probes_homebrew(self):
        text = SCRIPT.read_text()
        for needle in ("xcode-select -p", "xcode-select --install", "/opt/homebrew/bin", "/usr/local/bin"):
            self.assertIn(needle, text)


class StubbedInstallTest(unittest.TestCase):
    """Runs install.sh against stub tmux/uv/git in a throwaway PATH and HOME; the stubs only log their arguments."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        self.stub("tmux", "exit 0")
        self.stub("git", "exit 1")  # ls-remote fails: the latest-tag lookup falls back
        self.stub("uv", f'echo "$*" >> {self.log}\ncase "$1 $2" in\n'
                        '"tool dir") echo "$HOME/.local/bin" ;;\n'
                        f'"tool install") exit "${{STUB_UV_INSTALL_RC:-0}}" ;;\nesac')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def run_sh(self, *args, tty=None, install_rc="0", init_rc="0"):
        home = self.root / "home"
        (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(home), "STUB_UV_INSTALL_RC": install_rc,
               "SAMPAN_INSTALL_TTY": str(tty or self.root / "no-such-tty"), "STUB_INIT_RC": init_rc}
        return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, env=env)

    def with_init_stubs(self):
        """A fake claude on PATH, a readable tty file and a sampan stub that logs its arguments."""
        self.stub("claude", "exit 0")
        (self.root / "home" / ".local" / "bin").mkdir(parents=True, exist_ok=True)
        sampan = self.root / "home" / ".local" / "bin" / "sampan"
        sampan.write_text(f'#!/bin/sh\necho "sampan $*" >> {self.log}\n[ "$1" = init ] && exit "$STUB_INIT_RC"\nexit 0\n')
        sampan.chmod(0o755)
        tty = self.root / "tty"
        tty.write_text("\n")
        return tty

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_fallback_version_is_installed_with_reinstall_when_the_lookup_fails(self):
        self.run_sh("--no-init")
        self.assertTrue(any(c.startswith("tool install --reinstall git+") and c.endswith("@v0.13.4") for c in self.calls()), self.calls())
        self.assertFalse([c for c in self.calls() if "uninstall" in c])

    def test_failed_install_fails_the_script(self):
        r = self.run_sh("--yes", "--no-init", install_rc="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("failed", r.stderr)

    def test_version_without_v_gets_a_v(self):
        self.run_sh("--yes", "--no-init", "--version", "0.6.1")
        self.assertTrue(any(c.endswith("@v0.6.1") for c in self.calls()), self.calls())
        self.log.unlink()
        self.run_sh("--yes", "--no-init", "--version=v0.6.2")
        self.assertTrue(any(c.endswith("@v0.6.2") for c in self.calls()), self.calls())

    def test_yes_is_passed_through_to_init(self):
        tty = self.with_init_stubs()
        self.run_sh("--yes", tty=tty)
        self.assertIn("sampan init --yes", self.calls())
        self.log.unlink()
        self.run_sh(tty=tty)
        self.assertIn("sampan init", self.calls())
        self.assertNotIn("sampan init --yes", self.calls())

    def test_failing_init_does_not_abort_the_installer(self):
        tty = self.with_init_stubs()
        r = self.run_sh("--yes", tty=tty, init_rc="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sampan init did not finish (exit 2). Run 'sampan init' yourself in a terminal.", r.stdout)
        self.assertIn("sampan --version", self.calls())


if __name__ == "__main__":
    unittest.main()
