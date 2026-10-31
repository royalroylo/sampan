import shlex
import tempfile
import unittest
from pathlib import Path

from sampan import steps
from sampan.registry import Row
from sampan.screens import ANSI_RE, claude_input_state
from sampan.steps import RC_DISCONNECT, RcMenu, rc_menu, rc_reset, rc_step, rc_wanted
from tests.helpers import make_env

A = "aaaaaaaa-0000-4000-8000-000000000001"
FIX = Path(__file__).parent / "fixtures" / "rc"
ALIAS = "讀書 PM"


def fx(name):
    return (FIX / f"{name}.txt").read_text(encoding="utf-8")


ACTIVE = fx("active_idle")
MENU_CONTINUE = fx("menu_cursor_continue")
MENU_QR = fx("menu_cursor_qr")
MENU_DISCONNECT = fx("menu_cursor_disconnect")
DISCONNECTED = fx("disconnected")
RECONNECTED = fx("reconnected")
# the boot screen before the bridge is up: the same screen without the "/remote-control is active" lines
BOOTING = "\n".join(ln for ln in ACTIVE.split("\n") if "remote-control is active" not in ln
                    and "claude.ai/code" not in ln)
DRAFT = ACTIVE.replace("❯\xa0\n", "❯\xa0half typed\n")
OTHER_MENU = ACTIVE.split("────")[0] + "\n Some other dialog\n ❯ 1. Yes\n   2. No\n Enter to confirm · Esc to cancel\n"


def _split(screen):
    """(transcript, input box and below) of a fixture screen."""
    head, tail = screen.split("────", 1)
    return head, "────" + tail


# a resumed conversation reset under the same alias on its previous launch: the old reset is in the scrollback
OLD_RESET = RECONNECTED
OLD_THEN_DISCONNECTED = (_split(RECONNECTED)[0] + "❯ /remote-control\n  ⎿  Remote Control disconnected.\n"
                         + _split(DISCONNECTED)[1])
OLD_THEN_RECONNECTED = (_split(OLD_THEN_DISCONNECTED)[0] + f"❯ /remote-control {ALIAS}\n"
                        "  /remote-control is active · Continue here, on your phone, or at\n"
                        "  https://claude.ai/code/session_XXXX\n" + _split(DISCONNECTED)[1])
# the input box with Claude's dim placeholder (needs the escape codes to be told from typed text)
PLACEHOLDER_BOX = "\n".join((Path(__file__).parent / "fixtures" / "claude_empty_placeholder.ansi")
                            .read_text(encoding="utf-8").rstrip("\n").split("\n")[-6:]) + "\n"
ACTIVE_PLACEHOLDER = _split(ACTIVE)[0].rstrip("\n").rsplit("\n", 1)[0] + "\n" + PLACEHOLDER_BOX


class FakeTmux:
    """A pane whose screen changes as keys are sent. Each state is a screen or a list of screens: every capture takes
    the next one and the last one sticks (so a list models a screen that settles after a few polls)."""

    def __init__(self, start, after=()):
        self.state = self._queue(start)
        self.after = [self._queue(s) for s in after]  # the state after each send_keys, in order
        self.sent = []
        self.captures = 0
        self.escapes = []

    @staticmethod
    def _queue(s):
        return list(s) if isinstance(s, list) else [s]

    def send_keys(self, target, *keys):
        self.sent.append(list(keys))
        if self.after:
            self.state = self.after.pop(0)

    def capture(self, target, lines=30, escapes=False):
        self.captures += 1
        self.escapes.append(escapes)
        screen = self.state.pop(0) if len(self.state) > 1 else self.state[0]
        return screen if escapes else ANSI_RE.sub("", screen)


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


HAPPY_KEYS = [["/remote-control", "Enter"], ["Up"], ["Up"], ["Enter"], [f"/remote-control {ALIAS}", "Enter"]]


def happy_tmux():
    return FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED, RECONNECTED])


class RcMenuTest(unittest.TestCase):
    def test_rc_menu_parses_options_and_cursor(self):
        opts = ["Disconnect this session", "Show QR code", "Continue"]
        self.assertEqual(rc_menu(MENU_CONTINUE), RcMenu(opts, 2))
        self.assertEqual(rc_menu(MENU_QR), RcMenu(opts, 1))
        self.assertEqual(rc_menu(MENU_DISCONNECT), RcMenu(opts, 0))
        self.assertEqual(rc_menu(MENU_DISCONNECT).options[0], RC_DISCONNECT)

    def test_rc_menu_no_cursor_is_minus_one(self):
        self.assertEqual(rc_menu(MENU_CONTINUE.replace("❯ Continue", "  Continue")).cursor, -1)

    def test_rc_menu_none_for_other_screens(self):
        trust = (" Accessing workspace:\n\n /Users/u/proj\n\n Quick safety check: Is this a project you trust?\n\n"
                 " ❯ 1. Yes, I trust this folder\n   2. No, exit\n\n Enter to confirm · Esc to cancel\n")
        for screen in (ACTIVE, DISCONNECTED, RECONNECTED, trust, OTHER_MENU, ""):
            self.assertIsNone(rc_menu(screen))


class RcResetTest(unittest.TestCase):
    def setUp(self):
        self.sleep = Sleeps()

    def reset(self, tmux, **kw):
        return rc_reset(tmux, "%1", ALIAS, sleep=self.sleep, **kw)

    def test_rc_reset_happy_path_keys(self):
        tmux = happy_tmux()
        self.assertIsNone(self.reset(tmux))
        self.assertEqual(tmux.sent, HAPPY_KEYS)

    def test_rc_reset_waits_for_the_bridge(self):
        tmux = FakeTmux([BOOTING, BOOTING, ACTIVE], [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED,
                                                     RECONNECTED])
        self.assertIsNone(self.reset(tmux))
        self.assertEqual(tmux.sent, HAPPY_KEYS)

    def test_rc_reset_waits_for_bridge_then_gives_up(self):
        tmux = FakeTmux(BOOTING)
        self.assertEqual(self.reset(tmux, wait=3), "沒看到 Remote Control 連上")
        self.assertEqual(tmux.sent, [])
        self.assertEqual(sum(self.sleep), 3)  # the wait is counted in polls of 0.5 s
        self.assertTrue(all(s == steps.POLL for s in self.sleep))

    def test_rc_reset_typed_text_in_prompt_does_nothing(self):
        tmux = FakeTmux(DRAFT)
        self.assertEqual(self.reset(tmux), "輸入框有字")
        self.assertEqual(tmux.sent, [])

    def test_rc_reset_blocked_prompt_does_nothing(self):
        tmux = FakeTmux(OTHER_MENU.replace("Some other dialog", "/remote-control is active"))
        self.assertEqual(self.reset(tmux), "找不到空的輸入框")
        self.assertEqual(tmux.sent, [])

    def test_dim_placeholder_counts_as_empty(self):
        self.assertEqual(claude_input_state(ACTIVE_PLACEHOLDER), "empty")
        self.assertEqual(claude_input_state(ANSI_RE.sub("", ACTIVE_PLACEHOLDER)), "draft")  # why escapes are needed
        tmux = FakeTmux(ACTIVE_PLACEHOLDER, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED, RECONNECTED])
        self.assertIsNone(self.reset(tmux))
        self.assertEqual(tmux.sent, HAPPY_KEYS)
        self.assertTrue(all(tmux.escapes))

    def test_old_reset_with_same_alias_does_not_confirm_the_new_one(self):
        tmux = FakeTmux(OLD_RESET, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, OLD_THEN_DISCONNECTED,
                                    OLD_THEN_DISCONNECTED])
        self.assertEqual(self.reset(tmux, wait=2), "重新連線沒確認")
        self.assertEqual(tmux.sent, HAPPY_KEYS)

    def test_old_reset_with_same_alias_then_new_reconnect(self):
        tmux = FakeTmux(OLD_RESET, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, OLD_THEN_DISCONNECTED,
                                    [OLD_THEN_DISCONNECTED, OLD_THEN_RECONNECTED]])
        self.assertIsNone(self.reset(tmux))
        self.assertEqual(tmux.sent, HAPPY_KEYS)

    def test_old_active_in_scrollback_without_menu_escapes(self):
        # resumed: the old "is active" line passes step 1 before the new bridge is up; no menu → Escape, no Enter
        tmux = FakeTmux(OLD_RESET, [OLD_RESET])
        self.assertEqual(self.reset(tmux), "選單沒出現")
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Escape"]])

    def test_alias_ending_in_semicolon_is_sent_literally(self):
        alias = "PM;"
        done = RECONNECTED.replace(f"/remote-control {ALIAS}", f"/remote-control {alias}")
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED, done])
        self.assertIsNone(rc_reset(tmux, "%1", alias, sleep=self.sleep))
        self.assertEqual(tmux.sent[-1], ["/remote-control PM\\;", "Enter"])  # tmux: a trailing \; is a literal ;

    def test_draft_typed_after_disconnect_sends_no_reconnect(self):
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT,
                                 [DISCONNECTED, DISCONNECTED.replace("❯\xa0\n", "❯\xa0typed\n")]])
        self.assertEqual(self.reset(tmux), "重新連線前輸入框不是空的")
        self.assertEqual(tmux.sent, HAPPY_KEYS[:4])

    def test_modal_after_disconnect_escapes(self):
        modal = _split(DISCONNECTED)[0] + " Some dialog\n ❯ 1. Yes\n   2. No\n Enter to confirm · Esc to cancel\n"
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, [DISCONNECTED, modal]])
        self.assertEqual(self.reset(tmux), "重新連線前輸入框不是空的")
        self.assertEqual(tmux.sent, HAPPY_KEYS[:4] + [["Escape"]])

    def test_ctrl_c_with_the_menu_open_escapes(self):
        sleep = Sleeps()

        def interrupt(s):
            sleep(s)
            raise KeyboardInterrupt
        tmux = FakeTmux(ACTIVE, [BOOTING])  # the menu never shows: rc_reset sleeps while it may be opening
        with self.assertRaises(KeyboardInterrupt):
            rc_reset(tmux, "%1", ALIAS, sleep=interrupt)
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Escape"]])

    def test_tmux_error_with_the_menu_open_escapes(self):
        class Flaky(FakeTmux):
            def send_keys(self, target, *keys):
                if keys == ("Up",):
                    raise OSError("tmux gone")
                super().send_keys(target, *keys)
        tmux = Flaky(ACTIVE, [MENU_CONTINUE])
        with self.assertRaises(OSError):
            self.reset(tmux)
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Escape"]])

    def test_rc_reset_menu_never_shows_escapes(self):
        tmux = FakeTmux(ACTIVE, [ACTIVE])
        self.assertEqual(self.reset(tmux), "選單沒出現")
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Escape"]])

    def test_rc_reset_unknown_screen_escapes(self):
        tmux = FakeTmux(ACTIVE, [OTHER_MENU])
        self.assertEqual(self.reset(tmux), "選單沒出現")
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Escape"]])
        self.assertNotIn(["Enter"], tmux.sent)

    def test_rc_reset_cursor_not_on_disconnect_escapes(self):
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_CONTINUE, MENU_CONTINUE, MENU_CONTINUE])
        self.assertEqual(self.reset(tmux), "游標沒到 Disconnect")
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Up"], ["Up"], ["Up"], ["Escape"]])

    def test_rc_reset_menu_without_disconnect_escapes(self):
        menu = MENU_CONTINUE.replace("     Disconnect this session\n", "")
        tmux = FakeTmux(ACTIVE, [menu, menu, menu, menu])
        self.assertEqual(self.reset(tmux), "游標沒到 Disconnect")
        self.assertNotIn(["Enter"], tmux.sent)
        self.assertEqual(tmux.sent[-1], ["Escape"])

    def test_rc_reset_menu_gone_after_up_escapes(self):
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, OTHER_MENU])
        self.assertEqual(self.reset(tmux), "游標沒到 Disconnect")
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Up"], ["Escape"]])

    def test_rc_reset_slow_redraw_does_not_overshoot(self):
        # the cursor only moves a poll after Up: wait for it instead of pressing Up again
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, [MENU_CONTINUE, MENU_QR], [MENU_QR, MENU_DISCONNECT],
                                 DISCONNECTED, RECONNECTED])
        self.assertIsNone(self.reset(tmux))
        self.assertEqual(tmux.sent, HAPPY_KEYS)

    def test_rc_reset_cursor_already_on_disconnect(self):
        tmux = FakeTmux(ACTIVE, [MENU_DISCONNECT, DISCONNECTED, RECONNECTED])
        self.assertIsNone(self.reset(tmux))
        self.assertEqual(tmux.sent, [["/remote-control", "Enter"], ["Enter"], [f"/remote-control {ALIAS}", "Enter"]])

    def test_rc_reset_no_disconnect_seen(self):
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, ACTIVE])
        self.assertEqual(self.reset(tmux), "沒看到斷開")
        self.assertEqual(tmux.sent, HAPPY_KEYS[:4])

    def test_old_disconnect_in_scrollback_does_not_count(self):
        # an earlier reset in the resumed transcript: its "disconnected" is followed by a reconnect, so it is not ours
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, RECONNECTED])
        self.assertEqual(self.reset(tmux), "沒看到斷開")

    def test_rc_reset_reconnect_not_confirmed(self):
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED, DISCONNECTED])
        self.assertEqual(self.reset(tmux, wait=2), "重新連線沒確認")
        self.assertEqual(tmux.sent, HAPPY_KEYS)

    def test_reconnect_under_another_name_is_not_confirmed(self):
        other = RECONNECTED.replace(f"/remote-control {ALIAS}", "/remote-control other")
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED, other])
        self.assertEqual(self.reset(tmux, wait=2), "重新連線沒確認")

    def test_wrapped_alias_line_is_confirmed(self):
        alias = "很長的名字" * 10
        wrapped = RECONNECTED.replace(f"❯ /remote-control {ALIAS}\n",
                                      f"❯ /remote-control {alias[:30]}\n  {alias[30:]}\n")
        tmux = FakeTmux(ACTIVE, [MENU_CONTINUE, MENU_QR, MENU_DISCONNECT, DISCONNECTED, wrapped])
        self.assertIsNone(rc_reset(tmux, "%1", alias, sleep=self.sleep))


def rc_cfg(on):
    cfg = make_env(Path(tempfile.mkdtemp()))
    (cfg.profiles["alice"].config_dir / "settings.json").write_text(
        '{"remoteControlAtStartup": %s}' % ("true" if on else "false"))
    return cfg


def row(alias="", agent="claude", profile="alice"):
    return Row("rc-probe", agent, profile, A, "/tmp/w", alias=alias)


class RcStepTest(unittest.TestCase):
    def setUp(self):
        self.out, self.sleep = [], Sleeps()

    def test_rc_wanted(self):
        self.assertTrue(rc_wanted(rc_cfg(True), row()))
        self.assertFalse(rc_wanted(rc_cfg(False), row()))
        self.assertFalse(rc_wanted(rc_cfg(True), row(agent="codex", profile="")))
        self.assertFalse(rc_wanted(rc_cfg(True), row(profile="gone")))

    def test_rc_step_off_profile_sends_nothing(self):
        tmux = happy_tmux()
        rc_step(rc_cfg(False), tmux, row(ALIAS), "%1", self.out.append, sleep=self.sleep)
        self.assertEqual((tmux.sent, self.out, tmux.captures), ([], [], 0))

    def test_rc_step_success_line(self):
        tmux = happy_tmux()
        rc_step(rc_cfg(True), tmux, row(ALIAS), "%1", self.out.append, sleep=self.sleep)
        self.assertEqual(tmux.sent, HAPPY_KEYS)
        self.assertEqual(self.out, ["  … rc-probe：RC 校正中（最多約 20 秒）", f"  · rc-probe Remote Control 名稱已校正為「{ALIAS}」"])

    def test_rc_step_failure_line(self):
        rc_step(rc_cfg(True), FakeTmux(DRAFT), row(ALIAS), "%1", self.out.append, sleep=self.sleep)
        self.assertEqual(self.out, ["  … rc-probe：RC 校正中（最多約 20 秒）", "  ⚠ rc-probe：RC 校正沒做：輸入框有字；之後按 S 重開（或 sampan restart rc-probe）會再校正"])

    def test_rc_step_profile_override(self):
        tmux = happy_tmux()
        rc_step(rc_cfg(True), tmux, row(ALIAS, profile="work"), "%1", self.out.append, sleep=self.sleep,
                profile="alice")
        self.assertEqual(tmux.sent, HAPPY_KEYS)

    def test_rc_step_never_raises(self):
        class Broken(FakeTmux):
            def capture(self, target, lines=30, escapes=False):
                raise OSError("tmux gone")
        rc_step(rc_cfg(True), Broken(ACTIVE), row(ALIAS), "%1", self.out.append, sleep=self.sleep)
        self.assertEqual(self.out, ["  … rc-probe：RC 校正中（最多約 20 秒）", "  ⚠ rc-probe：RC 校正沒做：tmux gone；之後按 S 重開（或 sampan restart rc-probe）會再校正"])


class LaunchStepTest(unittest.TestCase):
    def test_launch_uses_alias_for_name_and_rc(self):
        cfg = rc_cfg(True)
        tmux = FakeTmux("")
        line = steps.launch(cfg, tmux, "%1", row(ALIAS), sleep=Sleeps())
        self.assertIn("--name '讀書 PM' --remote-control '讀書 PM'", line)
        self.assertEqual(shlex.split(line)[-6:], ["--resume", A, "--name", ALIAS, "--remote-control", ALIAS])
        self.assertEqual(tmux.sent, [["C-u"], [line, "Enter"]])

    def test_launch_without_alias_uses_the_session_name(self):
        line = steps.launch(rc_cfg(True), FakeTmux(""), "%1", row(), sleep=Sleeps())
        self.assertIn("--name rc-probe --remote-control rc-probe", line)
        line = steps.launch(rc_cfg(False), FakeTmux(""), "%1", row(), sleep=Sleeps())
        self.assertIn("--name rc-probe", line)
        self.assertNotIn("--remote-control", line)

    def test_launch_profile_and_cwd(self):
        cfg = rc_cfg(False)
        line = steps.launch(cfg, FakeTmux(""), "%1", row(), profile="work", cwd="/tmp/x y", sleep=Sleeps())
        self.assertTrue(line.startswith("cd '/tmp/x y' && "))
        self.assertIn(f"CLAUDE_CONFIG_DIR={cfg.profiles['work'].config_dir}", line)

    def test_launch_codex_sends_enter_separately(self):
        sleep = Sleeps()
        tmux = FakeTmux("")
        line = steps.launch(rc_cfg(True), tmux, "%1", row(agent="codex", profile=""), cwd="/tmp/w", sleep=sleep)
        self.assertNotIn("--name", line)
        self.assertEqual(tmux.sent, [["C-u"], [line], ["Enter"]])

    def test_launch_into_a_new_pane_does_not_clear(self):
        tmux = FakeTmux("")
        line = steps.launch(rc_cfg(False), tmux, "%1", row(ALIAS), sleep=Sleeps(), clear=False)
        self.assertEqual(tmux.sent, [[line, "Enter"]])
        self.assertEqual(line, steps.launch_cmd(rc_cfg(False), row(ALIAS)))


if __name__ == "__main__":
    unittest.main()
