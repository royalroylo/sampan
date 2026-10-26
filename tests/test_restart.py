import tempfile
import unittest
from pathlib import Path

from sampan.restart import BUSY, RestartPlan, cmd_restart, confirm_line, plan_restart
from sampan.status import State
from tests.helpers import make_env, status_row


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


if __name__ == "__main__":
    unittest.main()
