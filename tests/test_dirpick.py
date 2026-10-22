"""State machine for picking a directory (DirPicker), recently used directories, and its pure screen (render_dirpick). Everything uses temp directories; the real home directory is never touched."""
import os
import tempfile
import unittest
from pathlib import Path

from sampan.registry import Row
from sampan.ui import frame_text, render_dirpick, text_width
from sampan.ui.dirpick import DirPicker, recent_dirs, sessions_by_dir


class Tree:
    """A fake home directory tree inside a temp directory: home/Projects/{proj-a,webshop,ops-tools,zeta-sampan,.secret} and home/monitor."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._tmp.name)
        self.projects = os.path.join(self.home, "Projects")
        for d in ("proj-a", "webshop", "Xtools", "zeta-sampan", ".secret"):
            os.makedirs(os.path.join(self.projects, d))
        os.makedirs(os.path.join(self.home, "monitor"))
        Path(self.projects, "notes.txt").write_text("x")

    def close(self):
        self._tmp.cleanup()

    def p(self, *parts):
        return os.path.join(self.home, *parts)


class DirPickerTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.close)

    def dp(self, start=None, **kw):
        kw.setdefault("home", self.t.home)
        return DirPicker(start or self.t.projects, **kw)

    def names(self, dp):
        return [e for e in dp.entries]

    def keys(self, dp, *keys):
        res = None
        for k in keys:
            res = dp.key(k)
        return res

    def test_lists_subdirs_sorted_case_insensitive_without_hidden_and_files(self):
        dp = self.dp()
        self.assertEqual(dp.entries, ["..", "proj-a", "webshop", "Xtools", "zeta-sampan"])
        self.assertEqual(dp.cursor, 0)

    def test_dot_toggles_hidden(self):
        dp = self.dp()
        dp.key(".")
        self.assertIn(".secret", dp.entries)
        dp.key(".")
        self.assertNotIn(".secret", dp.entries)

    def test_no_dotdot_at_root(self):
        dp = DirPicker("/", home=self.t.home)
        self.assertNotIn("..", dp.entries)

    def test_enter_and_right_go_into_dir_left_goes_up(self):
        dp = self.dp()
        self.keys(dp, "KEY_DOWN", "KEY_RIGHT")
        self.assertEqual(dp.cwd, self.t.p("Projects", "proj-a"))
        self.assertEqual(dp.entries, [".."])
        dp.key("KEY_LEFT")
        self.assertEqual(dp.cwd, self.t.projects)
        self.assertEqual(dp.entries[dp.cursor], "proj-a")  # the cursor stays on the one that just appeared
        self.keys(dp, "KEY_DOWN", "\n")
        self.assertEqual(dp.cwd, self.t.p("Projects", "webshop"))
        dp.key("KEY_BACKSPACE")
        self.assertEqual(dp.cwd, self.t.projects)

    def test_enter_on_dotdot_goes_up(self):
        dp = self.dp()
        self.assertIsNone(dp.key("\n"))
        self.assertEqual(dp.cwd, self.t.home)

    def test_space_chooses_current_dir(self):
        dp = self.dp()
        dp.key("KEY_DOWN")
        self.assertEqual(dp.key(" "), ("done", self.t.projects))

    def test_escape_cancels(self):
        self.assertEqual(self.dp().key("\x1b"), ("cancel", None))

    def test_choosing_home_asks_first(self):
        dp = self.dp(self.t.home)
        self.assertIsNone(dp.key(" "))
        self.assertEqual(dp.prompt, "~ 是家目錄，建議用專案目錄。還是要用 ~？(y/N)")
        self.assertIsNone(dp.key("n"))
        self.assertIsNone(dp.prompt)
        dp.key(" ")
        self.assertEqual(dp.key("y"), ("done", self.t.home))

    def test_plus_makes_folder_and_enters_it(self):
        dp = self.dp()
        self.keys(dp, "+", "新", "專", "案", "\n")
        new = self.t.p("Projects", "新專案")
        self.assertTrue(os.path.isdir(new))
        self.assertEqual(dp.cwd, new)
        self.assertIsNone(dp.prompt)

    def test_plus_rejects_bad_names(self):
        for typed, msg in (([], "已取消"), (["a", "/", "b"], "不能含 /"), (["."], "不能用"), ([".", "."], "不能用"),
                           (list("webshop"), "已經有")):
            dp = self.dp()
            self.keys(dp, "+", *typed, "\n")
            self.assertEqual(dp.cwd, self.t.projects, typed)
            self.assertIn(msg, dp.message or "", typed)

    def test_plus_typing_dot_does_not_toggle_hidden_and_esc_only_leaves_input(self):
        dp = self.dp()
        self.keys(dp, "+", ".", "x", "\x7f", "\x1b")
        self.assertIsNone(dp.prompt)
        self.assertNotIn(".secret", dp.entries)
        self.assertEqual(dp.key("KEY_DOWN"), None)  # still picking a directory

    def test_mkdir_error_is_a_message(self):
        def boom(path):
            raise PermissionError("denied")
        dp = self.dp(mkdir=boom)
        self.keys(dp, "+", "x", "\n")
        self.assertIn("建立失敗", dp.message)
        self.assertEqual(dp.cwd, self.t.projects)

    def test_slash_types_a_path(self):
        dp = self.dp()
        res = self.keys(dp, "/", *"~/monitor", "\n")
        self.assertEqual(res, ("done", self.t.p("monitor")))

    def test_slash_relative_path_is_from_current_dir(self):
        dp = self.dp()
        self.assertEqual(self.keys(dp, "/", *"webshop", "\n"), ("done", self.t.p("Projects", "webshop")))

    def test_slash_missing_dir_for_new_stays_with_message(self):
        dp = self.dp()
        self.assertIsNone(self.keys(dp, "/", *"~/nope", "\n"))
        self.assertIn("目錄不存在", dp.message)

    def test_slash_missing_dir_allowed_for_move(self):
        dp = self.dp(allow_missing=True)
        self.assertEqual(self.keys(dp, "/", *"~/nope", "\n"), ("done", self.t.p("nope")))

    def test_slash_home_still_confirms(self):
        dp = self.dp()
        self.assertIsNone(self.keys(dp, "/", "~", "\n"))
        self.assertIn("家目錄", dp.prompt)

    def test_unreadable_dir_stays_in_parent(self):
        real = os.listdir

        def listdir(path):
            if path.endswith("webshop"):
                raise PermissionError("denied")
            return real(path)
        dp = self.dp(listdir=listdir)
        self.keys(dp, "KEY_DOWN", "KEY_DOWN", "\n")
        self.assertEqual(dp.cwd, self.t.projects)
        self.assertIn("讀不到", dp.message)

    def test_tab_switches_to_recent_and_enter_chooses_it(self):
        recent = [self.t.p("monitor"), self.t.p("Projects", "webshop")]
        dp = self.dp(recent=recent)
        dp.key("\t")
        self.assertEqual(dp.focus, "recent")
        dp.key("KEY_DOWN")
        self.assertEqual(dp.key("\n"), ("done", self.t.p("Projects", "webshop")))
        dp = self.dp(recent=recent)
        self.keys(dp, "\t", "\t")
        self.assertEqual(dp.focus, "list")
        dp.key("\t")
        self.assertEqual(dp.key(" "), ("done", self.t.p("monitor")))

    def test_tab_without_recent_stays_on_list(self):
        dp = self.dp()
        dp.key("\t")
        self.assertEqual(dp.focus, "list")

    def test_note_names_sessions_in_cursor_or_current_dir(self):
        sessions = {self.t.p("Projects", "proj-a"): ["proj-a-handy", "proj-a-web"], self.t.projects: ["ops"]}
        dp = self.dp(sessions=sessions)
        self.assertEqual(dp.note(), "Projects 裡已有 session：ops")
        dp.key("KEY_DOWN")
        self.assertEqual(dp.note(), "proj-a 裡已有 session：proj-a-handy, proj-a-web")
        dp.key("KEY_DOWN")
        self.assertEqual(dp.note(), "Projects 裡已有 session：ops")

    def test_start_dir_missing_falls_back_to_home(self):
        dp = self.dp(self.t.p("gone"))
        self.assertEqual(dp.cwd, self.t.home)


class RecentTest(unittest.TestCase):
    def test_distinct_existing_cwds_by_count_then_recency(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = (os.path.join(tmp, x) for x in "abc")
            for d in (a, b, c):
                os.mkdir(d)
            rows = [Row("1", "claude", "p", "i", a, seen_at="2026-10-01T10:00"),
                    Row("2", "claude", "p", "i", b, seen_at="2026-10-02T10:00"),
                    Row("3", "claude", "p", "i", b, seen_at="2026-09-01T10:00"),
                    Row("4", "claude", "p", "i", c, seen_at="2026-10-05T10:00"),
                    Row("5", "claude", "p", "i", os.path.join(tmp, "gone"), seen_at="2026-10-09T10:00")]
            self.assertEqual(recent_dirs(rows), [b, c, a])
            self.assertEqual(sessions_by_dir(rows)[b], ["2", "3"])


class RecentMixedOffsetTest(unittest.TestCase):
    def test_old_taipei_and_new_utc_stamps_compare_by_instant(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = (os.path.join(tmp, x) for x in "ab")
            for d in (a, b):
                os.mkdir(d)
            # a: 2026-10-21 08:00 Taipei = 00:00 UTC (older); b: 01:00 UTC (newer) although its string sorts lower
            rows = [Row("1", "claude", "p", "i", a, seen_at="2026-10-21T08:00:00+08:00"),
                    Row("2", "claude", "p", "i", b, seen_at="2026-10-21T01:00:00+00:00")]
            self.assertEqual(recent_dirs(rows), [b, a])


class RenderDirpickTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.close)
        os.makedirs(self.t.p("Projects", "帳務整理-二〇二六"))
        self.dp = DirPicker(self.t.projects, home=self.t.home,
                            recent=[self.t.p("Projects", "proj-a"), self.t.p("Projects", "webshop"), self.t.p("monitor")],
                            sessions={self.t.p("Projects", "proj-a"): ["proj-a-handy"]})
        self.dp.key("KEY_DOWN")

    def frame(self, w, h):
        return frame_text(render_dirpick(self.dp, w, h))

    def test_wide_shows_list_header_recent_and_note(self):
        lines = self.frame(120, 16)
        text = "\n".join(lines)
        self.assertIn("~/Projects/", lines[0])
        self.assertIn("proj-a/", text)
        self.assertIn("│ 最近用過", text)
        self.assertIn("~/monitor", text)
        self.assertIn("proj-a 裡已有 session：proj-a-handy", text)
        self.assertIn("空白 選這個目錄", lines[-1])

    def test_narrow_shows_list_only(self):
        lines = self.frame(80, 16)
        text = "\n".join(lines)
        self.assertNotIn("│", text)
        self.assertNotIn("最近用過", text)
        self.assertIn("帳務整理-二〇二六/", text)

    def test_narrow_with_recent_focus_lists_recent(self):
        self.dp.key("\t")
        text = "\n".join(self.frame(80, 16))
        self.assertIn("最近用過", text)
        self.assertIn("~/monitor", text)

    def test_no_line_wider_than_terminal_and_tiny_sizes(self):
        self.dp.key("+")
        for w, h in ((120, 16), (100, 10), (99, 10), (80, 16), (40, 8), (10, 3), (3, 2), (1, 1), (0, 0)):
            lines = self.frame(w, h)
            self.assertLessEqual(len(lines), max(h, 0))
            for line in lines:
                self.assertLessEqual(text_width(line), w, (w, h, line))

    def test_cjk_name_keeps_bar_aligned(self):
        lines = [x for x in self.frame(120, 16) if "│" in x]
        cols = {text_width(x[:x.index("│")]) for x in lines}
        self.assertEqual(len(cols), 1)


class ShimTest(unittest.TestCase):
    def test_sampan_dirpick_still_reexports_the_moved_module(self):
        import sampan.dirpick
        import sampan.ui.dirpick
        self.assertIs(sampan.dirpick.DirPicker, sampan.ui.dirpick.DirPicker)
        self.assertIs(sampan.dirpick.recent_dirs, sampan.ui.dirpick.recent_dirs)
        self.assertIs(sampan.dirpick.tilde, sampan.ui.dirpick.tilde)


if __name__ == "__main__":
    unittest.main()


class ChooseDirWiringTest(unittest.TestCase):
    """How CursesScreen.choose_dir and cmd_ui's n/m use the directory picker (fake screen, temp home directory)."""

    def setUp(self):
        from tests.helpers import make_env
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_env(Path(os.path.realpath(self._tmp.name)))
        self.home = str(self.cfg.home)
        for d in ("Projects/proj-a", "Projects/webshop", "monitor"):
            os.makedirs(os.path.join(self.home, d))

    def screen(self, keys):
        from sampan.ui import CursesScreen
        from tests.fakes import FakeRunner
        from tests.test_ui_screen import FakeScr
        self.scr = FakeScr(keys)
        return CursesScreen(self.scr, self.cfg, FakeRunner())

    def test_choose_dir_returns_chosen_and_draws_picker(self):
        s = self.screen(["KEY_DOWN", "\n", " "])
        got = s.choose_dir(os.path.join(self.home, "Projects"), home=self.home)
        self.assertEqual(got, os.path.join(self.home, "Projects", "proj-a"))
        self.assertTrue(any("選工作目錄" in "\n".join(x) for x in self.scr.screens))

    def test_choose_dir_escape_and_ctrl_c_cancel(self):
        self.assertIsNone(self.screen(["\x1b"]).choose_dir(self.home, home=self.home))
        self.assertIsNone(self.screen([KeyboardInterrupt]).choose_dir(self.home, home=self.home))

    def ui(self, action, answers, chosen, rows_reg=()):
        from sampan.registry import write_rows
        from sampan.ui import cmd_ui
        from tests.fakes import FakeRunner, cp
        from tests.helpers import status_row
        if rows_reg:
            write_rows(self.cfg.registry_path, list(rows_reg), "2026-10-21T10:00", self.cfg.history_dir)
        self.calls, self.starts, msgs = [], [], []
        answers = iter(answers)

        def choose(start, *, allow_missing):
            self.starts.append((start, allow_missing))
            return chosen
        rows = [status_row("a")]
        cmd_ui(self.cfg, FakeRunner().on(["tmux", "-u", "has-session"], cp(rc=1)), msgs.append,
               lambda _p: next(answers), rows_fn=lambda c, r: rows, picker_fn=lambda rows, cur: action,
               choose_dir_fn=choose,
               new_fn=lambda cfg, run, out, name, target, cwd, **kw: self.calls.append(("new", name, target, cwd)) or 0,
               move_fn=lambda cfg, run, out, name, dest, **kw: self.calls.append(("move", name, dest)) or 0)
        self.msgs = msgs

    def test_new_uses_dir_picker_starting_in_projects(self):
        chosen = os.path.join(self.home, "Projects", "webshop")
        self.ui(("new", []), ["fresh", "", ""], chosen)
        self.assertEqual(self.starts, [(os.path.join(self.home, "Projects"), False)])
        self.assertEqual(self.calls, [("new", "fresh", "claude:alice", chosen)])

    def test_new_starts_at_configured_ui_start_dir(self):
        from tests.helpers import TOML, make_env
        self.cfg = make_env(Path(self.home).parent, TOML + '\n[ui]\nstart_dir = "~/monitor"\n')
        self.ui(("new", []), ["fresh", "", ""], None)
        self.assertEqual(self.starts, [(os.path.join(self.home, "monitor"), False)])

    def test_new_falls_back_to_home_when_configured_start_dir_is_gone(self):
        from tests.helpers import TOML, make_env
        self.cfg = make_env(Path(self.home).parent, TOML + '\n[ui]\nstart_dir = "~/nowhere"\n')
        self.ui(("new", []), ["fresh", "", ""], None)
        self.assertEqual(self.starts, [(self.home, False)])

    def test_new_starts_at_home_without_projects_and_cancel_does_nothing(self):
        import shutil
        shutil.rmtree(os.path.join(self.home, "Projects"))
        self.ui(("new", []), ["fresh", "", ""], None)
        self.assertEqual(self.starts, [(self.home, False)])
        self.assertEqual(self.calls, [])
        self.assertIn("已取消", self.msgs)

    def test_move_starts_in_session_cwd_and_still_confirms(self):
        cwd = os.path.join(self.home, "Projects", "proj-a")
        dest = os.path.join(self.home, "monitor")
        self.ui(("move", ["a"]), ["y"], dest, rows_reg=[Row("a", "claude", "alice", "x", cwd)])
        self.assertEqual(self.starts, [(cwd, True)])
        self.assertIn(f"將搬移：a → {Path(dest).resolve()}", "\n".join(self.msgs))
        self.assertEqual(self.calls, [("move", "a", str(Path(dest).resolve()))])

    def test_move_cancelled_in_confirm(self):
        self.ui(("move", ["a"]), ["n"], os.path.join(self.home, "monitor"))
        self.assertEqual(self.starts, [(os.path.join(self.home, "Projects"), True)])
        self.assertEqual(self.calls, [])

    def test_dir_context_from_registry(self):
        from sampan.registry import write_rows
        from sampan.ui import dir_context
        cwd = os.path.join(self.home, "Projects", "webshop")
        write_rows(self.cfg.registry_path, [Row("c1", "claude", "alice", "x", cwd, seen_at="2026-10-01"),
                                            Row("c2", "claude", "alice", "x", cwd, seen_at="2026-10-02")],
                   "2026-10-21T10:00", self.cfg.history_dir)
        ctx = dir_context(self.cfg)
        self.assertEqual(ctx["recent"], [cwd])
        self.assertEqual(ctx["sessions"], {cwd: ["c1", "c2"]})

    def test_curses_n_opens_dir_picker_then_new(self):
        from sampan.ui import curses_ui
        from tests.fakes import FakeRunner, cp
        from tests.test_ui_screen import FakeScr
        calls = []
        keys = ["n", "f", "\n", "\n", "\n", "KEY_DOWN", "KEY_RIGHT", " ", "q"]
        scr = FakeScr(keys)
        curses_ui(self.cfg, FakeRunner().on(["tmux", "-u", "has-session"], cp(rc=1)), lambda _l: None,
                  wrapper=lambda fn: fn(scr), rows_fn=lambda c, r: [],
                  new_fn=lambda cfg, run, out, name, target, cwd, **kw: calls.append((name, target, cwd)) or 2)
        self.assertEqual(calls, [("f", "claude:alice", os.path.join(self.home, "Projects", "proj-a"))])
        self.assertTrue(any("選工作目錄　~/Projects/" in "\n".join(x) for x in scr.screens))
