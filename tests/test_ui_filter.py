import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from sampan.folders import Tree
from sampan.status import State
from sampan.ui.render import list_empty
from sampan.ui.state import Picker
from sampan.ui.tree import FILTERS, FolderItem, build_items, matches
from tests.helpers import status_row


def row(name, state=State.RUNNING, agent_status="idle"):
    return status_row(name, state, agent_status=agent_status)


def tab(p, n):
    for _ in range(n):
        p.key("\t")


def names(p):
    return [it.path if isinstance(it, FolderItem) else it.name for it in p.visible]


ROWS = [row("idle1"), row("busy1", agent_status="busy"), row("wait1", agent_status="waiting"),
        row("unk1", agent_status="unknown"), row("dash1", agent_status="-"),
        row("stop1", State.STOPPED, "-"), row("old1", State.RETIRED, "-"),
        row("trust1", State.AWAIT_TRUST, "-"), row("prof1", State.PROFILE_MISMATCH),
        row("multi1", State.MULTI, "-"), row("unreg1", State.UNREGISTERED), row("noag1", State.NO_AGENT, "-"),
        row("agmis1", State.AGENT_MISMATCH), row("unknown1", State.UNKNOWN, "-")]


class MatchesTest(unittest.TestCase):
    def kinds(self, name):
        r = next(x for x in ROWS if x.name == name)
        return [f for f in FILTERS if matches(r, f)]

    def test_filters_in_tab_order(self):
        self.assertEqual(FILTERS, ("全部", "等人", "執行中", "閒置", "已停止", "異常"))

    def test_each_row_is_in_all_and_at_most_one_other(self):
        self.assertEqual(self.kinds("idle1"), ["全部", "閒置"])
        self.assertEqual(self.kinds("busy1"), ["全部", "執行中"])
        self.assertEqual(self.kinds("wait1"), ["全部", "等人"])
        for n in ("unk1", "dash1"):  # no light: only under 全部
            self.assertEqual(self.kinds(n), ["全部"], n)
        self.assertEqual(self.kinds("stop1"), ["全部", "已停止"])
        self.assertEqual(self.kinds("old1"), ["全部"])
        for n in ("trust1", "prof1", "multi1", "unreg1", "noag1", "agmis1", "unknown1"):
            self.assertEqual(self.kinds(n), ["全部", "異常"], n)


def tree():
    return Tree({"客戶": False, "客戶/甲": False, "空": False},
                {"a1": "客戶", "b1": "客戶/甲", "s1": "客戶/甲"})


def frows():
    return [row("a1"), row("b1", agent_status="busy"), row("s1", State.STOPPED, "-"), row("top")]


class BuildItemsHideEmptyTest(unittest.TestCase):
    def test_empty_folders_hidden_only_when_asked(self):
        rows = [r for r in frows() if r.state is State.STOPPED]
        shown = [getattr(it, "path", None) or it.name for _, it in build_items(rows, tree(), None, hide_empty=True)]
        self.assertEqual(shown, ["客戶", "客戶/甲", "s1"])
        every = [getattr(it, "path", None) or it.name for _, it in build_items(rows, tree(), None)]
        self.assertIn("空", every)


class PickerFilterTest(unittest.TestCase):
    def picker(self, rows=None, current=None, t=None, others=()):
        return Picker(rows if rows is not None else frows(), current, tree=t, others=others)

    def test_tab_cycles_forward_and_shift_tab_back(self):
        p = self.picker()
        self.assertEqual(p.filter_name, "全部")
        seen = []
        for _ in range(6):
            self.assertIsNone(p.key("\t"))
            seen.append(p.filter_name)
        self.assertEqual(seen, ["等人", "執行中", "閒置", "已停止", "異常", "全部"])
        p.key("KEY_BTAB")
        self.assertEqual(p.filter_name, "異常")

    def test_filter_lists_only_matching_and_keeps_the_cursor_session(self):
        p = self.picker(current="a1")
        tab(p, 3)  # 閒置
        self.assertEqual(names(p), ["a1", "top"])
        self.assertEqual(p.visible[p.cursor].name, "a1")
        self.assertEqual(p.filter_count, 2)
        p.key("KEY_BTAB")  # 執行中: a1 gone, cursor lands on what is there
        self.assertEqual(names(p), ["b1"])
        self.assertEqual(p.cursor, 0)

    def test_folders_show_only_matches_and_hide_empty_ones(self):
        p = self.picker(t=tree())
        self.assertIn("空", names(p))
        tab(p, 4)  # 已停止
        self.assertEqual(names(p), ["客戶", "客戶/甲", "s1"])
        self.assertEqual([it.total for it in p.visible if isinstance(it, FolderItem)], [1, 1])

    def test_search_stacks_on_filter_and_tab_is_not_text(self):
        p = self.picker(rows=frows() + [row("a2")])
        tab(p, 3)  # 閒置: a1 top a2
        p.key("/")
        p.key("a")
        self.assertEqual(names(p), ["a1", "a2"])
        p.key("\t")
        self.assertEqual((p.query, p.filter_name), ("a", "閒置"))
        p.key("\x1b")
        self.assertEqual(sorted(names(p)), ["a1", "a2", "top"])

    def test_others_block_only_under_all(self):
        other = NS(name="\x00other:t", thread=NS(name="t"))
        p = self.picker(others=[other])
        self.assertEqual(len(p.visible), 5)
        tab(p, 3)
        self.assertEqual(names(p), ["a1", "top"])
        self.assertEqual(p.n_rows, len(p.visible))

    def test_digits_count_the_filtered_rows(self):
        p = self.picker()
        tab(p, 3)  # 閒置: a1 top
        p.key("2")
        self.assertEqual(p.visible[p.cursor].name, "top")

    def test_marks_follow_the_filter(self):
        p = self.picker(current="s1")
        p.key(" ")  # mark s1 (stopped)
        tab(p, 3)  # 閒置: s1 is not shown, so it is no longer a target
        self.assertEqual(p.marked, set())

    def test_folder_targets_only_the_matching_sessions(self):
        p = self.picker(t=tree())
        tab(p, 2)  # 執行中: 客戶, 客戶/甲, b1
        self.assertEqual(p.key("s"), None)
        self.assertEqual(p.key("y"), ("stop!", ["b1"]))


class BangTest(unittest.TestCase):
    def test_bang_jumps_to_waiting_and_back(self):
        from sampan.lights import IDLE, UNSEEN
        p = Picker(frows() + [row("w1", agent_status="waiting")], None, filter=3,
                   lights={"a1": UNSEEN, "top": IDLE})
        self.assertIsNone(p.key("!"))
        self.assertEqual(p.filter_name, "等人")
        self.assertEqual(names(p), ["a1", "w1"])
        p.key("!")
        self.assertEqual(p.filter_name, "全部")
        p.key("!")
        self.assertEqual(p.filter_name, "等人")

    def test_bang_is_text_while_searching(self):
        p = Picker(frows(), None)
        p.key("/")
        p.key("!")
        self.assertEqual((p.query, p.filter_name), ("!", "全部"))

    def test_set_lights_refilters(self):
        from sampan.lights import UNSEEN
        p = Picker(frows(), None, filter=1)
        self.assertEqual(names(p), [])
        p.set_lights({"top": UNSEEN})
        self.assertEqual(names(p), ["top"])


class FilterRefreshTest(unittest.TestCase):
    def test_marks_lost_on_refresh_refuse_instead_of_using_the_cursor(self):
        rows = [row("a"), row("b"), row("c")]
        p = Picker(rows, "a")
        tab(p, 3)  # 閒置
        p.key(" ")  # mark a; cursor on b
        p.reload([row("a", agent_status="busy"), row("b"), row("c")])
        self.assertEqual(p.marked, set())
        self.assertIsNone(p.key("S"))
        self.assertIn("請重新勾選", p.prompt)
        self.assertIsNone(p.restart_plan)
        p.key("S")  # said once: now the cursor row
        self.assertEqual(p.key("y"), ("restart!", [p.visible[p.cursor].name]))

    def test_marking_again_clears_the_refusal(self):
        p = Picker([row("a"), row("b")], "a")
        p.key(" ")
        tab(p, 2)  # 執行中: a is hidden, mark dropped
        tab(p, 4)  # back to 全部
        p.key("*")
        p.key("s")
        self.assertEqual(p.key("y"), ("stop!", ["a", "b"]))

    def test_filter_survives_a_new_picker_and_reload_keeps_it(self):
        p = Picker(frows(), None, filter=3)
        self.assertEqual(names(p), ["a1", "top"])
        p.reload(frows() + [row("new")])
        self.assertEqual(names(p), ["a1", "top", "new"])

    def test_collapse_and_set_tree_under_a_filter(self):
        p = Picker(frows(), None, tree=tree(), filter=4)  # 已停止: 客戶, 客戶/甲, s1
        self.assertEqual(p.key("\n"), ("collapse", ["客戶", "1"]))
        self.assertEqual(names(p), ["客戶"])
        t = tree()
        t.sessions["s1"] = "空"
        p.set_tree(t)
        self.assertEqual(names(p), ["空", "s1"])


class SelectAllTest(unittest.TestCase):
    def test_star_marks_visible_sessions_then_clears(self):
        p = Picker(frows(), None, tree=tree())
        tab(p, 3)  # 閒置: 客戶, a1, top
        self.assertIsNone(p.key("*"))
        self.assertEqual(p.marked, {"a1", "top"})
        self.assertEqual(p.marked_folders, set())
        p.key("*")
        self.assertEqual((p.marked, p.marked_folders), (set(), set()))

    def test_star_skips_sessions_in_collapsed_folders(self):
        t = tree()
        t.folders["客戶"] = True
        p = Picker(frows(), None, tree=t)
        p.key("*")
        self.assertEqual(p.marked, {"top"})

    def test_star_with_some_marked_marks_the_rest_and_a_second_press_clears_all(self):
        p = Picker(frows(), "a1")
        p.key(" ")
        p.key("*")
        self.assertEqual(p.marked, {"a1", "b1", "s1", "top"})
        p.key("*")
        self.assertEqual(p.marked, set())

    def test_star_then_S_restarts_the_idle_ones(self):
        p = Picker(frows(), None)
        tab(p, 3)
        p.key("*")
        p.key("S")
        self.assertEqual(p.key("y"), ("restart!", ["a1", "top"]))

    def test_star_then_S_under_stopped_resumes_them(self):
        p = Picker(frows(), None)
        tab(p, 4)
        p.key("*")
        p.key("S")
        self.assertEqual(p.key("y"), ("restart!", ["s1"]))


class EmptyTest(unittest.TestCase):
    def test_messages(self):
        rows = frows()
        self.assertEqual(list_empty(rows, None, "異常"), "沒有異常的 session")
        self.assertEqual(list_empty(rows, "zz", "閒置"), "閒置的 session 裡沒有名稱含「zz」的")
        self.assertEqual(list_empty(rows, "zz"), "沒有名稱含「zz」的 session")
        self.assertEqual(list_empty([], None, "閒置"), "目前沒有任何 session（n 開新的）")


class ScreenTest(unittest.TestCase):
    def test_title_shows_filter_and_count_and_help_lists_keys(self):
        from sampan.ui.screen import KEYS_HELP, CursesScreen
        from tests.fakes import FakeRunner, cp
        from tests.helpers import make_env
        from tests.test_ui_screen import NOW, FakeScr
        cfg = make_env(Path(tempfile.mkdtemp()))
        r = FakeRunner(strict=True).on(["tmux", "-u", "list-panes"], cp()).on(["tmux", "-u", "list-windows"], cp())
        scr = FakeScr(["\t", "\t", "\t", "\t", "\t", "q"])
        s = CursesScreen(scr, cfg, r, now_fn=lambda: NOW, rows_fn=lambda cfg, run: [])
        s.pick([row("a1"), row("b1", agent_status="busy")], None)
        self.assertNotIn("›", scr.screens[0][0])
        self.assertIn("› 等人（0）", scr.screens[1][0])
        self.assertIn("› 閒置（1）", scr.screens[3][0])
        self.assertIn("› 異常（0）", scr.screens[5][0])
        self.assertIn("沒有異常的 session", "\n".join(scr.screens[5]))
        self.assertIn("Tab 篩選　! 等人", KEYS_HELP)
        self.assertIn("v 預覽", KEYS_HELP)
        self.assertIn("* 全選　空白 勾選", KEYS_HELP)  # marking keys stay listed (batch sends use the marks)
        self.assertIn("D 派工　t 送", KEYS_HELP)
        from sampan.ui.text import text_width
        self.assertLessEqual(text_width(" " + KEYS_HELP), 119)  # one line at 120 columns (render gets width - 1)
        self.assertTrue(scr.screens[2][1].lstrip().startswith("1   ◆ b1"), scr.screens[2][1])  # numbered within the filter

    def test_filter_stays_after_an_action(self):
        from sampan.ui.screen import CursesScreen
        from tests.fakes import FakeRunner, cp
        from tests.helpers import make_env
        from tests.test_ui_screen import NOW, FakeScr
        cfg = make_env(Path(tempfile.mkdtemp()))
        r = FakeRunner(strict=True).on(["tmux", "-u", "list-panes"], cp()).on(["tmux", "-u", "list-windows"], cp())
        scr = FakeScr(["KEY_BTAB", "w", "q"])
        s = CursesScreen(scr, cfg, r, now_fn=lambda: NOW, rows_fn=lambda cfg, run: [])
        rows = [row("a1"), row("odd", State.UNKNOWN, "-")]
        self.assertEqual(s.pick(rows, None), ("save", []))
        self.assertEqual(s.pick(rows, None), ("quit", []))
        self.assertIn("› 異常（1）", scr.screens[-1][0])


if __name__ == "__main__":
    unittest.main()
