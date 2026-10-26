import tempfile
import unittest
from pathlib import Path

from sampan import folders
from sampan.folder_cmd import cmd_folder, sync_session
from sampan.registry import Row, write_rows
from tests.helpers import make_env

A = "aaaaaaaa-0000-4000-8000-000000000001"


class FolderCmdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.msgs = []
        write_rows(self.cfg.registry_path, [Row(n, "claude", "alice", A, "/c", "active", "t", "srv", None)
                                            for n in ("s1", "s2", "s3")], "t", self.cfg.history_dir)

    def run_cmd(self, action, *args, merge=False):
        self.msgs.clear()
        return cmd_folder(self.cfg, self.msgs.append, action, list(args), merge=merge)

    def tree(self):
        return folders.load(self.cfg.state_dir)

    def test_add_then_list_prints_tree(self):
        self.assertEqual(self.run_cmd("add", "客戶/甲"), 0)
        self.assertEqual(self.run_cmd("move", "s1", "客戶/甲"), 0)
        self.assertEqual(self.run_cmd("add", "開發"), 0)
        folders.edit(self.cfg.state_dir, lambda t: folders.set_collapsed(t, "開發", True))
        self.assertEqual(self.run_cmd("list"), 0)
        self.assertEqual(self.msgs, ["▾ 客戶/", "  ▾ 甲/", "    s1", "▸ 開發/", "s2", "s3"])

    def test_list_without_folders_prints_sessions_only(self):
        self.assertEqual(self.run_cmd("list"), 0)
        self.assertEqual(self.msgs, ["s1", "s2", "s3"])

    def test_list_empty_registry(self):
        write_rows(self.cfg.registry_path, [], "t", self.cfg.history_dir)
        self.assertEqual(self.run_cmd("list"), 0)
        self.assertEqual(self.msgs, ["目前沒有任何 session"])

    def test_move_session_and_folder(self):
        self.run_cmd("add", "客戶")
        self.run_cmd("add", "開發/甲")
        self.assertEqual(self.run_cmd("move", "s1", "開發/甲", "客戶"), 0)
        t = self.tree()
        self.assertEqual(t.sessions, {"s1": "客戶"})
        self.assertIn("客戶/甲", t.folders)
        self.assertNotIn("開發/甲", t.folders)
        self.assertEqual(self.run_cmd("move", "s1", "/"), 0)
        self.assertEqual(self.tree().sessions, {})

    def test_move_unknown_source_refused(self):
        self.run_cmd("add", "客戶")
        self.assertEqual(self.run_cmd("move", "nope", "客戶"), 2)
        self.assertEqual(self.msgs, ["❌ 找不到 nope"])
        self.assertEqual(self.tree().sessions, {})

    def test_rename_taken_without_merge_is_rc2(self):
        self.run_cmd("add", "甲")
        self.run_cmd("add", "乙")
        self.assertEqual(self.run_cmd("rename", "甲", "乙"), 2)
        self.assertIn("甲", self.tree().folders)
        self.assertEqual(self.run_cmd("rename", "甲", "乙", merge=True), 0)
        self.assertNotIn("甲", self.tree().folders)

    def test_rm_keeps_sessions(self):
        self.run_cmd("move", "s1", "客戶/甲")
        self.assertEqual(self.run_cmd("rm", "客戶"), 0)
        self.assertEqual(self.tree().sessions, {"s1": "甲"})
        self.run_cmd("list")
        self.assertEqual(self.msgs, ["▾ 甲/", "  s1", "s2", "s3"])

    def test_hand_written_child_makes_parent_real(self):
        path = self.cfg.state_dir / "folders.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": 1, "folders": {"A/B": {}}, "sessions": {}}')
        self.assertEqual(self.run_cmd("rename", "A", "Z"), 0, self.msgs)
        self.assertEqual(self.tree().folders, {"Z": False, "Z/B": False})
        self.assertEqual(self.run_cmd("rm", "Z"), 0, self.msgs)
        self.assertEqual(self.tree().folders, {"B": False})

    def test_broken_file_refuses_writes_and_keeps_file(self):
        path = self.cfg.state_dir / "folders.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{")
        for action, args in (("add", ["甲"]), ("rm", ["甲"]), ("move", ["s1", "甲"]), ("rename", ["甲", "乙"])):
            self.assertEqual(self.run_cmd(action, *args), 2)
            self.assertTrue(any("folders.json" in m and "壞" in m for m in self.msgs))
        self.assertEqual(path.read_text(), "{")
        self.assertEqual(self.run_cmd("list"), 0)  # still lists, flat
        self.assertEqual(self.msgs[-3:], ["s1", "s2", "s3"])

    def test_folder_ops_never_touch_registry(self):
        before = self.cfg.registry_path.read_bytes()
        self.run_cmd("add", "客戶")
        self.run_cmd("move", "s1", "客戶")
        self.run_cmd("rename", "客戶", "開發")
        self.run_cmd("rm", "開發")
        self.assertEqual(self.cfg.registry_path.read_bytes(), before)

    def test_bad_usage(self):
        self.assertEqual(self.run_cmd("add"), 2)
        self.assertEqual(self.run_cmd("move", "s1"), 2)
        self.assertEqual(self.run_cmd("add", "a//b"), 2)

    def test_sync_session_is_quiet_without_file_and_warns_when_broken(self):
        sync_session(self.cfg, self.msgs.append, "rename", "s1", "x")
        self.assertEqual(self.msgs, [])
        self.assertFalse((self.cfg.state_dir / "folders.json").exists())
        (self.cfg.state_dir / "folders.json").write_text("{")
        sync_session(self.cfg, self.msgs.append, "forget", "s1")
        self.assertEqual(len(self.msgs), 1)
        self.assertTrue(self.msgs[0].startswith("⚠ 資料夾設定沒同步："))


if __name__ == "__main__":
    unittest.main()
