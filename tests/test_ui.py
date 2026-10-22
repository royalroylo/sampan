import dataclasses
import tempfile
import unittest
from pathlib import Path

from sampan.restore import Candidate
from sampan.status import State
from sampan.ui import Checklist, cmd_ui
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env, status_row


def row(name, state=State.RUNNING, profile="alice", agent_status="idle", tokens=100_000, agent="claude"):
    return status_row(name, state, agent=agent, profile=profile, agent_status=agent_status, tokens=tokens,
                      sid="aaaaaaaa-x")


ROWS = [row("a"), row("b", agent_status="busy", tokens=300_000), row("old", state=State.RETIRED, agent_status="-")]


class UiTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.msgs, self.calls, self.execs = [], [], []
        self.r = FakeRunner()
        self.r.on(["tmux", "-u", "has-session"], cp())

    def ui(self, answers, *, current=None, rows=ROWS, restore_picker=None, adopt_picker=None):
        answers = iter(answers)
        return cmd_ui(
            self.cfg, self.r, self.msgs.append, lambda _prompt: next(answers),
            current_session=current, own_pane="%9", rows_fn=lambda cfg, run: rows,
            exec_fn=lambda *a: self.execs.append(a),
            stop_fn=lambda *a, **kw: self.calls.append(("stop", kw)) or 0,
            new_fn=lambda cfg, run, out, name, profile, cwd, **kw: self.calls.append(("new", name, profile, cwd)) or 0,
            set_state_fn=lambda cfg, out, name, state: self.calls.append(("state", name, state)) or 0,
            move_fn=lambda cfg, run, out, name, dest, **kw: self.calls.append(("move", name, dest, kw)) or 0,
            rename_fn=lambda cfg, run, out, old, new: self.calls.append(("rename", old, new)) or 0,
            restore_fn=lambda cfg, run, out, ask, **kw: self.calls.append(("restore", kw)) or 0,
            restore_picker_fn=restore_picker,
            adopt_fn=lambda cfg, run, out, ask, **kw: self.calls.append(("adopt", kw)) or 0,
            adopt_picker_fn=adopt_picker,
            save_fn=lambda cfg, run, out, **kw: self.calls.append(("save", kw)) or 0,
        )

    def text(self):
        return "\n".join(self.msgs)

    def test_list_shows_status_and_marks_current_and_large_context(self):
        self.ui(["q"], current="a")
        self.assertIn("[ 1] a ", self.text())
        self.assertIn("← 目前所在", self.text())
        self.assertIn("300k ⚠", self.text())
        self.assertIn("busy", self.text())

    def test_number_outside_tmux_attaches(self):
        self.ui(["2"])
        self.assertEqual(self.execs, [("tmux", ["tmux", "-u", "attach-session", "-t", "=b"])])

    def test_number_inside_tmux_switches_client(self):
        self.assertEqual(self.ui(["2"], current="a"), 0)
        self.assertEqual(self.r.called(["tmux", "-u", "switch-client"])[0]["args"][-2:], ["-t", "=b"])
        self.assertEqual(self.execs, [])

    def test_attach_to_session_without_tmux_suggests_start(self):
        self.r.on(["tmux", "-u", "has-session"], cp(rc=1))
        self.assertEqual(self.ui(["3"]), 1)
        self.assertIn("sampan restore old", self.text())
        self.assertEqual(self.execs, [])

    def test_enter_on_stopped_session_restores_then_attaches(self):
        self.r.on(["tmux", "-u", "has-session"], cp(rc=1))

        def restore(cfg, run, out, ask, **kw):
            self.calls.append(("restore", kw["names"], kw["confirm"]))
            self.r.on(["tmux", "-u", "has-session"], cp())
            kw["opened"].append("s")
            return 0
        rows = ROWS + [row("s", state=State.STOPPED, agent_status="-")]
        answers = iter(["4"])
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), own_pane="%9",
                    rows_fn=lambda c, r: rows, exec_fn=lambda *a: self.execs.append(a), restore_fn=restore)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [("restore", ("s",), False)])
        self.assertEqual(self.execs, [("tmux", ["tmux", "-u", "attach-session", "-t", "=s"])])

    def test_enter_on_stopped_session_restore_failed_does_not_attach(self):
        self.r.on(["tmux", "-u", "has-session"], cp(rc=1))
        rows = [row("s", state=State.STOPPED, agent_status="-")]
        answers = iter(["1"])
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), own_pane="%9",
                    rows_fn=lambda c, r: rows, exec_fn=lambda *a: self.execs.append(a),
                    restore_fn=lambda cfg, run, out, ask, **kw: 1)
        self.assertEqual(rc, 1)
        self.assertEqual(self.execs, [])

    def test_restore_key_attaches_first_opened(self):
        def restore(cfg, run, out, ask, **kw):
            kw["opened"].extend(["b", "a"])
            return 0
        answers = iter(["R"])
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), own_pane="%9",
               rows_fn=lambda c, r: ROWS, exec_fn=lambda *a: self.execs.append(a), restore_fn=restore)
        self.assertEqual(self.execs, [("tmux", ["tmux", "-u", "attach-session", "-t", "=b"])])
        self.assertIn("a 也已接回", self.text())

    def test_loop_returns_to_list_after_stop(self):
        actions = iter([("stop", ["a"]), ("quit", [])])
        answers = iter(["y", ""])
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), own_pane="%9", loop=True,
                    rows_fn=lambda c, r: ROWS, picker_fn=lambda rows, cur: next(actions),
                    stop_fn=lambda *a, **kw: self.calls.append(("stop", kw["names"])) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [("stop", ("a",))])

    def test_loop_q_after_action_leaves(self):
        answers = iter(["w", "q"])
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), own_pane="%9", loop=True,
                    rows_fn=lambda c, r: ROWS, save_fn=lambda *a, **kw: self.calls.append("save") or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, ["save"])
        self.assertEqual(self.text().count("═══ sampan sessions ═══"), 2)  # back to the list after the action

    def test_loop_reads_rows_again_after_each_action(self):
        seen = []
        actions = iter([("save", []), ("quit", [])])
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True,
               rows_fn=lambda c, r: seen.append(1) or ROWS, picker_fn=lambda rows, cur: next(actions),
               save_fn=lambda *a, **kw: 0)
        self.assertEqual(len(seen), 2)

    def test_loop_ctrl_c_during_action_returns_to_list(self):
        actions = iter([("save", []), ("quit", [])])

        def boom(*a, **kw):
            raise KeyboardInterrupt
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, rows_fn=lambda c, r: ROWS,
                    picker_fn=lambda rows, cur: next(actions), save_fn=boom)
        self.assertEqual(rc, 0)
        self.assertIn("已中斷", self.text())

    def test_loop_attach_without_tmux_stays_in_list(self):
        self.r.on(["tmux", "-u", "has-session"], cp(rc=1))
        actions = iter([("attach", ["old"]), ("quit", [])])
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, rows_fn=lambda c, r: ROWS,
                    picker_fn=lambda rows, cur: next(actions), exec_fn=lambda *a: self.execs.append(a))
        self.assertEqual(rc, 0)
        self.assertIn("sampan restore old", self.text())
        self.assertEqual(self.execs, [])

    def test_loop_attach_outside_tmux_returns_to_sampan_after_detach(self):
        attached = []
        actions = iter([("attach", ["a"]), ("attach", ["b"]), ("quit", [])])
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, rows_fn=lambda c, r: ROWS,
                    picker_fn=lambda rows, cur: next(actions), exec_fn=lambda *a: self.execs.append(a),
                    attach_fn=lambda args: attached.append(args) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(attached, [["tmux", "-u", "attach-session", "-t", "=a"],
                                    ["tmux", "-u", "attach-session", "-t", "=b"]])
        self.assertEqual(self.execs, [])  # no more exec: back to the list after detach
        self.assertIn("已從 b 回到 sampan", self.text())

    def test_loop_attach_inside_tmux_switches_and_stays_in_sampan(self):
        actions = iter([("attach", ["b"]), ("quit", [])])
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, rows_fn=lambda c, r: ROWS,
                    current_session="a", picker_fn=lambda rows, cur: next(actions))
        self.assertEqual(rc, 0)
        self.assertEqual(self.r.called(["tmux", "-u", "switch-client"])[0]["args"][-2:], ["-t", "=b"])
        self.assertIn("已切到 b", "\n".join(self.msgs))

    def test_enter_on_stopped_session_inside_tmux_switches(self):
        self.r.on(["tmux", "-u", "has-session"], cp(rc=1))

        def restore(cfg, run, out, ask, **kw):
            self.r.on(["tmux", "-u", "has-session"], cp())
            kw["opened"].append("s")
            return 0
        actions = iter([("attach", ["s"]), ("quit", [])])
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, current_session="a",
               rows_fn=lambda c, r: [row("s", state=State.STOPPED, agent_status="-")], picker_fn=lambda rows, cur: next(actions),
               restore_fn=restore)
        self.assertEqual(self.r.called(["tmux", "-u", "switch-client"])[0]["args"][-2:], ["-t", "=s"])

    def test_restore_key_nothing_opened_stays(self):
        actions = iter([("restore", []), ("quit", [])])
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, rows_fn=lambda c, r: ROWS,
               picker_fn=lambda rows, cur: next(actions), exec_fn=lambda *a: self.execs.append(a),
               restore_fn=lambda cfg, run, out, ask, **kw: out("已取消，什麼都沒做") or 0)
        self.assertEqual(self.execs, [])
        self.assertIn("已取消", self.text())

    def test_adopt_key_attaches_first_adopted(self):
        def adopt(cfg, run, out, ask, **kw):
            kw["opened"].extend(["p1", "p2"])
            return 0
        answers = iter(["A"])
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), own_pane="%9",
               rows_fn=lambda c, r: ROWS, exec_fn=lambda *a: self.execs.append(a), adopt_fn=adopt)
        self.assertEqual(self.execs, [("tmux", ["tmux", "-u", "attach-session", "-t", "=p1"])])
        self.assertIn("p2 也已接手", self.text())

    def test_new_attaches_the_new_session(self):
        self.ui(["n", "fresh", "", "", "/tmp"])
        self.assertEqual(self.execs, [("tmux", ["tmux", "-u", "attach-session", "-t", "=fresh"])])

    def test_new_failed_does_not_attach(self):
        self.r.on(["tmux", "-u", "has-session"], cp(rc=1))
        self.ui(["n", "fresh", "", "", "/tmp"])
        self.assertEqual(self.execs, [])

    def test_new_refused_does_not_attach_to_existing_session(self):
        answers = iter(["n", "taken", "", "", "/tmp"])
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), rows_fn=lambda c, r: ROWS,
               exec_fn=lambda *a: self.execs.append(a), new_fn=lambda *a, **kw: 2)
        self.assertEqual(self.execs, [])

    def test_prompt_fn_goes_to_restore_and_move_while_new_goes_straight_in(self):
        seen = []

        def fake(*a, **kw):
            seen.append(kw)
            return 0
        common = dict(rows_fn=lambda c, r: ROWS, exec_fn=lambda *a: self.execs.append(a), new_fn=fake,
                      restore_fn=fake, move_fn=fake, current_session="a", loop=False)
        for prompt_fn in (None, lambda *a: 0):
            seen.clear()
            for keys in (["n", "fresh", "", "", "/tmp"], ["R"], ["m", "1", "/tmp/x", "y"]):
                answers = iter(keys)
                cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), prompt_fn=prompt_fn, **common)
            self.assertEqual(len(seen), 3)
            # n: once opened, go straight into tmux; no waiting, no relaying prompts
            self.assertEqual(seen[0], {"wait": False})
            # R, m: prompts are relayed only when a prompt_fn is given
            self.assertTrue(all((x.get("on_prompt") is None) == (prompt_fn is None) for x in seen[1:]), seen)

    def test_enter_on_stopped_row_goes_into_live_tmux_when_restore_skips(self):
        # webshop-api:the tmux session is still there, but the pane is occupied by Python -> restore skips it; Enter must still go into that session
        rows = [dataclasses.replace(ROWS[0], state=State.STOPPED)]
        self.r.on(["tmux", "-u", "has-session"], cp())
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "1", rows_fn=lambda c, r: rows,
               exec_fn=lambda *a: self.execs.append(a), restore_fn=lambda *a, **kw: 1, loop=False)
        self.assertEqual(len(self.execs), 1)
        self.assertIn("=a", self.execs[0][1])
        self.assertTrue(any("沒辦法自動接回" in m for m in self.msgs))

    def test_enter_on_own_stopped_session_resumes_in_this_pane(self):
        # webshop-api:sampan was opened in the same pane after claude exited, and sampan occupies the only pane -> close sampan and resume in place
        from sampan.registry import Row, write_rows
        sid = "11111111-2222-3333-4444-555555555555"
        write_rows(self.cfg.registry_path, [Row("a", "claude", "alice", sid, "/tmp/proj")], "2026-10-21T13:00",
                   self.cfg.history_dir)
        rows = [dataclasses.replace(ROWS[0], state=State.STOPPED)]
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "1", rows_fn=lambda c, r: rows, current_session="a",
               exec_fn=lambda *a: self.execs.append(a), restore_fn=lambda *a, **kw: 1, loop=True)
        self.assertEqual(len(self.execs), 1)
        prog, args = self.execs[0]
        self.assertEqual(args[:2], ["/bin/sh", "-c"])
        self.assertIn("cd /tmp/proj", args[2])
        self.assertIn(f"--resume {sid}", args[2])
        self.assertTrue(any("原地接回" in m or "在這裡接回" in m for m in self.msgs))

    def test_enter_on_stopped_row_without_tmux_stays_in_list(self):
        rows = [dataclasses.replace(ROWS[0], state=State.STOPPED)]
        self.r.on(["tmux", "-u", "has-session"], cp(rc=1))
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "1", rows_fn=lambda c, r: rows,
               exec_fn=lambda *a: self.execs.append(a), restore_fn=lambda *a, **kw: 1, loop=False)
        self.assertEqual(self.execs, [])

    def test_enter_on_stopped_row_restores_without_waiting(self):
        seen = []

        def restore(cfg, run, out, ask, opened=None, **kw):
            seen.append(kw)
            opened.append(kw["names"][0])
            return 0
        rows = [dataclasses.replace(ROWS[0], state=State.STOPPED)]
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "1", rows_fn=lambda c, r: rows,
               exec_fn=lambda *a: self.execs.append(a), restore_fn=restore, prompt_fn=lambda *a: 0, loop=False)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0]["wait"], False)
        self.assertNotIn("on_prompt", seen[0])

    def test_going_in_from_a_prompt_attaches_that_session(self):
        from sampan.prompts import ATTACH

        def restore(cfg, run, out, ask, opened=None, on_prompt=None, **kw):
            opened += ["p1", "p2"]
            on_prompt("p2", "codex", None)
            return 1

        def move(cfg, run, out, name, dest, on_prompt=None, **kw):
            return 1 if on_prompt(name, "claude", None) == ATTACH else 0
        for keys, want in ((["R"], "=p2"), (["m", "1", "/tmp/x", "y"], "=a")):
            with self.subTest(keys=keys):
                self.execs = []
                answers = iter(keys)
                cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), rows_fn=lambda c, r: ROWS,
                       exec_fn=lambda *a: self.execs.append(a), restore_fn=restore, move_fn=move,
                       prompt_fn=lambda *a: ATTACH)
                self.assertEqual(self.execs, [("tmux", ["tmux", "-u", "attach-session", "-t", want])])

    def test_plain_list_shows_stopped_wording(self):
        self.ui(["q"], rows=[row("s", state=State.STOPPED, agent_status="-")])
        self.assertIn("已停止", self.text())
        self.assertNotIn("該跑沒跑", self.text())

    def test_stop_multiple_after_confirm(self):
        self.ui(["s", "1 2", "y"])
        self.assertEqual(self.calls, [("stop", {"names": ("a", "b"), "profile": None, "force": False,
                                                "dry_run": False, "timeout": 90, "own_pane": "%9"})])

    def test_stop_cancelled_without_yes(self):
        self.ui(["s", "1", ""])
        self.assertEqual(self.calls, [])
        self.assertIn("已取消", self.text())

    def test_invalid_number_does_nothing(self):
        self.assertEqual(self.ui(["s", "1 9"]), 1)
        self.assertEqual(self.calls, [])

    def test_retire_and_activate(self):
        self.ui(["r", "1"])
        self.ui(["a", "3"])
        self.assertEqual(self.calls, [("state", "a", "retired"), ("state", "old", "active")])

    def test_unknown_action_raises_instead_of_being_written_as_a_state(self):
        # a Picker key without an act branch used to fall through to set_state(<action>)
        with self.assertRaises(ValueError):
            cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", current_session=None,
                   rows_fn=lambda cfg, run: ROWS, picker_fn=lambda rows, cur: ("bogus", ["a"]),
                   set_state_fn=lambda *a: self.calls.append(("state", *a)) or 0)
        self.assertEqual(self.calls, [])

    def test_new_asks_name_profile_and_dir(self):
        self.ui(["n", "fresh", "1", "2", "~/x"])
        self.assertEqual(self.calls, [("new", "fresh", "claude:work", "~/x")])

    def test_new_profile_defaults_to_default_profile(self):
        self.ui(["n", "fresh", "", "", "/tmp"])
        self.assertEqual(self.calls, [("new", "fresh", "claude:alice", "/tmp")])

    def test_new_requires_name_and_dir(self):
        self.ui(["n", ""])
        self.ui(["n", "fresh", "", "", ""])
        self.assertEqual(self.calls, [])

    def test_new_asks_agent_then_profile(self):
        self.ui(["n", "fresh", "2", "~/x"])  # 2 = Codex: no profile question
        self.assertEqual(self.calls, [("new", "fresh", "codex", "~/x")])
        self.assertIn("Codex", self.text())

    def test_new_does_not_ask_agent_when_codex_disabled(self):
        self.cfg = dataclasses.replace(self.cfg, codex=dataclasses.replace(self.cfg.codex, enabled=False))
        self.ui(["n", "fresh", "", "/tmp"])
        self.assertEqual(self.calls, [("new", "fresh", "claude:alice", "/tmp")])

    def test_agent_column_rendered(self):
        self.ui(["q"], rows=ROWS + [row("cx", profile="-", agent_status="working", agent="codex")])
        lines = {m.split("] ", 1)[1].split()[0]: m for m in self.msgs if m.startswith("  [")}
        self.assertRegex(lines["a"], r"a +claude +alice ")
        self.assertRegex(lines["cx"], r"cx +codex +- ")

    def test_plain_restore_passes_picker(self):
        picker = lambda cands: ["x"]  # noqa: E731
        self.ui(["R"], restore_picker=picker)
        self.assertEqual([c[0] for c in self.calls], ["restore"])
        self.assertIs(self.calls[0][1]["picker_fn"], picker)

    def test_plain_w_saves_now(self):
        self.assertEqual(self.ui(["w"]), 0)
        self.assertEqual(self.calls, [("save", {})])
        self.assertIn("w 立即存檔", self.text())

    def test_plain_adopt_passes_picker(self):
        picker = lambda cands: ["1"]  # noqa: E731
        self.ui(["A"], adopt_picker=picker)
        self.assertEqual([c[0] for c in self.calls], ["adopt"])
        self.assertIs(self.calls[0][1]["picker_fn"], picker)
        self.assertIn("A 接手 tmux 外的", self.text())

    def test_ctrl_c_and_eof_at_prompts_cancel(self):
        for exc in (KeyboardInterrupt, EOFError):
            def boom(_p, exc=exc):
                raise exc
            self.assertEqual(cmd_ui(self.cfg, self.r, self.msgs.append, boom, rows_fn=lambda c, r: ROWS), 0)
            answers = iter(["n", "fresh"])

            def later(_p, answers=answers, exc=exc):
                try:
                    return next(answers)
                except StopIteration:
                    raise exc
            self.assertEqual(cmd_ui(self.cfg, self.r, self.msgs.append, later, rows_fn=lambda c, r: ROWS,
                                    new_fn=lambda *a: self.calls.append(a) or 0), 0)
        self.assertEqual(self.calls, [])

    def test_plain_rename(self):
        self.ui(["e", "2", "fresh"])
        self.assertEqual(self.calls, [("rename", "b", "fresh")])

    def test_move_asks_dir_shows_creation_and_confirms(self):
        dest = Path(tempfile.mkdtemp()).resolve() / "new-proj"
        self.ui(["m", "1", str(dest), "y"])
        self.assertIn(f"a → {dest}（目錄不存在，將建立）", self.text())
        self.assertEqual(self.calls, [("move", "a", str(dest), {"own_pane": "%9"})])

    def test_move_confirmation_names_the_agent(self):
        dest = Path(tempfile.mkdtemp()).resolve()
        self.ui(["m", "1", str(dest), "n"], rows=[row("cx", agent="codex", profile="-")])
        self.assertIn("會結束這個 session 的 codex", self.text())
        self.ui(["m", "1", str(dest), "n"])
        self.assertIn("會結束這個 session 的 claude", self.text())

    def test_move_expands_home_and_can_be_cancelled(self):
        self.ui(["m", "2", "~/somewhere", ""])
        self.assertIn(str(Path("~/somewhere").expanduser()), self.text())
        self.assertIn("已取消", self.text())
        self.ui(["m", "2", ""])
        self.assertEqual(self.calls, [])

    def test_quit_and_empty_input_exit_quietly(self):
        self.assertEqual(self.ui(["q"]), 0)
        self.assertEqual(self.ui([""]), 0)
        self.assertEqual(self.calls + self.execs, [])

    def test_no_sessions_still_offers_new(self):
        self.ui(["n", "fresh", "", "", "/tmp"], rows=[])
        self.assertIn("目前沒有任何 session", self.text())
        self.assertEqual(self.calls, [("new", "fresh", "claude:alice", "/tmp")])

    def test_end_of_input_exits(self):
        def eof(_p):
            raise EOFError
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, eof, rows_fn=lambda c, r: ROWS)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()


from sampan.ui import Picker, pad  # noqa: E402


class PadTest(unittest.TestCase):
    def test_pads_by_display_width(self):
        self.assertEqual(pad("003-帳務", 18), "003-帳務" + " " * 10)  # 4 + 4 = 8 cells
        self.assertEqual(pad("abc", 5), "abc  ")

    def test_row_text_shows_unknown_in_full(self):
        from sampan.ui import row_text
        r = row("a", profile="carol", agent_status="unknown", tokens=None)
        self.assertIn(" unknown ", row_text(r))

    def test_truncates_wide_text_without_splitting_a_character(self):
        self.assertEqual(pad("帳務帳務", 5), "帳務 ")


class PickerTest(unittest.TestCase):
    def setUp(self):
        self.p = Picker(ROWS, current="b")

    def test_cursor_starts_on_current_session(self):
        self.assertEqual(self.p.cursor, 1)

    def test_moves_and_clamps(self):
        for k in ("KEY_DOWN", "KEY_DOWN", "j"):
            self.p.key(k)
        self.assertEqual(self.p.cursor, 2)
        for k in ("KEY_UP", "k", "k", "k"):
            self.p.key(k)
        self.assertEqual(self.p.cursor, 0)

    def test_e_renames_cursor_row(self):
        self.assertEqual(self.p.key("e"), ("rename", ["b"]))

    def test_enter_attaches_cursor_row(self):
        self.assertEqual(self.p.key("\n"), ("attach", ["b"]))

    def test_stop_uses_marked_rows_or_cursor_and_confirms_in_place(self):
        self.assertIsNone(self.p.key("s"))
        self.assertEqual(self.p.prompt, "將停掉：b　確定？(y/N)")
        self.assertEqual(self.p.key("y"), ("stop!", ["b"]))
        self.assertIsNone(self.p.prompt)
        self.p.key(" ")            # mark b, cursor moves down
        self.p.key(" ")            # mark old
        self.p.key("s")
        self.assertEqual(self.p.key("Y"), ("stop!", ["b", "old"]))

    def test_stop_any_other_key_cancels(self):
        self.p.key("s")
        self.assertIsNone(self.p.key("n"))
        self.assertIsNone(self.p.prompt)
        self.assertIsNone(self.p.key("y"))  # cancelled; y no longer means confirm

    def test_space_toggles_mark(self):
        self.p.key(" ")
        self.p.key("KEY_UP")
        self.p.key(" ")
        self.assertEqual(self.p.marked, set())

    def test_other_actions(self):
        self.assertEqual(self.p.key("r"), ("retired", ["b"]))
        self.assertEqual(self.p.key("a"), ("active", ["b"]))
        self.assertEqual(self.p.key("n"), ("new", []))
        self.assertEqual(self.p.key("m"), ("move", ["b"]))
        self.assertEqual(self.p.key("q"), ("quit", []))
        self.assertEqual(self.p.key("\x1b"), ("quit", []))
        self.assertIsNone(self.p.key("x"))

    def test_w_is_save_even_with_empty_list(self):
        self.assertEqual(self.p.key("w"), ("save", []))
        self.assertEqual(Picker([]).key("w"), ("save", []))

    def test_capital_r_is_restore(self):
        self.assertEqual(self.p.key("R"), ("restore", []))
        self.assertEqual(Picker([]).key("R"), ("restore", []))

    def test_capital_a_is_adopt_lowercase_a_still_activates(self):
        self.assertEqual(self.p.key("A"), ("adopt", []))
        self.assertEqual(Picker([]).key("A"), ("adopt", []))
        self.assertEqual(self.p.key("a")[0], "active")

    def test_empty_list_only_allows_new_and_quit(self):
        p = Picker([])
        self.assertIsNone(p.key("\n"))
        self.assertIsNone(p.key("s"))
        self.assertEqual(p.key("n"), ("new", []))


class InteractiveUiTest(UiTest):
    def ui_keys(self, action, answers=(), current=None):
        answers = iter(answers)
        return cmd_ui(
            self.cfg, self.r, self.msgs.append, lambda _p: next(answers),
            current_session=current, own_pane="%9", rows_fn=lambda c, r: ROWS,
            picker_fn=lambda rows, cur: action,
            exec_fn=lambda *a: self.execs.append(a),
            stop_fn=lambda *a, **kw: self.calls.append(("stop", kw["names"])) or 0,
            new_fn=lambda cfg, run, out, name, profile, cwd, **kw: self.calls.append(("new", name, profile, cwd)) or 0,
            set_state_fn=lambda cfg, out, name, state: self.calls.append(("state", name, state)) or 0,
            move_fn=lambda cfg, run, out, name, dest, **kw: self.calls.append(("move", name, dest)) or 0,
            rename_fn=lambda cfg, run, out, old, new: self.calls.append(("rename", old, new)) or 0,
            restore_fn=lambda cfg, run, out, ask, **kw: self.calls.append(("restore",)) or 0,
            adopt_fn=lambda cfg, run, out, ask, **kw: self.calls.append(("adopt",)) or 0,
            save_fn=lambda cfg, run, out, **kw: self.calls.append(("save",)) or 0,
        )

    def test_attach(self):
        self.ui_keys(("attach", ["a"]))
        self.assertEqual(self.execs, [("tmux", ["tmux", "-u", "attach-session", "-t", "=a"])])

    def test_stop_confirms_first(self):
        self.ui_keys(("stop", ["a", "b"]), ["y"])
        self.assertEqual(self.calls, [("stop", ("a", "b"))])

    def test_confirmed_stop_is_not_asked_again(self):
        self.ui_keys(("stop!", ["a"]))
        self.assertEqual(self.calls, [("stop", ("a",))])

    def test_stop_cancel(self):
        self.ui_keys(("stop", ["a"]), [""])
        self.assertEqual(self.calls, [])

    def test_move_confirms_first(self):
        self.ui_keys(("move", ["a"]), ["/tmp", "y"])
        self.assertEqual(self.calls, [("move", "a", str(Path("/tmp").resolve()))])

    def test_state_and_new_and_quit(self):
        self.ui_keys(("retired", ["a"]))
        self.ui_keys(("new", []), ["fresh", "", "", "/tmp"])
        self.assertEqual(self.ui_keys(("quit", [])), 0)
        self.assertEqual(self.calls, [("state", "a", "retired"), ("new", "fresh", "claude:alice", "/tmp")])

    def test_rename_asks_new_name(self):
        self.ui_keys(("rename", ["a"]), ["fresh"])
        self.assertEqual(self.calls, [("rename", "a", "fresh")])

    def test_rename_empty_answer_cancels(self):
        self.ui_keys(("rename", ["a"]), [""])
        self.assertEqual(self.calls, [])

    def test_adopt_key_runs_adopt(self):
        self.ui_keys(("adopt", []))
        self.assertEqual(self.calls, [("adopt",)])

    def test_save_key_runs_save(self):
        self.ui_keys(("save", []))
        self.assertEqual(self.calls, [("save",)])

    def test_restore_key_runs_restore(self):
        self.ui_keys(("restore", []))
        self.assertEqual(self.calls, [("restore",)])


class ChecklistTest(unittest.TestCase):
    def cands(self):
        from sampan.registry import Row
        mk = lambda n, sel, skip=None: Candidate(Row(n, "claude", "alice", "i", "/c"), None, None, sel, skip)  # noqa: E731
        return [mk("a", True), mk("b", False), mk("c", False, "找不到逐字稿")]

    def test_defaults_toggle_confirm_and_cancel(self):
        c = Checklist(self.cands())
        self.assertEqual(c.marked, {"a"})
        c.key(" ")                      # deselect a, cursor moves down to b
        c.key(" ")                      # tick b
        self.assertEqual(c.key("\n"), ("done", ["b"]))
        self.assertEqual(Checklist(self.cands()).key("q"), ("cancel", None))

    def test_custom_ident_for_adopt_rows(self):
        from types import SimpleNamespace as NS
        cands = [NS(key="600", selected=True, skip=None), NS(key="700", selected=False, skip="判斷不出對話 id")]
        c = Checklist(cands, lambda x: x.key)
        self.assertEqual(c.key("\n"), ("done", ["600"]))

    def test_cannot_mark_skipped_rows(self):
        c = Checklist(self.cands())
        c.key("KEY_DOWN"); c.key("KEY_DOWN")  # noqa: E702
        c.key(" ")
        self.assertNotIn("c", c.marked)
