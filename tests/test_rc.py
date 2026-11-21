import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from sampan import cli, rc
from sampan.rc import cmd_rc, plan_lines, plan_rc, skip_lines
from sampan.restart import NOT_FOUND
from sampan.status import State
from tests.helpers import status_row
from tests.test_steps import (
    ACTIVE,
    DISCONNECTED,
    DRAFT,
    MENU_CONTINUE,
    MENU_DISCONNECT,
    MENU_QR,
    RECONNECTED,
    FakeTmux,
    Sleeps,
    rc_cfg,
)


def rename_tmux(start=ACTIVE, **kw):
    """A pane whose reset goes through: the screen of a session that has run a while (no "is active" line) → menu →
    disconnected → reconnected."""
    return FakeTmux(start, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED, RECONNECTED], **kw)


class Tty(io.StringIO):
    def isatty(self):
        return True


class Panes:
    """Several panes in one tmux: each pane id has its own FakeTmux; what was sent where is kept per pane."""

    def __init__(self, **panes):
        self.panes = panes

    def send_keys(self, target, *keys):
        self.panes[target].send_keys(target, *keys)

    def capture(self, target, lines=30, escapes=False):
        return self.panes[target].capture(target, lines, escapes)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.cfg = rc_cfg(True)

    def rows(self):
        return [
            status_row("idle", pane="%1", alias="Alpha"),
            status_row("busy", agent_status="busy", pane="%2"),
            status_row("waiting", agent_status="waiting", pane="%3"),
            status_row("shell", agent_status="shell", pane="%4"),
            status_row("me", pane="%9"),
            status_row("stopped", State.STOPPED),
            status_row("retired", State.RETIRED),
            status_row("unknown", State.UNKNOWN),
            status_row("mismatch", State.PROFILE_MISMATCH),
            status_row("cdx", agent="codex", profile="", pane="%5"),
            status_row("off", profile="work", pane="%6"),
        ]

    def test_each_state_gets_its_own_reason(self):
        names = [r.name for r in self.rows()] + ["ghost"]
        plan = plan_rc(self.cfg, self.rows(), names, own_pane="%9")
        self.assertEqual([r.name for r in plan.todo], ["idle"])
        skip = dict(plan.skip)
        self.assertEqual(skip["busy"], "忙")
        self.assertEqual(skip["waiting"], "忙")
        self.assertEqual(skip["shell"], "沒有閒著（shell）")
        self.assertEqual(skip["me"], "sampan 就在這個 pane")
        self.assertEqual(skip["stopped"], "已停止；下次接回會自動套用")
        self.assertEqual(skip["retired"], "retired")
        self.assertEqual(skip["unknown"], "狀態不明")
        self.assertEqual(skip["mismatch"], "prof 不符")
        self.assertEqual(skip["cdx"], "Codex 沒有 Remote Control")
        self.assertIn("work 沒開 Remote Control", skip["off"])
        self.assertEqual(skip["ghost"], NOT_FOUND)

    def test_profile_with_remote_control_off_is_skipped_for_every_session_on_it(self):
        plan = plan_rc(rc_cfg(False), [status_row("a", pane="%1")], ["a"])
        self.assertEqual(plan.todo, [])
        self.assertIn("alice 沒開 Remote Control", plan.skip[0][1])

    def test_all_profile_and_names_combine(self):
        rows = [status_row("a", pane="%1"), status_row("b", profile="work", pane="%2"), status_row("c", pane="%3")]
        cfg = rc_cfg(True)
        self.assertEqual([r.name for r in plan_rc(cfg, rows, [], everyone=True).todo], ["a", "c"])  # b: profile work is off
        self.assertEqual([r.name for r in plan_rc(cfg, rows, [], profile="alice").todo], ["a", "c"])
        self.assertEqual([r.name for r in plan_rc(cfg, rows, [], everyone=True, profile="work").todo], [])
        # names and --profile: the union, in order, each once
        plan = plan_rc(cfg, rows, ["c", "zz", "c"], profile="alice")
        self.assertEqual([r.name for r in plan.todo], ["c", "a"])
        self.assertEqual(plan.skip, [("zz", NOT_FOUND)])

    def test_lines_group_the_skipped_by_reason(self):
        self.assertEqual(skip_lines([("a", "忙"), ("b", "已停止；下次接回會自動套用"), ("c", "忙")]),
                         ["  略過（忙）：a、c", "  略過（已停止；下次接回會自動套用）：b"])
        rows = [status_row(f"s{i}", pane=f"%{i}", alias="A" if i == 0 else "") for i in range(10)]
        plan = plan_rc(self.cfg, rows, [], everyone=True)
        self.assertEqual(plan_lines(plan)[0], "將校正 10 個：s0（A）、s1、s2、s3、s4、s5、s6、s7、等 10 個")
        self.assertEqual(plan_lines(plan, limit=None)[0].count("、"), 9)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.cfg = rc_cfg(True)
        self.out, self.sleep = [], Sleeps()

    def run_rc(self, rows, panes, names=None, **kw):
        plan = plan_rc(self.cfg, rows, names or [r.name for r in rows])
        return cmd_rc(self.cfg, None, self.out.append, plan, sleep=self.sleep, rows_fn=lambda: rows,
                      tmux=Panes(**panes), **kw)

    def test_corrects_each_session_under_its_own_alias(self):
        rows = [status_row("a", pane="%1", alias="Alpha"), status_row("b", pane="%2")]
        panes = {"%1": rename_tmux(), "%2": rename_tmux()}
        for alias, pane in (("Alpha", "%1"), ("b", "%2")):
            panes[pane].after[-1] = [RECONNECTED.replace("讀書 PM", alias)]
        self.assertEqual(self.run_rc(rows, panes), 0)
        self.assertEqual(panes["%1"].sent[-1], ["/remote-control Alpha", "Enter"])
        self.assertEqual(panes["%2"].sent[-1], ["/remote-control b", "Enter"])
        self.assertEqual(self.out[-1], "校正 2、略過 0、失敗 0")
        self.assertIn("  · a Remote Control 名稱已校正為「Alpha」", self.out)

    def test_sessions_are_done_one_after_the_other(self):
        rows = [status_row("a", pane="%1", alias="讀書 PM"), status_row("b", pane="%2", alias="讀書 PM")]
        panes = {"%1": rename_tmux(), "%2": rename_tmux()}
        self.assertEqual(self.run_rc(rows, panes), 0)
        self.assertIn(rc.PAUSE, self.sleep)  # a pause between the two (the first one needs none)
        self.assertEqual(self.sleep.count(rc.PAUSE), 1)
        self.assertLess(self.out.index("  · a Remote Control 名稱已校正為「讀書 PM」"),
                        self.out.index("  … b：RC 校正中（最多約 20 秒）"))

    def test_a_failure_does_not_stop_the_next_one_and_makes_rc_1(self):
        rows = [status_row("a", pane="%1"), status_row("b", pane="%2", alias="讀書 PM")]
        panes = {"%1": FakeTmux(DRAFT), "%2": rename_tmux()}
        self.assertEqual(self.run_rc(rows, panes), 1)
        self.assertEqual(panes["%1"].sent, [])  # a draft in the box: nothing pressed
        self.assertIn("  ⚠ a：RC 校正沒做：輸入框有字；之後可再跑 sampan rc a", self.out)
        self.assertEqual(self.out[-1], "校正 1、略過 0、失敗 1")

    def test_a_session_that_became_busy_is_skipped_without_a_key(self):
        planned = [status_row("a", pane="%1")]
        plan = plan_rc(self.cfg, planned, ["a"])
        now = [status_row("a", pane="%1", agent_status="busy")]
        tmux = FakeTmux(ACTIVE)
        self.assertEqual(cmd_rc(self.cfg, None, self.out.append, plan, sleep=self.sleep, rows_fn=lambda: now,
                                tmux=Panes(**{"%1": tmux})), 0)
        self.assertEqual(tmux.sent, [])
        self.assertIn("  略過（忙）：a", self.out)
        self.assertEqual(self.out[-1], "校正 0、略過 1、失敗 0")

    def test_skipped_sessions_are_listed_with_reasons_and_not_touched(self):
        rows = [status_row("a", pane="%1", alias="讀書 PM"), status_row("b", State.STOPPED),
                status_row("c", agent="codex", profile="", pane="%3")]
        tmux = FakeTmux(ACTIVE)
        panes = {"%1": rename_tmux(), "%3": tmux}
        self.assertEqual(self.run_rc(rows, panes), 0)
        self.assertEqual(tmux.sent, [])
        self.assertIn("  略過（已停止；下次接回會自動套用）：b", self.out)
        self.assertIn("  略過（Codex 沒有 Remote Control）：c", self.out)
        self.assertEqual(self.out[-1], "校正 1、略過 2、失敗 0")

    def test_a_tmux_error_is_a_reason_not_a_crash(self):
        class Broken(FakeTmux):
            def capture(self, target, lines=30, escapes=False):
                raise OSError("tmux gone")
        rows = [status_row("a", pane="%1")]
        self.assertEqual(self.run_rc(rows, {"%1": Broken(ACTIVE)}), 1)
        self.assertIn("  ⚠ a：RC 校正沒做：tmux gone；之後可再跑 sampan rc a", self.out)

    def test_ctrl_c_stops_the_rest_and_lists_them(self):
        rows = [status_row(n, pane=f"%{i}", alias="讀書 PM") for i, n in enumerate("abc", 1)]
        panes = {f"%{i}": rename_tmux() for i in (1, 2, 3)}

        def interrupt(s):
            raise KeyboardInterrupt
        self.sleep = interrupt  # the pause before the second session
        self.assertEqual(self.run_rc(rows, panes), 1)
        self.assertIn("中斷：還沒處理 b、c", self.out)
        self.assertEqual(panes["%2"].sent, [])

    def test_failure_saves_the_screen_in_actions_log_only(self):
        rows = [status_row("a", pane="%1")]
        other = ACTIVE.split("────")[0] + "\n Some other dialog\n ❯ 1. Yes\n   2. No\n Enter to confirm · Esc to cancel\n"
        panes = {"%1": FakeTmux(ACTIVE, [other])}
        self.assertEqual(self.run_rc(rows, panes), 1)
        log = (self.cfg.state_dir / "actions.log").read_text(encoding="utf-8")
        self.assertIn("a：選單沒出現；最後的畫面：", log)
        self.assertIn("Some other dialog", log)
        self.assertNotIn("Some other dialog", "\n".join(self.out))

    def test_every_message_goes_to_actions_log(self):
        rows = [status_row("a", pane="%1", alias="讀書 PM")]
        self.assertEqual(self.run_rc(rows, {"%1": rename_tmux()}), 0)
        log = (self.cfg.state_dir / "actions.log").read_text(encoding="utf-8")
        self.assertIn("[RC 校正]", log)
        self.assertIn("校正 1、略過 0、失敗 0", log)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.cfg = rc_cfg(True)
        for p in (mock.patch("sampan.cli.load_config", return_value=self.cfg), mock.patch.dict("os.environ")):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        self.rows = [status_row("a", pane="%1"), status_row("b", pane="%2"), status_row("c", State.STOPPED)]

    def main(self, argv, *, tty=False, answer="y"):
        out = Tty() if tty else io.StringIO()
        stdin = mock.Mock(isatty=mock.Mock(return_value=tty))
        with mock.patch("sampan.status.status_rows", return_value=self.rows), \
                mock.patch("sampan.rc.cmd_rc", return_value=0) as run, \
                mock.patch("sampan.cli.sys.stdin", stdin), \
                mock.patch("builtins.input", return_value=answer) as ask, redirect_stdout(out):
            rc_ = cli.main(argv)
        return rc_, out.getvalue(), run, ask

    def test_no_target_is_an_error(self):
        rc_, out, run, _ = self.main(["rc"])
        self.assertEqual(rc_, 2)
        run.assert_not_called()
        self.assertIn("--all", out)

    def test_names_run_without_a_question(self):
        rc_, out, run, ask = self.main(["rc", "a"])
        self.assertEqual(rc_, 0)
        ask.assert_not_called()
        self.assertEqual([r.name for r in run.call_args.args[3].todo], ["a"])

    def test_not_found_is_rc_2_but_the_others_still_run(self):
        rc_, out, run, _ = self.main(["rc", "a", "zz"])
        self.assertEqual(rc_, 2)
        self.assertIn("❌ 找不到 session：zz", out)
        run.assert_called_once()

    def test_dry_run_prints_everything_and_presses_nothing(self):
        rc_, out, run, ask = self.main(["rc", "--all", "--dry-run"])
        self.assertEqual(rc_, 0)
        run.assert_not_called()
        ask.assert_not_called()
        self.assertIn("將校正 2 個：a、b", out)
        self.assertIn("略過（已停止；下次接回會自動套用）：c", out)
        self.assertIn("只改名（不斷開，手機列表不多一筆）", out)

    def test_all_asks_in_a_terminal_and_runs_on_y(self):
        rc_, out, run, ask = self.main(["rc", "--all"], tty=True)
        ask.assert_called_once()
        run.assert_called_once()
        self.assertIn("將校正 2 個：a、b", out)

    def test_all_cancelled_on_anything_but_y(self):
        rc_, out, run, ask = self.main(["rc", "--all"], tty=True, answer="n")
        self.assertEqual(rc_, 0)
        run.assert_not_called()
        self.assertIn("已取消", out)

    def test_all_without_a_terminal_needs_yes(self):
        rc_, out, run, ask = self.main(["rc", "--all"])
        self.assertEqual(rc_, 2)
        run.assert_not_called()
        self.assertIn("--yes", out)
        rc_, out, run, ask = self.main(["rc", "--all", "--yes"])
        self.assertEqual(rc_, 0)
        ask.assert_not_called()
        run.assert_called_once()

    def test_profile_that_does_not_exist_is_an_error(self):
        rc_, out, run, _ = self.main(["rc", "--profile", "nope", "--yes"])
        self.assertEqual(rc_, 2)
        run.assert_not_called()

    def test_nothing_to_correct_is_not_a_failure(self):
        rc_, out, run, _ = self.main(["rc", "c"])
        self.assertEqual(rc_, 0)
        run.assert_not_called()
        self.assertIn("沒有可校正的 session。", out)
        self.assertIn("略過（已停止；下次接回會自動套用）：c", out)

    def test_the_own_pane_is_never_corrected(self):
        os.environ["TMUX_PANE"] = "%1"
        rc_, out, run, _ = self.main(["rc", "a", "b"])
        self.assertEqual([r.name for r in run.call_args.args[3].todo], ["b"])
        self.assertEqual(run.call_args.kwargs["own_pane"], "%1")
        self.assertIn("sampan 就在這個 pane", "\n".join(f"{n}:{w}" for n, w in run.call_args.args[3].skip))


if __name__ == "__main__":
    unittest.main()
