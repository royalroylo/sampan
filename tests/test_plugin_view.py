import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sampan.plugin_view import (
    STATES,
    CatalogEntry,
    Change,
    Latest,
    ProfileChoice,
    build_rows,
    card_lines,
    catalog,
    is_newer,
    mismatch,
    newest,
    plan_changes,
    preview_lines,
    profile_choices,
    snapshot,
)
from tests.fakes import FakeRunner, cp
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




def write_mkt(cfg, profile, mkt, plugins):
    d = cfg.profiles[profile].config_dir / "plugins" / "marketplaces" / mkt / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "marketplace.json").write_text(plugins if isinstance(plugins, str) else json.dumps({"plugins": plugins}))
    return d.parent


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_catalog_reads_local_marketplaces_dedup_and_first_line(self):
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", known=GOOD_KNOWN)
        write_mkt(self.cfg, "alice", "my-plugins", [{"name": "tools", "description": "First\nsecond"}, {"name": "b"}])
        write_mkt(self.cfg, "work", "my-plugins", [{"name": "tools", "description": "other"}, {"name": "a", "description": "A"}])
        write_mkt(self.cfg, "work", "broken", "{not json")
        self.assertEqual(catalog(self.cfg, snapshot(self.cfg)), [
            CatalogEntry("a@my-plugins", "A", "owner/my-plugins"),
            CatalogEntry("b@my-plugins", "", "owner/my-plugins"),
            CatalogEntry("tools@my-plugins", "First", "owner/my-plugins")])

    def test_catalog_source_falls_back_to_profile_state_and_skips_missing_profile(self):
        write_plugins(self.cfg, "alice", known={"zz": "o/zz"})
        write_mkt(self.cfg, "alice", "zz", [{"name": "p"}])
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        self.assertEqual(catalog(self.cfg, snapshot(self.cfg)), [CatalogEntry("p@zz", "", "o/zz")])


class NewestTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN)

    def test_newest_version_field_plugin_json_head_sha_and_external_sha(self):
        clone = write_mkt(self.cfg, "alice", "m", [
            {"name": "v", "version": "2.0.0", "source": "./plugins/v"},
            {"name": "pj", "source": "./plugins/pj"},
            {"name": "head", "source": "./plugins/head"},
            {"name": "ext", "source": {"source": "github", "repo": "o/r", "sha": "deadbeefcafe1234"}},
            {"name": "odd", "source": {"source": "url"}}])
        pj = clone / "plugins" / "pj" / ".claude-plugin"
        pj.mkdir(parents=True)
        (pj / "plugin.json").write_text(json.dumps({"version": "3.1.0"}))
        run = FakeRunner(strict=True).on(["git", "-C", str(clone), "rev-parse", "HEAD"], cp(out="abcdef1234567890\n"))
        n = lambda name: newest(self.cfg.profiles["alice"], "m", name, run)  # noqa: E731
        self.assertEqual(n("v"), Latest("2.0.0", None))
        self.assertEqual(n("pj"), Latest("3.1.0", None))
        self.assertEqual(n("head"), Latest("abcdef123456", None))
        self.assertEqual(n("ext"), Latest(None, "deadbeefcafe1234"))
        self.assertEqual(n("odd"), Latest(None, None))
        self.assertEqual(n("nope"), Latest(None, None))
        self.assertEqual(newest(self.cfg.profiles["alice"], "nomkt", "x", run), Latest(None, None))
        self.assertEqual(len(run.calls), 1)

    def test_newest_source_cannot_escape_clone_and_empty_version_is_absent(self):
        clone = write_mkt(self.cfg, "alice", "m", [
            {"name": "up", "source": "../../x"}, {"name": "abs", "source": "/etc/foo"},
            {"name": "link", "source": "./link"}, {"name": "empty", "version": "", "source": "./empty"}])
        outside = Path(tempfile.mkdtemp())
        (outside / ".claude-plugin").mkdir()
        (outside / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}))
        (clone / "link").symlink_to(outside)
        (clone.parent.parent / "x" / ".claude-plugin").mkdir(parents=True)
        (clone.parent.parent / "x" / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "8.8.8"}))
        e = clone / "empty" / ".claude-plugin"
        e.mkdir(parents=True)
        (e / "plugin.json").write_text(json.dumps({"version": ""}))
        run = FakeRunner(strict=True).on(["git", "-C", str(clone), "rev-parse", "HEAD"], cp(out="abcdef1234567890\n"))
        for name in ("up", "abs", "link", "empty"):
            self.assertEqual(newest(self.cfg.profiles["alice"], "m", name, run), Latest("abcdef123456", None), name)

    def test_newest_failed_git_is_unknown(self):
        clone = write_mkt(self.cfg, "alice", "m", [{"name": "head", "source": "./plugins/head"}])
        run = FakeRunner(strict=True).on(["git", "-C", str(clone), "rev-parse", "HEAD"], cp(rc=128, err="fatal"))
        self.assertEqual(newest(self.cfg.profiles["alice"], "m", "head", run), Latest(None, None))

    def test_is_newer_rules(self):
        self.assertFalse(is_newer("abcdef123456XXXX", None, Latest("abcdef123456YYYY", None)))
        self.assertTrue(is_newer("1.0.0", None, Latest("1.1.0", None)))
        self.assertTrue(is_newer(None, "aaaaaaaaaaaa1", Latest(None, "bbbbbbbbbbbb2")))
        self.assertFalse(is_newer(None, "aaaaaaaaaaaa1", Latest(None, "aaaaaaaaaaaa2")))
        self.assertFalse(is_newer("1.0.0", None, Latest(None, None)))
        self.assertFalse(is_newer(None, None, Latest("1.1.0", "bbbbbbbbbbbb")))
        self.assertFalse(is_newer("1.0.0", None, Latest(None, "bbbbbbbbbbbb")))


class NewerRowsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        for name in ("alice", "work"):
            write_plugins(self.cfg, name, installed={TOOLS: [("user", "1.0.0", None)]}, known=GOOD_KNOWN)

    def test_build_rows_with_latest_marks_newer_and_card_arrow(self):
        snap = snapshot(self.cfg)
        row = {r.pid: r for r in build_rows(self.cfg, snap, {("alice", TOOLS): Latest("1.1.0", None),
                                                             ("work", TOOLS): Latest("1.0.0", None)})}[TOOLS]
        self.assertEqual((row.state, row.newer), ("有新版", {"alice": "1.1.0"}))
        self.assertIn("alice       1.0.0 → 1.1.0", card_lines(self.cfg, row))
        self.assertIn("work        1.0.0", card_lines(self.cfg, row))
        row = {r.pid: r for r in build_rows(self.cfg, snap, {("alice", TOOLS): Latest("1.0.0", None)})}[TOOLS]
        self.assertEqual((row.state, row.newer), ("最新", {}))

    def test_commit_based_newer_uses_12_char_display(self):
        for name in ("alice", "work"):
            d = self.cfg.profiles[name].config_dir / "plugins" / "installed_plugins.json"
            data = json.loads(d.read_text())
            data["plugins"][TOOLS][0]["gitCommitSha"] = "aaaaaaaaaaaa1111"
            d.write_text(json.dumps(data))
        row = {r.pid: r for r in build_rows(self.cfg, snapshot(self.cfg), {("work", TOOLS): Latest(None, "bbbbbbbbbbbbbbbb")})}[TOOLS]
        self.assertEqual((row.state, row.newer), ("有新版", {"work": "bbbbbbbbbbbb"}))

    def test_inconsistent_beats_newer(self):
        write_plugins(self.cfg, "work", installed={TOOLS: [("user", "0.9.0", None)]}, known=GOOD_KNOWN)
        row = {r.pid: r for r in build_rows(self.cfg, snapshot(self.cfg), {("alice", TOOLS): Latest("1.1.0", None)})}[TOOLS]
        self.assertEqual((row.state, row.newer), ("不一致", {"alice": "1.1.0"}))


if __name__ == "__main__":
    unittest.main()
