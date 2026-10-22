import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sampan import cli
from tests.helpers import make_env


class CliTest(unittest.TestCase):
    def setUp(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        p = mock.patch("sampan.cli.load_config", return_value=cfg)
        p.start()
        self.addCleanup(p.stop)
        # Same result inside or outside tmux, and never a real `tmux display-message`.
        p = mock.patch.dict("os.environ")
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)

    def test_version_flag_prints_version_without_config(self):
        buf = io.StringIO()
        with mock.patch("sampan.cli.version", return_value="0.2.0"), redirect_stdout(buf), \
                self.assertRaises(SystemExit) as e:
            cli.main(["--version"])
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), "sampan 0.2.0")

    def test_version_falls_back_when_not_installed(self):
        from importlib.metadata import PackageNotFoundError
        with mock.patch("sampan.update.metadata_version", side_effect=PackageNotFoundError):
            self.assertEqual(cli.version(), "unknown")

    def test_cli_without_subcommand_runs_ui(self):
        with mock.patch("sampan.ui.cmd_ui", return_value=0) as ui:
            self.assertEqual(cli.main([]), 0)
        self.assertEqual(ui.call_count, 1)

    def test_ui_plain_never_gets_curses_pickers(self):
        with mock.patch("sampan.ui.cmd_ui", return_value=0) as ui:
            cli.main(["ui", "--plain"])
        self.assertIsNone(ui.call_args.kwargs["picker_fn"])
        self.assertIsNone(ui.call_args.kwargs["restore_picker_fn"])
        self.assertIsNone(ui.call_args.kwargs["adopt_picker_fn"])
        self.assertTrue(ui.call_args.kwargs["loop"])

    def test_ui_on_a_terminal_uses_one_curses_screen(self):
        with mock.patch("sampan.ui.curses_ui", return_value=0) as cu, mock.patch("sampan.ui.cmd_ui") as ui, \
                mock.patch("sampan.update.Checker.start") as start, \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True):
            self.assertEqual(cli.main(["ui"]), 0)
        self.assertEqual(cu.call_count, 1)
        self.assertEqual(ui.call_count, 0)
        self.assertEqual(start.call_count, 1)  # background new-version check (replaced here, no network)
        self.assertIsNotNone(cu.call_args.kwargs["updater"])

    def test_ui_plain_only_reads_update_cache(self):
        with mock.patch("sampan.ui.cmd_ui", return_value=0) as ui, mock.patch("sampan.update.Checker.start") as start, \
                mock.patch("sampan.update.latest_tag") as net:
            cli.main(["ui", "--plain"])
        self.assertEqual(start.call_count + net.call_count, 0)
        self.assertIsNotNone(ui.call_args.kwargs["updater"])

    def test_save_does_not_check_for_updates(self):
        with mock.patch("sampan.save.cmd_save", return_value=0), mock.patch("sampan.update.latest_tag") as net, \
                mock.patch("sampan.update.Checker") as checker:
            cli.main(["save", "--quiet"])
        self.assertEqual(net.call_count + checker.call_count, 0)

    def test_new_and_restore_ask_about_prompts_only_on_a_terminal(self):
        for tty in (False, True):
            with mock.patch("sys.stdin.isatty", return_value=tty), mock.patch("sys.stdout.isatty", return_value=tty), \
                    mock.patch("sampan.restore.cmd_restore", return_value=0) as rs, \
                    mock.patch("sampan.session_run.cmd_new", return_value=0) as nw:
                cli.main(["restore"])
                cli.main(["new", "x", "codex", "/tmp"])
            for m in (rs, nw):
                self.assertEqual(m.call_args.kwargs["on_prompt"] is not None, tty)

    def test_going_in_from_a_cli_prompt_attaches(self):
        from sampan.prompts import ATTACH

        def new(cfg, run, out, name, target, cwd, dry_run=False, on_prompt=None):
            on_prompt(name, "codex", None)
            return 1
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("sampan.prompts.plain_prompt", return_value=lambda *a: ATTACH), \
                mock.patch("sampan.session_run.cmd_new", new), mock.patch("sampan.ui._attach", return_value=0) as at, \
                mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("TMUX", None)
            self.assertEqual(cli.main(["new", "x", "codex", "/tmp"]), 1)
        self.assertEqual(at.call_args.args[2:4], ("x", None))

    def test_ui_plain_gets_a_prompt_fn(self):
        with mock.patch("sampan.ui.cmd_ui", return_value=0) as ui:
            cli.main(["ui", "--plain"])
        self.assertIsNotNone(ui.call_args.kwargs["prompt_fn"])

    def test_adopt_dry_run(self):
        with mock.patch("sampan.adopt.cmd_adopt", return_value=0) as ad, mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["adopt", "--dry-run"]), 0)
        self.assertTrue(ad.call_args.kwargs["dry_run"])
        self.assertIsNone(ad.call_args.kwargs["picker_fn"])

    def test_commands_are_top_level(self):
        with mock.patch("sampan.restore.cmd_restore", return_value=0) as rs:
            cli.main(["restore", "a", "--all", "--dry-run"])
        self.assertEqual((rs.call_args.kwargs["names"], rs.call_args.kwargs["select_all"],
                          rs.call_args.kwargs["dry_run"]), (("a",), True, True))
        with mock.patch("sampan.session_run.cmd_new", return_value=0) as nw:
            cli.main(["new", "x", "codex", "/tmp"])
        self.assertEqual(nw.call_args.args[3:6], ("x", "codex", "/tmp"))

    def test_session_group_is_gone(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli.main(["session", "save"])

    def test_start_shows_rename_notice_with_any_args(self):
        for argv in (["start"], ["start", "--profile", "x", "--dry-run"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(argv)
            self.assertEqual(rc, 2)
            self.assertIn("sampan restore", buf.getvalue())

    def test_ctrl_c_is_a_quiet_cancel(self):
        with mock.patch("sampan.ui.cmd_ui", side_effect=KeyboardInterrupt), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["ui"]), 130)

    def test_init_runs_without_a_config(self):
        tmp = Path(tempfile.mkdtemp())
        env = {"SAMPAN_CONFIG": str(tmp / "none.toml"), "SAMPAN_STATE_DIR": str(tmp / "state")}
        with mock.patch.dict("os.environ", env), mock.patch("sampan.cli.load_config", side_effect=AssertionError), \
                mock.patch("sampan.init.cmd_init", return_value=0) as init:
            self.assertEqual(cli.main(["init"]), 0)
        self.assertEqual(init.call_args.args[:2], (tmp / "none.toml", tmp / "state"))
        self.assertIn("sampan_path", init.call_args.kwargs)
        self.assertFalse(init.call_args.kwargs["no_launchd"])
        with mock.patch.dict("os.environ", env), mock.patch("sampan.init.cmd_init", return_value=0) as init:
            cli.main(["init", "--no-launchd"])
        self.assertTrue(init.call_args.kwargs["no_launchd"])

    def test_profile_add_parses_dir(self):
        with mock.patch("sampan.init.cmd_profile_add", return_value=0) as add:
            cli.main(["profile", "add", "side", "--dir", "/Users/u/x"])
            cli.main(["profile", "add", "other"])
        self.assertEqual(add.call_args_list[0].args[3], "side")
        self.assertEqual(add.call_args_list[0].kwargs["dir"], Path("/Users/u/x"))
        self.assertIsNone(add.call_args_list[1].kwargs["dir"])


class EntryPointTest(unittest.TestCase):
    def test_pyproject_declares_only_the_sampan_entry_point(self):
        import tomllib
        data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["name"], "sampan")
        self.assertEqual(data["project"]["scripts"], {"sampan": "sampan.cli:main"})


class HookCliTest(unittest.TestCase):
    def test_hook_entry_point_is_silent_with_garbage_input_and_broken_config(self):
        # Review Focus 1, via the CLI entry point: stdout goes into the conversation, a failure blocks claude from starting
        tmp = Path(tempfile.mkdtemp())
        (tmp / "broken.toml").write_text("this is [not toml")
        env = {"SAMPAN_CONFIG": str(tmp / "broken.toml"), "SAMPAN_STATE_DIR": str(tmp / "state")}
        for stdin in ("\x00garbage{", "", '{"session_id": null}'):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict("os.environ", env), mock.patch("sys.stdin", io.StringIO(stdin)), \
                    mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                    self.assertRaises(SystemExit) as cm:
                cli.main(["hook", "session-start"])
            self.assertEqual((cm.exception.code, out.getvalue(), err.getvalue()), (0, "", ""), stdin)


if __name__ == "__main__":
    unittest.main()


class ModuleEntryTest(unittest.TestCase):
    def test_python_dash_m_sampan_runs_cli_main(self):
        import runpy
        import sys
        with mock.patch("sampan.cli.main", return_value=7) as main, mock.patch.object(sys, "argv", ["sampan"]):
            with self.assertRaises(SystemExit) as cm:
                runpy.run_module("sampan", run_name="__main__")
        self.assertEqual(cm.exception.code, 7)
        main.assert_called_once_with()
