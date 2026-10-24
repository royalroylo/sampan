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
from tests.fakes import FakeRunner
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
        self.assertIn("═══ sampan · plugin 管理 ═══", text)
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


if __name__ == "__main__":
    unittest.main()
