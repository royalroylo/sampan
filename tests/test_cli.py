import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from sampan import cli
from sampan.send import TERMINAL, Caller
from tests.fakes import FakeRunner, cp, ps_line
from tests.helpers import make_env, register


class CliTest(unittest.TestCase):
    def setUp(self):
        cfg = self.cfg = make_env(Path(tempfile.mkdtemp()))
        p = mock.patch("sampan.cli.load_config", return_value=cfg)  # never the host's config
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

    def test_ui_on_a_terminal_gets_a_daemon_watcher_plain_does_not(self):
        from sampan.ui.daemon_watch import DaemonWatcher
        with mock.patch("sampan.ui.curses_ui", return_value=0) as cu, mock.patch("sampan.update.Checker.start"), \
                mock.patch("sampan.ui.daemon_watch.DaemonWatcher.start") as wstart, \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True):
            cli.main(["ui"])
        self.assertIsInstance(cu.call_args.kwargs["watcher"], DaemonWatcher)
        self.assertEqual(wstart.call_count, 0)  # curses_ui starts it (replaced here): no thread in this test
        with mock.patch("sampan.ui.cmd_ui", return_value=0) as ui, \
                mock.patch("sampan.ui.daemon_watch.DaemonWatcher.__init__") as init:
            cli.main(["ui", "--plain"])
        self.assertNotIn("watcher", ui.call_args.kwargs)
        self.assertEqual(init.call_count, 0)

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

    def test_folder_commands_dispatch(self):
        with mock.patch("sampan.folder_cmd.cmd_folder", return_value=0) as fc:
            self.assertEqual(cli.main(["folder", "add", "客戶/甲"]), 0)
            self.assertEqual(fc.call_args.args[2:], ("add", ["客戶/甲"]))
            self.assertFalse(fc.call_args.kwargs["merge"])
            cli.main(["folder", "move", "a", "b", "/", "--merge"])
            self.assertEqual(fc.call_args.args[2:], ("move", ["a", "b", "/"]))
            self.assertTrue(fc.call_args.kwargs["merge"])
            cli.main(["folder", "list"])
            self.assertEqual(fc.call_args.args[2:], ("list", []))

    def test_cli_restart_nothing_to_do_rc1(self):
        from sampan.status import State
        from tests.helpers import status_row
        rows = [status_row("a", State.RETIRED)]
        out = io.StringIO()
        with mock.patch("sampan.status.status_rows", return_value=rows), \
                mock.patch("sampan.restart.cmd_restart") as cr, redirect_stdout(out):
            self.assertEqual(cli.main(["restart", "a", "zz"]), 1)
        cr.assert_not_called()
        self.assertIn("略過：a（retired）、zz（找不到）", out.getvalue())

    def test_new_passes_folder_only_when_given(self):
        with mock.patch("sampan.session_run.cmd_new", return_value=0) as nw:
            cli.main(["new", "x", "claude:alice", "/tmp", "--folder", "開發"])
            self.assertEqual(nw.call_args.kwargs["folder"], "開發")
            cli.main(["new", "x", "claude:alice", "/tmp"])
            self.assertNotIn("folder", nw.call_args.kwargs)

    def test_new_says_where_the_session_is_once_it_runs(self):
        for rc, argv, said in ((0, [], True), (1, [], False), (0, ["--dry-run"], False)):
            buf = io.StringIO()
            with mock.patch("sampan.session_run.cmd_new", return_value=rc), redirect_stdout(buf), \
                    mock.patch("sys.stdin.isatty", return_value=False):
                self.assertEqual(cli.main(["new", "wsl-test", "claude:alice", "/tmp", *argv]), rc)
            line = "✔ wsl-test 在背景執行；要進去：sampan 畫面選它，或 tmux attach -t wsl-test"
            self.assertEqual(line in buf.getvalue(), said, (rc, argv, buf.getvalue()))
        buf = io.StringIO()  # "go in" was chosen at a prompt: it attaches, so no line about the background
        with mock.patch("sampan.cli._prompt_fn", return_value=(None, ["wsl-test"])), \
                mock.patch("sampan.session_run.cmd_new", return_value=0), mock.patch("sampan.cli._go_in", return_value=0), \
                redirect_stdout(buf):
            cli.main(["new", "wsl-test", "claude:alice", "/tmp"])
        self.assertNotIn("在背景執行", buf.getvalue())

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

    def test_switch_parses_and_passes_own_pane(self):
        os.environ["TMUX_PANE"] = "%4"
        with mock.patch("sampan.session_run.cmd_switch", return_value=0) as sw, \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["switch", "a", "work", "--dry-run", "--timeout", "5"]), 0)
        self.assertEqual(sw.call_args.args[3:5], ("a", "work"))
        kw = sw.call_args.kwargs
        self.assertEqual((kw["dry_run"], kw["timeout"], kw["own_pane"], kw["on_prompt"]), (True, 5, "%4", None))

    def test_cli_rename_by_id(self):
        from sampan.registry import Row, write_rows
        write_rows(self.cfg.registry_path, [Row("a", "claude", "alice", "sa", "/c", id="abcdef12-0000-4000-8000-000000000001"),
                                            Row("b", "claude", "alice", "sb", "/c", id="12345678-0000-4000-8000-000000000002")],
                   "t", self.cfg.history_dir)
        with mock.patch("sampan.session_run.cmd_rename", return_value=0) as rn:
            self.assertEqual(cli.main(["rename", "12345678-0000-4000-8000-000000000002", "z"]), 0)
        self.assertEqual(rn.call_args.args[3:], ("b", "z"))

    def test_cli_switch_by_id_prefix(self):
        from sampan.registry import Row, write_rows
        write_rows(self.cfg.registry_path, [Row("a", "claude", "alice", "sa", "/c", id="abcdef12-0000-4000-8000-000000000001"),
                                            Row("b", "claude", "alice", "sb", "/c", id="abcdef99-0000-4000-8000-000000000002")],
                   "t", self.cfg.history_dir)
        with mock.patch("sampan.session_run.cmd_switch", return_value=0) as sw, \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["switch", "abcdef99", "work"]), 0)
            self.assertEqual(sw.call_args.args[3:5], ("b", "work"))
            cli.main(["switch", "abcdef", "work"])  # shorter than 8: not an id, passed on as typed
            self.assertEqual(sw.call_args.args[3], "abcdef")

    def test_switch_many_goes_to_batch_and_single_does_not(self):
        import dataclasses
        os.environ["TMUX_PANE"] = "%4"
        with mock.patch("sampan.cli.load_config", return_value=dataclasses.replace(self.cfg, mcp_confirm=False)), \
                mock.patch("sampan.switch_many.cmd_switch_many", return_value=0) as sm, \
                mock.patch("sampan.session_run.cmd_switch", return_value=0) as sw, \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["switch", "a", "b", "work", "--from", "alice", "--yes", "--dry-run"]), 0)
            self.assertEqual(sm.call_args.args[3:6], (None, ["a", "b"], "work"))
            kw = sm.call_args.kwargs
            self.assertEqual((kw["from_profile"], kw["yes"], kw["dry_run"], kw["folder"], kw["own_pane"]),
                             ("alice", True, True, None, "%4"))
            self.assertEqual(cli.main(["switch", "--folder", "客戶", "work"]), 0)
            self.assertEqual((sm.call_args.args[4:6], sm.call_args.kwargs["folder"]), (([], "work"), "客戶"))
            self.assertEqual(sw.call_count, 0)
            self.assertEqual(cli.main(["switch", "a", "work"]), 0)
            self.assertEqual(sw.call_count, 1)
            self.assertEqual(sm.call_count, 2)

    def test_switch_needs_names_or_folder_not_both(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["switch", "work"]), 2)
            self.assertEqual(cli.main(["switch", "--folder", "x", "a", "work"]), 2)
        self.assertIn("用法：sampan switch NAME... PROFILE", buf.getvalue())

    def test_cli_ambiguous_prefix_refused(self):
        from sampan.registry import Row, write_rows
        write_rows(self.cfg.registry_path, [Row("a", "claude", "alice", "sa", "/c", id="abcdef12-0000-4000-8000-000000000001"),
                                            Row("b", "claude", "alice", "sb", "/c", id="abcdef12-1111-4000-8000-000000000002")],
                   "t", self.cfg.history_dir)
        buf = io.StringIO()
        with mock.patch("sampan.session_run.cmd_forget") as fg, redirect_stdout(buf):
            self.assertEqual(cli.main(["forget", "abcdef12"]), 2)
        fg.assert_not_called()
        self.assertIn("對到多個", buf.getvalue())

    def test_machine_command_shows_and_renames(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["machine"]), 0)
        from sampan import machine
        m = machine.load_or_create(self.cfg.path)
        self.assertIn(m.id, buf.getvalue())
        self.assertIn(m.name, buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["machine", "--name", "書房 Mac"]), 0)
        m2 = machine.load_or_create(self.cfg.path)
        self.assertEqual((m2.id, m2.name), (m.id, "書房 Mac"))
        self.assertIn("書房 Mac", buf.getvalue())

    def test_machine_rename_refused_for_agents_under_confirm(self):
        cfg = self.cfg = __import__("dataclasses").replace(self.cfg, mcp_confirm=True)
        with mock.patch("sampan.cli.load_config", return_value=cfg), \
                mock.patch("sampan.send.command_caller", return_value=Caller("a", "alice", "claude", "%1")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["machine", "--name", "x"]), 2)
        from sampan import machine
        self.assertNotEqual(machine.load_or_create(cfg.path).name, "x")

    def test_cli_forget_parses(self):
        with mock.patch("sampan.session_run.cmd_forget", return_value=0) as fg, \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["forget", "a", "--dry-run"]), 0)
            self.assertEqual(cli.main(["forget", "b"]), 0)
        self.assertEqual(fg.call_args_list[0].args[3], "a")
        self.assertTrue(fg.call_args_list[0].kwargs["dry_run"])
        self.assertEqual(fg.call_args_list[1].args[3], "b")
        self.assertFalse(fg.call_args_list[1].kwargs["dry_run"])

    def test_cli_forget_takes_several_names_and_goes_on_after_a_failure(self):
        with mock.patch("sampan.session_run.cmd_forget", side_effect=[0, 2, 1]) as fg, \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["forget", "a", "b", "a", "c", "--dry-run"]), 2)
        self.assertEqual([c.args[3] for c in fg.call_args_list], ["a", "b", "c"])  # a repeated name once
        self.assertTrue(all(c.kwargs["dry_run"] for c in fg.call_args_list))
        with mock.patch("sampan.session_run.cmd_forget") as fg, redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            cli.main(["forget"])
        self.assertEqual(fg.call_count, 0)

    def test_statusline_passes_wrapped_command_and_stdin(self):
        stdin = mock.Mock()
        stdin.buffer.read.return_value = b'{"x": 1}'
        with mock.patch("sampan.usage.cmd_statusline", return_value=3) as sl, mock.patch("sys.stdin", stdin):
            self.assertEqual(cli.main(["statusline", "--", "npx", "-y", "ccstatusline@latest"]), 3)
            cfg, _out, wrapped = sl.call_args.args
            self.assertIs(cfg, self.cfg)
            self.assertEqual(wrapped, ["npx", "-y", "ccstatusline@latest"])
            self.assertEqual(sl.call_args.kwargs["stdin"], b'{"x": 1}')
            cli.main(["statusline"])
            self.assertEqual(sl.call_args.args[2], [])

    def test_statusline_passes_wrapped_flags_through(self):
        stdin = mock.Mock()
        stdin.buffer.read.return_value = b"{}"
        with mock.patch("sampan.usage.cmd_statusline", return_value=0) as sl, mock.patch("sys.stdin", stdin):
            self.assertEqual(cli.main(["statusline", "--", "cmd", "--flag"]), 0)
        self.assertEqual(sl.call_args.args[2], ["cmd", "--flag"])

    def test_statusline_runs_wrapped_even_when_config_fails(self):
        stdin = mock.Mock()
        stdin.buffer.read.return_value = b"{}"
        with mock.patch("sampan.cli.load_config", side_effect=cli.ConfigError("bad")), \
                mock.patch("sampan.usage.cmd_statusline", return_value=0) as sl, mock.patch("sys.stdin", stdin):
            self.assertEqual(cli.main(["statusline", "--", "echo", "hi"]), 0)
        self.assertIsNone(sl.call_args.args[0])
        self.assertEqual(sl.call_args.args[2], ["echo", "hi"])

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
                mock.patch("sampan.init.cmd_init", return_value=0) as init, mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            self.assertEqual(cli.main(["init"]), 0)
        self.assertEqual(init.call_args.args[:2], (tmp / "none.toml", tmp / "state"))
        self.assertIn("sampan_path", init.call_args.kwargs)
        self.assertFalse(init.call_args.kwargs["no_launchd"])
        with mock.patch.dict("os.environ", env), mock.patch("sampan.init.cmd_init", return_value=0) as init, \
                mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            cli.main(["init", "--no-launchd"])
        self.assertTrue(init.call_args.kwargs["no_launchd"])

    def test_init_refuses_without_tty(self):
        tmp = Path(tempfile.mkdtemp())
        env = {"SAMPAN_CONFIG": str(tmp / "none.toml"), "SAMPAN_STATE_DIR": str(tmp / "state")}
        buf = io.StringIO()
        runner = FakeRunner(strict=True)
        with mock.patch.dict("os.environ", env), mock.patch("sampan.init.cmd_init") as init, \
                mock.patch("sampan.runner.run", runner), mock.patch("sys.stdin") as stdin, redirect_stdout(buf):
            stdin.isatty.return_value = False
            self.assertEqual(cli.main(["init"]), 2)
        self.assertIn("sampan init 會問幾個會寫入檔案的問題，請在終端機直接執行。腳本裡要全部用預設值，加 --yes。",
                      buf.getvalue())
        init.assert_not_called()
        self.assertEqual(runner.calls, [])
        self.assertFalse((tmp / "none.toml").exists())

    def test_init_refuses_when_stdin_is_none(self):
        tmp = Path(tempfile.mkdtemp())
        env = {"SAMPAN_CONFIG": str(tmp / "none.toml"), "SAMPAN_STATE_DIR": str(tmp / "state")}
        buf = io.StringIO()
        with mock.patch.dict("os.environ", env), mock.patch("sampan.init.cmd_init") as init, \
                mock.patch("sys.stdin", None), redirect_stdout(buf):
            self.assertEqual(cli.main(["init"]), 2)
        self.assertIn("請在終端機直接執行", buf.getvalue())
        init.assert_not_called()

    def test_init_yes_and_hook_parse_without_a_tty(self):
        tmp = Path(tempfile.mkdtemp())
        env = {"SAMPAN_CONFIG": str(tmp / "none.toml"), "SAMPAN_STATE_DIR": str(tmp / "state")}
        with mock.patch.dict("os.environ", env), mock.patch("sampan.init.cmd_init", return_value=0) as init, \
                mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertEqual(cli.main(["init", "--yes", "--hook", "--no-launchd"]), 0)
        kw = init.call_args.kwargs
        self.assertTrue(kw["yes"] and kw["hook"] and kw["no_launchd"])

    def test_profile_add_parses_dir(self):
        with mock.patch("sampan.init.cmd_profile_add", return_value=0) as add:
            cli.main(["profile", "add", "side", "--dir", "/Users/u/x"])
            cli.main(["profile", "add", "other"])
        self.assertEqual(add.call_args_list[0].args[3], "side")
        self.assertEqual(add.call_args_list[0].kwargs["dir"], Path("/Users/u/x"))
        self.assertIsNone(add.call_args_list[1].kwargs["dir"])

    def test_profile_list_and_rename_parse(self):
        with mock.patch("sampan.profile_ops.cmd_profile_list", return_value=0) as ls, \
                mock.patch("sampan.profile_ops.cmd_profile_rename", return_value=0) as rn:
            cli.main(["profile", "list"])
            cli.main(["profile", "rename", "side", "other"])
        ls.assert_called_once()
        self.assertEqual(rn.call_args.args[3:], ("side", "other"))
        self.assertIs(rn.call_args.args[0], self.cfg)  # setUp's temp config, not the host's

    def test_profile_disable_enable_forget_parse(self):
        with mock.patch("sampan.profile_ops.cmd_profile_disable", return_value=0) as dis, \
                mock.patch("sampan.profile_ops.cmd_profile_enable", return_value=0) as en, \
                mock.patch("sampan.profile_ops.cmd_profile_forget", return_value=0) as fg:
            cli.main(["profile", "disable", "work"])
            cli.main(["profile", "enable", "work"])
            cli.main(["profile", "forget", "work"])
        self.assertEqual(dis.call_args.args[3], "work")
        self.assertEqual(en.call_args.args[3], "work")
        self.assertIs(fg.call_args.args[3], input)
        self.assertEqual(fg.call_args.args[4], "work")

    def test_new_session_refused_on_disabled_profile(self):
        from sampan.config import load_config
        from sampan.config_edit import set_profile_flag
        from tests.helpers import TOML
        self.cfg.path.write_text(set_profile_flag(TOML, "work", "disabled", True))
        cfg = load_config(self.cfg.path, home=self.cfg.home, state_dir=self.cfg.state_dir)
        buf = io.StringIO()
        with mock.patch("sampan.cli.load_config", return_value=cfg), redirect_stdout(buf), \
                mock.patch("sampan.launch.cmd_claude", side_effect=AssertionError("must not launch")), \
                mock.patch("sampan.runner.run", FakeRunner(strict=True)):
            self.assertEqual(cli.main(["claude", "work"]), 2)
            self.assertEqual(cli.main(["new", "x", "work", "/tmp"]), 2)
            self.assertEqual(cli.main(["new", "x", "claude:work", "/tmp"]), 2)
        self.assertEqual(buf.getvalue().splitlines(), ["❌ profile work 已停用（sampan profile enable work）"] * 3)

    def test_send_passes_message_wait_and_timeout(self):
        with mock.patch("sampan.send.cmd_send", return_value=124) as cs, mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["send", "api", "多行\n訊息", "--wait", "--timeout", "30"]), 124)
        self.assertEqual(cs.call_args.args[3:], ("api", "多行\n訊息"))
        self.assertEqual(cs.call_args.kwargs, {"wait": True, "timeout": 30, "caller": TERMINAL, "raw": False})
        with mock.patch("sampan.send.cmd_send", return_value=0) as cs, \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["send", "api", "hi"]), 0)
        self.assertEqual(cs.call_args.kwargs, {"wait": False, "timeout": 600, "caller": TERMINAL, "raw": False})
        with mock.patch("sampan.send.cmd_send", return_value=0) as cs, \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["send", "api", "/compact", "--raw"]), 0)
        self.assertEqual(cs.call_args.args[3:], ("api", "/compact"))
        self.assertIs(cs.call_args.kwargs["raw"], True)

    def test_send_run_by_an_agent_is_labelled_as_that_session(self):  # final review 9
        import dataclasses
        agent = Caller("a", "alice", "claude", "%1")
        with mock.patch("sampan.cli.load_config", return_value=dataclasses.replace(self.cfg, mcp_confirm=False)), \
                mock.patch("sampan.send.cmd_send", return_value=0) as cs, \
                mock.patch("sampan.send.command_caller", return_value=agent) as cc:
            cli.main(["send", "api", "hi"])
        self.assertEqual(cc.call_args.args[2], os.getpid())  # looked up from this process, like the MCP server
        self.assertIs(cs.call_args.kwargs["caller"], agent)

    def test_dispatches_show(self):
        with mock.patch("sampan.send.cmd_show", return_value=124) as sh:
            self.assertEqual(cli.main(["dispatches", "show", "ab12", "--wait", "5"]), 124)
        self.assertEqual(sh.call_args.args[3], "ab12")
        self.assertEqual(sh.call_args.kwargs, {"wait": 5})
        buf = io.StringIO()
        with mock.patch("sampan.send.cmd_show") as sh, redirect_stdout(buf):
            self.assertEqual(cli.main(["dispatches", "show"]), 2)
        self.assertEqual(sh.call_count, 0)

    def test_dispatches_wait_without_show_is_refused(self):
        for argv in (["dispatches", "--wait", "5"], ["dispatches", "--wait", "0"], ["dispatches", "cancel", "ab12", "--wait", "5"]):
            buf = io.StringIO()
            with mock.patch("sampan.send.cmd_dispatches") as cd, mock.patch("sampan.send.cmd_cancel") as cc, \
                    mock.patch("sampan.send.command_caller", return_value=TERMINAL), redirect_stdout(buf):
                self.assertEqual(cli.main(argv), 2, argv)
            self.assertEqual((cd.call_count, cc.call_count), (0, 0))
            self.assertEqual(buf.getvalue().strip(), "❌ --wait 只能跟 show 一起用：sampan dispatches show <編號> --wait 秒")

    def test_send_batch_passes_names_message_and_flags(self):
        with mock.patch("sampan.send.cmd_send_batch", return_value=124) as cs, \
                mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["send-batch", "a", "b", "-m", "/compact", "--raw", "--wait", "--timeout", "30"]),
                             124)
        self.assertEqual(cs.call_args.args[3:5], (["a", "b"], "/compact"))
        self.assertEqual({k: cs.call_args.kwargs[k] for k in ("raw", "wait", "timeout", "caller")},
                         {"raw": True, "wait": True, "timeout": 30, "caller": TERMINAL})

    def test_send_batch_folder_expands_to_sessions_under_it(self):
        from sampan import folders
        register(self.cfg, "a", "b", "c")
        folders.edit(self.cfg.state_dir, lambda t: [folders.place_session(t, n, p) for n, p in
                                                    {"b": "客戶/甲", "a": "客戶", "c": "其他"}.items()])
        buf = io.StringIO()
        with mock.patch("sampan.send.cmd_send_batch", return_value=0) as cs, redirect_stdout(buf), \
                mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["send-batch", "--folder", "客戶", "-m", "hi"]), 0)
            self.assertEqual(cs.call_args.args[3], ["a", "b"])
            self.assertEqual(cli.main(["send-batch", "--folder", "/客戶/甲/", "-m", "hi"]), 0)
            self.assertEqual(cs.call_args.args[3], ["b"])
            self.assertEqual(cs.call_count, 2)
            self.assertEqual(cli.main(["send-batch", "--folder", "沒有", "-m", "hi"]), 2)
            self.assertEqual(cli.main(["send-batch", "--folder", "/", "-m", "hi"]), 2)
            self.assertEqual(cli.main(["send-batch", "a", "--folder", "客戶", "-m", "hi"]), 2)
            self.assertEqual(cli.main(["send-batch", "-m", "hi"]), 2)
        self.assertEqual(cs.call_count, 2)
        self.assertEqual(len([x for x in buf.getvalue().splitlines() if x.startswith("❌")]), 4)

    def test_send_dash_reads_stdin(self):
        with mock.patch("sampan.send.cmd_send", return_value=0) as cs, mock.patch("sys.stdin", io.StringIO("from\nstdin $x `y`\n")), \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            cli.main(["send", "api", "-"])
        self.assertEqual(cs.call_args.args[4], "from\nstdin $x `y`\n")

    def test_dispatches_list_and_cancel(self):
        with mock.patch("sampan.send.cmd_dispatches", return_value=0) as cd:
            self.assertEqual(cli.main(["dispatches"]), 0)
            self.assertEqual(cli.main(["dispatches", "--all"]), 0)
        self.assertEqual([c.kwargs["all_"] for c in cd.call_args_list], [False, True])
        with mock.patch("sampan.send.cmd_cancel", return_value=2) as cc, \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["dispatches", "cancel", "ab12"]), 2)
        self.assertEqual(cc.call_args.args[2], "ab12")
        buf = io.StringIO()
        with mock.patch("sampan.send.cmd_cancel") as cc, redirect_stdout(buf), \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["dispatches", "cancel"]), 2)
        self.assertEqual(cc.call_count, 0)
        self.assertIn("用法", buf.getvalue())

    def test_mcp_serve_identifies_the_caller_and_keeps_stdout_for_the_protocol(self):
        from sampan.send import Caller
        caller = Caller("x", "alice", "claude", "%1")
        seen = {}

        def fake_serve(cfg, run, stdin, stdout, *, caller, resolve, parent_agent):
            seen.update(stdin=stdin, stdout=stdout, caller=caller, sys_stdout=sys.stdout, again=resolve(),
                        parent_agent=parent_agent)
            run(["tmux", "-u", "list-panes"])
            run(["codex", "queue"], stdin="given")
            return 0

        proto = io.StringIO()
        fake_run = FakeRunner()
        fake_run.on(["ps", "-A"], cp(out=ps_line(4321, 1, "claude", "claude") + "\n"))
        with mock.patch("sampan.mcp_server.identify_caller", return_value=caller) as ic, \
                mock.patch("sampan.mcp_server.lookup_caller", return_value=caller) as lc, \
                mock.patch("sampan.mcp_server.serve", fake_serve), mock.patch("sys.stdout", proto), \
                mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b""))), \
                mock.patch("sampan.runner.run", fake_run), mock.patch("os.getppid", return_value=4321):
            self.assertEqual(cli.main(["mcp", "serve"]), 0)
            restored = sys.stdout
        self.assertEqual(ic.call_args.args[2], 4321)  # at start
        self.assertEqual(lc.call_args.args[2], 4321)  # again on demand (unsure lookups raise, kept by the server)
        self.assertIs(seen["again"], caller)
        self.assertIs(seen["caller"], caller)
        self.assertIs(seen["parent_agent"], True)  # the parent (4321) is claude
        self.assertIs(seen["stdout"], proto)
        self.assertIsNot(seen["sys_stdout"], proto)  # a stray print cannot corrupt the protocol stream
        self.assertIs(restored, proto)
        self.assertIsInstance(seen["stdin"], io.BufferedIOBase)
        import subprocess
        # a child never inherits the JSON-RPC stdin (an explicit stdin is kept)
        self.assertEqual([c["stdin"] for c in fake_run.calls], [subprocess.DEVNULL] * 2 + ["given"])

    def test_mcp_serve_from_a_shell_has_no_agent_parent(self):
        from sampan.send import Caller
        caller = Caller(None, "", "claude", None, kind="unregistered")
        seen = {}

        def fake_serve(cfg, run, stdin, stdout, *, caller, resolve, parent_agent):
            seen["parent_agent"] = parent_agent
            return 0

        for ps in (cp(out=ps_line(4321, 1, "/bin/zsh", "-zsh") + "\n"), cp(1)):
            with self.subTest(ps=ps):
                fake_run = FakeRunner(strict=True)
                fake_run.on(["ps", "-A"], ps)
                with mock.patch("sampan.mcp_server.identify_caller", return_value=caller), \
                        mock.patch("sampan.mcp_server.serve", fake_serve), mock.patch("sys.stdout", io.StringIO()), \
                        mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b""))), \
                        mock.patch("sampan.runner.run", fake_run), mock.patch("os.getppid", return_value=4321):
                    self.assertEqual(cli.main(["mcp", "serve"]), 0)
                self.assertIs(seen["parent_agent"], False)

    def test_dispatches_cancel_end_to_end(self):
        from sampan import dispatch
        with dispatch.transaction(self.cfg.state_dir) as items:
            items.append(dispatch.Dispatch("ab12", "終端機", "", "", "b", "claude", "alice", "x", "queued",
                                           "2026-10-26T12:00:00+00:00"))
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.command_caller", return_value=TERMINAL):
            self.assertEqual(cli.main(["dispatches", "cancel", "ab12"]), 0)
            self.assertEqual(cli.main(["dispatches", "cancel", "ab12"]), 2)  # already cancelled
        self.assertEqual(dispatch.load(self.cfg.state_dir)[0].state, "cancelled")


class AgentRefusedTest(unittest.TestCase):
    """While [mcp] confirm is on, an agent running the acting commands through its shell is refused."""
    SESSION = Caller("a", "alice", "claude", "%1")
    UNREG = Caller(None, "", "claude", None, kind="unregistered")

    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.set_confirm(True)
        p = mock.patch.dict("os.environ")
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)

    def set_confirm(self, on):
        import dataclasses
        p = mock.patch("sampan.cli.load_config", return_value=dataclasses.replace(self.cfg, mcp_confirm=on))
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, argv, caller, **patches):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.command_caller", return_value=caller) as cc:
            rc = cli.main(argv)
        return rc, buf.getvalue(), cc

    def test_agent_send_refused_when_confirm(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.send.cmd_send") as cs:
            rc, text, _ = self.run_cli(["send", "api", "hi"], self.SESSION)
        self.assertEqual((rc, cs.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)

    def test_agent_batch_switch_refused_when_confirm(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.switch_many.cmd_switch_many") as sm:
            rc, text, _ = self.run_cli(["switch", "a", "b", "work", "--yes"], self.SESSION)
        self.assertEqual((rc, sm.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)

    def test_send_raw_is_refused_for_an_agent_when_confirm_is_on(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.send.cmd_send") as cs:
            rc, text, _ = self.run_cli(["send", "api", "/compact", "--raw"], self.SESSION)
        self.assertEqual((rc, cs.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)

    def test_unregistered_agent_send_refused_when_confirm(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.send.cmd_send") as cs:
            rc, text, _ = self.run_cli(["send", "api", "hi"], self.UNREG)
        self.assertEqual((rc, cs.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)

    def test_unsure_lookup_send_refused_when_confirm(self):
        from sampan.send import AGENT_REFUSED, CallerUnsure
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.lookup_caller", side_effect=CallerUnsure("讀不到 tmux", "claude")) as lc, \
                mock.patch("sampan.send.cmd_send") as cs:
            rc = cli.main(["send", "api", "hi"])
        self.assertEqual(lc.call_count, 1)  # the real command_caller ran: an unsure lookup is an unregistered agent
        self.assertEqual((rc, cs.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, buf.getvalue())

    def test_agent_new_with_message_refused(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.session_run.cmd_new") as cn:
            rc, text, _ = self.run_cli(["new", "x", "claude:work", "/tmp", "--message", "hi"], self.SESSION)
        self.assertEqual((rc, cn.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)

    def test_agent_cancel_refused(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.send.cmd_cancel") as cc:
            rc, text, _ = self.run_cli(["dispatches", "cancel", "ab12"], self.SESSION)
        self.assertEqual((rc, cc.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)

    def test_send_batch_refused_for_agent_when_confirm_on(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.send.cmd_send_batch") as cs:
            rc, text, _ = self.run_cli(["send-batch", "a", "b", "-m", "hi"], self.SESSION)
        self.assertEqual((rc, cs.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)
        self.set_confirm(False)
        with mock.patch("sampan.send.cmd_send_batch", return_value=0) as cs:
            rc, _, _ = self.run_cli(["send-batch", "a", "b", "-m", "hi"], self.SESSION)
        self.assertEqual((rc, cs.call_count), (0, 1))

    def test_cli_alias_refused_for_agent_when_confirm_on(self):
        from sampan.send import AGENT_REFUSED
        with mock.patch("sampan.session_run.cmd_alias") as ca:
            rc, text, _ = self.run_cli(["alias", "a", "讀書"], self.SESSION)
        self.assertEqual((rc, ca.call_count), (2, 0))
        self.assertIn(AGENT_REFUSED, text)
        self.set_confirm(False)
        with mock.patch("sampan.session_run.cmd_alias", return_value=0) as ca:
            rc, _, _ = self.run_cli(["alias", "a", "讀書"], self.SESSION)
            self.assertEqual((rc, ca.call_count), (0, 1))
            rc, _, _ = self.run_cli(["alias", "a", "--clear"], self.SESSION)
        self.assertEqual(rc, 0)
        self.assertEqual(ca.call_args.kwargs, {"clear": True})

    def test_terminal_caller_still_sends(self):
        with mock.patch("sampan.send.cmd_send", return_value=0) as cs:
            rc, _, _ = self.run_cli(["send", "api", "hi"], TERMINAL)
        self.assertEqual((rc, cs.call_count), (0, 1))
        with mock.patch("sampan.send.cmd_cancel", return_value=0) as cc:
            self.assertEqual(self.run_cli(["dispatches", "cancel", "ab12"], TERMINAL)[0], 0)
        self.assertEqual(cc.call_count, 1)

    def test_agent_allowed_when_confirm_false(self):
        self.set_confirm(False)
        with mock.patch("sampan.send.cmd_send", return_value=0) as cs:
            rc, _, cc = self.run_cli(["send", "api", "hi"], self.SESSION)
        self.assertEqual((rc, cs.call_count), (0, 1))
        with mock.patch("sampan.send.cmd_cancel", return_value=0) as cx:
            self.assertEqual(self.run_cli(["dispatches", "cancel", "ab12"], self.SESSION)[0], 0)
        self.assertEqual(cx.call_count, 1)

    def test_agent_new_without_message_not_refused(self):
        with mock.patch("sampan.session_run.cmd_new", return_value=0) as cn:
            rc, _, cc = self.run_cli(["new", "x", "claude:work", "/tmp"], self.SESSION)
        self.assertEqual((rc, cn.call_count, cc.call_count), (0, 1, 0))

    def test_agent_dispatches_show_not_refused(self):
        with mock.patch("sampan.send.cmd_show", return_value=0) as sh, mock.patch("sampan.send.cmd_dispatches", return_value=0) as dl:
            rc, _, cc = self.run_cli(["dispatches", "show", "ab12"], self.SESSION)
            rc2, _, cc2 = self.run_cli(["dispatches"], self.SESSION)
        self.assertEqual((rc, rc2, sh.call_count, dl.call_count, cc.call_count, cc2.call_count), (0, 0, 1, 1, 0, 0))


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


SHA = "0123456789abcdef0123456789abcdef01234567"


class UpdateCmdTest(unittest.TestCase):
    """sampan update [--yes] [--force]: always a fresh check; run/stream/which are fakes (no network, no real uv)."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.msgs, self.streamed, self.asked = [], [], []

    def call(self, *, latest="0.6.1", current="0.6.0", after=None, yes=False, force=False, tty=True, answer="y",
             cache=None):
        from sampan import update
        if cache is not None:
            update.write_cache(self.state, cache, update._now())
        self.run = FakeRunner(strict=True)
        if latest is None:
            self.run.on(["git", "ls-remote"], cp(rc=128))
        else:
            self.run.on(["git", "ls-remote"], cp(out=f"{SHA}\trefs/tags/v{latest}\n"))
        self.run.on(["sampan", "--version"], cp(out=f"sampan {after or latest or current}\n"))

        def stream(args, on_line):
            self.streamed.append(args)
            return 0

        def ask(prompt):
            self.asked.append(prompt)
            return answer
        with mock.patch("sampan.cli.out", self.msgs.append):
            return cli._update_cmd(yes, force, run=self.run, stream=stream, which=lambda n: "/x/uv", ask=ask,
                                   isatty=lambda: tty, current=current, state_dir=self.state)

    def test_newer_with_yes_updates(self):
        self.assertEqual(self.call(yes=True, cache="0.6.0"), 0)  # fresh cache says 0.6.0: ignored
        self.assertEqual(self.msgs[0], "目前 0.6.0，最新 0.6.1")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.streamed, [["uv", "tool", "upgrade", "sampan"]])
        self.assertIn("✔ 已更新到 0.6.1", self.msgs)
        self.assertFalse(any("重新開啟" in m for m in self.msgs), self.msgs)

    def test_newer_asks_on_a_terminal(self):
        self.assertEqual(self.call(answer="y"), 0)
        self.assertEqual(self.asked, ["更新到 v0.6.1？(y/N): "])
        self.assertEqual(len(self.streamed), 1)
        self.msgs, self.streamed, self.asked = [], [], []
        self.assertEqual(self.call(answer="n"), 0)
        self.assertEqual(self.streamed, [])
        self.assertIn("已取消", self.msgs)

    def test_update_failure_is_rc_1(self):
        self.assertEqual(self.call(yes=True, after="0.6.0"), 1)
        self.assertTrue(self.msgs[-1].startswith("❌"))

    def test_up_to_date_without_force(self):
        self.assertEqual(self.call(latest="0.6.0", tty=False), 0)
        self.assertEqual(self.msgs, ["目前 0.6.0，最新 0.6.0", "已是最新版"])
        self.assertEqual((self.asked, self.streamed), ([], []))

    def test_force_yes_reinstalls(self):
        with mock.patch("sampan.update.do_update", return_value=True) as du:
            self.assertEqual(self.call(latest="0.6.0", force=True, yes=True), 0)
        self.assertTrue(du.call_args.kwargs["force"])
        self.assertFalse(du.call_args.kwargs["reopen"])
        self.assertEqual(du.call_args.kwargs["latest"], "0.6.0")
        self.assertEqual(du.call_args.kwargs["current"], "0.6.0")
        self.assertEqual(self.asked, [])

    def test_force_asks_and_runs_reinstall_only(self):
        self.assertEqual(self.call(latest="0.6.0", force=True), 0)
        self.assertEqual(self.asked, ["要重新安裝 v0.6.0？(y/N): "])
        self.assertEqual(self.streamed, [["uv", "tool", "install", "--reinstall", "git+https://github.com/royalroylo/sampan@v0.6.0"]])
        self.assertIn("✔ 已重新安裝 0.6.0", self.msgs)
        self.assertFalse(any("重新開啟" in m for m in self.msgs), self.msgs)

    def test_force_with_unknown_latest_pins_clean_current(self):
        self.call(latest=None, force=True, answer="n")
        self.assertEqual(self.msgs[0], "目前 0.6.0，最新 查不到")
        self.assertEqual(self.asked, ["要重新安裝 v0.6.0？(y/N): "])
        self.msgs, self.streamed, self.asked = [], [], []
        self.assertEqual(self.call(latest=None, force=True, yes=True, after="0.6.0"), 0)
        self.assertEqual(self.streamed, [["uv", "tool", "install", "--reinstall", "git+https://github.com/royalroylo/sampan@v0.6.0"]])
        self.assertEqual(len(self.run.called(["git", "ls-remote"])), 1)  # no second lookup inside do_update

    def test_force_with_unknown_latest_and_dev_build_refuses(self):
        self.assertEqual(self.call(latest=None, current="0.6.1.dev2+gabc", force=True, yes=True), 1)
        self.assertEqual(self.msgs[-1], "查不到最新版（離線或連不到 GitHub）")
        self.assertEqual((self.asked, self.streamed), ([], []))

    def test_dev_build_ahead_of_latest(self):
        self.assertEqual(self.call(latest="0.6.1", current="0.6.2.dev1+gabc"), 0)
        self.assertEqual(self.msgs[-1], "目前版本比最新版新（v0.6.1）")
        self.assertEqual((self.asked, self.streamed), ([], []))
        self.msgs = []
        with mock.patch("sampan.update.do_update", return_value=True) as du:
            self.assertEqual(self.call(latest="0.6.1", current="0.6.2.dev1+gabc", force=True), 0)
        self.assertEqual(self.asked, ["目前版本比最新版新；要改裝 v0.6.1 嗎？(y/N): "])
        self.assertEqual((du.call_args.kwargs["latest"], du.call_args.kwargs["force"]), ("0.6.1", True))

    def test_unknown_current_version(self):
        self.assertEqual(self.call(current="unknown"), 1)
        self.assertEqual(self.msgs[-1], "讀不到目前版本；要重新安裝請加 --force")
        self.assertEqual((self.asked, self.streamed), ([], []))
        self.msgs = []
        with mock.patch("sampan.update.do_update", return_value=True) as du:
            self.assertEqual(self.call(current="unknown", force=True, yes=True), 0)
        self.assertEqual((du.call_args.kwargs["latest"], du.call_args.kwargs["force"]), ("0.6.1", True))
        self.msgs = []
        self.assertEqual(self.call(current="unknown", latest=None, force=True, yes=True), 1)
        self.assertEqual(self.msgs[-1], "查不到最新版（離線或連不到 GitHub）")

    def test_not_a_tty_without_yes_refuses_when_it_would_ask(self):
        for kw in ({}, {"latest": "0.6.0", "force": True}):
            self.msgs, self.streamed, self.asked = [], [], []
            with mock.patch("sampan.update.do_update") as du:
                self.assertEqual(self.call(tty=False, **kw), 2)
            du.assert_not_called()
            self.assertEqual((self.asked, self.streamed), ([], []))
            self.assertEqual(self.msgs[-1], "要更新請在終端機執行，或加 --yes")

    def test_latest_unknown(self):
        self.assertEqual(self.call(latest=None), 1)
        self.assertEqual(self.msgs, ["目前 0.6.0，最新 查不到", "查不到最新版"])
        self.assertEqual(self.streamed, [])

    def test_main_parses_and_needs_no_config(self):
        with mock.patch("sampan.cli.load_config", side_effect=AssertionError), \
                mock.patch("sampan.cli._update_cmd", return_value=0) as uc:
            self.assertEqual(cli.main(["update"]), 0)
            self.assertEqual(cli.main(["update", "--yes", "--force"]), 0)
        self.assertEqual([c.args for c in uc.call_args_list], [(False, False), (True, True)])

    def test_listed_in_help(self):
        self.assertIn("檢查並更新 sampan（不看快取）", cli.build_parser().format_help())

    def test_real_default_uses_state_dir_and_fresh_check(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict("os.environ", {"SAMPAN_STATE_DIR": str(tmp)}), \
                mock.patch("sampan.update.check", return_value=(None, None)) as ch, \
                mock.patch("sampan.cli.out", self.msgs.append):
            self.assertEqual(cli._update_cmd(False, False, current="0.6.0"), 1)
        self.assertEqual(ch.call_args.args, (tmp,))
        self.assertTrue(ch.call_args.kwargs["force"])


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
