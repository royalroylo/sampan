import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from sampan.config import SOURCE_RULE
from sampan.plugin_apply import Applied
from sampan.plugin_view import CatalogEntry, Change, ProfileChoice, profile_choices, snapshot
from sampan.ui import cmd_ui
from sampan.ui import plugin_actions as pa
from sampan.ui.plugin_state import CatalogPicker, PluginPicker, ProfileToggles
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env, status_row
from tests.test_plugin_apply import installer


def rows(*names):
    return [NS(name=n) for n in names]


def feed(obj, keys):
    out = None
    for k in keys:
        out = obj.key(k)
    return out


class PluginPickerTest(unittest.TestCase):
    def mk(self):
        return PluginPicker(rows("context-tools@x", "security-guidance@y", "other@z"))

    def test_plugin_picker_keys_and_search_enter_opens(self):
        p = self.mk()
        self.assertEqual(p.key("q"), ("back", []))
        self.assertEqual(p.key("\x1b"), ("back", []))
        self.assertEqual(p.key("\n"), ("open", ["context-tools@x"]))
        self.assertEqual(p.key("u"), ("update", ["context-tools@x"]))
        self.assertEqual(p.key("U"), ("update_all", []))
        self.assertEqual(p.key("a"), ("add", []))
        self.assertEqual(p.key("r"), ("check", []))
        for k in ("n", "R", "w", "s", " ", "z"):
            self.assertIsNone(p.key(k))
        p.key("KEY_DOWN")
        self.assertEqual(p.key("KEY_ENTER"), ("open", ["security-guidance@y"]))
        p = self.mk()
        for k in "/sec":
            self.assertIsNone(p.key(k))
        self.assertIn("/sec", p.prompt)
        self.assertEqual(p.key("\n"), ("open", ["security-guidance@y"]))
        self.assertIsNone(p.query)
        # inside a search q is text, not back
        p = self.mk()
        p.key("/")
        self.assertIsNone(p.key("q"))
        self.assertEqual(p.query, "q")

    def test_plugin_picker_digits_jump(self):
        p = self.mk()
        p.key("3")
        self.assertEqual(p.cursor, 2)


class CatalogPickerTest(unittest.TestCase):
    def test_catalog_picker_pick_type_back(self):
        ents = [NS(pid="a@m", description="", source="s"), NS(pid="b@m", description="", source="s")]
        p = CatalogPicker(ents)
        self.assertEqual([r.name for r in p.rows], ["a@m", "b@m"])
        self.assertEqual(p.key("KEY_DOWN"), None)
        self.assertEqual(p.key("\n"), ("pick", ["b@m"]))
        self.assertEqual(p.key("i"), ("type", []))
        self.assertEqual(p.key("q"), ("back", []))
        self.assertEqual(p.key("\x1b"), ("back", []))
        self.assertIsNone(p.key("u"))
        self.assertIsNone(p.key(" "))
        p.key("/")
        for k in "a@":
            p.key(k)
        self.assertEqual(p.key("\n"), ("pick", ["a@m"]))
        self.assertEqual(p.visible[0].entry.pid, "a@m")


def C(profile, installed, listed, disabled=None):
    return ProfileChoice(profile, profile.upper(), installed, listed, disabled)


class ProfileTogglesTest(unittest.TestCase):
    def test_toggles_space_skips_disabled_notes_and_marks(self):
        a, b, c, d = C("a", "1", True), C("b", None, False), C("c", None, False, "目錄不存在"), C("d", "1", False)
        t = ProfileToggles([a, b, c, d])
        self.assertEqual(t.checked, {"a": True, "b": False, "c": False, "d": True})
        self.assertEqual(t.initial, t.checked)
        self.assertFalse(t.dirty())
        self.assertEqual(t.mark(a), "[x]")
        self.assertEqual(t.mark(b), "[ ]")
        self.assertEqual(t.mark(c), "[-]")
        self.assertEqual(t.mark(d), "[x]!")
        self.assertIsNone(t.note(a))
        self.assertEqual(t.note(c), "目錄不存在")
        self.assertEqual(t.note(d), "已裝但清單沒列")
        t.key(" ")  # a off, cursor -> b
        self.assertEqual((t.checked["a"], t.cursor), (False, 1))
        self.assertEqual(t.note(a), "← 儲存時會移除")
        t.key(" ")  # b on
        self.assertEqual(t.note(b), "← 儲存時會安裝")
        t.key(" ")  # c disabled: no toggle; still moves down
        self.assertFalse(t.checked["c"])
        self.assertTrue(t.dirty())
        self.assertEqual(t.key("KEY_UP"), None)
        self.assertEqual(t.cursor, 2)
        self.assertEqual(t.key("s"), ("save", None))

    def test_toggles_custom_notes_and_mismatch_fn(self):
        b, d = C("b", None, False), C("d", "1", False)
        t = ProfileToggles([b, d], notes=("加", "除"), mismatch_fn=lambda c: None)
        t.key(" ")
        t.key(" ")
        self.assertEqual((t.note(b), t.note(d)), ("加", "除"))
        self.assertEqual(ProfileToggles([d], mismatch_fn=lambda c: None).mark(d), "[x]")

    def test_toggles_discard_prompt(self):
        t = ProfileToggles([C("a", None, False)])
        self.assertEqual(t.key("\x1b"), ("back", None))
        t = ProfileToggles([C("a", None, False)])
        t.key(" ")
        self.assertIsNone(t.key("\x1b"))
        self.assertEqual(t.prompt, "放棄變更？(y/N)")
        self.assertIsNone(t.key("n"))
        self.assertIsNone(t.prompt)
        self.assertIsNone(t.key("q"))
        self.assertEqual(t.key("y"), ("back", None))

    def test_toggles_empty(self):
        t = ProfileToggles([])
        self.assertIsNone(t.key(" "))
        self.assertEqual(t.key("q"), ("back", None))


def answers(*items):
    it = iter(items)
    return lambda _p: next(it)


def write_state(prof, installed=(), mkts=()):
    pd = Path(prof.config_dir) / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {pid: [{"scope": "user", "version": "1.0.0"}] for pid in installed}}))
    (pd / "known_marketplaces.json").write_text(json.dumps(
        {m: {"source": {"source": "github", "repo": src}} for m, src in mkts}))


TOOLS = "tools@my-plugins"
INSTALL_TOOLS = ["claude", "plugin", "install", TOOLS, "--scope", "user"]


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.out = []
        write_state(self.cfg.profiles["alice"], [TOOLS], [("my-plugins", "owner/my-plugins")])
        write_state(self.cfg.profiles["work"], [], [("my-plugins", "owner/my-plugins")])

    def lists(self):
        return {n: p.get("plugins", []) for n, p in tomllib.loads(self.cfg.path.read_text())["profiles"].items()}

    def choices(self, pid=TOOLS):
        snap = snapshot(self.cfg)
        return profile_choices(self.cfg, snap, pid, "owner/my-plugins")

    def test_save_flow_cancel_and_confirm(self):
        choices = self.choices()
        run = FakeRunner(strict=True)
        self.assertEqual(pa.save_flow(self.cfg, run, self.out.append, answers("y"), "extra@my-plugins",
                                      self.choices("extra@my-plugins"), {"alice": False, "work": False}), [])
        self.assertEqual(self.out, ["沒有要變更的"])
        self.out.clear()
        self.assertEqual(pa.save_flow(self.cfg, run, self.out.append, answers("n"), TOOLS, choices,
                                      {"alice": True, "work": False}), [])
        self.assertEqual(self.out[0], "將會：")
        self.assertIn("  · work（work@example.com）：清單拿掉（本來就沒裝）", self.out)
        self.assertEqual(self.out[-1], "已取消")
        self.assertEqual(run.calls, [])
        self.out.clear()
        with mock.patch.object(pa, "apply_changes", return_value=Applied(["work"], {}, None)) as ap:
            got = pa.save_flow(self.cfg, run, self.out.append, answers("y"), TOOLS, choices,
                               {"alice": True, "work": False}, new_source="x/y")
        self.assertEqual(got, ["work"])
        (cfg, _run, _out, pid, changes), kw = ap.call_args
        self.assertEqual(pid, TOOLS)
        self.assertEqual(changes, [Change("work", None, False, False)])
        self.assertEqual(kw, {"new_source": "x/y"})
        self.assertIn("  [marketplaces] 加上 my-plugins = x/y", self.out)

    def test_update_all_asks_single_does_not(self):
        calls = []
        with mock.patch.object(pa, "update_plugins", side_effect=lambda c, r, o, pids=None: calls.append(pids) or ["alice"]):
            prompts = []
            ask = lambda p: prompts.append(p) or "n"  # noqa: E731
            self.assertEqual(pa.update_flow(self.cfg, None, self.out.append, ask, None), [])
            self.assertEqual(prompts, ["全部 plugin、全部 profile 都更新？(y/N): "])
            self.assertEqual(self.out, ["已取消"])
            self.assertEqual(calls, [])
            self.assertEqual(pa.update_flow(self.cfg, None, self.out.append, answers("y"), None), ["alice"])
            self.assertEqual(pa.update_flow(self.cfg, None, self.out.append, ask, [TOOLS]), ["alice"])
            self.assertEqual(len(prompts), 1)
            self.assertEqual(calls, [None, [TOOLS]])

    def add(self, choose, *asked):
        snap = snapshot(self.cfg)
        return pa.add_flow(self.cfg, self.out.append, answers(*asked), snap, lambda entries: choose)

    def test_add_flow_known_new_and_asked_source(self):
        self.assertEqual(self.add("extra@my-plugins"), ("extra@my-plugins", None))
        self.assertEqual(self.add("", "extra@my-plugins"), ("extra@my-plugins", None))
        self.assertIsNone(self.add(None))
        # not in [marketplaces] but a profile already knows it
        write_state(self.cfg.profiles["work"], [], [("other", "someone/other")])
        self.assertEqual(self.add("", "p@other"), ("p@other", "someone/other"))
        # nobody knows it: ask for the source
        prompts = []
        snap = snapshot(self.cfg)
        it = iter(["q@newmkt", "me/newmkt"])
        ask = lambda p: prompts.append(p) or next(it)  # noqa: E731
        self.assertEqual(pa.add_flow(self.cfg, self.out.append, ask, snap, lambda e: ""), ("q@newmkt", "me/newmkt"))
        self.assertEqual(prompts, ["plugin@marketplace: ", "newmkt 的來源（owner/repo 或 git 網址）: "])

    def test_add_flow_invalid_id_and_source(self):
        self.assertIsNone(self.add("", "no-marketplace"))
        self.assertEqual(self.out, ["❌ 要寫成 plugin@marketplace"])
        self.out.clear()
        self.assertIsNone(self.add("", "q@newmkt", "not a source"))
        self.assertEqual(self.out, [f"❌ 來源要寫成 {SOURCE_RULE}"])
        self.out.clear()
        self.assertIsNone(self.add("", "q@newmkt", "https://user:tok@example.com/r.git"))
        self.assertEqual(self.out, [f"❌ 來源要寫成 {SOURCE_RULE}"])

    def test_restart_offer_R_and_later(self):
        rows = [status_row("a", profile="work"), status_row("b", profile="work"), status_row("c", profile="alice")]
        calls = []

        def restart(cfg, run, out, ask, profiles, **kw):
            calls.append((profiles, kw))
            return 3
        keys = []
        rc = pa.restart_offer(self.cfg, None, self.out.append, None, ["work"], lambda p: keys.append(p) or "R",
                              own_pane="%1", rows_fn=lambda c, r: rows, restart_fn=restart)
        self.assertEqual(rc, 3)
        self.assertEqual(keys, ["要生效需重開 work 的 session（2 個）：按 R 立刻重開，其他鍵稍後"])
        self.assertEqual(calls, [(["work"], {"own_pane": "%1", "on_prompt": None})])
        self.assertIn(keys[0], self.out)
        self.out.clear()
        rc = pa.restart_offer(self.cfg, None, self.out.append, None, ["work"], lambda p: "x",
                              rows_fn=lambda c, r: rows, restart_fn=restart)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.out[-1], "稍後要生效：sampan stop --profile work 後 sampan restore")
        self.out.clear()
        rc = pa.restart_offer(self.cfg, None, self.out.append, None, ["work"], lambda p: "R",
                              rows_fn=lambda c, r: [], restart_fn=restart)
        self.assertEqual((rc, self.out), (0, ["（這些 profile 目前沒有執行中的 session）"]))

    def test_plain_plugins_toggle_save_round_trip(self):
        original = self.cfg.path.read_text()
        run = FakeRunner(strict=True)
        run.on(INSTALL_TOOLS, installer(run, self.cfg, version="1.0.0"))
        prompts = []
        it = iter(["2", "2", "s", "y", "x", "q"])
        ask = lambda p: prompts.append(p) or next(it)  # noqa: E731
        rows = [status_row("a", profile="work")]
        rc = pa.plain_plugins(self.cfg, run, self.out.append, ask, rows_fn=lambda c, r: rows)
        self.assertEqual(rc, 0)
        text = "\n".join(self.out)
        self.assertIn("═══ sampan · plugin 管理 ［▸plugin◂｜來源］ ═══", text)
        self.assertIn("  [ 2] tools                        1/2  不一致", text)
        self.assertIn("  [2] [ ]! work  —  清單有但沒裝", text)
        self.assertIn("  [2] [x]! work  —  ← 儲存時會安裝", text)
        self.assertIn("  + 安裝到 work（work@example.com）", text)
        self.assertIn("  [ 2] tools                        2/2  最新", text)
        self.assertEqual([c["args"] for c in run.calls], [INSTALL_TOOLS])
        self.assertEqual(self.lists()["work"], ["tools@my-plugins"])
        self.assertEqual(self.cfg.path.read_text(), original)  # already listed: the list did not change
        self.assertEqual(prompts[:4], [pa.MENU, pa.TOGGLE, pa.TOGGLE, "確定？(y/N): "])
        self.assertEqual(prompts[4], "要生效需重開 work 的 session（1 個）：按 R 立刻重開，其他鍵稍後（輸入 R）: ")
        self.assertIn("稍後要生效：sampan stop --profile work 後 sampan restore", text)

    def test_plain_plugins_reconcile_only_rereads_config(self):
        run = FakeRunner(strict=True)
        it = iter(["2", "s", "y", "q"])
        self.assertEqual(pa.plain_plugins(self.cfg, run, self.out.append, lambda p: next(it)), 0)
        self.assertIn("  · work（work@example.com）：清單拿掉（本來就沒裝）", self.out)
        self.assertEqual(self.lists()["work"], [])
        self.assertEqual(run.calls, [])
        self.assertNotIn("（這些 profile 目前沒有執行中的 session）", self.out)  # nothing ran: no restart offer
        self.assertEqual([line for line in self.out if "tools" in line][-1].split()[-2:], ["1/2", "最新"])

    def test_plain_plugins_discard_and_add(self):
        run = FakeRunner(strict=True)
        prompts = []
        it = iter(["1", "1", "q", "y", "a", "", "extra@my-plugins", "1", "q", "y", ""])
        ask = lambda p: prompts.append(p) or next(it)  # noqa: E731
        self.assertEqual(pa.plain_plugins(self.cfg, run, self.out.append, ask), 0)
        self.assertIn("放棄變更？(y/N): ", prompts)
        self.assertIn("搜尋文字（Enter 自己輸入 plugin@marketplace）: ", prompts)
        self.assertIn("  [1] [x] alice  —  ← 儲存時會安裝", self.out)
        self.assertEqual(run.calls, [])

    def test_plain_add_installed_plugin_keeps_install_state(self):
        run = FakeRunner(strict=True)
        run.on(INSTALL_TOOLS, installer(run, self.cfg, version="1.0.0"))
        it = iter(["a", "", TOOLS, "2", "s", "y", "q"])
        self.assertEqual(pa.plain_plugins(self.cfg, run, self.out.append, lambda p: next(it),
                                          rows_fn=lambda c, r: []), 0)
        self.assertIn("  [1] [x] alice  1.0.0  ", self.out)
        self.assertIn("  + 安裝到 work（work@example.com）", self.out)
        self.assertFalse([line for line in self.out if line.startswith("  − 從")])
        self.assertEqual([c["args"] for c in run.calls], [INSTALL_TOOLS])

    def test_reload_config_warns_on_broken_file(self):
        self.cfg.path.write_text("this is = = not toml")
        self.assertIs(pa.reload_config(self.cfg, self.out.append), self.cfg)
        self.assertEqual(len(self.out), 1)
        self.assertTrue(self.out[0].startswith("⚠ 設定檔讀取失敗（"))
        self.assertTrue(self.out[0].endswith("），沿用目前內容"))

    def test_plain_choose_searches_catalog(self):
        entries = [CatalogEntry(f"p{i}@m", "desc", None) for i in range(40)] + [CatalogEntry("zz@m", "Zeta", None)]
        self.assertEqual(pa._plain_choose(self.out.append, answers("zeta", "1"))(entries), "zz@m")
        self.out.clear()
        self.assertIsNone(pa._plain_choose(self.out.append, answers("p", ""))(entries))
        self.assertEqual(len([line for line in self.out if line.startswith("  [")]), 30)
        self.assertEqual(pa._plain_choose(self.out.append, answers(""))(entries), "")
        self.assertIsNone(pa._plain_choose(self.out.append, answers("nothing"))(entries))


class PlainMenuTest(unittest.TestCase):
    def test_plain_menu_p_routes_to_plugins(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        calls = []
        rc = cmd_ui(cfg, FakeRunner(), [].append, answers("p", "q"), own_pane="%9", loop=True,
                    rows_fn=lambda c, r: [],
                    plugins_fn=lambda **kw: calls.append(kw) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["own_pane"], "%9")
        self.assertIn("ask", calls[0])

    def test_help_mentions_p(self):
        from sampan.ui.actions import HELP
        self.assertIn("　p plugin 管理", HELP)


class CursesPluginScreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        write_state(self.cfg.profiles["alice"], [TOOLS], [("my-plugins", "owner/my-plugins")])
        write_state(self.cfg.profiles["work"], [], [("my-plugins", "owner/my-plugins")])

    def screen(self, keys, run=None, w=120, rows=()):
        from sampan.ui.screen import CursesScreen
        from tests.test_ui_screen import FakeScr
        self.scr = FakeScr(keys, w=w)
        self.run = run or FakeRunner(strict=True)
        return CursesScreen(self.scr, self.cfg, self.run, rows_fn=lambda c, r: list(rows))

    def frames(self):
        return ["\n".join(f) for f in self.scr.screens]

    def test_p_opens_plugins_and_q_returns(self):
        from sampan.ui import curses_ui
        from tests.test_ui_screen import FakeScr
        write_state(self.cfg.profiles["work"], [TOOLS], [("my-plugins", "owner/my-plugins")])
        box = {}

        def wrapper(fn):
            box["scr"] = FakeScr(["p", "q", "q"])
            return fn(box["scr"])
        run = FakeRunner(strict=True).on(["tmux", "-u", "list-panes"], cp()).on(["tmux", "-u", "list-windows"], cp())
        rc = curses_ui(self.cfg, run, lambda _l: None, wrapper=wrapper, rows_fn=lambda c, r: [])
        self.assertEqual(rc, 0)
        screens = ["\n".join(f) for f in box["scr"].screens]
        shown = [f for f in screens if "plugin 管理" in f]
        self.assertTrue(shown)
        self.assertIn("2/2", shown[0])
        self.assertIn("↑↓ 移動　1-9 跳列　/ 搜尋　Enter 選 profile　u 更新這個", shown[0])
        self.assertNotIn("plugin 管理", screens[-1].split("\n")[0])
        self.assertTrue(screens[-1].startswith("═══ sampan"))
        self.assertIn("p plugin　q 離開", screens[-1])

    def test_layer2_toggle_save_confirm_applies(self):
        run = FakeRunner(strict=True)
        run.on(INSTALL_TOOLS, installer(run, self.cfg, version="1.0.0"))
        s = self.screen(["2", "\n", "KEY_DOWN", " ", "s", "y", "\n", "x", "q"], run,
                        rows=[status_row("a", profile="work")])
        self.assertEqual(s.plugins(ask=s.ask), 0)
        self.assertEqual([c["args"] for c in run.calls], [INSTALL_TOOLS])
        self.assertEqual(tomllib.loads(self.cfg.path.read_text())["profiles"]["work"]["plugins"], [TOOLS])
        text = "\n".join(self.frames())
        self.assertIn("sampan · plugin 管理 › tools@my-plugins", text)
        self.assertIn("來源 owner/my-plugins", text)
        self.assertIn("最新 未檢查", text)
        self.assertIn("[x]! work", text)
        self.assertIn("← 儲存時會安裝", text)
        self.assertIn("+ 安裝到 work（work@example.com）", text)
        self.assertIn("要生效需重開 work 的 session（1 個）：按 R 立刻重開，其他鍵稍後", text)
        self.assertIn("稍後要生效：sampan stop --profile work 後 sampan restore", self.frames()[-1])
        self.assertIn(" 2/2 ", self.frames()[-1])
        self.assertIn("-1", [str(t) for t in self.scr.timeouts])

    def test_layer2_esc_with_changes_asks_discard(self):
        original = self.cfg.path.read_text()
        s = self.screen(["\n", "KEY_DOWN", " ", "\x1b", "n", "\x1b", "y", "q"])
        self.assertEqual(s.plugins(ask=s.ask), 0)
        frames = self.frames()
        self.assertTrue(any("放棄變更？(y/N)" in f for f in frames))
        self.assertIn("sampan · plugin 管理", frames[-1])
        self.assertNotIn("›", frames[-1].split("\n")[0])
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.cfg.path.read_text(), original)

    def test_render_cells_exact(self):
        from sampan.plugin_view import build_rows
        from sampan.ui.render import plugin_cells, toggle_cells
        rows_ = build_rows(self.cfg, snapshot(self.cfg))
        tools = next(r for r in rows_ if r.pid == TOOLS)
        sp = next(r for r in rows_ if r.short == "superpowers")
        self.assertEqual(plugin_cells(tools, 2, 12), (" 2 tools", "1/2", ("不一致", "err")))
        self.assertEqual(plugin_cells(sp, 1, 2), ("1 superpowers", "0/2", ("未裝", "muted")))
        t = ProfileToggles([C("alice", "1.0.0-abcdefghijkl", True), C("work", None, True), C("x", None, False, "目錄不存在")])
        a, w, x = t.choices
        self.assertEqual(toggle_cells(t, a), ("[x] alice", "1.0.0-abcdef", ("", "muted")))
        self.assertEqual(toggle_cells(t, w), ("[ ]! work", "—", ("清單有但沒裝", "muted")))
        self.assertEqual(toggle_cells(t, x), ("[-] x", "—", ("目錄不存在", "muted")))
        t.key("KEY_DOWN")
        t.key(" ")
        self.assertEqual(toggle_cells(t, w), ("[x]! work", "—", ("← 儲存時會安裝", "attn")))

    def test_narrow_terminal_renders(self):
        from tests.test_ui_screen import text_width
        s = self.screen(["/", "t", "\x1b", "\n", " ", "\x1b", "y", "q"], w=60)
        self.assertEqual(s.plugins(ask=s.ask), 0)
        self.assertTrue(self.scr.screens)
        for f in self.scr.screens:
            for line in f:
                self.assertLessEqual(text_width(line), 60, line)

    def test_check_add_and_catalog_screen(self):
        clone = Path(self.cfg.profiles["alice"].config_dir) / "plugins" / "marketplaces" / "my-plugins" / ".claude-plugin"
        clone.mkdir(parents=True)
        (clone / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"name": "tools", "version": "2.0.0"}, {"name": "extra", "description": "Extra things"}]}))
        run = FakeRunner(strict=True)
        update = ["claude", "plugin", "marketplace", "update", "my-plugins"]
        run.on(update, lambda a: cp())
        # r checks, ↓ shows tools' card; a opens the catalog (sorted: extra, tools) and Enter picks extra
        s = self.screen(["r", "KEY_DOWN", "a", "\n", "\x1b", "q"], run)
        self.assertEqual(s.plugins(ask=s.ask), 0)
        frames = self.frames()
        text = "\n".join(frames)
        self.assertRegex(text, r"sampan · plugin 管理 ［▸plugin◂｜來源］　上次檢查 \d\d:\d\d")
        self.assertIn("1.0.0 → 2.0.0", text)
        self.assertIn("↑↓ 移動　/ 搜尋　Enter 選這個　i 自己輸入　q 返回", text)
        self.assertIn("extra@my-plugins", text)
        self.assertIn("sampan · plugin 管理 › extra@my-plugins", text)
        self.assertIn("[ ] alice", text)  # a new plugin: boxes start at the install state
        self.assertEqual([c["args"] for c in run.calls], [update])

    def test_add_type_it_and_update_this(self):
        run = FakeRunner(strict=True)
        mkt = ["claude", "plugin", "marketplace", "update", "my-plugins"]
        update = ["claude", "plugin", "update", TOOLS, "--scope", "user"]
        run.on(mkt, lambda a: cp()).on(update, lambda a: cp())
        keys = ["a", "i", *"extra@my-plugins", "\n", "\x1b", "KEY_DOWN", "u", "q"]
        s = self.screen(keys, run)
        self.assertEqual(s.plugins(ask=s.ask), 0)
        text = "\n".join(self.frames())
        self.assertIn("plugin@marketplace: extra@my-plugins", text)
        self.assertIn("sampan · plugin 管理 › extra@my-plugins", text)
        self.assertEqual([c["args"] for c in run.calls], [mkt, update])
        self.assertIn("沒有變動", text)


TOKEN_REFUSAL = "❌ 網址裡不要放帳號或 token，認證交給 git 的 credential helper"


def market_runner(cfg, **kw):
    from tests.test_plugin_apply import market_fake
    run = FakeRunner(strict=True)
    rule = market_fake(run, cfg, **kw)
    run.on(["claude", "plugin", "marketplace", "add"], rule)
    run.on(["claude", "plugin", "marketplace", "remove"], rule)
    return run


class MarketTabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.out = []
        write_state(self.cfg.profiles["alice"], [TOOLS], [("my-plugins", "owner/my-plugins")])
        write_state(self.cfg.profiles["work"], [], [("my-plugins", "owner/my-plugins")])

    def screen(self, keys, run=None, rows=()):
        from sampan.ui.screen import CursesScreen
        from tests.test_ui_screen import FakeScr
        self.scr = FakeScr(keys)
        self.run = run or FakeRunner(strict=True)
        return CursesScreen(self.scr, self.cfg, self.run, rows_fn=lambda c, r: list(rows))

    def frames(self):
        return ["\n".join(f) for f in self.scr.screens]

    def markets(self):
        return tomllib.loads(self.cfg.path.read_text()).get("marketplaces", {})

    def test_picker_tab_key(self):
        self.assertEqual(PluginPicker(rows("a")).key("\t"), ("tab", []))
        self.assertEqual(PluginPicker([]).key("\t"), ("tab", []))

    def test_tab_switches_and_title_marks_active(self):
        from sampan.ui.screen import MARKETS_HELP, PLUGINS_HELP
        self.assertTrue(PLUGINS_HELP.endswith("　Tab 切到來源"))
        self.assertEqual(MARKETS_HELP, "↑↓ 移動　1-9 跳列　/ 搜尋　Enter 選 profile　u 更新這個　U 全部更新　a 新增來源"
                                       "　Tab 切到 plugin　q 返回")
        s = self.screen(["\t", "\t", "\t", "q"])
        self.assertEqual(s.plugins(ask=s.ask), 0)
        heads = [f.split("\n")[0] for f in self.frames()]
        self.assertIn("sampan · plugin 管理 ［▸plugin◂｜來源］", heads[0])
        market = [f for f in self.frames() if "［plugin｜▸來源◂］" in f.split("\n")[0]]
        self.assertTrue(market)
        self.assertIn("my-plugins", market[0])
        self.assertIn(" 2/2 ", market[0])
        self.assertIn("一致", market[0])
        self.assertIn("目錄有 0 個 plugin", market[0])  # the layer-1 card is market_card
        self.assertIn("a 新增來源", market[0])
        self.assertTrue(any("［▸plugin◂｜來源］" in h for h in heads[heads.index(next(
            h for h in heads if "▸來源◂" in h)):]))  # Tab again comes back to plugin
        self.assertEqual(self.run.calls, [])

    def test_market_cells_exact(self):
        from sampan.marketplace_view import build_market_rows
        from sampan.ui.render import market_cells
        mrows = build_market_rows(self.cfg, snapshot(self.cfg))
        mp = next(r for r in mrows if r.name == "my-plugins")
        self.assertEqual(market_cells(mp, 2, 12), (" 2 my-plugins", "2/2", ("一致", "ok")))
        write_state(self.cfg.profiles["work"], [], [("my-plugins", "owner/my-plugins"), ("other", "x/other")])
        mrows = build_market_rows(self.cfg, snapshot(self.cfg))
        other = next(r for r in mrows if r.name == "other")
        self.assertEqual(market_cells(other, 3, 3), ("3 other", "1/2", ("清單外", "warn")))

    def test_market_layer2_add_profile_save_confirm(self):
        run = market_runner(self.cfg, name_for=lambda s: "new-mkt")
        keys = ["\t", "a", *"owner/new-mkt", "\n", " ", "s", "y", "\n", "q"]
        s = self.screen(keys, run)
        self.assertEqual(s.plugins(ask=s.ask), 0)
        self.assertEqual([c["args"] for c in run.calls], [["claude", "plugin", "marketplace", "add", "owner/new-mkt"]])
        self.assertEqual(self.markets()["new-mkt"], "owner/new-mkt")
        text = "\n".join(self.frames())
        self.assertIn("來源（owner/repo 或 git 網址）: owner/new-mkt", text)
        self.assertIn("sampan · plugin 管理 › owner/new-mkt", text)
        self.assertIn("名稱 （加入後才知道名稱）", text)
        self.assertIn("[x] alice", text)
        self.assertIn("← 儲存時會加入", text)
        self.assertIn("+ 加入到 alice（alice@example.com）", text)
        self.assertIn("✔ alice：已加入 new-mkt", text)
        self.assertNotIn("要生效需重開", text)  # adding a marketplace needs no restart
        last = self.frames()[-1]
        self.assertIn("［plugin｜▸來源◂］", last.split("\n")[0])
        self.assertIn("new-mkt", last)
        self.assertIn(" 1/2 ", last)

    def test_market_layer2_existing_row_card_and_update_keys(self):
        run = FakeRunner(strict=True)
        upd = ["claude", "plugin", "marketplace", "update", "my-plugins"]
        run.on(upd, lambda a: cp())
        prompts = []
        # tab, ↓ to my-plugins, Enter (layer 2: both disabled, they list tools), Esc; u updates it; U asks, n
        s = self.screen(["\t", "KEY_DOWN", "\n", "\x1b", "u", "U", "n", "\n", "q"], run)
        ask = lambda p: prompts.append(p) or s.ask(p)  # noqa: E731
        self.assertEqual(s.plugins(ask=ask), 0)
        text = "\n".join(self.frames())
        self.assertIn("sampan · plugin 管理 › my-plugins", text)
        self.assertIn("來源 owner/my-plugins", text)
        self.assertIn("目錄有 0 個 plugin", text)
        self.assertIn("[-] alice", text)
        self.assertIn("清單還有 1 個 plugin", text)  # the note column (TOGGLE_COLS) cuts the rest
        self.assertIn("alice：清單還有 1 個 plugin 用它（先在 plugin 頁移掉）", text)  # the card has the full reason
        self.assertIn("work：清單還有 1 個 plugin 用它（先在 plugin 頁移掉）", text)
        self.assertEqual([c["args"] for c in run.calls], [upd, upd])  # u: once per profile that has it
        self.assertEqual(prompts, ["全部來源、全部 profile 都更新？(y/N): "])
        self.assertIn("已取消", text)

    def test_plugin_layer2_card_lists_disabled_reasons(self):
        write_state(self.cfg.profiles["work"], [], [("my-plugins", "other/fork")])
        s = self.screen(["2", "\n", "\x1b", "q"])
        self.assertEqual(s.plugins(ask=s.ask), 0)
        text = "\n".join(self.frames())
        self.assertIn("sampan · plugin 管理 › tools@my-plugins", text)
        self.assertIn("work：來源不同：other/fork", text)

    def test_disabled_lines_wrap_in_the_card_at_narrow_width(self):
        from sampan.ui.cards import Wrap
        from sampan.ui.render import render
        from tests.test_ui_screen import frame_text
        reason = "來源不同：" + "x" * 40 + "（先改設定檔）"
        lines = pa.disabled_lines([C("work", None, False, reason)])
        self.assertEqual(lines, [f"work：{reason}"])  # plain output unchanged
        self.assertTrue(all(isinstance(line, Wrap) for line in lines))
        text = frame_text(render(title="t", left=[("a", "b", "c", "d")], cursor=0, card=lines, width=40, height=20))
        self.assertGreater(sum("x" in t for t in text), 1)
        self.assertIn("（先改設定檔）", "".join(text))

    def test_market_add_flow_refuses_token_url(self):
        prompts = []
        ask = lambda p: prompts.append(p) or "https://user:tok@example.com/r.git"  # noqa: E731
        self.assertIsNone(pa.market_add_flow(self.out.append, ask))
        self.assertEqual(prompts, ["來源（owner/repo 或 git 網址）: "])
        self.assertEqual(self.out, [TOKEN_REFUSAL])
        self.out.clear()
        self.assertIsNone(pa.market_add_flow(self.out.append, answers("not a source")))
        self.assertEqual(self.out, [f"❌ 來源要寫成 {SOURCE_RULE}"])
        self.out.clear()
        self.assertIsNone(pa.market_add_flow(self.out.append, answers("")))
        self.assertEqual(self.out, [])
        self.assertEqual(pa.market_add_flow(self.out.append, answers(" owner/new-mkt ")), "owner/new-mkt")

    def test_market_save_flow_cancel_and_drops_count_as_changed(self):
        from sampan.marketplace_view import market_choices
        write_state(self.cfg.profiles["work"], ["foo@other"], [("my-plugins", "owner/my-plugins"), ("other", "x/other")])
        run = market_runner(self.cfg)
        choices = market_choices(self.cfg, snapshot(self.cfg), "other", None)
        self.assertEqual(pa.market_save_flow(self.cfg, run, self.out.append, answers("y"), "other", choices,
                                             {"alice": False, "work": True}, source=None, new=False), [])
        self.assertEqual(self.out, ["沒有要變更的"])
        self.out.clear()
        self.assertEqual(pa.market_save_flow(self.cfg, run, self.out.append, answers("n"), "other", choices,
                                             {"alice": False, "work": False}, source=None, new=False), [])
        self.assertIn("  − 從 work（work@example.com） 移除（連同已裝的 foo 一起解除安裝）", self.out)
        self.assertEqual((self.out[-1], run.calls), ("已取消", []))
        self.out.clear()
        got = pa.market_save_flow(self.cfg, run, self.out.append, answers("y"), "other", choices,
                                  {"alice": False, "work": False}, source=None, new=False)
        self.assertEqual(got, ["work"])
        self.assertEqual([c["args"] for c in run.calls], [["claude", "plugin", "marketplace", "remove", "other"]])

    def test_plain_t_toggles_tab(self):
        prompts = []
        it = iter(["t", "t", "q"])
        ask = lambda p: prompts.append(p) or next(it)  # noqa: E731
        self.assertEqual(pa.plain_plugins(self.cfg, FakeRunner(strict=True), self.out.append, ask), 0)
        self.assertEqual(prompts, [pa.MENU, pa.MARKET_MENU, pa.MENU])
        self.assertIn("t 切換 plugin／來源", pa.MENU)
        self.assertIn("t 切換 plugin／來源", pa.MARKET_MENU)
        self.assertIn("a 新增來源", pa.MARKET_MENU)
        self.assertIn("═══ sampan · plugin 管理 ［plugin｜▸來源◂］ ═══", self.out)
        self.assertIn("  [ 2] my-plugins                   2/2  一致", self.out)
        self.assertEqual(self.out.count("═══ sampan · plugin 管理 ［▸plugin◂｜來源］ ═══"), 2)

    def test_plain_market_remove_with_drops_offers_restart(self):
        write_state(self.cfg.profiles["work"], ["foo@other"], [("my-plugins", "owner/my-plugins"), ("other", "x/other")])
        run = market_runner(self.cfg)
        it = iter(["t", "3", "1 2", "s", "y", "x", "q"])
        prompts = []
        ask = lambda p: prompts.append(p) or next(it)  # noqa: E731
        rows_ = [status_row("a", profile="work")]
        self.assertEqual(pa.plain_plugins(self.cfg, run, self.out.append, ask, rows_fn=lambda c, r: rows_), 0)
        self.assertIn("── other ──", self.out)
        self.assertIn("  [2] [ ] work  x/other  ← 儲存時會移除", self.out)
        self.assertIn("  [1] [-] alice  —  不在設定檔，只能移除", self.out)
        self.assertIn("  alice：不在設定檔，只能移除", self.out)  # disabled reasons listed under the rows
        self.assertIn("alice 不能改：不在設定檔，只能移除", self.out)
        self.assertEqual([c["args"] for c in run.calls], [["claude", "plugin", "marketplace", "remove", "other"]])
        self.assertIn("要生效需重開 work 的 session（1 個）：按 R 立刻重開，其他鍵稍後（輸入 R）: ", prompts)
        self.assertEqual(prompts[-1], pa.MARKET_MENU)  # back on the 來源 tab

    def test_plain_market_add_update_and_update_all(self):
        run = market_runner(self.cfg, name_for=lambda s: "new-mkt")
        upd = ["claude", "plugin", "marketplace", "update", "my-plugins"]
        run.on(upd, lambda a: cp())
        it = iter(["t", "a", "owner/new-mkt", "1", "s", "y", "u2", "U", "n", "a", "https://u:t@h/r.git", "q"])
        prompts = []
        ask = lambda p: prompts.append(p) or next(it)  # noqa: E731
        self.assertEqual(pa.plain_plugins(self.cfg, run, self.out.append, ask), 0)
        self.assertIn("── owner/new-mkt ──", self.out)
        self.assertEqual(self.markets()["new-mkt"], "owner/new-mkt")
        self.assertEqual([c["args"] for c in run.calls],
                         [["claude", "plugin", "marketplace", "add", "owner/new-mkt"], upd, upd])
        self.assertIn("全部來源、全部 profile 都更新？(y/N): ", prompts)
        self.assertIn(TOKEN_REFUSAL, self.out)


if __name__ == "__main__":
    unittest.main()
