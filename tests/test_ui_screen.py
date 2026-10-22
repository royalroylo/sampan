"""The ui screen: pure-function render, the right-hand info card, and the curses adapter layer (using a fake screen object, no real curses)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from sampan.registry import Row, write_rows
from sampan.restore import Candidate
from sampan.status import State
from sampan.ui import (
    CursesScreen,
    Wrap,
    card_lines,
    curses_ui,
    frame_text,
    gather_cards,
    list_state,
    render,
    row_keys,
    show_state,
    text_width,
)
from tests.fakes import FakeRunner, cp, pane_line
from tests.helpers import make_env, status_row

NOW = datetime(2026, 10, 20, 14, 17, tzinfo=timezone.utc)
SID = "b5768af2-0000-4000-8000-000000000001"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"


def row(name, state=State.RUNNING, profile="carol", agent_status="idle", tokens=93_000, agent="claude", sid=SID, last=None,
        detail="", pane=None):
    return status_row(name, state, agent=agent, profile=profile, agent_status=agent_status, tokens=tokens, sid=sid,
                      last=last, detail=detail, pane=pane)


LEFT = [("● proj-a-handy", "claude", "執行中", "idle"), ("  zeta-sampan", "claude", "執行中", "busy"),
        ("  003-帳務", "claude", "已停止", "-"), ("  tb-task", "codex", "執行中", "working")]
CARD = ["proj-a-handy", "─" * 80, "狀態     執行中（idle）", "目錄     ~/Projects/proj-a/handy", "",
        Wrap("「" + "幫我把 handy 的設定頁改成" * 12 + "」"), "", Wrap("Enter 接上　s 停掉　m 搬　e 改名　r retire")]


def frame(width, height, **kw):
    args = dict(title="sampan", left=LEFT, cursor=0, card=CARD, messages=["✔ proj-a-handy：已結束"],
                prompt=None, help="↑↓ 移動　q 離開", width=width, height=height)
    args.update(kw)
    return frame_text(render(**args))


class RenderTest(unittest.TestCase):
    def test_wide_puts_card_beside_list(self):
        lines = frame(120, 30)
        self.assertEqual(len(lines), 30)
        self.assertTrue(lines[0].startswith("═══ sampan "))
        self.assertIn("proj-a-handy", lines[1])
        self.assertIn("│ proj-a-handy", lines[1])
        self.assertIn("zeta-sampan", lines[2])
        self.assertIn("│ ───", lines[2])
        self.assertTrue(any("┴" in line for line in lines))
        self.assertIn("✔ proj-a-handy：已結束", lines[-2])
        self.assertTrue(lines[-1].lstrip().startswith("↑↓ 移動"))

    def test_bar_column_is_aligned_with_wide_characters(self):
        lines = frame(120, 30)
        cols = {self.bar_col(line) for line in lines[1:8]}
        self.assertEqual(len(cols), 1, lines[1:8])

    @staticmethod
    def bar_col(line):
        return text_width(line[:line.index("│")])

    def test_narrow_puts_card_below_list(self):
        lines = frame(80, 30)
        self.assertFalse(any("│" in line for line in lines))
        names = [i for i, line in enumerate(lines) if "tb-task" in line]
        card = [i for i, line in enumerate(lines) if line.startswith("proj-a-handy")]
        self.assertTrue(names and card)
        self.assertLess(names[0], card[0])

    def test_no_line_wider_than_terminal(self):
        for w, h in ((120, 30), (80, 30), (100, 12), (99, 12), (40, 10)):
            for line in frame(w, h, prompt="將停掉：帳務帳務帳務　確定？(y/N) _"):
                self.assertLessEqual(text_width(line), w, (w, h, line))

    def test_tiny_sizes_degrade_without_error(self):
        for w, h in ((0, 0), (1, 1), (5, 2), (10, 3), (30, 4), (120, 3), (3, 40)):
            lines = frame(w, h)
            self.assertLessEqual(len(lines), max(h, 0))
            self.assertTrue(all(text_width(line) <= w for line in lines))

    def test_quote_is_wrapped_and_truncated(self):
        lines = frame(120, 30)
        quote = [line.split("│ ", 1)[1].rstrip() for line in lines if "│ " in line and ("「" in line or "設定頁" in line)]
        self.assertGreaterEqual(len(quote), 2)
        self.assertLessEqual(len(quote), 3)
        self.assertTrue(quote[-1].endswith("…"))

    def test_messages_scroll_to_last_lines_and_prompt_shows(self):
        msgs = [f"line {i}" for i in range(50)]
        lines = frame(120, 30, messages=msgs, prompt="確定？(y/N) _")
        shown = [line.strip() for line in lines]
        self.assertIn("line 49", shown)
        self.assertNotIn("line 0", shown)
        self.assertIn("確定？(y/N) _", lines[-2])

    def test_list_scrolls_to_keep_cursor_visible(self):
        left = [(f"  s{i:02}", "claude", "執行中", "idle") for i in range(60)]
        lines = frame(120, 20, left=left, cursor=55)
        self.assertTrue(any("s55" in line for line in lines))

    def test_empty_list_shows_note(self):
        lines = frame(120, 20, left=[], card=[], empty="目前沒有任何 session（n 開新的）")
        self.assertIn("目前沒有任何 session", "\n".join(lines))

    def test_cursor_row_is_highlighted(self):
        segs = render(title="sampan", left=LEFT, cursor=1, card=CARD, width=120, height=30)
        self.assertEqual([s for s in segs[2] if s[1] == "sel"][0][0].split()[0], "zeta-sampan")


class CardTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def card(self, r, **info):
        info.setdefault("cwd", str(self.cfg.home / "Projects/proj-a/handy"))
        return [str(x) for x in card_lines(self.cfg, r, info, NOW)]

    def test_running_claude_card(self):
        lines = self.card(row("proj-a-handy", last=NOW - timedelta(minutes=3)), windows=1, panes=1, quote="幫我改設定頁")
        text = "\n".join(lines)
        self.assertEqual(lines[0], "proj-a-handy")
        for want in ("執行中（idle）", "claude ・ carol", "~/Projects/proj-a/handy", "b5768af2", "（3 分鐘前）",
                     "93k", "1 個視窗、1 個 pane", "最後一句話", "「幫我改設定頁」",
                     "Enter 接上　s 停掉　m 搬　e 改名　r retire"):
            self.assertIn(want, text)

    def test_stopped_card_says_stopped_and_offers_restore(self):
        text = "\n".join(self.card(row("m", state=State.STOPPED, agent_status="-"), windows=0, panes=0))
        self.assertIn("已停止", text)
        self.assertNotIn("該跑沒跑", text)
        self.assertNotIn("tmux ", text)
        self.assertIn("Enter 接回　e 改名　r retire", text)

    def test_codex_card_has_no_profile(self):
        text = "\n".join(self.card(row("cx", agent="codex", profile="", sid=TID), windows=1, panes=2))
        self.assertIn("agent    codex", text)
        self.assertNotIn("・", text)
        self.assertIn("1 個視窗、2 個 pane", text)

    def test_retired_card_offers_activate(self):
        text = "\n".join(self.card(row("old", state=State.RETIRED, agent_status="-"), windows=0, panes=0))
        self.assertIn("a activate", text)
        self.assertNotIn("Enter", text)

    def test_show_state_and_keys(self):
        self.assertEqual(show_state(row("x", State.STOPPED)), "已停止")
        self.assertEqual(show_state(row("x", State.RUNNING)), "執行中")
        self.assertEqual(show_state(row("x", State.AGENT_MISMATCH, detail="（登記 codex）")), "agent 不符（登記 codex）")
        self.assertEqual(list_state(row("x", State.STOPPED)), "已停止")
        self.assertEqual(list_state(row("x", State.NO_AGENT)), "沒有 agent")
        self.assertEqual(list_state(row("x", State.PROFILE_MISMATCH, detail="（登記 carol）")), "prof 不符")
        self.assertEqual(list_state(row("x", State.AGENT_MISMATCH, detail="（登記 codex）")), "agent 不符")
        self.assertEqual(row_keys(row("x", state=State.UNREGISTERED), {"panes": 1}), "Enter 接上")

    def test_gather_cards_reads_registry_tmux_and_last_message(self):
        cwd = self.cfg.home / "proj"
        cwd.mkdir()
        write_rows(self.cfg.registry_path, [Row("a", "claude", "alice", SID, str(cwd))], "t", self.cfg.history_dir)
        d = self.cfg.profiles["alice"].projects_dir / "-x"
        d.mkdir(parents=True)
        (d / f"{SID}.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-20T14:00:00Z",
                                                    "message": {"role": "user", "content": "幫我\n改  設定頁"}}) + "\n")
        r = FakeRunner()
        r.on(["tmux", "-u", "list-panes"], cp(out="\n".join([
            pane_line("a", "%1", 10, "claude", "/elsewhere"), pane_line("a", "%2", 11, "zsh", "/x"),
            pane_line("u", "%3", 12, "zsh", "/tmp/u")]) + "\n"))
        r.on(["tmux", "-u", "list-windows"], cp(out="a\na\nu\n"))
        cards = gather_cards(self.cfg, r, [row("a", profile="alice"), row("u", sid="")])
        self.assertEqual(cards["a"], {"cwd": str(cwd), "windows": 2, "panes": 2, "quote": "幫我 改 設定頁"})
        self.assertEqual(cards["u"]["cwd"], "/tmp/u")
        self.assertEqual(cards["u"]["quote"], "")


TICK = object()  # placing this in FakeScr's key sequence = get_wch gets no key (curses timeout)


class FakeScr:
    """Stand-in for a curses window: records the screen on each refresh, get_wch yields keys in order; TICK raises curses.error (timeout)."""

    def __init__(self, keys, h=30, w=120, on_tick=None):
        self.keys, self.h, self.w = list(keys), h, w
        self.cur, self.screens = {}, []
        self.timeouts, self.on_tick = [], on_tick

    def timeout(self, ms):
        self.timeouts.append(ms)

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.cur = {}

    def addstr(self, y, x, text, attr=0):
        self.cur.setdefault(y, []).append((x, text))

    def refresh(self):
        self.screens.append(["".join(t for _x, t in sorted(self.cur.get(y, []))) for y in range(self.h)])

    def keypad(self, flag):
        pass

    def get_wch(self):
        k = self.keys.pop(0)
        if k is TICK:
            if self.on_tick:
                self.on_tick()
            import curses
            raise curses.error("no input")
        if isinstance(k, type) and issubclass(k, BaseException):
            raise k
        return k

    def last(self):
        return "\n".join(self.screens[-1])


class CursesScreenTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.r = FakeRunner()

    def screen(self, keys, **kw):
        self.scr = FakeScr(keys, **kw)
        return CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW)

    def test_ask_reads_wide_chars_with_backspace(self):
        s = self.screen(["帳", "務", "x", "\x7f", "\n"])
        self.assertEqual(s.ask("名稱："), "帳務")
        self.assertTrue(any("名稱：帳務x_" in "\n".join(scr) for scr in self.scr.screens))
        self.assertIn("名稱：帳務", s.messages)

    def test_pick_shows_tmux_unreadable_warning(self):
        from sampan.status import Rows
        rows = Rows()
        rows.warning = "讀不到 tmux（Permission denied），狀態無法判斷"
        rows.append(row("one", State.UNKNOWN, profile="alice", agent_status="-", tokens=None, sid=""))
        s = self.screen(["q"])
        s.pick(rows, None)
        shown = "\n".join(self.scr.screens[0])
        self.assertIn("讀不到 tmux（Permission denied）", shown)
        self.assertNotIn("已停止", shown)

    def test_prompt_choice_defaults_to_going_in(self):
        from sampan.prompts import ATTACH, parse_prompt
        from tests.test_prompts import CODEX_TRUST
        p = parse_prompt(CODEX_TRUST, "codex")
        s = self.screen(["\n"])
        self.assertEqual(s.prompt_choice("001-proj", "codex", p), ATTACH)
        shown = "\n".join(self.scr.screens[0])
        self.assertIn(" ? 001-proj（codex）在問：Do you trust the contents of this directory?", shown)
        self.assertIn("   ● 進去自己操作", shown)
        self.assertIn("     1. Yes, continue", shown)
        self.assertIn("     先不管，之後再處理", shown)
        self.assertIn("↑↓ 選擇　Enter 送出　Esc 先不管", self.scr.screens[0][-1])
        self.assertEqual(s.extra, [])
        self.assertIn("  → 001-proj：進去自己操作", s.messages)
        self.assertNotIn("● 進去自己操作", self.scr.last())

    def test_prompt_choice_arrows_digits_escape(self):
        from sampan.prompts import SKIP, parse_prompt
        from tests.test_prompts import CLAUDE_TRUST, CODEX_UPDATE
        p = parse_prompt(CLAUDE_TRUST, "claude")
        self.assertEqual(self.screen(["KEY_DOWN", "KEY_DOWN", "\n"]).prompt_choice("x", "claude", p), 1)
        self.assertIn("   ● 2. Yes, I trust this folder", "\n".join(self.scr.screens[-2]))
        self.assertEqual(self.screen(["9", "3"]).prompt_choice("x", "codex", parse_prompt(CODEX_UPDATE, "codex")), 2)
        self.assertEqual(self.screen(["\x1b"]).prompt_choice("x", "codex", p), SKIP)
        self.assertEqual(self.screen(["KEY_UP"] + ["KEY_DOWN"] * 5 + ["\n"]).prompt_choice("x", "claude", p),
                         SKIP)  # cursor stays on the last one ("ignore for now")

    def test_prompt_choice_waits_without_ticking(self):
        from sampan.prompts import ATTACH, Prompt
        s = self.screen([TICK, "1", "\n"])
        self.assertEqual(s.prompt_choice("x", "codex", Prompt("codex", "", (), None, ("line a", "line b"))), ATTACH)
        self.assertEqual(self.scr.timeouts[0], -1)
        shown = "\n".join(self.scr.screens[0])
        self.assertIn("認不出", shown)
        self.assertIn("line b", shown)
        self.assertNotIn("1. ", shown)

    def test_ask_escape_cancels(self):
        s = self.screen(["a", "\x1b"])
        self.assertIsNone(s.ask("名稱："))
        self.assertIsNone(s.prompt)

    def test_ask_ctrl_c_propagates(self):
        s = self.screen([KeyboardInterrupt])
        with self.assertRaises(KeyboardInterrupt):
            s.ask("x")
        self.assertIsNone(s.prompt)

    def test_out_redraws_immediately(self):
        s = self.screen([])
        s.out("▶ 等待它結束…")
        self.assertIn("▶ 等待它結束…", self.scr.last())

    def test_pick_shows_list_card_and_confirms_stop_in_place(self):
        rows = [row("a", profile="alice"), row("m", state=State.STOPPED, agent_status="-")]
        s = self.screen(["KEY_DOWN", "s", "y"])
        s.out("上一個動作的訊息")
        self.assertEqual(s.pick(rows, None), ("stop!", ["m"]))
        shown = "\n".join(self.scr.screens[-1])
        self.assertIn("將停掉：m　確定？(y/N)", shown)
        self.assertIn("已停止", shown)
        self.assertIn("│ m", shown)  # info card for the cursor row
        self.assertEqual(s.messages, [])  # new action starts: message area is cleared
        first = self.scr.screens[-1][1]
        self.assertRegex(first, r"^ +a +claude alice +執行中 +idle +│")  # profile column follows agent, fields sit right against the name

    def test_cursor_stays_on_same_session_after_an_action(self):
        rows = [row("a"), row("b"), row("c")]
        s = self.screen(["KEY_DOWN", "KEY_DOWN", "w", "q"])
        self.assertEqual(s.pick(rows, "a"), ("save", []))
        s.pick([row("a"), row("c")], "a")
        self.assertIn("│ c", "\n".join(self.scr.screens[-1]))

    def test_pick_ctrl_c_quits(self):
        s = self.screen([KeyboardInterrupt])
        self.assertEqual(s.pick([row("a")], None), ("quit", []))

    def test_pick_quit_keeps_messages(self):
        s = self.screen(["q"])
        s.out("✔ 存好了")
        self.assertEqual(s.pick([row("a")], None), ("quit", []))
        self.assertEqual(s.messages, ["✔ 存好了"])

    def test_restore_checklist_shows_candidate_card(self):
        cands = [Candidate(Row("a", "claude", "alice", SID, str(self.cfg.home / "p")), NOW, 50_000, True, None),
                 Candidate(Row("b", "claude", "alice", SID, "/c"), None, None, False, "找不到逐字稿")]
        s = self.screen(["KEY_DOWN", " ", "\n"])
        self.assertEqual(s.restore_checklist(cands), ["a"])
        shown = "\n".join(self.scr.screens[-1])
        self.assertIn("[-] b", shown)
        self.assertIn("找不到逐字稿", shown)

    def test_adopt_checklist_cancel(self):
        c = NS(key="600", agent="claude", profile="alice", session_id=SID, cwd="/p", where="~/p", last=NOW,
               ago="3 分鐘前", text="修 bug", quote="「修 bug」", selected=True, skip=None)
        s = self.screen(["q"])
        self.assertIsNone(s.adopt_checklist([c]))
        self.assertIn("「修 bug」", "\n".join(self.scr.screens[-1]))

    def test_tiny_terminal_does_not_crash(self):
        s = self.screen(["q"], h=2, w=4)
        self.assertEqual(s.pick([row("a")], None), ("quit", []))


class CursesUiTest(unittest.TestCase):
    def test_attach_runs_while_sampan_waits_then_back_to_list(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        r = FakeRunner().on(["tmux", "-u", "has-session"], cp())
        events = []

        def wrapper(fn):
            rc = fn(FakeScr(["\n", "q"]))
            events.append("curses closed")
            return rc
        rc = curses_ui(cfg, r, lambda _l: None, wrapper=wrapper, rows_fn=lambda c, run: [row("a")],
                       exec_fn=lambda f, a: events.append(("exec", a)),
                       run_outside=lambda scr, args: events.append(("attach", args)) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(events, [("attach", ["tmux", "-u", "attach-session", "-t", "=a"]), "curses closed"])

    def test_quit_prints_last_messages_to_terminal(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        printed = []
        rc = curses_ui(cfg, FakeRunner(), printed.append, wrapper=lambda fn: fn(FakeScr(["w", "q"])),
                       rows_fn=lambda c, run: [row("a")], save_fn=lambda cfg, run, out, **kw: out("✔ 已存檔") or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(printed, ["✔ 已存檔"])


if __name__ == "__main__":
    unittest.main()


class ColorTest(unittest.TestCase):
    def styles(self, frame):
        return [(t.strip(), s) for segs in frame for t, s in segs if t.strip()]

    def test_list_state_and_agent_status_are_coloured_and_selection_keeps_colour(self):
        from sampan.ui import _list_cells, list_cols
        rows = [row("a"), row("b", state=State.STOPPED, agent_status="-"), row("c", agent_status="busy")]
        left = [_list_cells(r, set(), None) for r in rows]
        st = self.styles(render(title="sampan", left=left, cursor=0, card=[], cols=list_cols(rows), width=120, height=10))
        self.assertIn(("執行中", "sel+ok"), st)
        self.assertIn(("idle", "sel+ok"), st)
        self.assertIn(("已停止", "muted"), st)
        self.assertIn(("busy", "warn"), st)

    def test_profile_column_after_agent(self):
        from sampan.ui import _list_cells, list_cols
        rows = [row("a", profile="carol"), row("b", profile="work-team-1"), row("cx", agent="codex", profile="-")]
        self.assertEqual(_list_cells(rows[0], set(), None)[:3], ("  a", "claude", "carol"))
        self.assertEqual(_list_cells(rows[2], set(), None)[2], "-")
        self.assertEqual(list_cols(rows), (6, 11, 10, 7))
        self.assertEqual(list_cols([row("a", profile="x" * 30)])[1], 14)
        self.assertEqual(list_cols([row("cx", agent="codex", profile="-")])[1], 1)
        self.assertEqual(list_cols([]), (6, 1, 10, 7))
        left = [_list_cells(r, set(), None) for r in rows]
        lines = frame_text(render(title="sampan", left=left, cursor=0, card=[], cols=list_cols(rows), width=120,
                                  height=8))
        self.assertIn("work-team-1", lines[2])
        self.assertLess(lines[2].index("claude"), lines[2].index("work-team-1"))
        self.assertLess(lines[2].index("work-team-1"), lines[2].index("執行中"))

    def test_name_column_is_capped_so_columns_sit_next_to_names(self):
        from sampan.ui import _list_cells, list_cols
        rows = [row("a"), row("003-帳務"), row("zeta-sampan")]
        left = [_list_cells(r, set(), None) for r in rows]
        for width in (90, 120, 200):  # 90: info card below, list takes the full row width
            with self.subTest(width=width):
                lines = frame_text(render(title="sampan", left=left, cursor=0, card=[], cols=list_cols(rows),
                                          width=width, height=8))[1:4]
                cols = {text_width(line[:line.index("claude")]) for line in lines}
                self.assertEqual(cols, {text_width("   zeta-sampan") + 2 + 1}, lines)  # name column header (indented) + 2 cells + column gap
                self.assertTrue(all(text_width(line) == width for line in lines))

    def test_capped_list_still_degrades_at_tiny_widths(self):
        from sampan.ui import _list_cells, list_cols
        rows = [row("a-very-long-session-name"), row("b")]
        left = [_list_cells(r, set(), None) for r in rows]
        for width in (10, 30, 45):
            for line in frame_text(render(title="sampan", left=left, cursor=0, card=[], cols=list_cols(rows),
                                          width=width, height=6)):
                self.assertLessEqual(text_width(line), width)

    def test_card_labels_state_and_keys(self):
        from sampan.ui import Keys, _label
        from sampan.ui.cards import StateLine
        card = ["a", "─" * 80, StateLine(_label("狀態", "執行中（idle）"), State.RUNNING), _label("最後活動", "10/20 22:14"), "", Keys("Enter 接上")]
        st = self.styles(render(title="sampan", left=[], cursor=0, card=card, width=120, height=12))
        self.assertIn(("a", "head"), st)
        self.assertIn(("狀態", "label"), st)
        self.assertIn(("執行中（idle）", "ok"), st)
        self.assertIn(("最後活動", "label"), st)
        self.assertIn(("Enter 接上", "key"), st)

    def test_messages_coloured_by_mark_and_widths_still_fit(self):
        msgs = ["✔ a：已結束", "⚠ b：忙碌中", "❌ 失敗", "▶ c：已送出 /exit", "一般訊息"]
        frame = render(title="sampan", left=[], cursor=0, card=[], messages=msgs, width=80, height=20)
        st = dict(self.styles(frame))
        self.assertEqual([st[m] for m in msgs], ["ok", "warn", "err", "info", ""])
        from sampan.ui import text_width
        for line in frame_text(frame):
            self.assertLessEqual(text_width(line), 80)



class TitleVersionTest(unittest.TestCase):
    def test_title_shows_running_version(self):
        from sampan.ui import screen_title

        class U:
            current = "0.5.1"
        self.assertEqual(screen_title(U()), "sampan 0.5.1")

    def test_unknown_version_is_just_sampan(self):
        from sampan.ui import screen_title

        class U:
            current = "unknown"
        self.assertEqual(screen_title(U()), "sampan")
