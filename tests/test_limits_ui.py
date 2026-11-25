import dataclasses
import tempfile
import unittest
from pathlib import Path

from sampan.restart import RestartPlan
from sampan.status import State
from sampan.ui import resource_bar as rb
from sampan.ui.actions import cmd_ui
from sampan.ui.screen import CursesScreen
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env, status_row
from tests.test_resource_bar import reading

ROW = status_row("a")


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.msgs, self.execs, self.seen = [], [], {}
        self.r = FakeRunner(strict=True)
        self.r.on(["tmux", "-u", "has-session"], cp())


class EnterRefused(Base):
    def stopped(self):
        return [dataclasses.replace(ROW, state=State.STOPPED)]

    def test_a_refusal_neither_goes_in_nor_says_press_enter_again(self):
        rows = self.stopped()

        def restore_fn(cfg, run, out, ask, **kw):
            kw["refused"].append("a")  # the limit check turned it away
            out("❌ 不開 a：記憶體紅燈")
            return 1
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "1", rows_fn=lambda c, r: rows,
               exec_fn=lambda *a: self.execs.append(a), restore_fn=restore_fn, loop=False)
        self.assertEqual(self.execs, [])
        self.assertFalse(any("沒辦法自動接回" in m or "再按 Enter" in m for m in self.msgs), self.msgs)

    def test_not_refused_still_goes_in_as_before(self):
        rows = self.stopped()
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "1", rows_fn=lambda c, r: rows,
               exec_fn=lambda *a: self.execs.append(a), restore_fn=lambda *a, **kw: 1, loop=False)
        self.assertEqual(len(self.execs), 1)

    def test_the_enter_path_gets_the_y_n_answer(self):
        rows = self.stopped()
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "1", rows_fn=lambda c, r: rows,
               exec_fn=lambda *a: None, restore_fn=lambda *a, **kw: self.seen.update(kw) or 0, loop=False,
               limit_override=True)
        self.assertTrue(callable(self.seen["ask_limit"]))


class AnswerFunction(Base):
    def answer(self, typed):
        def new_fn(cfg, run, out, name, profile, cwd, **kw):
            self.seen["yes"] = kw["ask_limit"]("仍要開？(y/N) ")
            return 0
        answers = iter(["fresh", "", "", "/tmp"])
        asked = []

        def ask(prompt):
            if "仍要開" in prompt:
                asked.append(prompt)
                return typed
            return next(answers)
        cmd_ui(self.cfg, self.r, self.msgs.append, ask, rows_fn=lambda c, r: [ROW],
               picker_fn=lambda rows, cur: ("new", []), exec_fn=lambda *a: None, new_fn=new_fn, limit_override=True)
        return asked

    def test_y_means_yes_anything_else_no(self):
        for typed, want in (("y", True), ("Y", True), (" y ", True), ("", False), ("n", False), ("yes", False)):
            with self.subTest(typed):
                self.seen.clear()
                self.assertEqual(len(self.answer(typed)), 1)
                self.assertEqual(self.seen["yes"], want)

    def test_restart_and_restore_and_move_and_switch_get_it_too(self):
        got = {}
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "y", rows_fn=lambda c, r: [ROW],
               picker_fn=lambda rows, cur: ("restart!", [RestartPlan(["a"], [], [])]),
               exec_fn=lambda *a: None, restart_fn=lambda *a, **kw: got.update(restart=kw) or 0, limit_override=True)
        self.assertTrue(callable(got["restart"]["ask_limit"]))
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "y", rows_fn=lambda c, r: [ROW],
               picker_fn=lambda rows, cur: ("restore", []), exec_fn=lambda *a: None,
               restore_fn=lambda *a, **kw: got.update(restore=kw) or 0, limit_override=True)
        self.assertTrue(callable(got["restore"]["ask_limit"]))


class PlainLine(Base):
    def test_printed_before_each_listing(self):
        answers = iter(["", "q"])
        lines = []
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), rows_fn=lambda c, r: [ROW],
               exec_fn=lambda *a: None, loop=True, resource_line_fn=lambda rows: lines.append(len(rows)) or "◆ 執行中 1")
        self.assertGreaterEqual(len(lines), 1)
        self.assertIn("◆ 執行中 1", self.msgs)

    def test_none_means_nothing(self):
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "q", rows_fn=lambda c, r: [ROW], exec_fn=lambda *a: None,
               loop=True)
        self.assertFalse(any("◆" in m for m in self.msgs))


class ScreenGauge(unittest.TestCase):
    def screen(self, **cfg_changes):
        cfg = dataclasses.replace(make_env(Path(tempfile.mkdtemp())), **cfg_changes)
        w = rb.ResourceWatcher(cfg, FakeRunner(strict=True), clock=lambda: 2.0)
        r = reading()
        w.latest = dataclasses.replace(r, running=19, limit=16, count_at=1.0)
        s = CursesScreen.__new__(CursesScreen)
        s.cfg, s.resource_watcher = cfg, w
        return s

    def test_gauge_function_for_the_main_list(self):
        s = self.screen()
        g = s._gauge([ROW, ROW])
        self.assertIn("執行中 19/16!", rb.gauge_text(g(120)))  # limits.count's number, not the two rows

    def test_an_old_or_missing_count_falls_back_to_the_rows_and_says_so(self):
        s = self.screen()
        s.resource_watcher.latest = dataclasses.replace(s.resource_watcher.latest, count_at=2.0 - rb.COUNT_FRESH_S - 1)
        self.assertIn("執行中 2?/16", rb.gauge_text(s._gauge([ROW, ROW])(120)))
        s.resource_watcher.latest = dataclasses.replace(s.resource_watcher.latest, running=None, count_at=None)
        self.assertIn("執行中 2?/16", rb.gauge_text(s._gauge([ROW, ROW])(120)))

    def test_off_in_config_or_without_a_watcher(self):
        self.assertIsNone(self.screen(resource_bar=False)._gauge([ROW]))
        s = self.screen()
        s.resource_watcher = None
        self.assertIsNone(s._gauge([ROW]))


if __name__ == "__main__":
    unittest.main()


class HelpLegend(unittest.TestCase):
    def test_the_page_explains_the_line_when_there_is_room_and_nothing_breaks_when_there_is_not(self):
        from sampan.ui.help import help_frame
        big = ["".join(t for t, _s in r) for r in help_frame(160, 40)]
        self.assertTrue(any("右下角的資源列" in r for r in big))
        self.assertTrue(any("總燈：只看記憶體" in r for r in big))
        small = ["".join(t for t, _s in r) for r in help_frame(80, 12)]
        self.assertEqual(len(small), 12)
