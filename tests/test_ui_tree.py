import unittest

from sampan.folders import Tree
from sampan.status import State
from sampan.ui.tree import FolderItem, build_items, folder_of
from tests.helpers import status_row


def row(name, agent_status="idle", state=State.RUNNING):
    return status_row(name, state, agent_status=agent_status)


def shape(items):
    return [(d, it.path if isinstance(it, FolderItem) else it.name) for d, it in items]


ROWS = [row("z-top"), row("b1"), row("a1"), row("c1", agent_status="busy"), row("d1"), row("a-top")]
TREE = Tree({"客戶": False, "客戶/乙": False, "客戶/甲": False, "開發": False},
            {"a1": "客戶", "b1": "客戶/甲", "c1": "客戶/乙", "d1": "開發"})


class BuildItemsTest(unittest.TestCase):
    def test_flat_when_no_tree(self):
        rows = [row("b"), row("a")]
        self.assertEqual(shape(build_items(rows, None, None)), [(0, "a"), (0, "b")])
        self.assertEqual(shape(build_items(rows, Tree(), None)), [(0, "a"), (0, "b")])

    def test_folders_first_then_sessions_by_name(self):
        self.assertEqual(shape(build_items(ROWS, TREE, None)), [
            (0, "客戶"), (1, "客戶/乙"), (2, "c1"), (1, "客戶/甲"), (2, "b1"), (1, "a1"),
            (0, "開發"), (1, "d1"), (0, "a-top"), (0, "z-top")])

    def test_top_level_sessions_after_folders(self):
        tree = Tree({"維運": False}, {"b": "維運"})
        self.assertEqual(shape(build_items([row("a"), row("b"), row("c")], tree, None)),
                         [(0, "維運"), (1, "b"), (0, "a"), (0, "c")])

    def test_collapsed_hides_children_but_counts(self):
        tree = Tree(dict(TREE.folders, 客戶=True), dict(TREE.sessions))
        items = build_items(ROWS, tree, None)
        self.assertEqual(shape(items), [(0, "客戶"), (0, "開發"), (1, "d1"), (0, "a-top"), (0, "z-top")])
        top = items[0][1]
        self.assertEqual((top.collapsed, top.total, top.busy, top.depth), (True, 3, 1, 0))
        self.assertEqual(top.name, "\x00folder:客戶")
        self.assertEqual((items[1][1].total, items[1][1].busy, items[1][1].collapsed), (1, 0, False))

    def test_unknown_sessions_dropped_and_missing_parents_added(self):
        tree = Tree({}, {"a1": "客戶/甲", "gone": "開發"})
        self.assertEqual(shape(build_items([row("a1"), row("x")], tree, None)),
                         [(0, "客戶"), (1, "客戶/甲"), (2, "a1"), (0, "x")])  # 開發 only held a gone session

    def test_search_lists_matching_sessions_only(self):
        tree = Tree(dict(TREE.folders, 客戶=True), dict(TREE.sessions))
        self.assertEqual(shape(build_items(ROWS, tree, "1")), [(0, "a1"), (0, "b1"), (0, "c1"), (0, "d1")])
        self.assertEqual(shape(build_items(ROWS, tree, "TOP")), [(0, "a-top"), (0, "z-top")])
        self.assertEqual(shape(build_items(ROWS, None, "")), shape(build_items(ROWS, None, None)))

    def test_folder_of(self):
        self.assertEqual(folder_of(TREE, "b1"), "客戶/甲")
        self.assertEqual(folder_of(TREE, "a-top"), "")
        self.assertEqual(folder_of(None, "b1"), "")


if __name__ == "__main__":
    unittest.main()
