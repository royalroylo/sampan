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
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import TOML, make_env, status_row

NOW = datetime(2026, 10, 20, 14, 17, tzinfo=timezone.utc)
SID = "b5768af2-0000-4000-8000-000000000001"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
TOML_NO_CODEX = TOML + "\n[codex]\nenabled = false\n"


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


class MessageWrapTest(unittest.TestCase):
    def test_long_message_wraps_with_style_and_indent(self):
        msg = "⚠ alice-team 沒有裝：" + "字" * 91  # 200 cells
        segs = render(title="sampan", left=LEFT, cursor=0, card=CARD, messages=[msg], prompt=None, help="h", width=80, height=30)
        got = [seg for seg in segs if "字" in "".join(t for t, _ in seg)]
        text = ["".join(t for t, _ in seg) for seg in got]
        self.assertEqual(len(got), 3)
        self.assertTrue(all(st == "warn" for seg in got for _, st in seg if _.strip()))
        self.assertTrue(all(t.startswith("   字") for t in text[1:]))  # 1 gutter + 2 indent
        self.assertEqual("".join(text).count("字"), 91)

    def test_long_newest_message_keeps_its_head_cut_with_ellipsis(self):
        msgs = ["⚠ HEAD" + "字" * 300]
        lines = frame_text(render(title="sampan", left=LEFT, cursor=0, card=CARD, messages=msgs, prompt=None, help="h",
                                  width=60, height=12))
        shown = [t for t in lines if "字" in t or "HEAD" in t]
        self.assertIn("HEAD", shown[0])
        self.assertTrue(shown[-1].rstrip().endswith("…"))

    def test_continuation_indent_is_two_cells_when_wrap_breaks_at_a_space(self):
        from sampan.ui.text import text_width
        head = "⚠ "
        msgs = [head + "a" * (79 - text_width(head)) + " " + "Z" * 20]  # first line fills width-1; the space overflows
        segs = render(title="sampan", left=LEFT, cursor=0, card=CARD, messages=msgs, prompt=None, help="h", width=80,
                      height=30)
        text = ["".join(t for t, _ in seg) for seg in segs if "Z" in "".join(t for t, _ in seg)]
        self.assertEqual(len(text), 1)
        self.assertTrue(text[0].startswith("   Z"), text[0])  # gutter + exactly two spaces
        self.assertFalse(text[0].startswith("    Z"), text[0])

    def test_wrapped_lines_count_against_the_height_limit(self):
        msgs = ["⚠ " + "字" * 100] * 3
        lines = frame_text(render(title="sampan", left=LEFT, cursor=0, card=CARD, messages=msgs, prompt=None, help="h",
                                  width=80, height=12))
        self.assertEqual(len(lines), 12)
        self.assertLessEqual(sum(1 for t in lines if "字" in t), 4)  # height // 3


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
                     "Enter 接上　s 停掉　S 重開　m 搬　P 換 profile　e 改名　r retire"):
            self.assertIn(want, text)

    def test_stopped_card_says_stopped_and_offers_restore(self):
        text = "\n".join(self.card(row("m", state=State.STOPPED, agent_status="-"), windows=0, panes=0))
        self.assertIn("已停止", text)
        self.assertNotIn("該跑沒跑", text)
        self.assertNotIn("tmux ", text)
        self.assertIn("Enter 接回　S 背景接回　P 換 profile　e 改名　r retire　X 移除", text)

    def test_codex_card_has_no_profile(self):
        text = "\n".join(self.card(row("cx", agent="codex", profile="", sid=TID), windows=1, panes=2))
        self.assertIn("agent    codex", text)
        self.assertNotIn("・", text)
        self.assertIn("1 個視窗、2 個 pane", text)

    def test_retired_card_offers_activate(self):
        text = "\n".join(self.card(row("old", state=State.RETIRED, agent_status="-"), windows=0, panes=0))
        self.assertIn("a activate", text)
        self.assertNotIn("Enter", text)

    def test_card_keys_show_X_for_stopped_and_retired(self):
        self.assertEqual(row_keys(row("x", state=State.STOPPED), {"panes": 0}),
                         "Enter 接回　S 背景接回　P 換 profile　e 改名　r retire　X 移除")
        self.assertEqual(row_keys(row("x", state=State.RETIRED), {"panes": 0}), "a activate　X 移除")
        self.assertNotIn("X 移除", row_keys(row("x", state=State.RUNNING), {"panes": 1}))

    def test_show_state_and_keys(self):
        self.assertEqual(show_state(row("x", State.STOPPED)), "已停止")
        self.assertEqual(show_state(row("x", State.RUNNING)), "執行中")
        self.assertEqual(show_state(row("x", State.AGENT_MISMATCH, detail="（登記 codex）")), "agent 不符（登記 codex）")
        self.assertEqual(list_state(row("x", State.STOPPED)), "已停止")
        self.assertEqual(list_state(row("x", State.NO_AGENT)), "沒有 agent")
        self.assertEqual(list_state(row("x", State.PROFILE_MISMATCH, detail="（登記 carol）")), "prof 不符")
        self.assertEqual(list_state(row("x", State.AGENT_MISMATCH, detail="（登記 codex）")), "agent 不符")
        self.assertEqual(row_keys(row("x", state=State.UNREGISTERED), {"panes": 1}), "Enter 接上")
        self.assertEqual(row_keys(row("x", state=State.RUNNING, agent="codex"), {"panes": 1}),
                         "Enter 接上　s 停掉　S 重開　m 搬　e 改名　r retire")  # Codex has no profiles

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

    def test_pick_stop_confirm_fits_narrow_screen(self):
        names = [f"{i:02d}" + "n" * 18 for i in range(10)]  # 20 cells each
        rows = [row(n) for n in names]
        s = self.screen([" "] * 10 + ["s", "y"], w=61)
        self.assertEqual(s.pick(rows, None), ("stop!", names))
        shown = "\n".join(self.scr.screens[-1])
        self.assertIn("等 10 個　確定？(y/N)", shown)

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
        self.assertRegex(first, r"^ +1 +a +claude alice +執行中 +idle +│")  # profile column follows agent, fields sit right against the name

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
               ago="3 分鐘前", text="修 bug", quote="「修 bug」", selected=True, skip=None, guessed=False)
        s = self.screen(["q"])
        self.assertIsNone(s.adopt_checklist([c]))
        self.assertIn("「修 bug」", "\n".join(self.scr.screens[-1]))

    def test_adopt_card_says_a_guessed_codex_conversation(self):
        c = NS(key="700", agent="codex", profile="", session_id=SID, cwd="/p", where="~/p", last=NOW,
               ago="3 分鐘前", text="整理", quote="「整理」", selected=False, skip=None, guessed=True)
        s = self.screen(["q"])
        self.assertIsNone(s.adopt_checklist([c]))
        self.assertIn("推測", "\n".join(self.scr.screens[-1]))

    def test_tiny_terminal_does_not_crash(self):
        s = self.screen(["q"], h=2, w=4)
        self.assertEqual(s.pick([row("a")], None), ("quit", []))


class ListSearchScreenTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.r = FakeRunner(strict=True).on(["tmux", "-u", "list-panes"], cp()).on(["tmux", "-u", "list-windows"], cp())
        self.t = [0.0]
        self.rows_now = []

    def tick(self):
        self.t[0] += 1.0

    def screen(self, keys, h=30, w=120):
        self.scr = FakeScr(keys, h=h, w=w, on_tick=self.tick)
        return CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW, clock=lambda: self.t[0],
                            rows_fn=lambda cfg, run: self.rows_now)

    def test_render_row_numbers_right_aligned(self):
        from sampan.ui import _list_cells
        rows = [row(f"s{i}") for i in range(1, 13)]
        cells = [_list_cells(r, set(), None, i, len(rows))[0] for i, r in enumerate(rows, 1)]
        self.assertTrue(cells[0].startswith(" 1 "), cells[0])
        self.assertEqual(cells[0], " 1   s1")
        self.assertTrue(cells[11].startswith("12 "), cells[11])
        s = self.screen(["q"])
        s.pick(rows, None)
        shown = self.scr.screens[0]
        self.assertTrue(shown[1].startswith("  1   s1 "), shown[1])
        self.assertTrue(shown[12].startswith(" 12   s12 "), shown[12])

    def test_row_numbers_stay_while_filtering(self):
        rows = [row(f"s{i}") for i in range(1, 13)]
        s = self.screen(["/", "1", "2", "\x1b", "q"])
        s.pick(rows, None)
        shown = self.scr.screens[3]
        self.assertTrue(shown[1].startswith(" 12   s12 "), shown[1])
        self.assertIn("/12▏（1 筆，Enter 進入，Esc 取消）", "\n".join(shown))

    def test_search_no_match_enter_is_noop(self):
        s = self.screen(["/", "z", "z", "z", "\n", "\x1b", "q"])
        self.assertEqual(s.pick([row("a"), row("b")], None), ("quit", []))
        shown = "\n".join(self.scr.screens[4])  # after Enter
        self.assertIn("沒有名稱含「zzz」的 session", shown)
        self.assertIn("/zzz▏（0 筆，Enter 進入，Esc 取消）", shown)

    def test_search_digits_go_to_query_and_attach(self):
        rows = [row("001-a"), row("003-foo"), row("010-bar")]
        s = self.screen(["/", "0", "0", "3", "\n"])
        self.assertEqual(s.pick(rows, None), ("attach", ["003-foo"]))

    def test_digit_jump_attaches(self):
        rows = [row(f"s{i:02d}") for i in range(1, 33)]
        s = self.screen(["1", "2", "\n"])
        self.assertEqual(s.pick(rows, None), ("attach", ["s12"]))
        self.assertIn("跳到第 1 列", "\n".join(self.scr.screens[1]))

    def test_reload_keeps_query_and_cursor(self):
        rows = [row("a"), row("foo-1"), row("foo-2")]
        self.rows_now = [row("0new-foo"), row("a"), row("foo-1"), row("foo-2")]
        s = self.screen(["/", "f", "o", "o", "KEY_DOWN"] + [TICK] * 6 + ["\n"])
        self.assertEqual(s.pick(rows, None), ("attach", ["foo-2"]))
        self.assertEqual([r.name for r in rows], ["0new-foo", "a", "foo-1", "foo-2"])
        shown = "\n".join(self.scr.screens[-1])
        self.assertIn("/foo▏（3 筆，Enter 進入，Esc 取消）", shown)
        self.assertIn("0new-foo", shown)

    def test_reload_outside_search_keeps_index_when_session_vanishes(self):
        rows = [row("a"), row("b"), row("c"), row("d")]
        self.rows_now = [row("a"), row("b"), row("d")]
        s = self.screen(["KEY_DOWN", "KEY_DOWN"] + [TICK] * 6 + ["\n"])
        self.assertEqual(s.pick(rows, None), ("attach", ["d"]))

    def test_narrow_layout_shows_row_numbers(self):
        rows = [row(f"s{i}") for i in range(1, 13)]
        s = self.screen(["q"], w=80)
        s.pick(rows, None)
        shown = self.scr.screens[0]
        self.assertTrue(shown[1].startswith("  1   s1 "), shown[1])
        self.assertNotIn("│", shown[1])  # info card is underneath, not to the right

    def test_filtering_keeps_name_column_width(self):
        s = self.screen(["/", "a", "\x1b", "\x1b"])
        s.pick([row("a"), row("zzzz-very-long-item")], None)
        full = self.scr.screens[0][1]
        just_a = self.scr.screens[2][1]  # after "/a": only row a is visible
        self.assertIn(" 1   a", just_a)
        self.assertNotIn("zzzz", "\n".join(self.scr.screens[2][1:3]))
        self.assertEqual(full.index("claude"), just_a.index("claude"))

    def test_help_mentions_jump_and_search(self):
        s = self.screen(["q"])
        s.pick([row("a")], None)
        self.assertIn("/ 搜尋　Tab 篩選　* 全選　空白 勾選", self.scr.screens[0][-1])


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


class FakeWatcher:
    """Stand-in for daemon_watch.DaemonWatcher: latest is set by the test; start/stop are recorded (no thread)."""

    def __init__(self, latest=None):
        self.latest, self.events, self.cfg = latest, [], None

    def start(self):
        self.events.append("start")

    def stop(self, **_kw):
        self.events.append("stop")


def daemon_snap(threads=(), loaded=()):
    from sampan.codex_daemon import DaemonSnapshot
    return DaemonSnapshot(Path("/h"), None, tuple(threads), frozenset(loaded), NOW)


def codex_thread(tid, status, flags=()):
    from sampan.codex_daemon import CodexThread
    return CodexThread(tid, "", "/w", "codex-tui", "cli", None, status, tuple(flags), NOW, None)


class WatcherScreenTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.rows = [row("cl", agent_status="busy"), row("cx", agent="codex", sid=TID, agent_status="working")]

    def pick(self, keys, watcher, **kw):
        self.setUp()  # fresh rows: apply_codex_status changes them in place
        self.scr = FakeScr(keys)
        s = CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, rows_fn=lambda c, r: list(self.rows),
                         watcher=watcher, **kw)
        s.pick(list(self.rows), None)
        return s

    def test_ui_uses_watcher_latest(self):
        w = FakeWatcher(daemon_snap([codex_thread(TID, "active", ["waitingOnApproval"])]))
        s = self.pick(["q"], w)
        cells = {c[0].split()[-1]: c[4] for c in s.view["left"]}
        self.assertEqual(cells["cx"], ("waiting", "attn"))
        self.assertEqual(cells["cl"], ("busy", "warn"))
        self.assertEqual(w.events, [])  # the screen never starts or stops the watcher itself
        plain = self.pick(["q"], None)
        self.assertEqual({c[0].split()[-1]: c[4] for c in plain.view["left"]}["cx"], ("working", "warn"))
        self.assertEqual([c[4] for c in plain.view["left"]], [("busy", "warn"), ("working", "warn")])

    def test_new_snapshot_applies_and_rereads_usage_without_waiting(self):
        w = FakeWatcher(None)
        calls = []
        shown = []

        def tick():
            shown.append(dict((c[0].split()[-1], c[4][0]) for c in s.view["left"])["cx"])
            if len(shown) == 1:
                w.latest = daemon_snap([codex_thread(TID, "active", ["waitingOnUserInput"])])
        self.scr = FakeScr([TICK, TICK, "q"], on_tick=tick)
        s = CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, clock=lambda: 0.0,
                         rows_fn=lambda c, r: list(self.rows), watcher=w,
                         usage_fn=lambda cfg, p: calls.append(p.name))
        s.pick(list(self.rows), None)
        self.assertEqual(shown, ["working", "waiting"])
        self.assertEqual(calls.count("codex"), 2)  # entry, then the new snapshot (clock never moved)

    def test_reload_reapplies_latest(self):
        w = FakeWatcher(daemon_snap(loaded={TID}))
        t = [0.0]
        self.scr = FakeScr([TICK, "q"], on_tick=lambda: t.__setitem__(0, t[0] + 10))
        fresh = [row("cl", agent_status="busy"), row("cx", agent="codex", sid=TID, agent_status="working")]
        s = CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, clock=lambda: t[0],
                         rows_fn=lambda c, r: list(fresh), watcher=w)
        shown = list(self.rows)
        s.pick(shown, None)
        self.assertIs(shown[1], fresh[1])  # the list was re-read
        self.assertEqual(shown[1].agent_status, "idle")  # rows re-read from rows_fn get the snapshot too

    def test_curses_ui_starts_and_stops_watcher(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        w = FakeWatcher()
        rc = curses_ui(cfg, FakeRunner(), lambda _l: None, wrapper=lambda fn: fn(FakeScr(["q"])),
                       rows_fn=lambda c, run: [row("a")], watcher=w)
        self.assertEqual(rc, 0)
        self.assertEqual(w.events, ["start", "stop"])

        def boom(fn):
            raise RuntimeError("curses broke")
        w2 = FakeWatcher()
        with self.assertRaises(RuntimeError):
            curses_ui(cfg, FakeRunner(), lambda _l: None, wrapper=boom, watcher=w2)
        self.assertEqual(w2.events, ["start", "stop"])


def desk_thread(tid, name, status="idle", flags=(), hours=1.0):
    from sampan.codex_daemon import CodexThread
    return CodexThread(tid, name, "/w/proj", "Codex Desktop", "vscode", None, status, tuple(flags),
                       NOW - timedelta(hours=hours), None)


class CodexOthersScreenTest(unittest.TestCase):
    """The「Codex 其他對話」block under the tmux rows: display only, never an action."""

    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.snap = daemon_snap([desk_thread("d1", "desk-ask", "active", ["waitingOnApproval"]),
                                 desk_thread("d2", "desk-idle", hours=2)])

    def pick(self, keys, latest="snap", cfg=None, w=120):
        self.scr = FakeScr(keys, w=w)
        self.s = CursesScreen(self.scr, cfg or self.cfg, FakeRunner(), now_fn=lambda: NOW,
                              watcher=FakeWatcher(self.snap if latest == "snap" else latest))
        return self.s.pick([row("a"), row("b")], None)

    def test_section_renders_after_tmux_rows(self):
        from sampan.ui.codex_others import SECTION_TITLE
        for w in (120, 80):
            self.pick(["q"], w=w)
            lines = self.scr.screens[0]
            at = {k: next(i for i, x in enumerate(lines) if k in x) for k in ("  b ", SECTION_TITLE, "desk-ask")}
            self.assertLess(at["  b "], at[SECTION_TITLE], (w, lines))
            self.assertLess(at[SECTION_TITLE], at["desk-ask"], w)
            self.assertIn("等你核准", lines[at["desk-ask"]])
            self.assertIn("Desktop", lines[at["desk-ask"]])
            self.assertIn("desk-idle", "\n".join(lines))

    def test_section_only_under_the_all_filter(self):
        from sampan.ui.codex_others import SECTION_TITLE
        self.pick(["\t", "q"])
        self.assertIn(SECTION_TITLE, "\n".join(self.scr.screens[0]))
        self.assertNotIn(SECTION_TITLE, "\n".join(self.scr.screens[1]))
        self.assertNotIn("desk-ask", "\n".join(self.scr.screens[1]))

    def test_cursor_skips_section_title(self):
        from sampan.ui.codex_others import GO_DESKTOP
        self.pick(["KEY_DOWN", "KEY_DOWN", "q"])
        self.assertEqual(self.s.view["cursor"], 3)  # a, b, title, desk-ask
        self.assertEqual(self.s.view["card"][-1], GO_DESKTOP)
        self.assertEqual(self.s.view["card"][0], "desk-ask")
        sel = [seg for seg in render(**self.s.view, width=119, height=30)[4] if seg[1].startswith("sel")]
        self.assertTrue(sel and "desk-ask" in sel[0][0])
        self.pick(["KEY_DOWN", "KEY_DOWN", "KEY_UP", "q"])
        self.assertEqual(self.s.view["cursor"], 1)  # back on b, the title is never landed on

    def test_digit_jump_counts_only_tmux_rows(self):
        self.assertEqual(self.pick(["3", "2", "\n"]), ("attach", ["b"]))

    def test_actions_on_external_rows_are_refused(self):
        from sampan.ui.codex_others import GO_DESKTOP
        down = ["KEY_DOWN", "KEY_DOWN"]
        for keys in (down + ["\n"], down + ["s", "y"], down + ["X"], down + ["m"],
                     ["/", "d", "e", "s", "k", "\n", "\x1b"]):
            self.assertEqual(self.pick(keys + ["q"]), ("quit", []), keys)
            self.assertIn(GO_DESKTOP, "\n".join(self.scr.screens[-1]), keys)
            self.assertNotIn("將停掉", "\n".join("\n".join(x) for x in self.scr.screens), keys)
        self.assertEqual(self.pick(down + ["R"]), ("restore", []))  # global keys are not row actions
        self.assertEqual(self.pick(down + [" ", "KEY_UP", "KEY_UP", "\n"]), ("attach", ["a"]))
        self.assertNotIn("●", "".join(c[0] for c in self.s.view["left"][:2] if isinstance(c, tuple)))

    def test_bulk_action_with_cursor_on_external_row_is_refused(self):
        from sampan.ui.codex_others import GO_DESKTOP
        self.assertEqual(self.pick([" ", " ", "s", "y", "q"]), ("quit", []))  # a, b marked; cursor moved onto d1
        self.assertIn(GO_DESKTOP, "\n".join(self.scr.screens[-1]))

    def test_search_matches_external_names(self):
        from sampan.ui.codex_others import SECTION_TITLE
        self.pick(["/", "i", "d", "l", "e", "\x1b", "q"])
        shown = "\n".join(self.scr.screens[5])  # after "/idle"
        self.assertIn("desk-idle", shown)
        self.assertIn(SECTION_TITLE, shown)
        self.assertNotIn("desk-ask", shown)

    def test_daemon_down_line(self):
        from sampan.ui.codex_others import DAEMON_DOWN, SECTION_TITLE
        for state in (State.RUNNING, State.STOPPED):  # a Codex row in any state is enough
            self.scr = FakeScr(["q"])
            CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, watcher=FakeWatcher(None)).pick(
                [row("a"), row("cx", state=state, agent="codex")], None)
            self.assertIn(DAEMON_DOWN, "\n".join(self.scr.screens[0]), state)
            self.assertNotIn(SECTION_TITLE, "\n".join(self.scr.screens[0]))
        no_codex = make_env(Path(tempfile.mkdtemp()), TOML_NO_CODEX)
        self.pick(["q"], latest=None, cfg=no_codex)
        self.assertNotIn(DAEMON_DOWN, "\n".join(self.scr.screens[0]))

    def test_daemon_down_is_silent_without_a_codex_row(self):
        from sampan.ui.codex_others import DAEMON_DOWN
        self.pick(["q"], latest=None)  # an enabled Codex profile, but only Claude rows in the list
        self.assertNotIn(DAEMON_DOWN, "\n".join(self.scr.screens[0]))

    def _pick_started(self, rows, **attrs):
        w = FakeWatcher(None)
        for k, v in attrs.items():
            setattr(w, k, v)
        self.scr = FakeScr(["q"])
        CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, watcher=w).pick(rows, None)
        return "\n".join(self.scr.screens[0])

    def test_screen_shows_starting_without_codex_rows(self):
        from sampan.ui.codex_others import STARTING
        self.assertIn(STARTING, self._pick_started([row("a")], polled=False, start_state="starting"))

    def test_starting_note_hidden_once_latest_is_set(self):
        from sampan.ui.codex_others import SECTION_TITLE, STARTING
        w = FakeWatcher(self.snap)
        w.polled, w.start_state = True, "starting"
        self.scr = FakeScr(["q"])
        CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, watcher=w).pick([row("a")], None)
        shown = "\n".join(self.scr.screens[0])
        self.assertIn(SECTION_TITLE, shown)
        self.assertIn("desk-ask", shown)
        self.assertNotIn(STARTING, shown)

    def test_screen_shows_start_failed(self):
        shown = self._pick_started([row("a")], polled=True, start_state="failed", start_error="x")
        self.assertIn("啟動失敗：x", shown)

    def test_not_found_failure_hidden_without_codex_rows(self):
        from sampan.codex_daemon import CODEX_NOT_FOUND
        from sampan.ui.codex_others import DAEMON_DOWN
        shown = self._pick_started([row("a")], polled=True, start_state="failed", start_error=CODEX_NOT_FOUND)
        self.assertNotIn("啟動失敗", shown)
        self.assertNotIn(DAEMON_DOWN, shown)

    def test_not_found_failure_shown_with_codex_row(self):
        from sampan.codex_daemon import CODEX_NOT_FOUND
        shown = self._pick_started([row("a", agent="codex")], polled=True, start_state="failed",
                                   start_error=CODEX_NOT_FOUND)
        self.assertIn("啟動失敗：找不到 codex 指令", shown)

    def test_other_failure_shown_without_codex_rows(self):
        shown = self._pick_started([row("a")], polled=True, start_state="failed", start_error="other")
        self.assertIn("啟動失敗：other", shown)

    def test_failed_note_hidden_once_daemon_is_up(self):
        from sampan.ui.codex_others import SECTION_TITLE
        w = FakeWatcher(self.snap)
        w.polled, w.start_state, w.start_error = True, "failed", "x"
        self.scr = FakeScr(["q"])
        CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, watcher=w).pick([row("a")], None)
        shown = "\n".join(self.scr.screens[0])
        self.assertIn(SECTION_TITLE, shown)
        self.assertIn("desk-ask", shown)
        self.assertNotIn("啟動失敗", shown)

    def test_screen_down_rule_unchanged_after_start(self):
        from sampan.ui.codex_others import DAEMON_DOWN, STARTING
        shown = self._pick_started([row("a")], polled=True, start_state=None)
        self.assertNotIn(DAEMON_DOWN, shown)
        self.assertNotIn(STARTING, shown)

    def test_ui_reload_never_reads_the_daemon_itself(self):
        # a Codex pane whose thread is unresolved must not make the ui thread talk to the daemon: the watcher does that
        from unittest import mock

        from sampan import codex_daemon
        from sampan.ui.screen import curses_ui
        r = FakeRunner()
        r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("cx", "%1", 100, "codex", "/w/proj") + "\n"))
        r.on(["ps", "-A"], cp(out=f"{ps_line(100, 1, '-zsh', '-zsh')}\n{ps_line(101, 100, 'codex', 'codex')}\n"))
        r.on(["ps", "-o", "etime="], cp(out="05:00\n"))
        with mock.patch.object(codex_daemon, "snapshot", side_effect=AssertionError("daemon read on the ui thread")):
            curses_ui(self.cfg, r, lambda _l: None, wrapper=lambda fn: fn(FakeScr(["q"])), watcher=FakeWatcher(None))
        self.assertTrue(r.called(["ps", "-o", "etime="]))  # the pane really was unresolved after lsof

    def test_block_fits_any_width(self):
        self.snap = daemon_snap([desk_thread("d1", "很長的對話名稱" * 6, "active", ["waitingOnApproval"])])
        for w in (120, 100, 80, 40, 20, 8):
            self.pick(["KEY_DOWN", "KEY_DOWN", "q"], w=w)
            for line in self.scr.screens[-1]:
                self.assertLessEqual(text_width(line), w, (w, line))

    def test_nothing_before_the_first_read(self):
        from sampan.ui.codex_others import DAEMON_DOWN
        self.scr = FakeScr(["q"])
        w = FakeWatcher(None)
        w.polled = False
        CursesScreen(self.scr, self.cfg, FakeRunner(), now_fn=lambda: NOW, watcher=w).pick([row("a")], None)
        self.assertNotIn(DAEMON_DOWN, "\n".join(self.scr.screens[0]))

    def test_nothing_matching_hides_the_block(self):
        from sampan.ui.codex_others import DAEMON_DOWN, SECTION_TITLE
        self.snap = daemon_snap([desk_thread("old", "desk-old", hours=30)])
        self.pick(["q"])
        shown = "\n".join(self.scr.screens[0])
        self.assertNotIn(SECTION_TITLE, shown)
        self.assertNotIn(DAEMON_DOWN, shown)

    def test_overflow_line(self):
        self.snap = daemon_snap([desk_thread(f"d{i}", f"desk-{i:02}", hours=i / 10) for i in range(13)])
        self.pick(["q"])
        self.assertIn("還有 3 段", "\n".join(self.scr.screens[0]))


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


class FolderScreenTest(unittest.TestCase):
    """Folder rows on the curses list: rendering, the folder card, a broken folders.json, in-place collapse."""

    def setUp(self):
        from sampan import folders
        self.F = folders
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.r = FakeRunner(strict=True).on(["tmux", "-u", "list-panes"], cp()).on(["tmux", "-u", "list-windows"], cp())
        self.rows = [row("a"), row("b", agent_status="busy"), row("c", state=State.STOPPED, agent_status="-"),
                     row("d")]

    def tree(self, fn):
        self.F.edit(self.cfg.state_dir, fn)

    def screen(self, keys, h=30, w=120):
        self.scr = FakeScr(keys, h=h, w=w)
        return CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW, rows_fn=lambda cfg, run: self.rows)

    def shown(self, i=0):
        return self.scr.screens[i]

    def test_folder_row_rendering(self):
        def fn(t):
            for n in ("a", "b", "c"):
                self.F.place_session(t, n, "客戶/甲")
            self.F.set_collapsed(t, "客戶/甲", True)
        self.tree(fn)
        s = self.screen(["q"])
        s.pick(self.rows, None)
        lines = self.shown()
        self.assertIn("▾ 客戶 (3)", lines[1])
        self.assertNotIn("忙", lines[1])  # open folders do not count busy ones
        self.assertIn("  ▸ 甲 (3)　1 忙", lines[2])
        self.assertRegex(lines[3], r"^ +1 +d +claude")  # hidden sessions are not numbered; d is the first shown
        self.assertNotIn(" a ", "\n".join(lines[1:5]))

    def test_session_rows_indented_under_folder(self):
        self.tree(lambda t: (self.F.place_session(t, "a", "客戶"), self.F.place_session(t, "b", "客戶/甲")))
        s = self.screen(["q"])
        s.pick(self.rows, None)
        lines = self.shown()
        self.assertIn("▾ 客戶 (2)", lines[1])
        self.assertIn("  ▾ 甲 (1)", lines[2])
        self.assertIn(" 1       b", lines[3])  # number, mark, then two spaces per level
        self.assertIn(" 2     a", lines[4])
        self.assertIn(" 3   c", lines[5])

    def test_search_shows_folder_path_before_name(self):
        self.tree(lambda t: self.F.place_session(t, "b", "客戶/甲"))
        s = self.screen(["/", "b", "\x1b", "q"])
        s.pick(self.rows, None)
        self.assertIn("客戶/甲 · b", "\n".join(self.shown(2)))
        self.assertNotIn("· a", "\n".join(self.shown(1)))  # top-level sessions get no prefix

    def test_folder_card_keys(self):
        self.tree(lambda t: (self.F.place_session(t, "a", "客戶"), self.F.place_session(t, "b", "客戶"),
                             self.F.place_session(t, "c", "客戶/甲")))
        s = self.screen(["q"], w=200)
        s.pick(self.rows, None)
        text = "\n".join(self.shown())
        self.assertIn("│ 客戶", text)
        self.assertIn("session 數 3", text)
        self.assertIn("執行中 2　已停止 1　retired 0　忙 1", text)
        self.assertIn("Enter 收合／展開　g 包成資料夾　f 搬　e 改名　X 刪除資料夾", text)  # the keys line wraps
        self.assertIn("S 重開全部　n 在這裡開新的", text)
        self.assertEqual(self.r.called(["tmux", "-u", "capture-pane"]), [])  # no preview on a folder row

    def test_broken_folders_warns_once_and_lists_all(self):
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "folders.json").write_text("{")
        s = self.screen(["q", "q"], w=400)
        s.pick(self.rows, None)
        first = "\n".join(self.shown())
        self.assertIn("資料夾設定讀不到", first)
        self.assertIn("先照原樣列出", first)
        for n in ("a", "b", "c", "d"):
            self.assertRegex(first, rf"\d +{n} +claude")
        seen = len(self.scr.screens)
        s.pick(self.rows, None)
        self.assertNotIn("資料夾設定讀不到", "\n".join("\n".join(x) for x in self.scr.screens[seen:]))
        self.assertEqual((self.cfg.state_dir / "folders.json").read_text(), "{")

    def test_folders_changed_on_disk_show_on_refresh(self):
        t = [0.0]

        def tick():
            t[0] += 1.0
            if t[0] == 2.0:
                self.tree(lambda tr: self.F.place_session(tr, "d", "維運"))
        self.scr = FakeScr([TICK] * 6 + ["q"], on_tick=tick)
        s = CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW, clock=lambda: t[0],
                         rows_fn=lambda cfg, run: self.rows)
        s.pick(self.rows, None)
        self.assertNotIn("維運", "\n".join(self.shown(0)))
        self.assertIn("▾ 維運 (1)", "\n".join(self.shown(-1)))

    def test_enter_on_folder_collapses_in_place_and_saves(self):
        self.tree(lambda t: self.F.place_session(t, "a", "客戶"))
        s = self.screen([" ", "KEY_UP", "\n", "q"])  # mark a, back to the folder row, collapse
        self.assertEqual(s.pick(self.rows, None), ("quit", []))
        self.assertTrue(self.F.load(self.cfg.state_dir).folders["客戶"])
        last = "\n".join(self.shown(-1))
        self.assertIn("▸ 客戶 (1)", last)

    def test_marks_are_gone_after_a_folder_action(self):
        s = self.screen([" ", "g", "q"])
        self.assertEqual(s.pick(self.rows, None), ("group", ["session:a"]))
        self.assertIn("●", "\n".join(self.shown(1)))
        s.pick(self.rows, None)
        self.assertNotIn("●", "\n".join(self.shown(-1)))

    def test_restart_returns_the_confirmed_plan(self):
        from sampan.restart import RestartPlan
        s = self.screen(["S", "y"])
        action = s.pick(self.rows, None)
        self.assertEqual(action, ("restart!", [RestartPlan(["a"], [], [])]))
        self.assertIn("將重開：a　確定？(y/N)", "\n".join(self.shown(-1)))

    def test_restart_confirm_skips_sampans_own_pane(self):
        import dataclasses
        self.rows[0] = dataclasses.replace(self.rows[0], pane="%5")
        self.scr = FakeScr([" ", "KEY_DOWN", "KEY_DOWN", " ", "S", "q", "q"])  # mark a and d
        s = CursesScreen(self.scr, self.cfg, self.r, now_fn=lambda: NOW, rows_fn=lambda cfg, run: self.rows,
                         own_pane="%5")
        s.pick(self.rows, None)
        self.assertTrue(any("略過：a（sampan 就在這個 pane）" in "\n".join(x) for x in self.scr.screens))

    def test_choose_folder_list(self):
        s = self.screen(["KEY_DOWN", "KEY_DOWN", "\n"])
        self.assertEqual(s.choose_folder(["（最外層）", "新資料夾…", "客戶"]), 2)
        self.assertIn("新資料夾…", "\n".join(self.shown()))
        s = self.screen(["\x1b"])
        self.assertIsNone(s.choose_folder(["（最外層）", "新資料夾…"]))

    def test_help_and_row_keys_mention_S(self):
        s = self.screen(["q"])
        s.pick(self.rows, None)
        self.assertIn("S 重開　g 包資料夾　f 搬進資料夾", self.shown()[-1])
        self.assertIn("S 重開", row_keys(row("x"), {"panes": 1}))
        self.assertIn("S 背景接回", row_keys(row("x", state=State.STOPPED), {"panes": 0}))


class KeysWrapTest(unittest.TestCase):
    def test_keys_line_wraps_between_keys_not_inside_one(self):
        from sampan.ui.cards import Keys
        from sampan.ui.render import _card_rows
        line = Keys("Enter 收合／展開　g 包成資料夾　f 搬　e 改名　X 刪除資料夾　s 停全部　S 重開全部　n 在這裡開新的")
        rows = [r[0][0].rstrip() for r in _card_rows([line], 40)]
        self.assertEqual(rows, ["Enter 收合／展開　g 包成資料夾　f 搬", "e 改名　X 刪除資料夾　s 停全部",
                                "S 重開全部　n 在這裡開新的"])

    def test_a_key_longer_than_the_width_still_wraps(self):
        from sampan.ui.cards import Keys
        from sampan.ui.render import _card_rows
        rows = [r[0][0].rstrip() for r in _card_rows([Keys("x" * 25)], 10)]
        self.assertEqual(rows, ["x" * 10, "x" * 10, "x" * 5])
