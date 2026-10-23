"""Files sampan writes under the state dir are 0600 and its directories 0700, whatever the umask; a new config is 0600."""
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sampan import hook, update
from sampan.fsutil import atomic_write, private_dir
from sampan.procs import Proc
from sampan.registry import Row, lock, write_rows
from sampan.save import append_log
from tests.helpers import make_env
from tests.test_init import Base as InitBase
from tests.test_init import asker

SID = "aaaaaaaa-0000-4000-8000-000000000001"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Umask022(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)
        self.tmp = Path(tempfile.mkdtemp())


class FsutilTests(Umask022):
    def test_private_dir_creates_and_tightens(self):
        d = private_dir(self.tmp / "a" / "state")
        self.assertEqual(mode(d), 0o700)
        os.chmod(d, 0o755)
        private_dir(d)
        self.assertEqual(mode(d), 0o700)

    def test_atomic_write_mode_is_forced_even_over_a_loose_file(self):
        p = self.tmp / "f.json"
        atomic_write(p, "a", mode=0o600)
        self.assertEqual(mode(p), 0o600)
        os.chmod(p, 0o644)
        atomic_write(p, "b", mode=0o600)
        self.assertEqual(mode(p), 0o600)

    def test_new_mode_only_for_a_new_file(self):
        p = self.tmp / "config.toml"
        atomic_write(p, "a", new_mode=0o600)
        self.assertEqual(mode(p), 0o600)
        os.chmod(p, 0o644)
        atomic_write(p, "b", new_mode=0o600)
        self.assertEqual(mode(p), 0o644)  # an existing user file keeps its mode

    def test_default_is_unchanged_umask_for_new_file(self):
        p = self.tmp / "plain"
        atomic_write(p, "a")
        self.assertEqual(mode(p), 0o644)


class StateFilesTests(Umask022):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"

    def test_registry_history_and_lock(self):
        path, hist = self.state / "registry.json", self.state / "history"
        with lock(self.state):
            pass
        self.assertEqual(mode(self.state / "registry.lock"), 0o600)
        self.assertEqual(mode(self.state), 0o700)
        rows = [Row("one", "claude", "alice", SID, "/c")]
        write_rows(path, rows, "t", hist)
        os.chmod(path, 0o644)  # an older sampan left it world-readable
        os.chmod(self.state, 0o755)
        write_rows(path, rows, "t2", hist)
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(mode(self.state), 0o700)
        self.assertEqual(mode(hist), 0o700)
        snaps = list(hist.glob("registry-*.json"))
        self.assertTrue(snaps)
        for s in snaps:
            self.assertEqual(mode(s), 0o600)

    def test_update_cache(self):
        update.write_cache(self.state, "0.6.3", datetime(2026, 10, 23, tzinfo=timezone.utc))
        self.assertEqual(mode(self.state / update.CACHE), 0o600)
        self.assertEqual(mode(self.state), 0o700)

    def test_hook_record(self):
        fake = {p.pid: p for p in (Proc(100, 1, "S", "claude", "claude"), Proc(101, 100, "S", "sh", "sh"))}
        stdin = io.StringIO(json.dumps({"session_id": "s-1", "cwd": "/x", "source": "startup"}))
        hook.run_session_start(stdin, {}, state_dir=self.state, procs_fn=lambda: fake, now=lambda: "t",
                               start_pid=101)
        self.assertEqual(mode(self.state / "hooks" / "100.json"), 0o600)
        self.assertEqual(mode(self.state / "hooks"), 0o700)
        self.assertEqual(mode(self.state), 0o700)

    def test_save_log(self):
        cfg = make_env(self.tmp)
        append_log(cfg, "one")
        os.chmod(cfg.save_log, 0o644)
        append_log(cfg, "two")
        self.assertEqual(mode(cfg.save_log), 0o600)
        self.assertEqual(mode(cfg.state_dir), 0o700)
        self.assertEqual(len(cfg.save_log.read_text().splitlines()), 2)


class InitConfigModeTests(InitBase):
    def setUp(self):
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)
        super().setUp()

    def test_new_config_is_private(self):
        self.assertEqual(self.init(self.runner(), asker({}, []), no_launchd=True), 0)
        self.assertEqual(mode(self.cfg_path), 0o600)
