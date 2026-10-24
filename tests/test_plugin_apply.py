import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from sampan import actionlog
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
            kw["why"]["busy1"] = "忙碌中"
            return 1 if kw["profile"] == "alice" else 0

        def restore_fn(cfg, run, out_, ask, **kw):
            calls.append(("restore", kw["names"], kw["confirm"], kw["on_prompt"]))
            return 0
        rc = pa.restart_profiles("cfg", "run", out.append, "ask", ["alice", "work"], own_pane="%3",
                                 on_prompt="P", stop_fn=stop_fn, restore_fn=restore_fn)
        self.assertEqual(calls, [("stop", "alice", "%3", 90), ("restore", ("s1",), False, "P"),
                                 ("stop", "work", "%3", 90)])
        self.assertEqual(rc, 1)
        self.assertEqual(out, ["⚠ 這些還在跑舊版，處理後再重開：busy1（忙碌中）、me"])


# ---- Task 10: marketplace apply ----
from unittest import mock  # noqa: E402

from sampan.config_edit import remove_marketplace  # noqa: E402
from sampan.marketplace_view import MarketChange  # noqa: E402
from tests.helpers import km_source  # noqa: E402

CRED = "❌ 網址裡不要放帳號或 token，認證交給 git 的 credential helper"


def profile_of(run, cfg):
    env = run.calls[-1]["env"]
    return next(p for p in cfg.profiles.values() if p.env({}).get("CLAUDE_CONFIG_DIR") == env.get("CLAUDE_CONFIG_DIR"))


def known(cfg, profile):
    f = cfg.profiles[profile].config_dir / "plugins" / "known_marketplaces.json"
    return json.loads(f.read_text()) if f.exists() else {}


def write_known(cfg, profile, data):
    pd = cfg.profiles[profile].config_dir / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "known_marketplaces.json").write_text(json.dumps(data))


def market_fake(run, cfg, *, name_for=lambda s: s.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"), fail=(),
                during=None):
    """`marketplace add/remove` rule: edits known_marketplaces.json of the profile it ran for; fails for `fail`."""
    def rule(args):
        prof = profile_of(run, cfg)
        if during:
            during()
        if prof.name in fail:
            return cp(1, "", "boom")
        data = known(cfg, prof.name)
        if args[3] == "add":
            name = name_for(args[4])
            if name:
                data[name] = {"source": km_source(args[4])}
        else:
            data.pop(args[4], None)
        write_known(cfg, prof.name, data)
        return cp()
    return rule


UNUSED_TOML = TOML.replace('plugins = ["tools@my-plugins", ', 'plugins = [').replace('plugins = ["tools@my-plugins"]', 'plugins = []')


def no_ask(prompt):
    raise AssertionError(f"unexpected question: {prompt}")


class MarketApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.out = []

    def backups(self):
        return sorted(self.tmp.glob("config.toml.bak-*"))

    def runner(self, **kw):
        run = FakeRunner(strict=True)
        rule = market_fake(run, self.cfg, **kw)
        run.on(["claude", "plugin", "marketplace", "add"], rule)
        run.on(["claude", "plugin", "marketplace", "remove"], rule)
        return run

    def apply(self, run, name, changes, source, ask=no_ask):
        return pa.apply_market(self.cfg, run, self.out.append, ask, name, changes, source=source, stamp=lambda: "S")

    def markets(self):
        return tomllib.loads(self.cfg.path.read_text()).get("marketplaces", {})

    def test_add_new_source_learns_name_and_writes_config(self):
        original = self.cfg.path.read_bytes()
        run = self.runner(name_for=lambda s: "fancy")
        res = self.apply(run, None, [MarketChange("alice", True, ()), MarketChange("work", True, ())], "owner/new-mkt")
        self.assertEqual([c["args"] for c in run.calls],
                         [["claude", "plugin", "marketplace", "add", "owner/new-mkt"]] * 2)
        self.assertEqual(self.markets()["fancy"], "owner/new-mkt")
        self.assertEqual(lists(self.cfg), {"alice": ["tools@my-plugins", "superpowers@claude-plugins-official"],
                                           "work": ["tools@my-plugins"]})
        self.assertEqual(res.changed, ["alice", "work"])
        self.assertEqual(res.failed, {})
        self.assertEqual(self.backups(), [res.backup])
        self.assertEqual(res.backup.read_bytes(), original)
        self.assertEqual(self.out, ["✔ alice：已加入 fancy", "✔ work：已加入 fancy", f"✔ 設定檔已更新（備份 {res.backup.name}）"])
        alice, work = run.calls
        self.assertNotIn("CLAUDE_CONFIG_DIR", alice["env"])
        self.assertEqual(work["env"]["CLAUDE_CONFIG_DIR"], str(self.cfg.profiles["work"].config_dir))
        self.assertEqual({alice["cwd"], work["cwd"]}, {str(self.cfg.home)})

    def test_add_name_not_found_keeps_config(self):
        original = self.cfg.path.read_bytes()
        res = self.apply(self.runner(name_for=lambda s: None), None, [MarketChange("alice", True, ())], "owner/new-mkt")
        self.assertIn("❌ 加入後找不到這個來源的名稱，設定檔沒改", self.out)
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assertEqual(res.changed, ["alice"])

    def test_add_name_clash_reports_and_keeps_config(self):
        original = self.cfg.path.read_bytes()
        res = self.apply(self.runner(name_for=lambda s: "my-plugins"), None,
                         [MarketChange("alice", True, ()), MarketChange("work", True, ())], "other/fork")
        self.assertIn("❌ my-plugins 在設定檔已經指向 owner/my-plugins，設定檔沒改"
                      "（alice、work 已加入的保留，可用 claude plugin marketplace remove my-plugins 移除）", self.out)
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assertIsNone(res.backup)
        self.assertEqual(known(self.cfg, "alice")["my-plugins"], {"source": km_source("other/fork")})

    def test_add_known_name_same_source_leaves_config(self):
        original = self.cfg.path.read_bytes()
        res = self.apply(self.runner(), "my-plugins", [MarketChange("work", True, ())], "owner/my-plugins")
        self.assertEqual(self.out, ["✔ work：已加入 my-plugins"])
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(res.changed, ["work"])

    def seed_both(self, name="my-plugins", source="owner/my-plugins"):
        for p in ("alice", "work"):
            write_known(self.cfg, p, {name: {"source": km_source(source)}})

    def test_remove_runs_remove_and_asks_before_dropping_from_config(self):
        self.cfg = make_env(self.tmp, UNUSED_TOML)  # no list uses my-plugins, so the line may go
        original = self.cfg.path.read_text()
        for answer, dropped in (("n", False), ("y", True)):
            self.out.clear()
            self.seed_both()
            asked = []
            run = self.runner()
            res = self.apply(run, "my-plugins", [MarketChange("alice", False, ("tools@my-plugins",)),
                                                 MarketChange("work", False, ())], None,
                             ask=lambda q: asked.append(q) or answer)
            self.assertEqual([c["args"] for c in run.calls],
                             [["claude", "plugin", "marketplace", "remove", "my-plugins"]] * 2)
            self.assertEqual(asked, ["也從設定檔 [marketplaces] 拿掉 my-plugins？(y/N): "])
            self.assertEqual(res.changed, ["alice", "work"])
            self.assertEqual(self.out[:2], ["✔ alice：已移除 my-plugins", "✔ work：已移除 my-plugins"])
            if dropped:
                self.assertEqual(self.cfg.path.read_text(), remove_marketplace(original, "my-plugins"))
                self.assertNotIn("my-plugins", self.markets())
                self.assertEqual(res.backup.read_text(), original)
            else:
                self.assertEqual(self.cfg.path.read_text(), original)
                self.assertIsNone(res.backup)

    def test_remove_not_asked_while_a_profile_still_has_it(self):
        original = self.cfg.path.read_bytes()
        self.seed_both()
        res = self.apply(self.runner(), "my-plugins", [MarketChange("alice", False, ())], None)
        self.assertEqual(res.changed, ["alice"])
        self.assertEqual(self.cfg.path.read_bytes(), original)

    def test_remove_outside_config_never_asks_or_touches_config(self):
        original = self.cfg.path.read_bytes()
        write_known(self.cfg, "alice", {"stray": {"source": km_source(None)}})
        res = self.apply(self.runner(), "stray", [MarketChange("alice", False, ())], None)
        self.assertEqual(self.out, ["✔ alice：已移除 stray"])
        self.assertEqual(res.changed, ["alice"])
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_partial_failure_only_successful_profiles_counted(self):
        run = self.runner(fail=("work",))
        res = self.apply(run, None, [MarketChange("alice", True, ()), MarketChange("work", True, ())], "owner/new-mkt")
        self.assertEqual(res.changed, ["alice"])
        self.assertEqual(list(res.failed), ["work"])
        self.assertIn("❌ work：claude plugin marketplace add owner/new-mkt 失敗：boom", self.out)
        self.assertIn("⚠ work 沒有改（設定檔也沒動那幾個 profile）", self.out)
        self.assertEqual(self.markets()["new-mkt"], "owner/new-mkt")
        # every add fails: nothing written
        original = self.cfg.path.read_bytes()
        res = self.apply(self.runner(fail=("alice", "work")), None, [MarketChange("alice", True, ())], "owner/other")
        self.assertEqual((res.changed, list(res.failed), res.backup), ([], ["alice"], None))
        self.assertEqual(self.cfg.path.read_bytes(), original)
        # a failed removal leaves the profile registered: no question
        self.seed_both()
        res = self.apply(self.runner(fail=("work",)), "my-plugins",
                         [MarketChange("alice", False, ()), MarketChange("work", False, ())], None)
        self.assertEqual(res.changed, ["alice"])
        self.assertEqual(self.cfg.path.read_bytes(), original)

    def test_credentialed_or_invalid_source_runs_nothing(self):
        original = self.cfg.path.read_bytes()
        run = FakeRunner(strict=True)
        res = self.apply(run, None, [MarketChange("alice", True, ())], "https://bot:tok@git.example.com/o/r.git")
        self.assertEqual((run.calls, res.changed, res.failed, res.backup), ([], [], {}, None))
        self.assertEqual(self.out, [CRED])
        self.out.clear()
        res = self.apply(run, "stray", [MarketChange("alice", True, ())], None)
        self.assertEqual(run.calls, [])
        self.assertEqual(len(self.out), 1)
        self.assertTrue(self.out[0].startswith("❌ 來源要寫成 "))
        self.assertEqual(self.cfg.path.read_bytes(), original)

    def test_refuse_credentials(self):
        from sampan.config import SOURCE_RULE
        self.assertEqual(pa.refuse_credentials("https://u:t@host/r.git"), CRED)
        self.assertEqual(pa.refuse_credentials("not a source"), f"❌ 來源要寫成 {SOURCE_RULE}")
        self.assertIsNone(pa.refuse_credentials("owner/repo"))
        self.assertIsNone(pa.refuse_credentials("https://host/r.git"))

    def test_update_markets_only_registered_profiles(self):
        write_known(self.cfg, "alice", {"my-plugins": {"source": km_source("owner/my-plugins")},
                                        "x": {"source": km_source("owner/x")}})
        write_known(self.cfg, "work", {"x": {"source": km_source("owner/x")}})
        run = FakeRunner(strict=True).on(mkt_update("x"), cp()).on(mkt_update("my-plugins"), cp())
        pa.update_markets(self.cfg, run, self.out.append)
        self.assertEqual([c["args"] for c in run.calls], [mkt_update("my-plugins"), mkt_update("x"), mkt_update("x")])
        self.assertEqual(self.out, ["✔ alice：my-plugins 已更新", "✔ alice：x 已更新", "✔ work：x 已更新"])
        self.assertEqual(run.calls[2]["env"]["CLAUDE_CONFIG_DIR"], str(self.cfg.profiles["work"].config_dir))
        self.assertEqual(run.calls[0]["cwd"], str(self.cfg.home))
        self.out.clear()
        run = FakeRunner(strict=True).on(mkt_update("my-plugins"), cp(1, "", "offline"))
        pa.update_markets(self.cfg, run, self.out.append, ["my-plugins"])
        self.assertEqual([c["args"] for c in run.calls], [mkt_update("my-plugins")])
        self.assertEqual(self.out, ["❌ alice：claude plugin marketplace update my-plugins 失敗：offline"])

    def test_edit_while_commands_run_survives(self):
        cfg = self.cfg = make_env(self.tmp, TOML + "\n[restore]\nrecent_days = 7\n")
        hand = "# edited by hand while the commands ran\n"

        def edit():
            if hand not in cfg.path.read_text():
                cfg.path.write_text(cfg.path.read_text().replace("recent_days = 7", "recent_days = 3") + hand)
        res = self.apply(self.runner(during=edit), None, [MarketChange("alice", True, ())], "owner/new-mkt")
        text = cfg.path.read_text()
        self.assertIn(hand, text)
        data = tomllib.loads(text)
        self.assertEqual(data["restore"]["recent_days"], 3)
        self.assertEqual(data["marketplaces"]["new-mkt"], "owner/new-mkt")
        self.assertIn(hand, res.backup.read_text())  # the backup holds the fresh on-disk bytes

    def test_write_error_reported_and_applied_returned(self):
        with mock.patch("sampan.plugin_apply.atomic_write", side_effect=OSError("disk full")):
            res = self.apply(self.runner(), None, [MarketChange("alice", True, ())], "owner/new-mkt")
        self.assertEqual(res.changed, ["alice"])
        self.assertIsNone(res.backup)
        self.assertIn("❌ 設定檔寫入失敗（disk full），指令已執行；請手動改後跑 sampan plugins sync", self.out)


class WriteConfigTest(unittest.TestCase):
    """R5: apply_changes re-reads the file before writing and survives write errors."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = []

    def test_edit_while_install_runs_survives(self):
        cfg = make_env(self.tmp, TOML + "\n[restore]\nrecent_days = 7\n")
        hand = "# edited by hand while the commands ran\n"
        run = FakeRunner(strict=True)
        inst = installer(run, cfg)

        def rule(args):
            cfg.path.write_text(cfg.path.read_text().replace("recent_days = 7", "recent_days = 3") + hand)
            return inst(args)
        run.on(INSTALL, rule)
        res = apply_changes(cfg, run, self.out.append, PID, [Change("work", True, True, False)], stamp=lambda: "S")
        text = cfg.path.read_text()
        self.assertIn(hand, text)
        self.assertEqual(tomllib.loads(text)["restore"]["recent_days"], 3)
        self.assertEqual(lists(cfg)["work"], ["tools@my-plugins", PID])
        self.assertIn(hand, res.backup.read_text())

    def test_unparseable_edit_while_install_runs_not_written(self):
        cfg = make_env(self.tmp)
        run = FakeRunner(strict=True)
        inst = installer(run, cfg)
        broken = cfg.path.read_text() + "\n[[[ broken\n"

        def rule(args):
            cfg.path.write_text(broken)
            return inst(args)
        run.on(INSTALL, rule)
        res = apply_changes(cfg, run, self.out.append, PID, [Change("work", True, True, False)], stamp=lambda: "S")
        self.assertEqual(cfg.path.read_text(), broken)
        self.assertIsNone(res.backup)
        self.assertEqual(res.changed, ["work"])
        self.assertIn(f"❌ 設定檔無法自動修改（{cfg.path}），指令已執行；請手動改後跑 sampan plugins sync", self.out)
        self.assertEqual(sorted(self.tmp.glob("config.toml.bak-*")), [])

    def test_write_error_reported_and_applied_returned(self):
        cfg = make_env(self.tmp)
        run = FakeRunner(strict=True)
        run.on(INSTALL, installer(run, cfg))
        with mock.patch("sampan.plugin_apply.atomic_write", side_effect=OSError("disk full")):
            res = apply_changes(cfg, run, self.out.append, PID, [Change("work", True, True, False)], stamp=lambda: "S")
        self.assertEqual((res.changed, res.failed, res.backup), (["work"], {}, None))
        self.assertEqual(sorted(self.tmp.glob("config.toml.bak-*")), [])  # no backup of a write that did not happen
        self.assertIn("❌ 設定檔寫入失敗（disk full），指令已執行；請手動改後跑 sampan plugins sync", self.out)


FOO = "foo@handmade"  # its marketplace is not in [marketplaces]


def loads_ok(cfg):
    from sampan.config import load_config
    return load_config(cfg.path, home=cfg.home, state_dir=cfg.state_dir)


class LoadableConfigTest(unittest.TestCase):
    """C1 / I1: a save never writes a config load_config rejects."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.out = []

    def test_list_add_without_marketplace_refused_before_any_command(self):
        original = self.cfg.path.read_bytes()
        run = FakeRunner(strict=True)
        res = apply_changes(self.cfg, run, self.out.append, FOO, [Change("alice", None, True, False)], stamp=lambda: "S")
        self.assertEqual(run.calls, [])
        self.assertEqual((res.changed, res.failed, res.backup), ([], {}, None))
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(self.out, [f"❌ 設定檔無法自動修改（{self.cfg.path}），什麼都沒做；請手動改後跑 sampan plugins sync"])

    def test_list_add_with_new_source_writes_loadable_config(self):
        res = apply_changes(self.cfg, FakeRunner(strict=True), self.out.append, FOO,
                            [Change("alice", None, True, False)], new_source="me/handmade", stamp=lambda: "S")
        self.assertIsNotNone(res.backup)
        cfg = loads_ok(self.cfg)
        self.assertEqual(cfg.marketplaces["handmade"], "me/handmade")
        self.assertIn(FOO, cfg.profiles["alice"].plugins)

    def test_market_edit_refuses_dropping_a_used_marketplace(self):
        text = self.cfg.path.read_text()
        self.assertIsNone(pa._market_edit(text, "my-plugins", None))  # both lists use tools@my-plugins
        unused = UNUSED_TOML
        self.assertIsNotNone(pa._market_edit(unused, "my-plugins", None))

    def test_remove_keeps_config_line_while_a_list_uses_it(self):
        # work lists tools@my-plugins but its directory is gone (e.g. that profile lives on another machine)
        import shutil
        self.cfg = make_env(self.tmp, TOML.replace('plugins = ["tools@my-plugins", ', 'plugins = ['))
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        write_known(self.cfg, "alice", {"my-plugins": {"source": km_source("owner/my-plugins")}})
        original = self.cfg.path.read_bytes()
        asked = []
        run = FakeRunner(strict=True)
        run.on(["claude", "plugin", "marketplace", "remove"], market_fake(run, self.cfg))
        res = pa.apply_market(self.cfg, run, self.out.append, lambda q: asked.append(q) or "y", "my-plugins",
                              [MarketChange("alice", False, ())], source="owner/my-plugins", stamp=lambda: "S")
        self.assertEqual(asked, [])
        self.assertEqual(res.changed, ["alice"])
        self.assertEqual(self.cfg.path.read_bytes(), original)
        self.assertEqual(self.out, ["✔ alice：已移除 my-plugins",
                                    "設定檔 [marketplaces] 保留 my-plugins：work 的清單還有用到它的 plugin"])
        loads_ok(self.cfg)


class RedactTest(unittest.TestCase):
    """M1: command errors shown to the user never echo a token."""

    def test_redact_userinfo_anywhere(self):
        self.assertEqual(pa.redact("fatal: https://bob:ghp_x@github.com/o/r.git denied; ssh://tok@h/x"),
                         "fatal: https://***@github.com/o/r.git denied; ssh://***@h/x")
        self.assertEqual(pa.redact("no url here; owner/repo"), "no url here; owner/repo")

    def test_install_failure_line_redacted(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        out = []
        run = FakeRunner(strict=True)
        run.on(INSTALL, cp(1, "", "clone https://x-access-token:SECRET@github.com/o/r failed"))
        apply_changes(cfg, run, out.append, PID, [Change("work", True, True, False)], stamp=lambda: "S")
        self.assertFalse(any("SECRET" in line for line in out))
        self.assertIn("https://***@github.com/o/r", "\n".join(out))
        log = actionlog.log_path(cfg).read_text()
        self.assertIn(f"[plugin 存檔 {PID}] ❌ work：", log)
        self.assertNotIn("SECRET", log)


if __name__ == "__main__":
    unittest.main()
