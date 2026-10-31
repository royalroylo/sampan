import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan.restore import Candidate
from sampan.status import State
from sampan.ui import Checklist, cmd_ui
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env, register, status_row


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
            switch_fn=lambda cfg, run, out, name, prof, **kw: self.calls.append(("switch", name, prof, kw)) or 0,
            rename_fn=lambda cfg, run, out, old, new: self.calls.append(("rename", old, new)) or 0,
            alias_fn=lambda cfg, out, name, alias, **kw: self.calls.append(("alias", name, alias, kw)) or 0,
            forget_fn=lambda cfg, run, out, name: self.calls.append(("forget", name)) or 0,
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

    def test_loop_action_error_is_shown_and_the_list_stays(self):
        actions = iter([("save", []), ("bogus", ["a"]), ("quit", [])])

        def boom(*a, **kw):
            raise RuntimeError("壞掉了")
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, rows_fn=lambda c, r: ROWS,
                    picker_fn=lambda rows, cur: next(actions), save_fn=boom)
        self.assertEqual(rc, 0)
        self.assertIn("❌ save 失敗：壞掉了", self.text())
        self.assertIn("❌ bogus 失敗：未知的 ui action：'bogus'", self.text())

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

    def plain_choice(self, choice, rows=ROWS, then=None):
        from sampan.ui.actions import HELP, _plain_action
        answers = iter([choice] + ([then] if then is not None else []))
        result = _plain_action(lambda _p: next(answers), self.msgs.append, rows, None)
        self.after_menu = self.msgs[self.msgs.index(HELP) + 1:]  # what was printed after the menu itself
        return result

    def test_plain_slash_one_match_attaches(self):
        self.assertEqual(self.plain_choice("/OLD"), ("attach", ["old"]))

    def test_plain_slash_name_with_digits(self):
        rows = [row("proj-001"), row("proj-002"), row("proj-003")]
        self.assertEqual(self.plain_choice("/003", rows), ("attach", ["proj-003"]))

    def test_plain_slash_several_matches_listed_with_original_numbers(self):
        rows = [row("web-a"), row("db"), row("web-b")]
        self.assertEqual(self.plain_choice("/web", rows, then=""), ("invalid", []))
        text = "\n".join(self.after_menu)
        self.assertIn("[ 1] web-a", text)
        self.assertIn("[ 3] web-b", text)
        self.assertNotIn("db", text)

    def test_plain_slash_several_matches_then_number_attaches(self):
        rows = [row("web-a"), row("db"), row("web-b")]
        self.assertEqual(self.plain_choice("/web", rows, then="3"), ("attach", ["web-b"]))

    def test_plain_slash_several_matches_then_empty_or_q_returns_invalid(self):
        rows = [row("web-a"), row("db"), row("web-b")]
        for then in ("", "q"):
            with self.subTest(then=then):
                self.assertEqual(self.plain_choice("/web", rows, then=then), ("invalid", []))
                self.msgs.clear()

    def test_plain_slash_several_matches_then_bad_number_is_invalid(self):
        rows = [row("web-a"), row("db"), row("web-b")]
        self.assertEqual(self.plain_choice("/web", rows, then="9"), ("invalid", []))
        self.assertIn("無效編號：9", self.after_menu)

    def test_plain_slash_no_match_or_empty_filter(self):
        for choice, query in (("/zzz", "zzz"), ("/", "")):
            with self.subTest(choice=choice):
                self.assertEqual(self.plain_choice(choice), ("invalid", []))
                self.assertEqual(self.after_menu, [f"沒有名稱含「{query}」的 session"])
                self.msgs.clear()

    def test_plain_help_mentions_slash_search(self):
        self.ui(["q"])
        self.assertIn("/文字 搜尋", self.text())

    def test_plain_rename(self):
        self.ui(["e", "2", "fresh"])
        self.assertEqual(self.calls, [("rename", "b", "fresh")])

    def test_move_asks_dir_shows_creation_and_confirms(self):
        dest = Path(tempfile.mkdtemp()).resolve() / "new-proj"
        self.ui(["m", "1", str(dest), "y"])
        self.assertIn(f"a → {dest}（目錄不存在，將建立）", self.text())
        self.assertEqual(self.calls, [("move", "a", str(dest), {"own_pane": "%9"})])

    def test_plain_switch_picks_profile_confirms_and_warns_gap(self):
        self.ui(["P", "1", "2", "y"])
        text = self.text()
        self.assertIn("[1] alice（alice@example.com）  ← 目前", text)
        self.assertIn("將換 profile：a　alice → work（work@example.com）", text)
        self.assertIn("⚠ work 沒有裝：superpowers@claude-plugins-official", text)
        self.assertIn("會結束這個 session 的 claude", text)
        self.assertEqual(self.calls, [("switch", "a", "work", {"own_pane": "%9", "warn_gap": False})])

    def test_plain_switch_cancel_same_and_codex(self):
        self.ui(["P", "1", "2", "n"])
        self.ui(["P", "1", "1"])
        self.assertIn("a 已經是 alice", self.text())
        self.ui(["P", "1", ""])
        self.ui(["P", "1"], rows=[row("cx", agent="codex", profile="-")])
        self.assertIn("只有 Claude session 能換 profile", self.text())
        self.assertEqual(self.calls, [])

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
        self.assertEqual(self.p.key("n"), ("new", [""]))
        self.assertEqual(self.p.key("m"), ("move", ["b"]))
        self.assertEqual(self.p.key("P"), ("switch", ["b"]))
        self.assertEqual(self.p.key("q"), ("quit", []))
        self.assertIsNone(self.p.key("\x1b"))  # a lone Esc does not quit the list: only q does
        self.assertIsNone(self.p.key("x"))

    def test_w_is_save_even_with_empty_list(self):
        self.assertEqual(self.p.key("w"), ("save", []))
        self.assertEqual(Picker(ROWS).key("X"), ("forget", ["a"]))
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
        self.assertEqual(p.key("n"), ("new", [""]))


class Clock:
    """Fake monotonic clock: advance() moves time forward."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def many(n):
    return [row(f"s{i:02d}") for i in range(1, n + 1)]


class PickerJumpSearchTest(unittest.TestCase):
    def press(self, p, *keys):
        out = None
        for k in keys:
            out = p.key(k)
        return out

    def test_slash_search_enter_attaches(self):
        p = Picker([row("001-a"), row("003-foo"), row("010-bar")])
        self.assertEqual(self.press(p, "/", "0", "0", "3", "\n"), ("attach", ["003-foo"]))
        self.assertIsNone(p.query)

    def test_digit_jump_two_digits(self):
        rows = many(32)
        p = Picker(rows, clock=Clock())
        self.press(p, "1", "2")
        self.assertEqual(p.cursor, 11)
        self.assertEqual(p.jump, "")
        self.assertEqual(p.key("\n"), ("attach", [rows[11].name]))

    def test_digit_jump_shows_prompt_and_enter_after_one_digit_attaches(self):
        rows = many(32)
        p = Picker(rows, clock=Clock())
        p.key("1")
        self.assertEqual(p.prompt, "跳到第 1 列")
        self.assertEqual(p.key("\n"), ("attach", [rows[0].name]))
        self.assertEqual(p.jump, "")
        self.assertIsNone(p.prompt)

    def test_digit_jump_single_when_no_longer_possible(self):
        p = Picker(many(32), clock=Clock())
        p.key("4")
        self.assertEqual(p.cursor, 3)
        self.assertEqual(p.jump, "")

    def test_digit_jump_timeout(self):
        clock = Clock()
        p = Picker(many(32), clock=clock)
        p.key("1")
        self.assertEqual(p.cursor, 0)
        clock.advance(1.5)
        self.assertIsNone(p.prompt)  # the buffer has expired
        p.key("5")
        self.assertEqual(p.cursor, 4)

    def test_digit_jump_out_of_range_restarts_and_zero_clears(self):
        p = Picker(many(32), clock=Clock())
        self.press(p, "3", "5")  # 35 > 32: restart with 5
        self.assertEqual(p.cursor, 4)
        self.assertEqual(p.jump, "")
        p.key("0")
        self.assertEqual(p.jump, "")
        self.assertEqual(p.cursor, 4)

    def test_nine_rows_every_digit_jumps(self):
        p = Picker(many(9), clock=Clock())
        self.press(p, "1", "2")
        self.assertEqual(p.cursor, 1)
        self.assertEqual(p.jump, "")

    def test_search_no_match_enter_is_noop(self):
        p = Picker(ROWS)
        self.assertIsNone(self.press(p, "/", "z", "z", "z", "\n"))
        self.assertEqual(p.visible, [])
        self.assertEqual(p.query, "zzz")

    def test_search_keys_are_text(self):
        p = Picker(ROWS)
        self.assertIsNone(self.press(p, "/", "q", "j"))
        self.assertEqual(p.query, "qj")
        self.assertEqual(p.cursor, 0)
        self.assertIsNone(self.press(p, "s", " ", "n"))  # no stop prompt, no mark, no new
        self.assertEqual(p.marked, set())
        self.assertEqual(p.prompt, "/qjs n▏（0 筆，Enter 進入，Esc 取消）")

    def test_search_filters_case_insensitively_and_prompt_counts(self):
        p = Picker([row("Alpha"), row("beta"), row("ALPS")])
        self.press(p, "/", "a", "l", "P")
        self.assertEqual([r.name for r in p.visible], ["Alpha", "ALPS"])
        self.assertEqual(p.prompt, "/alP▏（2 筆，Enter 進入，Esc 取消）")
        p.key("KEY_DOWN")
        self.assertEqual(p.key("\n"), ("attach", ["ALPS"]))

    def test_search_esc_keeps_cursor_session(self):
        p = Picker([row("a"), row("foo-1"), row("b"), row("foo-2")])
        self.press(p, "/", "f", "o", "o", "KEY_DOWN")
        self.assertEqual(p.visible[p.cursor].name, "foo-2")
        self.assertIsNone(p.key("\x1b"))  # Esc leaves search, does not quit
        self.assertIsNone(p.query)
        self.assertEqual(len(p.visible), 4)
        self.assertEqual(p.visible[p.cursor].name, "foo-2")
        self.assertIsNone(p.prompt)
        self.assertIsNone(p.key("\x1b"))  # back in normal mode: a lone Esc does nothing there
        self.assertEqual(p.key("q"), ("quit", []))

    def test_search_backspace_and_leave_on_empty(self):
        p = Picker([row("a"), row("b")])
        self.press(p, "/", "b")
        self.assertEqual(p.query, "b")
        p.key("KEY_BACKSPACE")
        self.assertEqual(p.query, "")
        self.assertEqual(len(p.visible), 2)
        p.key("\x7f")
        self.assertIsNone(p.query)
        self.assertEqual(p.cursor, 0)

    def test_search_ignores_other_keys(self):
        p = Picker([row("a"), row("b")])
        self.press(p, "/", "KEY_LEFT", "\t")
        self.assertEqual(p.query, "")

    def test_reload_keeps_query_and_cursor(self):
        clock = Clock()
        p = Picker([row("a"), row("foo-1"), row("foo-2")], clock=clock)
        self.press(p, "/", "f", "o", "o", "KEY_DOWN")
        p.reload([row("0new-foo"), row("a"), row("foo-1"), row("foo-2")])
        self.assertEqual(p.query, "foo")
        self.assertEqual([r.name for r in p.visible], ["0new-foo", "foo-1", "foo-2"])
        self.assertEqual(p.visible[p.cursor].name, "foo-2")
        p.reload([row("a"), row("foo-1")])  # foo-2 is gone: cursor goes to the first row
        self.assertEqual(p.cursor, 0)
        self.assertEqual(p.visible[p.cursor].name, "foo-1")

    def test_reload_keeps_jump_buffer(self):
        clock = Clock()
        rows = many(32)
        p = Picker(rows, clock=clock)
        p.key("1")
        p.reload(many(32))
        self.assertEqual(p.jump, "1")
        p.key("2")
        self.assertEqual(p.cursor, 11)

    def test_stop_prompt_has_priority(self):
        clock = Clock()
        rows = many(32)
        p = Picker(rows, clock=clock)
        p.key("1")
        self.assertEqual(p.prompt, "跳到第 1 列")
        p.key("s")
        self.assertEqual(p.prompt, "將停掉：s01　確定？(y/N)")
        # s already clears the digit buffer: set both states directly to prove the property's order
        p.jump, p._jump_at, p._stop = "1", clock(), ["s01"]
        self.assertEqual(p.prompt, "將停掉：s01　確定？(y/N)")
        p.query = ""
        self.assertEqual(p.prompt, "將停掉：s01　確定？(y/N)")
        p._stop = None
        self.assertEqual(p.prompt, "/▏（32 筆，Enter 進入，Esc 取消）")
        p.query = None
        self.assertEqual(p.prompt, "跳到第 1 列")

    def test_expired_jump_is_cleared_on_prompt_read_and_on_any_key(self):
        clock = Clock()
        p = Picker(many(32), clock=clock)
        p.key("1")
        clock.advance(1.5)
        self.assertIsNone(p.prompt)
        self.assertEqual(p.jump, "")
        p.key("1")
        clock.advance(1.5)
        p.key("KEY_LEFT")  # an ignored key still drops the stale buffer
        self.assertEqual(p.jump, "")
        p.key("1")
        clock.advance(1.5)
        p.key("/")
        self.assertEqual(p.jump, "")

    def test_reload_outside_search_keeps_index_when_session_vanishes(self):
        p = Picker([row("a"), row("b"), row("c"), row("d")], current="c")
        self.assertEqual(p.cursor, 2)
        p.reload([row("a"), row("b"), row("d")])
        self.assertEqual(p.cursor, 2)
        self.assertEqual(p.visible[p.cursor].name, "d")
        p.reload([row("a")])
        self.assertEqual(p.cursor, 0)  # clamped


from sampan.folders import Tree  # noqa: E402
from sampan.textwidth import text_width  # noqa: E402
from sampan.ui.tree import FolderItem  # noqa: E402


def folder_rows():
    return [row("a1"), row("b1"), row("top"), row("x", state=State.STOPPED, agent_status="-")]


def folder_tree(**collapsed):
    folders = {"客戶": False, "客戶/甲": False}
    folders.update({k.replace("_", "/"): v for k, v in collapsed.items()})
    return Tree(folders, {"a1": "客戶", "b1": "客戶/甲", "x": "客戶/甲"})


def here(p):
    it = p.visible[p.cursor]
    return ("folder", it.path) if isinstance(it, FolderItem) else ("session", it.name)


class PickerFolderTest(unittest.TestCase):
    """Visible order: 客戶, 客戶/甲, b1, x, a1, top."""

    def picker(self, current=None, tree=None, rows=None, others=()):
        return Picker(rows or folder_rows(), current, clock=Clock(), tree=tree or folder_tree(), others=others)

    def press(self, p, *keys):
        out = None
        for k in keys:
            out = p.key(k)
        return out

    def test_layout_and_cursor(self):
        p = self.picker(current="a1")
        self.assertEqual(here(p), ("session", "a1"))
        self.assertEqual(p.n_rows, 6)
        self.assertTrue(isinstance(p.visible[0], FolderItem))
        self.assertTrue(p.on_folder() is False and p.here_folder() is None)
        p.key("k")
        p.key("k")
        p.key("k")
        self.assertEqual(here(p), ("folder", "客戶/甲"))
        self.assertTrue(p.on_folder())
        self.assertEqual(p.here_folder(), "客戶/甲")

    def test_enter_on_folder_collapses(self):
        p = self.picker()
        self.assertEqual(p.key("\n"), ("collapse", ["客戶", "1"]))
        self.assertEqual([here(p)], [("folder", "客戶")])
        self.assertEqual([getattr(i, "path", i.name) for i in p.visible], ["客戶", "top"])
        self.assertTrue(p.tree.folders["客戶"])
        self.assertEqual(p.key("KEY_ENTER"), ("collapse", ["客戶", "0"]))
        self.assertEqual(p.n_rows, 6)

    def test_arrows_on_folder_and_session(self):
        p = self.picker(current="b1")
        self.assertIsNone(p.key("KEY_RIGHT"))  # nothing on a session row
        self.assertIsNone(p.key("KEY_LEFT"))   # jumps to its folder row
        self.assertEqual(here(p), ("folder", "客戶/甲"))
        self.assertEqual(p.key("KEY_LEFT"), ("collapse", ["客戶/甲", "1"]))
        self.assertIsNone(p.key("KEY_LEFT"))  # already collapsed: go to the parent folder row
        self.assertEqual(here(p), ("folder", "客戶"))
        p.key("j")
        self.assertEqual(p.key("KEY_RIGHT"), ("collapse", ["客戶/甲", "0"]))
        self.assertIsNone(p.key("KEY_RIGHT"))  # already open
        p = self.picker(current="top")
        self.assertIsNone(p.key("KEY_LEFT"))  # top level: no folder row to go to
        self.assertEqual(here(p), ("session", "top"))

    def test_digits_count_sessions_only(self):
        p = self.picker()
        p.key("1")
        self.assertEqual(here(p), ("session", "b1"))
        p.key("4")
        self.assertEqual(here(p), ("session", "top"))
        p = self.picker()
        p.key("\n")  # collapse 客戶: the first session row is now top
        p.key("1")
        self.assertEqual(here(p), ("session", "top"))

    def test_space_marks_folder(self):
        p = self.picker()
        p.key(" ")
        self.assertEqual(p.marked_folders, {"客戶"})
        self.assertEqual(p.marked, set())
        self.assertEqual(p.cursor, 1)
        p.key("k")
        p.key(" ")
        self.assertEqual(p.marked_folders, set())

    def test_ui_p_on_folder_row_goes_batch(self):
        p = self.picker()  # cursor on the folder 客戶
        self.assertEqual(p.key("P"), ("switch_many", ["a1", "b1", "x"]))
        p = self.picker(current="a1")
        self.assertEqual(p.key("P"), ("switch", ["a1"]))  # one session: the single flow
        self.press(p, " ", "j", " ")  # a1 and the next session marked
        names = p.key("P")
        self.assertEqual(names[0], "switch_many")
        self.assertEqual(len(names[1]), 2)
        p = self.picker(current="a1")
        p.key(" ")  # one session marked: still the single flow, on the cursor row
        self.assertEqual(p.key("P")[0], "switch")

    def test_g_returns_marked_items(self):
        p = self.picker(current="top")
        p.key(" ")  # top
        self.press(p, "k", "k", "k", "k", " ")  # 客戶/甲
        self.assertEqual(p.key("g"), ("group", ["folder:客戶/甲", "session:top"]))
        self.assertEqual((p.marked, p.marked_folders), ({"top"}, {"客戶/甲"}))  # cleared by the caller
        p.clear_marks()
        self.assertEqual((p.marked, p.marked_folders), (set(), set()))

    def test_f_without_marks_uses_cursor_item(self):
        p = self.picker(current="a1")
        self.assertEqual(p.key("f"), ("file", ["session:a1"]))
        p.key("1")
        self.press(p, "k", "k")
        self.assertEqual(p.key("f"), ("file", ["folder:客戶"]))
        self.assertEqual(p.key("g"), ("group", ["folder:客戶"]))

    def test_folder_row_e_x_and_inert_keys(self):
        p = self.picker()
        self.assertEqual(p.key("e"), ("rename_folder", ["客戶"]))
        self.assertEqual(p.key("X"), ("rm_folder", ["客戶"]))
        for k in "mra":
            self.assertIsNone(p.key(k))
        p = self.picker(current="a1")
        self.assertEqual(p.key("e"), ("rename", ["a1"]))
        self.assertEqual(p.key("X"), ("forget", ["a1"]))

    def test_stop_on_collapsed_folder_lists_hidden_names(self):
        p = self.picker()
        p.key("\n")  # collapse 客戶
        self.assertIsNone(p.key("s"))
        self.assertEqual(p.prompt, "將停掉：a1、b1、x　確定？(y/N)")
        self.assertEqual(p.key("y"), ("stop!", ["a1", "b1", "x"]))

    def test_stop_mixed_marks_deduplicates(self):
        p = self.picker()
        self.press(p, "j", " ", " ")  # 客戶/甲 and b1
        p.key("s")
        self.assertEqual(p.prompt, "將停掉：b1、x　確定？(y/N)")

    def test_restart_on_folder_skips_stopped(self):
        p = self.picker()
        p.key("j")
        self.assertIsNone(p.key("S"))
        self.assertEqual(p.prompt, "將重開：b1　略過：x（已停止）　確定？(y/N)")
        self.assertEqual(p.key("y"), ("restart!", ["b1"]))
        self.assertEqual(p.restart_plan.skip, [("x", "已停止")])
        self.assertIsNone(p.prompt)
        p.key("S")
        self.assertIsNone(p.key("n"))  # any other key cancels
        self.assertIsNone(p.prompt)

    def test_restart_on_stopped_session_row_resumes(self):
        p = self.picker(current="x")
        p.key("S")
        self.assertEqual(p.prompt, "將重開：x（背景接回）　確定？(y/N)")
        self.assertEqual(p.key("Y"), ("restart!", ["x"]))
        p = self.picker(current="b1")
        self.press(p, " ", " ", "S")  # marks b1, x: only sessions
        self.assertEqual(p.key("y"), ("restart!", ["b1", "x"]))
        p = self.picker(current="x")
        self.press(p, " ", "1", "k", " ", "S")  # x marked itself and the folder 客戶/甲: x was asked for by name
        self.assertEqual(p.key("y"), ("restart!", ["b1", "x"]))

    def test_restart_folder_plus_session_keeps_folder_only_stopped_stopped(self):
        p = self.picker(current="top")
        self.press(p, " ", "1", "k", " ", "S")  # top and the folder 客戶/甲 (b1, stopped x)
        self.assertEqual(p.key("y"), ("restart!", ["b1", "top"]))
        self.assertEqual(p.restart_plan.skip, [("x", "已停止")])

    def test_stop_confirm_fits_width_and_keeps_all_names(self):
        names = [f"{i:02d}" + "n" * 18 for i in range(10)]  # 20 cells each
        rows = [row(n) for n in names]
        p = Picker(rows, None, clock=Clock(), tree=Tree({"甲": False}, {n: "甲" for n in names}))
        p.width = 60
        p.key("s")  # on the folder row
        self.assertTrue(p.prompt.endswith("確定？(y/N)"), p.prompt)
        self.assertIn("等 10 個", p.prompt)
        self.assertLessEqual(text_width(p.prompt), 60)
        self.assertEqual(p.key("y"), ("stop!", names))
        p.key("S")
        self.assertTrue(p.prompt.endswith("確定？(y/N)"), p.prompt)
        self.assertIn("等 10 個", p.prompt)
        self.assertLessEqual(text_width(p.prompt), 60)
        self.assertEqual(p.key("y"), ("restart!", names))

    def test_restart_nothing_to_do_shows_reason(self):
        rows = folder_rows()
        tree = Tree({"維運": False}, {"x": "維運"})
        p = self.picker(rows=rows, tree=tree)
        self.assertIsNone(p.key("S"))
        self.assertEqual(p.prompt, "沒有可以重開的：x（已停止）")
        self.assertIsNone(p.key("y"))  # nothing was waiting for a y
        self.assertIsNone(p.prompt)

    def test_restart_confirm_lists_own_pane_as_skipped(self):
        rows = [dataclasses.replace(row("a"), pane="%9"), row("b")]
        p = Picker(rows, "a", clock=Clock(), own_pane="%9")
        p.key(" ")
        p.key(" ")
        p.key("S")
        self.assertEqual(p.prompt, "將重開：b　略過：a（sampan 就在這個 pane）　確定？(y/N)")

    def test_new_targets_cursor_folder(self):
        p = self.picker()
        p.key("j")
        self.assertEqual(p.key("n"), ("new", ["客戶/甲"]))
        self.assertEqual(p.here_parent(), "客戶/甲")
        p = self.picker(current="a1")
        self.assertEqual(p.key("n"), ("new", ["客戶"]))
        p = self.picker(current="top")
        self.assertEqual(p.key("n"), ("new", [""]))

    def test_end_search_reveals_session_in_collapsed_folder(self):
        p = self.picker()
        self.press(p, "j", "\n", "k", "\n")  # collapse 客戶/甲, then 客戶
        self.press(p, "/", "b", "1")
        self.assertEqual([here(p)], [("session", "b1")])
        self.assertTrue(all(not isinstance(i, FolderItem) for i in p.visible))
        self.assertIsNone(p.key("\x1b"))
        self.assertIsNone(p.query)
        self.assertEqual(here(p), ("session", "b1"))
        self.assertEqual(p.tree.folders, {"客戶": False, "客戶/甲": False})

    def test_search_never_lands_on_a_folder_row(self):
        p = self.picker()  # cursor on the folder 客戶
        self.assertEqual(p.key("/"), None)
        self.assertTrue(all(not isinstance(i, FolderItem) for i in p.visible))
        self.assertEqual(p.key("\n"), ("attach", ["a1"]))  # the first match, never the folder
        p = self.picker()
        p.key("/")
        for _ in range(6):
            p.key("KEY_DOWN")
            self.assertNotIsInstance(p.visible[p.cursor], FolderItem)
        p = self.picker(current="a1")
        p.key("/")
        self.assertEqual(here(p), ("session", "a1"))  # a session stays under the cursor

    def test_current_session_in_collapsed_folder_is_revealed(self):
        tree = folder_tree(客戶=True, 客戶_甲=True)
        p = self.picker(current="x", tree=tree)
        self.assertEqual(here(p), ("session", "x"))
        self.assertEqual(p.tree.folders, {"客戶": False, "客戶/甲": False})
        self.assertEqual(tree.folders, {"客戶": True, "客戶/甲": True})  # the caller's tree is untouched

    def test_broken_folders_lists_flat(self):
        p = Picker(folder_rows(), tree=None)
        self.assertEqual([i.name for i in p.visible], ["a1", "b1", "top", "x"])
        self.assertFalse(p.on_folder())
        self.assertIsNone(p.key("KEY_LEFT"))
        self.assertEqual(p.cursor, 0)
        self.assertEqual(p.key("g"), ("group", ["session:a1"]))
        self.assertEqual(p.key("n"), ("new", [""]))

    def test_set_tree_keeps_cursor_item(self):
        p = self.picker(current="b1")
        p.key(" ")
        p.key("k")
        p.set_tree(Tree({"開發": False}, {"b1": "開發"}))
        self.assertEqual(here(p), ("session", "b1"))
        self.assertEqual([getattr(i, "path", i.name) for i in p.visible], ["開發", "b1", "a1", "top", "x"])
        self.assertEqual(p.marked, {"b1"})

    def test_folder_rows_are_not_others(self):
        other = type("O", (), {"name": "o1", "thread": type("T", (), {"name": "th"})()})()
        p = self.picker(others=[other])
        self.assertFalse(p.on_other())
        self.assertEqual(p.n_rows, 6)
        self.press(p, *["j"] * 6)
        self.assertTrue(p.on_other())
        self.assertEqual(p.key("e"), ("desktop", []))
        self.assertIsNone(p.key("g"))


class InteractiveUiTest(UiTest):
    def ui_keys(self, action, answers=(), current=None):
        answers = iter(answers)
        self.asked = []

        def ask(p):
            self.asked.append(p)
            return next(answers)
        return cmd_ui(
            self.cfg, self.r, self.msgs.append, ask,
            current_session=current, own_pane="%9", rows_fn=lambda c, r: ROWS,
            picker_fn=lambda rows, cur: action,
            exec_fn=lambda *a: self.execs.append(a),
            stop_fn=lambda *a, **kw: self.calls.append(("stop", kw["names"])) or 0,
            new_fn=lambda cfg, run, out, name, profile, cwd, **kw: self.calls.append(("new", name, profile, cwd)) or 0,
            set_state_fn=lambda cfg, out, name, state: self.calls.append(("state", name, state)) or 0,
            move_fn=lambda cfg, run, out, name, dest, **kw: self.calls.append(("move", name, dest)) or 0,
            switch_fn=lambda cfg, run, out, name, prof, **kw: self.calls.append(("switch", name, prof, kw)) or 0,
            rename_fn=lambda cfg, run, out, old, new: self.calls.append(("rename", old, new)) or 0,
            alias_fn=lambda cfg, out, name, alias, **kw: self.calls.append(("alias", name, alias, kw)) or 0,
            forget_fn=lambda cfg, run, out, name: self.calls.append(("forget", name)) or 0,
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

    def test_switch_stopped_says_reattach(self):
        stopped = [row("a", state=State.STOPPED, agent_status="-")]
        answers = iter(["2", "y"])
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), own_pane="%9",
               rows_fn=lambda c, r: stopped, picker_fn=lambda rows, cur: ("switch", ["a"]),
               switch_fn=lambda cfg, run, out, name, prof, **kw: self.calls.append(("switch", name, prof)) or 0)
        self.assertIn("會用新 profile 接回這段對話", "\n".join(self.msgs))
        self.assertEqual(self.calls, [("switch", "a", "work")])

    def test_switch_many_picks_profile_asks_and_runs_batch(self):
        rows = [row("a"), row("b"), row("c", agent_status="busy")]
        answers = iter(["2", "y"])
        asked = []
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda p: asked.append(p) or next(answers), own_pane="%9",
               rows_fn=lambda c, r: rows, picker_fn=lambda rows, cur: ("switch_many", ["a", "b", "c"]),
               switch_fn=lambda cfg, run, out, name, prof, **kw: self.calls.append(("switch", name, prof, kw)) or 0)
        self.assertIn("開始？ y/N: ", asked)
        self.assertEqual([c[:3] for c in self.calls], [("switch", "a", "work"), ("switch", "b", "work")])
        self.assertIn("跳過 1 個：c（忙碌中）", self.text())

    def test_move_confirms_first(self):
        self.ui_keys(("move", ["a"]), ["/tmp", "y"])
        self.assertEqual(self.calls, [("move", "a", str(Path("/tmp").resolve()))])

    def test_ui_X_confirms_then_forgets(self):
        self.ui_keys(("forget", ["old"]), ["y"])
        self.assertEqual(self.calls, [("forget", "old")])
        self.assertIn("從登記表移除 old？之後不會再列出，也不會被 R 接回；對話紀錄不刪 (y/N)", "\n".join(self.asked))

    def test_ui_X_n_cancels(self):
        self.ui_keys(("forget", ["old"]), ["n"])
        self.assertEqual(self.calls, [])
        self.assertIn("已取消", self.msgs)

    def test_plain_X_picks_by_number(self):
        self.ui(["X", "3", "y"])
        self.assertEqual(self.calls, [("forget", "old")])

    def test_state_and_new_and_quit(self):
        self.ui_keys(("retired", ["a"]))
        self.ui_keys(("new", []), ["fresh", "", "", "/tmp"])
        self.assertEqual(self.ui_keys(("quit", [])), 0)
        self.assertEqual(self.calls, [("state", "a", "retired"), ("new", "fresh", "claude:alice", "/tmp")])

    def test_rename_asks_new_name(self):
        self.ui_keys(("rename", ["a"]), ["fresh"])
        self.assertEqual(self.calls, [("rename", "a", "fresh")])

    def test_ui_L_key_sets_alias(self):
        self.assertEqual(Picker(ROWS, current="b").key("L"), ("alias", ["b"]))
        self.ui_keys(("alias", ["a"]), ["讀書工具 PM"])
        self.assertEqual(self.calls, [("alias", "a", "讀書工具 PM", {})])
        self.assertIn("別名（空白＝清掉）：", self.asked)

    def test_ui_L_blank_answer_clears_alias(self):
        self.ui_keys(("alias", ["a"]), [""])
        self.assertEqual(self.calls, [("alias", "a", "", {"clear": True})])

    def test_plain_L_picks_by_number(self):
        self.ui(["L", "2", "x y"])
        self.assertEqual(self.calls[0][:3], ("alias", "b", "x y"))

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


class FolderUiTest(unittest.TestCase):
    """The folder actions and S of cmd_ui (curses keys arrive through picker_fn; --plain types numbers)."""
    ui, text = UiTest.ui, UiTest.text
    ui_keys = InteractiveUiTest.ui_keys

    def setUp(self):
        UiTest.setUp(self)
        register(self.cfg, "a", "b", "old")

    def tree(self, fn):
        from sampan import folders
        folders.edit(self.cfg.state_dir, fn)

    def load(self):
        from sampan import folders
        return folders.load(self.cfg.state_dir)

    def folders_file(self):
        return self.cfg.state_dir / "folders.json"

    def test_restart_action_calls_cmd_restart_and_never_attaches(self):
        from sampan.restart import RestartPlan
        plan = RestartPlan(["a"], ["s"], [("b", "忙")])
        seen = []

        def stop(cfg, run, out, **kw):
            seen.append(("stop", kw["names"], kw["own_pane"]))
            kw["stopped"].append("a")
            return 0

        def restore(cfg, run, out, ask, **kw):
            seen.append(("restore", kw["names"], kw["confirm"]))
            return 0
        rows = ROWS + [row("s", state=State.STOPPED, agent_status="-")]
        rc = cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", current_session="old", own_pane="%9",
                    rows_fn=lambda c, r: rows, picker_fn=lambda rows, cur: ("restart!", [plan]),
                    exec_fn=lambda *a: self.execs.append(a), stop_fn=stop, restore_fn=restore)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [("stop", ("a",), "%9"), ("restore", ("a", "s"), False)])
        self.assertEqual(self.execs, [])
        self.assertEqual(self.r.called(["tmux", "-u", "switch-client"]), [])
        self.assertIn("略過：b（忙）", self.text())

    def test_group_action_asks_name_and_writes(self):
        self.ui_keys(("group", ["session:a", "session:b"]), ["客戶"])
        self.assertIn("新資料夾名稱：", "\n".join(self.asked))
        self.assertEqual(self.load().sessions, {"a": "客戶", "b": "客戶"})
        self.assertIn("已建立資料夾 客戶（2 項）", self.text())
        self.assertEqual(self.calls, [])  # no session was stopped, renamed or forgotten

    def test_group_bad_name_says_why_and_writes_nothing(self):
        self.ui_keys(("group", ["session:a"]), ["甲/乙"])
        self.assertIn("名稱不能含 /", self.text())
        self.assertFalse(self.folders_file().exists())

    def test_group_name_taken_asks_merge(self):
        from sampan import folders
        self.tree(lambda t: folders.place_session(t, "old", "客戶"))
        before = self.folders_file().read_bytes()
        self.ui_keys(("group", ["session:a"]), ["客戶", "n"])
        self.assertIn("同一層已經有「客戶」，合併進去？(y/N)", "\n".join(self.asked))
        self.assertEqual(self.folders_file().read_bytes(), before)
        self.ui_keys(("group", ["session:a"]), ["客戶", "y"])
        self.assertEqual(self.load().sessions, {"a": "客戶", "old": "客戶"})

    def test_rm_folder_asks_and_keeps_sessions(self):
        from sampan import folders
        self.tree(lambda t: (folders.place_session(t, "a", "客戶/甲"), folders.place_session(t, "b", "客戶")))
        self.ui_keys(("rm_folder", ["客戶"]), ["y"])
        self.assertIn("刪除資料夾 客戶？裡面的 session 會往上移一層，不會停掉 (y/N)", "\n".join(self.asked))
        t = self.load()
        self.assertEqual(t.sessions, {"a": "甲"})
        self.assertNotIn("客戶", t.folders)
        self.assertEqual(self.calls, [])

    def test_rm_folder_no_keeps_it(self):
        from sampan import folders
        self.tree(lambda t: folders.place_session(t, "a", "客戶"))
        self.ui_keys(("rm_folder", ["客戶"]), [""])
        self.assertEqual(self.load().sessions, {"a": "客戶"})
        self.assertIn("已取消", self.msgs)

    def test_rename_folder_asks_and_merges(self):
        from sampan import folders
        self.tree(lambda t: (folders.place_session(t, "a", "客戶"), folders.place_session(t, "b", "開發")))
        self.ui_keys(("rename_folder", ["客戶"]), ["維運"])
        self.assertIn("新名稱：", "\n".join(self.asked))
        self.assertEqual(self.load().sessions, {"a": "維運", "b": "開發"})
        self.ui_keys(("rename_folder", ["維運"]), ["開發", "y"])
        self.assertEqual(self.load().sessions, {"a": "開發", "b": "開發"})

    def test_file_action_lists_destinations_and_moves(self):
        from sampan import folders
        self.tree(lambda t: (folders.add(t, "開發"), folders.place_session(t, "b", "客戶/甲")))
        self.ui_keys(("file", ["session:a"]), ["4"])
        text = self.text()
        for line in ("[1] （最外層）", "[2] 新資料夾…", "[3] 客戶", "[4] 客戶/甲", "[5] 開發"):
            self.assertIn(line, text)
        self.assertEqual(self.load().sessions, {"a": "客戶/甲", "b": "客戶/甲"})
        self.assertIn("已搬到 客戶/甲", text)

    def test_file_action_excludes_moved_folder_and_its_children(self):
        from sampan import folders
        self.tree(lambda t: (folders.add(t, "開發"), folders.place_session(t, "b", "客戶/甲")))
        self.ui_keys(("file", ["folder:客戶"]), ["3"])
        text = self.text()
        self.assertNotIn("] 客戶", text)
        self.assertEqual(self.load().sessions, {"b": "開發/客戶/甲"})
        self.assertIn("已搬到 開發", text)

    def test_file_action_to_top_and_to_a_new_folder(self):
        from sampan import folders
        self.tree(lambda t: folders.place_session(t, "a", "客戶"))
        self.ui_keys(("file", ["session:a"]), ["1"])
        self.assertEqual(self.load().sessions, {})
        self.assertIn("已搬到 最外層", self.text())
        self.ui_keys(("file", ["session:a"]), ["2", "維運"])
        self.assertEqual(self.load().sessions, {"a": "維運"})

    def test_file_new_folder_with_taken_name_asks_merge(self):
        from sampan import folders
        self.tree(lambda t: folders.place_session(t, "old", "客戶"))
        before = self.folders_file().read_bytes()
        self.ui_keys(("file", ["session:a"]), ["2", "客戶", "n"])
        self.assertIn("同一層已經有「客戶」，合併進去？(y/N)", "\n".join(self.asked))
        self.assertIn("已取消", self.msgs)
        self.assertEqual(self.folders_file().read_bytes(), before)
        self.ui_keys(("file", ["session:a"]), ["2", "客戶", "y"])
        self.assertEqual(self.load().sessions, {"a": "客戶", "old": "客戶"})
        self.assertIn("✔ 已搬到 客戶", self.text())

    def test_name_taken_after_merge_shows_the_error(self):
        from unittest import mock

        from sampan import folders
        self.tree(lambda t: folders.add(t, "客戶"))
        with mock.patch("sampan.folders.group", side_effect=folders.NameTaken("客戶")):
            self.ui_keys(("group", ["session:a"]), ["客戶", "y"])
        self.assertIn("❌ 同一層已經有「客戶」", self.text())
        self.assertNotIn("已取消", self.msgs)

    def test_file_action_uses_the_chooser_when_given(self):
        from sampan import folders
        self.tree(lambda t: folders.add(t, "客戶"))
        offered = []
        cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", rows_fn=lambda c, r: ROWS,
               picker_fn=lambda rows, cur: ("file", ["session:a"]),
               choose_folder_fn=lambda labels: offered.append(labels) or 2)
        self.assertEqual(offered, [["（最外層）", "新資料夾…", "客戶"]])
        self.assertEqual(self.load().sessions, {"a": "客戶"})

    def test_broken_folders_refuse_folder_actions(self):
        self.folders_file().parent.mkdir(parents=True, exist_ok=True)
        self.folders_file().write_text("{")
        for action in (("group", ["session:a"]), ("file", ["session:a"]), ("rename_folder", ["客戶"]),
                       ("rm_folder", ["客戶"])):
            with self.subTest(action=action[0]):
                self.msgs.clear()
                self.ui_keys(action, [])
                self.assertIn("資料夾設定壞了，修好或刪掉 folders.json 再試", self.text())
                self.assertEqual(self.asked, [])
        self.ui_keys(("collapse", ["客戶", "1"]))
        self.assertEqual(self.folders_file().read_text(), "{")

    def test_collapse_saves_silently(self):
        from sampan import folders
        self.tree(lambda t: folders.place_session(t, "a", "客戶"))
        self.msgs.clear()
        self.ui_keys(("collapse", ["客戶", "1"]))
        self.assertTrue(self.load().folders["客戶"])
        self.ui_keys(("collapse", ["不存在/的", "0"]))  # gone since the last refresh: nothing, never an error
        self.assertNotIn("❌", self.text())
        self.assertNotIn("不存在", self.load().folders)

    def test_collapse_never_recreates_a_deleted_folder(self):
        from sampan import folders
        self.tree(lambda t: folders.add(t, "甲"))
        self.tree(lambda t: folders.remove(t, "甲"))  # another sampan deleted it
        self.ui_keys(("collapse", ["甲", "1"]))
        self.assertEqual(self.load().folders, {})

    def test_new_in_folder_passes_folder(self):
        seen = []
        for target in (["客戶"], [""], []):
            answers = iter(["fresh", "", "", "/tmp"])
            cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: next(answers), rows_fn=lambda c, r: ROWS,
                   picker_fn=lambda rows, cur, t=target: ("new", t), exec_fn=lambda *a: self.execs.append(a),
                   new_fn=lambda cfg, run, out, name, profile, cwd, **kw: seen.append(kw) or 0)
        self.assertEqual(seen, [{"wait": False, "folder": "客戶"}, {"wait": False}, {"wait": False}])

    def test_plain_lists_folder_prefix_and_S_restarts(self):
        from sampan import folders
        self.tree(lambda t: folders.place_session(t, "b", "客戶"))
        self.ui(["S", "1 2", "y"])
        text = self.text()
        self.assertIn("[ 1] 客戶 · b", text)  # the tree's order: folders first
        self.assertIn("[ 2] a ", text)
        self.assertIn("將重開：a　略過：b（忙）", text)
        self.assertEqual([c[0] for c in self.calls], ["stop"])
        self.assertEqual(self.calls[0][1]["names"], ("a",))
        self.assertEqual(self.execs, [])

    def test_plain_S_cancel_and_nothing_to_do(self):
        self.ui(["S", "1", ""])
        self.assertEqual(self.calls, [])
        self.assertIn("已取消", self.text())
        self.ui(["S", "3"])
        self.assertIn("沒有可以重開的", self.text())
        self.assertEqual(self.calls, [])

    def test_plain_folder_keys_point_to_folder_command(self):
        self.ui(["g"])
        self.assertIn("sampan folder", self.text())
        self.assertIn("S 重開", self.text())

    def test_plain_broken_folders_lists_flat_and_warns(self):
        self.folders_file().parent.mkdir(parents=True, exist_ok=True)
        self.folders_file().write_text("{")
        self.ui(["q"])
        self.assertIn("資料夾設定讀不到", self.text())
        self.assertIn("[ 1] a ", self.text())

    def test_folder_actions_logged(self):
        self.ui_keys(("group", ["session:a"]), ["客戶"])
        log = (self.cfg.state_dir / "actions.log").read_text()
        self.assertIn("[資料夾] ✔ 已建立資料夾 客戶（1 項）", log)


class DetachKeyUiTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.msgs, self.marked = [], []
        self.r = FakeRunner()
        self.r.on(["tmux", "-u", "has-session"], cp())

    def run_ui(self, actions, warning=None):
        actions = iter(actions)

        def detach(cfg, run, names):
            self.marked.append(list(names))
            return warning
        return cmd_ui(self.cfg, self.r, self.msgs.append, lambda _p: "", loop=True, rows_fn=lambda c, r: ROWS,
                      picker_fn=lambda rows, cur: next(actions), exec_fn=lambda *a: None,
                      attach_fn=lambda args: 0, detach_fn=detach)

    def test_marks_running_sessions_at_start_and_the_target_before_attaching(self):
        self.run_ui([("attach", ["b"]), ("quit", [])])
        alive = [r.name for r in ROWS if r.tmux_alive]
        self.assertEqual(self.marked[0], alive)
        self.assertEqual(self.marked[1], ["b"])

    def test_an_unmanaged_tmux_session_is_never_marked(self):
        global ROWS
        saved = ROWS
        try:
            ROWS = saved + [row("main", state=State.NO_AGENT, agent="-", agent_status="-")]
            self.run_ui([("attach", ["main"]), ("quit", [])])
        finally:
            ROWS = saved
        self.assertNotIn(["main"], self.marked)
        self.assertNotIn("main", sum(self.marked, []))

    def test_a_warning_is_shown_once(self):
        self.run_ui([("attach", ["a"]), ("attach", ["b"]), ("quit", [])], warning="tmux 已經把 C-q 綁成別的功能")
        self.assertEqual(sum("C-q" in m for m in self.msgs), 1)


class TellPickerTest(unittest.TestCase):
    """t: the keys only; the flow is TellFlowTest. Visible order: 客戶, 客戶/甲, b1, x, a1, top."""

    def picker(self, tree=None, others=()):
        return Picker(folder_rows(), "a1", clock=Clock(), tree=tree or folder_tree(), others=others)

    def test_t_on_a_session_row(self):
        self.assertEqual(self.picker().key("t"), ("tell", ["a1"]))

    def test_t_on_marked_rows_in_display_order(self):
        p = self.picker()
        p.cursor = 2
        p.key(" ")  # b1 (cursor moves on to x)
        p.key(" ")  # x
        self.assertEqual(p.key("t"), ("tell", ["b1", "x"]))

    def test_t_on_a_folder_row_targets_its_sessions(self):
        p = self.picker()
        p.cursor = 0
        self.assertEqual(p.key("t"), ("tell", ["a1", "b1", "x"]))

    def test_t_on_an_empty_folder_says_so(self):
        p = self.picker(tree=Tree({"空": False}, {}))
        p.cursor = 0
        self.assertTrue(p.on_folder())
        self.assertIsNone(p.key("t"))
        self.assertIn("沒有 session", p._notice)

    def test_t_on_another_machines_row_only_says_where_to_go(self):
        from types import SimpleNamespace as NS
        p = self.picker(others=[NS(name="\x00other:t", thread=NS(name="t"))])
        p.cursor = len(p.visible) - 1
        self.assertTrue(p.on_other())
        self.assertEqual(p.key("t"), ("desktop", []))


class TellFlowTest(unittest.TestCase):
    setUp, text = UiTest.setUp, UiTest.text
    ui_keys = InteractiveUiTest.ui_keys

    def disp(self, did, to, state="queued", reason=""):
        from sampan.dispatch import Dispatch
        return Dispatch(did, "終端機", "", "", to, "claude", "alice", "x", state, "2026-10-31T00:00:00Z", reason=reason)

    def tell(self, names, answers):
        return self.ui_keys(("tell", names), answers)

    def test_t_single_queues_through_submit(self):
        seen = []

        def submit(cfg, run, caller, to, message, **kw):
            seen.append((caller, to, message, kw["raw"], kw["notify"]))
            return self.disp("1a2b", to, reason="對方忙")
        with mock.patch("sampan.send.submit", submit):
            self.tell(["b"], ["請看一下"])
        from sampan.send import TERMINAL
        self.assertEqual(seen, [(TERMINAL, "b", "請看一下", False, False)])
        self.assertEqual(self.asked, ["訊息："])
        self.assertIn("1a2b", self.text())
        self.assertIn("排隊中", self.text())
        self.assertIn("對方忙", self.text())

    def test_t_marked_rows_go_through_submit_batch_and_list_skipped(self):
        from sampan.send import Batch
        seen = []

        def batch(cfg, run, caller, names, message, **kw):
            seen.append((names, message, kw["raw"], kw["notify"]))
            return Batch("b0a1", [self.disp("1111", "a")], [("b", "對方在忙")])
        with mock.patch("sampan.send.submit_batch", batch), \
                mock.patch("sampan.send.submit", side_effect=AssertionError("single path")):
            self.tell(["a", "b"], ["大家好"])
        self.assertEqual(seen, [(["a", "b"], "大家好", False, False)])
        t = self.text()
        self.assertIn("b0a1", t)
        self.assertIn("1111", t)
        self.assertIn("跳過 b", t)
        self.assertIn("對方在忙", t)

    def test_t_batch_refusal_shown(self):
        with mock.patch("sampan.send.submit_batch", return_value="沒有可以送的 session"):
            self.tell(["a", "b"], ["hi"])
        self.assertIn("❌ 沒有可以送的 session", self.text())

    def test_t_slash_asks_raw_and_default_is_no(self):
        seen = []

        def submit(cfg, run, caller, to, message, **kw):
            seen.append(kw["raw"])
            return self.disp("2222", to)
        with mock.patch("sampan.send.submit", submit):
            self.tell(["a"], ["/compact", ""])
            self.tell(["a"], ["/compact", "n"])
            self.tell(["a"], ["/compact", "y"])
            self.tell(["a"], ["  /compact", "Y"])
        self.assertEqual(seen, [False, False, True, True])
        self.assertEqual(self.asked, ["訊息：", "直接送（不加標頭）？ y/N"])

    def test_t_non_slash_does_not_ask_raw(self):
        with mock.patch("sampan.send.submit", return_value=self.disp("3333", "a")):
            self.tell(["a"], ["hello"])
        self.assertEqual(self.asked, ["訊息："])

    def test_t_raw_problem_shown_and_nothing_created(self):
        with mock.patch("sampan.send.submit", return_value="直接送只能是一行"):
            self.tell(["a"], ["/a\nb", "y"])
        self.assertIn("❌ 直接送只能是一行", self.text())

    def test_t_empty_message_cancels(self):
        for ans in ("", "   ", None):
            with mock.patch("sampan.send.submit", side_effect=AssertionError), \
                    mock.patch("sampan.send.submit_batch", side_effect=AssertionError):
                self.tell(["a"], [ans])
        self.assertNotIn("❌", self.text())

    def test_t_on_folder_targets_its_sessions(self):
        from sampan.send import Batch
        seen = []
        with mock.patch("sampan.send.submit_batch", lambda c, r, caller, names, m, **kw: seen.append(names) or Batch(
                "b000", [], [])):
            self.tell(["a1", "b1", "x"], ["hi"])
        self.assertEqual(seen, [["a1", "b1", "x"]])
