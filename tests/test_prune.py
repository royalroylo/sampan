import tempfile
import unittest
from pathlib import Path

from sampan.plugins import cmd_sync, plan_prune, read_state
from tests.fakes import FakeRunner
from tests.helpers import make_env
from tests.test_plugins import GOOD_KNOWN, write_plugins

CT = "tools@my-plugins"
UA = "understand-anything@understand-anything"


class PlanPruneTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.work = self.cfg.profiles["work"]
        self.repo = self.cfg.home / "repos" / "proj"
        self.repo.mkdir(parents=True)

    def plan(self, profile="work"):
        p = self.cfg.profiles[profile]
        return plan_prune(self.cfg, p, read_state(p))

    def test_unlisted_user_plugin_is_uninstalled(self):
        write_plugins(self.cfg, "work", installed={CT: [("user", "1", None)], UA: [("user", "2", None)]},
                      known=GOOD_KNOWN)
        cmds, _skipped = self.plan()
        self.assertEqual(cmds, [(["claude", "plugin", "uninstall", UA, "--scope", "user"], None)])

    def test_local_install_is_uninstalled_from_its_directory(self):
        write_plugins(self.cfg, "work", installed={CT: [("user", "1", None), ("local", "1", str(self.repo))]},
                      known=GOOD_KNOWN)
        cmds, _skipped = self.plan()
        self.assertEqual(cmds, [(["claude", "plugin", "uninstall", CT, "--scope", "local"], str(self.repo))])

    def test_local_install_in_missing_directory_is_skipped(self):
        gone = str(self.cfg.home / "gone")
        write_plugins(self.cfg, "work", installed={CT: [("user", "1", None), ("local", "1", gone)]},
                      known=GOOD_KNOWN)
        cmds, skipped = self.plan()
        self.assertEqual(cmds, [])
        self.assertTrue(any(gone in s for s in skipped))

    def test_project_scope_in_home_is_never_touched(self):
        # ~/.claude/settings.json is alice's user settings; uninstalling there would disable alice's plugins
        write_plugins(self.cfg, "work", installed={CT: [("user", "1", None), ("project", "1", str(self.cfg.home))]},
                      known=GOOD_KNOWN)
        cmds, skipped = self.plan()
        self.assertEqual(cmds, [])
        self.assertTrue(any("~/.claude/settings.json" in s for s in skipped))

    def test_project_scope_in_a_repo_is_left_for_a_human(self):
        write_plugins(self.cfg, "work", installed={CT: [("user", "1", None), ("project", "1", str(self.repo))]},
                      known=GOOD_KNOWN)
        cmds, skipped = self.plan()
        self.assertEqual(cmds, [])
        self.assertTrue(any(str(self.repo) in s and ".claude/settings.json" in s for s in skipped))

    def test_unlisted_marketplace_is_removed_after_plugins(self):
        write_plugins(self.cfg, "work", installed={UA: [("user", "2", None)]},
                      known={**GOOD_KNOWN, "understand-anything": "Lum1104/Understand-Anything"})
        cmds, _skipped = self.plan()
        self.assertEqual([c for c, _cwd in cmds], [
            ["claude", "plugin", "uninstall", UA, "--scope", "user"],
            ["claude", "plugin", "marketplace", "remove", "understand-anything"],
        ])

    def test_clean_profile_has_nothing_to_prune(self):
        write_plugins(self.cfg, "work", installed={CT: [("user", "1", None)]}, known=GOOD_KNOWN)
        self.assertEqual(self.plan(), ([], []))


class SyncPruneCmdTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.repo = self.cfg.home / "repos" / "proj"
        self.repo.mkdir(parents=True)
        write_plugins(self.cfg, "alice", installed={CT: [("user", "1", None)]}, known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", installed={CT: [("user", "1", None), ("local", "1", str(self.repo))],
                                                     UA: [("user", "2", None)]}, known=GOOD_KNOWN)
        self.msgs = []

    def test_without_prune_nothing_is_removed(self):
        r = FakeRunner()
        cmd_sync(self.cfg, r, self.msgs.append)
        self.assertEqual(r.called(["claude", "plugin", "uninstall"]), [])

    def test_prune_runs_uninstalls_with_profile_env_and_cwd(self):
        r = FakeRunner()
        self.assertEqual(cmd_sync(self.cfg, r, self.msgs.append, prune=True), 0)
        calls = r.called(["claude", "plugin", "uninstall"])
        work_dir = str(self.cfg.profiles["work"].config_dir)
        self.assertEqual(sorted((c["args"][3], c["args"][5], c["cwd"]) for c in calls),
                         sorted([(UA, "user", str(self.cfg.home)), (CT, "local", str(self.repo))]))
        self.assertTrue(all(c["env"].get("CLAUDE_CONFIG_DIR") == work_dir for c in calls))

    def test_prune_dry_run_lists_without_running(self):
        r = FakeRunner()
        cmd_sync(self.cfg, r, self.msgs.append, prune=True, dry_run=True)
        self.assertEqual(r.calls, [])
        self.assertTrue(any("[dry-run]" in m and "uninstall" in m and UA in m for m in self.msgs))
        self.assertTrue(any("[dry-run]" in m and str(self.repo) in m for m in self.msgs))


if __name__ == "__main__":
    unittest.main()


class QuietOutputTest(unittest.TestCase):
    def test_home_project_scope_is_one_line_per_profile(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        home = str(cfg.home)
        write_plugins(cfg, "alice", installed={CT: [("user", "1", None)]}, known=GOOD_KNOWN)
        write_plugins(cfg, "work", installed={CT: [("user", "1", None), ("project", "1", home)],
                                                UA: [("project", "1", home)]}, known=GOOD_KNOWN)
        msgs = []
        cmd_sync(cfg, FakeRunner(), msgs.append, profile="work", prune=True, dry_run=True)
        self.assertEqual(len([m for m in msgs if "~/.claude/settings.json" in m]), 1)
        self.assertEqual([m for m in msgs if "project scope 安裝：" in m], [])
