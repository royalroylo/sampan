import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan.config import ConfigError, load_config, same_source
from tests.helpers import TOML, make_env


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_load_profiles_and_paths(self):
        cfg = make_env(self.tmp)
        home = self.tmp / "home"
        self.assertEqual(cfg.default_profile.name, "alice")
        self.assertEqual(cfg.profiles["alice"].config_dir, home / ".claude")
        self.assertEqual(cfg.profiles["alice"].claude_json, home / ".claude.json")
        self.assertEqual(cfg.profiles["work"].claude_json, home / ".claude-profiles/work/.claude.json")
        self.assertEqual(cfg.profiles["alice"].projects_dir, home / ".claude/projects")
        self.assertEqual(cfg.hooks_dir, self.tmp / "state/hooks")
        self.assertEqual(cfg.registry_path, self.tmp / "state/registry.json")

    def test_env_default_unsets_and_other_sets(self):
        cfg = make_env(self.tmp)
        base = {"CLAUDE_CONFIG_DIR": "/somewhere", "PATH": "/bin"}
        self.assertNotIn("CLAUDE_CONFIG_DIR", cfg.profiles["alice"].env(base))
        self.assertEqual(cfg.profiles["work"].env(base)["CLAUDE_CONFIG_DIR"],
                         str(self.tmp / "home/.claude-profiles/work"))
        self.assertEqual(base["CLAUDE_CONFIG_DIR"], "/somewhere")  # does not modify the original dict

    def test_plugin_with_unknown_marketplace_rejected(self):
        bad = TOML.replace('plugins = ["tools@my-plugins"]', 'plugins = ["x@nowhere"]')
        with self.assertRaises(ConfigError):
            make_env(self.tmp, bad)

    def test_non_github_marketplace_rejected(self):
        bad = TOML.replace('"anthropics/claude-plugins-official"', '"/Users/u/hooks"')
        with self.assertRaises(ConfigError):
            make_env(self.tmp, bad)

    def test_git_url_marketplace_accepted(self):
        for src in ("https://git.example.com/owner/my-plugins.git", "git@git.example.com:owner/my-plugins.git", "ssh://git@git.example.com/owner/x"):
            cfg = make_env(self.tmp, TOML.replace('"owner/my-plugins"', f'"{src}"'))
            self.assertEqual(cfg.marketplaces["my-plugins"], src)

    def test_unsupported_marketplace_urls_rejected(self):
        for src in ("file:///Users/u/m", "https://", "git@host", "./hooks", "../x", "~/m", "a/b/c"):
            with self.assertRaises(ConfigError, msg=src):
                make_env(self.tmp, TOML.replace('"owner/my-plugins"', f'"{src}"'))

    def test_url_with_credentials_rejected_without_echoing_secret(self):
        with self.assertRaises(ConfigError) as e:
            make_env(self.tmp, TOML.replace('"owner/my-plugins"', '"https://bot:secret123@git.example.com/owner/my-plugins.git"'))
        self.assertIn("https://git.example.com/owner/my-plugins.git", str(e.exception))

    def test_same_source(self):
        self.assertTrue(same_source("owner/r", "https://github.com/owner/r.git"))
        self.assertTrue(same_source("https://git.x/a/b.git", "https://git.x/a/b/"))
        self.assertTrue(same_source("Owner/R", "owner/r"))
        self.assertTrue(same_source("https://bot:secret123@git.example.com/owner/my-plugins.git", "https://git.example.com/owner/my-plugins.git"))
        self.assertFalse(same_source("a/b", "a/c"))
        self.assertFalse(same_source("https://git.x/a/b", "https://git.y/a/b"))
        self.assertFalse(same_source(None, "a/b"))
        self.assertFalse(same_source(None, None))

    def test_exactly_one_default(self):
        bad = TOML.replace("default = true\n", "")
        with self.assertRaises(ConfigError):
            make_env(self.tmp, bad)

    def test_cli_bad_config_exits_2_with_message(self):
        import io
        from contextlib import redirect_stderr

        from sampan.cli import main
        err = io.StringIO()
        with mock.patch.dict("os.environ", {"SAMPAN_CONFIG": str(self.tmp / "none.toml"),
                                            "SAMPAN_STATE_DIR": str(self.tmp / "st")}), redirect_stderr(err):
            rc = main(["doctor"])
        self.assertEqual(rc, 2)
        self.assertTrue(err.getvalue().startswith("❌"))
        self.assertIn("sampan init", err.getvalue())

    def test_v1_schema_defaults(self):
        home = self.tmp / "home"
        cfg = make_env(self.tmp, 'version = 1\n[profiles.alice]\nconfig_dir = "~/.claude"\ndefault = true\n')
        self.assertEqual(cfg.version, 1)
        self.assertEqual(cfg.recent_days, 7)
        self.assertTrue(cfg.codex.enabled)
        self.assertEqual(cfg.codex.home, home / ".codex")

    def test_ui_start_dir_default_is_projects_if_it_exists_else_home(self):
        home = self.tmp / "home"
        cfg = make_env(self.tmp, 'version = 1\n[profiles.alice]\nconfig_dir = "~/.claude"\ndefault = true\n')
        self.assertEqual(cfg.start_dir, home)
        (home / "Projects").mkdir()
        self.assertEqual(cfg.start_dir, home / "Projects")

    def test_ui_start_dir_from_config(self):
        home = self.tmp / "home"
        base = 'version = 1\n%s[profiles.alice]\nconfig_dir = "~/.claude"\ndefault = true\n'
        self.assertEqual(make_env(self.tmp, base % '[ui]\nstart_dir = "~/work"\n').start_dir, home / "work")
        self.assertEqual(make_env(self.tmp, base % '[ui]\nstart_dir = "/srv/code"\n').start_dir, Path("/srv/code"))
        with self.assertRaises(ConfigError) as cm:
            make_env(self.tmp, base % '[ui]\nstart_dir = 3\n')
        self.assertIn("start_dir", str(cm.exception))

    def test_codex_home_from_env_and_config(self):
        home = self.tmp / "home"
        base = 'version = 1\n%s[profiles.alice]\nconfig_dir = "~/.claude"\ndefault = true\n'
        path = self.tmp / "c.toml"
        path.write_text(base % "")
        with mock.patch.dict("os.environ", {"CODEX_HOME": "/Users/u/cx"}):
            self.assertEqual(load_config(path, home=home).codex.home, Path("/Users/u/cx"))
            path.write_text(base % '[codex]\nenabled = false\nhome = "~/mine"\n')
            cfg = load_config(path, home=home)
        self.assertEqual(cfg.codex.home, home / "mine")
        self.assertFalse(cfg.codex.enabled)

    def test_bad_config_messages(self):
        home = self.tmp / "home"
        with self.assertRaises(ConfigError) as cm:
            load_config(self.tmp / "none.toml", home=home)
        self.assertIn("sampan init", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            make_env(self.tmp, TOML.replace("version = 1", "version = 2"))
        self.assertIn("version", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            make_env(self.tmp, TOML.replace("version = 1\n", ""))
        self.assertIn("version", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            make_env(self.tmp, TOML.replace('label = "work@example.com"', 'default = true'))
        self.assertIn("default", str(cm.exception))
        def bad(old, new, needle):
            with self.assertRaises(ConfigError) as cm:
                make_env(self.tmp, TOML.replace(old, new))
            self.assertIn(needle, str(cm.exception))

        bad('config_dir = "~/.claude-profiles/work"\n', "", "config_dir")
        bad("version = 1\n", 'version = 1\n[restore]\nrecent_days = "x"\n', "recent_days")
        bad("version = 1\n", "version = 1\ncodex = 1\n", "[codex]")
        bad("[profiles.work]", "[profiles]\nzed = 1\n[profiles.work]", "zed")
        gone = TOML.replace("~/.claude-profiles/work", "~/gone")
        cfg = make_env(self.tmp, gone)
        self.assertFalse(cfg.profiles["work"].config_dir.exists())
