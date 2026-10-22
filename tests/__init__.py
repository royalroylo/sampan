"""Importing the test package installs the real-command guard (see tests/guard.py) and points HOME at a throwaway
directory, so no test can read or write the real ~/.config, ~/.local/state or ~/Library (a stray cli.main() call must
never touch a real home)."""
import atexit
import os
import shutil
import tempfile

from tests import guard

guard.install()

_HOME = tempfile.mkdtemp(prefix="sampan-test-home-")
os.environ["HOME"] = _HOME
for _k in ("SAMPAN_CONFIG", "SAMPAN_STATE_DIR", "XDG_CONFIG_HOME", "CODEX_HOME"):
    os.environ.pop(_k, None)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
