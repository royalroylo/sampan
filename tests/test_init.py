import json
import plistlib
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from sampan import launchd
from sampan.config import load_config
from sampan.hook import hook_status, install_hook
from sampan.init import Detected, cmd_init, cmd_profile_add, detect_profiles, render_config
from sampan.procs import Proc
from tests.fakes import FakeRunner, cp
from tests.helpers import km_source

NO_PROC = Path("/nonexistent-proc-root")


def write_plugin_files(config_dir, installed=(), known=None):
    pd = Path(config_dir) / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {pid: [{"scope": "user", "version": "1.0.0"}] for pid in installed}}))
    km = {n: {"source": km_source(r)} for n, r in (known or {}).items()}
    (pd / "known_marketplaces.json").write_text(json.dumps(km))


class Runner(FakeRunner):
    """FakeRunner whose `claude auth status` answer depends on CLAUDE_CONFIG_DIR in env."""

    def __init__(self, emails, ps_env=None):
        super().__init__()
        self.emails = emails  # {config_dir str or None(default): email}
        self.ps_env = ps_env or {}  # {pid: "VAR=x VAR2=y"}

    def __call__(self, args, *, env=None, cwd=None):
        args = list(args)
        if args[:3] == ["claude", "auth", "status"]:
            self.calls.append({"args": args, "env": env, "cwd": cwd})
            email = self.emails.get((env or {}).get("CLAUDE_CONFIG_DIR"))
            return cp(out=json.dumps({"loggedIn": True, "email": email})) if email else cp(rc=1, err="not logged in")
        if args[:2] == ["ps", "eww"]:
            self.calls.append({"args": args, "env": env, "cwd": cwd})
            pid = int(args[-1])
            return cp(out=f"claude {self.ps_env[pid]}") if pid in self.ps_env else cp(rc=1)
        return super().__call__(args, env=env, cwd=cwd)


def loads(run):
    """launchctl calls that change state (bootout/bootstrap); `launchctl print` is a read-only check."""
    return [c["args"] for c in run.calls if c["args"][:1] == ["launchctl"] and c["args"][1] != "print"]


def claude_procs(*pids):
    return {pid: Proc(pid, 1, "S", "claude", "claude") for pid in pids}


def asker(answers=None, prompts=None):
    """Answer by the first key contained in the prompt; everything else gets Enter ("")."""
    answers = answers or {}

    def ask(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return next((v for k, v in answers.items() if k in prompt), "")
    return ask


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude-profiles" / "work").mkdir(parents=True)
        self.work = self.home / ".claude-profiles" / "work"
        self.cfg_path = self.tmp / "config" / "config.toml"
        self.state = self.tmp / "state"
        self.sampan = self.tmp / "bin" / "sampan"
        self.sampan.parent.mkdir()
        self.sampan.write_text("#!/bin/sh\n")
        self.lines = []
        self.env = {"PATH": f"{self.home}/.local/bin:/usr/bin"}
        write_plugin_files(self.home / ".claude", ["superpowers@claude-plugins-official"],
                           {"claude-plugins-official": "anthropics/claude-plugins-official"})
        write_plugin_files(self.work, ["tools@my-plugins"], {"my-plugins": "owner/my-plugins"})

    def runner(self, **kw):
        return Runner({None: "alice@example.com", str(self.work): "work@example.com"}, **kw)

    def init(self, run, ask, **kw):
        kw.setdefault("env", self.env)
        kw.setdefault("platform", "darwin")  # launchd steps are macOS-only
        kw.setdefault("which", lambda name: None)
        kw.setdefault("deps", lambda out, ask: True)
        kw.setdefault("procs", {})
        kw.setdefault("proc_root", NO_PROC)
        return cmd_init(self.cfg_path, self.state, run, self.lines.append, ask, home=self.home,
                        sampan_path=self.sampan, **kw)

    def text(self):
        return "\n".join(self.lines)


class DepsTests(Base):
    def test_missing_deps_stop_before_anything_is_written(self):
        rc = self.init(self.runner(), lambda _p: "", deps=lambda out, ask: out("缺少 tmux") or False)
        self.assertEqual(rc, 1)
        self.assertFalse(self.cfg_path.exists())
        self.assertIn("缺少 tmux", self.text())


class DetectTests(Base):
    def test_detect_default_and_subdirs_and_env_dirs(self):
        other = self.tmp / "elsewhere" / "other"
        other.mkdir(parents=True)
        (self.home / ".claude-profiles" / "notes.txt").write_text("not a dir")
        r = self.runner(ps_env={
            10: f"CLAUDE_CONFIG_DIR={other} PATH=/usr/bin",
            11: f"CLAUDE_CONFIG_DIR={self.work} PATH=/usr/bin",  # already found via the subdirectory, deduplicated
            12: "PATH=/usr/bin HOME=/Users/u",
            13: f"CLAUDE_CONFIG_DIR={other} PATH=/usr/bin",
        })
        found = detect_profiles(self.home, r, claude_procs(10, 11, 12, 13), proc_root=NO_PROC)
        self.assertEqual([d.config_dir for d in found], [self.home / ".claude", self.work, other])
        self.assertEqual([d.default for d in found], [True, False, False])
        self.assertEqual(found[0].plugins, ["superpowers@claude-plugins-official"])
        self.assertEqual(found[0].marketplaces, {"claude-plugins-official": "anthropics/claude-plugins-official"})
        self.assertEqual(found[1].plugins, ["tools@my-plugins"])
        self.assertEqual(found[2].plugins, [])
        envs = {c["env"].get("CLAUDE_CONFIG_DIR") for c in r.called(["claude", "auth", "status"])}
        self.assertEqual(envs, {None, str(self.work), str(other)})

    def test_detect_profiles_skips_retired(self):
        (self.home / ".claude-profiles" / "old.retired.20261018").mkdir()
        found = detect_profiles(self.home, self.runner(), {}, proc_root=NO_PROC)
        self.assertEqual([d.config_dir for d in found], [self.home / ".claude", self.work])

    def test_name_suggestions(self):
        found = detect_profiles(self.home, self.runner(), {}, proc_root=NO_PROC)
        self.assertEqual([(d.name, d.email) for d in found],
                         [("alice", "alice@example.com"), ("work", "work@example.com")])
        found = detect_profiles(self.home, Runner({}), {}, proc_root=NO_PROC)
        self.assertEqual([(d.name, d.email) for d in found], [("default", None), ("work", None)])


class RenderTests(unittest.TestCase):
    def test_render_config_roundtrips(self):
        home = Path(tempfile.mkdtemp())
        detected = [
            Detected(home / ".claude", "alice@example.com", "alice",
                     ["superpowers@claude-plugins-official", "local-thing@my-dir"],
                     {"claude-plugins-official": "anthropics/claude-plugins-official", "my-dir": None}, True),
            Detected(home / ".claude-profiles" / "work", None, "work", ["tools@my-plugins", "ct@gitea"],
                     {"my-plugins": "owner/my-plugins", "gitea": "https://git.example.com/owner/my-plugins.git"}, False),
        ]
        text = render_config(detected, codex_enabled=False, codex_home=home / ".codex", home=home)
        path = home / "config.toml"
        path.write_text(text)
        cfg = load_config(path, home=home)
        self.assertEqual(cfg.profiles["alice"].plugins, ("superpowers@claude-plugins-official",))
        self.assertEqual(cfg.profiles["work"].plugins, ("tools@my-plugins", "ct@gitea"))
        self.assertTrue(cfg.profiles["alice"].default)
        self.assertEqual(cfg.profiles["alice"].config_dir, home / ".claude")
        self.assertEqual(cfg.profiles["alice"].label, "alice@example.com")
        self.assertEqual(cfg.marketplaces, {"claude-plugins-official": "anthropics/claude-plugins-official",
                                            "my-plugins": "owner/my-plugins", "gitea": "https://git.example.com/owner/my-plugins.git"})
        self.assertFalse(cfg.codex.enabled)
        self.assertEqual(cfg.codex.home, home / ".codex")
        self.assertEqual(cfg.recent_days, 7)
        self.assertIn('config_dir = "~/.claude"', text)
        comment = text[:text.index("[profiles.alice]")].splitlines()[-1]
        self.assertTrue(comment.startswith("#"), comment)
        self.assertIn("local-thing@my-dir", comment)

    def test_render_quotes_unusual_values(self):
        home = Path(tempfile.mkdtemp())
        d = Detected(Path("/Users/u/我的 設定"), 'a"b@example.com', "alice", [], {}, True)
        path = home / "c.toml"
        path.write_text(render_config([d], codex_enabled=True, codex_home=Path("/x/codex")))
        cfg = load_config(path, home=home)
        self.assertEqual(cfg.profiles["alice"].config_dir, Path("/Users/u/我的 設定"))
        self.assertEqual(cfg.profiles["alice"].label, 'a"b@example.com')


class InitFirstRunTests(Base):
    def test_first_run_writes_config_installs_launchd(self):
        r = self.runner()
        prompts = []
        self.assertEqual(self.init(r, asker({"work": "team"}, prompts)), 0)
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual(sorted(cfg.profiles), ["alice", "team"])
        self.assertEqual(cfg.profiles["team"].plugins, ("tools@my-plugins",))
        self.assertIn("[profiles.team]", self.text())  # full text is shown first
        self.assertEqual(launchd.check(self.home, self.sampan)[0], "ok")
        self.assertEqual(len(r.called(["launchctl", "bootstrap"])), 1)
        self.assertFalse(any("PATH" in line and "export" in line for line in self.lines))

    def test_first_run_asks_before_installing_launchd(self):
        r = self.runner()
        prompts = []
        self.assertEqual(self.init(r, asker({"排程": "n"}, prompts)), 0)
        plist = str(launchd.plist_path(self.home))
        summary = next(i for i, line in enumerate(self.lines) if plist in line)
        written = next(i for i, line in enumerate(self.lines) if "已寫入" in line)
        self.assertLess(summary, written)  # the summary before writing already lists the schedule
        self.assertTrue(any("排程" in p and "[Y/n]" in p for p in prompts))
        self.assertTrue(any("每 5 分鐘 sampan save --quiet" in p for p in prompts))
        self.assertFalse(any("每小時" in x for x in self.lines + prompts))
        self.assertFalse(launchd.plist_path(self.home).exists())
        self.assertEqual(loads(r), [])
        self.assertTrue(self.cfg_path.exists())

    def test_no_launchd_flag_skips_schedule(self):
        r = self.runner()
        prompts = []
        self.assertEqual(self.init(r, asker({}, prompts), no_launchd=True), 0)
        self.assertFalse(any("排程" in p for p in prompts))
        self.assertFalse(launchd.plist_path(self.home).exists())
        self.assertEqual(r.called(["launchctl"]), [])
        self.assertFalse(any(str(launchd.plist_path(self.home)) in line for line in self.lines))

    def test_non_macos_skips_launchd_and_says_so(self):
        r = self.runner()
        prompts = []
        self.assertEqual(self.init(r, asker({}, prompts), platform="linux"), 0)
        self.assertFalse(any("排程" in p for p in prompts))
        self.assertFalse(launchd.plist_path(self.home).exists())
        self.assertEqual(r.called(["launchctl"]), [])
        self.assertTrue(any("不是 macOS" in x and "sampan save" in x for x in self.lines), self.lines)
        self.assertFalse(any(str(launchd.plist_path(self.home)) in x for x in self.lines))
        self.assertTrue(self.cfg_path.exists())
        note = "\n".join(self.lines)
        self.assertIn("*/5 * * * * $HOME/.local/bin/sampan save --quiet", note)  # a line to paste, not just "use cron"
        self.assertIn("docs/install.md#linux", note)

    def test_sampan_env_overrides_go_into_plist(self):
        env = dict(self.env, SAMPAN_CONFIG=str(self.cfg_path), SAMPAN_STATE_DIR=str(self.state))
        self.assertEqual(self.init(self.runner(), asker(), env=env), 0)
        data = plistlib.loads(launchd.plist_path(self.home).read_bytes())
        self.assertEqual(data["EnvironmentVariables"]["SAMPAN_CONFIG"], str(self.cfg_path))
        self.assertEqual(data["EnvironmentVariables"]["SAMPAN_STATE_DIR"], str(self.state))
        # init re-run with a different SAMPAN_CONFIG: the values in the schedule differ -> propose reinstalling
        self.lines.clear()
        prompts = []
        r = self.runner()
        self.assertEqual(self.init(r, asker({}, prompts), env=self.env), 0)
        self.assertTrue(any("排程" in p and "SAMPAN_CONFIG" in p for p in prompts))
        data = plistlib.loads(launchd.plist_path(self.home).read_bytes())
        self.assertNotIn("SAMPAN_CONFIG", data["EnvironmentVariables"])

    def test_failed_bootstrap_is_retried_on_rerun(self):
        r = self.runner().on(["launchctl", "bootstrap"], cp(rc=5, err="Input/output error"))
        self.assertEqual(self.init(r, asker()), 0)
        self.assertTrue(any(line.startswith("⚠") and "bootstrap" in line for line in self.lines))
        self.assertEqual(launchd.check(self.home, self.sampan)[0], "ok")  # the plist itself is correct
        self.lines.clear()
        r = self.runner().on(["launchctl", "print"], cp(rc=113, err="Could not find service"))
        prompts = []
        self.assertEqual(self.init(r, asker({}, prompts)), 0)
        self.assertTrue(any("排程" in p and "沒有載入" in p for p in prompts))
        self.assertEqual(len(r.called(["launchctl", "bootstrap"])), 1)
        self.assertFalse(any("已安裝並已載入" in line for line in self.lines))

    def test_plist_write_failure_does_not_claim_bootstrap_failed(self):
        launchd.plist_path(self.home).parent.parent.mkdir(parents=True)
        launchd.plist_path(self.home).parent.write_text("a file, not a dir")
        r = self.runner()
        self.assertEqual(self.init(r, asker()), 0)
        self.assertTrue(any("寫不進" in line for line in self.lines))
        self.assertFalse(any("bootstrap" in line for line in self.lines))
        self.assertEqual(loads(r), [])

    def test_declining_write_leaves_nothing(self):
        r = self.runner()
        self.assertEqual(self.init(r, asker({"寫入": "n"})), 1)
        self.assertFalse(self.cfg_path.exists())
        self.assertFalse(launchd.plist_path(self.home).exists())
        self.assertEqual(r.called(["launchctl"]), [])

    def test_invalid_or_duplicate_name_is_asked_again(self):
        answers = iter(["bad name", "alice", "team"])
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return next(answers) if "名稱" in prompt and "work" in prompt else ""
        self.assertEqual(self.init(self.runner(), ask), 0)
        self.assertEqual(sorted(load_config(self.cfg_path, home=self.home).profiles), ["alice", "team"])
        self.assertEqual(sum("名稱" in p and "work" in p for p in prompts), 3)

    def test_hook_offer_default_no(self):
        prompts = []
        self.init(self.runner(), asker({}, prompts), hook=True)
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual({hook_status(p) for p in cfg.profiles.values()}, {"absent"})
        self.assertTrue(any("hook" in p and "[y/N]" in p for p in prompts))

        self.cfg_path.unlink()
        self.init(self.runner(), asker({"hook": "y"}), hook=True)
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual({hook_status(p) for p in cfg.profiles.values()}, {"installed"})

    def test_init_yes_takes_defaults_without_reading_stdin(self):
        def ask(prompt):
            raise AssertionError(f"asked: {prompt}")
        self.assertEqual(self.init(self.runner(), ask, yes=True), 0)
        self.assertEqual(sorted(load_config(self.cfg_path, home=self.home).profiles), ["alice", "work"])
        questions = [line for line in self.lines if "[Y/n]" in line or "[y/N]" in line or "名稱" in line]
        self.assertTrue(questions)
        self.assertTrue(all(line.endswith(" → 預設（--yes）") for line in questions))
        self.assertFalse(any("  → 預設" in line for line in questions))

    def test_init_does_not_offer_hook_without_flag(self):
        prompts = []
        self.init(self.runner(), asker({"hook": "y"}, prompts))
        self.assertNotIn("SessionStart hook", self.text())
        self.assertFalse(any("hook" in p for p in prompts))
        for d in (self.home / ".claude", self.work):
            self.assertFalse((d / "settings.json").exists())

    def test_init_offers_hook_with_flag(self):
        self.init(self.runner(), asker({"hook": "y"}), hook=True)
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual({hook_status(p) for p in cfg.profiles.values()}, {"installed"})

    def test_codex_detection(self):
        self.init(self.runner(), asker())
        self.assertFalse(load_config(self.cfg_path, home=self.home).codex.enabled)
        self.cfg_path.unlink()
        self.init(self.runner(), asker(), which=lambda name: "/opt/homebrew/bin/codex" if name == "codex" else None)
        self.assertTrue(load_config(self.cfg_path, home=self.home).codex.enabled)
        self.cfg_path.unlink()
        (self.home / ".codex").mkdir()
        self.init(self.runner(), asker())
        self.assertTrue(load_config(self.cfg_path, home=self.home).codex.enabled)

    def test_launchd_failure_only_warns_and_path_hint(self):
        r = self.runner().on(["launchctl", "bootstrap"], cp(rc=5, err="boom"))
        self.assertEqual(self.init(r, asker(), env={"PATH": "/usr/bin:/bin"}), 0)
        self.assertTrue(any(line.startswith("⚠") and "排程" in line for line in self.lines))
        self.assertTrue(any('export PATH="$HOME/.local/bin:$PATH"' in line for line in self.lines))

    def test_missing_default_dir_is_an_error(self):
        shutil.rmtree(self.home / ".claude")
        self.assertEqual(self.init(self.runner(), asker()), 1)
        self.assertFalse(self.cfg_path.exists())


EXISTING = """# 我的設定
version = 1

[codex]
enabled = false

[marketplaces]
claude-plugins-official = "anthropics/claude-plugins-official"

[profiles.alice]
config_dir = "~/.claude"
default = true
label = "手改的 label"  # 自己寫的註解
plugins = [
  "superpowers@claude-plugins-official",
]

[profiles.work]
config_dir = "~/.claude-profiles/work"
label = "公司"
plugins = []
"""


class InitRerunTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text(EXISTING)
        write_plugin_files(self.home / ".claude",
                           ["superpowers@claude-plugins-official", "extra@claude-plugins-official"],
                           {"claude-plugins-official": "anthropics/claude-plugins-official"})
        self.extra = self.home / ".claude-profiles" / "extra"
        write_plugin_files(self.extra, ["tools@my-plugins"], {"my-plugins": "owner/my-plugins"})
        launchd.install(FakeRunner(), self.sampan, self.state, self.home)

    def test_rerun_only_proposes_additions(self):
        prompts = []
        self.assertEqual(self.init(self.runner(), asker({}, prompts)), 0)
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual(cfg.profiles["alice"].label, "手改的 label")
        self.assertEqual(cfg.profiles["work"].label, "公司")
        self.assertFalse(cfg.codex.enabled)
        self.assertEqual(cfg.profiles["alice"].plugins,
                         ("superpowers@claude-plugins-official", "extra@claude-plugins-official"))
        self.assertEqual(cfg.profiles["work"].plugins, ("tools@my-plugins",))
        self.assertEqual(cfg.profiles["extra"].plugins, ("tools@my-plugins",))
        self.assertEqual(cfg.marketplaces["my-plugins"], "owner/my-plugins")
        text = self.cfg_path.read_text()
        self.assertIn("# 自己寫的註解", text)
        self.assertTrue(text.startswith("# 我的設定\n"))
        self.assertEqual(sum("名稱" in p for p in prompts), 1)  # only the new profile's name is asked
        self.assertTrue(any("extra@claude-plugins-official" in p for p in prompts))
        self.assertFalse(any("superpowers@claude-plugins-official" in p for p in prompts))

    def test_rerun_refusing_items_leaves_file_unchanged(self):
        before = self.cfg_path.read_bytes()
        prompts = []
        self.assertEqual(self.init(self.runner(), asker({"新增": "n"}, prompts)), 0)
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertFalse(any("寫入" in p for p in prompts))

    def test_rerun_refusing_write_leaves_file_unchanged(self):
        before = self.cfg_path.read_bytes()
        self.assertEqual(self.init(self.runner(), asker({"寫入": "n"})), 1)
        self.assertEqual(self.cfg_path.read_bytes(), before)

    def test_rerun_with_nothing_new_does_not_write(self):
        (self.home / ".claude" / "plugins" / "installed_plugins.json").unlink()
        write_plugin_files(self.work, [], {})
        shutil.rmtree(self.extra)
        before = self.cfg_path.read_bytes()
        prompts = []
        self.assertEqual(self.init(self.runner(), asker({}, prompts)), 0)
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertFalse(any("寫入" in p for p in prompts))

    def test_rerun_skips_marketplace_with_conflicting_source(self):
        write_plugin_files(self.extra, ["x@claude-plugins-official"],
                           {"claude-plugins-official": "someone/fork"})
        self.init(self.runner(), asker())
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual(cfg.profiles["extra"].plugins, ())
        self.assertEqual(cfg.marketplaces["claude-plugins-official"], "anthropics/claude-plugins-official")
        self.assertIn("x@claude-plugins-official", self.text())

    def test_rerun_ignores_disabled_profile_dir_and_plugins(self):
        self.cfg_path.write_text(EXISTING.replace('label = "公司"', 'label = "公司"\ndisabled = true'))
        shutil.rmtree(self.extra)
        prompts = []
        self.assertEqual(self.init(self.runner(), asker({}, prompts)), 0)
        self.assertFalse(any("work" in p for p in prompts))  # neither re-added as a new profile nor offered its plugins
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertTrue(cfg.all_profiles["work"].disabled)
        self.assertEqual(cfg.all_profiles["work"].plugins, ())

    def test_rerun_new_dir_named_like_disabled_profile_gets_another_name(self):
        self.cfg_path.write_text(EXISTING + '\n[profiles.extra]\nconfig_dir = "~/elsewhere"\ndisabled = true\n')
        prompts = []
        self.assertEqual(self.init(self.runner(), asker({}, prompts)), 0)
        self.assertTrue(any("[extra-2]" in p for p in prompts))
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual(cfg.profiles["extra-2"].config_dir, self.extra)
        self.assertTrue(cfg.all_profiles["extra"].disabled)

    def test_rerun_adds_plugin_from_git_marketplace(self):
        # Another machine: the first init skipped the Gitea-sourced plugin; after the fix, re-running init must add it to the existing profile
        git = "https://git.example.com/owner/my-plugins.git"
        write_plugin_files(self.work, ["tools@gitea-plugins"], {"gitea-plugins": "https://bot:secret123@git.example.com/owner/my-plugins.git"})
        prompts = []
        self.init(self.runner(), asker({}, prompts))
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual(cfg.profiles["work"].plugins, ("tools@gitea-plugins",))
        self.assertEqual(cfg.marketplaces["gitea-plugins"], git)
        self.assertTrue(any("tools@gitea-plugins" in p for p in prompts))
        self.assertNotIn("略過新安裝的 tools", self.text())
        self.assertNotIn("secret123", self.cfg_path.read_text() + self.text())

    def test_rerun_offers_reinstall_for_old_interval_schedule(self):
        path = launchd.plist_path(self.home)
        data = plistlib.loads(path.read_bytes())
        del data["StartCalendarInterval"]
        data["StartInterval"] = 3600
        path.write_bytes(plistlib.dumps(data))
        prompts = []
        r = self.runner()
        self.init(r, asker({}, prompts))
        self.assertTrue(any("排程" in p and "睡眠" in p for p in prompts), prompts)
        self.assertEqual(launchd.check(self.home, self.sampan)[0], "ok")
        self.assertEqual(len(r.called(["launchctl", "bootstrap"])), 1)

    def test_rerun_offers_launchd_reinstall_when_wrong(self):
        path = launchd.plist_path(self.home)
        path.write_bytes(plistlib.dumps({"ProgramArguments": ["/old/sampan", "save", "--quiet"]}))
        r = self.runner()
        self.init(r, asker({"排程": "n"}))
        self.assertEqual(launchd.check(self.home, self.sampan)[0], "wrong")
        self.assertEqual(loads(r), [])
        r = self.runner()
        self.init(r, asker())
        self.assertEqual(launchd.check(self.home, self.sampan)[0], "ok")
        self.assertEqual(len(r.called(["launchctl", "bootstrap"])), 1)

    def test_rerun_leaves_correct_launchd_alone(self):
        r = self.runner()
        prompts = []
        self.init(r, asker({}, prompts))
        self.assertEqual(loads(r), [])
        self.assertFalse(any("排程" in p for p in prompts))

    def test_rerun_offers_stale_hook_repair(self):
        old = self.tmp / "old" / "sampan"
        old.parent.mkdir()
        old.write_text("")
        cfg = load_config(self.cfg_path, home=self.home)
        install_hook(cfg.profiles["work"], old)
        old.unlink()
        self.assertEqual(hook_status(cfg.profiles["work"]), "stale")
        self.init(self.runner(), asker({"修復": "n"}))
        self.assertEqual(hook_status(cfg.profiles["work"]), "stale")
        self.init(self.runner(), asker())
        self.assertEqual(hook_status(cfg.profiles["work"]), "installed")
        settings = json.loads((self.work / "settings.json").read_text())
        self.assertEqual([h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]],
                         [f"{self.sampan} hook session-start"])

    def test_init_still_repairs_broken_hook_without_flag(self):
        old = self.tmp / "old" / "sampan"
        old.parent.mkdir()
        old.write_text("")
        cfg = load_config(self.cfg_path, home=self.home)
        install_hook(cfg.profiles["work"], old)
        old.unlink()
        prompts = []
        self.init(self.runner(), asker({"修復": "n"}, prompts))
        self.assertTrue(any("修復" in p for p in prompts))
        self.assertFalse(any("安裝 SessionStart" in p for p in prompts))

    def test_broken_existing_config_is_not_touched(self):
        self.cfg_path.write_text("version = 2\n")
        self.assertEqual(self.init(self.runner(), asker()), 2)
        self.assertEqual(self.cfg_path.read_text(), "version = 2\n")


class ProfileAddTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text(EXISTING)
        self.cfg = load_config(self.cfg_path, home=self.home, state_dir=self.state)
        self.logins = []

    def interactive(self, args, env):
        self.logins.append((list(args), env.get("CLAUDE_CONFIG_DIR")))
        return 0

    def add(self, run, name, dir=None):
        return cmd_profile_add(self.cfg, run, self.lines.append, name, dir=dir, home=self.home,
                               env={"PATH": "/usr/bin"}, interactive=self.interactive)

    def test_profile_add_new_dir_and_existing_dir(self):
        help_text = "Commands:\n  login [options]   Sign in to your Anthropic account\n  status [options]  Show\n"
        r = self.runner().on(["claude", "auth", "--help"], cp(out=help_text))
        self.assertEqual(self.add(r, "side"), 0)
        new_dir = self.home / ".claude-profiles" / "side"
        self.assertTrue(new_dir.is_dir())
        self.assertEqual(self.logins, [(["claude", "auth", "login"], str(new_dir))])
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual(cfg.profiles["side"].config_dir, new_dir)
        self.assertFalse(cfg.profiles["side"].default)
        self.assertEqual(cfg.profiles["alice"].label, "手改的 label")
        self.assertIn("# 自己寫的註解", self.cfg_path.read_text())

        # already exists -> error
        self.cfg = cfg
        before = self.cfg_path.read_bytes()
        (self.home / ".claude-profiles" / "dup").mkdir()
        self.assertEqual(self.add(r, "dup"), 1)
        self.assertEqual(self.cfg_path.read_bytes(), before)

        # --dir does not exist -> error, nothing created
        self.logins.clear()
        missing = self.tmp / "nope"
        self.assertEqual(self.add(r, "nope", dir=missing), 1)
        self.assertFalse(missing.exists())
        self.assertEqual(self.cfg_path.read_bytes(), before)

        # --dir already exists -> don't create it, don't log in, just write it into the config
        given = self.tmp / "given dir"
        given.mkdir()
        self.assertEqual(self.add(r, "given", dir=given), 0)
        self.assertEqual(self.logins, [])
        self.assertEqual(load_config(self.cfg_path, home=self.home).profiles["given"].config_dir, given)

    def test_profile_add_without_login_command_prints_fallback(self):
        r = self.runner().on(["claude", "auth", "--help"], cp(out="Commands:\n  status   Show\n"))
        self.assertEqual(self.add(r, "side"), 0)
        self.assertEqual(self.logins, [])
        self.assertIn("sampan claude side", self.text())
        self.assertIn("/login", self.text())
        self.assertIn("side", load_config(self.cfg_path, home=self.home).profiles)

    def test_profile_add_rejects_bad_or_known_names_and_known_dirs(self):
        r = self.runner()
        before = self.cfg_path.read_bytes()
        self.assertEqual(self.add(r, "work"), 1)
        self.assertEqual(self.add(r, "a.b"), 1)
        self.assertEqual(self.add(r, "again", dir=self.work), 1)
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertFalse((self.home / ".claude-profiles" / "a.b").exists())


class ProfileAddCodexTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text(EXISTING + '\n[profiles.old]\nconfig_dir = "~/.claude-profiles/old"\ndisabled = true\n')
        self.cfg = load_config(self.cfg_path, home=self.home, state_dir=self.state)
        self.logins = []
        self.rc = 0

    def interactive(self, args, env):
        self.logins.append((list(args), env.get("CODEX_HOME")))
        return self.rc

    def add(self, name, dir=None, which=lambda n: "/usr/bin/codex"):
        return cmd_profile_add(self.cfg, self.runner(), self.lines.append, name, dir=dir, home=self.home, codex=True,
                               env={"PATH": "/usr/bin"}, interactive=self.interactive, which=which)

    def test_add_codex_profile_creates_dir_logs_in_and_writes_section(self):
        self.assertEqual(self.add("cx"), 0)
        target = self.home / ".codex-profiles" / "cx"
        self.assertTrue(target.is_dir())
        self.assertEqual(self.logins, [(["codex", "login"], str(target))])
        text = self.cfg_path.read_text()
        self.assertIn('[profiles.cx]\nagent = "codex"\nconfig_dir = "~/.codex-profiles/cx"\nlabel = ""', text)
        cfg = load_config(self.cfg_path, home=self.home)
        self.assertEqual(cfg.all_profiles["cx"].agent, "codex")
        self.assertEqual(cfg.codex.profile, "cx")
        self.assertIn("# 自己寫的註解", text)
        self.assertIn("只用第一個使用中的 Codex profile（cx）", self.text())
        self.assertNotIn("[codex]", text)  # the stale `enabled = false` does not linger

    def test_add_codex_name_clash_with_disabled_profile(self):
        before = self.cfg_path.read_bytes()
        self.assertEqual(self.add("old"), 1)
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertEqual(self.logins, [])

    def test_add_codex_without_codex_or_failed_login_prints_hint_and_still_writes(self):
        self.assertEqual(self.add("nocli", which=lambda n: None), 0)
        self.assertEqual(self.logins, [])
        self.assertIn("CODEX_HOME=", self.text())
        self.assertIn("codex login", self.text())
        self.rc = 1
        self.cfg = load_config(self.cfg_path, home=self.home, state_dir=self.state)
        self.assertEqual(self.add("bad"), 0)
        self.assertIn("登入沒有完成", self.text())
        self.assertEqual(load_config(self.cfg_path, home=self.home).all_profiles["bad"].agent, "codex")

    def test_add_codex_with_dir_does_not_create_or_login(self):
        given = self.tmp / "cxdir"
        given.mkdir()
        self.assertEqual(self.add("given", dir=given), 0)
        self.assertEqual(self.logins, [])
        self.assertEqual(load_config(self.cfg_path, home=self.home).all_profiles["given"].config_dir, given)
        self.assertEqual(self.add("missing", dir=self.tmp / "nope"), 1)


class AppendPluginsTests(unittest.TestCase):
    def test_append_plugins_keeps_array_style(self):
        from sampan.init import _append_plugins
        cases = [
            'plugins = []',
            'plugins = ["a@m"]',
            'plugins = ["a@m",]  # 註解 ]',
            'plugins = [\n  "a@m"  # x ] y\n]',
            'plugins = [\n  "a@m",\n  # 舊的 "z@m"\n]',
            'label = "x"',
        ]
        for body in cases:
            text = f'[profiles."work"]\nconfig_dir = "/x"\n{body}\n\n[profiles.other]\nplugins = ["o@m"]\n'
            new = _append_plugins(text, "work", ["b@m", "c@m"])
            data = tomllib.loads(new)
            want = (["a@m"] if '"a@m"' in body else []) + ["b@m", "c@m"]
            self.assertEqual(data["profiles"]["work"]["plugins"], want, body)
            self.assertEqual(data["profiles"]["other"]["plugins"], ["o@m"], body)
            for comment in ("# 註解 ]", "# x ] y", '# 舊的 "z@m"'):
                if comment in body:
                    self.assertIn(comment, new)
        self.assertIsNone(_append_plugins("[profiles.alice]\n", "work", ["b@m"]))


if __name__ == "__main__":
    unittest.main()
