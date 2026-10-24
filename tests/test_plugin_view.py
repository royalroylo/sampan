import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sampan.plugin_view import (
    STATES,
    Change,
    ProfileChoice,
    build_rows,
    card_lines,
    mismatch,
    plan_changes,
    preview_lines,
    profile_choices,
    snapshot,
)
from tests.helpers import make_env
from tests.test_plugins import GOOD_KNOWN, write_plugins

TOOLS, SUPER = "tools@my-plugins", "superpowers@claude-plugins-official"


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def rows(self):
        return {r.pid: r for r in build_rows(self.cfg, snapshot(self.cfg))}

    def test_rows_union_counts_and_states(self):
        write_plugins(self.cfg, "alice", installed={TOOLS: [("user", "1.0.0", None)], SUPER: [("user", "5.0", None)]},
                      known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", installed={TOOLS: [("user", "1.0.0", None)],
                                                   "extra@my-plugins": [("user", "2", None)]}, known=GOOD_KNOWN)
        r = self.rows()
        self.assertEqual(list(r), sorted(r))
        self.assertEqual((r[TOOLS].state, len(r[TOOLS].installed), r[TOOLS].total), ("最新", 2, 2))
        self.assertEqual((r[SUPER].state, len(r[SUPER].installed), r[SUPER].listed), ("最新", 1, frozenset({"alice"})))
        self.assertEqual(r["extra@my-plugins"].state, "不一致")
        self.assertEqual((r[TOOLS].short, r[TOOLS].mkt, r[TOOLS].name), ("tools", "my-plugins", "tools"))
        self.assertEqual(r[TOOLS].source, "owner/my-plugins")
        self.assertEqual(r[TOOLS].newer, {})
        self.assertEqual(STATES, ("最新", "有新版", "未裝", "不一致"))
        write_plugins(self.cfg, "work", installed={TOOLS: [("user", "1.1.0", None)]}, known=GOOD_KNOWN)
        self.assertEqual(self.rows()[TOOLS].state, "不一致")

    def test_listed_nowhere_installed_is_not_installed(self):
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", known=GOOD_KNOWN)
        self.assertEqual(self.rows()[TOOLS].state, "未裝")

    def test_duplicate_short_name_uses_pid(self):
        write_plugins(self.cfg, "alice", installed={"a@m1": [("user", "1", None)], "a@m2": [("user", "1", None)]})
        write_plugins(self.cfg, "work")
        r = self.rows()
        self.assertEqual((r["a@m1"].name, r["a@m2"].name), ("a@m1", "a@m2"))

    def test_card_lines_exact(self):
        write_plugins(self.cfg, "alice", installed={TOOLS: [("user", "1.0.0", None)]}, known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", known=GOOD_KNOWN)
        row = self.rows()[TOOLS]
        self.assertEqual(row.state, "不一致")
        self.assertEqual(card_lines(self.cfg, row), [
            "tools（my-plugins）", "─" * 30, "來源 owner/my-plugins",
            "alice       1.0.0", "work        —（清單有、沒裝）"])

    def test_card_lines_variants(self):
        write_plugins(self.cfg, "alice", installed={SUPER: [("user", "abcdefabcdef123", None)]}, known=GOOD_KNOWN)
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        row = self.rows()[SUPER]
        self.assertEqual(card_lines(self.cfg, row)[3:], ["alice       abcdefabcdef", "work        目錄不存在"])
        write_plugins(self.cfg, "alice", installed={"x@y": [("user", "1", None)]})
        row = self.rows()["x@y"]
        self.assertEqual(card_lines(self.cfg, row), ["x（y）", "─" * 30, "來源 本機或不支援的來源",
                                                     "alice       1（清單沒列）", "work        目錄不存在"])
        row = self.rows()[TOOLS]
        self.assertEqual(card_lines(self.cfg, row)[3], "alice       —（清單有、沒裝）")

    def test_choices_disabled_missing_dir_and_other_source(self):
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", known=GOOD_KNOWN)
        snap = snapshot(self.cfg)
        ch = profile_choices(self.cfg, snap, TOOLS, "owner/my-plugins")
        self.assertEqual([c.profile for c in ch], ["alice", "work"])
        self.assertEqual([c.disabled for c in ch], [None, None])
        self.assertEqual((ch[0].label, ch[0].listed, ch[0].installed), ("alice@example.com", True, None))
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        snap = snapshot(self.cfg)
        self.assertEqual(snap.missing, frozenset({"work"}))
        self.assertEqual(profile_choices(self.cfg, snap, TOOLS, "owner/my-plugins")[1].disabled, "目錄不存在")
        write_plugins(self.cfg, "alice", known={"my-plugins": "other/repo"})
        ch = profile_choices(self.cfg, snapshot(self.cfg), TOOLS, "owner/my-plugins")
        self.assertEqual(ch[0].disabled, "來源不同：other/repo")
        write_plugins(self.cfg, "alice", known={"my-plugins": None})
        ch = profile_choices(self.cfg, snapshot(self.cfg), TOOLS, "owner/my-plugins")
        self.assertEqual(ch[0].disabled, "來源不同：本機或不支援的來源")

    def test_mismatch(self):
        self.assertEqual(mismatch(ProfileChoice("a", "", "1", False, None)), "已裝但清單沒列")
        self.assertEqual(mismatch(ProfileChoice("a", "", None, True, None)), "清單有但沒裝")
        self.assertIsNone(mismatch(ProfileChoice("a", "", "1", True, None)))
        self.assertIsNone(mismatch(ProfileChoice("a", "", None, False, None)))

    def test_plan_changes_install_remove_reconcile_and_skip_disabled(self):
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN)
        write_plugins(self.cfg, "work")
        snap = snapshot(self.cfg)
        ch = profile_choices(self.cfg, snap, TOOLS, "owner/my-plugins")
        self.assertEqual(plan_changes(snap, ch, {"alice": True, "work": True}, "my-plugins"),
                         [Change("alice", True, True, False), Change("work", True, True, True)])
        self.assertEqual(plan_changes(snap, ch, {"alice": True, "work": False}, "my-plugins"),
                         [Change("alice", True, True, False), Change("work", None, False, False)])
        # uninstall, reconcile in both directions
        c = [ProfileChoice("alice", "", "1", True, None), ProfileChoice("work", "", "1", False, None),
             ProfileChoice("p3", "", None, True, None), ProfileChoice("p4", "", "1", True, "目錄不存在")]
        snap2 = snapshot(self.cfg)
        snap2.states.update({"work": snap2.states["work"], "p3": snap2.states["work"], "p4": snap2.states["work"]})
        got = plan_changes(snap2, c, {"alice": False, "work": True, "p3": False, "p4": False}, "my-plugins")
        self.assertEqual(got, [Change("alice", False, False, False), Change("work", None, True, False),
                               Change("p3", None, False, False)])

    def test_plan_changes_nothing_differs(self):
        write_plugins(self.cfg, "alice", installed={TOOLS: [("user", "1", None)]}, known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", installed={TOOLS: [("user", "1", None)]}, known=GOOD_KNOWN)
        snap = snapshot(self.cfg)
        ch = profile_choices(self.cfg, snap, TOOLS, "owner/my-plugins")
        self.assertEqual(plan_changes(snap, ch, {"alice": True, "work": True}, "my-plugins"), [])

    def test_preview_lines_exact(self):
        changes = [Change("alice", True, True, True), Change("work", False, False, False),
                   Change("p3", None, True, False), Change("p4", None, False, False)]
        cfg = self.cfg
        lines = preview_lines(cfg, TOOLS, changes, new_source="owner/my-plugins")
        self.assertEqual(lines, [
            "將會：",
            "  + 安裝到 alice（alice@example.com）（同時加入 my-plugins 來源）",
            "  − 從 work（work@example.com） 移除",
            "  · p3：清單加上（已經裝了）",
            "  · p4：清單拿掉（本來就沒裝）",
            "  [marketplaces] 加上 my-plugins = owner/my-plugins",
            f"設定檔 {cfg.path} 會一起更新（先備份）"])

    def test_import_is_a_leaf(self):
        code = "import sys, sampan.plugin_view; sys.exit(1 if 'sampan.ui' in sys.modules else 0)"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
