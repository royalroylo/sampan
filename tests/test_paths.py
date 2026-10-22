import unittest
from pathlib import Path

from sampan.paths import config_path, state_dir


class PathsTest(unittest.TestCase):
    def test_env_overrides_and_defaults(self):
        home = Path("/Users/u")
        self.assertEqual(config_path({}, home), home / ".config/sampan/config.toml")
        self.assertEqual(state_dir({}, home), home / ".local/state/sampan")
        env = {"SAMPAN_CONFIG": "/tmp/x/c.toml", "SAMPAN_STATE_DIR": "/tmp/x/state"}
        self.assertEqual(config_path(env, home), Path("/tmp/x/c.toml"))
        self.assertEqual(state_dir(env, home), Path("/tmp/x/state"))
