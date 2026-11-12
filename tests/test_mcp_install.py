import dataclasses
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sampan import actionlog, cli, codex_approval, mcp_install
from tests.fakes import FakeRunner, cp
from tests.helpers import TOML, make_env

SP = "/home/u/.local/bin/sampan"
CODEX_TOML = TOML + """
[profiles.cx]
config_dir = "~/.codex-alt"
agent = "codex"
"""
INSTALLED = f'[mcp_servers.sampan]\ncommand = "{SP}"\nargs = ["mcp", "serve"]\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp, CODEX_TOML)
        self.out = []

    def o(self, s=""):
        self.out.append(s)

    def runner(self, installed=()):
        """claude/codex get answers rc 0 for the names in installed; add/remove succeed."""
        r = FakeRunner(strict=True)

        def get(args):
            env = r.calls[-1]["env"] or {}
            who = env.get("CLAUDE_CONFIG_DIR", "default") if args[0] == "claude" else "codex"
            return cp(0, "sampan") if who in installed else cp(1, "", "No MCP server found")
        r.on(["claude", "mcp", "get"], get).on(["codex", "mcp", "get"], get)
        r.on(["claude", "mcp", "add"], cp()).on(["codex", "mcp", "add"], cp())
        r.on(["claude", "mcp", "remove"], cp()).on(["codex", "mcp", "remove"], cp())
        return r

    def calls(self, r, verb):
        return [c for c in r.calls if c["args"][1:3] == ["mcp", verb]]


class McpInstallTest(Base):
    def test_install_runs_add_per_profile_with_its_env(self):
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP), 0)
        adds = self.calls(r, "add")
        self.assertEqual([c["args"] for c in adds[:2]],
                         [["claude", "mcp", "add", "--scope", "user", "sampan", "--", SP, "mcp", "serve"]] * 2)
        envs = [c["env"].get("CLAUDE_CONFIG_DIR") for c in adds[:2]]
        self.assertEqual(envs, [None, str(self.cfg.profiles["work"].config_dir)])  # default profile: no variable
        self.assertEqual(adds[2]["args"], ["codex", "mcp", "add", "sampan", "--", SP, "mcp", "serve"])
        self.assertEqual(adds[2]["env"]["CODEX_HOME"], str(self.cfg.codex.home))  # not ~/.codex
        self.assertEqual(len(adds), 3)

    def test_default_codex_home_sets_no_variable(self):
        cfg = make_env(Path(tempfile.mkdtemp()), TOML)  # implicit Codex profile at ~/.codex
        r = self.runner()
        with mock.patch.dict("os.environ"):
            os.environ.pop("CODEX_HOME", None)
            mcp_install.install(cfg, r, self.o, sampan_path=SP)
        add = self.calls(r, "add")[-1]
        self.assertEqual(add["args"][0], "codex")
        self.assertNotIn("CODEX_HOME", add["env"])

    def test_ambient_codex_home_is_removed_when_the_configured_home_is_default(self):
        cfg = make_env(Path(tempfile.mkdtemp()), TOML)
        r = self.runner()
        with mock.patch.dict("os.environ", {"CODEX_HOME": "/elsewhere"}):
            mcp_install.install(cfg, r, self.o, sampan_path=SP)
        codex = [c for c in r.calls if c["args"][0] == "codex"]
        self.assertTrue(codex)
        for c in codex:  # both get and add
            self.assertNotIn("CODEX_HOME", c["env"])

    def test_already_installed_is_skipped_and_said(self):
        r = self.runner(installed={"default"})
        mcp_install.install(self.cfg, r, self.o, sampan_path=SP)
        self.assertEqual(len(self.calls(r, "add")), 2)  # work + codex
        self.assertTrue(any("alice" in s and "已裝" in s for s in self.out), self.out)

    def test_disabled_profile_and_codex_are_not_installed(self):
        cfg = make_env(Path(tempfile.mkdtemp()), CODEX_TOML.replace(
            'label = "work@example.com"', 'label = "w"\ndisabled = true').replace(
            'agent = "codex"', 'agent = "codex"\ndisabled = true'))
        r = self.runner()
        mcp_install.install(cfg, r, self.o, sampan_path=SP)
        self.assertEqual([c["args"][0] for c in self.calls(r, "add")], ["claude"])

    def test_dry_run_changes_nothing(self):
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP, dry_run=True), 0)
        self.assertEqual(self.calls(r, "add"), [])
        self.assertTrue(any("claude mcp add" in s for s in self.out), self.out)
        r2 = self.runner(installed={"default"})
        mcp_install.uninstall(self.cfg, r2, self.o, dry_run=True)
        self.assertEqual(self.calls(r2, "remove"), [])

    def test_failure_is_reported_and_rc_1(self):
        r = self.runner()
        r.on(["claude", "mcp", "add"], cp(1, "", "boom"))
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP), 1)
        self.assertTrue(any("boom" in s for s in self.out), self.out)

    def test_install_refuses_a_sampan_path_that_is_not_sampan(self):
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path="/usr/bin/python3"), 2)
        self.assertEqual(self.calls(r, "add"), [])

    def test_uninstall_removes_only_where_installed(self):
        r = self.runner(installed={"default", "codex"})
        self.assertEqual(mcp_install.uninstall(self.cfg, r, self.o), 0)
        rem = self.calls(r, "remove")
        self.assertEqual([c["args"] for c in rem],
                         [["claude", "mcp", "remove", "--scope", "user", "sampan"], ["codex", "mcp", "remove", "sampan"]])
        self.assertTrue(any("work" in s and "沒裝" in s for s in self.out), self.out)

    def write_claude_json(self, name, text):
        f = self.cfg.profiles[name].claude_json
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def test_installed_in_the_config_file_runs_no_claude(self):
        # 10/30: `claude mcp get` from an SSH shell refreshed each profile's login into .credentials.json
        self.write_claude_json("alice", '{"mcpServers": {"sampan": {"command": "x"}}}')
        self.write_claude_json("work", '{"mcpServers": {"sampan": {"command": "x"}}}')
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP), 0)
        self.assertEqual([c["args"][0] for c in r.calls], ["codex", "codex"])  # Codex as before: get, add
        self.assertEqual(sum("已裝，略過" in s for s in self.out), 2, self.out)

    def test_config_file_without_sampan_adds_without_asking_claude(self):
        self.write_claude_json("alice", '{"mcpServers": {"other": {}}}')
        self.write_claude_json("work", '{"numStartups": 3}')
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP), 0)
        claude = [c["args"][:3] for c in r.calls if c["args"][0] == "claude"]
        self.assertEqual(claude, [["claude", "mcp", "add"]] * 2)

    def test_unreadable_config_file_falls_back_to_claude_mcp_get(self):
        self.write_claude_json("alice", "{not json")
        self.write_claude_json("work", '{"mcpServers": ["sampan"]}')
        r = self.runner(installed={"default"})
        mcp_install.install(self.cfg, r, self.o, sampan_path=SP)
        gets = [c["env"].get("CLAUDE_CONFIG_DIR", "default") for c in self.calls(r, "get") if c["args"][0] == "claude"]
        self.assertEqual(gets, ["default", str(self.cfg.profiles["work"].config_dir)])
        self.assertTrue(any("alice" in s and "已裝" in s for s in self.out), self.out)

    def test_uninstall_reads_the_config_file_too(self):
        self.write_claude_json("alice", '{"mcpServers": {"sampan": {}}}')
        self.write_claude_json("work", '{"mcpServers": {}}')
        r = self.runner()
        mcp_install.uninstall(self.cfg, r, self.o, profiles=("alice", "work"))
        self.assertEqual([c["args"][:3] for c in r.calls], [["claude", "mcp", "remove"]])

    def test_status_reads_the_config_files_without_running_anything(self):
        p = self.cfg.profiles["alice"]
        p.claude_json.parent.mkdir(parents=True, exist_ok=True)
        p.claude_json.write_text('{"mcpServers": {"sampan": {"command": "x"}}}')
        self.cfg.codex.home.mkdir(parents=True, exist_ok=True)
        (self.cfg.codex.home / "config.toml").write_text('[mcp_servers.sampan]\ncommand = "x"\n')
        self.assertEqual(mcp_install.status(self.cfg), {"alice": True, "work": False, self.cfg.codex.profile: True})

    def test_status_survives_unreadable_files(self):
        p = self.cfg.profiles["alice"]
        p.claude_json.parent.mkdir(parents=True, exist_ok=True)
        p.claude_json.write_text("{not json")
        self.assertEqual(mcp_install.status(self.cfg)["alice"], False)

    # --- Codex config.toml: approval_mode = "prompt" for sampan's acting tools ------------------------------------

    def codex_file(self):
        return self.cfg.codex.home / "config.toml"

    def write_codex(self, text):
        self.cfg.codex.home.mkdir(parents=True, exist_ok=True)
        self.codex_file().write_text(text)

    def backups(self):
        return [p.name for p in self.cfg.codex.home.iterdir() if ".bak-sampan-" in p.name]

    def test_install_sets_codex_tools_to_prompt(self):
        r = self.runner()
        r.on(["codex", "mcp", "add"], lambda _a: (self.write_codex(INSTALLED), cp())[1])  # codex writes its entry
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP), 0)
        text = self.codex_file().read_text()
        self.assertTrue(text.startswith(INSTALLED))
        self.assertFalse(codex_approval.needs_fix(text, True))
        [b] = self.backups()
        self.assertTrue(any(s == f"Codex config.toml：已更新（備份：{b}）" for s in self.out), self.out)
        log = actionlog.log_path(self.cfg).read_text()
        self.assertIn("Codex config.toml：已更新", log)

    def test_install_when_already_installed_still_fixes_approve(self):
        self.write_codex(INSTALLED + '\n[mcp_servers.sampan.tools.send]\napproval_mode = "approve"\n')
        r = self.runner(installed={"codex"})
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP), 0)
        self.assertEqual([c["args"][0] for c in self.calls(r, "add")], ["claude", "claude"])
        self.assertFalse(codex_approval.needs_fix(self.codex_file().read_text(), True))
        mcp_install.install(self.cfg, self.runner(installed={"codex"}), self.o, sampan_path=SP)
        self.assertEqual(self.out[-1], "Codex config.toml：不用改")

    def test_install_without_confirm_removes_the_tables(self):
        self.write_codex(codex_approval.with_prompt(INSTALLED))
        cfg = dataclasses.replace(self.cfg, mcp_confirm=False)
        mcp_install.install(cfg, self.runner(installed={"codex"}), self.o, sampan_path=SP)
        approve = "".join(f'\n{codex_approval.section(t)}\napproval_mode = "approve"\n' for t in codex_approval.INBOX_TOOLS)
        self.assertEqual(self.codex_file().read_text(), INSTALLED + approve)  # the mail tools' "approve" stays

    def test_uninstall_strips_the_tables_before_codex_remove(self):
        self.write_codex(codex_approval.with_prompt(INSTALLED))
        seen = []
        r = self.runner(installed={"codex"})
        r.on(["codex", "mcp", "remove"], lambda _a: (seen.append(self.codex_file().read_text()), cp())[1])
        self.assertEqual(mcp_install.uninstall(self.cfg, r, self.o), 0)
        self.assertEqual(seen, [INSTALLED])
        self.assertEqual(len(self.backups()), 1)

    def test_codex_dry_run_writes_nothing(self):
        before = INSTALLED + '\n[mcp_servers.sampan.tools.send]\napproval_mode = "approve"\n'
        self.write_codex(before)
        mcp_install.install(self.cfg, self.runner(installed={"codex"}), self.o, sampan_path=SP, dry_run=True)
        self.assertIn("Codex config.toml：（試跑）會更新", self.out)
        mcp_install.uninstall(self.cfg, self.runner(installed={"codex"}), self.o, dry_run=True)
        self.assertEqual(self.codex_file().read_text(), before)
        self.assertEqual(self.backups(), [])

    def test_codex_dry_run_before_codex_has_sampan(self):
        mcp_install.install(self.cfg, self.runner(), self.o, sampan_path=SP, dry_run=True)
        self.assertIn("Codex config.toml：（試跑）會設成每次詢問", self.out)

    def test_broken_codex_config_is_reported_and_rc_1(self):
        self.write_codex("[x")
        r = self.runner(installed={"codex"})
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP), 1)
        self.assertEqual(self.codex_file().read_text(), "[x")
        self.assertTrue(any(s.startswith("❌ Codex config.toml 不是合法的 TOML") for s in self.out),
                        self.out)
        self.assertEqual(len(self.calls(r, "add")), 2)  # the Claude profiles were still installed
        self.assertEqual(mcp_install.uninstall(self.cfg, self.runner(installed={"codex"}), self.o), 1)

    def work_dir(self):
        return str(self.cfg.profiles["work"].config_dir)

    def test_profile_limits_uninstall_to_that_profile(self):
        self.write_codex(codex_approval.with_prompt(INSTALLED))
        before = self.codex_file().read_text()
        r = self.runner(installed={"default", self.work_dir(), "codex"})
        self.assertEqual(mcp_install.uninstall(self.cfg, r, self.o, profiles=["work"]), 0)
        rem = self.calls(r, "remove")
        self.assertEqual(len(rem), 1)
        self.assertEqual(rem[0]["env"].get("CLAUDE_CONFIG_DIR"), self.work_dir())
        self.assertEqual(self.codex_file().read_text(), before)  # Codex not chosen: its tables stay
        self.assertFalse(any("alice" in s for s in self.out), self.out)

    def test_profile_can_name_codex_and_repeat(self):
        r = self.runner(installed={"default", self.work_dir(), "codex"})
        self.assertEqual(mcp_install.uninstall(self.cfg, r, self.o, profiles=["cx", "alice"]), 0)
        self.assertEqual([c["args"][0] for c in self.calls(r, "remove")], ["claude", "codex"])  # config order
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP, profiles=["work"]), 0)
        self.assertEqual(len(self.calls(r, "add")), 1)

    def test_unknown_profile_refuses_and_runs_nothing(self):
        r = self.runner(installed={"default", self.work_dir(), "codex"})
        self.assertEqual(mcp_install.uninstall(self.cfg, r, self.o, profiles=["work", "nope"]), 2)
        self.assertEqual(r.calls, [])
        self.assertEqual(self.out, ["❌ 沒有這個使用中的 profile：nope（可用：alice, work, cx）"])
        self.assertFalse(actionlog.log_path(self.cfg).exists())

    def test_install_profile_refuses_unknown_and_leaves_codex_alone(self):
        self.write_codex(INSTALLED)
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP, profiles=["nope"]), 2)
        self.assertEqual(r.calls, [])
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP, profiles=["alice"]), 0)
        self.assertEqual(self.codex_file().read_text(), INSTALLED)
        self.assertEqual(self.backups(), [])

    def test_profile_with_dry_run_shows_only_that_profile(self):
        r = self.runner(installed={"default", self.work_dir(), "codex"})
        self.assertEqual(mcp_install.uninstall(self.cfg, r, self.o, profiles=["work"], dry_run=True), 0)
        self.assertEqual(self.calls(r, "remove"), [])
        self.assertEqual([s for s in self.out if "試跑" in s], ["work：（試跑）會執行 claude mcp remove --scope user sampan"])

    def test_cli_wires_install_and_uninstall(self):
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.mcp_install.install", return_value=0) as i, \
                mock.patch("sampan.doctor.current_sampan", return_value=SP):
            self.assertEqual(cli.main(["mcp", "install", "--dry-run"]), 0)
        self.assertEqual(i.call_args.kwargs, {"sampan_path": SP, "dry_run": True, "profiles": [], "allow_inbox": False})
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.mcp_install.uninstall", return_value=0) as u:
            self.assertEqual(cli.main(["mcp", "uninstall"]), 0)
            self.assertEqual(cli.main(["mcp", "uninstall", "--profile", "work", "--profile", "cx"]), 0)
        self.assertEqual(u.call_args_list[0].kwargs, {"dry_run": False, "profiles": []})
        self.assertEqual(u.call_args_list[1].kwargs, {"dry_run": False, "profiles": ["work", "cx"]})


NOW = datetime(2026, 11, 13, 9, 5, 7)
BAK = "settings.json.bak-sampan-20261113-090507"
ALLOW = ["mcp__sampan__inbox_take", "mcp__sampan__reply"]


class AllowInboxTest(unittest.TestCase):
    """settings.json permissions.allow: only sampan's two entries are added or removed, everything else stays."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "settings.json"

    def backups(self):
        return sorted(p.name for p in self.dir.iterdir() if ".bak-sampan-" in p.name)

    def test_allow_inbox_adds_two_and_keeps_everything_else(self):
        self.assertEqual(mcp_install.ALLOW, tuple(ALLOW))
        before = {"model": "m", "permissions": {"deny": ["Bash(rm:*)"], "allow": ["Bash(ls)"], "defaultMode": "auto"},
                  "hooks": {"SessionStart": []}, "statusLine": {"type": "command", "command": "x"}}
        raw = json.dumps(before, indent=4)
        self.path.write_text(raw)
        os.chmod(self.path, 0o640)
        self.assertFalse(mcp_install.allow_status(self.dir))
        msg = mcp_install.allow_inbox(self.dir, now=NOW)
        self.assertEqual(msg, f"已允許 inbox_take、reply（備份：{BAK}）")
        after = json.loads(self.path.read_text())
        self.assertEqual(list(after), list(before))  # key order kept
        self.assertEqual(list(after["permissions"]), ["deny", "allow", "defaultMode"])
        self.assertEqual(after["permissions"]["allow"], ["Bash(ls)", *ALLOW])
        self.assertEqual({k: v for k, v in after.items() if k != "permissions"},
                         {k: v for k, v in before.items() if k != "permissions"})
        self.assertEqual(after["permissions"]["deny"], ["Bash(rm:*)"])
        self.assertEqual(self.backups(), [BAK])
        self.assertEqual((self.dir / BAK).read_text(), raw)  # the original bytes
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), oct(0o640))
        self.assertTrue(mcp_install.allow_status(self.dir))

    def test_allow_inbox_adds_only_the_missing_one(self):
        self.path.write_text(json.dumps({"permissions": {"allow": ["mcp__sampan__reply", "Read"]}}))
        mcp_install.allow_inbox(self.dir, now=NOW)
        self.assertEqual(json.loads(self.path.read_text())["permissions"]["allow"],
                         ["mcp__sampan__reply", "Read", "mcp__sampan__inbox_take"])

    def test_allow_inbox_is_idempotent(self):
        self.path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        mcp_install.allow_inbox(self.dir, now=NOW)
        text = self.path.read_text()
        self.assertEqual(mcp_install.allow_inbox(self.dir, now=datetime(2026, 11, 14)), "已允許，不用改")
        self.assertEqual(self.path.read_text(), text)
        self.assertEqual(self.backups(), [BAK])  # no second backup

    def test_missing_settings_is_created(self):
        self.assertEqual(mcp_install.allow_inbox(self.dir, now=NOW), "已建立，允許 inbox_take、reply")
        self.assertEqual(json.loads(self.path.read_text()), {"permissions": {"allow": ALLOW}})
        self.assertEqual(self.backups(), [])

    def test_broken_settings_json_is_refused(self):
        for text in ("{not json", "[]", '{"permissions": []}', '{"permissions": {"allow": "Read"}}'):
            with self.subTest(text=text):
                self.path.write_text(text)
                for fn in (mcp_install.allow_inbox, mcp_install.remove_inbox_allow):
                    with self.assertRaises(ValueError) as e:
                        fn(self.dir, now=NOW)
                    self.assertIn(str(self.path), str(e.exception))
                self.assertEqual(self.path.read_text(), text)
                self.assertFalse(mcp_install.allow_status(self.dir))
        self.assertEqual(self.backups(), [])

    def test_uninstall_removes_only_ours(self):
        before = {"permissions": {"allow": ["Bash(ls)", "mcp__sampan__inbox_take", "mcp__sampan__send",
                                            "mcp__sampan__reply", "mcp__sampan__inbox_take_x"], "deny": ["X"]},
                  "env": {"A": "1"}}
        self.path.write_text(json.dumps(before))
        self.assertEqual(mcp_install.remove_inbox_allow(self.dir, now=NOW), f"已移除 inbox_take、reply 的允許（備份：{BAK}）")
        after = json.loads(self.path.read_text())
        self.assertEqual(after, {"permissions": {"allow": ["Bash(ls)", "mcp__sampan__send", "mcp__sampan__inbox_take_x"],
                                                 "deny": ["X"]}, "env": {"A": "1"}})
        self.assertEqual(list(after["permissions"]), ["allow", "deny"])
        self.assertEqual(self.backups(), [BAK])
        self.assertEqual(mcp_install.remove_inbox_allow(self.dir, now=datetime(2026, 11, 14)), "沒有允許，不用改")
        self.assertEqual(self.backups(), [BAK])

    def test_remove_with_no_file_writes_nothing(self):
        self.assertEqual(mcp_install.remove_inbox_allow(self.dir, now=NOW), "沒有允許，不用改")
        self.assertFalse(self.path.exists())

    def test_symlinked_settings_is_written_through_the_link(self):
        real = self.dir / "shared" / "settings.json"
        real.parent.mkdir()
        real.write_text("{}")
        self.path.symlink_to(real)
        mcp_install.allow_inbox(self.dir, now=NOW)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"permissions": {"allow": ALLOW}})


class InstallAllowTest(Base):
    """`sampan mcp install --allow-inbox` / the y/N question, and uninstall removing the two entries."""

    def settings(self, name):
        return Path(self.cfg.profiles[name].config_dir) / "settings.json"

    def test_install_with_allow_adds_to_every_claude_profile(self):
        r = self.runner()
        self.assertEqual(mcp_install.install(self.cfg, r, self.o, sampan_path=SP, allow_inbox=True), 0)
        for name in ("alice", "work"):
            self.assertTrue(mcp_install.allow_status(self.cfg.profiles[name].config_dir), name)
        self.assertIn("alice：settings.json 已建立，允許 inbox_take、reply", self.out)
        self.assertFalse(any("cx：settings.json" in s for s in self.out), self.out)  # Codex: its tables, not this

    def test_install_without_allow_leaves_settings_alone(self):
        mcp_install.install(self.cfg, self.runner(), self.o, sampan_path=SP)
        for name in ("alice", "work"):
            self.assertFalse(self.settings(name).exists())

    def test_install_allow_dry_run_writes_nothing(self):
        mcp_install.install(self.cfg, self.runner(), self.o, sampan_path=SP, allow_inbox=True, dry_run=True)
        self.assertIn("alice：settings.json（試跑）會允許 inbox_take、reply", self.out)
        self.assertFalse(self.settings("alice").exists())

    def test_install_allow_only_for_chosen_profiles(self):
        mcp_install.install(self.cfg, self.runner(), self.o, sampan_path=SP, allow_inbox=True, profiles=["work"])
        self.assertTrue(self.settings("work").exists())
        self.assertFalse(self.settings("alice").exists())

    def test_broken_settings_is_reported_and_rc_1(self):
        self.settings("alice").parent.mkdir(parents=True, exist_ok=True)
        self.settings("alice").write_text("{oops")
        rc = mcp_install.install(self.cfg, self.runner(), self.o, sampan_path=SP, allow_inbox=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.settings("alice").read_text(), "{oops")
        self.assertTrue(any(s.startswith("❌ alice：") and "沒改" in s for s in self.out), self.out)
        self.assertTrue(mcp_install.allow_status(self.cfg.profiles["work"].config_dir))

    def test_uninstall_always_removes_the_allow(self):
        for name in ("alice", "work"):
            self.settings(name).parent.mkdir(parents=True, exist_ok=True)
            self.settings(name).write_text(json.dumps({"permissions": {"allow": ["Read", *ALLOW]}}))
        r = self.runner(installed={"default"})  # work's MCP is not installed: its allow goes all the same
        self.assertEqual(mcp_install.uninstall(self.cfg, r, self.o), 0)
        for name in ("alice", "work"):
            self.assertEqual(json.loads(self.settings(name).read_text()), {"permissions": {"allow": ["Read"]}})
        before = list(self.out)
        mcp_install.uninstall(self.cfg, self.runner(), self.o)
        self.assertFalse(any("settings.json" in s for s in self.out[len(before):]))  # nothing to remove: quiet

    def test_uninstall_dry_run_keeps_the_allow(self):
        self.settings("alice").parent.mkdir(parents=True, exist_ok=True)
        self.settings("alice").write_text(json.dumps({"permissions": {"allow": ALLOW}}))
        mcp_install.uninstall(self.cfg, self.runner(installed={"default"}), self.o, dry_run=True)
        self.assertIn("alice：settings.json（試跑）會移除 inbox_take、reply 的允許", self.out)
        self.assertEqual(json.loads(self.settings("alice").read_text()), {"permissions": {"allow": ALLOW}})

    def test_install_asks_on_tty_and_skips_without(self):
        asked = []

        def ask(q):
            asked.append(q)
            return "y"
        self.assertTrue(cli._want_inbox_allow(False, dry_run=False, isatty=lambda: True, ask=ask))
        self.assertEqual(asked, ["讓 agent 取信和回覆不用每次按允許？(y/N): "])
        for answer in ("", "n", "N", "no"):
            self.assertFalse(cli._want_inbox_allow(False, dry_run=False, isatty=lambda: True, ask=lambda q: answer))

        def eof(q):
            raise EOFError
        self.assertFalse(cli._want_inbox_allow(False, dry_run=False, isatty=lambda: True, ask=eof))
        no_ask = lambda q: 1 / 0  # noqa: E731
        self.assertFalse(cli._want_inbox_allow(False, dry_run=False, isatty=lambda: False, ask=no_ask))  # no tty
        self.assertTrue(cli._want_inbox_allow(True, dry_run=False, isatty=lambda: True, ask=no_ask))  # the flag: no question
        self.assertTrue(cli._want_inbox_allow(True, dry_run=False, isatty=lambda: False, ask=no_ask))
        self.assertFalse(cli._want_inbox_allow(False, dry_run=True, isatty=lambda: True, ask=no_ask))  # a dry run asks nothing

    def test_cli_passes_the_flag(self):
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.mcp_install.install", return_value=0) as i, \
                mock.patch("sampan.doctor.current_sampan", return_value=SP), \
                mock.patch("sampan.cli._want_inbox_allow", return_value=True) as want:
            self.assertEqual(cli.main(["mcp", "install", "--allow-inbox"]), 0)
        self.assertEqual(want.call_args.args, (True,))
        self.assertEqual(i.call_args.kwargs["allow_inbox"], True)


if __name__ == "__main__":
    unittest.main()
