import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sampan import cli
from tests.fakes import FakeRunner, cp
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

    def test_force_with_unknown_latest_names_current(self):
        self.call(latest=None, force=True, answer="n")
        self.assertEqual(self.msgs[0], "目前 0.6.0，最新 查不到")
        self.assertEqual(self.asked, ["要重新安裝 v0.6.0？(y/N): "])

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
