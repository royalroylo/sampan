import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sampan.registry import Row, load_rows, write_rows
from sampan.status import State, _fmt_tokens, cmd_status, status_rows
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import local_tz, make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
D = "dddddddd-0000-4000-8000-000000000004"


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(self.cfg.registry_path, [
            Row("running", "claude", "alice", A, "/c"), Row("down", "claude", "alice", B, "/c"),
            Row("wrongprof", "claude", "alice", C, "/c"), Row("old", "claude", "alice", D, "/c", "retired"),
        ], "t", self.cfg.history_dir)
        panes = [pane_line("running", "%1", 100, "claude", "/c"), pane_line("wrongprof", "%2", 101, "claude", "/c"),
                 pane_line("extra", "%3", 102, "claude", "/c")]
        write_session(self.cfg, "alice", 200, sid=A, tmux="running:@1.%1", status="busy")
        write_session(self.cfg, "work", 201, sid=C, tmux="wrongprof:@2.%2")
        write_session(self.cfg, "work", 202, sid=D, tmux="extra:@3.%3")
        d = self.cfg.default_profile.projects_dir / "-c"
        d.mkdir(parents=True)
        (d / f"{A}.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-17T03:00:00.000Z",
                                                  "message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 300000}}}) + "\n")
        procs = [ps_line(100 + i, 1, "-zsh", "-zsh") for i in range(3)] + \
                [ps_line(200 + i, 100 + i, "claude", "claude") for i in range(3)]
        self.r = FakeRunner()
        self.r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        self.r.on(["ps", "-A"], cp(out="\n".join(procs) + "\n"))

    def test_states(self):
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        self.assertEqual(rows["running"].state, State.RUNNING)
        self.assertEqual(rows["running"].agent_status, "busy")
        self.assertEqual(rows["down"].state, State.STOPPED)
        self.assertEqual(rows["down"].display, "已停止")  # same word as the sampan screen; a stop on purpose is not a fault
        self.assertEqual(rows["wrongprof"].state, State.PROFILE_MISMATCH)
        self.assertEqual(rows["wrongprof"].detail, "（登記 alice）")
        self.assertEqual(rows["wrongprof"].display, "profile 不符（登記 alice）")
        self.assertEqual(rows["old"].state, State.RETIRED)
        self.assertEqual(rows["extra"].state, State.UNREGISTERED)
        self.assertEqual(rows["running"].tokens, 300001)

    def test_status_row_carries_id(self):
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        reg = {r.tmux: r.id for r in load_rows(self.cfg.registry_path)}
        self.assertTrue(reg["running"])
        self.assertEqual((rows["running"].id, rows["old"].id), (reg["running"], reg["old"]))
        self.assertEqual(rows["extra"].id, "")  # live but not registered

    def test_rows_carry_the_agent_pane(self):
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        self.assertEqual(rows["running"].pane, "%1")
        self.assertIsNone(rows["down"].pane)

    def test_output_marks_large_context_and_local_time(self):
        msgs = []
        with local_tz("Asia/Taipei"):  # the expected time below is Taipei time, whatever this machine's zone is
            self.assertEqual(cmd_status(self.cfg, self.r, msgs.append), 0)
        line = next(m for m in msgs if m.startswith("running"))
        self.assertIn("300k ⚠", line)
        self.assertIn("10/17 11:00", line)


class ShortIdStatusTest(StatusTest):
    def test_status_shows_short_id(self):
        msgs = []
        cmd_status(self.cfg, self.r, msgs.append)
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        self.assertTrue(rows["running"].id)
        self.assertEqual(msgs[0].split()[3:5], ["id", "對話"])
        line = next(m for m in msgs if m.startswith("running"))
        self.assertEqual(line.split()[3], rows["running"].id[:8])


class TrustPromptStatusTest(StatusTest):
    def test_session_waiting_at_the_trust_prompt_is_not_reported_as_down(self):
        panes = [pane_line("running", "%1", 100, "claude", "/c"), pane_line("down", "%4", 103, "claude", "/c")]
        self.r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        self.r.on(["tmux", "-u", "capture-pane"], cp(out="Do you trust this folder?\n ❯ No, exit\n   Yes, I trust this folder\n"))
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        self.assertEqual(rows["down"].state, State.AWAIT_TRUST)

    def test_trust_text_left_in_the_scrollback_stays_down(self):
        panes = [pane_line("running", "%1", 100, "claude", "/c"), pane_line("down", "%4", 103, "claude", "/c")]
        self.r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        self.r.on(["tmux", "-u", "capture-pane"], cp(out=" ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm · Esc to cancel\n\n> hi\n\n  ? for shortcuts\n"))
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        self.assertEqual(rows["down"].state, State.STOPPED)

    def test_down_session_without_a_prompt_stays_down(self):
        panes = [pane_line("running", "%1", 100, "claude", "/c"), pane_line("down", "%4", 103, "zsh", "/c")]
        self.r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        self.r.on(["tmux", "-u", "capture-pane"], cp(out="➜  ~\n"))
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        self.assertEqual(rows["down"].state, State.STOPPED)
        self.assertTrue(rows["down"].tmux_alive)  # final review 1: a new session still starting has its tmux

    def test_tmux_alive_only_when_the_tmux_session_exists(self):
        rows = {x.name: x for x in status_rows(self.cfg, self.r)}
        self.assertEqual((rows["down"].state, rows["down"].tmux_alive), (State.STOPPED, False))
        self.assertTrue(rows["running"].tmux_alive)
        self.assertTrue(rows["extra"].tmux_alive)


TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FmtTokensTest(unittest.TestCase):
    def test_fmt_tokens_codex_threshold(self):
        self.assertEqual(_fmt_tokens(206720, 258400), "207k ⚠")  # 80% of the model window
        self.assertEqual(_fmt_tokens(200000, 258400), "200k")
        self.assertEqual(_fmt_tokens(260000), "260k ⚠")  # no window: the fixed Claude limit


class AgentStatusTest(unittest.TestCase):
    def test_rows_have_agent_and_last_activity(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(cfg.registry_path, [Row("cl", "claude", "work", A, "/c"), Row("cx", "codex", "", TID, "/c")],
                   "t", cfg.history_dir)
        # Even with no registry row, work's transcript must be found (prefer uses the running profile)
        write_session(cfg, "work", 200, sid=A, tmux="cl:@1.%1", status="idle")
        write_session(cfg, "work", 202, sid=B, tmux="extra:@3.%3", status="idle")
        d = cfg.profiles["work"].projects_dir / "-c"
        d.mkdir(parents=True)
        (d / f"{A}.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-17T03:00:00.000Z"}) + "\n")
        (d / f"{B}.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-10-18T03:00:00.000Z"}) + "\n")
        rollout = cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(json.dumps({"timestamp": "2026-10-20T01:00:00.000Z", "type": "session_meta"}) + "\n"
                           + json.dumps({"timestamp": "2026-10-20T02:30:00.000Z", "type": "event_msg"}) + "\n"
                           + json.dumps({"timestamp": "2026-10-20T02:30:00.000Z", "type": "event_msg", "payload": {
                               "type": "token_count", "info": {"last_token_usage": {"input_tokens": 49227},
                                                               "model_context_window": 258400}}}) + "\n")
        panes = [pane_line("cl", "%1", 100, "claude", "/c"), pane_line("cx", "%2", 101, "codex", "/c"),
                 pane_line("extra", "%3", 102, "claude", "/c")]
        procs = [ps_line(100 + i, 1, "-zsh", "-zsh") for i in range(3)] + [
            ps_line(200, 100, "claude", "claude"), ps_line(201, 101, "codex", "codex"),
            ps_line(202, 102, "claude", "claude")]
        r = FakeRunner()
        r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        r.on(["ps", "-A"], cp(out="\n".join(procs) + "\n"))
        r.on(["lsof"], lambda a: cp(out=f"p201\nf10\nn{rollout}\n" if a[2] == "201" else ""))
        r.on(["tmux", "-u", "capture-pane"], cp(out=(FIXTURES / "codex_working.txt").read_text()))
        rows = {x.name: x for x in status_rows(cfg, r)}
        self.assertEqual((rows["cl"].agent, rows["cl"].state), ("claude", State.RUNNING))
        self.assertEqual(rows["cl"].last, datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc))
        self.assertEqual((rows["cx"].agent, rows["cx"].state, rows["cx"].agent_status), ("codex", State.RUNNING, "working"))
        self.assertEqual(rows["cx"].last, datetime(2026, 10, 20, 2, 30, tzinfo=timezone.utc))
        self.assertEqual((rows["cx"].tokens, rows["cx"].context_window), (49227, 258400))
        self.assertIsNone(rows["cl"].context_window)
        # old "" registry row + live codex: the profile reads as the Codex profile name, so no mismatch is flagged
        self.assertEqual(rows["cx"].profile, cfg.codex.profile)
        self.assertNotEqual(cfg.codex.profile, "")
        self.assertEqual(rows["extra"].last, datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc))
        msgs = []
        with local_tz("Asia/Taipei"):
            cmd_status(cfg, r, msgs.append)
        self.assertIn("10/20 10:30", next(m for m in msgs if m.startswith("cx")))
        msgs.clear()
        with local_tz("America/New_York"):  # shown in the machine's own timezone, not a fixed one
            cmd_status(cfg, r, msgs.append)
        self.assertIn("10/19 22:30", next(m for m in msgs if m.startswith("cx")))

    def test_codex_last_falls_back_to_rollout_mtime(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(cfg.registry_path, [Row("cx", "codex", "", TID, "/c")], "t", cfg.history_dir)
        rollout = cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("not json\n")
        os.utime(rollout, (1_792_000_000, 1_792_000_000))
        r = FakeRunner().on(["tmux", "-u", "list-panes"], cp(out=""))
        rows = status_rows(cfg, r)
        self.assertEqual(rows[0].last, datetime.fromtimestamp(1_792_000_000, timezone.utc))


class StatusAlignmentTest(unittest.TestCase):
    def test_columns_line_up_by_display_width_with_cjk_names(self):
        from sampan.textwidth import text_width
        cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(cfg.registry_path, [Row("報價系統-測試", "claude", "alice", A, "/c"),
                                       Row("plain", "claude", "alice", B, "/c")], "t", cfg.history_dir)
        r = FakeRunner().on(["tmux", "-u", "list-panes"], cp(out=""))
        msgs = []
        cmd_status(cfg, r, msgs.append)
        lines = [m for m in msgs if not m.startswith("⚠")]
        # the agent column starts at the same screen column on the header and every row
        starts = {text_width(m[:m.index(word)]) for m, word in zip(lines, ["agent", "claude", "claude"])}
        self.assertEqual(len(starts), 1, lines)

    def test_long_name_is_not_cut(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        long = "長" * 30
        write_rows(cfg.registry_path, [Row(long, "claude", "alice", A, "/c")], "t", cfg.history_dir)
        r = FakeRunner().on(["tmux", "-u", "list-panes"], cp(out=""))
        msgs = []
        cmd_status(cfg, r, msgs.append)
        self.assertTrue(any(m.startswith(long + " ") for m in msgs), msgs)


class MultiAgentStatusTest(unittest.TestCase):
    def test_session_with_two_agents_is_unmanaged(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(cfg.registry_path, [Row("multi", "claude", "alice", A, "/c")], "t", cfg.history_dir)
        write_session(cfg, "alice", 200, sid=A, tmux="multi:@1.%1", status="idle")
        write_session(cfg, "alice", 201, sid=B, tmux="multi:@1.%2", status="idle")
        panes = [pane_line("multi", "%1", 100, "claude", "/c"), pane_line("multi", "%2", 101, "claude", "/c")]
        procs = [ps_line(100 + i, 1, "-zsh", "-zsh") for i in range(2)] + \
                [ps_line(200 + i, 100 + i, "claude", "claude") for i in range(2)]
        r = FakeRunner()
        r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        r.on(["ps", "-A"], cp(out="\n".join(procs) + "\n"))
        rows = {x.name: x for x in status_rows(cfg, r)}
        self.assertEqual(rows["multi"].state, State.MULTI)


class StateEnumTest(unittest.TestCase):
    def test_every_state_has_a_label_and_values_are_english_identifiers(self):
        from sampan.status import LABELS, State
        self.assertEqual(set(LABELS), set(State))
        for s in State:
            self.assertTrue(s.value.isascii() and s.value.replace("_", "").isalpha(), s.value)
            if s is not State.RETIRED:  # "retired" was never translated, so label == value
                self.assertNotEqual(str(s), LABELS[s])  # the zh text lives only in LABELS

    def test_display_is_label_plus_detail(self):
        from sampan.status import State, StatusRow
        r = StatusRow("a", "claude", "carol", "", State.AGENT_MISMATCH, "（登記 codex）")
        self.assertEqual(r.display, "agent 不符（登記 codex）")
        self.assertEqual(StatusRow("a", "claude", "carol", "", State.RUNNING).display, "執行中")
