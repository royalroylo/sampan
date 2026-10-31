"""Live preview on the right of the ui: tidying capture-pane text, render layout, and re-capturing every second while pick waits for a key."""
import tempfile
import unittest
from pathlib import Path

from sampan.registry import RegistryError
from sampan.status import State
from sampan.ui import (
    NO_SCREEN,
    SELF_SCREEN,
    CursesScreen,
    clean_capture,
    frame_text,
    preview_header,
    preview_lines,
    render,
    text_width,
)
from tests.fakes import FakeRunner, cp, pane_line
from tests.helpers import make_env, register
from tests.test_ui_screen import LEFT, NOW, TICK, FakeScr, row

HEADER = ["proj-a-handy（預覽）", "─" * 200, "執行中（idle）・claude・carol・context 93k", "~/Projects/proj-a/handy",
          "Enter 接上　s 停掉　m 搬　P 換 profile　e 改名　L 別名　r retire"]
SHOT = [f"line {i:02}" for i in range(40)] + ["⏺ 我已經把設定頁改成兩欄", "  ⎿ Updated src/settings.tsx", "> "]


def frame(width, height, **kw):
    args = dict(title="sampan", left=LEFT, cursor=0, card=HEADER, preview=SHOT, help="↑↓ 移動　q 離開",
                width=width, height=height)
    args.update(kw)
    return frame_text(render(**args))


class CleanCaptureTest(unittest.TestCase):
    def test_drops_trailing_blank_lines_and_trailing_spaces(self):
        self.assertEqual(clean_capture("a  \nb\n\n   \n\n"), ["a", "b"])

    def test_keeps_blank_lines_in_the_middle(self):
        self.assertEqual(clean_capture("a\n\nb\n"), ["a", "", "b"])

    def test_strips_control_characters(self):
        self.assertEqual(clean_capture("a\x1b[31mred\x07\r\x00\nz​"), ["a[31mred", "z"])

    def test_tabs_become_spaces(self):
        self.assertEqual(clean_capture("a\tb"), ["a    b"])

    def test_empty_capture(self):
        self.assertEqual(clean_capture(""), [])
        self.assertEqual(clean_capture("\n\n"), [])
        self.assertEqual(clean_capture(None), [])


class PreviewLinesTest(unittest.TestCase):
    def test_shows_the_last_lines_that_fit(self):
        self.assertEqual([x.rstrip() for x in preview_lines(["a", "b", "c", "d"], 5, 2)], ["c", "d"])

    def test_pads_and_crops_to_width_without_splitting_wide_chars(self):
        out = preview_lines(["abc", "設定頁改成兩欄"], 5, 5)
        self.assertEqual(out, ["abc  ", "設定 "])
        self.assertTrue(all(text_width(x) == 5 for x in out))

    def test_zero_sizes(self):
        self.assertEqual(preview_lines(["a"], 0, 3), [])
        self.assertEqual(preview_lines(["a"], 3, 0), [])
        self.assertEqual(preview_lines([], 3, 3), [])


class HeaderTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_compact_header(self):
        info = {"cwd": str(self.cfg.home / "Projects/proj-a/handy"), "panes": 1, "windows": 1, "quote": "幫我改設定頁"}
        lines = [_text(x) for x in preview_header(self.cfg, row("proj-a-handy"), info)]
        self.assertEqual(lines[0], "proj-a-handy（預覽）")
        self.assertEqual(lines[2], "執行中（idle）・claude・carol・context 93k")
        self.assertEqual(lines[3], "~/Projects/proj-a/handy")
        self.assertEqual(lines[4], "Enter 接上　s 停掉　S 重開　m 搬　P 換 profile　e 改名　L 別名　r retire")
        self.assertNotIn("幫我改設定頁", "".join(lines))  # the last message isn't included: the preview shows it

    def test_codex_header_has_no_profile_or_context(self):
        lines = preview_header(self.cfg, row("cx", agent="codex", profile="-", agent_status="working", tokens=None),
                               {"cwd": "/tmp/x", "panes": 1})
        self.assertEqual(_text(lines[2]), "執行中（working）・codex")
        self.assertEqual(lines[2][0][1], "ok")


def _text(line):
    return "".join(t for t, _s in line) if isinstance(line, tuple) else str(line)


class RenderPreviewTest(unittest.TestCase):
    def test_wide_shows_header_separator_and_last_lines_of_the_pane(self):
        lines = frame(120, 30)
        right = [line.split(" │ ", 1)[1].rstrip() if " │ " in line else None for line in lines]
        self.assertEqual(right[1], "proj-a-handy（預覽）")
        self.assertEqual(right[3], "執行中（idle）・claude・carol・context 93k")
        self.assertEqual(right[6].strip("─"), "")
        self.assertIn("> ", [x for x in right if x is not None][-1] + " ")
        body = [x for x in right if x is not None]
        self.assertEqual(body[-1], ">")
        self.assertEqual(body[-3], "⏺ 我已經把設定頁改成兩欄")
        self.assertNotIn("line 00", "\n".join(lines))
        self.assertEqual(len(lines), 30)

    def test_preview_never_exceeds_width(self):
        wide = ["設定頁" * 80, "x" * 300]
        for w, h in ((120, 30), (100, 12), (99, 30), (80, 30), (40, 10)):
            for line in frame(w, h, preview=wide):
                self.assertLessEqual(text_width(line), w, (w, h, line))

    def test_narrow_puts_header_and_preview_below_the_list(self):
        lines = frame(80, 30)
        self.assertFalse(any("│" in line for line in lines))
        name = next(i for i, line in enumerate(lines) if "tb-task" in line)
        head = next(i for i, line in enumerate(lines) if line.startswith("proj-a-handy（預覽）"))
        last = next(i for i, line in enumerate(lines) if line.startswith("⏺ 我已經"))
        self.assertLess(name, head)
        self.assertLess(head, last)
        self.assertGreaterEqual(sum(1 for line in lines[head:] if line.startswith("line ")), 3)

    def test_narrow_and_small_shows_at_least_the_header(self):
        lines = frame(80, 12)
        self.assertTrue(any(line.startswith("proj-a-handy（預覽）") for line in lines))

    def test_tiny_sizes_do_not_crash(self):
        for w, h in ((0, 0), (1, 1), (5, 2), (10, 3), (30, 4), (120, 3), (3, 40), (99, 5), (100, 5)):
            lines = frame(w, h)
            self.assertLessEqual(len(lines), max(h, 0))
            self.assertTrue(all(text_width(line) <= w for line in lines))

    def test_without_preview_the_card_is_unchanged(self):
        self.assertEqual(frame(120, 30, preview=None), frame_text(render(
            title="sampan", left=LEFT, cursor=0, card=HEADER, help="↑↓ 移動　q 離開", width=120, height=30)))


class PickPreviewTest(unittest.TestCase):
    """While pick waits for a key, re-capture the cursor row's screen every second (TICK); re-read the list every 5 seconds; capture immediately on movement."""

    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.t = [0.0]
        self.r = FakeRunner()
        self.r.on(["tmux", "-u", "list-panes"], cp(out="\n".join([
            pane_line("a", "%1", 10, "claude", "/tmp/a"), pane_line("b", "%2", 11, "claude", "/tmp/b")]) + "\n"))
        self.shots = {"%1": iter(f"a screen {i}\n" for i in range(100)),
                      "%2": iter(f"b screen {i}\n" for i in range(100))}
        self.r.on(["tmux", "-u", "capture-pane"], self.capture)
        self.rows_calls = 0

    def capture(self, args):
        target = args[args.index("-t") + 1]
        self.assertNotIn("-e", args)
        if target not in self.shots:
            return cp(rc=1, err="can't find pane")
        return cp(out=next(self.shots[target]))

    def tick(self):
        self.t[0] += 1.0

    def rows_fn(self, cfg, run):
        self.rows_calls += 1
        return self.rows_now

    def screen(self, keys, h=30, w=120):
        self.scr = FakeScr(keys, h=h, w=w, on_tick=self.tick)
        return CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW, clock=lambda: self.t[0],
                            rows_fn=self.rows_fn)

    def captures(self):
        return [c["args"][c["args"].index("-t") + 1] for c in self.r.called(["tmux", "-u", "capture-pane"])]

    def rows(self):
        return [row("a", tokens=None, pane="%1"), row("b", pane="%2"),
                row("m", state=State.STOPPED, agent_status="-")]

    def test_ticks_recapture_only_the_cursor_row(self):
        self.rows_now = self.rows()
        s = self.screen([TICK, TICK, "q"])
        self.assertEqual(s.pick(self.rows_now, None), ("quit", []))
        self.assertEqual(self.captures(), ["%1", "%1", "%1"])
        self.assertIn("a screen 2", "\n".join(self.scr.screens[-1]))
        self.assertIn(1000, self.scr.timeouts)
        self.assertEqual(self.scr.timeouts[-1], -1)  # after leaving the list, ask/checklist wait for keys as usual

    def test_moving_captures_the_new_row_immediately(self):
        self.rows_now = self.rows()
        s = self.screen(["KEY_DOWN", "q"])
        s.pick(self.rows_now, None)
        self.assertEqual(self.captures(), ["%1", "%2"])
        self.assertIn("b screen 0", "\n".join(self.scr.screens[-1]))

    def test_row_without_tmux_session_shows_full_card_and_is_not_captured(self):
        self.rows_now = self.rows()
        s = self.screen(["KEY_DOWN", "KEY_DOWN", TICK, "q"])
        s.pick(self.rows_now, None)
        self.assertEqual(self.captures(), ["%1", "%2"])
        shown = "\n".join(self.scr.screens[-1])
        self.assertIn("已停止", shown)
        self.assertIn("Enter 接回", shown)
        self.assertNotIn("（預覽）", shown)

    def test_capture_failure_shows_note_and_keeps_going(self):
        rows = self.rows()
        rows[0].pane = "%9"
        self.rows_now = rows
        s = self.screen([TICK, "q"])
        self.assertEqual(s.pick(rows, None), ("quit", []))
        self.assertIn(NO_SCREEN, "\n".join(self.scr.screens[-1]))

    def test_falls_back_to_the_sessions_active_pane(self):
        rows = self.rows()
        rows[0].pane = None
        self.rows_now = rows
        self.screen(["q"]).pick(rows, None)
        self.assertEqual(self.captures(), ["=a:"])

    def test_sampans_own_pane_is_not_captured(self):
        rows = self.rows()
        self.rows_now = rows
        self.scr = FakeScr(["KEY_DOWN", "q"], on_tick=self.tick)
        s = CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW, clock=lambda: self.t[0],
                         rows_fn=self.rows_fn, own_pane="%1")
        s.pick(rows, None)
        self.assertEqual(self.captures(), ["%2"])
        self.assertIn(SELF_SCREEN, "\n".join(self.scr.screens[0]))

    def test_current_session_without_known_agent_pane_is_not_captured(self):
        rows = self.rows()
        rows[0].pane = None
        self.rows_now = rows
        self.screen(["q"]).pick(rows, "a")
        self.assertEqual(self.captures(), [])
        self.assertIn(SELF_SCREEN, "\n".join(self.scr.screens[-1]))

    def test_list_is_refreshed_every_five_seconds_keeping_cursor_and_marks(self):
        rows = self.rows()
        self.rows_now = rows
        s = self.screen([" ", "KEY_UP"] + [TICK] * 6 + ["\n"])
        # after the re-read: one more session sorts first, and b becomes busy
        self.rows_now = [row("0new"), row("a", tokens=None, pane="%1"),
                         row("b", agent_status="busy", pane="%2"), row("m", state=State.STOPPED, agent_status="-")]
        action = s.pick(rows, None)
        self.assertEqual(self.rows_calls, 1)
        self.assertEqual(action, ("attach", ["a"]))  # cursor is still on a
        self.assertEqual([r.name for r in rows], ["0new", "a", "b", "m"])  # cmd_ui got the new list
        shown = "\n".join(self.scr.screens[-1])
        self.assertIn("2 ● ◆ a", shown)  # tick preserved (● is the mark, ◆ the light)
        self.assertRegex(shown, r"◆ b +claude +carol +執行中 +執行中")  # b's light: running

    def test_refresh_error_keeps_old_rows(self):
        rows = self.rows()

        def boom(cfg, run):
            raise RegistryError("壞了")
        self.scr = FakeScr([TICK] * 6 + ["q"], on_tick=self.tick)
        s = CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW, clock=lambda: self.t[0], rows_fn=boom)
        self.assertEqual(s.pick(rows, None), ("quit", []))
        self.assertEqual(len(rows), 3)

    def test_tick_keeps_pending_stop_confirmation(self):
        self.rows_now = self.rows()
        s = self.screen(["s", TICK, "y"])
        self.assertEqual(s.pick(self.rows_now, None), ("stop!", ["a"]))

    def test_ask_and_checklist_ignore_ticks(self):
        s = self.screen(["帳", TICK, "務", "\n"])
        self.assertEqual(s.ask("名稱："), "帳務")
        s = self.screen([TICK, "\n"])
        self.assertEqual(s.restore_checklist([]), [])

    def test_narrow_terminal_shows_preview_below(self):
        self.rows_now = self.rows()
        s = self.screen(["q"], w=80)
        s.pick(self.rows_now, None)
        shown = self.scr.screens[-1]
        self.assertTrue(any(line.startswith("a（預覽）") for line in shown))
        self.assertIn("a screen 0", "\n".join(shown))

    def test_tiny_terminal_with_preview_does_not_crash(self):
        self.rows_now = self.rows()
        s = self.screen([TICK, "q"], h=3, w=6)
        self.assertEqual(s.pick(self.rows_now, None), ("quit", []))


class LightsPreviewTest(unittest.TestCase):
    """Lights on the list: a preview held SEEN_DWELL_S marks the row seen, v hides the preview (kept in ui.json)."""
    capture, rows_fn, screen, captures, rows = (PickPreviewTest.capture, PickPreviewTest.rows_fn,
                                                PickPreviewTest.screen, PickPreviewTest.captures, PickPreviewTest.rows)

    def setUp(self):
        PickPreviewTest.setUp(self)
        self.steps = []
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "seen.json").write_text("{}")  # not the first 0.14 run: no bootstrap
        register(self.cfg, "a", "b")

    def tick(self):
        self.t[0] += self.steps.pop(0) if self.steps else 1.0

    def lit_rows(self):
        from datetime import timedelta
        rows = self.rows()
        rows[0].last = NOW - timedelta(minutes=5)  # a finished, nobody has looked: 待看
        return rows

    def test_dwell_three_seconds_marks_seen(self):
        from unittest import mock

        from sampan import lights
        self.rows_now = self.lit_rows()
        self.steps = [2.9, 0.1, 1.0, 2.0, 1.0]
        calls = []

        def spy(state_dir, name, now):
            calls.append((name, now))
            lights.mark_seen(state_dir, name, now)
        with mock.patch("sampan.ui.screen.mark_seen", spy):
            s = self.screen([TICK, TICK, TICK, "KEY_DOWN", "KEY_UP", TICK, TICK, "q"])
            s.pick(self.rows_now, None)
        # t=2.9 nothing; t=3.0 once; t=4.0 same row, not again; away and back at t=4 restarts: t=6 no, t=7 yes
        self.assertEqual(calls, [("a", NOW), ("a", NOW)])
        self.assertIn("a", lights.load_seen(self.cfg.state_dir))
        first, after = "\n".join(self.scr.screens[0]), "\n".join(self.scr.screens[2])
        self.assertIn("待看", first)
        self.assertNotIn("待看", after)  # seen: the light goes back to 閒置 at once

    def test_no_screen_is_not_looking(self):
        from unittest import mock
        rows = self.lit_rows()
        rows[0].pane = "%9"  # capture fails: （看不到畫面）
        self.rows_now = rows
        with mock.patch("sampan.ui.screen.mark_seen") as spy:
            self.screen([TICK] * 4 + ["q"]).pick(self.rows_now, None)
        spy.assert_not_called()
        self.assertIn("待看", "\n".join(self.scr.screens[-1]))

    def test_preview_never_sends_keys(self):
        self.rows_now = self.lit_rows()
        s = self.screen([TICK, TICK, TICK, TICK, "KEY_DOWN", TICK, "v", "v", TICK, "q"])
        s.pick(self.rows_now, None)
        self.assertEqual(self.r.sent_keys(), [])
        tmux = {c["args"][2] for c in self.r.calls if c["args"][:2] == ["tmux", "-u"]}
        self.assertLessEqual(tmux, {"capture-pane", "list-panes", "list-windows", "list-sessions"}, tmux)
        self.assertIn("capture-pane", tmux)

    def test_v_hides_and_persists(self):
        import json
        self.rows_now = self.rows()
        s = self.screen(["v", "q"])
        self.assertEqual(s.pick(self.rows_now, None), ("quit", []))
        self.assertIn("a screen 0", "\n".join(self.scr.screens[0]))
        hidden = "\n".join(self.scr.screens[-1])
        self.assertNotIn("a screen", hidden)
        self.assertNotIn("（預覽）", hidden)  # the full card instead of the preview header
        self.assertEqual(json.loads((self.cfg.state_dir / "ui.json").read_text()), {"preview": False})
        n = len(self.captures())
        s = self.screen([TICK, "v", "q"])  # a new screen reads it back: no capture until v
        s.pick(self.rows_now, None)
        self.assertEqual(len(self.captures()), n + 1)
        self.assertEqual(json.loads((self.cfg.state_dir / "ui.json").read_text()), {"preview": True})

    def test_unreadable_ui_json_means_preview_on(self):
        (self.cfg.state_dir / "ui.json").write_text("{nope")
        self.rows_now = self.rows()
        self.screen(["q"]).pick(self.rows_now, None)
        self.assertEqual(self.captures(), ["%1"])

    def test_first_run_marks_every_running_session_seen(self):
        from sampan import lights
        (self.cfg.state_dir / "seen.json").unlink()
        self.rows_now = self.lit_rows()
        self.screen(["q"]).pick(self.rows_now, None)
        self.assertEqual(lights.load_seen(self.cfg.state_dir), {"a": NOW, "b": NOW})
        self.assertNotIn("待看", "\n".join(self.scr.screens[0]))

    def test_folder_row_counts_and_bang(self):
        from sampan import folders as F
        F.edit(self.cfg.state_dir, lambda t: (F.place_session(t, "a", "客戶"), F.place_session(t, "b", "客戶")))
        rows = self.lit_rows()
        rows[1].agent_status = "busy"
        self.rows_now = rows
        s = self.screen(["!", "q"])
        s.pick(self.rows_now, None)
        self.assertIn("▾ 客戶 (2)　1 等人 1 執行中", "\n".join(self.scr.screens[0]))
        self.assertIn("› 等人（1）", self.scr.screens[-1][0])


if __name__ == "__main__":
    unittest.main()
