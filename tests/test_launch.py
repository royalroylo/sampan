import dataclasses
import io
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sampan.launch import claude_argv, cmd_claude, cmd_codex, codex_argv, launch_line, remote_control_on, shell_command
from tests.helpers import make_env

A = "aaaaaaaa-0000-4000-8000-000000000001"


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.alice = self.cfg.profiles["alice"]

    def test_claude_argv_variants(self):
        self.assertEqual(claude_argv(self.alice), ["claude"])
        self.assertEqual(claude_argv(self.alice, resume=A), ["claude", "--resume", A])
        self.assertEqual(claude_argv(self.alice, session_id=A, extra=["-p", "x"]),
                         ["claude", "--session-id", A, "-p", "x"])

    def test_codex_argv_variants(self):
        self.assertEqual(codex_argv(), ["codex"])
        self.assertEqual(codex_argv(resume=A, extra=["--x"]), ["codex", "resume", A, "--x"])

    def test_resume_and_session_id_are_exclusive(self):
        with self.assertRaises(ValueError):
            claude_argv(self.alice, resume=A, session_id=A)

    def test_shell_command_quotes_spaces_and_cjk(self):
        cmd = shell_command(["claude", "--resume", A], env_set={}, env_unset=("CLAUDE_CONFIG_DIR",),
                            cwd="/Users/u/我的 專案")
        self.assertTrue(cmd.startswith("cd '/Users/u/我的 專案' && "))
        self.assertEqual(shlex.split(cmd.split(" && ", 1)[1]),
                         ["env", "-u", "CLAUDE_CONFIG_DIR", "claude", "--resume", A])

    def test_resume_with_invalid_id_is_refused(self):
        # An empty or non-UUID id would make claude/codex open a new conversation (or pick an old one): always block it
        for agent, prof in (("claude", "alice"), ("codex", "")):
            for bad in ("", "None", "../x", "*", A.upper() + "x"):
                with self.assertRaises(ValueError, msg=(agent, bad)):
                    launch_line(self.cfg, agent, prof, resume=bad)
        self.assertIn(A, launch_line(self.cfg, "codex", "", resume=A))

    def test_default_profile_unsets_config_dir(self):
        self.assertEqual(launch_line(self.cfg, "claude", "alice", resume=A),
                         f"env -u CLAUDE_CONFIG_DIR claude --resume {A}")

    def test_prompt_file_is_the_startup_prompt_and_quoted_inside(self):
        pf = Path("/tmp/a b/x.txt")
        self.assertEqual(launch_line(self.cfg, "claude", "alice", session_id=A, prompt_file=pf),
                         f"env -u CLAUDE_CONFIG_DIR claude --session-id {A} \"$(cat '/tmp/a b/x.txt')\"")
        self.assertEqual(launch_line(self.cfg, "codex", "", cwd="/p", prompt_file=pf),
                         "cd /p && codex -C /p \"$(cat '/tmp/a b/x.txt')\"")
        self.assertEqual(launch_line(self.cfg, "claude", "alice", session_id=A), f"env -u CLAUDE_CONFIG_DIR claude --session-id {A}")

    def test_name_is_passed_to_claude_after_resume(self):
        self.assertEqual(launch_line(self.cfg, "claude", "alice", resume=A, name="proj"),
                         f"env -u CLAUDE_CONFIG_DIR claude --resume {A} --name proj")
        self.assertEqual(launch_line(self.cfg, "claude", "alice", session_id=A, name="proj"),
                         f"env -u CLAUDE_CONFIG_DIR claude --session-id {A} --name proj")
        self.assertEqual(claude_argv(None, resume=A, name="n", extra=("x",)), ["claude", "--resume", A, "--name", "n", "x"])

    def test_name_with_space_and_cjk_is_quoted(self):
        line = launch_line(self.cfg, "claude", "alice", resume=A, name="rc probe 名稱")
        self.assertIn("--name 'rc probe 名稱'", line)
        self.assertEqual(shlex.split(line)[-2:], ["--name", "rc probe 名稱"])

    def test_codex_never_gets_name(self):
        self.assertNotIn("--name", launch_line(self.cfg, "codex", "", resume=A, name="proj"))

    def test_no_name_means_no_flag(self):
        self.assertNotIn("--name", launch_line(self.cfg, "claude", "alice", resume=A))

    def test_other_profile_sets_config_dir(self):
        self.assertEqual(launch_line(self.cfg, "claude", "work", session_id=A, cwd="/Users/u/p"),
                         f"cd /Users/u/p && env CLAUDE_CONFIG_DIR={self.cfg.profiles['work'].config_dir} claude --session-id {A}")

    def codex_home(self, home):
        return dataclasses.replace(self.cfg, codex=dataclasses.replace(self.cfg.codex, home=Path(home)))

    def test_codex_home_only_when_not_default(self):
        default = self.codex_home(self.cfg.home / ".codex")
        # Compare only against ~/.codex: the caller's own CODEX_HOME doesn't count (it isn't necessarily set in a tmux pane)
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/elsewhere"}):
            self.assertEqual(launch_line(default, "codex", "", resume=A), f"codex resume {A}")
            self.assertEqual(launch_line(self.codex_home("/elsewhere"), "codex", None), "env CODEX_HOME=/elsewhere codex")

    def test_codex_resume_in_other_dir_passes_cd(self):
        # codex 0.147 on a real machine: resuming from another directory pops up the "Choose working directory" menu; passing -C avoids the question
        line = launch_line(self.cfg, "codex", "", resume=A, cwd="/Users/u/我的 專案")
        self.assertTrue(line.startswith("cd '/Users/u/我的 專案' && "))
        self.assertEqual(shlex.split(line.split(" && ", 1)[1]), ["codex", "resume", A, "-C", "/Users/u/我的 專案"])

    def test_non_default_codex_home_without_env(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(launch_line(self.codex_home("/Users/u/cx"), "codex", "", resume=A),
                             f"env CODEX_HOME=/Users/u/cx codex resume {A}")

    def test_cmd_claude_picker_and_non_tty_error(self):
        msgs, calls = [], []
        ex = lambda f, a, e: calls.append((f, a, e))
        self.assertEqual(cmd_claude(self.cfg, None, msgs.append, lambda p: "2", None, ["--resume", "x"],
                                    execvpe=ex, isatty=lambda: True), 0)
        self.assertIn("  2) work  — work@example.com", msgs)
        f, argv, env = calls[0]
        self.assertEqual((f, argv), ("claude", ["claude", "--resume", "x"]))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.cfg.profiles["work"].config_dir))
        # an invalid choice, a non-tty and an unknown profile all skip exec
        self.assertEqual(cmd_claude(self.cfg, None, msgs.append, lambda p: "9", None, [], execvpe=ex, isatty=lambda: True), 1)
        self.assertEqual(cmd_claude(self.cfg, None, msgs.append, lambda p: "1", None, [], execvpe=ex, isatty=lambda: False), 2)
        self.assertEqual(cmd_claude(self.cfg, None, msgs.append, input, "nobody", [], execvpe=ex), 2)
        self.assertEqual(len(calls), 1)

    def test_cmd_claude_eof_at_picker_is_a_cancel(self):
        msgs, calls = [], []

        def eof(_p):
            raise EOFError
        rc = cmd_claude(self.cfg, None, msgs.append, eof, None, [], execvpe=lambda *a: calls.append(a),
                        isatty=lambda: True)
        self.assertEqual((rc, calls), (1, []))
        self.assertIn("已取消", msgs)

    def test_cmd_claude_default_profile_drops_config_dir(self):
        calls = []
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/x"}):
            cmd_claude(self.cfg, None, lambda m: None, input, "alice", [], execvpe=lambda f, a, e: calls.append(e))
        self.assertNotIn("CLAUDE_CONFIG_DIR", calls[0])

    def test_cmd_codex_execs(self):
        calls = []
        cmd_codex(self.cfg, ["--x"], execvpe=lambda f, a, e: calls.append((f, a)))
        self.assertEqual(calls, [("codex", ["codex", "--x"])])

    def test_cmd_codex_refuses_when_codex_is_off(self):
        from dataclasses import replace
        cfg = replace(self.cfg, codex=replace(self.cfg.codex, enabled=False))
        calls, lines = [], []
        rc = cmd_codex(cfg, [], out=lines.append, execvpe=lambda f, a, e: calls.append((f, a)))
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertEqual(lines, ["❌ Codex 已關閉（沒有使用中的 Codex profile）"])


class RemoteControlTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        base = self.cfg.profiles["alice"]
        d = Path(tempfile.mkdtemp())
        self.p = dataclasses.replace(base, config_dir=d / "cfg", claude_json=d / "cj.json")
        self.p.config_dir.mkdir()

    def settings(self, text):
        (self.p.config_dir / "settings.json").write_text(text)

    def legacy(self, text):
        self.p.claude_json.write_text(text)

    def test_settings_true_is_on(self):
        self.settings('{"remoteControlAtStartup": true}')
        self.assertTrue(remote_control_on(self.p))

    def test_settings_wins_over_legacy(self):
        self.settings('{"remoteControlAtStartup": false}')
        self.legacy('{"remoteControlAtStartup": true}')
        self.assertFalse(remote_control_on(self.p))

    def test_settings_true_wins_over_legacy_false(self):
        self.settings('{"remoteControlAtStartup": true}')
        self.legacy('{"remoteControlAtStartup": false}')
        self.assertTrue(remote_control_on(self.p))

    def test_legacy_used_when_settings_has_no_key(self):
        self.settings('{"other": 1}')
        self.legacy('{"remoteControlAtStartup": true}')
        self.assertTrue(remote_control_on(self.p))

    def test_legacy_used_when_settings_missing(self):
        self.legacy('{"remoteControlAtStartup": true}')
        self.assertTrue(remote_control_on(self.p))

    def test_both_missing_is_off(self):
        self.assertFalse(remote_control_on(self.p))

    def test_broken_json_is_off(self):
        self.settings("{nope")
        self.legacy("{nope")
        self.assertFalse(remote_control_on(self.p))

    def test_non_bool_is_off(self):
        self.settings('{"remoteControlAtStartup": "true"}')
        self.assertFalse(remote_control_on(self.p))
        self.settings('{"remoteControlAtStartup": 1}')
        self.assertFalse(remote_control_on(self.p))
        self.settings("[1]")
        self.assertFalse(remote_control_on(self.p))

    def test_claude_argv_order(self):
        self.assertEqual(claude_argv(self.p, resume=A, name="n", remote_control="n", extra=("x",)),
                         ["claude", "--resume", A, "--name", "n", "--remote-control", "n", "x"])
        self.assertEqual(claude_argv(self.p, resume=A, name="n"), ["claude", "--resume", A, "--name", "n"])

    def test_launch_line_follows_the_profile(self):
        cfg = dataclasses.replace(self.cfg, profiles={**self.cfg.profiles, "alice": self.p})
        self.assertNotIn("--remote-control", launch_line(cfg, "claude", "alice", resume=A, name="proj"))
        self.settings('{"remoteControlAtStartup": true}')
        self.assertEqual(launch_line(cfg, "claude", "alice", resume=A, name="proj"),
                         f"env -u CLAUDE_CONFIG_DIR claude --resume {A} --name proj --remote-control proj")
        self.assertNotIn("--remote-control", launch_line(cfg, "claude", "alice", resume=A))
        self.assertNotIn("--remote-control", launch_line(cfg, "codex", "", resume=A, name="proj"))
        line = launch_line(cfg, "claude", "alice", resume=A, name="rc probe 名稱")
        self.assertEqual(shlex.split(line)[-4:], ["--name", "rc probe 名稱", "--remote-control", "rc probe 名稱"])
        self.assertIn("--remote-control 'rc probe 名稱'", line)


class CliPassthroughTest(unittest.TestCase):
    def test_flags_pass_through_to_claude(self):
        from sampan import cli
        tmp = Path(tempfile.mkdtemp())
        cfg = make_env(tmp)
        seen = []
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch("sampan.launch.cmd_claude", lambda *a, **k: seen.append(a[4:]) or 0):
            cli.main(["claude", "--resume", "x"])
            cli.main(["claude", "work", "--resume", "x", "-p"])
        self.assertEqual(seen, [(None, ["--resume", "x"]), ("work", ["--resume", "x", "-p"])])

    def test_unknown_profile_word_is_rejected_not_sent_as_prompt(self):
        from sampan import cli
        cfg = make_env(Path(tempfile.mkdtemp()))
        seen, buf = [], io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=cfg), redirect_stdout(buf), \
                mock.patch("sampan.launch.cmd_claude", lambda *a, **k: seen.append(a[4:]) or 0):
            self.assertEqual(cli.main(["claude", "hello", "world"]), 2)
        self.assertEqual(seen, [])
        self.assertIn("沒有這個 profile：hello（可用：alice, work）", buf.getvalue())
