import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sampan.registry import Observed, RegistryError, Row, load_machine, load_rows, lock, merge, set_state, write_rows

NOW = "2026-10-17T12:00:00+08:00"
SRV = "4242-1792197209"
OLD_SRV = "1111-1700000000"
A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
I1 = "11111111-0000-4000-8000-0000000000a1"
I2 = "22222222-0000-4000-8000-0000000000a2"


class MergeTest(unittest.TestCase):
    def test_add_new(self):
        r = merge([], [Observed("001-proj", "claude", "work", A, "/c", 1)], {"001-proj"}, NOW, SRV)
        self.assertEqual(r.added, ["001-proj"])
        self.assertEqual((r.rows[0].state, r.rows[0].seen_at, r.rows[0].server), ("active", NOW, SRV))

    def test_update_keeps_state(self):
        old = [Row("x", "claude", "alice", A, "/c", "retired", "old", SRV)]
        r = merge(old, [Observed("x", "claude", "alice", B, "/c", 1)], {"x"}, NOW, SRV)
        self.assertEqual(r.updated, ["x"])
        self.assertEqual(r.rows[0].session_id, B)
        self.assertEqual(r.rows[0].state, "retired")

    def test_unchanged_is_not_counted_as_update(self):
        old = [Row("x", "claude", "alice", A, "/c", "active", "old", SRV)]
        r = merge(old, [Observed("x", "claude", "alice", A, "/c", 1)], {"x"}, NOW, SRV)
        self.assertEqual(r.updated, [])
        self.assertEqual(r.rows[0].seen_at, NOW)

    def test_tmux_exists_without_claude_keeps_row_and_warns(self):
        # Review Focus 2: after boot, continuum restores only a shell; the row must not be deleted
        old = [Row("x", "claude", "alice", A, "/c", "active", "old", SRV), Row("y", "claude", "work", B, "/d", "active", "old", SRV)]
        r = merge(old, [], {"x", "y"}, NOW, SRV)
        self.assertEqual([row.tmux for row in r.rows], ["x", "y"])
        self.assertEqual(r.rows[0].seen_at, "old")
        self.assertEqual(len(r.warnings), 2)

    def test_gone_in_same_server_is_kept_as_stopped(self):
        # Regression: sessions that vanished during the same tmux server's lifetime were once treated as closed by the user and deleted, so stopped sessions disappeared from the list
        old = [Row("x", "claude", "alice", A, "/c", "active", "", SRV), Row("y", "claude", "alice", B, "/d", "retired", "", SRV)]
        r = merge(old, [], set(), NOW, SRV)
        self.assertEqual(r.pending, ["x"])
        self.assertEqual([row.tmux for row in r.rows], ["x", "y"])

    def test_gone_but_seen_only_in_older_server_is_pending_not_removed(self):
        # Review R1: tmux was started first after a reboot; the auto-save must not delete rows that haven't been reopened yet
        old = [Row("x", "claude", "alice", A, "/c", "active", "old", OLD_SRV), Row("legacy", "claude", "alice", B, "/c", "active", "old", "")]
        r = merge(old, [], set(), NOW, SRV)
        self.assertEqual(r.pending, ["legacy", "x"])
        self.assertEqual([row.tmux for row in r.rows], ["legacy", "x"])

    def test_rename_carries_state(self):
        old = [Row("old-name", "claude", "alice", A, "/c", "retired", "", SRV)]
        r = merge(old, [Observed("new-name", "claude", "alice", A, "/c", 1)], {"new-name"}, NOW, SRV)
        self.assertEqual(r.renamed, [("old-name", "new-name")])
        self.assertEqual([(row.tmux, row.state) for row in r.rows], [("new-name", "retired")])

    def test_same_uuid_but_old_name_still_exists_is_add_not_rename(self):
        old = [Row("a", "claude", "alice", A, "/c", "active", "", SRV)]
        r = merge(old, [Observed("b", "claude", "alice", A, "/c", 1)], {"a", "b"}, NOW, SRV)
        self.assertEqual(r.added, ["b"])
        self.assertEqual(sorted(row.tmux for row in r.rows), ["a", "b"])

    def test_kept_row_warning_names_its_agent(self):
        old = [Row("x", "codex", "", A, "/c", "active", "old", SRV), Row("y", "claude", "work", B, "/d", "active", "old", SRV)]
        r = merge(old, [], {"x", "y"}, NOW, SRV)
        self.assertEqual(r.warnings, [f"x：沒有 codex，沿用上次的對話 {A[:8]}", f"y：沒有 claude，沿用上次的對話 {B[:8]}"])

    def test_keep_silently_keeps_row_without_warning(self):
        # sessions skipped because of multiple agents (they already have their own warning), and a Codex with no first prompt yet
        old = [Row("x", "codex", "", A, "/c", "active", "old", SRV)]
        r = merge(old, [], {"x"}, NOW, SRV, keep_silently={"x"})
        self.assertEqual(([row.tmux for row in r.rows], r.warnings), (["x"], []))

    def test_merge_tracks_agent_change(self):
        old = [Row("x", "claude", "alice", A, "/c", "active", "old", SRV)]
        r = merge(old, [Observed("x", "codex", "", A, "/c", 7)], {"x"}, NOW, SRV)
        self.assertEqual(r.updated, ["x"])
        self.assertEqual((r.rows[0].agent, r.rows[0].profile, r.rows[0].pid), ("codex", "", 7))

    def test_pid_change_alone_is_not_update_but_is_stored(self):
        old = [Row("x", "claude", "alice", A, "/c", "active", "old", SRV, 5)]
        r = merge(old, [Observed("x", "claude", "alice", A, "/c", 9)], {"x"}, NOW, SRV)
        self.assertEqual(r.updated, [])
        self.assertEqual(r.rows[0].pid, 9)

    def test_rows_sorted_by_name(self):
        obs = [Observed("b", "claude", "alice", A, "/c", 1), Observed("a", "claude", "alice", B, "/c", 1)]
        self.assertEqual([row.tmux for row in merge([], obs, {"a", "b"}, NOW, SRV).rows], ["a", "b"])


class MergeIdTest(unittest.TestCase):
    def test_merge_new_row_gets_id(self):
        r = merge([], [Observed("x", "claude", "work", A, "/c", 1)], {"x"}, NOW, SRV)
        self.assertEqual(len(r.rows[0].id), 36)

    def test_merge_update_keeps_id(self):
        old = [Row("x", "claude", "alice", A, "/c", "active", "old", SRV, None, I1)]
        r = merge(old, [Observed("x", "claude", "alice", B, "/c", 1)], {"x"}, NOW, SRV)
        self.assertEqual(r.rows[0].id, I1)
        r = merge(old, [], {"x"}, NOW, SRV)  # kept as is
        self.assertEqual(r.rows[0].id, I1)

    def test_merge_rename_by_session_id_keeps_id(self):
        old = [Row("old-name", "claude", "alice", A, "/c", "active", "", SRV, None, I1)]
        r = merge(old, [Observed("new-name", "claude", "alice", A, "/c", 1)], {"new-name"}, NOW, SRV)
        self.assertEqual((r.renamed, r.rows[0].id), ([("old-name", "new-name")], I1))

    def test_merge_rename_by_sampan_id(self):
        # the conversation id changed as well as the name: only the stamped id recognises the row
        old = [Row("old-name", "claude", "alice", A, "/c", "active", "", SRV, None, I1),
               Row("other", "claude", "alice", B, "/c", "active", "", SRV, None, I2)]
        r = merge(old, [Observed("new-name", "claude", "alice", "cccccccc-0000-4000-8000-000000000003", "/c", 1, I1)],
                  {"new-name", "other"}, NOW, SRV)
        self.assertEqual((r.renamed, r.added), ([("old-name", "new-name")], []))
        self.assertEqual({x.tmux: x.id for x in r.rows}, {"new-name": I1, "other": I2})

    def test_unknown_sampan_id_falls_back_to_session_id_rule(self):
        old = [Row("old-name", "claude", "alice", A, "/c", "active", "", SRV, None, I1)]
        r = merge(old, [Observed("new-name", "claude", "alice", A, "/c", 1, I2)], {"new-name"}, NOW, SRV)
        self.assertEqual(r.rows[0].id, I1)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "state/registry.json"
        self.hist = self.tmp / "state/history"

    def test_old_codex_rows_read_as_codex_profile(self):
        write_rows(self.path, [Row("c", "codex", "", B, "/w"), Row("a", "claude", "work", A, "/w")], "t", self.hist)
        rows = {r.tmux: r for r in load_rows(self.path, codex_profile="cx")}
        self.assertEqual((rows["c"].profile, rows["a"].profile), ("cx", "work"))
        self.assertEqual(load_rows(self.path)[0].profile, "")
    def test_missing_file_is_empty(self):
        self.assertEqual(load_rows(self.path), [])

    def test_round_trip_and_unicode(self):
        rows = [Row("003-帳務", "claude", "work", A, "/Users/u/proj", "active", NOW, SRV)]
        write_rows(self.path, rows, NOW, self.hist)
        self.assertEqual(load_rows(self.path), rows)
        self.assertIn("帳務", self.path.read_text())
        self.assertEqual(json.loads(self.path.read_text())["saved_at"], NOW)

    def test_rows_without_server_field_load(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 1, "sessions": [
            {"tmux": "x", "profile": "alice", "session_id": A, "cwd": "/c", "state": "active", "seen_at": ""}]}))
        self.assertEqual(load_rows(self.path)[0].server, "")

    def test_loads_v1_as_claude(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 1, "sessions": [
            {"tmux": "x", "profile": "alice", "session_id": A, "cwd": "/c", "state": "active", "seen_at": ""}]}))
        rows = load_rows(self.path)
        self.assertEqual((rows[0].agent, rows[0].pid), ("claude", None))
        write_rows(self.path, rows, NOW, self.hist)
        self.assertEqual(json.loads(self.path.read_text())["version"], 3)
        self.assertEqual(load_rows(self.path), rows)

    def test_v2_round_trips_codex_and_pid(self):
        rows = [Row("c", "codex", "", A, "/c", "active", NOW, SRV, 123)]
        write_rows(self.path, rows, NOW, self.hist)
        self.assertEqual(load_rows(self.path), rows)

    def test_registry_written_by_v0_5_3_reads_and_rewrites_with_the_same_values(self):
        # tests/fixtures/registry_v0.5.3.json is hand-written in the shape v0.5.3 wrote (local times with a +08:00 offset): active (claude, codex),
        # retired and stopped rows, a non-ASCII name, null pid. The State enum is a display status
        # (status.py) and must not leak into the persisted active/retired values. Since version 3 the rewrite adds
        # the header "machine" and a row "id"; every other value, and the order, stays as it was.
        fixture = Path(__file__).resolve().parent / "fixtures" / "registry_v0.5.3.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(fixture.read_bytes())
        rows = load_rows(self.path)
        self.assertEqual([(r.tmux, r.state) for r in rows], [("003-帳務", "active"), ("api", "active"),
                                                              ("old-experiment", "retired"), ("stopped-one", "active")])
        data = json.loads(fixture.read_text())

        def check():
            out = json.loads(self.path.read_text())
            self.assertEqual((out["version"], out["machine"], out["saved_at"]), (3, "m", data["saved_at"]))
            for old, new in zip(data["sessions"], out["sessions"]):
                self.assertEqual({k: v for k, v in new.items() if k != "id"}, old)
                self.assertTrue(new["id"])
        write_rows(self.path, rows, data["saved_at"], self.hist, machine="m")
        check()
        # retire then activate through set_state lands on the same values, with the same ids
        ids_before = [r.id for r in rows]
        self.assertTrue(set_state(rows, "api", "retired"))
        self.assertTrue(set_state(rows, "api", "active"))
        write_rows(self.path, rows, data["saved_at"], self.hist, machine="m")
        check()
        self.assertEqual([r.id for r in load_rows(self.path)], ids_before)

    def test_v1_registry_loads_and_rewrites_with_the_same_values(self):
        body = {"version": 1, "saved_at": NOW, "sessions": [
            {"tmux": "x", "profile": "alice", "session_id": A, "cwd": "/c", "state": "active", "seen_at": NOW, "server": SRV},
            {"tmux": "gone", "profile": "work", "session_id": B, "cwd": "/g", "state": "retired", "seen_at": NOW}]}
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(body))
        rows = load_rows(self.path)
        write_rows(self.path, rows, NOW, self.hist)
        out = json.loads(self.path.read_text())
        self.assertEqual(out["version"], 3)
        for old, new in zip(body["sessions"], out["sessions"]):
            self.assertEqual({k: new[k] for k in old}, old)  # every v1 value survives unchanged
            self.assertEqual((new["agent"], new["pid"]), ("claude", None))

    def test_v2_file_loads_with_empty_ids(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 2, "saved_at": NOW, "sessions": [
            {"tmux": "x", "agent": "claude", "profile": "a", "session_id": A, "cwd": "/c"}]}))
        self.assertEqual(load_rows(self.path)[0].id, "")
        self.assertEqual(load_machine(self.path), "")

    def test_write_fills_missing_ids_and_keeps_existing(self):
        rows = [Row("a", "claude", "p", A, "/c"), Row("b", "claude", "p", B, "/c", id=I2)]
        write_rows(self.path, rows, NOW, self.hist)
        self.assertEqual(len(rows[0].id), 36)  # filled in the passed object
        self.assertEqual(rows[1].id, I2)
        again = load_rows(self.path)
        self.assertEqual([r.id for r in again], [rows[0].id, I2])
        write_rows(self.path, again, NOW, self.hist)
        self.assertEqual([r.id for r in load_rows(self.path)], [rows[0].id, I2])

    def test_write_is_version_3_with_machine(self):
        write_rows(self.path, [Row("a", "claude", "p", A, "/c")], NOW, self.hist, machine="mach-1")
        body = json.loads(self.path.read_text())
        self.assertEqual((body["version"], body["machine"]), (3, "mach-1"))
        self.assertEqual(load_machine(self.path), "mach-1")

    def test_v4_refused(self):
        self.test_rejects_unknown_version()

    def test_rejects_unknown_version(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 4, "sessions": []}))
        with self.assertRaises(RegistryError):
            load_rows(self.path)

    def test_rejects_bad_agent(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 2, "sessions": [
            {"tmux": "x", "agent": "gemini", "profile": "", "session_id": A, "cwd": "/c"}]}))
        with self.assertRaisesRegex(RegistryError, "x"):
            load_rows(self.path)

    def test_rejects_non_int_pid(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 2, "sessions": [
            {"tmux": "x", "agent": "claude", "profile": "a", "session_id": A, "cwd": "/c", "pid": "12"}]}))
        with self.assertRaisesRegex(RegistryError, "x"):
            load_rows(self.path)

    def test_rejects_non_str_session_id(self):
        self.path.parent.mkdir(parents=True)
        for bad in (None, 12, ["x"]):
            self.path.write_text(json.dumps({"version": 2, "sessions": [
                {"tmux": "x", "agent": "claude", "profile": "a", "session_id": bad, "cwd": "/c"}]}))
            with self.assertRaisesRegex(RegistryError, "session_id"):
                load_rows(self.path)

    def test_corrupt_file_raises(self):
        # Review Focus 4
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ broken")
        with self.assertRaises(RegistryError):
            load_rows(self.path)
        self.path.write_text(json.dumps({"version": 1, "sessions": [{"tmux": "x"}]}))
        with self.assertRaises(RegistryError):
            load_rows(self.path)

    def test_history_keeps_last_n(self):
        for i in range(5):
            write_rows(self.path, [Row(f"s{i}", "claude", "alice", A, "/c")], NOW, self.hist, keep=3)
        files = sorted(self.hist.iterdir())
        self.assertEqual(len(files), 3)
        self.assertEqual(json.loads(files[-1].read_text())["sessions"][0]["tmux"], "s3")
        self.assertFalse(any(".tmp" in p.name for p in self.path.parent.iterdir()))

    def test_history_snapshot_skipped_when_only_seen_at_changes(self):
        write_rows(self.path, [Row("s", "claude", "alice", A, "/c", seen_at="t1", pid=1, server="a", id=I1)], NOW, self.hist)
        self.assertFalse(self.hist.exists() and list(self.hist.iterdir()))  # no old file: no snapshot
        write_rows(self.path, [Row("s", "claude", "alice", A, "/c", seen_at="t2", pid=2, server="b", id=I1)], NOW, self.hist)
        self.assertFalse(self.hist.exists() and list(self.hist.iterdir()))
        self.assertEqual(json.loads(self.path.read_text())["sessions"][0]["seen_at"], "t2")

    def test_history_snapshot_kept_when_session_id_changes(self):
        write_rows(self.path, [Row("s", "claude", "alice", A, "/c", seen_at="t1")], NOW, self.hist)
        write_rows(self.path, [Row("s", "claude", "alice", "other-id", "/c", seen_at="t2")], NOW, self.hist)
        self.assertEqual(len(list(self.hist.iterdir())), 1)

    def test_history_pruning_ignores_name_order_across_timezone_change(self):
        """Old snapshots are named in local time (+8h), new ones in UTC: names then sort lower despite being newer."""
        import os
        write_rows(self.path, [Row("s", "claude", "alice", A, "/c")], NOW, self.hist, keep=20)  # creates registry
        self.hist.mkdir(parents=True, exist_ok=True)
        old = []
        for i in range(20):
            p = self.hist / f"registry-29991231-2300{i:02d}-000000.json"  # sorts after any real UTC stamp
            p.write_text("{}")
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
            old.append(p)
        write_rows(self.path, [Row("s2", "claude", "alice", A, "/c")], NOW, self.hist, keep=20)
        left = list(self.hist.glob("registry-*.json"))
        self.assertEqual(len(left), 20)
        self.assertFalse(old[0].exists())  # the oldest by time goes, not the newest by time
        self.assertTrue(any(not p.name.startswith("registry-2999") for p in left))  # fresh snapshot survived

    def test_set_state_rejects_values_outside_states(self):
        rows = [Row("x", "claude", "alice", A, "/c")]
        for bad in ("bogus", "執行中", "", None, "Retired"):
            with self.assertRaises(ValueError):
                set_state(rows, "x", bad)
        self.assertEqual(rows[0].state, "active")

    def test_set_state(self):
        rows = [Row("x", "claude", "alice", A, "/c")]
        self.assertTrue(set_state(rows, "x", "retired"))
        self.assertEqual(rows[0].state, "retired")
        self.assertFalse(set_state(rows, "nope", "retired"))

    def test_lock_serialises(self):
        # Review R7
        state = self.tmp / "state"
        order = []
        held = threading.Event()

        def holder():
            with lock(state):
                held.set()
                time.sleep(0.3)
                order.append("holder done")

        t = threading.Thread(target=holder)
        t.start()
        held.wait()
        with lock(state):
            order.append("second")
        t.join()
        self.assertEqual(order, ["holder done", "second"])
