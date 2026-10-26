import json
import stat
import tempfile
import unittest
from pathlib import Path

from sampan import folders
from sampan.folders import FoldersError, Tree


def mk(folder_paths=(), sessions=None, collapsed=()):
    return Tree({p: p in collapsed for p in folder_paths}, dict(sessions or {}))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "state"

    def put(self, text):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / folders.FOLDERS_FILE).write_text(text)

    def test_missing_file_is_empty_tree(self):
        self.assertEqual(folders.load(self.dir), Tree({}, {}))

    def test_save_load_round_trip_and_mode(self):
        tree = Tree({"客戶": False, "客戶/甲": True}, {"a1": "客戶/甲"})
        folders.save(self.dir, tree)
        self.assertEqual(folders.load(self.dir), tree)
        path = self.dir / folders.FOLDERS_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = json.loads(path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["folders"]["客戶/甲"], {"collapsed": True})
        self.assertIn("客戶", path.read_text())  # not escaped

    def assert_unreadable(self, text):
        self.put(text)
        with self.assertRaises(FoldersError) as cm:
            folders.load(self.dir)
        self.assertTrue(str(cm.exception).startswith("資料夾設定讀不到："))

    def test_load_bad_json(self):
        self.assert_unreadable("{nope")
        self.assert_unreadable("")

    def test_load_bad_version(self):
        self.assert_unreadable('{"version": 2, "folders": {}, "sessions": {}}')

    def test_load_bad_shape(self):
        self.assert_unreadable("[]")
        self.assert_unreadable('{"version": 1, "folders": [], "sessions": {}}')
        self.assert_unreadable('{"version": 1, "folders": {"a": true}, "sessions": {}}')
        self.assert_unreadable('{"version": 1, "folders": {"a": {"collapsed": "no"}}, "sessions": {}}')
        self.assert_unreadable('{"version": 1, "folders": {}, "sessions": {"a1": 3}}')

    def test_broken_file_is_not_overwritten_by_edit(self):
        self.put("{nope")
        with self.assertRaises(FoldersError):
            folders.edit(self.dir, lambda t: folders.add(t, "x"))
        self.assertEqual((self.dir / folders.FOLDERS_FILE).read_text(), "{nope")

    def test_edit_saves_and_returns(self):
        self.assertEqual(folders.edit(self.dir, lambda t: (folders.add(t, "客戶/甲"), 7)[1]), 7)
        self.assertEqual(set(folders.load(self.dir).folders), {"客戶", "客戶/甲"})

    def test_edit_does_not_write_on_error(self):
        with self.assertRaises(FoldersError):
            folders.edit(self.dir, lambda t: (folders.add(t, "x"), (_ for _ in ()).throw(FoldersError("no"))))
        self.assertEqual(folders.load(self.dir), Tree({}, {}))


class ResolveTest(unittest.TestCase):
    def test_resolve_drops_unknown_sessions_and_adds_parents(self):
        tree = Tree({}, {"a1": "客戶/甲", "gone": "開發"})
        out = folders.resolve(tree, {"a1", "b1"})
        self.assertEqual(out.sessions, {"a1": "客戶/甲"})
        self.assertIn("客戶", out.folders)
        self.assertIn("客戶/甲", out.folders)
        self.assertFalse(out.folders["客戶"])
        self.assertEqual(tree, Tree({}, {"a1": "客戶/甲", "gone": "開發"}))

    def test_resolve_adds_missing_parent_of_folder_and_keeps_collapsed(self):
        out = folders.resolve(mk(["客戶/甲"], collapsed=["客戶/甲"]), [])
        self.assertEqual(out.folders, {"客戶": False, "客戶/甲": True})


class PathTest(unittest.TestCase):
    def test_name_problem(self):
        for bad in ("", "  ", "a/b", "a\nb", "a\x00b"):
            self.assertTrue(folders.name_problem(bad), repr(bad))
        self.assertIsNone(folders.name_problem("甲 乙"))

    def test_path_helpers(self):
        self.assertEqual(folders.parent("客戶/甲"), "客戶")
        self.assertEqual(folders.parent("客戶"), "")
        self.assertEqual(folders.basename("客戶/甲"), "甲")
        self.assertEqual(folders.join("", "甲"), "甲")
        self.assertEqual(folders.join("客戶", "甲"), "客戶/甲")


class OpsTest(unittest.TestCase):
    def test_add_creates_every_level_and_is_idempotent(self):
        t = mk()
        folders.add(t, "客戶/甲")
        folders.add(t, "客戶/甲")
        self.assertEqual(t.folders, {"客戶": False, "客戶/甲": False})

    def test_add_refuses_bad_segment(self):
        t = mk()
        with self.assertRaises(FoldersError):
            folders.add(t, "客戶//甲")
        self.assertEqual(t, mk())

    def test_rename_moves_children_and_sessions(self):
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲", "b1": "客戶"}, collapsed=["客戶/甲"])
        self.assertEqual(folders.rename(t, "客戶", "開發"), "開發")
        self.assertEqual(t.folders, {"開發": False, "開發/甲": True})
        self.assertEqual(t.sessions, {"a1": "開發/甲", "b1": "開發"})

    def test_rename_name_taken_refused(self):
        t = mk(["客戶", "開發"], {"a1": "客戶"})
        before = Tree(dict(t.folders), dict(t.sessions))
        with self.assertRaises(FoldersError) as cm:
            folders.rename(t, "客戶", "開發")
        self.assertEqual(str(cm.exception), "同一層已經有「開發」")
        self.assertEqual(t, before)

    def test_rename_merge(self):
        t = mk(["客戶", "客戶/甲", "開發"], {"a1": "客戶/甲", "b1": "客戶", "c1": "開發"},
               collapsed=["開發"])
        self.assertEqual(folders.rename(t, "客戶", "開發", merge=True), "開發")
        self.assertEqual(t.folders, {"開發": True, "開發/甲": False})
        self.assertEqual(t.sessions, {"a1": "開發/甲", "b1": "開發", "c1": "開發"})

    def test_rename_bad_name_or_missing_folder(self):
        t = mk(["客戶"])
        with self.assertRaises(FoldersError):
            folders.rename(t, "客戶", "a/b")
        with self.assertRaises(FoldersError):
            folders.rename(t, "沒有", "x")

    def test_remove_moves_children_up_one_level(self):
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲", "b1": "客戶"})
        folders.remove(t, "客戶")
        self.assertEqual(t.folders, {"甲": False})
        self.assertEqual(t.sessions, {"a1": "甲"})

    def test_remove_merges_same_named_child_into_parent_sibling(self):
        t = mk(["客戶", "客戶/甲", "甲", "客戶/甲/乙"], {"a1": "客戶/甲/乙", "b1": "甲"})
        folders.remove(t, "客戶")
        self.assertEqual(t.folders, {"甲": False, "甲/乙": False})
        self.assertEqual(t.sessions, {"a1": "甲/乙", "b1": "甲"})

    def test_remove_missing_refused(self):
        with self.assertRaises(FoldersError):
            folders.remove(mk(), "客戶")

    def test_move_into_own_descendant_refused(self):
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲"})
        before = Tree(dict(t.folders), dict(t.sessions))
        for dest in ("客戶/甲", "客戶"):
            with self.assertRaises(FoldersError) as cm:
                folders.move(t, [("folder", "客戶")], dest)
            self.assertEqual(str(cm.exception), "不能把資料夾搬進它自己裡面")
        self.assertEqual(t, before)

    def test_move_to_top(self):
        t = mk(["客戶"], {"a1": "客戶"})
        folders.move(t, [("session", "a1")], "")
        self.assertNotIn("a1", t.sessions)

    def test_move_folder_and_session_into_new_dest(self):
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲", "b1": ""})
        t.sessions.pop("b1")
        folders.move(t, [("folder", "客戶/甲"), ("session", "b1")], "開發/乙")
        self.assertEqual(t.folders, {"客戶": False, "開發": False, "開發/乙": False, "開發/乙/甲": False})
        self.assertEqual(t.sessions, {"a1": "開發/乙/甲", "b1": "開發/乙"})

    def test_move_name_taken_refused_and_untouched(self):
        t = mk(["客戶", "開發", "開發/客戶"], {"a1": "客戶", "b1": "開發/客戶"})
        before = Tree(dict(t.folders), dict(t.sessions))
        with self.assertRaises(FoldersError):
            folders.move(t, [("session", "a1"), ("folder", "客戶")], "開發")
        self.assertEqual(t, before)

    def test_move_merge(self):
        t = mk(["客戶", "開發", "開發/客戶"], {"a1": "客戶", "b1": "開發/客戶"}, collapsed=["開發/客戶"])
        folders.move(t, [("folder", "客戶")], "開發", merge=True)
        self.assertEqual(t.folders, {"開發": False, "開發/客戶": True})
        self.assertEqual(t.sessions, {"a1": "開發/客戶", "b1": "開發/客戶"})

    def test_move_to_current_parent_is_noop(self):
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲"})
        before = Tree(dict(t.folders), dict(t.sessions))
        folders.move(t, [("folder", "客戶/甲")], "客戶")
        self.assertEqual(t, before)

    def test_group_at_deepest_common_parent(self):
        t = mk(["客戶", "客戶/甲", "客戶/乙"], {"a1": "客戶/甲", "a2": "客戶/乙"})
        path = folders.group(t, [("session", "a1"), ("session", "a2")], "合")
        self.assertEqual(path, "客戶/合")
        self.assertEqual(t.sessions, {"a1": "客戶/合", "a2": "客戶/合"})

    def test_group_at_top_when_nothing_in_common(self):
        t = mk(["客戶"], {"a1": "客戶"})
        path = folders.group(t, [("session", "a1"), ("session", "z9")], "合")
        self.assertEqual(path, "合")
        self.assertEqual(t.sessions, {"a1": "合", "z9": "合"})

    def test_group_folders_use_their_parent(self):
        t = mk(["客戶", "客戶/甲", "客戶/乙"])
        self.assertEqual(folders.group(t, [("folder", "客戶/甲"), ("folder", "客戶/乙")], "合"), "客戶/合")
        self.assertEqual(set(t.folders), {"客戶", "客戶/合", "客戶/合/甲", "客戶/合/乙"})

    def test_group_name_taken_refused_and_merge(self):
        t = mk(["客戶", "客戶/甲", "客戶/合"], {"a1": "客戶/甲", "b1": "客戶/合"})
        before = Tree(dict(t.folders), dict(t.sessions))
        with self.assertRaises(FoldersError):
            folders.group(t, [("session", "a1"), ("session", "b1")], "合")
        self.assertEqual(t, before)
        self.assertEqual(folders.group(t, [("session", "a1"), ("session", "b1")], "合", merge=True), "客戶/合")
        self.assertEqual(t.sessions, {"a1": "客戶/合", "b1": "客戶/合"})

    def test_group_bad_name_refused(self):
        with self.assertRaises(FoldersError):
            folders.group(mk(), [("session", "a1")], "a/b")

    def test_top_items_drops_contained(self):
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲", "b1": "開發"})
        items = [("folder", "客戶"), ("session", "a1"), ("folder", "客戶/甲"), ("session", "b1"), ("session", "b1")]
        self.assertEqual(folders.top_items(t, items), [("folder", "客戶"), ("session", "b1")])

    def test_set_collapsed(self):
        t = mk(["客戶"])
        folders.set_collapsed(t, "客戶", True)
        self.assertTrue(t.folders["客戶"])
        with self.assertRaises(FoldersError):
            folders.set_collapsed(t, "沒有", True)

    def test_session_helpers(self):
        t = mk()
        folders.place_session(t, "a1", "客戶/甲")
        self.assertEqual(t.sessions, {"a1": "客戶/甲"})
        self.assertIn("客戶/甲", t.folders)
        folders.rename_session(t, "a1", "a2")
        self.assertEqual(t.sessions, {"a2": "客戶/甲"})
        folders.rename_session(t, "nope", "x")
        self.assertEqual(folders.sessions_under(t, "客戶"), {"a2"})
        self.assertEqual(folders.sessions_under(t, "客"), set())
        folders.place_session(t, "a2", "")
        self.assertEqual(t.sessions, {})
        folders.place_session(t, "b1", "開發")
        folders.forget_session(t, "b1")
        folders.forget_session(t, "b1")
        self.assertEqual(t.sessions, {})


if __name__ == "__main__":
    unittest.main()
