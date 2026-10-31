import json
import stat
import tempfile
import unittest
from pathlib import Path

from sampan import folders, registry
from sampan.folders import FoldersError, Tree
from sampan.registry import Row


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

    def rows(self, *names, with_id=True):
        return [Row(n, "claude", "p", "c-" + n, "/c", id=("id-" + n) if with_id else "") for n in names]

    def write_registry(self, rows):
        self.dir.mkdir(parents=True, exist_ok=True)
        registry.write_rows(self.dir / "registry.json", rows, "t", self.dir / "history")
        return registry.load_rows(self.dir / "registry.json")

    def test_save_load_round_trip_and_mode(self):
        rows = self.rows("a1")
        tree = Tree({"客戶": False, "客戶/甲": True}, {"a1": "客戶/甲"})
        folders.save(self.dir, tree, rows)
        self.assertEqual(folders.load(self.dir, rows), tree)
        path = self.dir / folders.FOLDERS_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = json.loads(path.read_text())
        self.assertEqual(data["version"], 2)
        fid = tree.ids["客戶/甲"]
        self.assertEqual(data["folders"][fid], {"name": "甲", "parent": tree.ids["客戶"], "collapsed": True})
        self.assertEqual(data["folders"][tree.ids["客戶"]]["parent"], None)
        self.assertEqual(data["sessions"], {"id-a1": fid})
        self.assertIn("客戶", path.read_text())  # not escaped
        self.assertEqual(folders.load(self.dir, rows).ids, tree.ids)

    def test_v1_file_loads_and_saves_as_v2_with_same_membership(self):
        rows = self.write_registry(self.rows("a1", "b1", with_id=False))
        self.put('{"version": 1, "folders": {"客戶/甲": {"collapsed": true}}, "sessions": {"a1": "客戶/甲", "b1": "客戶"}}')
        tree = folders.load(self.dir)
        self.assertEqual(tree.sessions, {"a1": "客戶/甲", "b1": "客戶"})
        self.assertEqual(set(tree.ids), {"客戶", "客戶/甲"})
        folders.save(self.dir, tree, rows)  # rows from the registry carry ids now
        self.assertEqual(json.loads((self.dir / folders.FOLDERS_FILE).read_text())["version"], 2)
        again = folders.load(self.dir)
        self.assertEqual(again.sessions, tree.sessions)
        self.assertEqual(again.folders, tree.folders)
        self.assertEqual(again.ids, tree.ids)

    def test_save_keeps_v1_when_rows_lack_ids(self):
        rows = self.rows("a1", with_id=False)
        folders.save(self.dir, Tree({"客戶": False}, {"a1": "客戶"}), rows)
        data = json.loads((self.dir / folders.FOLDERS_FILE).read_text())
        self.assertEqual(data, {"version": 1, "folders": {"客戶": {"collapsed": False}}, "sessions": {"a1": "客戶"}})
        self.assertEqual(folders.load(self.dir, rows).sessions, {"a1": "客戶"})

    def two_level(self):
        rows = self.rows("a1", "b1")
        tree = mk(["客戶", "客戶/甲", "開發"], {"a1": "客戶/甲", "b1": "開發"})
        folders.save(self.dir, tree, rows)
        return rows, (self.dir / folders.FOLDERS_FILE)

    def records(self, path):
        data = json.loads(path.read_text())
        return data["folders"], data["sessions"]

    def test_rename_folder_changes_one_record(self):
        rows, path = self.two_level()
        before_f, before_s = self.records(path)
        tree = folders.load(self.dir, rows)
        folders.rename(tree, "客戶", "大客戶")
        folders.save(self.dir, tree, rows)
        after_f, after_s = self.records(path)
        changed = [k for k in after_f if after_f[k] != before_f[k]]
        self.assertEqual(changed, [tree.ids["大客戶"]])
        self.assertEqual(after_f[changed[0]]["name"], "大客戶")
        self.assertEqual(after_f.keys(), before_f.keys())
        self.assertEqual(after_s, before_s)

    def test_move_folder_changes_only_parent(self):
        rows, path = self.two_level()
        before_f, before_s = self.records(path)
        tree = folders.load(self.dir, rows)
        folders.move(tree, [("folder", "客戶/甲")], "開發")
        folders.save(self.dir, tree, rows)
        after_f, after_s = self.records(path)
        jia = tree.ids["開發/甲"]
        self.assertEqual(jia, [k for k, v in before_f.items() if v["name"] == "甲"][0])
        self.assertEqual(after_f[jia]["parent"], tree.ids["開發"])
        self.assertEqual({k: v for k, v in after_f.items() if k != jia}, {k: v for k, v in before_f.items() if k != jia})
        self.assertEqual({k: v for k, v in before_f[jia].items() if k != "parent"},
                         {k: v for k, v in after_f[jia].items() if k != "parent"})
        self.assertEqual(after_s, before_s)

    def test_remove_folder_keeps_children_ids(self):
        rows, path = self.two_level()
        tree = folders.load(self.dir, rows)
        jia, ke = tree.ids["客戶/甲"], tree.ids["客戶"]
        folders.remove(tree, "客戶")
        self.assertEqual(tree.ids["甲"], jia)
        self.assertNotIn(ke, tree.ids.values())
        folders.save(self.dir, tree, rows)
        self.assertEqual(folders.load(self.dir, rows).ids["甲"], jia)

    def test_session_rename_in_registry_keeps_folder(self):
        rows = self.write_registry(self.rows("a1"))
        folders.edit(self.dir, lambda t: folders.place_session(t, "a1", "客戶"))
        rows = registry.load_rows(self.dir / "registry.json")
        rows[0].tmux = "a2"
        registry.write_rows(self.dir / "registry.json", rows, "t", self.dir / "history")
        self.assertEqual(folders.load(self.dir).sessions, {"a2": "客戶"})

    def test_pending_member_survives_save_until_registered(self):
        rows = self.rows("a1")
        tree = Tree({"客戶": False}, {"a1": "客戶", "n1": "客戶"})
        folders.save(self.dir, tree, rows)
        data = json.loads((self.dir / folders.FOLDERS_FILE).read_text())
        self.assertEqual(data["pending"], {"n1": tree.ids["客戶"]})
        self.assertEqual(folders.load(self.dir, rows).sessions, {"a1": "客戶", "n1": "客戶"})

    def test_pending_member_moves_to_sessions_once_registered(self):
        rows = self.rows("a1")
        folders.save(self.dir, Tree({"客戶": False}, {"n1": "客戶"}), rows)
        rows = self.rows("a1", "n1")
        tree = folders.load(self.dir, rows)
        self.assertEqual(tree.sessions, {"n1": "客戶"})
        folders.save(self.dir, tree, rows)
        data = json.loads((self.dir / folders.FOLDERS_FILE).read_text())
        self.assertEqual(data["sessions"], {"id-n1": tree.ids["客戶"]})
        self.assertEqual(data["pending"], {})

    def test_v2_malformed_shapes_refused_not_crash(self):
        for text in ('{"version": 2, "folders": {"f1": {"name": "a", "parent": []}}, "sessions": {}}',
                     '{"version": 2, "folders": {"f1": {"name": "a", "parent": {}}}, "sessions": {}}',
                     '{"version": 2, "folders": {"f1": 3}, "sessions": {}}',
                     '{"version": 2, "folders": {"f1": {"name": 5, "parent": null}}, "sessions": {}}',
                     '{"version": 2, "folders": {"f1": {"name": "a", "parent": null}}, "sessions": {"s": []}}',
                     '{"version": 2, "folders": {"f1": {"name": "a", "parent": null}}, "sessions": {}, "pending": []}',
                     '{"version": 2, "folders": {"f1": {"name": "a", "parent": null}}, "sessions": {}, "pending": {"n": []}}'):
            self.assert_unreadable(text)

    def test_edit_without_registry_keeps_members_as_pending(self):
        self.put('{"version": 1, "folders": {"客戶": {"collapsed": false}}, "sessions": {"a1": "客戶"}}')
        folders.edit(self.dir, lambda t: folders.add(t, "開發"))
        data = json.loads((self.dir / folders.FOLDERS_FILE).read_text())
        self.assertEqual(data["sessions"], {})
        self.assertEqual(list(data["pending"]), ["a1"])
        self.assertEqual(folders.load(self.dir).sessions, {"a1": "客戶"})

    def test_edit_refuses_when_registry_missing_but_file_has_id_members(self):
        rows = self.rows("a1")
        folders.save(self.dir, Tree({"客戶": False}, {"a1": "客戶"}), rows)
        before = (self.dir / folders.FOLDERS_FILE).read_text()
        with self.assertRaises(FoldersError):
            folders.edit(self.dir, lambda t: folders.add(t, "x"))
        self.assertEqual((self.dir / folders.FOLDERS_FILE).read_text(), before)

    def test_v1_member_without_registry_row_ends_in_pending(self):
        rows = self.rows("a1")
        self.put('{"version": 1, "folders": {"客戶": {"collapsed": false}}, "sessions": {"a1": "客戶", "ghost": "客戶"}}')
        folders.save(self.dir, folders.load(self.dir), rows)
        data = json.loads((self.dir / folders.FOLDERS_FILE).read_text())
        self.assertEqual(list(data["sessions"]), ["id-a1"])
        self.assertEqual(list(data["pending"]), ["ghost"])

    def test_group_keeps_folder_ids(self):
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲"})
        folders.add(t, "客戶/乙")
        folders._fill(t)
        before = dict(t.ids)
        path = folders.group(t, [("folder", "客戶/甲"), ("folder", "客戶/乙")], "合")
        self.assertEqual(path, "客戶/合")
        self.assertEqual(t.ids["客戶/合/甲"], before["客戶/甲"])
        self.assertEqual(t.ids["客戶/合/乙"], before["客戶/乙"])
        self.assertEqual(t.ids["客戶"], before["客戶"])

    def test_unknown_session_id_dropped(self):
        rows = self.rows("a1")
        folders.save(self.dir, Tree({"客戶": False}, {"a1": "客戶"}), rows)
        self.assertEqual(folders.load(self.dir, []).sessions, {})
        self.assertEqual(folders.load(self.dir, []).folders, {"客戶": False})

    def test_v3_refused_untouched(self):
        text = '{"version": 3, "folders": {}, "sessions": {}}'
        self.put(text)
        with self.assertRaises(FoldersError):
            folders.load(self.dir)
        with self.assertRaises(FoldersError):
            folders.edit(self.dir, lambda t: folders.add(t, "x"))
        self.assertEqual((self.dir / folders.FOLDERS_FILE).read_text(), text)

    def test_v2_bad_shape_refused(self):
        for folders_part in ('{"f1": {"name": "a", "parent": "nope"}}',
                             '{"f1": {"name": "a", "parent": "f2"}, "f2": {"name": "b", "parent": "f1"}}',
                             '{"f1": {"name": "a/b", "parent": null}}',
                             '{"f1": {"name": "a", "parent": null}, "f2": {"name": "a", "parent": null}}'):
            self.assert_unreadable('{"version": 2, "folders": %s, "sessions": {}}' % folders_part)
        self.assert_unreadable('{"version": 2, "folders": {}, "sessions": {"s": "zz"}}')

    def assert_unreadable(self, text):
        self.put(text)
        with self.assertRaises(FoldersError) as cm:
            folders.load(self.dir)
        self.assertTrue(str(cm.exception).startswith("資料夾設定讀不到："))

    def test_load_adds_implied_parents(self):
        self.put('{"version": 1, "folders": {"A/B/C": {"collapsed": true}, "A": {"collapsed": true}}, '
                 '"sessions": {"s": "D/E"}}')
        tree = folders.load(self.dir)
        self.assertEqual(tree.folders, {"A/B/C": True, "A": True, "A/B": False, "D": False, "D/E": False})

    def test_load_bad_json(self):
        self.assert_unreadable("{nope")
        self.assert_unreadable("")

    def test_load_bad_version(self):
        self.assert_unreadable('{"version": 3, "folders": {}, "sessions": {}}')

    def test_load_bad_shape(self):
        self.assert_unreadable("[]")
        self.assert_unreadable('{"version": 1, "folders": [], "sessions": {}}')
        self.assert_unreadable('{"version": 1, "folders": {"a": true}, "sessions": {}}')
        self.assert_unreadable('{"version": 1, "folders": {"a": {"collapsed": "no"}}, "sessions": {}}')
        self.assert_unreadable('{"version": 1, "folders": {}, "sessions": {"a1": 3}}')

    def test_load_bad_path(self):
        for bad in ("", "/x", "a/", "a//b", "a/ /b", "a\nb"):
            self.assert_unreadable(json.dumps({"version": 1, "folders": {bad: {"collapsed": False}}, "sessions": {}}))
            self.assert_unreadable(json.dumps({"version": 1, "folders": {}, "sessions": {"a1": bad}}))

    def test_broken_file_is_not_overwritten_by_edit(self):
        self.put("{nope")
        with self.assertRaises(FoldersError):
            folders.edit(self.dir, lambda t: folders.add(t, "x"))
        self.assertEqual((self.dir / folders.FOLDERS_FILE).read_text(), "{nope")

    def test_edit_saves_and_returns(self):
        self.assertEqual(folders.edit(self.dir, lambda t: (folders.add(t, "客戶/甲"), 7)[1]), 7)
        self.assertEqual(set(folders.load(self.dir).folders), {"客戶", "客戶/甲"})

    def test_edit_reads_rows_without_taking_the_lock_twice(self):
        self.write_registry(self.rows("a1"))
        folders.edit(self.dir, lambda t: folders.place_session(t, "a1", "客戶"))
        self.assertEqual(json.loads((self.dir / folders.FOLDERS_FILE).read_text())["sessions"].keys(), {"id-a1"})

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

    def test_remove_with_same_named_child(self):
        t = mk(["客戶", "客戶/客戶", "客戶/客戶/x"], {"a1": "客戶/客戶/x", "b1": "客戶", "c1": "客戶/客戶"})
        folders.remove(t, "客戶")
        self.assertEqual(t.folders, {"客戶": False, "客戶/x": False})
        self.assertEqual(t.sessions, {"a1": "客戶/x", "c1": "客戶"})

    def test_group_nothing_refused(self):
        with self.assertRaises(FoldersError):
            folders.group(mk(), [], "合")

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
        t = mk(["客戶", "客戶/甲"], {"a1": "客戶/甲"})
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
