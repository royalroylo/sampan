import os
import subprocess
import unittest

from sampan import runner
from tests.fakes import FakeRunner, UnmatchedCommand, cp
from tests.guard import RealCommandBlocked, allow_real_commands, blocked_calls, check


class GuardTest(unittest.TestCase):
    def _expect_blocked(self, fn):
        with self.assertRaises(RealCommandBlocked):
            fn()
        blocked_calls.clear()  # deliberate block: do not fail the run at exit

    def test_real_tmux_is_blocked(self):
        self._expect_blocked(lambda: runner.run(["tmux", "ls"]))

    def test_blocked_through_every_call_point(self):
        self._expect_blocked(lambda: subprocess.run(["tmux", "ls"]))
        self._expect_blocked(lambda: subprocess.Popen(["claude", "--version"]))
        self._expect_blocked(lambda: subprocess.call(["codex"]))
        self._expect_blocked(lambda: os.execvp("tmux", ["tmux", "attach"]))
        self._expect_blocked(lambda: os.execv("/opt/homebrew/bin/tmux", ["tmux"]))

    def test_absolute_path_matches_on_basename(self):
        self._expect_blocked(lambda: runner.run(["/usr/sbin/lsof", "-p", "1"]))

    def test_all_listed_commands_blocked(self):
        for cmd in ("tmux", "claude", "codex", "lsof", "launchctl", "uv", "osascript"):
            self._expect_blocked(lambda cmd=cmd: runner.run([cmd]))

    def test_block_is_not_swallowed_by_ordinary_handlers(self):
        # runner.run catches FileNotFoundError, callers often catch OSError/Exception
        def swallow():
            try:
                runner.run(["tmux", "ls"])
            except Exception:
                return "swallowed"
        self._expect_blocked(swallow)

    def test_git_network_and_mutating_blocked_local_read_allowed(self):
        for sub in ("ls-remote", "fetch", "clone", "push", "pull"):
            self._expect_blocked(lambda sub=sub: runner.run(["git", sub]))
        self._expect_blocked(lambda: runner.run(["git", "-C", "/tmp", "push", "origin"]))
        self.assertEqual(runner.run(["git", "--version"]).returncode, 0)

    def test_blocked_command_hidden_in_a_shell_string(self):
        self._expect_blocked(lambda: check(["sh", "-c", "tmux ls"]))
        self._expect_blocked(lambda: check(["/bin/bash", "-c", "echo hi && tmux ls"]))
        self._expect_blocked(lambda: check(["zsh", "-lc", "cd /tmp;/opt/homebrew/bin/tmux kill-server"]))
        self._expect_blocked(lambda: check("echo hi | launchctl list"))
        self._expect_blocked(lambda: subprocess.run("true && tmux ls", shell=True))
        check(["sh", "-c", "echo ok"])  # no raise
        check(["bash", "script.sh", "tmux"])  # a script argument is not a -c command string
        check("echo ok")

    def test_opt_out_allows_one_command(self):
        with allow_real_commands("uv"):
            check(["/opt/homebrew/bin/uv", "--version"])  # no raise; nothing is executed
        self._expect_blocked(lambda: runner.run(["uv", "--version"]))


class StrictFakeRunnerTest(unittest.TestCase):
    def test_strict_fakerunner_raises_on_unmatched(self):
        r = FakeRunner(strict=True).on(["tmux", "-u", "has-session"], cp())
        with self.assertRaises(UnmatchedCommand):
            r(["tmux", "-u", "kill-session", "-t", "x"])

    def test_strict_answers_matching_rule(self):
        r = FakeRunner(strict=True).on(["tmux", "-u", "has-session"], cp())
        self.assertEqual(r(["tmux", "-u", "has-session", "-t", "x"]).returncode, 0)

    def test_non_strict_default_unchanged(self):
        self.assertEqual(FakeRunner()(["whatever"]).returncode, 0)

    def test_sent_keys_lists_send_keys_args(self):
        r = FakeRunner()
        r(["tmux", "-u", "send-keys", "-t", "s:1", "claude", "Enter"])
        r(["tmux", "-u", "display-message", "-p", "x"])
        r(["/opt/homebrew/bin/tmux", "-u", "send-keys", "-t", "s:1", "-l", "hi"])
        self.assertEqual(r.sent_keys(), [["-t", "s:1", "claude", "Enter"], ["-t", "s:1", "-l", "hi"]])


if __name__ == "__main__":
    unittest.main()
