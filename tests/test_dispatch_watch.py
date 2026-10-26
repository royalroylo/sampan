import subprocess
import tempfile
import unittest
from pathlib import Path

from sampan import dispatch
from sampan.dispatch import Dispatch
from sampan.ui.actions import cancel_flow, cmd_ui
from sampan.ui.dispatch_watch import (
    INTERVAL_S,
    DispatchPicker,
    DispatchWatcher,
    dispatch_cells,
    dispatch_cols,
    dispatch_summary,
    make_dispatch_watcher,
)
from sampan.ui.render import render
from sampan.ui.screen import CursesScreen
from sampan.ui.state import Picker
from tests.fakes import FakeRunner
from tests.helpers import make_env
from tests.test_ui_screen import NOW, FakeScr, row

CREATED = "2026-10-26T12:00:00+00:00"


def mk(did, state="queued", to="bob", **kw):
    return Dispatch(id=did, from_name="alice", from_profile="p", from_agent="claude", to_name=to, to_agent="codex",
                    to_profile="q", text="hi", state=state, created=CREATED, **kw)


class SummaryTest(unittest.TestCase):
    def test_counts_only_queued_and_sent(self):
        items = [mk("a1"), mk("a2"), mk("a3", "sent"), mk("a4", "done"), mk("a5", "failed"), mk("a6", "cancelled")]
        self.assertEqual(dispatch_summary(items), "派工：2 排隊、1 等回覆（D 看清單）")

    def test_absent_when_nothing_pending(self):
        self.assertIsNone(dispatch_summary([]))
        self.assertIsNone(dispatch_summary([mk("a4", "done", notify=True)]))  # a notice owed is not shown

    def test_renders_on_the_screen_only_when_given(self):
        kw = dict(title="t", left=[], cursor=0, card=[], width=100, height=12)
        self.assertNotIn("派工：", "\n".join("".join(t for t, _ in line) for line in render(**kw)))
        shown = render(**kw, dispatch="派工：1 排隊、0 等回覆（D 看清單）")
        self.assertIn("派工：1 排隊、0 等回覆（D 看清單）", "".join(t for t, _ in shown[-2]))  # just above the key help


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.advanced = []

    def watcher(self, ids=("a1", "a2"), items=(), advance=None):
        def adv(cfg, run, did, *, sleep, **_kw):
            self.advanced.append((did, run))
            if advance:
                advance(did)
        return DispatchWatcher(self.cfg, FakeRunner(strict=True), advance_fn=adv, pending_fn=lambda cfg: list(ids),
                               load_fn=lambda d: list(items))

    def test_interval_is_three_seconds(self):
        self.assertEqual(INTERVAL_S, 3)

    def test_poll_once_advances_every_pending_id_and_publishes_items(self):
        items = [mk("a1")]
        w = self.watcher(items=items)
        w.poll_once()
        self.assertEqual([d for d, _ in self.advanced], ["a1", "a2"])
        self.assertEqual(w.items, items)

    def test_a_failing_advance_does_not_stop_the_others(self):
        def boom(did):
            if did == "a1":
                raise RuntimeError("x")
        w = self.watcher(advance=boom)
        w.poll_once()
        self.assertEqual([d for d, _ in self.advanced], ["a1", "a2"])

    def test_broken_pending_or_load_is_silent(self):
        w = DispatchWatcher(self.cfg, FakeRunner(strict=True), pending_fn=lambda c: 1 / 0,
                            load_fn=lambda d: (_ for _ in ()).throw(dispatch.DispatchError("bad")))
        w.items = [mk("a1")]
        w.poll_once()
        self.assertEqual(w.items, [])

    def test_commands_run_with_stdin_devnull(self):
        seen = []
        r = FakeRunner()
        w = DispatchWatcher(self.cfg, r, pending_fn=lambda c: ["a1"], load_fn=lambda d: [],
                            advance_fn=lambda cfg, run, did, **k: seen.append(run(["echo", "x"]) and run(["y"], stdin="in")))
        w.poll_once()
        self.assertEqual([c["stdin"] for c in r.calls], [subprocess.DEVNULL, "in"])  # an explicit stdin is kept

    def test_stop_before_start_is_harmless_and_make_watcher_builds_one(self):
        w = make_dispatch_watcher(self.cfg, FakeRunner(strict=True))
        w.stop()
        self.assertIsNone(w._thread)

    def test_stop_ends_the_round_between_ids(self):
        w = self.watcher(ids=("a1", "a2", "a3"), advance=lambda did: w._stop.set())
        w.poll_once()
        self.assertEqual([d for d, _ in self.advanced], ["a1"])

    def test_stop_waits_for_a_round_in_flight_and_says_so(self):  # final review 2
        import threading
        import time
        inside, release = threading.Event(), threading.Event()

        def slow(did):
            inside.set()
            release.wait(5)
        w = self.watcher(ids=("a1",), advance=slow)
        w.interval = 60
        w.start()
        self.assertTrue(inside.wait(5))
        notes = []
        threading.Timer(0.8, release.set).start()
        t0 = time.monotonic()
        w.stop(wait_s=5, notice=notes.append)
        self.assertGreaterEqual(time.monotonic() - t0, 0.7)  # it waited for the delivery to end
        self.assertFalse(w._thread.is_alive())
        self.assertEqual(notes, ["等派工送完再離開…"])

    def test_stop_wait_is_bounded(self):
        import threading
        import time
        inside, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def stuck(did):
            inside.set()
            release.wait(10)
        w = self.watcher(ids=("a1",), advance=stuck)
        w.start()
        self.assertTrue(inside.wait(5))
        t0 = time.monotonic()
        w.stop(wait_s=1)
        self.assertLess(time.monotonic() - t0, 3)


class ListTest(unittest.TestCase):
    def test_cells_use_state_label_and_reason(self):
        d = mk("ab12", "failed", reason="找不到回覆")
        cells = dispatch_cells(d)
        self.assertEqual(cells[0], "ab12")
        self.assertEqual(cells[1], "alice")
        self.assertEqual(cells[2], "bob")
        self.assertEqual(cells[3][0], "失敗")
        self.assertIn("找不到回覆", cells[-1])

    def test_cells_align_cjk(self):
        from sampan.ui.text import text_width
        items = [mk("ab12", to="小明"), mk("cd34", to="bob")]
        out = render(title="t", left=[dispatch_cells(d) for d in items], cursor=0, card=[], width=100, height=10,
                     cols=dispatch_cols(items), solo=True)
        lines = ["".join(t for t, _ in line) for line in out]
        a, b = next(x for x in lines if "ab12" in x), next(x for x in lines if "cd34" in x)
        self.assertEqual(text_width(a[:a.index("排隊")]), text_width(b[:b.index("排隊")]))

    def test_wide_list_keeps_id_and_reason_on_one_line(self):
        items = [mk("ab12", "failed", reason="目標不在執行中")]
        out = render(title="t", left=[dispatch_cells(d) for d in items], cursor=0, card=[], width=120, height=10,
                     cols=dispatch_cols(items), solo=True)
        line = next("".join(t for t, _ in x) for x in out if "ab12" in "".join(t for t, _ in x))
        self.assertIn("失敗", line)
        self.assertIn("目標不在執行中", line)
        self.assertNotIn("│", line)

    def test_picker_moves_cancels_and_goes_back(self):
        p = DispatchPicker([mk("a1"), mk("a2", "sent")])
        self.assertEqual(p.key("KEY_DOWN"), None)
        self.assertEqual(p.key("x"), ("cancel", ["a2"]))
        self.assertEqual(p.key("q"), ("back", []))
        self.assertEqual(p.key("\x1b"), ("back", []))
        p.reload([mk("a0"), mk("a1"), mk("a2", "sent")])
        self.assertEqual(p.items[p.cursor].id, "a2")  # the cursor stays on the same dispatch

    def test_picker_empty_ignores_row_keys(self):
        p = DispatchPicker([])
        self.assertIsNone(p.key("x"))
        self.assertEqual(p.key("q"), ("back", []))

    def test_main_picker_d_opens_the_dispatch_list(self):
        self.assertEqual(Picker([row("a")]).key("D"), ("dispatches", []))
        self.assertEqual(Picker([]).key("D"), ("dispatches", []))


class CancelFlowTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.out, self.cancelled = [], []

    def flow(self, d, answer):
        return cancel_flow(self.cfg, self.out.append, lambda p: answer, d,
                           cancel_fn=lambda cfg, did: self.cancelled.append(did) or mk(did, "cancelled"))

    def test_finished_cannot_be_cancelled_and_say_why(self):
        for state in ("done", "failed", "cancelled"):
            self.out.clear()
            self.assertEqual(self.flow(mk("a1", state), "y"), 1)
            self.assertIn("只能取消排隊中的", self.out[-1])
        self.assertEqual(self.cancelled, [])

    def test_sent_asks_to_stop_waiting(self):  # final review 4
        asked = []
        rc = cancel_flow(self.cfg, self.out.append, lambda p: asked.append(p) or "y", mk("a1", "sent"),
                         cancel_fn=lambda cfg, did: self.cancelled.append(did)
                         or mk(did, "cancelled", reason="不再等回覆"))
        self.assertEqual(rc, 0)
        self.assertIn("不再等派工 a1 的回覆", asked[0])
        self.assertIn("收不回", asked[0])
        self.assertEqual(self.cancelled, ["a1"])
        self.assertIn("不再等派工 a1 的回覆", self.out[-1])

    def test_asks_first_and_cancels_on_yes(self):
        asked = []
        rc = cancel_flow(self.cfg, self.out.append, lambda p: asked.append(p) or "y", mk("a1"),
                         cancel_fn=lambda cfg, did: self.cancelled.append(did) or mk(did, "cancelled"))
        self.assertEqual(rc, 0)
        self.assertIn("a1", asked[0])
        self.assertIn("bob", asked[0])
        self.assertEqual(self.cancelled, ["a1"])
        self.assertIn("已取消派工 a1", self.out[-1])

    def test_anything_but_yes_cancels_nothing(self):
        for answer in ("", "n", None, "x"):
            self.assertEqual(self.flow(mk("a1"), answer), 0)
        self.assertEqual(self.cancelled, [])

    def test_a_refusal_from_cancel_is_shown(self):
        rc = cancel_flow(self.cfg, self.out.append, lambda p: "y", mk("a1"), cancel_fn=lambda cfg, did: "派工 a1 已送出")
        self.assertEqual(rc, 1)
        self.assertIn("派工 a1 已送出", self.out[-1])


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def screen(self, keys, items=(), cancel_calls=None):
        self.scr = FakeScr(keys)
        self.items = list(items)
        s = CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW,
                         dispatch_watcher=type("W", (), {"items": self.items, "cfg": None})(),
                         dispatches_load=lambda state_dir: list(self.items))
        return s

    def shown(self):
        return "\n".join("\n".join(x) for x in self.scr.screens)

    def test_summary_line_on_the_main_list(self):
        s = self.screen(["q"], [mk("a1"), mk("a2", "sent")])
        s.pick([row("a")], None)
        self.assertIn("派工：1 排隊、1 等回覆（D 看清單）", self.scr.last())

    def test_no_line_without_pending(self):
        s = self.screen(["q"], [mk("a1", "done")])
        s.pick([row("a")], None)
        self.assertNotIn("派工：", self.scr.last())
        self.assertIn("D 派工", self.scr.last())  # the key is in the help line

    def test_empty_list_says_none(self):
        s = self.screen([])
        self.assertEqual(s.dispatches(ask=lambda p: None), 0)
        self.assertIn("沒有派工", s.messages)

    def test_list_shows_rows_and_x_on_done_explains(self):
        s = self.screen(["KEY_DOWN", "x", "q"], [mk("a1"), mk("a2", "done")])
        s.dispatches(ask=lambda p: self.fail("must not ask for a finished one"))
        self.assertIn("a1", self.shown())
        self.assertIn("已回覆", self.shown())
        self.assertIn("只能取消排隊中的", " ".join(s.messages))

    def test_x_on_queued_asks_then_cancels_and_refreshes(self):
        calls = []

        def cancel(cfg, did):
            calls.append(did)
            self.items[0] = mk(did, "cancelled")
            return self.items[0]
        s = self.screen(["x", "q"], [mk("a1")])
        s.cancel_fn = cancel
        s.dispatches(ask=lambda p: "y")
        self.assertEqual(calls, ["a1"])
        self.assertIn("已取消", self.shown())

    def test_help_line_with_q_fits_120_columns(self):
        s = self.screen(["q"])
        s.pick([row("a")], None)
        self.assertIn("q 離開", self.scr.screens[-1][-1])

    def test_cancel_returns_promptly_when_the_lock_is_held(self):
        import fcntl
        import os
        import time

        from sampan import send
        with dispatch.transaction(self.cfg.state_dir) as items:
            items.append(mk("a1"))
        fd = os.open(self.cfg.state_dir / "dispatches.lock", os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)  # another process is delivering
        try:
            t0 = time.monotonic()
            r = send.cancel(self.cfg, "a1", wait_s=0.2)
            took = time.monotonic() - t0
        finally:
            os.close(fd)
        self.assertEqual(r, "正在送出，稍後再試")
        self.assertLess(took, 2)
        self.assertEqual(dispatch.load(self.cfg.state_dir)[0].state, "queued")
        self.assertEqual(send.cancel(self.cfg, "a1", wait_s=0.2).state, "cancelled")  # free again

    def test_ui_d_key_runs_the_handler(self):
        called = []
        picks = iter([("dispatches", []), ("quit", [])])
        out = []
        rc = cmd_ui(self.cfg, FakeRunner(strict=True), out.append, lambda p: "", picker_fn=lambda rows, cur: next(picks),
                    rows_fn=lambda c, r: [row("a")], dispatches_fn=lambda **kw: called.append(1) or 0, loop=True)
        self.assertEqual(rc, 0)
        self.assertEqual(called, [1])


if __name__ == "__main__":
    unittest.main()
