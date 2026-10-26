import json
import os
import plistlib
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sampan import codex_approval, launchd
from sampan.doctor import current_sampan, run_checks
from sampan.hook import install_hook
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import TOML, local_tz, make_env
from tests.test_claude_state import write_session
from tests.test_codex_state import make_db
from tests.test_plugins import GOOD_KNOWN, write_plugins

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone(timedelta(hours=8)))
SAMPAN = "/Users/u/.local/bin/sampan"


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        for p in ("alice", "work"):
            write_plugins(self.cfg, p, known=GOOD_KNOWN)
        self.cfg.save_log.parent.mkdir(parents=True)
        self.cfg.save_log.write_text(f"{(NOW - timedelta(minutes=30)).isoformat()} ok 新增 0\n")
        self.r = FakeRunner().on(["claude", "auth", "status"],
                                 cp(out=json.dumps({"loggedIn": True, "email": "alice@example.com"})))

    def checks(self, uid=None, sampan_path=SAMPAN, which=lambda name: f"/opt/homebrew/bin/{name}", env=None, **kw):
        env = {"PATH": f"{self.cfg.home}/.local/bin:/usr/bin"} if env is None else env
        kw.setdefault("platform", "darwin")  # the launchd / Homebrew checks only exist on macOS
        return run_checks(self.cfg, self.r, now=NOW, uid=os.getuid() if uid is None else uid, sampan_path=sampan_path,
                          which=which, env=env, **kw)

    def test_non_macos_has_no_homebrew_or_launchd_checks(self):
        self.cfg.save_log.unlink()  # no save log either: must not warn on Linux
        checks = self.checks(platform="linux", which=lambda name: f"/usr/bin/{name}")
        bad = [c.title for c in checks if c.level in ("warn", "fail")]
        self.assertFalse([t for t in bad if "Homebrew" in t or "排程" in t or "自動存檔" in t], bad)
        infos = [c.title for c in checks if c.level == "info"]
        self.assertTrue(any("不是 macOS" in t for t in infos), infos)
        self.assertEqual(self.r.called(["launchctl"]), [])

    def test_deps_reported(self):
        titles = [c.title for c in self.checks()]
        self.assertIn("tmux：/opt/homebrew/bin/tmux", titles)
        missing = self.checks(which=lambda name: None, env={"PATH": "/usr/bin"})
        levels = {c.title.split("（")[0]: c.level for c in missing}
        self.assertEqual(levels["找不到 tmux"], "fail")
        self.assertEqual(levels["找不到 claude"], "fail")
        self.assertEqual(levels["~/.local/bin 不在 PATH"], "warn")

    def titles(self, level):
        return [c.title for c in self.checks() if c.level == level]

    def write_plist(self, program):
        path = launchd.plist_path(self.cfg.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({"Label": launchd.LABEL, "ProgramArguments": program,
                                         "StartCalendarInterval": launchd.SCHEDULE}))

    def failing(self):
        return [c.title for c in self.checks() if c.level == "fail"]

    def warning(self):
        return [c.title for c in self.checks() if c.level == "warn"]

    def test_clean_setup_has_no_failures(self):
        self.assertEqual(self.failing(), [])

    def test_foreign_owner_fails(self):
        self.cfg.profiles["alice"].claude_json.write_text("{}")
        checks = self.checks(uid=os.getuid() + 1)
        self.assertTrue(any(c.level == "fail" and ".claude.json" in c.title for c in checks))

    def test_duplicate_repo_and_unlisted_marketplace_fail(self):
        # Review R11:my-plugins is correct, another name points at the same repo
        write_plugins(self.cfg, "work", known={**GOOD_KNOWN, "extra-hooks": "owner/my-plugins"})
        titles = self.failing()
        self.assertTrue(any("同一個 repo" in t for t in titles))
        self.assertTrue(any("清單外" in t and "extra-hooks" in t for t in titles))

    def test_wrong_source_fails(self):
        write_plugins(self.cfg, "work", known={**GOOD_KNOWN, "claude-plugins-official": "someone/else"})
        self.assertTrue(any("來源" in t and "claude-plugins-official" in t for t in self.failing()))

    def test_git_url_marketplace_is_fine(self):
        self.cfg = make_env(self.cfg.path.parent, TOML.replace('"owner/my-plugins"', '"https://git.example.com/owner/my-plugins.git"'))
        for p in ("alice", "work"):
            write_plugins(self.cfg, p, known={**GOOD_KNOWN, "my-plugins": "https://git.example.com/owner/my-plugins.git"})
        self.assertEqual(self.failing(), [])

    def test_unlisted_marketplace_with_installed_plugin_is_not_removed(self):
        # Another machine: config lacks this marketplace, but tools are loading from it; removing it would break the plugin
        write_plugins(self.cfg, "work", installed={"tools@gitea-plugins": [("user", "1", None)]},
                      known={**GOOD_KNOWN, "gitea-plugins": "https://git.example.com/owner/my-plugins.git"},
                      settings={"extraKnownMarketplaces": {"gitea-plugins": {}}})
        checks = [c for c in self.checks() if "gitea-plugins" in c.title]
        self.assertTrue(checks)
        self.assertEqual({c.level for c in checks}, {"warn"})
        self.assertFalse(any("marketplace remove" in c.detail for c in checks), [c.detail for c in checks])
        self.assertTrue(all("sampan init" in c.detail for c in checks))

    def test_extra_marketplace_in_settings_fails(self):
        (self.cfg.profiles["work"].config_dir / "settings.json").write_text(
            json.dumps({"extraKnownMarketplaces": {"extra-hooks": {}}}))
        self.assertTrue(any("extraKnownMarketplaces" in t for t in self.failing()))

    def test_logged_out_fails(self):
        self.r.on(["claude", "auth", "status"], cp(out=json.dumps({"loggedIn": False})))
        self.assertTrue(any("未登入" in t for t in self.failing()))

    def test_stale_session_files_and_old_save_log_warn(self):
        write_session(self.cfg, "alice", 99999)
        self.cfg.save_log.write_text(f"{(NOW - timedelta(hours=5)).isoformat()} ok\n")
        warns = self.warning()
        self.assertTrue(any("殘留" in t for t in warns))
        self.assertTrue(any("自動存檔" in t for t in warns))


    def test_missing_profile_dir_warns_and_never_runs_claude_for_it(self):
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        work_dir = str(self.cfg.profiles["work"].config_dir)
        self.assertTrue(any("work" in t and "不存在" in t for t in self.titles("warn")))
        self.assertFalse(self.cfg.profiles["work"].config_dir.exists())
        self.assertFalse([c for c in self.r.calls if (c["env"] or {}).get("CLAUDE_CONFIG_DIR") == work_dir])
        self.assertEqual(len(self.r.called(["claude", "auth", "status"])), 1)

    def test_doctor_reports_unlisted_profile_dir(self):
        (self.cfg.home / ".claude-profiles" / "extra").mkdir()
        infos = [c for c in self.checks() if c.level == "info"]
        hit = [c for c in infos if "extra" in c.title]
        self.assertEqual(len(hit), 1)
        self.assertIn("sampan profile add extra --dir", hit[0].title + hit[0].detail)
        self.assertFalse(any("work" in c.title and "profile add" in c.detail for c in infos))

    def test_missing_launchd_plist_warns(self):
        self.assertTrue(any("排程" in t for t in self.titles("warn")))

    def test_save_log_time_is_shown_in_local_time(self):
        with local_tz("Asia/Tokyo"):
            self.cfg.save_log.write_text("2026-10-20T00:00:00+00:00 ok\n")  # 09:00 in Tokyo; NOW (04:00 UTC) is 4 h later
            hit = [c for c in self.checks() if "自動存檔" in c.title and c.level == "warn"]
        self.assertTrue(any("10/20 09:00" in c.title for c in hit), [c.title for c in hit])

    def test_correct_launchd_plist_is_ok(self):
        self.write_plist(launchd.expected_program(SAMPAN))
        self.assertFalse(any("排程" in t for t in self.titles("warn")))
        self.assertTrue(any("排程" in t for t in self.titles("ok")))

    def test_doctor_reports_wrong_launchd_program(self):
        self.write_plist(["/old/place/sampan", "save", "--quiet"])
        warns = [c for c in self.checks() if c.level == "warn" and "排程" in c.title]
        self.assertEqual(len(warns), 1)
        self.assertIn("/old/place/sampan", warns[0].title + warns[0].detail)
        self.assertIn(SAMPAN, warns[0].title + warns[0].detail)

    def test_doctor_warns_on_old_interval_schedule(self):
        path = launchd.plist_path(self.cfg.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({"Label": launchd.LABEL, "ProgramArguments": launchd.expected_program(SAMPAN),
                                         "StartInterval": 3600}))
        warns = [c for c in self.checks() if c.level == "warn" and "排程" in c.title]
        self.assertEqual(len(warns), 1)
        self.assertIn("sampan init", warns[0].detail)

    def test_doctor_warns_when_plist_ok_but_not_loaded(self):
        self.write_plist(launchd.expected_program(SAMPAN))
        self.r.on(["launchctl", "print"], cp(rc=113, err="Could not find service"))
        warns = [c for c in self.checks() if c.level == "warn" and "排程" in c.title]
        self.assertEqual(len(warns), 1)
        self.assertIn("沒有載入", warns[0].title)
        self.assertFalse(any("排程" in t for t in self.titles("ok")))

    def test_unreadable_launchd_plist_warns(self):
        path = launchd.plist_path(self.cfg.home)
        path.parent.mkdir(parents=True)
        path.write_text("not a plist")
        self.assertTrue(any("排程" in t and "讀不到" in t for t in self.titles("warn")))

    def test_current_sampan_keeps_a_symlinked_argv0_unresolved(self):
        """uv tool installs ~/.local/bin/sampan as a symlink into the tool's venv; the stable symlink path is the one to record."""
        tool = self.cfg.home / "tool" / "bin"
        tool.mkdir(parents=True)
        (tool / "sampan").write_text("#!/bin/sh\n")
        link = self.cfg.home / ".local" / "bin" / "sampan"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(tool / "sampan")
        with mock.patch("shutil.which", return_value=None), mock.patch("sys.argv", [str(link), "init"]):
            self.assertEqual(current_sampan(), str(link))

    def test_doctor_ignores_retired_profile_dir(self):
        (self.cfg.home / ".claude-profiles" / "old.retired.20261018").mkdir()
        self.assertFalse(any("retired" in c.title for c in self.checks()))

    def test_doctor_silent_about_absent_hook(self):
        self.assertFalse(any("hook" in c.title for c in self.checks()))

    def test_doctor_hook_states(self):
        real = self.cfg.home / "bin" / "sampan"
        real.parent.mkdir()
        real.write_text("#!/bin/sh\n")
        install_hook(self.cfg.profiles["alice"], real)          # installed
        install_hook(self.cfg.profiles["work"], "/gone/sampan")  # stale
        checks = self.checks()
        by = {c.title: c.level for c in checks if "hook" in c.title}
        self.assertEqual(sorted(by.values()), ["ok", "warn"])
        self.assertEqual(by[next(t for t in by if t.startswith("alice"))], "ok")
        self.assertEqual(by[next(t for t in by if t.startswith("work"))], "warn")

    def test_doctor_codex_state_unreadable(self):
        self.assertTrue(any("Codex" in t for t in self.titles("warn")))  # no state_*.sqlite
        home = self.cfg.codex.home
        home.mkdir(parents=True)
        (home / "state_5.sqlite").write_text("not a database")
        self.assertTrue(any("Codex" in t for t in self.titles("warn")))
        (home / "state_5.sqlite").unlink()
        make_db(home, [])
        self.assertFalse(any("Codex" in t for t in self.titles("warn")))

    def test_codex_state_advice_names_profile_disable(self):
        c = next(c for c in self.checks() if c.title.startswith("讀不到 Codex"))
        self.assertEqual(c.detail, "沒有用 Codex 的話：sampan profile disable codex")

    def test_disabled_profile_dir_is_known_and_reported(self):
        (self.cfg.home / ".claude-profiles" / "old").mkdir(parents=True)
        self.cfg = make_env(self.cfg.path.parent, TOML + '\n[profiles.old]\nconfig_dir = "~/.claude-profiles/old"\n'
                            'disabled = true\n')
        checks = self.checks()
        self.assertFalse([c.title for c in checks if "不在設定裡" in c.title])
        self.assertIn(("info", "old 已停用"), [(c.level, c.title) for c in checks])

    def test_codex_disabled_skips_codex_check(self):
        self.cfg = replace(self.cfg, codex=replace(self.cfg.codex, enabled=False))
        self.assertFalse(any("Codex" in c.title for c in self.checks()))

    def test_doctor_reports_daemon(self):
        from sampan.codex_daemon import DaemonSnapshot
        seen = []

        def up(home, run, *, now, **_kw):
            seen.append(home)
            return DaemonSnapshot(home, None, (), frozenset(), now)

        home = self.cfg.codex.home
        checks = self.checks(daemon_fn=up)
        self.assertIn(("info", f"Codex 常駐服務：執行中（{home}）"), [(c.level, c.title) for c in checks])
        self.assertEqual(seen, [home])
        checks = self.checks(daemon_fn=lambda home, run, *, now, **_kw: None)
        self.assertIn(("info", "Codex 常駐服務沒在執行（codex remote-control start）"),
                      [(c.level, c.title) for c in checks])

    def test_doctor_daemon_default_never_reaches_a_real_daemon(self):
        # the temp CODEX_HOME has no socket: the default snapshot asks the (fake) codex binary and gets nothing
        self.r.on(["codex", "app-server", "daemon", "version"], cp(rc=1))
        titles = [c.title for c in self.checks()]
        self.assertIn("Codex 常駐服務沒在執行（codex remote-control start）", titles)
        self.assertEqual(len(self.r.called(["codex", "app-server", "daemon", "version"])), 1)

    def test_doctor_daemon_skipped_without_codex_profile(self):
        self.cfg = replace(self.cfg, codex=replace(self.cfg.codex, enabled=False))
        called = []
        checks = self.checks(daemon_fn=lambda *a, **k: called.append(a))
        self.assertFalse(any("常駐服務" in c.title for c in checks))
        self.assertEqual(called, [])

    def test_doctor_multi_agent_session(self):
        self.r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("x", "%1", 100, "claude", "/c") + "\n"
                                                  + pane_line("x", "%2", 101, "codex", "/c") + "\n"))
        self.r.on(["ps", "-A"], cp(out="\n".join([
            ps_line(1, 0, "launchd", "launchd"), ps_line(100, 1, "-zsh", "-zsh"), ps_line(101, 1, "-zsh", "-zsh"),
            ps_line(200, 100, "claude", "claude"), ps_line(201, 101, "codex", "codex")]) + "\n"))
        hits = [c for c in self.checks() if c.level == "warn" and "x" in c.title and "agent" in c.title]
        self.assertEqual(len(hits), 1)
        self.assertIn("拆成", hits[0].detail)

    def test_single_agent_session_is_not_flagged(self):
        self.r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("x", "%1", 100, "claude", "/c") + "\n"))
        self.r.on(["ps", "-A"], cp(out="\n".join([
            ps_line(1, 0, "launchd", "launchd"), ps_line(100, 1, "-zsh", "-zsh"),
            ps_line(200, 100, "claude", "claude")]) + "\n"))
        self.assertFalse(any("同一個 tmux session" in c.title for c in self.checks()))


class ScopedInstallDoctorTest(DoctorTest):
    """Scoped installs explain where they come from instead of a generic warning."""

    CT = "tools@my-plugins"

    def _checks_for(self, profile, entries):
        write_plugins(self.cfg, profile, installed={self.CT: [("user", "1", None), *entries]}, known=GOOD_KNOWN)
        return [c for c in self.checks() if c.level == "warn"]

    def test_home_project_scope_on_another_profile_names_the_home_sessions(self):
        from sampan.registry import Row, write_rows
        write_rows(self.cfg.registry_path, [Row("004-design", "claude", "work", "a" * 36, str(self.cfg.home)),
                                            Row("001-proj", "claude", "work", "b" * 36, "/elsewhere"),
                                            Row("999-todos", "claude", "alice", "c" * 36, str(self.cfg.home))],
                   "t", self.cfg.history_dir)
        warns = self._checks_for("work", [("project", "1", str(self.cfg.home))])
        hits = [c for c in warns if "~/.claude/settings.json" in c.title]
        self.assertEqual(len(hits), 1)
        self.assertIn("004-design", hits[0].detail)
        self.assertNotIn("001-proj", hits[0].detail)
        self.assertNotIn("999-todos", hits[0].detail)

    def test_home_project_scope_on_the_default_profile_is_not_a_warning(self):
        warns = self._checks_for("alice", [("project", "1", str(self.cfg.home))])
        self.assertEqual([c.title for c in warns if "scope" in c.title or "settings.json" in c.title], [])

    def test_local_install_points_to_prune(self):
        warns = self._checks_for("work", [("local", "1", "/repo/x")])
        self.assertTrue(any("/repo/x" in c.title and "--prune" in c.detail for c in warns))

    def test_repo_project_scope_points_to_the_repo_settings(self):
        warns = self._checks_for("work", [("project", "1", "/repo/y")])
        self.assertTrue(any("/repo/y" in c.title and ".claude/settings.json" in c.detail for c in warns))


class McpDoctorTest(DoctorTest):
    def mcp_titles(self):
        return [c.title for c in self.checks() if "sampan MCP" in c.title]

    def test_nothing_installed_is_quiet(self):
        self.assertEqual(self.mcp_titles(), [])

    def test_shows_where_it_is_installed_and_where_not(self):
        for name in ("alice", "work"):
            p = self.cfg.profiles[name]
            p.claude_json.parent.mkdir(parents=True, exist_ok=True)
            p.claude_json.write_text('{"mcpServers": {"sampan": {}}}' if name == "alice" else "{}")
        titles = self.mcp_titles()
        self.assertEqual(titles, ["sampan MCP：裝在 alice；未裝：work、codex"])
        self.assertEqual([c.level for c in self.checks() if "sampan MCP" in c.title], ["info"])

    def test_everything_installed_has_no_install_hint(self):
        for p in self.cfg.profiles.values():
            p.claude_json.parent.mkdir(parents=True, exist_ok=True)
            p.claude_json.write_text('{"mcpServers": {"sampan": {}}}')
        self.cfg.codex.home.mkdir(parents=True, exist_ok=True)
        (self.cfg.codex.home / "config.toml").write_text('[mcp_servers.sampan]\ncommand = "sampan"\n')
        hits = [c for c in self.checks() if "sampan MCP" in c.title]
        self.assertEqual([c.title for c in hits], ["sampan MCP：裝在 alice、work、codex"])
        self.assertEqual(hits[0].detail, "")

    def codex_toml(self, text):
        self.cfg.codex.home.mkdir(parents=True, exist_ok=True)
        (self.cfg.codex.home / "config.toml").write_text(text)

    def approval_titles(self):
        return [c.title for c in self.checks() if "Codex 的 sampan 工具" in c.title]

    def test_doctor_flags_codex_tool_not_prompt(self):
        installed = '[mcp_servers.sampan]\ncommand = "sampan"\n'
        self.codex_toml(installed + '\n[mcp_servers.sampan.tools.send]\napproval_mode = "approve"\n')  # "always allow"
        self.assertEqual(self.approval_titles(),
                         ["Codex 的 sampan 工具沒設成每次詢問（[mcp] confirm = true）：跑 sampan mcp install 補上"])
        self.codex_toml(codex_approval.with_prompt(installed))
        self.cfg = replace(self.cfg, mcp_confirm=False)
        self.assertEqual(self.approval_titles(),
                         ["Codex 的 sampan 工具還設著每次詢問（[mcp] confirm = false）：跑 sampan mcp install 拿掉"])

    def test_doctor_quiet_when_codex_prompt_set(self):
        installed = '[mcp_servers.sampan]\ncommand = "sampan"\n'
        self.codex_toml(codex_approval.with_prompt(installed))
        self.assertEqual(self.approval_titles(), [])
        self.codex_toml(installed)
        self.cfg = replace(self.cfg, mcp_confirm=False)
        self.assertEqual(self.approval_titles(), [])
        self.codex_toml("a = 1\n")  # sampan MCP not in Codex: nothing to say
        self.cfg = replace(self.cfg, mcp_confirm=True)
        self.assertEqual(self.approval_titles(), [])
