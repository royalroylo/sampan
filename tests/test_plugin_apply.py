import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from sampan.plugin_apply import apply_changes
from sampan.plugin_view import Change, snapshot
from tests.fakes import FakeRunner, cp
from tests.helpers import TOML, make_env

PID = "extra@my-plugins"
INSTALL = ["claude", "plugin", "install", PID, "--scope", "user"]
UNINSTALL = ["claude", "plugin", "uninstall", "tools@my-plugins", "--scope", "user"]


def installer(run, cfg, version="abcdef1234567890", fail=None):
    """An install rule that records the install in installed_plugins.json of the profile it ran for; fails for `fail`."""
    def rule(args):
        env = run.calls[-1]["env"]
        prof = next(p for p in cfg.profiles.values() if p.env({}).get("CLAUDE_CONFIG_DIR") == env.get("CLAUDE_CONFIG_DIR"))
        if prof.name == fail:
            return cp(1, "", "boom")
        pd = prof.config_dir / "plugins"
        pd.mkdir(parents=True, exist_ok=True)
        f = pd / "installed_plugins.json"
        data = json.loads(f.read_text()) if f.exists() else {"version": 2, "plugins": {}}
        data["plugins"][args[3]] = [{"scope": "user", "version": version}]
        f.write_text(json.dumps(data))
        return cp()
    return rule


def lists(cfg):
    return {n: p.get("plugins", []) for n, p in tomllib.loads(cfg.path.read_text())["profiles"].items()}


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.out = []

    def backups(self):
        return sorted(self.tmp.glob("config.toml.bak-*"))

    def apply(self, run, changes, **kw):
        return apply_changes(self.cfg, run, self.out.append, PID, changes, stamp=lambda: "STAMP", **kw)

    def test_install_two_profiles_one_fails(self):
        original = self.cfg.path.read_bytes()
        run = FakeRunner(strict=True)
        run.on(INSTALL, installer(run, self.cfg, fail="work"))
        res = self.apply(run, [Change("alice", True, True, False), Change("work", True, True, False)])
        self.assertEqual(lists(self.cfg)["alice"], ["tools@my-plugins", "superpowers@claude-plugins-official", PID])
        self.assertEqual(lists(self.cfg)["work"], ["tools@my-plugins"])
        self.assertEqual(res.changed, ["alice"])
        self.assertEqual(list(res.failed), ["work"])
        self.assertIn("✔ alice：已安裝 extra abcdef123456", self.out)
        bad = [line for line in self.out if line.startswith("❌ work：")]
        self.assertEqual(len(bad), 1)
        self.assertIn("boom", bad[0])
        self.assertIn("claude plugin install extra@my-plugins --scope user 失敗：boom", bad[0])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), original)
        self.assertEqual(res.backup, self.backups()[0])
        self.assertIn(f"✔ 設定檔已更新（備份 {res.backup.name}）", self.out)
        self.assertIn("⚠ work 沒有改（設定檔也沒動那幾個 profile）", self.out)

    def test_uninstall_and_reconcile_lines(self):
        run = FakeRunner(strict=True).on(UNINSTALL, cp())
        res = apply_changes(self.cfg, run, self.out.append, "tools@my-plugins",
                            [Change("alice", False, False, False), Change("work", None, False, False)],
                            stamp=lambda: "S")
        self.assertEqual(lists(self.cfg), {"alice": ["superpowers@claude-plugins-official"], "work": []})
        self.assertEqual([c["args"] for c in run.calls], [UNINSTALL])
        self.assertEqual(res.changed, ["alice"])
        self.assertEqual(res.failed, {})
        self.assertIn("✔ alice：已移除 tools", self.out)
        self.assertIn("✔ work：清單已更新", self.out)

    def test_config_only_runs_nothing(self):
        run = FakeRunner(strict=True)
        res = self.apply(run, [Change("work", None, True, False)])
        self.assertEqual(run.calls, [])
        self.assertEqual(res.changed, [])
        self.assertEqual(lists(self.cfg)["work"], ["tools@my-plugins", PID])

    def test_marketplace_added_first_and_new_source_written(self):
        cfg = self.cfg
        pid = "x@new-mkt"
        run = FakeRunner(strict=True)
        run.on(["claude", "plugin", "marketplace", "add", "owner/new-mkt"], cp())
        run.on(["claude", "plugin", "install", pid], installer(run, cfg))
        res = apply_changes(cfg, run, self.out.append, pid, [Change("work", True, True, True)],
                            new_source="owner/new-mkt", stamp=lambda: "S")
        self.assertEqual([c["args"] for c in run.calls], [
            ["claude", "plugin", "marketplace", "add", "owner/new-mkt"],
            ["claude", "plugin", "install", pid, "--scope", "user"]])
        data = tomllib.loads(cfg.path.read_text())
        self.assertEqual(data["marketplaces"]["new-mkt"], "owner/new-mkt")
        self.assertEqual(data["profiles"]["work"]["plugins"], ["tools@my-plugins", pid])
        self.assertEqual(res.changed, ["work"])

    def test_marketplace_add_uses_configured_source(self):
        run = FakeRunner(strict=True)
        run.on(["claude", "plugin", "marketplace", "add", "owner/my-plugins"], cp())
        run.on(INSTALL, installer(run, self.cfg))
        res = self.apply(run, [Change("work", True, True, True)])
        self.assertEqual(run.calls[0]["args"], ["claude", "plugin", "marketplace", "add", "owner/my-plugins"])
        self.assertEqual(res.changed, ["work"])

    def test_marketplace_add_failure_skips_install(self):
        cfg = self.cfg
        original = cfg.path.read_bytes()
        run = FakeRunner(strict=True).on(["claude", "plugin", "marketplace", "add"], cp(1, "nope", ""))
        res = apply_changes(cfg, run, self.out.append, "x@new-mkt", [Change("work", True, True, True)],
                            new_source="owner/new-mkt", stamp=lambda: "S")
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(list(res.failed), ["work"])
        self.assertEqual(cfg.path.read_bytes(), original)  # no profile succeeded: [marketplaces] not touched either
        self.assertIn("nope", res.failed["work"])

    def test_credentialed_new_source_runs_nothing(self):
        original = self.cfg.path.read_bytes()
        run = FakeRunner(strict=True)
        res = apply_changes(self.cfg, run, self.out.append, "x@new-mkt", [Change("work", True, True, True)],
                            new_source="https://bot:tok@git.example.com/o/new-mkt.git", stamp=lambda: "S")
        self.assertEqual(run.calls, [])
        self.assertEqual((res.changed, res.failed), ([], {}))
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(self.out, ["❌ new-mkt 沒有可用的來源，什麼都沒做"])

    def test_nothing_written_when_all_fail(self):
        original = self.cfg.path.read_bytes()
        run = FakeRunner(strict=True).on(INSTALL, cp(1, "", "boom"))
        res = self.apply(run, [Change("alice", True, True, False), Change("work", True, True, False)])
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(res.changed, [])
        self.assertEqual(sorted(res.failed), ["alice", "work"])
        self.assertIsNone(res.backup)
        self.assertIn("⚠ alice, work 沒有改（設定檔也沒動那幾個 profile）", self.out)

    def test_preflight_failure_runs_nothing(self):
        # a quoted table name is valid TOML but not a layout config_edit edits
        cfg = self.cfg = make_env(self.tmp, TOML.replace("[profiles.work]", '["profiles"."work"]'))
        original = cfg.path.read_bytes()
        run = FakeRunner(strict=True)
        res = apply_changes(cfg, run, self.out.append, PID,
                            [Change("alice", True, True, False), Change("work", True, True, False)], stamp=lambda: "S")
        self.assertEqual(run.calls, [])
        self.assertEqual((res.changed, res.failed, res.backup), ([], {}, None))
        self.assertEqual(cfg.path.read_bytes(), original)
        self.assertEqual(self.out, [f"❌ 設定檔無法自動修改（{cfg.path}），什麼都沒做；請手動改後跑 sampan plugins sync"])

    def test_concurrent_edit_survives(self):
        cfg = self.cfg = make_env(self.tmp, TOML + "\n[restore]\nrecent_days = 7\n")
        snapshot(cfg)
        cfg.path.write_text(cfg.path.read_text().replace("recent_days = 7", "recent_days = 3") + "# edited by hand while the screen was open\n")
        run = FakeRunner(strict=True)
        run.on(INSTALL, installer(run, cfg))
        self.apply(run, [Change("work", True, True, False)])
        text = cfg.path.read_text()
        self.assertIn("# edited by hand while the screen was open", text)
        self.assertEqual(tomllib.loads(text)["restore"]["recent_days"], 3)
        self.assertEqual(lists(cfg)["work"], ["tools@my-plugins", PID])

    def test_env_and_cwd(self):
        run = FakeRunner(strict=True)
        run.on(INSTALL, installer(run, self.cfg))
        self.apply(run, [Change("alice", True, True, False), Change("work", True, True, False)])
        alice, work = run.calls
        self.assertNotIn("CLAUDE_CONFIG_DIR", alice["env"])
        self.assertEqual(work["env"]["CLAUDE_CONFIG_DIR"], str(self.cfg.profiles["work"].config_dir))
        self.assertEqual({alice["cwd"], work["cwd"]}, {str(self.cfg.home)})

    def test_backup_keeps_mode(self):
        self.cfg.path.chmod(0o600)
        run = FakeRunner(strict=True)
        res = self.apply(run, [Change("work", None, True, False)])
        self.assertEqual(res.backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cfg.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
