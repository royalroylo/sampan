"""Importing the test package installs the real-command guard (see tests/guard.py) and points HOME at a throwaway
directory, so no test can read or write the real ~/.config, ~/.local/state or ~/Library (a stray cli.main() call must
never touch a real home)."""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

from tests import guard

guard.install()

# Every temp directory a test makes (mkdtemp, TemporaryDirectory, also in child processes through TMPDIR) goes under
# one root for this run, removed at exit: tests that never clean up can no longer pile up in the system temp dir
# (tens of thousands of tmp* folders did). SHORT_TMP is the same under /tmp, for unix socket paths that must stay short.
_SYSTEM_TMP = Path(tempfile.gettempdir())
_WATCHED = (_SYSTEM_TMP, Path("/tmp"))


def _tmp_entries():
    return {d: {p.name for p in d.glob("tmp*")} for d in _WATCHED if d.is_dir()}


_BEFORE = _tmp_entries()
_ROOT = tempfile.mkdtemp(prefix="sampan-tests-")
SHORT_TMP = tempfile.mkdtemp(prefix="sampan-t-", dir="/tmp")
tempfile.tempdir = _ROOT
os.environ["TMPDIR"] = _ROOT

_HOME = tempfile.mkdtemp(prefix="sampan-test-home-")
os.environ["HOME"] = _HOME
for _k in ("SAMPAN_CONFIG", "SAMPAN_STATE_DIR", "XDG_CONFIG_HOME", "CODEX_HOME"):
    os.environ.pop(_k, None)


def _leaked():
    """tmp* entries that appeared in the watched temp dirs during the run and look like a test's (another process on
    the machine may make its own meanwhile: those are only mentioned)."""
    after = _tmp_entries()
    ours, other = [], []
    for d, names in after.items():
        for name in sorted(names - _BEFORE.get(d, set())):
            p = d / name
            looks = not p.is_dir() or not any(p.iterdir()) or any((p / x).exists() for x in ("config.toml", "home", "state"))
            (ours if looks else other).append(str(p))
    return ours, other


def _finish():
    shutil.rmtree(_ROOT, ignore_errors=True)
    shutil.rmtree(SHORT_TMP, ignore_errors=True)
    ours, other = _leaked()
    if other:
        print(f"tests: {len(other)} new tmp* entries in the temp dir that do not look like a test's: {other[:3]}",
              file=sys.stderr)
    if ours:
        print(f"tests: FAILED, {len(ours)} temp folders left outside the tests' temp root: {ours[:5]}", file=sys.stderr)
        sys.stderr.flush()
        os._exit(1)


atexit.register(_finish)
