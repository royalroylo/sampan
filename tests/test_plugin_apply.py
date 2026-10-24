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


# ---- Task 5: update-only planner, latest check, restart helpers ----
from sampan import plugin_apply as pa  # noqa: E402
from sampan.plugin_view import Latest  # noqa: E402
from sampan.status import State, StatusRow  # noqa: E402


def seed(cfg, profile, plugins, markets=("my-plugins",), version="v1"):
    """Write installed_plugins.json and known_marketplaces.json for a profile."""
    pd = cfg.profiles[profile].config_dir / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {p: [{"scope": "user", "version": version}] for p in plugins}}))
    (pd / "known_marketplaces.json").write_text(json.dumps(
        {m: {"source": {"source": "github", "repo": f"owner/{m}"}} for m in markets}))


def mkt_update(m):
    return ["claude", "plugin", "marketplace", "update", m]


def plugin_update(pid):
    return ["claude", "plugin", "update", pid, "--scope", "user"]


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.out = []
        # alice: tools installed (listed), extra installed (not listed); work: nothing installed though tools is listed
        seed(self.cfg, "alice", ["tools@my-plugins", "extra@my-plugins"])
        seed(self.cfg, "work", [])

    def test_plan_update_only_installed_and_ignores_lists(self):
        plan = pa.plan_update(snapshot(self.cfg))
        self.assertEqual(plan, {"alice": [mkt_update("my-plugins"), plugin_update("extra@my-plugins"),
                                          plugin_update("tools@my-plugins")]})
        only = pa.plan_update(snapshot(self.cfg), ["tools@my-plugins"])
        self.assertEqual(only, {"alice": [mkt_update("my-plugins"), plugin_update("tools@my-plugins")]})
        self.assertEqual(pa.plan_update(snapshot(self.cfg), ["zzz@my-plugins"]), {})

    def test_plan_update_skips_unknown_marketplace_and_missing_profile(self):
        seed(self.cfg, "alice", ["tools@my-plugins"], markets=())
        self.assertEqual(pa.plan_update(snapshot(self.cfg)), {"alice": [plugin_update("tools@my-plugins")]})
        import shutil
        shutil.rmtree(self.cfg.profiles["alice"].config_dir)
        self.assertEqual(pa.plan_update(snapshot(self.cfg)), {})

    def test_update_plugins_reports_only_real_changes(self):
        cfg = self.cfg

        def upd(args):
            if args[3] != "extra@my-plugins":
                return cp()
            f = cfg.profiles["alice"].config_dir / "plugins" / "installed_plugins.json"
            data = json.loads(f.read_text())
            data["plugins"][args[3]] = [{"scope": "user", "version": "v2-aaaaaaaaaaaaaaaa"}]
            f.write_text(json.dumps(data))
            return cp()
        run = FakeRunner(strict=True)
        run.on(["claude", "plugin", "marketplace", "update"], cp())
        run.on(["claude", "plugin", "update"], upd)
        changed = pa.update_plugins(cfg, run, self.out.append)
        self.assertEqual(changed, ["alice"])
        arrows = [x for x in self.out if "→" in x]
        self.assertEqual(arrows, ["  alice：extra v1 → v2-aaaaaaaaa"])
        self.assertNotIn("沒有變動", self.out)
        self.out.clear()
        self.assertEqual(pa.update_plugins(cfg, run, self.out.append), [])
        self.assertIn("沒有變動", self.out)
        self.assertEqual([x for x in self.out if "→" in x], [])

    def test_update_plugins_failure_line(self):
        run = FakeRunner(strict=True)
        run.on(["claude", "plugin", "marketplace", "update"], cp())
        run.on(["claude", "plugin", "update"], cp(1, "", "boom"))
        pa.update_plugins(self.cfg, run, self.out.append, ["tools@my-plugins"])
        self.assertIn("❌ alice：claude plugin update tools@my-plugins --scope user 失敗：boom", self.out)

    def test_check_latest_runs_marketplace_update_then_reads(self):
        cfg = self.cfg
        d = cfg.profiles["alice"].config_dir / "plugins" / "marketplaces" / "my-plugins" / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "marketplace.json").write_text(json.dumps({"plugins": [{"name": "tools", "version": "9.9"}]}))
        run = FakeRunner(strict=True).on(mkt_update("my-plugins"), cp())
        res = pa.check_latest(cfg, run, self.out.append, snapshot(cfg))
        self.assertEqual([c["args"] for c in run.calls], [mkt_update("my-plugins")])
        self.assertEqual(self.out, ["▶ alice：更新 my-plugins 來源…"])
        self.assertEqual(res[("alice", "tools@my-plugins")], Latest("9.9", None))
        self.assertEqual(res[("alice", "extra@my-plugins")], Latest(None, None))
        self.assertEqual(set(res), {("alice", "tools@my-plugins"), ("alice", "extra@my-plugins")})

    def test_check_latest_failure_continues(self):
        run = FakeRunner(strict=True).on(mkt_update("my-plugins"), cp(1, "", "offline"))
        res = pa.check_latest(self.cfg, run, self.out.append, snapshot(self.cfg))
        self.assertEqual(len(res), 2)
        self.assertTrue(any(x.startswith("⚠ ") and "offline" in x for x in self.out))


class RestartTest(unittest.TestCase):
    def test_running_counts_and_hint_exact(self):
        rows = [StatusRow("a", "claude", "alice", "s", State.RUNNING),
                StatusRow("b", "claude", "alice", "s", State.RUNNING),
                StatusRow("c", "claude", "work", "s", State.STOPPED),
                StatusRow("d", "codex", "", "s", State.RUNNING),
                StatusRow("e", "claude", "other", "s", State.RUNNING)]
        counts = pa.running_counts(rows, ["alice", "work"])
        self.assertEqual(counts, {"alice": 2})
        self.assertEqual(pa.restart_hint(counts), "要生效需重開 alice 的 session（2 個）：按 R 立刻重開，其他鍵稍後")
        self.assertEqual(pa.restart_hint({"a": 1, "b": 3}),
                         "要生效需重開 a 的 session（1 個）、b 的 session（3 個）：按 R 立刻重開，其他鍵稍後")
        self.assertIsNone(pa.restart_hint({}))

    def test_restart_profiles_restores_only_stopped_and_lists_left(self):
        out, calls = [], []

        def stop_fn(cfg, run, out_, **kw):
            calls.append(("stop", kw["profile"], kw["own_pane"], kw["timeout"]))
            kw["stopped"].extend(["s1"] if kw["profile"] == "alice" else [])
            kw["left"].extend(["busy1", "me"])
            return 1 if kw["profile"] == "alice" else 0

        def restore_fn(cfg, run, out_, ask, **kw):
            calls.append(("restore", kw["names"], kw["confirm"], kw["on_prompt"]))
            return 0
        rc = pa.restart_profiles("cfg", "run", out.append, "ask", ["alice", "work"], own_pane="%3",
                                 on_prompt="P", stop_fn=stop_fn, restore_fn=restore_fn)
        self.assertEqual(calls, [("stop", "alice", "%3", 90), ("restore", ("s1",), False, "P"),
                                 ("stop", "work", "%3", 90)])
        self.assertEqual(rc, 1)
        self.assertEqual(out, ["⚠ 這些還在跑舊版，閒下來後再重開：busy1、me"])
