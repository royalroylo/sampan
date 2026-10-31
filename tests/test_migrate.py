import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from unittest import mock

from sampan import cli, folders, migrate, registry
from sampan.procs import is_uuid
from tests.helpers import make_env

NOW = datetime(2026, 10, 31, 12, 0, 0)
LATER = datetime(2026, 10, 31, 13, 0, 0)


def sid(i):
    return f"00000000-0000-4000-8000-{i:012d}"


def v2_sessions():
    """Ten rows shaped like a real 0.14 registry: claude and codex (one with the old empty profile), active and
    retired, pid set and null."""
    out = []
    for i, name in enumerate(["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa"]):
        out.append({"tmux": name, "agent": "claude", "profile": "alice" if i % 2 else "work", "session_id": sid(i),
                    "cwd": f"/work/proj{i}", "state": "retired" if i in (3, 7) else "active",
                    "seen_at": "2026-10-30T10:00:00+08:00", "server": "123-456", "pid": 1000 + i if i % 3 else None})
    out.append({"tmux": "lambda", "agent": "codex", "profile": "", "session_id": sid(10), "cwd": "/work/cx",
                "state": "active", "seen_at": "", "server": "", "pid": None})
    return out


V1_FOLDERS = {
    "version": 1,
    "folders": {"客戶": {"collapsed": False}, "客戶/甲": {"collapsed": True}, "客戶/乙": {"collapsed": False},
                "空的": {"collapsed": False}, "開發": {"collapsed": True}},
    # "工具/小" implies 「工具」; "later" has no registry row yet (it waits as pending)
    "sessions": {"alpha": "客戶/甲", "beta": "客戶/甲", "gamma": "客戶/乙", "delta": "客戶", "zeta": "開發",
                 "eta": "工具/小", "later": "開發"},
}


@dataclass
class Row014:
    """sampan 0.14's registry row: Row(**s) raises TypeError on any key it does not know."""
    tmux: str
    agent: str
    profile: str
    session_id: str
    cwd: str
    state: str = "active"
    seen_at: str = ""
    server: str = ""
    pid: int | None = None


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_env(Path(self._tmp.name))
        self.state = self.cfg.state_dir
        self.state.mkdir(parents=True, exist_ok=True)

    def put_v2(self, sessions=None, folders_doc=V1_FOLDERS):
        doc = {"version": 2, "saved_at": "2026-10-30T10:00:00+08:00", "sessions": sessions or v2_sessions()}
        (self.state / "registry.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        if folders_doc is not None:
            (self.state / "folders.json").write_text(json.dumps(folders_doc, ensure_ascii=False, indent=1) + "\n")

    def put_dispatches(self):
        doc = {"version": 2, "dispatches": [{"id": "d1", "to": "alpha", "from": "beta", "status": "done"}]}
        (self.state / "dispatches.json").write_text(json.dumps(doc) + "\n")

    def read(self, name):
        return json.loads((self.state / name).read_text())

    def bytes_of(self, *names):
        return {n: (self.state / n).read_bytes() for n in names}

    def all_files(self):
        """Every file under state (the empty registry.lock that taking the lock leaves is not data)."""
        return {str(p.relative_to(self.state)): p.read_bytes() for p in sorted(self.state.rglob("*"))
                if p.is_file() and p.name != "registry.lock"}

    def backups(self):
        return sorted(p for p in self.state.iterdir() if p.name.startswith("backup-0.15-"))

    def ensure(self, now=NOW):
        buf = io.StringIO()
        rc = migrate.ensure(self.cfg, lambda m="": buf.write(m + "\n"), now=now)
        return rc, buf.getvalue()

    def cmd(self, **kw):
        buf = io.StringIO()
        rc = migrate.cmd_migrate(self.cfg, lambda m="": buf.write(m + "\n"), now=NOW, **kw)
        return rc, buf.getvalue()

    # --- needed / snapshot

    def test_empty_state_needs_nothing(self):
        self.assertFalse(migrate.needed(self.cfg))
        self.assertEqual(self.ensure(), (0, ""))
        self.assertEqual(self.all_files(), {})

    def test_needed_for_old_formats_only(self):
        self.put_v2()
        self.assertTrue(migrate.needed(self.cfg))
        self.ensure()
        self.assertFalse(migrate.needed(self.cfg))

    def test_snapshot_shape(self):
        self.put_v2()
        snap = migrate.snapshot(self.cfg)
        self.assertIn(("alpha", sid(0), "claude", "work", "active"), snap["sessions"])
        self.assertEqual(len(snap["sessions"]), 11)
        self.assertIn(("alpha", "客戶/甲"), snap["membership"])
        self.assertIn(("later", "開發"), snap["membership"])
        self.assertEqual(snap["sessions"], sorted(snap["sessions"]))

    # --- ensure

    def test_migrate_real_shape_copy(self):
        self.put_v2()
        self.put_dispatches()
        before = migrate.snapshot(self.cfg)
        rc, text = self.ensure()
        self.assertEqual(rc, 0, text)
        self.assertIn("已把資料升到新格式", text)
        self.assertIn("backup-0.15-20261031-120000", text)
        self.assertEqual(migrate.snapshot(self.cfg), before)
        reg = self.read("registry.json")
        self.assertEqual(reg["version"], 3)
        self.assertTrue(all(is_uuid(s["id"]) for s in reg["sessions"]))
        self.assertEqual(len({s["id"] for s in reg["sessions"]}), 11)
        fol = self.read("folders.json")
        self.assertEqual(fol["version"], 2)
        self.assertEqual(fol["pending"], {"later": next(i for i, m in fol["folders"].items() if m["name"] == "開發")})
        tree = folders.load(self.state)
        self.assertIn("空的", tree.folders)
        self.assertTrue(tree.folders["客戶/甲"])
        self.assertTrue(tree.folders["開發"])
        self.assertFalse(tree.folders["工具"])
        self.assertEqual(self.read("dispatches.json")["version"], 2)

    def test_second_run_changes_nothing(self):
        self.put_v2()
        self.put_dispatches()
        self.ensure()
        files = self.all_files()
        self.assertEqual(self.ensure(LATER), (0, ""))
        self.assertEqual(self.all_files(), files)
        self.assertEqual(len(self.backups()), 1)

    def test_interrupted_after_registry_resumes(self):
        self.put_v2()
        rows = registry.load_rows(self.cfg.registry_path, codex_profile=self.cfg.codex.profile)
        registry.write_rows(self.cfg.registry_path, rows, "t", self.cfg.history_dir)  # v3, ids filled; folders still v1
        ids = {s["tmux"]: s["id"] for s in self.read("registry.json")["sessions"]}
        before = migrate.snapshot(self.cfg)
        self.assertTrue(migrate.needed(self.cfg))
        rc, text = self.ensure()
        self.assertEqual(rc, 0, text)
        self.assertEqual({s["tmux"]: s["id"] for s in self.read("registry.json")["sessions"]}, ids)
        self.assertEqual(self.read("folders.json")["version"], 2)
        self.assertEqual(migrate.snapshot(self.cfg), before)
        self.assertEqual(set(self.read("folders.json")["sessions"]), {ids[n] for n in V1_FOLDERS["sessions"] if n != "later"})

    def test_mismatch_restores_backup(self):
        self.put_v2()
        self.put_dispatches()
        original = self.bytes_of("registry.json", "folders.json", "dispatches.json")
        real_save = folders.save

        def lossy_save(state_dir, tree, rows=None):
            tree.sessions.pop("gamma", None)  # a bug that drops a membership
            return real_save(state_dir, tree, rows)

        with mock.patch("sampan.folders.save", side_effect=lossy_save):
            rc, text = self.ensure()
        self.assertEqual(rc, 2)
        self.assertIn("gamma", text)
        self.assertIn("還原", text)
        self.assertEqual(self.bytes_of("registry.json", "folders.json", "dispatches.json"), original)
        self.assertEqual(len(self.backups()), 1)  # the backup is kept

    def test_write_error_restores_backup(self):
        self.put_v2()
        original = self.bytes_of("registry.json", "folders.json")
        with mock.patch("sampan.folders.save", side_effect=OSError("disk full")):
            rc, text = self.ensure()
        self.assertEqual(rc, 2)
        self.assertIn("disk full", text)
        self.assertEqual(self.bytes_of("registry.json", "folders.json"), original)

    def test_unreadable_folders_touches_nothing(self):
        self.put_v2(folders_doc=None)
        (self.state / "folders.json").write_text("{not json")
        files = self.all_files()
        rc, text = self.ensure()
        self.assertEqual(rc, 2)
        self.assertIn("沒有升級", text)
        self.assertEqual(self.all_files(), files)

    def test_backup_has_all_three_files(self):
        self.put_v2()
        self.put_dispatches()
        original = self.bytes_of("registry.json", "folders.json", "dispatches.json")
        self.ensure()
        [backup] = self.backups()
        self.assertEqual(backup.name, "backup-0.15-20261031-120000")
        self.assertEqual({n: (backup / n).read_bytes() for n in original}, original)

    def test_backup_skips_missing_files(self):
        self.put_v2(folders_doc=None)
        self.ensure()
        [backup] = self.backups()
        self.assertEqual(sorted(p.name for p in backup.iterdir()), ["registry.json"])
        self.assertFalse((self.state / "folders.json").exists())

    def test_backup_dir_never_reused(self):
        self.put_v2()
        (self.state / "backup-0.15-20261031-120000").mkdir()
        self.ensure()
        self.assertEqual([p.name for p in self.backups()], ["backup-0.15-20261031-120000", "backup-0.15-20261031-120000-2"])

    # --- sampan migrate

    def test_dry_run_writes_nothing(self):
        self.put_v2()
        self.put_dispatches()
        files = self.all_files()
        rc, text = self.cmd(dry_run=True)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.all_files(), files)
        self.assertIn("11", text)  # ids it would create
        self.assertIn("沒有差異", text)

    def test_dry_run_when_already_migrated(self):
        self.put_v2()
        self.ensure()
        rc, text = self.cmd(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("已是新格式", text)

    def test_migrate_command_runs_ensure(self):
        self.put_v2()
        rc, text = self.cmd()
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.read("registry.json")["version"], 3)
        rc, text = self.cmd()
        self.assertEqual(rc, 0)
        self.assertIn("已是新格式", text)

    def test_revert_writes_v2_v1_that_0_14_can_read(self):
        self.put_v2()
        self.put_dispatches()
        before = migrate.snapshot(self.cfg)
        self.ensure()
        ids = {s["tmux"]: s["id"] for s in self.read("registry.json")["sessions"]}
        rc, text = self.cmd(revert=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("降版前請先跑這個；再升級會產生新的 id", text)
        reg = self.read("registry.json")
        self.assertEqual(set(reg), {"version", "saved_at", "sessions"})
        self.assertEqual(reg["version"], 2)
        known = {f.name for f in fields(Row014)}
        for s in reg["sessions"]:
            self.assertLessEqual(set(s), known)
            Row014(**({"agent": "claude", "pid": None} | s))  # 0.14's reader
        fol = self.read("folders.json")
        self.assertEqual(fol["version"], 1)
        self.assertEqual(fol["folders"]["客戶/甲"], {"collapsed": True})
        self.assertIn("空的", fol["folders"])
        self.assertEqual(fol["sessions"]["alpha"], "客戶/甲")
        self.assertEqual(fol["sessions"]["later"], "開發")  # a pending member, by name
        self.assertEqual(migrate.snapshot(self.cfg), before)
        self.assertEqual(len(self.backups()), 2)  # migrate's and revert's (same stamp, so -2)
        # 0.15 again: migrates with new ids
        self.assertTrue(migrate.needed(self.cfg))
        self.assertEqual(self.ensure(LATER)[0], 0)
        again = {s["tmux"]: s["id"] for s in self.read("registry.json")["sessions"]}
        self.assertTrue(set(again.values()).isdisjoint(ids.values()))
        self.assertEqual(migrate.snapshot(self.cfg), before)

    def test_revert_without_registry(self):
        rc, text = self.cmd(revert=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.all_files(), {})

    def test_doctor_line(self):
        from sampan.doctor import _data_format
        self.put_v2()
        [c] = _data_format(self.cfg)
        self.assertEqual(c.level, "warn")
        self.assertIn("登記表 v2、資料夾 v1", c.title)
        self.assertIn("上次備份 （沒有）", c.title)
        self.ensure()
        self.ensure(LATER)  # nothing to do: no second backup
        [c] = _data_format(self.cfg)
        self.assertEqual(c.level, "info")
        self.assertIn("登記表 v3、資料夾 v2", c.title)
        self.assertIn("backup-0.15-20261031-120000", c.title)
        (self.state / "backup-0.15-20261031-120000-10").mkdir()
        (self.state / "backup-0.15-20261031-120000-9").mkdir()
        self.assertEqual(migrate.last_backup(self.state).name, "backup-0.15-20261031-120000-10")

    def test_dry_run_and_revert_together_refused(self):
        rc, _text = self.cmd(dry_run=True, revert=True)
        self.assertEqual(rc, 2)


class CliMigrateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_env(Path(self._tmp.name))
        p = mock.patch("sampan.cli.load_config", return_value=self.cfg)
        p.start()
        self.addCleanup(p.stop)
        self.cfg.state_dir.mkdir(parents=True)
        doc = {"version": 2, "saved_at": "t", "sessions": v2_sessions()}
        self.cfg.registry_path.write_text(json.dumps(doc))

    def version(self):
        return json.loads(self.cfg.registry_path.read_text())["version"]

    def test_state_command_migrates_first(self):
        with mock.patch("sampan.folder_cmd.cmd_folder", return_value=0) as fc, redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(cli.main(["folder", "list"]), 0)
        fc.assert_called_once()
        self.assertEqual(self.version(), 3)
        self.assertIn("已把資料升到新格式", buf.getvalue())

    def test_failed_migration_stops_the_command(self):
        with mock.patch("sampan.migrate.ensure", return_value=2), \
                mock.patch("sampan.folder_cmd.cmd_folder", return_value=0) as fc:
            self.assertEqual(cli.main(["folder", "list"]), 2)
        fc.assert_not_called()

    def test_doctor_does_not_migrate(self):
        with mock.patch("sampan.doctor.cmd_doctor", return_value=0):
            self.assertEqual(cli.main(["doctor"]), 0)
        self.assertEqual(self.version(), 2)

    def test_mcp_serve_migrates_to_stderr(self):
        with mock.patch("sampan.mcp_server.serve", return_value=0), \
                mock.patch("sampan.mcp_server.identify_caller", return_value=None), \
                mock.patch("sampan.mcp_server.parent_is_agent", return_value=False), \
                redirect_stdout(io.StringIO()) as buf, mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.main(["mcp", "serve"]), 0)
        self.assertEqual(buf.getvalue(), "")
        self.assertIn("已把資料升到新格式", err.getvalue())
        self.assertEqual(self.version(), 3)

    def test_migrate_subcommand(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(cli.main(["migrate", "--dry-run"]), 0)
        self.assertEqual(self.version(), 2)
        self.assertIn("沒有差異", buf.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["migrate"]), 0)
            self.assertEqual(self.version(), 3)
            self.assertEqual(cli.main(["migrate", "--revert"]), 0)
        self.assertEqual(self.version(), 2)


if __name__ == "__main__":
    unittest.main()
