import dataclasses
import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan import dispatch
from sampan.dispatch import Dispatch, DispatchError

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def mk(did="ab12", state="queued", created=None, **kw):
    return Dispatch(id=did, from_name="a", from_profile="p", from_agent="claude", to_name="b", to_agent="codex",
                    to_profile="q", text="hi", state=state, created=created or NOW.isoformat(timespec="seconds"), **kw)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "state"

    def test_missing_file_is_empty(self):
        self.assertEqual(dispatch.load(self.dir), [])

    def test_round_trip_and_permissions(self):
        items = [mk("aaaa", reply="x", notify=True, thread_id="t1"), mk("bbbb", "done", sent_at=ago(1), done_at=ago(0))]
        with dispatch.transaction(self.dir) as cur:
            cur.extend(items)
        self.assertEqual(dispatch.load(self.dir), items)
        self.assertEqual(json.loads((self.dir / "dispatches.json").read_text())["version"], 1)
        self.assertEqual(stat.S_IMODE((self.dir / "dispatches.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.dir / "dispatches.lock").stat().st_mode), 0o600)

    def test_from_kind_round_trips_and_is_inferred_for_old_records(self):
        items = [mk("aaaa", from_kind="unregistered"), mk("bbbb")]
        with dispatch.transaction(self.dir) as cur:
            cur.extend(items)
        self.assertEqual([d.from_kind for d in dispatch.load(self.dir)], ["unregistered", "session"])
        old = {k: v for k, v in dataclasses.asdict(mk("cccc")).items() if k != "from_kind"}
        term = dict(old, id="dddd", from_name="終端機", from_profile="")
        (self.dir / "dispatches.json").write_text(json.dumps({"version": 1, "dispatches": [old, term]}))
        self.assertEqual([d.from_kind for d in dispatch.load(self.dir)], ["session", "terminal"])

    def test_bad_from_kind_raises(self):
        self.dir.mkdir(parents=True)
        rec = dict(dataclasses.asdict(mk()), from_kind="admin")
        (self.dir / "dispatches.json").write_text(json.dumps({"version": 1, "dispatches": [rec]}))
        with self.assertRaises(DispatchError):
            dispatch.load(self.dir)

    def test_bad_json_raises_and_keeps_file(self):
        self.dir.mkdir(parents=True)
        path = self.dir / "dispatches.json"
        path.write_text("{not json")
        with self.assertRaises(DispatchError):
            dispatch.load(self.dir)
        with self.assertRaises(DispatchError):
            with dispatch.transaction(self.dir) as cur:
                cur.append(mk())
        self.assertEqual(path.read_text(), "{not json")

    def test_bad_shape_and_state_raise(self):
        self.dir.mkdir(parents=True)
        path = self.dir / "dispatches.json"
        for text in ('{"version": 2, "dispatches": []}', '{"version": 1}', '{"version": 1, "dispatches": [{"id": "x"}]}'):
            path.write_text(text)
            with self.assertRaises(DispatchError):
                dispatch.load(self.dir)
        d = mk()
        d.state = "weird"
        path.write_text(json.dumps({"version": 1, "dispatches": [d.__dict__]}))
        with self.assertRaises(DispatchError):
            dispatch.load(self.dir)

    def test_unknown_fields_are_ignored_and_startup_defaults_off(self):  # forward compatibility
        self.dir.mkdir(parents=True)
        rec = {k: v for k, v in dataclasses.asdict(mk()).items() if k != "startup"}
        rec["added_by_a_newer_sampan"] = 1
        (self.dir / "dispatches.json").write_text(json.dumps({"version": 1, "dispatches": [rec]}))
        [d] = dispatch.load(self.dir)
        self.assertEqual((d.id, d.startup), ("ab12", False))
        (self.dir / "dispatches.json").write_text(json.dumps({"version": 1, "dispatches": ["nope"]}))
        with self.assertRaises(DispatchError):
            dispatch.load(self.dir)

    def test_checkpoint_writes_inside_the_transaction_and_survives_an_exception(self):
        with dispatch.transaction(self.dir) as cur:
            cur.append(mk("aaaa"))
        with self.assertRaises(KeyboardInterrupt):
            with dispatch.transaction(self.dir) as cur:
                cur[0].state = "sent"
                dispatch.checkpoint(self.dir, cur)
                raise KeyboardInterrupt  # killed while delivering
        self.assertEqual(dispatch.load(self.dir)[0].state, "sent")

    def test_exception_does_not_write(self):
        with dispatch.transaction(self.dir) as cur:
            cur.append(mk("aaaa"))
        with self.assertRaises(RuntimeError):
            with dispatch.transaction(self.dir) as cur:
                cur.append(mk("bbbb"))
                raise RuntimeError("boom")
        self.assertEqual([d.id for d in dispatch.load(self.dir)], ["aaaa"])

    def test_second_transaction_sees_first(self):
        with dispatch.transaction(self.dir) as cur:
            cur.append(mk("aaaa"))
        with dispatch.transaction(self.dir) as cur:
            cur[0].state = "sent"
        with dispatch.transaction(self.dir) as cur:
            self.assertEqual(cur[0].state, "sent")
            cur[0].state = "done"
        self.assertEqual(dispatch.load(self.dir)[0].state, "done")


class PruneTest(unittest.TestCase):
    def test_old_finished_dropped_active_kept(self):
        items = [mk("a001", "done", created=ago(8)), mk("a002", "failed", created=ago(8)), mk("a003", "cancelled", created=ago(8)),
                 mk("a004", "done", created=ago(6)), mk("a005", "queued", created=ago(30)), mk("a006", "sent", created=ago(30))]
        kept = [d.id for d in dispatch.prune(items, NOW)]
        self.assertEqual(kept, ["a004", "a005", "a006"])

    def test_keeps_newest_200(self):
        items = [mk(f"{i:04x}", "queued", created=(NOW - timedelta(minutes=i)).isoformat(timespec="seconds")) for i in range(250)]
        kept = dispatch.prune(items, NOW)
        self.assertEqual(len(kept), 200)
        self.assertEqual(kept[0].id, "0000")
        self.assertEqual(kept[-1].id, f"{199:04x}")

    def test_unparseable_created_is_kept(self):
        self.assertEqual(len(dispatch.prune([mk(created="garbage", state="done")], NOW)), 1)


class IdAndOutboxTest(unittest.TestCase):
    def test_new_id_shape_and_avoids_existing(self):
        i = dispatch.new_id(set())
        self.assertRegex(i, r"^[0-9a-f]{4}$")
        existing = {f"{n:04x}" for n in range(0x10000) if n != 0xbeef}
        self.assertEqual(dispatch.new_id(existing), "beef")

    def test_outbox(self):
        with tempfile.TemporaryDirectory() as t:
            sd = Path(t) / "state"
            self.assertEqual(dispatch.outbox_path(sd, "ab12"), sd / "outbox" / "ab12.txt")
            p = dispatch.write_outbox(sd, "ab12", "多行\n文字")
            self.assertEqual(p.read_text(), "多行\n文字")
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
