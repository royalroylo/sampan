import io
import json
import shutil
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from unittest import mock

from sampan import cli, dispatch, migrate, migrate_017, store
from sampan.dispatch import Dispatch
from sampan.migrate_017 import MigrateError
from tests.fakes import FakeRunner
from tests.helpers import make_env

FIXTURE = Path(__file__).parent / "fixtures" / "dispatches-0.16.json"
NOW = datetime(2026, 11, 12, 9, 0, 0)
LATER = datetime(2026, 11, 12, 10, 0, 0)
STUB = {"version": 3, "dispatches": [], "moved_to": "dispatches.db"}
REINSTALL = "uv tool install --reinstall git+https://github.com/royalroylo/sampan@v0.16.1"
OLD_FIELDS = [f.name for f in fields(Dispatch) if f.name not in ("status", "in_reply_to", "thread", "extra")]

# id -> (status, delivery kind or None, delivery state, delivery link archived, reply part text or None)
EXPECTED = {
    "a001": ("queued", None, None, None, None),
    "a002": ("pasted", "paste", "active", 0, None),  # 送出中: the checkpoint before any key was pressed
    "a003": ("pasted", "paste", "active", 0, None),
    "a004": ("replied", "paste", "ended", 0, "好了，改完了。\n\n第二段"),
    "a005": ("failed", "paste", "ended", 0, None),  # pasted, Enter not pressed
    "a006": ("failed", None, None, None, None),  # never sent
    "a007": ("failed", "paste", "ended", 0, "做到一半的回覆"),
    "a008": ("cancelled", "paste", "ended", 1, None),
    "a009": ("cancelled", None, None, None, None),
    "a00a": ("replied", "raw", "ended", 0, "指令已執行（沒有文字回覆）"),
    "a00b": ("pasted", "paste", "ended", 0, None),  # an older "sent" to the same target: only the newest is active
    "a00c": ("replied", "paste", "ended", 0, "舊的回覆"),
}


class Killed(BaseException):
    """The process dies here: nothing after it runs, no clean-up either."""


def read_016(state_dir):
    """sampan 0.16's dispatch.load, copied (not imported): READABLE_VERSIONS = (1, 2)."""
    readable = (1, 2)
    path = Path(state_dir) / "dispatches.json"
    data = json.loads(path.read_text())
    if data["version"] not in readable:
        raise ValueError(f"{path} 是未知的版本 {data['version']!r}（只認得 {'、'.join(map(str, readable))}）。不會覆蓋它。")
    return data["dispatches"]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_env(Path(self._tmp.name))
        self.state = self.cfg.state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE, self.state / "dispatches.json")
        self.original = (self.state / "dispatches.json").read_bytes()
        self.old = {r["id"]: r for r in json.loads(self.original)["dispatches"]}

    def ensure(self, now=NOW, **kw):
        buf = io.StringIO()
        rc = migrate.ensure(self.cfg, lambda m="": buf.write(m + "\n"), now=now, **kw)
        return rc, buf.getvalue()

    def cmd(self, now=NOW, **kw):
        buf = io.StringIO()
        rc = migrate.cmd_migrate(self.cfg, lambda m="": buf.write(m + "\n"), now=now, **kw)
        return rc, buf.getvalue()

    def json_doc(self):
        return json.loads((self.state / "dispatches.json").read_text())

    def sql(self, query, *args):
        conn = store.connect(self.state)
        try:
            return conn.execute(query, args).fetchall()
        finally:
            conn.close()

    def all_files(self):
        """Every file under state (the empty dispatches.lock that taking the lock leaves is not data)."""
        return {str(p.relative_to(self.state)): p.read_bytes() for p in sorted(self.state.rglob("*"))
                if p.is_file() and p.name != "dispatches.lock"}

    def backups(self):
        return sorted(p for p in self.state.iterdir() if p.name.startswith(migrate_017.BACKUP_PREFIX))

    def assert_migrated(self, extra_ids=()):
        self.assertEqual(self.json_doc(), STUB)
        self.assertFalse((self.state / migrate_017.JOURNAL).exists())
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith("dispatches.db.tmp")], [])
        self.assertEqual({d.id for d in dispatch.load(self.state)}, set(self.old) | set(extra_ids))

    def assert_json_whole(self):
        """dispatches.json is a complete v2 (the original) or the complete v3 stub, never anything in between."""
        doc = self.json_doc()
        if doc["version"] == 3:
            self.assertEqual(doc, STUB)
        else:
            self.assertEqual((self.state / "dispatches.json").read_bytes(), self.original)


class UpgradeTest(Base):
    def test_needed(self):
        self.assertTrue(migrate_017.needed(self.cfg))
        self.assertFalse(migrate_017.reverted(self.cfg))
        migrate_017.upgrade(self.cfg, now=NOW)
        self.assertFalse(migrate_017.needed(self.cfg))
        (self.state / "dispatches.json").unlink()
        self.assertFalse(migrate_017.needed(self.cfg))

    def test_upgrade_maps_every_old_state(self):
        lines = migrate_017.upgrade(self.cfg, now=NOW)
        self.assertTrue(any("12" in line for line in lines), lines)
        self.assert_migrated()
        self.assertEqual(stat.S_IMODE((self.state / store.DB_FILE).stat().st_mode), 0o600)
        [bk] = self.backups()
        self.assertEqual(bk.name, "backup-0.17-20261112-090000")
        self.assertEqual((bk / "dispatches.json").read_bytes(), self.original)
        loaded = {d.id: d for d in dispatch.load(self.state)}
        for did, rec in self.old.items():
            d = loaded[did]
            for name in OLD_FIELDS:
                default = Dispatch(**{k: "" for k in ("id", "from_name", "from_profile", "from_agent", "to_name",
                                                      "to_agent", "to_profile", "text", "state", "created")})
                self.assertEqual(getattr(d, name), rec.get(name, getattr(default, name)), f"{did}.{name}")
        self.assertEqual(loaded["a00c"].extra, {"future_field": {"x": 1}})
        for did, (status, kind, dstate, link_archived, part) in EXPECTED.items():
            [(got_status,)] = self.sql("SELECT status FROM messages WHERE id=?", did)
            self.assertEqual(got_status, status, did)
            dels = self.sql("SELECT d.*, dm.archived AS link_archived, dm.position FROM deliveries d "
                            "JOIN delivery_messages dm ON dm.delivery=d.id WHERE dm.message=?", did)
            if kind is None:
                self.assertEqual(dels, [], did)
            else:
                [dl] = dels
                rec = self.old[did]
                self.assertEqual((dl["kind"], dl["state"], dl["link_archived"]), (kind, dstate, link_archived), did)
                self.assertEqual(dl["reason"], rec["reason"], did)
                self.assertEqual(dl["sent_at"], rec["sent_at"], did)
                self.assertEqual(dl["text"], rec["text"], did)
                self.assertEqual(dl["to_id"], rec.get("to_id", ""), did)
                self.assertEqual(dl["conversation"], rec.get("thread_id"), did)
                self.assertEqual(dl["ended_at"], rec["done_at"] if dstate == "ended" and did != "a00b" else None, did)
            parts = self.sql("SELECT p.*, r.delivery, r.source FROM reply_parts p JOIN replies r ON r.id=p.reply "
                             "WHERE p.message=?", did)
            if part is None:
                self.assertEqual(parts, [], did)
            else:
                [p] = parts
                self.assertEqual((p["how"], p["text"], p["late"], p["text_len"]), ("whole", part, 0, len(part)), did)
                self.assertEqual(p["at"], self.old[did]["done_at"], did)
                self.assertTrue(p["id"].startswith("p") and len(p["id"]) == 9, p["id"])
        # one active delivery per target
        self.assertEqual(sorted(r[0] for r in self.sql("SELECT to_id FROM deliveries WHERE state='active'")),
                         ["s-beta", "s-gamma"])

    def test_map_old_rows(self):
        msg, dl, part = migrate_017.map_old(self.old["a004"])
        self.assertEqual((msg["id"], msg["state"], msg["status"]), ("a004", "done", "replied"))
        self.assertEqual((dl["kind"], dl["state"]), ("paste", "ended"))
        self.assertEqual((part["how"], part["text"]), ("whole", self.old["a004"]["reply"]))
        self.assertEqual(migrate_017.map_old(self.old["a001"])[1:], (None, None))
        msg, dl, part = migrate_017.map_old(self.old["a002"])
        self.assertEqual((msg["status"], dl["reason"], part), ("pasted", "送出中", None))
        with self.assertRaises(MigrateError):
            migrate_017.map_old({"id": "zz"})  # 0.16 could not read it either
        with self.assertRaises(MigrateError):
            migrate_017.map_old({**self.old["a001"], "state": "lost"})

    def test_stub_is_refused_by_0_16_reader(self):
        self.assertEqual(len(read_016(self.state)), 12)
        migrate_017.upgrade(self.cfg, now=NOW)
        with self.assertRaises(ValueError) as cm:
            read_016(self.state)
        self.assertIn("未知的版本 3", str(cm.exception))

    def test_ensure_runs_it_after_0_15_and_says_so(self):
        rc, text = self.ensure()
        self.assertEqual(rc, 0, text)
        self.assertIn("dispatches.db", text)
        self.assertIn("backup-0.17-20261112-090000", text)
        self.assert_migrated()
        self.assertEqual(self.ensure(LATER), (0, ""))  # nothing left to do
        self.assertEqual(len(self.backups()), 1)

    def test_0_15_failure_stops_before_0_17(self):
        with mock.patch("sampan.migrate.needed", return_value=True), \
                mock.patch("sampan.migrate._upgrade", return_value=(2, ["❌ 0.15 壞了"], None, "")):
            rc, _text = self.ensure()
        self.assertEqual(rc, 2)
        self.assertEqual((self.state / "dispatches.json").read_bytes(), self.original)
        self.assertFalse((self.state / store.DB_FILE).exists())

    def test_interrupted_at_each_step_recovers(self):
        for step, at in (("_build", "backed_up"), ("_install", "db_written"), ("_write_stub", "db_ready"),
                         ("_drop_journal", "stub_written")):
            with self.subTest(at=at):
                self.reset()
                with mock.patch.object(migrate_017, step, side_effect=Killed), self.assertRaises(Killed):
                    self.ensure()
                self.assertEqual(json.loads((self.state / migrate_017.JOURNAL).read_text())["step"], at)
                self.assert_json_whole()
                self.assertTrue(migrate_017.needed(self.cfg))
                rc, text = self.ensure(LATER)
                self.assertEqual(rc, 0, text)
                self.assert_migrated()
                self.assertEqual(len(self.backups()), 1)  # the same bytes: the backup is reused
                self.assertEqual(self.ensure(LATER), (0, ""))

    def reset(self):
        for p in list(self.state.iterdir()):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copyfile(FIXTURE, self.state / "dispatches.json")

    def test_failure_rolls_back_to_the_v2(self):
        with mock.patch.object(migrate_017, "_write_stub", side_effect=OSError("disk full")):
            with self.assertRaises(MigrateError) as cm:
                migrate_017.upgrade(self.cfg, now=NOW)
        self.assertIn("disk full", str(cm.exception))
        self.assertEqual((self.state / "dispatches.json").read_bytes(), self.original)
        self.assertFalse((self.state / store.DB_FILE).exists())
        self.assertFalse((self.state / migrate_017.JOURNAL).exists())
        self.assertEqual(len(self.backups()), 1)  # kept
        migrate_017.upgrade(self.cfg, now=LATER)
        self.assert_migrated()
        self.assertEqual(len(self.backups()), 1)

    def test_mismatch_after_writing_rolls_back(self):
        real = migrate_017._insert

        def lossy(conn, olds):
            real(conn, olds)
            conn.execute("UPDATE messages SET reply='不一樣' WHERE id='a004'")

        with mock.patch.object(migrate_017, "_insert", side_effect=lossy), self.assertRaises(MigrateError) as cm:
            migrate_017.upgrade(self.cfg, now=NOW)
        self.assertIn("a004", str(cm.exception))
        self.assertEqual((self.state / "dispatches.json").read_bytes(), self.original)
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith("dispatches.db")], [])

    def test_failure_blocks_only_dispatch_commands(self):
        doc = json.loads(self.original)
        doc["dispatches"].append({"id": "bad1"})  # 0.16 could not read it either
        (self.state / "dispatches.json").write_text(json.dumps(doc))
        before = self.all_files()
        rc, text = self.ensure(touches_dispatches=True)
        self.assertEqual(rc, 2)
        self.assertIn("派工資料沒有升級", text)
        rc, text = self.ensure()
        self.assertEqual(rc, 0)
        self.assertIn("派工資料沒有升級", text)
        after = self.all_files()
        self.assertEqual(after.pop("dispatches.json"), before.pop("dispatches.json"))
        self.assertFalse(any(n.startswith("dispatches.db") or n == migrate_017.JOURNAL for n in after))

    def test_db_beside_v2_without_journal_is_not_touched(self):
        conn = store.connect(self.state, create=True)
        conn.close()
        files = self.all_files()
        with self.assertRaises(MigrateError) as cm:
            migrate_017.upgrade(self.cfg, now=NOW)
        self.assertIn("sampan migrate --revert", str(cm.exception))
        self.assertEqual(self.all_files(), files)

    def test_leftover_wal_and_shm_of_a_half_db_are_removed(self):
        def half(state_dir, olds):
            for suffix in ("", "-wal", "-shm", "-journal"):
                (Path(state_dir) / f"dispatches.db.tmp{suffix}").write_bytes(b"half written")
            raise Killed

        for suffix in ("-wal", "-shm"):  # orphans of a db that is not there
            (self.state / f"dispatches.db{suffix}").write_bytes(b"\x00junk" * 100)
        with mock.patch.object(migrate_017, "_build", side_effect=half), self.assertRaises(Killed):
            self.ensure()
        rc, text = self.ensure(LATER)
        self.assertEqual(rc, 0, text)
        self.assert_migrated()
        for suffix in ("-wal", "-shm"):
            p = self.state / f"dispatches.db{suffix}"
            self.assertFalse(p.exists() and b"junk" in p.read_bytes())
        self.assertEqual(self.sql("PRAGMA integrity_check")[0][0], "ok")

    def concurrent_writer(self, times):
        """_build that, the first `times` calls, lets a 0.16 add a dispatch to dispatches.json meanwhile."""
        real = migrate_017._build
        calls = []

        def build(state_dir, olds):
            real(state_dir, olds)
            calls.append(1)
            if len(calls) <= times:
                doc = json.loads((self.state / "dispatches.json").read_text())
                doc["dispatches"].append({**self.old["a001"], "id": f"f{len(calls):03d}"})
                (self.state / "dispatches.json").write_text(json.dumps(doc, ensure_ascii=False))
        return build, calls

    def test_concurrent_v2_write_restarts_from_backup(self):
        build, calls = self.concurrent_writer(1)
        with mock.patch.object(migrate_017, "_build", side_effect=build):
            migrate_017.upgrade(self.cfg, now=NOW)
        self.assertEqual(len(calls), 2)
        self.assert_migrated(extra_ids={"f001"})
        first, second = self.backups()
        self.assertEqual((first / "dispatches.json").read_bytes(), self.original)
        self.assertIn(b"f001", (second / "dispatches.json").read_bytes())

    def test_concurrent_writes_three_times_fail(self):
        build, calls = self.concurrent_writer(99)
        with mock.patch.object(migrate_017, "_build", side_effect=build), self.assertRaises(MigrateError):
            migrate_017.upgrade(self.cfg, now=NOW)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.json_doc()["version"], 2)
        self.assertIn("f003", {r["id"] for r in self.json_doc()["dispatches"]})
        self.assertFalse((self.state / store.DB_FILE).exists())
        self.assertFalse((self.state / migrate_017.JOURNAL).exists())

    def test_upgrade_takes_the_dispatches_lock(self):
        import fcntl
        seen = []
        real = migrate_017._build

        def build(state_dir, olds):
            with open(self.state / "dispatches.lock") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append(False)
                except OSError:
                    seen.append(True)
            real(state_dir, olds)

        with mock.patch.object(migrate_017, "_build", side_effect=build):
            migrate_017.upgrade(self.cfg, now=NOW)
        self.assertEqual(seen, [True])

    def test_dry_run_writes_nothing(self):
        files = self.all_files()
        rc, text = self.cmd(dry_run=True)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.all_files(), files)
        self.assertIn("12", text)
        self.assertIn("done 3", text)
        self.assertIn("sent 3", text)
        self.assertIn("沒有差異", text)
        self.assertEqual(migrate_017.dry_run(self.cfg)[0].count("12"), 1)

    def test_fresh_install_needs_nothing(self):
        (self.state / "dispatches.json").unlink()
        self.assertFalse(migrate_017.needed(self.cfg))
        self.assertEqual(self.ensure(touches_dispatches=True), (0, ""))
        rc, text = self.cmd()
        self.assertEqual(rc, 0)
        self.assertIn("不用升級", text)


class RevertTest(Base):
    def add_new_message(self):
        """A 0.17 message with a delivery and three reply parts: two in order and a late one."""
        with dispatch.transaction(self.state) as items:
            items.append(Dispatch(id="b001", from_name="alpha-pm", from_profile="work", from_agent="claude",
                                  to_name="beta-dev", to_agent="claude", to_profile="work", text="新的一則",
                                  state="done", created="2026-11-12T00:00:00+00:00", sent_at="2026-11-12T00:00:05+00:00",
                                  done_at="2026-11-12T00:09:00+00:00", reply="第二段", to_id="s-beta", status="replied"))
            c = items.conn
            c.execute("INSERT INTO deliveries (id, kind, to_id, text, created, sent_at, state) VALUES "
                      "('dnew01','inbox','s-beta','新的一則','2026-11-12T00:00:05+00:00','2026-11-12T00:01:00+00:00','ended')")
            c.execute("INSERT INTO delivery_messages (delivery, message, position) VALUES ('dnew01','b001',0)")
            c.execute("INSERT INTO replies (id, delivery, first_at, source) VALUES ('rnew01','dnew01','x','tool')")
            for pid, text, at, late in (("p00000001", "第一段", "2026-11-12T00:05:00+00:00", 0),
                                        ("p00000002", "第二段", "2026-11-12T00:08:00+00:00", 0),
                                        ("p00000003", "遲到的", "2026-11-12T00:20:00+00:00", 1)):
                c.execute("INSERT INTO reply_parts (id, reply, message, text, how, at, late) VALUES (?,?,?,?,?,?,?)",
                          (pid, "rnew01", "b001", text, "tool", at, late))

    def test_revert_roundtrip(self):
        migrate_017.upgrade(self.cfg, now=NOW)
        self.add_new_message()
        lines = migrate_017.revert(self.cfg, now=LATER)
        self.assertIn(REINSTALL, "\n".join(lines))
        self.assertIn("7 天", "\n".join(lines))
        recs = {r["id"]: r for r in read_016(self.state)}  # 0.16 reads it
        self.assertEqual(self.json_doc()["version"], 2)
        self.assertEqual(set(recs), set(self.old) | {"b001"})
        for did, old in self.old.items():
            self.assertEqual({k: recs[did][k] for k in old}, old, did)
        new = recs["b001"]
        self.assertEqual((new["state"], new["reply"], new["sent_at"]), ("done", "第二段", "2026-11-12T00:01:00+00:00"))
        for r in recs.values():
            self.assertFalse({"status", "in_reply_to", "thread", "extra", "text_len"} & set(r), r["id"])
        self.assertTrue(migrate_017.reverted(self.cfg))
        self.assertFalse(migrate_017.needed(self.cfg))
        self.assertFalse((self.state / store.DB_FILE).exists())
        [renamed] = [p for p in self.state.iterdir() if p.name.startswith("dispatches.db")]
        self.assertEqual(renamed.name, "dispatches.db.reverted-20261112-100000")
        conn = sqlite3.connect(renamed)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 13)  # nothing lost from it
        conn.close()

    def test_revert_drops_purged_messages_and_uses_cached_reply_without_parts(self):
        migrate_017.upgrade(self.cfg, now=NOW)
        conn = store.connect(self.state)
        conn.execute("UPDATE messages SET text=NULL, reply=NULL, purged=1 WHERE id='a006'")
        conn.execute("DELETE FROM reply_parts WHERE message='a004'")
        conn.commit()
        conn.close()
        migrate_017.revert(self.cfg, now=LATER)
        recs = {r["id"]: r for r in read_016(self.state)}
        self.assertNotIn("a006", recs)
        self.assertEqual(recs["a004"]["reply"], self.old["a004"]["reply"])

    def test_save_after_revert_does_not_migrate(self):
        migrate_017.upgrade(self.cfg, now=NOW)
        migrate_017.revert(self.cfg, now=LATER)
        doc = self.json_doc()
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.runner.run", FakeRunner()), redirect_stdout(io.StringIO()) as buf:
            rc = cli.main(["save"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertNotIn("已降回", buf.getvalue())
        self.assertFalse((self.state / store.DB_FILE).exists())
        self.assertFalse((self.state / migrate_017.JOURNAL).exists())
        self.assertEqual(self.json_doc(), doc)
        self.assertTrue(migrate_017.reverted(self.cfg))

    def test_send_after_revert_refuses(self):
        migrate_017.revert(self.cfg, now=LATER)  # json still v2 (never migrated): the mark alone stops it
        self.assertFalse((self.state / store.DB_FILE).exists())
        cases = (["send", "beta-dev", "hi"], ["send-batch", "beta-dev", "-m", "hi"], ["dispatches"],
                 ["new", "x", "claude:work", "/tmp", "--message", "hi"])
        for argv in cases:
            with self.subTest(argv=argv), mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                    redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(cli.main(argv), 2)
            self.assertIn("已降回 0.16 格式；要再升級請跑 sampan migrate", buf.getvalue())
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.main(["mcp", "serve"]), 2)
        self.assertIn("已降回 0.16 格式", err.getvalue())
        self.assertEqual((self.state / "dispatches.json").read_bytes(), self.original)
        self.assertFalse((self.state / store.DB_FILE).exists())

    def test_migrate_again_after_revert(self):
        migrate_017.upgrade(self.cfg, now=NOW)
        self.add_new_message()
        migrate_017.revert(self.cfg, now=LATER)
        rc, text = self.cmd(now=LATER)
        self.assertEqual(rc, 0, text)
        self.assertFalse(migrate_017.reverted(self.cfg))
        self.assert_migrated(extra_ids={"b001"})
        [(reply,)] = self.sql("SELECT text FROM reply_parts WHERE message='b001'")
        self.assertEqual(reply, "第二段")

    def test_revert_before_any_migration_marks_only(self):
        lines = migrate_017.revert(self.cfg, now=NOW)
        self.assertIn(REINSTALL, "\n".join(lines))
        self.assertTrue(migrate_017.reverted(self.cfg))
        self.assertEqual((self.state / "dispatches.json").read_bytes(), self.original)
        self.assertEqual(self.ensure(LATER), (0, ""))  # save, status…: nothing, no message
        self.assertFalse((self.state / store.DB_FILE).exists())

    def test_revert_cut_short_finishes_on_the_next_revert(self):
        migrate_017.upgrade(self.cfg, now=NOW)
        with mock.patch.object(migrate_017, "_retire_db", side_effect=Killed), self.assertRaises(Killed):
            migrate_017.revert(self.cfg, now=LATER)
        self.assertTrue(migrate_017.reverted(self.cfg))  # marked first: nothing migrates it back meanwhile
        self.assertEqual(self.ensure(LATER), (0, ""))
        self.assertEqual(self.ensure(LATER, touches_dispatches=True)[0], 2)
        rc, text = self.cmd(now=LATER)  # sampan migrate does not guess which copy is newer
        self.assertEqual(rc, 2)
        self.assertIn("--revert", text)
        self.assertTrue((self.state / store.DB_FILE).exists())
        rc, text = self.cmd(now=LATER, revert=True)
        self.assertEqual(rc, 0, text)
        self.assertEqual(set(r["id"] for r in read_016(self.state)), set(self.old))
        self.assertFalse((self.state / store.DB_FILE).exists())

    def test_revert_mid_upgrade_cleans_the_journal(self):
        with mock.patch.object(migrate_017, "_write_stub", side_effect=Killed), self.assertRaises(Killed):
            migrate_017.upgrade(self.cfg, now=NOW)
        migrate_017.revert(self.cfg, now=LATER)
        self.assertFalse((self.state / migrate_017.JOURNAL).exists())
        self.assertEqual({r["id"] for r in read_016(self.state)}, set(self.old))
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith("dispatches.db")],
                         ["dispatches.db.reverted-20261112-100000"])

    def test_revert_command(self):
        migrate_017.upgrade(self.cfg, now=NOW)
        rc, text = self.cmd(revert=True, now=LATER)
        self.assertEqual(rc, 0, text)
        self.assertIn(REINSTALL, text)
        self.assertIn("12", text)
        self.assertEqual(self.json_doc()["version"], 2)


if __name__ == "__main__":
    unittest.main()
