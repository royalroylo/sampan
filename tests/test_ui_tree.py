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
        self.assertEqual((top.collapsed, top.total, top.counts, top.depth), (True, 3, {"等人": 0, "執行中": 1}, 0))
        self.assertEqual(top.name, "\x00folder:客戶")
        self.assertEqual((items[1][1].total, items[1][1].counts["執行中"], items[1][1].collapsed), (1, 0, False))

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


class LightFilterTest(unittest.TestCase):
    """The state filter by light: 等人 holds waiting and unseen, 執行中 only the running ones, 閒置 is what S restarts."""

    def test_filters_order(self):
        from sampan.ui.tree import FILTERS
        self.assertEqual(FILTERS, ("全部", "等人", "執行中", "閒置", "已停止", "異常"))

    def test_running_filter_excludes_waiting(self):
        from sampan.lights import RUNNING, WAITING
        from sampan.ui.tree import matches
        self.assertTrue(matches(row("a", "busy"), "執行中", RUNNING))
        self.assertTrue(matches(row("cx", "working"), "執行中", RUNNING))
        self.assertFalse(matches(row("w", "waiting"), "執行中", WAITING))
        self.assertTrue(matches(row("w", "waiting"), "等人", WAITING))
        self.assertTrue(matches(row("a", "busy"), "執行中"))  # no light given: the agent's own status decides
        self.assertFalse(matches(row("w", "waiting"), "執行中"))

    def test_waiting_filter_includes_unseen(self):
        from sampan.lights import IDLE, UNSEEN
        from sampan.ui.tree import matches
        self.assertTrue(matches(row("u"), "等人", UNSEEN))
        self.assertFalse(matches(row("i"), "等人", IDLE))
        self.assertFalse(matches(row("s", "-", State.STOPPED), "等人", None))

    def test_idle_filter_unchanged(self):
        from sampan.lights import IDLE, RUNNING, UNSEEN
        from sampan.ui.tree import matches
        self.assertTrue(matches(row("i"), "閒置", IDLE))
        self.assertTrue(matches(row("u"), "閒置", UNSEEN))  # unseen is still idle: S may restart it
        self.assertFalse(matches(row("a", "busy"), "閒置", RUNNING))
        self.assertFalse(matches(row("s", "idle", State.STOPPED), "閒置", None))

    def test_folder_counts_from_lights(self):
        from sampan.lights import IDLE, RUNNING, UNSEEN, WAITING
        lights = {"a1": UNSEEN, "b1": WAITING, "c1": RUNNING, "d1": IDLE}
        items = build_items(ROWS, TREE, None, lights=lights)
        top = items[0][1]
        self.assertEqual(top.counts, {"等人": 2, "執行中": 1})
        dev = next(it for _, it in items if isinstance(it, FolderItem) and it.path == "開發")
        self.assertEqual(dev.counts, {"等人": 0, "執行中": 0})


class LightRenderTest(unittest.TestCase):
    def folder(self, collapsed, counts):
        return FolderItem("客戶/甲", 1, collapsed, 5, counts)

    def test_folder_row_shows_counts_open_or_collapsed(self):
        from sampan.ui.render import folder_cells
        for collapsed in (True, False):
            text = folder_cells(self.folder(collapsed, {"等人": 2, "執行中": 1}), set())[0]
            self.assertTrue(text.endswith(f"{'▸' if collapsed else '▾'} 甲 (5)　2 等人 1 執行中"), text)
            self.assertNotIn("忙", text)

    def test_zero_counts_hidden(self):
        from sampan.ui.render import folder_cells
        self.assertTrue(folder_cells(self.folder(False, {"等人": 0, "執行中": 3}), set())[0].endswith("(5)　3 執行中"))
        self.assertTrue(folder_cells(self.folder(True, {"等人": 1, "執行中": 0}), set())[0].endswith("(5)　1 等人"))
        self.assertTrue(folder_cells(self.folder(True, {"等人": 0, "執行中": 0}), set())[0].endswith("▸ 甲 (5)"))

    def test_light_dot_styles(self):
        from sampan.lights import IDLE, RUNNING, UNSEEN, WAITING
        from sampan.ui.render import _list_cells
        want = {RUNNING: "info", WAITING: "warn", UNSEEN: "warn", IDLE: "muted"}
        for label, style in want.items():
            with self.subTest(label=label):
                cells = _list_cells(row("a"), set(), None, light=label)
                runs = cells[0]
                self.assertIn(("◆", style), runs)
                self.assertEqual("".join(t for t, _s in runs), "  ◆ a")
                self.assertNotIn("●", "".join(t for t, _s in runs))  # ● is only ever the mark
                self.assertEqual(cells[4], (label, style))  # the activity column says the light
        plain = _list_cells(row("s", "-", State.STOPPED), set(), None)
        self.assertEqual(plain[0], "    s")  # no light: a blank cell where the dot would be
        self.assertEqual(plain[4][0], "-")  # and the agent's own status as before
