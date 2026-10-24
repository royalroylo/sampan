import json
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sampan import profile_ops
from sampan.status import State
from sampan.ui import cmd_ui
from sampan.ui import profile_actions as pa
from sampan.ui.render import frame_text, render
from sampan.ui.state import ProfilePicker
from sampan.usage import Usage, Window
from tests.fakes import FakeRunner, cp
from tests.helpers import TOML, local_tz, make_env, status_row

NOW = datetime(2026, 10, 24, 4, 30, tzinfo=timezone.utc)  # 12:30 in Asia/Taipei
OLD = TOML + '\n[profiles.old]\nconfig_dir = "~/.claude-profiles/old"\ndisabled = true\n'
ALICE_USAGE = Usage(Window(42, NOW + timedelta(hours=2)), Window(18, NOW + timedelta(days=3)),
                    datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc))


def usage_of(table):
    return lambda cfg, p: table.get(p.name)


def answers(*xs):
    it = iter(xs)
    return lambda _p: next(it)


def runner():
    r = FakeRunner(strict=True)
    r.on(["claude", "auth", "status"], lambda a: cp(out=json.dumps({"email": "a@example.com"})))
    r.on(["codex", "login", "status"], cp(out="Logged in using ChatGPT\n"))
    r.on(["tmux", "-u", "list-panes"], cp()).on(["tmux", "-u", "list-windows"], cp())
    return r


class AccountLinesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()), OLD)

    def test_account_lines_exact(self):
        rows = [status_row("a"), status_row("b"), status_row("c", State.STOPPED, profile="work"),
                status_row("d", profile="old"), status_row("x", State.STOPPED, agent="codex", profile="codex")]
        with local_tz("Asia/Taipei"):
            lines = pa.account_lines(self.cfg, rows, NOW, usage_fn=usage_of({"alice": ALICE_USAGE}))
        self.assertEqual(lines, ["alice 2 個執行中・5h 42%・週 18%（12:00）", "work 0 個執行中",
                                 "codex（Codex） 0 個執行中"])

    def test_codex_running_counts_only_codex_rows(self):
        rows = [status_row("x", agent="codex", profile="codex"), status_row("y", profile="codex")]
        lines = pa.account_lines(self.cfg, rows, NOW, usage_fn=usage_of({}))
        self.assertEqual(lines[-1], "codex（Codex） 1 個執行中")

    def test_render_accounts_between_list_and_messages_and_kept_when_small(self):
        args = dict(title="sampan", left=[("s1",), ("s2",), ("s3",)], cursor=0, card=[], help="q 離開", cols=(),
                    messages=["做完了"], accounts=["alice 2 個執行中", "work 0 個執行中"])
        lines = frame_text(render(**args, width=80, height=20))
        at = [i for i, x in enumerate(lines) if "個執行中" in x]
        self.assertEqual([lines[i].strip() for i in at], ["alice 2 個執行中", "work 0 個執行中"])
        self.assertLess(max(i for i, x in enumerate(lines) if "s3" in x), at[0])
        self.assertLess(at[-1], next(i for i, x in enumerate(lines) if "做完了" in x))
        small = frame_text(render(**args, width=80, height=6))
        self.assertEqual(len(small), 6)
        self.assertTrue(any("alice 2 個執行中" in x for x in small))
        self.assertTrue(small[-1].strip().startswith("q 離開"))


class ProfilePickerTest(unittest.TestCase):
    def test_running_column_fits_two_digit_counts(self):
        rows = [profile_ops.ProfileRow(n, "claude", Path("/x"), "", False, False, k, 1, "a")
                for n, k in (("alice", 12), ("bob", 4))]
        lines = frame_text(render(title="t", left=[pa.profile_cells(r) for r in rows], cursor=0, card=[],
                                  cols=pa.profile_cols(rows), width=80, height=10))
        self.assertTrue(any("12 個執行中" in line for line in lines), lines)
        self.assertEqual(pa.profile_cols([]), pa.PROFILE_COLS)

    def test_keys(self):
        p = ProfilePicker([profile_ops.ProfileRow(n, "claude", Path("/x"), "", False, False, 0, 0, "")
                           for n in ("alice", "work")])
        self.assertEqual(p.key("r"), ("rename", ["alice"]))
        self.assertIsNone(p.key("KEY_DOWN"))
        self.assertEqual(p.key("d"), ("toggle", ["work"]))
        self.assertEqual(p.key("f"), ("forget", ["work"]))
        self.assertEqual(p.key("i"), ("statusline", ["work"]))
        self.assertEqual(p.key("a"), ("add", []))
        self.assertEqual(p.key("q"), ("back", []))
        self.assertEqual(p.key("\x1b"), ("back", []))
        for k in ("n", "p", "o", "\n", " ", "s"):
            self.assertIsNone(p.key(k))
        self.assertEqual(ProfilePicker([]).key("a"), ("add", []))
        self.assertIsNone(ProfilePicker([]).key("r"))


class Base(unittest.TestCase):
    toml = OLD

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp, self.toml)
        self.msgs = []
        patch = mock.patch.object(profile_ops, "running_sessions", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def data(self):
        return tomllib.loads(self.cfg.path.read_text())

    def screen(self, keys, run=None, rows=(), h=30, w=120, **kw):
        from sampan.ui.screen import CursesScreen
        from tests.test_ui_screen import FakeScr
        self.scr = FakeScr(keys, h=h, w=w)
        self.run = run or runner()
        return CursesScreen(self.scr, self.cfg, self.run, rows_fn=lambda c, r: list(rows),
                            usage_fn=usage_of({"alice": ALICE_USAGE}), **kw)

    def frames(self):
        return ["\n".join(f) for f in self.scr.screens]


class MainListTest(Base):
    def test_main_list_shows_account_lines(self):
        s = self.screen(["q"], rows=[status_row("a")])
        s.now_fn = lambda: NOW
        with local_tz("Asia/Taipei"):
            self.assertEqual(s.pick([status_row("a")], None), ("quit", []))
        last = self.frames()[-1]
        self.assertIn(" alice 1 個執行中・5h 42%・週 18%（12:00）", last)
        self.assertIn(" work 0 個執行中", last)
        self.assertIn(" codex（Codex） 0 個執行中", last)
        self.assertNotIn("old", last)
        self.assertIn("p plugin　o 帳號　q 離開", last)

    def test_account_usage_recomputed_at_most_every_60s(self):
        from tests.test_ui_screen import TICK
        calls = []
        t = [0.0]
        s = self.screen([TICK, TICK, TICK, "q"], clock=lambda: t[0])
        s.usage_fn = lambda cfg, p: calls.append(p.name)

        def tick():
            t[0] += 30
        s.scr.on_tick = tick
        s.pick([], None)
        self.assertEqual(calls.count("alice"), 2)  # on entry, then once 60 s later (ticks at 30, 60, 90 s)

    def test_o_opens_profiles_and_q_returns(self):
        from sampan.ui import curses_ui
        from tests.test_ui_screen import FakeScr
        box = {}

        def wrapper(fn):
            box["scr"] = FakeScr(["o", "q", "q"])
            return fn(box["scr"])
        rc = curses_ui(self.cfg, runner(), lambda _l: None, wrapper=wrapper, rows_fn=lambda c, r: [])
        self.assertEqual(rc, 0)
        screens = ["\n".join(f) for f in box["scr"].screens]
        shown = [f for f in screens if f.startswith("═══ sampan · 帳號")]
        self.assertTrue(shown)
        self.assertIn("↑↓ 移動　r 改名　d 停用／恢復　f forget　i 裝狀態列　a 新增　q 返回", shown[0])
        self.assertIn("old（停用）", shown[0])
        self.assertIn("目錄      ~/.claude", shown[0])
        self.assertIn("帳號      a@example.com", shown[0])
        self.assertIn("plugin 數 2", shown[0])
        self.assertIn("狀態列    沒裝", shown[0])
        self.assertTrue(screens[-1].startswith("═══ sampan"))
        self.assertNotIn("帳號", screens[-1].split("\n")[0])

    def test_main_list_o_key(self):
        from sampan.ui.state import Picker
        self.assertEqual(Picker([]).key("o"), ("profiles", []))


class ProfileScreenTest(Base):
    def test_profile_screen_rename_flow(self):
        s = self.screen(["KEY_DOWN", "r", "w", "2", "\n", "q"])
        self.assertEqual(s.profiles(ask=s.ask), 0)
        self.assertIn("w2", self.data()["profiles"])
        self.assertNotIn("work", self.data()["profiles"])
        frames = self.frames()
        self.assertTrue(any("work 的新名稱（Enter 取消）: w2_" in f for f in frames))
        last = frames[-1]
        self.assertIn("✔ 已把 profile work 改名為 w2（登記表 0 列一起改）", last)
        self.assertIn("w2", last.split("\n")[2])  # the list was re-read and the cursor followed the new name
        self.assertIn("w2", s.cfg.profiles)  # the main list goes on with the new config

    def test_codex_card_and_disabled_card(self):
        s = self.screen(["KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "q"])
        s.profiles(ask=s.ask)
        frames = self.frames()
        codex = frames[3]
        self.assertIn("帳號      Logged in using ChatGPT", codex)
        self.assertNotIn("plugin 數", codex)
        self.assertIn("狀態列    —", codex)
        self.assertIn("額度      —", codex)
        self.assertIn("old（停用）", frames[2])

    def test_forget_only_disabled_and_toggle_enable(self):
        s = self.screen(["f", "KEY_DOWN", "KEY_DOWN", "d", "q"])
        s.profiles(ask=s.ask)
        text = "\n".join(self.frames())
        self.assertIn("❌ 只有停用的 profile 能 forget；先按 d 停用", text)
        self.assertIn("✔ 已恢復 old", text)
        self.assertNotIn("disabled", self.data()["profiles"]["old"])

    def test_disable_asks_then_forget(self):
        s = self.screen(["KEY_DOWN", "d", "y", "\n", "f", "w", "o", "r", "k", "\n", "q"])
        s.profiles(ask=s.ask)
        text = "\n".join(self.frames())
        self.assertIn("停用 work？目錄保留，之後可以按 d 恢復 (y/N): ", text)
        self.assertIn("✔ 已停用 work", text)
        self.assertIn("輸入 work 確認：", text)
        self.assertNotIn("work", self.data()["profiles"])

    def test_statusline_install_on_cursor_profile(self):
        sampan = str(self.tmp / "bin" / "sampan")
        s = self.screen(["KEY_DOWN", "i", "y", "\n", "q"], sampan_path=sampan)
        s.profiles(ask=s.ask)
        settings = json.loads((self.cfg.home / ".claude-profiles" / "work" / "settings.json").read_text())
        self.assertEqual(settings["statusLine"]["command"], f"{sampan} statusline")
        self.assertFalse((self.cfg.home / ".claude" / "settings.json").exists())
        self.assertIn("狀態列    已接上", self.frames()[-1])

    def test_statusline_refused_for_codex(self):
        s = self.screen(["KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "i", "q"])
        s.profiles(ask=s.ask)
        self.assertIn("Codex 沒有狀態列；i 只裝 Claude profile 的", self.frames()[-1])

    def test_add_without_login_terminal_says_how(self):
        s = self.screen(["a", "n", "e", "w", "\n", "x", "\n", "q"])
        s.profiles(ask=s.ask)
        self.assertIn("新增帳號要登入，請離開後執行：sampan profile add new --codex", self.frames()[-1])
        self.assertNotIn("new", self.data()["profiles"])

    def test_add_claude_runs_login_outside(self):
        logins = []
        run = runner().on(["claude", "auth", "--help"], cp(out="Commands:\n  login  Log in\n"))
        s = self.screen(["a", "c", "x", "\n", "c", "\n", "q"], run,
                        interactive=lambda args, env: logins.append((args, env["CLAUDE_CONFIG_DIR"])) or 0)
        s.profiles(ask=s.ask)
        self.assertEqual(logins, [(["claude", "auth", "login"], str(self.cfg.home / ".claude-profiles" / "cx"))])
        self.assertIn("cx", self.data()["profiles"])
        self.assertIn("cx", self.frames()[-1])


class PlainTest(Base):
    def test_plain_profiles_disable_flow(self):
        rc = pa.plain_profiles(self.cfg, runner(), self.msgs.append, answers("2", "d", "y", "q"),
                               rows_fn=lambda c, r: [], usage_fn=usage_of({}))
        self.assertEqual(rc, 0)
        self.assertTrue(self.data()["profiles"]["work"]["disabled"])
        text = "\n".join(self.msgs)
        self.assertIn("═══ sampan · 帳號 ═══", text)
        self.assertIn("✔ 已停用 work", text)
        listings = [m for m in self.msgs if m.startswith("  [ 2]")]
        self.assertEqual(len(listings), 2)
        self.assertIn("work", listings[0])
        self.assertNotIn("（停用）", listings[0])
        self.assertIn("work（停用）", listings[1])

    def test_plain_invalid_and_quit_inside(self):
        rc = pa.plain_profiles(self.cfg, runner(), self.msgs.append, answers("9", "1", "q", None),
                               rows_fn=lambda c, r: [], usage_fn=usage_of({}))
        self.assertEqual(rc, 0)
        self.assertIn("無效編號：9", self.msgs)

    def test_plain_menu_o_routes_to_profiles(self):
        calls = []
        rc = cmd_ui(self.cfg, FakeRunner(), [].append, answers("o", "q"), loop=True, rows_fn=lambda c, r: [],
                    profiles_fn=lambda **kw: calls.append(kw) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("ask", calls[0])

    def test_help_mentions_o(self):
        from sampan.ui.actions import HELP
        self.assertIn("　o 帳號", HELP)


class ConfigReloadTest(Base):
    """cmd_ui picks up config.toml edited elsewhere before the next action (I3)."""

    def picker(self, *actions):
        it = iter(actions)
        return lambda rows, current: next(it)

    def test_config_edited_while_the_list_is_shown_is_seen_by_the_very_next_action(self):
        seen, pushed, picks = [], [], []
        path = self.cfg.path

        def picker(rows, current):
            picks.append(1)
            if len(picks) == 1:  # edited in another terminal while sampan waits for a key
                path.write_text(path.read_text().replace("[profiles.work]", "[profiles.job]"))
                return ("save", [])
            return ("quit", [])
        msgs = []
        rc = cmd_ui(self.cfg, FakeRunner(), msgs.append, loop=True, rows_fn=lambda c, r: [],
                    save_fn=lambda cfg, run, out: seen.append(cfg) or 0, picker_fn=picker, set_cfg_fn=pushed.append)
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertIn("job", seen[0].profiles)
        self.assertNotIn("work", seen[0].profiles)
        self.assertEqual([list(c.profiles) for c in pushed], [["alice", "job"]])  # reloaded and pushed once

    def test_broken_edit_warns_once(self):
        path = self.cfg.path
        picks = []

        def picker(rows, current):
            picks.append(1)
            if len(picks) == 1:
                path.write_text("not toml [")
            return ("save", []) if len(picks) < 3 else ("quit", [])
        msgs, seen = [], []
        cmd_ui(self.cfg, FakeRunner(), msgs.append, loop=True, rows_fn=lambda c, r: [],
               save_fn=lambda cfg, run, out: seen.append(cfg) or 0, picker_fn=picker)
        self.assertEqual(sum("設定檔讀取失敗" in m for m in msgs), 1)
        self.assertTrue(all(c is self.cfg for c in seen))

    def test_unchanged_config_is_not_reread(self):
        seen = []
        cmd_ui(self.cfg, FakeRunner(), [].append, loop=True, rows_fn=lambda c, r: [],
               save_fn=lambda cfg, run, out: seen.append(cfg) or 0,
               picker_fn=self.picker(("save", []), ("save", []), ("quit", [])))
        self.assertIs(seen[0], self.cfg)
        self.assertIs(seen[1], self.cfg)

    def test_config_reloaded_even_when_profile_screen_raises(self):
        import os
        seen = []
        st = self.cfg.path.stat()

        def profiles(**kw):
            # same size and mtime: only the reload after the profile screen can notice the change
            self.cfg.path.write_text(self.cfg.path.read_text().replace("[profiles.work]", "[profiles.wolk]"))
            os.utime(self.cfg.path, ns=(st.st_atime_ns, st.st_mtime_ns))
            raise RuntimeError("boom")
        msgs = []
        cmd_ui(self.cfg, FakeRunner(), msgs.append, loop=True, rows_fn=lambda c, r: [], profiles_fn=profiles,
               save_fn=lambda cfg, run, out: seen.append(cfg) or 0,
               picker_fn=self.picker(("profiles", []), ("save", []), ("quit", [])))
        self.assertIn("❌ profiles 失敗：boom", msgs)
        self.assertIn("wolk", seen[0].profiles)

    def test_curses_screen_gets_the_reloaded_config(self):
        from sampan.ui import curses_ui
        from tests.test_ui_screen import FakeScr
        box = {}

        def wrapper(fn):
            box["scr"] = FakeScr(["w", "w", "q"])
            return fn(box["scr"])

        def save(cfg, run, out):
            cfg.path.write_text(cfg.path.read_text().replace("[profiles.work]", "[profiles.job]"))
            return 0
        with mock.patch("sampan.ui.screen.CursesScreen.pick", autospec=True,
                        side_effect=lambda self_, rows, cur: box.setdefault("cfgs", []).append(self_.cfg)
                        or (("save", []) if len(box["cfgs"]) < 3 else ("quit", []))):
            curses_ui(self.cfg, runner(), lambda _l: None, wrapper=wrapper, rows_fn=lambda c, r: [], save_fn=save)
        self.assertIn("work", box["cfgs"][0].profiles)
        self.assertIn("job", box["cfgs"][1].profiles)


if __name__ == "__main__":
    unittest.main()
