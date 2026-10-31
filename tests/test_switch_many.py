import tempfile
import unittest
from pathlib import Path

from sampan import folders
from sampan.registry import Row, write_rows
from sampan.status import State
from sampan.switch_many import cmd_switch_many, plan
from tests.helpers import make_env, status_row


class Env(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.out = []
        self.calls = []
        self.rc = {}  # name -> rc switch_fn returns
        self.turn_busy = {}  # name -> rows seen after the first status read

    def reg(self, *names, cwds=None):
        cwds = cwds or {}
        write_rows(self.cfg.registry_path,
                   [Row(n, "claude", "alice", f"00000000-0000-4000-8000-{i:012d}", cwds.get(n, "/c"),
                        id=f"abcdef{i:02d}-0000-4000-8000-000000000000") for i, n in enumerate(names)],
                   "t", self.cfg.history_dir)

    def switch_fn(self, cfg, run, out, name, profile, **kw):
        self.calls.append((name, profile, kw))
        return self.rc.get(name, 0)

    def go(self, rows, refs, profile="work", ask=None, **kw):
        reads = []

        def status_fn(cfg, run):
            reads.append(1)
            if len(reads) > 1:
                return [status_row(r.name, agent_status="busy", profile=r.profile) if r.name in self.turn_busy else r
                        for r in rows]
            return rows

        return cmd_switch_many(self.cfg, None, self.out.append, ask, refs, profile, switch_fn=self.switch_fn,
                               status_fn=status_fn, **kw)

    def text(self):
        return "\n".join(self.out)


class PlanTest(Env):
    def test_plan_skips_each_kind_with_reason(self):
        self.reg("ok", "busy", "wait", "cx", "ret", "wt", "me", "already", "other", "stopped",
                 cwds={"wt": "/p/.claude/worktrees/x"})
        rows = [
            status_row("ok"),
            status_row("busy", agent_status="busy"),
            status_row("wait", agent_status="waiting"),
            status_row("cx", agent="codex", profile="-"),
            status_row("ret", State.RETIRED, agent_status="-"),
            status_row("wt"),
            status_row("me", pane="%9"),
            status_row("already", profile="work"),
            status_row("other", profile="third"),
            status_row("stopped", State.STOPPED, agent_status="-"),
        ]
        names = [r.name for r in rows]
        go, skip = plan(self.cfg, rows, names, "work", from_profile="alice", own_pane="%9")
        self.assertEqual(go, ["ok", "stopped"])
        self.assertEqual(dict(skip), {
            "busy": "忙碌中", "wait": "在等人回應", "cx": "Codex 不支援換 profile", "ret": "已 retired",
            "wt": "在 worktree 裡", "me": "是 sampan 所在的 pane", "already": "已經是 work",
            "other": "目前不是 alice"})


class RunTest(Env):
    def test_runs_each_in_order_and_prints_table(self):
        self.reg("a", "b")
        rows = [status_row("a"), status_row("b", profile="work"), status_row("c")]
        self.reg("a", "b", "c")
        rc = self.go(rows, ["c", "a", "b"], yes=True, own_pane="%1")
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in self.calls], ["c", "a"])
        self.assertEqual(self.calls[0][2], {"own_pane": "%1", "on_prompt": None, "warn_gap": False})
        t = self.text()
        self.assertIn("會換 2 個：c、a", t)
        self.assertIn("跳過 1 個：b（已經是 work）", t)
        self.assertIn("結果：", t)
        self.assertIn("c  已換  work", t)
        self.assertIn("b  略過  已經是 work", t)

    def test_turned_busy_before_start_is_skipped_others_continue(self):
        self.reg("a", "b")
        self.turn_busy = {"a": 1}
        rows = [status_row("a"), status_row("b")]
        self.assertEqual(self.go(rows, ["a", "b"], yes=True), 0)
        self.assertEqual([c[0] for c in self.calls], ["b"])
        self.assertIn("a  略過  開始前變忙了", self.text())

    def test_failed_one_gives_rc_1_and_others_still_run(self):
        self.reg("a", "b")
        self.rc = {"a": 1}
        self.assertEqual(self.go([status_row("a"), status_row("b")], ["a", "b"], yes=True), 1)
        self.assertEqual([c[0] for c in self.calls], ["a", "b"])
        self.assertIn("a  失敗  rc 1", self.text())

    def test_no_yes_non_interactive_refused(self):
        self.reg("a", "b")
        self.assertEqual(self.go([status_row("a"), status_row("b")], ["a", "b"], ask=None), 2)
        self.assertIn("要確認請加 --yes", self.text())
        self.assertEqual(self.calls, [])

    def test_ask_only_y_continues(self):
        self.reg("a", "b")
        rows = [status_row("a"), status_row("b")]
        asked = []
        self.assertEqual(self.go(rows, ["a", "b"], ask=lambda p: asked.append(p) or "n"), 0)
        self.assertEqual((asked, self.calls), (["開始？ y/N"], []))
        self.assertEqual(self.go(rows, ["a", "b"], ask=lambda p: "Y"), 0)
        self.assertEqual(len(self.calls), 2)

    def test_dry_run_changes_nothing(self):
        self.reg("a", "b")
        self.assertEqual(self.go([status_row("a"), status_row("b")], ["a", "b"], dry_run=True), 0)
        self.assertEqual(self.calls, [])
        self.assertIn("會換 2 個：a、b", self.text())

    def test_folder_includes_subfolders(self):
        self.reg("top", "in1", "in2", "elsewhere")
        tree = folders.load(self.cfg.state_dir)
        folders.add(tree, "客戶/甲")
        for n, p in (("in1", "客戶"), ("in2", "客戶/甲"), ("elsewhere", "")):
            if p:
                folders.place_session(tree, n, p)
        folders.save(self.cfg.state_dir, tree)
        rows = [status_row(n) for n in ("top", "in1", "in2", "elsewhere")]
        self.assertEqual(self.go(rows, [], folder="客戶", yes=True), 0)
        self.assertEqual([c[0] for c in self.calls], ["in1", "in2"])

    def test_unknown_folder_and_profile_and_name(self):
        self.reg("a")
        rows = [status_row("a")]
        self.assertEqual(self.go(rows, [], folder="沒有", yes=True), 2)
        self.assertEqual(self.go(rows, ["a"], profile="nope", yes=True), 2)
        self.assertEqual(self.go(rows, ["a", "ghost"], yes=True), 2)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
