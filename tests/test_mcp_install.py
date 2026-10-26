import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan import cli, mcp_install
from tests.fakes import FakeRunner, cp
from tests.helpers import TOML, make_env

SP = "/home/u/.local/bin/sampan"
CODEX_TOML = TOML + """
[profiles.cx]
config_dir = "~/.codex-alt"
agent = "codex"
"""


class McpInstallTest(unittest.TestCase):
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

    def test_cli_wires_install_and_uninstall(self):
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.mcp_install.install", return_value=0) as i, \
                mock.patch("sampan.doctor.current_sampan", return_value=SP):
            self.assertEqual(cli.main(["mcp", "install", "--dry-run"]), 0)
        self.assertEqual(i.call_args.kwargs, {"sampan_path": SP, "dry_run": True})
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.mcp_install.uninstall", return_value=0) as u:
            self.assertEqual(cli.main(["mcp", "uninstall"]), 0)
        self.assertEqual(u.call_args.kwargs, {"dry_run": False})


if __name__ == "__main__":
    unittest.main()
