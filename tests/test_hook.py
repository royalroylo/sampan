import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan import hook
from sampan.procs import Proc
from tests.helpers import make_env

NOW = "2026-10-20T10:00:00+08:00"


def procs(*items):
    return {p.pid: p for p in (Proc(pid, ppid, "S", comm, comm) for pid, ppid, comm in items)}


def stdin_for(**kw):
    return io.StringIO(json.dumps({"session_id": "s-1", "cwd": "/Users/u/proj", "source": "startup", **kw}))


class RunSessionStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"

    def test_record_written_for_claude_ancestor(self):
        fake = procs((100, 1, "claude"), (101, 100, "sh"), (102, 101, "sampan"))
        hook.run_session_start(stdin_for(), {"TMUX_PANE": "%3", "CLAUDE_CONFIG_DIR": "/c/work"},
                               state_dir=self.state, procs_fn=lambda: fake, now=lambda: NOW, start_pid=102)
        rec = json.loads((self.state / "hooks" / "100.json").read_text())
        self.assertEqual(rec, {"pid": 100, "session_id": "s-1", "cwd": "/Users/u/proj", "pane": "%3",
                               "config_dir": "/c/work", "source": "startup", "at": NOW})
        self.assertEqual([p.name for p in (self.state / "hooks").iterdir()], ["100.json"])

    def test_record_defaults_without_env(self):
        fake = procs((100, 1, "claude"), (101, 100, "sh"))
        hook.run_session_start(stdin_for(), {}, state_dir=self.state, procs_fn=lambda: fake,
                               now=lambda: NOW, start_pid=101)
        rec = json.loads((self.state / "hooks" / "100.json").read_text())
        self.assertIsNone(rec["pane"])
        self.assertEqual(rec["config_dir"], "")

    def test_hook_is_silent_and_never_fails(self):
        good = procs((100, 1, "claude"), (101, 100, "sh"))
        no_claude = procs((100, 1, "zsh"), (101, 100, "sh"))
        blocker = self.tmp / "file"
        blocker.write_text("x")

        def boom():
            raise RuntimeError("ps failed")

        cases = [
            (io.StringIO("not json"), self.state, lambda: good),
            (io.StringIO("[1, 2]"), self.state, lambda: good),
            (stdin_for(), self.state, lambda: no_claude),
            (stdin_for(), self.state, boom),
            (stdin_for(), blocker / "sub", lambda: good),  # state_dir is a file underneath, so not writable
            (stdin_for(session_id=None), self.state, lambda: good),
        ]
        for stdin, state, fn in cases:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                hook.run_session_start(stdin, {}, state_dir=state, procs_fn=fn, now=lambda: NOW, start_pid=101)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "")
        self.assertFalse((self.state / "hooks").exists())


class ReadRecordsTests(unittest.TestCase):
    def test_read_hook_records_prunes_dead(self):
        state = Path(tempfile.mkdtemp())
        d = state / "hooks"
        d.mkdir()
        for pid in (100, 200):
            (d / f"{pid}.json").write_text(json.dumps(
                {"pid": pid, "session_id": f"s-{pid}", "cwd": "/x", "pane": "%1", "config_dir": "",
                 "source": "resume", "at": NOW}))
        (d / "junk.json").write_text("{")
        recs = hook.read_hook_records(state, {100})
        self.assertEqual(set(recs), {100})
        self.assertEqual(recs[100].session_id, "s-100")
        self.assertEqual(recs[100].pane, "%1")
        self.assertTrue((d / "100.json").exists())
        self.assertFalse((d / "200.json").exists())

    def test_read_without_dir(self):
        self.assertEqual(hook.read_hook_records(Path(tempfile.mkdtemp()) / "nope", {1}), {})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.profile = self.cfg.profiles["work"]
        self.settings = self.profile.config_dir / "settings.json"
        self.sampan = self.tmp / "bin" / "sampan"
        self.sampan.parent.mkdir()
        self.sampan.write_text("#!/bin/sh\n")

    def commands(self):
        data = json.loads(self.settings.read_text())
        return [h["command"] for g in data.get("hooks", {}).get("SessionStart", []) for h in g["hooks"]]

    def test_install_creates_missing_settings(self):
        self.assertEqual(hook.hook_status(self.profile), "absent")
        self.assertTrue(hook.install_hook(self.profile, self.sampan))
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": f"{self.sampan} hook session-start"}]}]}})
        self.assertEqual(hook.hook_status(self.profile), "installed")

    def test_install_preserves_other_hooks_and_is_idempotent(self):
        original = {"model": "opus", "hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}]}}
        self.settings.write_text(json.dumps(original))
        self.assertTrue(hook.install_hook(self.profile, self.sampan))
        self.assertTrue(hook.install_hook(self.profile, self.sampan))
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["Stop"], original["hooks"]["Stop"])
        self.assertEqual(self.commands(), ["echo hi", f"{self.sampan} hook session-start"])
        self.assertEqual(len(list(self.settings.parent.glob("settings.json.bak-sampan-*"))), 1)

    def test_unreadable_json_is_left_alone(self):
        self.settings.write_text("{oops")
        self.assertFalse(hook.install_hook(self.profile, self.sampan))
        self.assertFalse(hook.uninstall_hook(self.profile))
        self.assertEqual(self.settings.read_text(), "{oops")
        self.assertEqual(hook.hook_status(self.profile), "absent")

    def test_uninstall_removes_only_ours(self):
        self.settings.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "echo hi"},
                       {"type": "command", "command": f"{self.sampan} hook session-start"}]}]}}))
        self.assertTrue(hook.uninstall_hook(self.profile))
        self.assertEqual(self.commands(), ["echo hi"])
        self.assertEqual(hook.hook_status(self.profile), "absent")

    def test_uninstall_drops_empty_containers(self):
        self.settings.write_text(json.dumps({"model": "opus"}))
        hook.install_hook(self.profile, self.sampan)
        self.assertTrue(hook.uninstall_hook(self.profile))
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "opus"})

    def test_uninstall_nothing_to_remove(self):
        self.assertFalse(hook.uninstall_hook(self.profile))
        self.assertFalse(self.settings.exists())

    def test_install_writes_through_symlink_and_keeps_mode(self):
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"model": "opus"}))
        os.chmod(real, 0o600)
        self.settings.symlink_to(real)
        self.assertTrue(hook.install_hook(self.profile, self.sampan))
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(self.settings.resolve(), real.resolve())
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)
        self.assertEqual(self.commands(), [f"{self.sampan} hook session-start"])
        self.assertTrue(hook.uninstall_hook(self.profile))
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"model": "opus"})
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)

    def test_failed_write_leaves_file_and_no_temp(self):
        self.settings.write_text(json.dumps({"model": "opus"}))
        with mock.patch("sampan.fsutil.os.replace", side_effect=OSError("disk full")):
            self.assertFalse(hook.install_hook(self.profile, self.sampan))
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "opus"})
        self.assertEqual(list(self.settings.parent.glob(".settings.json.tmp-*")), [])
        hook.install_hook(self.profile, self.sampan)
        with mock.patch("sampan.fsutil.os.replace", side_effect=OSError("disk full")):
            self.assertFalse(hook.uninstall_hook(self.profile))
        self.assertEqual(hook.hook_status(self.profile), "installed")
        self.assertEqual(list(self.settings.parent.glob(".settings.json.tmp-*")), [])

    def test_status_stale_when_path_missing(self):
        hook.install_hook(self.profile, self.sampan)
        self.sampan.unlink()
        self.assertEqual(hook.hook_status(self.profile), "stale")


if __name__ == "__main__":
    unittest.main()
