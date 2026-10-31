import tempfile
import unittest
from pathlib import Path

from sampan.restart import BUSY, RestartPlan, cmd_restart, confirm_line, plan_restart, stop_line
from sampan.status import State
from sampan.textwidth import text_width
from tests.helpers import make_env, status_row
from tests.test_restore import NOW, A, B, StepsFixture, dead


class PlanTest(unittest.TestCase):
    def test_plan_classifies_each_state(self):
        rows = [
            status_row("idle", pane="%1"),
            status_row("busy", agent_status="busy", pane="%2"),
            status_row("waiting", agent_status="waiting", pane="%3"),
            status_row("me", pane="%9"),
            status_row("stopped", State.STOPPED),
            status_row("retired", State.RETIRED),
            status_row("unknown", State.UNKNOWN),
            status_row("mismatch", State.PROFILE_MISMATCH),
        ]
        names = ["idle", "busy", "waiting", "me", "stopped", "retired", "unknown", "mismatch", "ghost"]
        plan = plan_restart(rows, names, resume_stopped=True, own_pane="%9")
        self.assertEqual(plan.restart, ["idle"])
        self.assertEqual(plan.resume, ["stopped"])
        skip = dict(plan.skip)
        self.assertEqual(skip["busy"], "忙")
        self.assertEqual(skip["waiting"], "忙")
        self.assertEqual(skip["me"], "sampan 就在這個 pane")
        self.assertEqual(skip["retired"], "retired")
        self.assertEqual(skip["unknown"], "狀態不明")
        self.assertEqual(skip["mismatch"], "prof 不符")
        self.assertEqual(skip["ghost"], "找不到")
        self.assertEqual(BUSY, {"busy", "working", "waiting"})
        plan = plan_restart(rows, ["stopped"], resume_stopped=False)
        self.assertEqual(plan.resume, [])
        self.assertEqual(plan.skip, [("stopped", "已停止")])

    def test_plan_only_restarts_idle(self):
        rows = [status_row("u", agent_status="unknown"), status_row("d", agent_status="-"),
                status_row("i", agent_status="idle"), status_row("s", agent_status="shell")]
        plan = plan_restart(rows, ["u", "d", "i", "s"], resume_stopped=True)
        self.assertEqual(plan.restart, ["i"])
        # shell: a background task runs; stopping the session would kill it
        self.assertEqual(plan.skip, [("u", "沒有閒著（unknown）"), ("d", "沒有閒著（-）"), ("s", "沒有閒著（shell）")])

    def test_plan_keeps_order_and_dedups(self):
        rows = [status_row("a"), status_row("b"), status_row("c", State.STOPPED)]
        plan = plan_restart(rows, ["b", "c", "a", "b", "x", "x"], resume_stopped=True)
        self.assertEqual(plan.restart, ["b", "a"])
        self.assertEqual(plan.resume, ["c"])
        self.assertEqual(plan.skip, [("x", "找不到")])

    def test_confirm_line_exact(self):
        plan = RestartPlan(["a", "b"], [], [("c", "忙")])
        self.assertEqual(confirm_line(plan), "將重開：a、b　略過：c（忙）　確定？(y/N)")
        plan = RestartPlan(["a"], ["s"], [("d", "retired")])
        self.assertEqual(confirm_line(plan), "將重開：a、s（背景接回）　略過：d（retired）　確定？(y/N)")
        self.assertEqual(confirm_line(RestartPlan(["a"], [], [])), "將重開：a　確定？(y/N)")
        self.assertEqual(confirm_line(RestartPlan([], [], [("c", "忙")])), "")

    def test_confirm_line_truncates(self):
        names = list("abcdefghij")
        line = confirm_line(RestartPlan(names, [], []))
        self.assertIn("h等 10 個", line)
        self.assertNotIn("i", line.replace("將重開", "").replace("確定", ""))
        line = confirm_line(RestartPlan(["a"], [], [(n, "忙") for n in names]))
        self.assertIn("h（忙）等 10 個", line)


    def test_plan_resume_stopped_per_name(self):
        rows = [status_row("a", State.STOPPED), status_row("b", State.STOPPED), status_row("c")]
        plan = plan_restart(rows, ["a", "b", "c"], resume_stopped={"a"})
        self.assertEqual(plan.restart, ["c"])
        self.assertEqual(plan.resume, ["a"])
        self.assertEqual(plan.skip, [("b", "已停止")])
        plan = plan_restart(rows, ["a", "b"], resume_stopped=())
        self.assertEqual(plan.resume, [])

    def test_confirm_line_fits_width_with_question(self):
        names = [f"{i:02d}" + "n" * 18 for i in range(10)]  # 20 cells each
        line = confirm_line(RestartPlan(names, [], []), width=60)
        self.assertTrue(line.endswith("確定？(y/N)"), line)
        self.assertIn("等 10 個", line)
        self.assertLessEqual(text_width(line), 60)
        line = confirm_line(RestartPlan(names[:1], [], [(n, "忙") for n in names]), width=60)
        self.assertTrue(line.endswith("確定？(y/N)"), line)
        self.assertLessEqual(text_width(line), 60)
        self.assertEqual(confirm_line(RestartPlan(["a"], [], []), width=60), "將重開：a　確定？(y/N)")

    def test_confirm_line_without_limit_lists_all(self):
        names = list("abcdefghij")
        self.assertEqual(confirm_line(RestartPlan(names, [], []), limit=None),
                         "將重開：" + "、".join(names) + "　確定？(y/N)")

    def test_stop_line_shortens_and_fits(self):
        self.assertEqual(stop_line(["a", "b"]), "將停掉：a、b　確定？(y/N)")
        self.assertIn("h等 10 個", stop_line(list("abcdefghij")))
        names = [f"{i:02d}" + "n" * 18 for i in range(10)]
        line = stop_line(names, width=60)
        self.assertTrue(line.endswith("確定？(y/N)"), line)
        self.assertIn("等 10 個", line)
        self.assertLessEqual(text_width(line), 60)


class CmdTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.calls = []
        self.lines = []

    def stop_fn(self, cfg, run, out, **kw):
        self.calls.append(("stop", kw))
        kw["stopped"] += ["a"]
        kw["left"] += ["b"]
        kw["why"]["b"] = "忙"
        return 0

    def restore_fn(self, cfg, run, out, ask, **kw):
        self.calls.append(("restore", kw))
        return 1

    def run_cmd(self, plan, **kw):
        return cmd_restart(self.cfg, None, self.lines.append, None, plan, stop_fn=self.stop_fn,
                           restore_fn=self.restore_fn, **kw)

    def test_restart_restores_only_what_stopped_plus_resume(self):
        rc = self.run_cmd(RestartPlan(["a", "b"], ["c"], [("d", "retired")]), own_pane="%9")
        self.assertEqual(rc, 1)
        stop = self.calls[0][1]
        self.assertEqual(stop["names"], ("a", "b"))
        self.assertEqual(stop["own_pane"], "%9")
        self.assertEqual(stop["timeout"], 90)
        self.assertEqual(self.calls[1][1]["names"], ("a", "c"))
        text = "\n".join(self.lines)
        self.assertIn("略過：d（retired）、b（忙）", text)

    def test_restart_never_attaches(self):
        self.run_cmd(RestartPlan(["a"], [], []), on_prompt="P")
        kw = self.calls[1][1]
        self.assertNotIn("opened", kw)
        self.assertNotIn("wait", kw)
        self.assertIs(kw["confirm"], False)
        self.assertEqual(kw["on_prompt"] is not None, True)

    def test_nothing_stopped_and_no_resume_skips_restore(self):
        def stop_none(cfg, run, out, **kw):
            kw["left"].append("a")
            return 0
        cmd_restart(self.cfg, None, self.lines.append, None, RestartPlan(["a"], [], []), stop_fn=stop_none,
                    restore_fn=self.restore_fn)
        self.assertEqual(self.calls, [])
        self.assertIn("略過：a", "\n".join(self.lines))


class RestartStepsTest(StepsFixture):
    """Restart = stop + the real restore: each session reattached gets one RC reset."""

    def test_restart_runs_rc_step_once_per_session(self):
        from sampan.registry import Row
        from sampan.restore import cmd_restore
        self.transcript(A)
        self.transcript(B)
        self.registry([Row("a", "claude", "alice", A, str(self.cwd)), Row("b", "claude", "alice", B, str(self.cwd))])
        r = self.runner()

        def stop_fn(cfg, run, out, **kw):
            kw["stopped"] += ["a"]
            return 0

        def restore_fn(cfg, run, out, ask, **kw):
            return cmd_restore(cfg, run, out, ask, sleep=self.sleep, now_fn=lambda: NOW, timeout=3, alive=dead, **kw)
        with self.recorded():
            rc = cmd_restart(self.cfg, r, self.msgs.append, None, RestartPlan(["a"], ["b"], []),
                             stop_fn=stop_fn, restore_fn=restore_fn)
        self.assertEqual(rc, 0)
        self.assertEqual([o[:2] for o in self.order], [("launch", "a"), ("launch", "b"), ("rc", "a"), ("rc", "b")])


if __name__ == "__main__":
    unittest.main()
