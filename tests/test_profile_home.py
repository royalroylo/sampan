"""Whether Claude runs without CLAUDE_CONFIG_DIR follows where a profile lives (~/.claude), not `default = true`."""
import tempfile
import unittest
from pathlib import Path

from sampan import doctor, migrate
from sampan.launch import launch_line
from sampan.observe import profile_for_dir
from sampan.steps import launch_cmd
from tests.helpers import make_env

TEAM_DEFAULT = """
version = 1

[marketplaces]

[profiles.alice]
config_dir = "~/.claude"
label = "alice@example.com"

[profiles.work]
config_dir = "~/.claude-profiles/work"
default = true
label = "work@example.com"
"""

HOME_DEFAULT = TOML = """
version = 1

[marketplaces]

[profiles.alice]
config_dir = "~/.claude"
default = true
label = "alice@example.com"

[profiles.work]
config_dir = "~/.claude-profiles/work"
label = "work@example.com"
"""

NO_HOME = """
version = 1

[marketplaces]

[profiles.a]
config_dir = "~/.claude-profiles/a"
default = true

[profiles.b]
config_dir = "~/.claude-profiles/b"
"""


def env_of(toml):
    tmp = Path(tempfile.mkdtemp())
    cfg = make_env(tmp, toml)
    for d in (".claude-profiles/work", ".claude-profiles/a", ".claude-profiles/b"):
        (cfg.home / d).mkdir(parents=True, exist_ok=True)
    return cfg


class DefaultOutsideHomeTest(unittest.TestCase):
    """The reported failure: `default = true` moved to a profile that is not ~/.claude."""

    def setUp(self):
        self.cfg = env_of(TEAM_DEFAULT)
        self.work, self.alice = self.cfg.profiles["work"], self.cfg.profiles["alice"]

    def test_which_profile_lives_in_home(self):
        self.assertEqual((self.work.default, self.work.uses_home), (True, False))
        self.assertEqual((self.alice.default, self.alice.uses_home), (False, True))
        self.assertIs(self.cfg.default_profile, self.work)
        self.assertIs(self.cfg.home_profile, self.alice)

    def test_the_default_profile_outside_home_gets_its_own_config_dir_and_claude_json(self):
        self.assertEqual(self.work.claude_json, self.work.config_dir / ".claude.json")
        env = self.work.env({"CLAUDE_CONFIG_DIR": "/elsewhere", "X": "1"})
        self.assertEqual((env["CLAUDE_CONFIG_DIR"], env["X"]), (str(self.work.config_dir), "1"))
        line = launch_line(self.cfg, "claude", "work", resume="aaaaaaaa-0000-4000-8000-000000000001", cwd="/c")
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.work.config_dir}", line)
        self.assertNotIn("-u CLAUDE_CONFIG_DIR", line)

    def test_the_profile_in_home_gets_no_variable_whether_or_not_it_is_the_default(self):
        self.assertEqual(self.alice.claude_json, self.cfg.home / ".claude.json")
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.alice.env({"CLAUDE_CONFIG_DIR": "/elsewhere"}))
        line = launch_line(self.cfg, "claude", "alice", resume="aaaaaaaa-0000-4000-8000-000000000001", cwd="/c")
        self.assertIn("-u CLAUDE_CONFIG_DIR", line)
        self.assertNotIn("CLAUDE_CONFIG_DIR=", line)

    def test_every_launch_path_uses_it(self):
        from sampan.registry import Row
        row = Row("x", "claude", "work", "aaaaaaaa-0000-4000-8000-000000000001", "/c")
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.work.config_dir}", launch_cmd(self.cfg, row, cwd="/c"))
        self.assertIn("-u CLAUDE_CONFIG_DIR", launch_cmd(self.cfg, row, profile="alice", cwd="/c"))  # switching to alice

    def test_a_claude_without_the_variable_belongs_to_the_profile_in_home(self):
        self.assertEqual(profile_for_dir(self.cfg, None), "alice")
        self.assertEqual(profile_for_dir(self.cfg, ""), "alice")
        self.assertEqual(profile_for_dir(self.cfg, str(self.work.config_dir)), "work")

    def test_without_any_profile_in_home_such_a_claude_is_unknown_not_the_default(self):
        cfg = env_of(NO_HOME)
        self.assertIsNone(cfg.home_profile)
        self.assertIsNone(profile_for_dir(cfg, None))
        self.assertFalse(any(p.uses_home for p in cfg.profiles.values()))

    def test_the_keychain_service_follows_it_too(self):
        self.assertEqual(doctor._keychain_service(self.alice), doctor.KEYCHAIN_SERVICE)
        self.assertNotEqual(doctor._keychain_service(self.work), doctor.KEYCHAIN_SERVICE)


class OldConfigUnchangedTest(unittest.TestCase):
    def test_default_in_home_behaves_exactly_as_before(self):
        cfg = env_of(HOME_DEFAULT)
        alice, work = cfg.profiles["alice"], cfg.profiles["work"]
        self.assertEqual((alice.default, alice.uses_home, work.uses_home), (True, True, False))
        self.assertEqual(alice.claude_json, cfg.home / ".claude.json")
        self.assertEqual(work.claude_json, work.config_dir / ".claude.json")
        self.assertIs(cfg.default_profile, cfg.home_profile)
        self.assertEqual(profile_for_dir(cfg, None), "alice")
        self.assertIn("-u CLAUDE_CONFIG_DIR", launch_line(cfg, "claude", "alice", cwd="/c"))
        self.assertIn(f"CLAUDE_CONFIG_DIR={work.config_dir}", launch_line(cfg, "claude", "work", cwd="/c"))

    def test_a_config_dir_that_is_a_symlink_to_home_claude_counts_as_home(self):
        tmp = Path(tempfile.mkdtemp())
        home = tmp / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude-profiles").mkdir()
        (home / ".claude-profiles" / "alias").symlink_to(home / ".claude")
        text = HOME_DEFAULT.replace("~/.claude-profiles/work", "~/.claude-profiles/alias")
        (tmp / "config.toml").write_text(text)
        from sampan.config import load_config
        cfg = load_config(tmp / "config.toml", home=home, state_dir=tmp / "state")
        self.assertTrue(cfg.profiles["work"].uses_home)  # the same directory under another name
        self.assertEqual(cfg.profiles["work"].claude_json, home / ".claude.json")


class SmallFixesTest(unittest.TestCase):
    def test_profile_for_dir_compares_real_paths(self):
        tmp = Path(tempfile.mkdtemp())
        home = tmp / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude-profiles").mkdir()
        (home / ".claude-profiles" / "alias").symlink_to(home / ".claude")
        (tmp / "config.toml").write_text(HOME_DEFAULT.replace("~/.claude-profiles/work", "~/.claude-profiles/other"))
        (home / ".claude-profiles" / "other").mkdir()
        from sampan.config import load_config
        cfg = load_config(tmp / "config.toml", home=home, state_dir=tmp / "state")
        self.assertEqual(profile_for_dir(cfg, str(home / ".claude-profiles" / "alias")), "alice")  # the same directory

    def test_forget_resume_line_for_the_profile_in_home_has_no_variable(self):
        from sampan.profile_ops import _resume_line
        from sampan.registry import Row
        cfg = env_of(TEAM_DEFAULT)
        row = Row("x", "claude", "alice", "aaaaaaaa-0000-4000-8000-000000000001", "/c")
        self.assertIn("env -u CLAUDE_CONFIG_DIR", _resume_line(cfg, cfg.profiles["alice"], row))
        self.assertNotIn("CLAUDE_CONFIG_DIR=", _resume_line(cfg, cfg.profiles["alice"], row))
        self.assertIn(f"CLAUDE_CONFIG_DIR={cfg.profiles['work'].config_dir}", _resume_line(cfg, cfg.profiles["work"], row))


class DoctorTest(unittest.TestCase):
    def test_a_default_outside_home_is_explained(self):
        cfg = env_of(TEAM_DEFAULT)
        [c] = [c for c in doctor._profile_dirs(cfg) if "預設 profile work" in c.title]
        self.assertEqual(c.level, "info")
        self.assertIn(f"CLAUDE_CONFIG_DIR={cfg.profiles['work'].config_dir}", c.title)
        self.assertIn("alice", c.detail)
        self.assertIn("sampan restart", c.detail)

    def test_nothing_to_explain_when_the_default_lives_in_home(self):
        self.assertFalse([c for c in doctor._profile_dirs(env_of(HOME_DEFAULT)) if "預設 profile" in c.title])


class ProfileMismatchTest(unittest.TestCase):
    def rows(self, *rows):
        return lambda cfg, run: list(rows)

    def test_a_session_running_in_home_but_registered_on_a_profile_outside_it_is_warned_about(self):
        from sampan.status import State
        from tests.helpers import status_row
        cfg = env_of(TEAM_DEFAULT)
        stale = status_row("flow", State.PROFILE_MISMATCH, profile="alice", detail="（登記 work）")
        fine = status_row("ok", profile="work")
        got = doctor._profile_mismatch(cfg, None, self.rows(stale, fine))
        [c] = got
        self.assertEqual(c.level, "warn")
        self.assertIn("flow：登記在 work，實際跑在 alice", c.title)
        self.assertIn("沒有 CLAUDE_CONFIG_DIR", c.title)
        self.assertIn("sampan restart flow", c.detail)

    def test_other_mismatches_are_warned_about_without_the_home_explanation(self):
        from sampan.status import State
        from tests.helpers import status_row
        cfg = env_of(TEAM_DEFAULT)
        [c] = doctor._profile_mismatch(cfg, None, self.rows(
            status_row("x", State.PROFILE_MISMATCH, profile="work", detail="（登記 alice）")))
        self.assertNotIn("沒有 CLAUDE_CONFIG_DIR", c.title)
        self.assertIn("sampan switch x work", c.detail)

    def test_an_unreadable_status_is_no_check(self):
        def boom(cfg, run):
            raise OSError("no tmux")
        self.assertEqual(doctor._profile_mismatch(env_of(TEAM_DEFAULT), None, boom), [])


class LastBackupTest(unittest.TestCase):
    def test_the_newest_of_any_migration_not_only_0_15(self):
        state = Path(tempfile.mkdtemp())
        self.assertIsNone(migrate.last_backup_any(state))
        for n in ("backup-0.15-20261031-235129", "backup-0.17-20261113-195547", "backup-0.17-20261113-195547-2",
                  "backup-0.15-20261101-000000"):
            (state / n).mkdir()
        (state / "backup-0.18-20271231-000000").write_text("a file, not a backup")
        (state / "backup-oops").mkdir()
        self.assertEqual(migrate.last_backup_any(state).name, "backup-0.17-20261113-195547-2")
        self.assertEqual(migrate.last_backup(state).name, "backup-0.15-20261101-000000")  # the 0.15 one is unchanged

    def test_doctor_shows_it(self):
        cfg = env_of(HOME_DEFAULT)
        (cfg.state_dir).mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "backup-0.17-20261113-195547").mkdir()
        [c] = doctor._data_format(cfg)
        self.assertIn("backup-0.17-20261113-195547", c.title)


if __name__ == "__main__":
    unittest.main()
