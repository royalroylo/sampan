import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sampan.plugins import cmd_sync, plan_sync, read_state, status_lines
from tests.fakes import FakeRunner, cp
from tests.helpers import TOML, km_source, make_env


def write_plugins(cfg, profile, installed=None, known=None, settings=None):
    """installed: {id: [(scope, version, projectPath)]}; known: {name: repo or None}"""
    prof = cfg.profiles[profile]
    pd = prof.config_dir / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    ip = {"version": 2, "plugins": {
        pid: [{"scope": s, "version": v, **({"projectPath": pp} if pp else {})} for s, v, pp in entries]
        for pid, entries in (installed or {}).items()}}
    (pd / "installed_plugins.json").write_text(json.dumps(ip))
    km = {n: {"source": km_source(r)} for n, r in (known or {}).items()}
    (pd / "known_marketplaces.json").write_text(json.dumps(km))
    (prof.config_dir / "settings.json").write_text(json.dumps(settings or {}))


GOOD_KNOWN = {"claude-plugins-official": "anthropics/claude-plugins-official",
              "my-plugins": "owner/my-plugins"}


class StateTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_read_state(self):
        write_plugins(self.cfg, "alice",
                      installed={"tools@my-plugins": [("user", "fe133de6db43", None),
                                                                         ("project", "fe133de6db43", "/Users/u")]},
                      known={**GOOD_KNOWN, "extra-hooks": None},
                      settings={"enabledPlugins": {"tools@my-plugins": True, "x@y": False},
                                "extraKnownMarketplaces": {"extra-hooks": {}}})
        st = read_state(self.cfg.profiles["alice"])
        self.assertEqual(st.installed, {"tools@my-plugins": "fe133de6db43"})
        self.assertEqual(len(st.project_scoped), 1)
        self.assertEqual(st.enabled, {"tools@my-plugins"})
        self.assertIsNone(st.marketplaces["extra-hooks"])
        self.assertEqual(st.extra_marketplaces, {"extra-hooks"})

    def test_read_state_git_source_is_its_url(self):
        write_plugins(self.cfg, "alice", known={"my-plugins": "https://git.example.com/owner/my-plugins.git"})
        self.assertEqual(read_state(self.cfg.profiles["alice"]).marketplaces, {"my-plugins": "https://git.example.com/owner/my-plugins.git"})

    def test_read_state_strips_url_credentials(self):
        write_plugins(self.cfg, "alice", known={"my-plugins": "https://bot:secret123@git.example.com/owner/my-plugins.git"})
        self.assertEqual(read_state(self.cfg.profiles["alice"]).marketplaces, {"my-plugins": "https://git.example.com/owner/my-plugins.git"})

    def test_missing_files_are_empty(self):
        st = read_state(self.cfg.profiles["work"])
        self.assertEqual((st.installed, st.marketplaces, st.enabled), ({}, {}, set()))


class PlanSyncTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.alice = self.cfg.profiles["alice"]

    def test_install_update_and_marketplaces(self):
        write_plugins(self.cfg, "alice", installed={"tools@my-plugins": [("user", "1", None)]},
                      known={"my-plugins": "owner/my-plugins"})
        cmds, errors, _warnings = plan_sync(self.cfg, self.alice, read_state(self.alice))
        self.assertEqual(errors, [])
        self.assertEqual(cmds, [
            ["claude", "plugin", "marketplace", "add", "anthropics/claude-plugins-official"],
            ["claude", "plugin", "marketplace", "update", "claude-plugins-official"],
            ["claude", "plugin", "marketplace", "update", "my-plugins"],
            ["claude", "plugin", "update", "tools@my-plugins", "--scope", "user"],
            ["claude", "plugin", "install", "superpowers@claude-plugins-official", "--scope", "user"],
        ])

    def test_git_url_marketplace_matches(self):
        cfg = make_env(Path(tempfile.mkdtemp()), TOML.replace('"owner/my-plugins"', '"https://git.example.com/owner/my-plugins.git"'))
        alice = cfg.profiles["alice"]
        write_plugins(cfg, "alice", known={**GOOD_KNOWN, "my-plugins": "https://git.example.com/owner/my-plugins"})
        cmds, errors, _w = plan_sync(cfg, alice, read_state(alice))
        self.assertEqual(errors, [])
        self.assertIn(["claude", "plugin", "marketplace", "update", "my-plugins"], cmds)

    def test_git_url_marketplace_added_by_url(self):
        cfg = make_env(Path(tempfile.mkdtemp()), TOML.replace('"owner/my-plugins"', '"https://git.example.com/owner/my-plugins.git"'))
        write_plugins(cfg, "work", known={})
        cmds, _e, _w = plan_sync(cfg, cfg.profiles["work"], read_state(cfg.profiles["work"]))
        self.assertIn(["claude", "plugin", "marketplace", "add", "https://git.example.com/owner/my-plugins.git"], cmds)

    def test_wrong_source_blocks_that_marketplace(self):
        write_plugins(self.cfg, "alice", known={**GOOD_KNOWN, "my-plugins": None})
        cmds, errors, _w = plan_sync(self.cfg, self.alice, read_state(self.alice))
        self.assertEqual(len(errors), 1)
        self.assertFalse(any("my-plugins" in " ".join(c) for c in cmds))
        self.assertTrue(any("superpowers" in " ".join(c) for c in cmds))

    def test_warnings_for_unlisted(self):
        write_plugins(self.cfg, "alice", installed={"a@b": [("project", "1", "/Users/u")]},
                      known={**GOOD_KNOWN, "stray": "x/y"},
                      settings={"enabledPlugins": {"a@b": True}})
        _c, _e, warnings = plan_sync(self.cfg, self.alice, read_state(self.alice))
        self.assertEqual(len(warnings), 3)


class SyncCmdTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN)
        write_plugins(self.cfg, "work", known=GOOD_KNOWN)
        self.msgs = []

    def test_runs_with_profile_env_and_repo_cwd(self):
        r = FakeRunner()
        self.assertEqual(cmd_sync(self.cfg, r, self.msgs.append), 0)
        work_dir = str(self.cfg.profiles["work"].config_dir)
        work_calls = [c for c in r.calls if c["env"].get("CLAUDE_CONFIG_DIR") == work_dir]
        alice_calls = [c for c in r.calls if "CLAUDE_CONFIG_DIR" not in c["env"]]
        self.assertTrue(work_calls and alice_calls)
        self.assertTrue(all(c["cwd"] == str(self.cfg.home) for c in r.calls))
        self.assertTrue(all("--scope" in c["args"] for c in r.calls if c["args"][2] in ("install", "update")))

    def test_failure_continues_and_returns_1(self):
        r = FakeRunner().on(["claude", "plugin", "install"], cp(rc=1, err="boom"))
        self.assertEqual(cmd_sync(self.cfg, r, self.msgs.append), 1)
        self.assertTrue(r.called(["claude", "plugin", "marketplace"]))
        self.assertTrue(any("boom" in m for m in self.msgs))

    def test_sync_skips_missing_profile_dir(self):
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        r = FakeRunner()
        self.assertEqual(cmd_sync(self.cfg, r, self.msgs.append), 0)
        work_dir = str(self.cfg.profiles["work"].config_dir)
        self.assertFalse([c for c in r.calls if c["env"].get("CLAUDE_CONFIG_DIR") == work_dir])
        self.assertTrue(r.calls)  # alice syncs as usual
        self.assertFalse(self.cfg.profiles["work"].config_dir.exists())
        self.assertIn(f"⚠ work：{work_dir} 不存在，略過（不會建立）", self.msgs)

    def test_sync_missing_profile_dir_dry_run_and_prune(self):
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        r = FakeRunner()
        cmd_sync(self.cfg, r, self.msgs.append, profile="work", prune=True)
        self.assertEqual(r.calls, [])
        self.assertFalse(self.cfg.profiles["work"].config_dir.exists())

    def test_unchanged_versions_are_not_listed(self):
        alice = self.cfg.profiles["alice"]
        pids = list(alice.plugins)
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN,
                      installed={pid: [("user", "2.6.119", "")] for pid in pids})
        cmd_sync(self.cfg, FakeRunner(), self.msgs.append, profile="alice")
        self.assertFalse([m for m in self.msgs if "2.6.119 → 2.6.119" in m], self.msgs)
        self.assertIn("  沒有變動", self.msgs)
        self.assertFalse([m for m in self.msgs if "要生效需整個重開" in m])

    def test_changed_version_listed_with_restart_hint(self):
        pids = list(self.cfg.profiles["alice"].plugins)
        write_plugins(self.cfg, "alice", known=GOOD_KNOWN, installed={pid: [("user", "1.0.0", "")] for pid in pids})

        def bump(_args):
            write_plugins(self.cfg, "alice", known=GOOD_KNOWN,
                          installed={pid: [("user", "2.0.0" if pid == pids[0] else "1.0.0", "")] for pid in pids})
            return cp()
        cmd_sync(self.cfg, FakeRunner().on(["claude", "plugin"], bump), self.msgs.append, profile="alice")
        self.assertIn(f"  {pids[0]}：1.0.0 → 2.0.0", self.msgs)
        self.assertFalse([m for m in self.msgs if "1.0.0 → 1.0.0" in m])
        self.assertNotIn("  沒有變動", self.msgs)
        self.assertTrue([m for m in self.msgs if "要生效需整個重開" in m])

    def test_dry_run(self):
        r = FakeRunner()
        cmd_sync(self.cfg, r, self.msgs.append, dry_run=True)
        self.assertEqual(r.calls, [])
        self.assertTrue(any(m.strip().startswith("[dry-run] claude plugin") for m in self.msgs))


class StatusTest(unittest.TestCase):
    def test_status_marks_missing_profile(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        write_plugins(cfg, "alice")
        shutil.rmtree(cfg.profiles["work"].config_dir)
        lines = status_lines(cfg)
        self.assertIn(f"⚠ work：{cfg.profiles['work'].config_dir} 不存在，略過（不會建立）", lines)
        self.assertFalse(cfg.profiles["work"].config_dir.exists())
        self.assertTrue(any("目錄不存在" in line for line in lines))

    def test_matrix_marks_missing_extra_and_drift(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        write_plugins(cfg, "alice", installed={"tools@my-plugins": [("user", "aaa", None)],
                                             "extra@claude-plugins-official": [("user", "1", None)]})
        write_plugins(cfg, "work", installed={"tools@my-plugins": [("user", "bbb", None)]})
        text = "\n".join(status_lines(cfg))
        self.assertIn("版本不一致", text)
        self.assertIn("✗ 未裝", text)   # alice's list has superpowers but it isn't installed
        self.assertIn("1 +", text)      # extra is not in the list
