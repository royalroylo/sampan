"""Sending: submit a dispatch, advance it (queued → sent → done / failed), notify the caller, the send / dispatches commands."""
import dataclasses
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sampan import deliver, dispatch, send
from sampan.send import Caller
from sampan.status import State
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env, status_row

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
SID_B = "11111111-2222-3333-4444-555555555555"  # Claude target "b"
SID_A = "22222222-3333-4444-5555-666666666666"  # Claude caller "a"
TID_C = "33333333-4444-5555-6666-777777777777"  # Codex target "c"
TID_X = "44444444-5555-6666-7777-888888888888"  # Codex caller "x"
SID_D = "55555555-6666-7777-8888-999999999999"  # Claude target "d"
SID_Z = "88888888-9999-aaaa-bbbb-cccccccccccc"  # Claude caller "z"
CALLER_A = Caller("a", "alice", "claude", "%1")
EMPTY = (FIXTURES / "claude_empty_placeholder.ansi").read_text()
DRAFT = (FIXTURES / "claude_draft.ansi").read_text()


def later(seconds):
    return NOW + timedelta(seconds=seconds)


def user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def assistant(t):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def codex_item(turn, kind, text, phase=None):
    item = {"type": kind, "content": [{"type": "text" if kind == "UserMessage" else "Text", "text": text}]}
    if phase:
        item["phase"] = phase
    return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": item}}


def codex_done(turn, last):
    return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn, "last_agent_message": last}}


def tmux_runner(screen=EMPTY):
    run = FakeRunner(strict=True)
    run.on(["tmux", "-u", "capture-pane"], cp(0, screen))
    run.on(["tmux", "-u", "display-message"], cp(0, "0\n"))
    run.on(["tmux", "-u", "load-buffer"], cp())
    run.on(["tmux", "-u", "paste-buffer"], cp())
    run.on(["tmux", "-u", "send-keys"], cp())
    return run


def tmux_writes(run, pane=None):
    """Every tmux call that is not a capture (so: a key or a paste), optionally only those aimed at pane."""
    out = []
    for c in run.calls:
        a = c["args"]
        if a[0] == "tmux" and not {"capture-pane", "display-message"} & set(a) and (pane is None or pane in a):
            out.append(a)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        cfg = make_env(tmp)
        self.cfg = dataclasses.replace(cfg, codex=dataclasses.replace(cfg.codex, home=tmp / "codex-home"))
        self.rows = {
            "a": status_row("a", sid=SID_A, pane="%1"),
            "b": status_row("b", sid=SID_B, pane="%2"),
            "c": status_row("c", agent="codex", profile="codex", sid=TID_C, pane="%3"),
        }
        self.sleeps = []
        self.status_calls = 0

    def status_fn(self, cfg, run, snapshot_fn=None):
        self.status_calls += 1
        return list(self.rows.values())

    def set_row(self, name, **changes):
        self.rows[name] = dataclasses.replace(self.rows[name], **changes)

    def submit(self, to="b", message="hello", caller=CALLER_A, notify=False, run=None, **kw):
        return send.submit(self.cfg, run or FakeRunner(strict=True), caller, to, message, notify=notify, now=NOW,
                           status_fn=self.status_fn, **kw)

    def advance(self, run, did, at=NOW):
        return send.advance(self.cfg, run, did, sleep=self.sleeps.append, status_fn=self.status_fn, now=at)

    def transcript(self, sid, recs, profile_dir=".claude"):
        d = self.cfg.home / profile_dir / "projects" / "-proj"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))

    def rollout(self, tid, recs):
        d = self.cfg.codex.home / "sessions" / "2026" / "10" / "26"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"rollout-2026-10-26T12-00-00-{tid}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))

    def stored(self, did):
        return next(d for d in dispatch.load(self.cfg.state_dir) if d.id == did)


class SubmitTest(Base):
    def test_raw_problem(self):
        self.assertIsNone(send.raw_problem("/compact"))
        self.assertIsNone(send.raw_problem(" /compact"))  # stripped first
        for bad in ("hello", "/a\n/b", "/" + "x" * 500):
            self.assertTrue(send.raw_problem(bad), bad)

    def test_raw_problem_refuses_control_characters(self):  # an ESC could end the bracketed paste early
        for bad in ("/compact\x1b[201~x", "/model\tsonnet", "/a\x00", "/a\x7f"):
            self.assertTrue(send.raw_problem(bad), repr(bad))

    def test_raw_submit_stores_the_bare_text(self):
        d = self.submit(message=" /compact\n", raw=True)
        self.assertEqual(d.text, "/compact")
        self.assertTrue(d.raw)
        self.assertEqual(dispatch.outbox_path(self.cfg.state_dir, d.id).read_text(), "/compact")
        self.assertIn("直接送", (self.cfg.state_dir / "actions.log").read_text())

    def test_raw_with_a_bad_message_creates_nothing(self):
        self.assertEqual(self.submit(message="hello", raw=True), send.raw_problem("hello"))
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])

    def test_raw_to_codex_is_refused(self):
        self.assertEqual(self.submit(to="c", message="/compact", raw=True), "直接送目前只支援 Claude")
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])

    def test_batch_id_is_recorded(self):
        self.assertEqual(self.submit(batch="b1a2").batch, "b1a2")

    def test_creates_a_queued_dispatch_with_outbox_and_log(self):
        d = self.submit(message="line1\nline2", notify=True)
        self.assertIsInstance(d, dispatch.Dispatch)
        self.assertEqual(d.state, "queued")
        self.assertEqual(d.text, f"〔sampan 轉送｜來自 a（alice）｜派工 {d.id}｜直接回覆文字即可，sampan 會帶回〕\nline1\nline2")
        self.assertEqual((d.from_name, d.from_profile, d.from_agent), ("a", "alice", "claude"))
        self.assertEqual((d.to_name, d.to_agent, d.to_profile), ("b", "claude", "alice"))
        self.assertTrue(d.notify)
        self.assertEqual(d.created, "2026-10-26T12:00:00+00:00")
        self.assertEqual(dispatch.load(self.cfg.state_dir), [d])
        self.assertEqual(dispatch.outbox_path(self.cfg.state_dir, d.id).read_text(), d.text)
        log = (self.cfg.state_dir / "actions.log").read_text()
        self.assertIn("[send]", log)
        self.assertIn(d.id, log)

    def test_every_log_line_names_the_sampan_process(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        send.cancel(self.cfg, d.id, now=later(1))
        lines = [ln for ln in (self.cfg.state_dir / "actions.log").read_text().splitlines() if d.id in ln]
        self.assertGreaterEqual(len(lines), 3)  # submitted, delivered, cancelled
        by = f"〔sampan {send.RUNNING_VERSION}，pid {os.getpid()}〕"
        for ln in lines:
            self.assertTrue(ln.endswith(by), ln)

    def test_the_version_is_the_one_this_process_loaded(self):
        from sampan import update
        with mock.patch.object(update, "current_version", return_value="9.9.9"):  # upgraded on disk meanwhile
            self.assertNotEqual(send.process_tag(), f"〔sampan 9.9.9，pid {os.getpid()}〕")

    def test_terminal_caller_is_never_notified(self):
        d = self.submit(caller=send.TERMINAL, notify=True)
        self.assertEqual(d.text.splitlines()[0], f"〔sampan 轉送｜來自 終端機｜派工 {d.id}｜直接回覆文字即可，sampan 會帶回〕")
        self.assertEqual(d.from_name, "終端機")
        self.assertFalse(d.notify)

    def test_unregistered_agent_caller_is_not_the_terminal(self):
        d = self.submit(caller=Caller(None, "", "codex", None), notify=True)
        self.assertEqual(d.text.splitlines()[0], f"〔sampan 轉送｜來自 未登記的 session（codex）｜派工 {d.id}｜直接回覆文字即可，sampan 會帶回〕")
        self.assertEqual(d.from_name, "未登記的 session（codex）")
        self.assertFalse(d.notify)
        self.assertIn("未登記的 session（codex） → b", (self.cfg.state_dir / "actions.log").read_text())
        send.set_notify(self.cfg, d.id, True)  # never turned on for a caller that is not a session
        self.assertFalse(self.stored(d.id).notify)

    def test_from_kind_comes_from_the_caller(self):
        self.assertEqual(self.submit(caller=send.TERMINAL).from_kind, "terminal")
        self.assertEqual(self.submit(caller=Caller(None, "", "claude", None)).from_kind, "unregistered")
        self.assertEqual(self.submit().from_kind, "session")

    def test_a_session_named_like_the_terminal_does_not_get_terminal_notices(self):
        d = self.submit(caller=send.TERMINAL)
        send.set_notify(self.cfg, d.id, True)
        self.assertFalse(self.stored(d.id).notify)
        # a session really named 終端機 (made before names were checked) still owns its own dispatches
        s = self.submit(caller=Caller("終端機", "alice", "claude", "%9"))
        self.assertEqual(s.from_kind, "session")
        send.set_notify(self.cfg, s.id, True)
        self.assertTrue(self.stored(s.id).notify)

    def test_caller_label(self):
        self.assertEqual(send.caller_label(send.TERMINAL), "終端機")
        self.assertEqual(send.caller_label(Caller(None, "", "claude", None)), "未登記的 session（claude）")
        self.assertEqual(send.caller_label(CALLER_A), "a")

    def test_codex_target_caches_its_thread(self):
        d = self.submit(to="c")
        self.assertEqual((d.to_agent, d.thread_id), ("codex", TID_C))

    def test_refuses_self(self):  # Review Focus 5
        r = self.submit(to="a")
        self.assertIsInstance(r, str)
        self.assertIn("自己", r)
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])

    def test_refuses_unregistered_or_missing(self):  # Review Focus 5
        self.rows["u"] = status_row("u", State.UNREGISTERED, sid=SID_B, pane="%9")
        for name in ("nope", "u"):
            with self.subTest(name):
                r = self.submit(to=name)
                self.assertIsInstance(r, str)
                self.assertIn("登記表", r)
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])

    def test_refuses_target_that_is_not_running(self):  # Review Focus 5
        for state in (State.STOPPED, State.RETIRED, State.UNKNOWN, State.AWAIT_TRUST):
            with self.subTest(state):
                self.set_row("b", state=state)
                r = self.submit()
                self.assertIsInstance(r, str)
                self.assertIn("不是執行中", r)
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])

    def test_refuses_empty_message(self):
        self.assertIsInstance(self.submit(message="  \n"), str)

    def test_prunes_old_finished_dispatches(self):
        old = dataclasses.replace(self.submit(), state="done", created=(NOW - timedelta(days=8)).isoformat())
        with dispatch.transaction(self.cfg.state_dir) as items:
            items[:] = [old]
        d = self.submit()
        self.assertEqual([x.id for x in dispatch.load(self.cfg.state_dir)], [d.id])
        self.assertFalse(dispatch.outbox_path(self.cfg.state_dir, old.id).exists())


class AdvanceClaudeTest(Base):
    def test_idle_target_sent_then_done(self):
        d = self.submit()
        run = tmux_runner()
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "sent")
        self.assertEqual(d.sent_at, "2026-10-26T12:00:00+00:00")
        outbox = str(dispatch.outbox_path(self.cfg.state_dir, d.id))
        self.assertEqual(tmux_writes(run), [
            ["tmux", "-u", "load-buffer", "-b", f"sampan-{d.id}", outbox],
            ["tmux", "-u", "paste-buffer", "-p", "-d", "-b", f"sampan-{d.id}", "-t", "%2"],
            ["tmux", "-u", "send-keys", "-t", "%2", "Enter"],
        ])
        self.assertEqual(self.stored(d.id).state, "sent")

        self.transcript(SID_B, [user("old"), assistant("old reply"), user(d.text), assistant("the reply")])
        run2 = FakeRunner(strict=True)
        d = self.advance(run2, d.id, later(5))
        self.assertEqual((d.state, d.reply), ("done", "the reply"))
        self.assertEqual(d.done_at, "2026-10-26T12:00:05+00:00")
        self.assertEqual(run2.calls, [])
        self.assertEqual(self.stored(d.id), d)

    def test_message_ending_in_newline_binds_its_reply(self):
        d = self.submit(message="line one\nline two\n")
        self.assertFalse(d.text.endswith("\n"))
        self.assertEqual(dispatch.outbox_path(self.cfg.state_dir, d.id).read_text(), d.text)
        d = self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("ok")])
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).reply, "ok")

    def test_long_paste_wrapped_by_claude_binds_its_reply(self):
        d = self.submit(message="1\n2\n3\n4\n5\n")
        d = self.advance(tmux_runner(), d.id)
        wrapped = '\n\n<pasted_content id="ab12">\n' + d.text + '\n</pasted_content id="ab12">\n'
        self.transcript(SID_B, [user(wrapped), assistant("got it")])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reply), ("done", "got it"))

    def test_busy_target_stays_queued_without_any_tmux_call(self):
        d = self.submit()
        self.set_row("b", agent_status="busy")
        run = FakeRunner(strict=True)  # any tmux call would raise
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "queued")
        self.assertEqual(d.reason, "對方正在忙")
        self.assertEqual(run.calls, [])
        self.set_row("b", agent_status="idle")
        self.assertEqual(self.advance(tmux_runner(), d.id).state, "sent")

    def test_waiting_or_unknown_status_is_not_idle(self):
        d = self.submit()
        for status in ("waiting", "unknown", "-"):
            with self.subTest(status):
                self.set_row("b", agent_status=status)
                run = FakeRunner(strict=True)
                self.assertEqual(self.advance(run, d.id).state, "queued")
                self.assertEqual(run.calls, [])

    def test_draft_in_the_prompt_stays_queued(self):
        d = self.submit()
        run = tmux_runner(DRAFT)
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "queued")
        self.assertEqual(d.reason, "對方輸入框有未送出的字")
        self.assertEqual(tmux_writes(run), [])

    def test_failed_enter_after_paste_fails_and_is_never_pasted_again(self):
        d = self.submit()
        run = tmux_runner().on(["tmux", "-u", "send-keys"], cp(1, "", "can't find pane"))
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "failed")
        self.assertIn("can't find pane", d.reason)
        self.assertIn("已貼進", d.reason)
        run = tmux_runner()
        self.assertEqual(self.advance(run, d.id, later(5)).state, "failed")
        self.assertEqual(run.calls, [])
        self.assertEqual(send.cmd_cancel(self.cfg, lambda line: None, d.id), 2)

    def test_mid_turn_text_is_not_done_until_idle(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("working on it")])
        self.set_row("b", agent_status="busy")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "sent")
        self.set_row("b", agent_status="idle")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(10)).reply, "working on it")

    def test_sent_then_target_stopped_fails(self):  # Review Focus 4
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None)
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual(d.state, "failed")
        self.assertIn("停止", d.reason)
        self.assertIsNone(d.reply)

    def test_sent_then_target_forgotten_fails(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        del self.rows["b"]
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "failed")

    def test_unknown_tmux_does_not_fail(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.set_row("b", state=State.UNKNOWN, agent_status="-", pane=None)
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(60)).state, "sent")

    def test_idle_target_without_the_message_fails_with_no_reply(self):  # Review Focus 4
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        # /clear: the session now runs another conversation, whose transcript has other text only
        new_sid = "99999999-2222-3333-4444-555555555555"
        self.set_row("b", sid=new_sid)
        self.transcript(new_sid, [user("something else"), assistant("other text")])
        self.transcript(SID_B, [user(d.text), assistant("the old conversation's reply")])
        # right after the paste the transcript may lag: no verdict within the grace period
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(2)).state, "sent")
        d = self.advance(FakeRunner(strict=True), d.id, later(20))
        self.assertEqual(d.state, "sent")  # first look past the grace period: not seen yet
        d = self.advance(FakeRunner(strict=True), d.id, later(22))
        self.assertEqual(d.state, "failed")
        self.assertIn("找不到回覆", d.reason)
        self.assertIsNone(d.reply)  # never another conversation's text

    def test_message_seen_again_clears_the_not_seen_mark(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.advance(FakeRunner(strict=True), d.id, later(20))  # not seen
        self.transcript(SID_B, [user(d.text)])
        self.set_row("b", agent_status="busy")
        d = self.advance(FakeRunner(strict=True), d.id, later(22))
        self.assertEqual((d.state, d.reason), ("sent", ""))
        self.set_row("b", agent_status="idle")
        self.transcript(SID_B, [user(d.text), assistant("ok")])
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(24)).state, "done")

    def test_queued_target_stopped_stays_queued(self):
        d = self.submit()
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None)
        run = FakeRunner(strict=True)
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "queued")
        self.assertIn("沒在執行", d.reason)
        self.assertEqual(run.calls, [])

    def test_finished_dispatches_are_left_alone(self):
        d = self.submit()
        for state in ("done", "failed", "cancelled"):
            with self.subTest(state):
                with dispatch.transaction(self.cfg.state_dir) as items:
                    items[0].state = state
                self.status_calls = 0
                run = FakeRunner(strict=True)
                self.assertEqual(self.advance(run, d.id).state, state)
                self.assertEqual((run.calls, self.status_calls), ([], 0))

    def test_unknown_id_raises(self):
        with self.assertRaises(dispatch.DispatchError):
            self.advance(FakeRunner(strict=True), "zzzz")


MENU = (FIXTURES / "claude_slash_menu.ansi").read_text()
SID_NEW = "55555555-6666-7777-8888-999999999999"


def stamped(rec, seconds=1.5):
    """rec with a transcript timestamp seconds after NOW (Claude writes UTC with a Z)."""
    return {**rec, "timestamp": (NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")}


class RawCheckTest(Base):
    """A direct send: done once the command shows in the transcript (at or after sent_at) and the target is idle."""

    def sent(self, message="/compact"):
        d = self.submit(message=message, raw=True)
        run = tmux_runner()
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "sent")
        self.assertEqual([a[2] for a in tmux_writes(run)], ["load-buffer", "paste-buffer", "send-keys"])
        return d

    def test_raw_command_seen_then_idle_twice_is_done_without_text(self):
        d = self.sent()
        self.transcript(SID_B, [stamped(user("/compact")), stamped(user("<command-name>/compact</command-name>")),
                                stamped(user("<local-command-stdout>Compacted</local-command-stdout>"))])
        d = self.advance(FakeRunner(strict=True), d.id, later(20))
        self.assertEqual(d.state, "sent")
        d = self.advance(FakeRunner(strict=True), d.id, later(23))
        self.assertEqual((d.state, d.reply, d.reason), ("done", "指令已執行（沒有文字回覆）", ""))

    def test_raw_command_found_within_the_grace_waits(self):
        d = self.sent()
        self.transcript(SID_B, [stamped(user("<command-name>/compact</command-name>"))])
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(3)).state, "sent")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(6)).state, "sent")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(20)).state, "sent")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(23)).state, "done")

    def test_raw_busy_look_starts_over(self):
        d = self.sent()
        self.transcript(SID_B, [stamped(user("<command-name>/compact</command-name>"))])
        self.advance(FakeRunner(strict=True), d.id, later(20))
        self.set_row("b", agent_status="busy")
        self.advance(FakeRunner(strict=True), d.id, later(23))
        self.set_row("b", agent_status="idle")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(26)).state, "sent")

    def test_raw_command_with_text_is_done_with_that_text(self):
        d = self.sent("/x:summary")
        self.transcript(SID_B, [stamped(user("<command-name>/x:summary</command-name>")), assistant("summary saved")])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reply), ("done", "summary saved"))

    def test_raw_command_before_sent_at_is_not_ours(self):
        d = self.sent()
        self.transcript(SID_B, [stamped(user("/compact"), -5), stamped(user("<command-name>/compact</command-name>"), -5)])
        run = tmux_runner()
        self.assertEqual(self.advance(run, d.id, later(20)).reason, send.REASON_NOT_SEEN)
        d = self.advance(run, d.id, later(23))
        self.assertEqual((d.state, d.reason), ("failed", send.REASON_NOT_FOUND))
        self.assertEqual(tmux_writes(run), [])

    def test_raw_local_command_record_counts(self):
        d = self.sent("/context")
        self.transcript(SID_B, [stamped({"type": "system", "subtype": "local_command",
                                         "content": "<command-name>/context</command-name>"})])
        self.advance(FakeRunner(strict=True), d.id, later(20))
        d = self.advance(FakeRunner(strict=True), d.id, later(23))
        self.assertEqual((d.state, d.reply), ("done", send.REASON_RAN))

    def test_raw_new_conversation_id_is_done(self):  # Review Focus 3: /clear starts a new conversation
        d = self.sent("/clear")
        self.set_row("b", sid=SID_NEW)
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reply), ("done", "對方換成新對話（55555555）"))

    def test_raw_menu_on_screen_fails_without_keys(self):  # Review Focus 2: e.g. /model with no argument
        d = self.sent("/model")
        run = tmux_runner(MENU)
        self.assertEqual(self.advance(run, d.id, later(5)).state, "sent")  # within the grace: no look
        self.assertEqual(run.calls, [])
        d = self.advance(run, d.id, later(20))
        self.assertEqual(d.state, "sent")
        d = self.advance(run, d.id, later(23))
        self.assertEqual((d.state, d.reason), ("failed", "指令打開了選單，請自己到那個 session 處理"))
        self.assertTrue(run.called(["tmux", "-u", "capture-pane"]))
        self.assertEqual(tmux_writes(run), [])

    def test_raw_menu_while_not_idle_also_fails(self):  # a menu may leave the status other than idle
        d = self.sent("/model")
        self.set_row("b", agent_status="waiting")
        run = tmux_runner(MENU)
        self.advance(run, d.id, later(20))
        self.assertEqual(self.advance(run, d.id, later(23)).reason, send.REASON_MENU)
        self.assertEqual(tmux_writes(run), [])

    def test_raw_busy_screen_without_a_box_is_not_a_menu(self):  # /compact's spinner may hide the box
        d = self.sent()
        self.set_row("b", agent_status="busy")
        run = tmux_runner(MENU)
        for t in (20, 23, 26, 29):
            d = self.advance(run, d.id, later(t))
            self.assertEqual((d.state, d.reason), ("sent", ""), t)
        self.assertEqual(tmux_writes(run), [])
        self.set_row("b", agent_status="idle")  # a busy look in between started the menu count over
        self.assertEqual(self.advance(run, d.id, later(32)).state, "sent")

    def test_raw_menu_then_gone_starts_over(self):
        d = self.sent("/model")
        self.advance(tmux_runner(MENU), d.id, later(20))
        d = self.advance(tmux_runner(), d.id, later(23))  # the person closed it: one not-seen strike, not failed
        self.assertEqual((d.state, d.reason), ("sent", send.REASON_NOT_SEEN))

    def test_raw_unreadable_screen_is_not_a_menu(self):
        d = self.sent("/model")
        run = tmux_runner("")
        self.advance(run, d.id, later(20))
        self.assertNotEqual(self.advance(run, d.id, later(23)).reason, send.REASON_MENU)


class ConcurrentAdvanceTest(Base):
    def test_two_watchers_deliver_once(self):  # Review Focus 3
        d = self.submit()
        second_started = threading.Event()
        calls = []

        def slow_delivery(tmux, pane_id, outbox, did, sleep, raw=False):
            calls.append(did)
            self.assertTrue(second_started.wait(5))  # the other watcher is running before this delivery ends
            time.sleep(0.2)  # ... and has reached the lock
            return None

        results = {}

        def watcher(key):
            results[key] = send.advance(self.cfg, FakeRunner(strict=True), d.id, sleep=lambda s: None,
                                        status_fn=self.status_fn, now=NOW)

        with mock.patch.object(deliver, "to_claude", side_effect=slow_delivery):
            t1 = threading.Thread(target=watcher, args=("first",))
            t1.start()
            for _ in range(500):  # wait until the first watcher is inside the delivery
                if calls:
                    break
                time.sleep(0.01)
            t2 = threading.Thread(target=watcher, args=("second",))
            t2.start()
            second_started.set()
            t1.join(10)
            t2.join(10)
        self.assertEqual(calls, [d.id])
        self.assertEqual(results["first"].state, "sent")
        self.assertEqual(results["second"].state, "sent")
        self.assertEqual(self.stored(d.id).state, "sent")


def _bump(state_dir, did, rounds):
    """A writer in its own process: rounds transactions, each adding 1 to dispatch did's batch_skipped."""
    for _ in range(rounds):
        with dispatch.transaction(state_dir) as items:
            d = next(x for x in items if x.id == did)
            d.batch_skipped += 1


class IsolationTest(Base):
    """A change to one dispatch never touches another record, and concurrent writers never lose an update."""

    def done_and_notified(self, message):
        d = self.submit(message=message, notify=True)
        self.assertEqual(self.advance(tmux_runner(), d.id).state, "sent")
        self.recs.extend([user(self.stored(d.id).text), assistant("answer")])
        self.transcript(SID_B, self.recs)
        run = tmux_runner()
        d = self.advance(run, d.id, later(5))
        self.assertEqual((d.state, d.notified), ("done", True))
        return d

    def test_cancel_of_a_queued_dispatch_leaves_finished_ones_alone(self):
        self.recs = []
        done = [self.done_and_notified(f"m{i}") for i in range(3)]
        before = {d.id: self.stored(d.id) for d in done}
        self.set_row("b", agent_status="busy")
        q = self.submit(message="queued one", notify=True)
        self.assertEqual(self.advance(FakeRunner(strict=True), q.id).state, "queued")
        self.assertEqual(send.cancel(self.cfg, q.id, now=later(6)).state, "cancelled")
        self.set_row("b", agent_status="idle")
        self.assertEqual(send.pending(self.cfg), [])
        for d in [*done, q]:
            run = FakeRunner(strict=True)  # any tmux call would raise
            self.advance(run, d.id, later(30))
            self.assertEqual(run.calls, [])
        self.assertEqual({d.id: self.stored(d.id) for d in done}, before)
        self.assertEqual(self.stored(q.id).state, "cancelled")

    def test_concurrent_processes_do_not_lose_updates(self):
        import multiprocessing
        d1, d2 = self.submit(message="one"), self.submit(message="two")
        ctx = multiprocessing.get_context("spawn")
        rounds = 40
        procs = [ctx.Process(target=_bump, args=(self.cfg.state_dir, did, rounds)) for did in (d1.id, d2.id)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual((self.stored(d1.id).batch_skipped, self.stored(d2.id).batch_skipped), (rounds, rounds))

    def test_concurrent_threads_do_not_lose_updates(self):
        d1, d2 = self.submit(message="one"), self.submit(message="two")
        threads = [threading.Thread(target=_bump, args=(self.cfg.state_dir, did, 100)) for did in (d1.id, d2.id)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual((self.stored(d1.id).batch_skipped, self.stored(d2.id).batch_skipped), (100, 100))

    def test_cancel_racing_an_advance_of_another_dispatch(self):
        d1, d2 = self.submit(message="one"), self.submit(message="two")
        inside = threading.Event()

        def slow_delivery(tmux, pane_id, outbox, did, sleep, raw=False):
            inside.set()
            time.sleep(0.2)  # the cancel below runs now and waits for the lock
            return None

        results = {}

        def advancer():
            results["advance"] = send.advance(self.cfg, FakeRunner(strict=True), d1.id, sleep=lambda s: None,
                                              status_fn=self.status_fn, now=NOW)

        with mock.patch.object(deliver, "to_claude", side_effect=slow_delivery):
            t = threading.Thread(target=advancer)
            t.start()
            self.assertTrue(inside.wait(5))
            results["cancel"] = send.cancel(self.cfg, d2.id, now=NOW)
            t.join(10)
        self.assertEqual(results["advance"].state, "sent")
        self.assertEqual(results["cancel"].state, "cancelled")
        self.assertEqual((self.stored(d1.id).state, self.stored(d2.id).state), ("sent", "cancelled"))


class AdvanceCodexTest(Base):
    def codex_runner(self, rc=0, err=""):
        return FakeRunner(strict=True).on(["codex", "queue"], cp(rc, "", err))

    def test_queues_even_when_busy_then_done_when_idle(self):
        d = self.submit(to="c")
        self.set_row("c", agent_status="working")
        run = self.codex_runner()
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "sent")
        self.assertEqual(run.calls[0]["args"], ["codex", "queue", "--thread", TID_C, "--message", d.text])

        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_item("t1", "AgentMessage", "codex says", "final_answer"),
                             codex_done("t1", "codex says")])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))  # the rollout has task_complete: done whatever the screen says
        self.assertEqual((d.state, d.reply), ("done", "codex says"))

    def test_uses_the_current_thread_not_the_cached_one(self):  # Ruling 2
        d = self.submit(to="c")
        new_tid = "55555555-4444-5555-6666-777777777777"
        self.set_row("c", sid=new_tid)
        run = self.codex_runner()
        d = self.advance(run, d.id)
        self.assertEqual(run.calls[0]["args"][3], new_tid)
        self.assertEqual(d.thread_id, new_tid)

    def test_no_thread_yet_stays_queued(self):
        self.set_row("c", sid="")
        d = self.submit(to="c")
        run = FakeRunner(strict=True)
        d = self.advance(run, d.id)
        self.assertEqual(d.state, "queued")
        self.assertEqual(run.calls, [])

    def test_queue_failure_stays_queued_with_reason(self):
        d = self.submit(to="c")
        d = self.advance(self.codex_runner(1, "boom"), d.id)
        self.assertEqual((d.state, d.reason), ("queued", "boom"))


class NotifyTest(Base):
    def done_dispatch(self, caller=CALLER_A, notify=True):
        d = self.submit(caller=caller, notify=notify)
        self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("answer")])
        return d

    def test_idle_caller_gets_the_notice_with_enter(self):
        d = self.done_dispatch()
        run = tmux_runner()
        d = self.advance(run, d.id, later(5))
        self.assertEqual(d.state, "done")
        self.assertTrue(d.notified)
        notice = f"〔sampan：b 已回覆派工 {d.id}，用 sampan 工具 dispatch_result 看內容〕"
        writes = tmux_writes(run)
        self.assertEqual([w[2] for w in writes], ["load-buffer", "paste-buffer", "send-keys"])
        self.assertEqual(writes[1][-1], "%1")  # the caller's pane, not the target's
        self.assertEqual(writes[2][-2:], ["%1", "Enter"])
        self.assertEqual(Path(writes[0][-1]).read_text(), notice)
        self.assertTrue(self.stored(d.id).notified)

    def test_busy_caller_is_tried_again_later(self):
        d = self.done_dispatch()
        self.set_row("a", agent_status="busy")
        run = FakeRunner(strict=True)
        d = self.advance(run, d.id, later(5))
        self.assertEqual((d.state, d.notified), ("done", False))
        self.assertEqual(run.calls, [])
        self.assertIn(d.id, send.pending(self.cfg))
        self.set_row("a", agent_status="idle")
        run = tmux_runner()
        d = self.advance(run, d.id, later(10))
        self.assertTrue(d.notified)
        self.assertEqual(len(tmux_writes(run, "%1")), 2)  # paste + Enter
        self.assertNotIn(d.id, send.pending(self.cfg))

    def test_notice_pasted_without_enter_is_not_pasted_again(self):
        d = self.done_dispatch()
        run = tmux_runner().on(["tmux", "-u", "send-keys"], cp(1, "", "lost pane"))
        d = self.advance(run, d.id, later(5))
        self.assertEqual(d.state, "done")
        self.assertTrue(d.notified)
        self.assertIn("lost pane", d.reason)
        self.assertNotIn(d.id, send.pending(self.cfg))
        run = tmux_runner()
        self.advance(run, d.id, later(10))
        self.assertEqual(run.calls, [])

    def test_caller_with_a_draft_is_tried_again_later(self):
        d = self.done_dispatch()
        d = self.advance(tmux_runner(DRAFT), d.id, later(5))
        self.assertFalse(d.notified)

    def test_codex_caller_gets_codex_queue(self):
        self.rows["x"] = status_row("x", agent="codex", profile="codex", sid=TID_X, pane="%5")
        d = self.done_dispatch(caller=Caller("x", "codex", "codex", "%5"))
        run = FakeRunner(strict=True).on(["codex", "queue"], cp())
        d = self.advance(run, d.id, later(5))
        self.assertTrue(d.notified)
        self.assertEqual(run.calls[0]["args"][:4], ["codex", "queue", "--thread", TID_X])
        self.assertIn(f"已回覆派工 {d.id}", run.calls[0]["args"][5])

    def test_no_notice_when_not_asked(self):
        d = self.done_dispatch(notify=False)
        run = FakeRunner(strict=True)
        d = self.advance(run, d.id, later(5))
        self.assertEqual((d.state, d.notified), ("done", False))
        self.assertEqual(run.calls, [])

    def test_caller_gone_gives_up(self):
        d = self.done_dispatch()
        del self.rows["a"]
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertFalse(d.notify)
        self.assertNotIn(d.id, send.pending(self.cfg))


class ShellStatusTest(Base):
    """Claude reports "shell" while a background shell task runs after the turn ended: the prompt takes input."""

    def test_delivery_to_a_shell_target_pastes(self):
        d = self.submit()
        self.set_row("b", agent_status="shell")
        run = tmux_runner()
        self.assertEqual(self.advance(run, d.id).state, "sent")
        self.assertEqual([w[2] for w in tmux_writes(run, "%2")], ["paste-buffer", "send-keys"])

    def test_completion_is_judged_with_shell_looks(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.set_row("b", agent_status="shell")
        self.transcript(SID_B, [user(d.text), assistant("the reply")])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reply), ("done", "the reply"))

    def test_a_turn_without_text_fails_after_two_shell_looks(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.set_row("b", agent_status="shell")
        self.transcript(SID_B, [user(d.text)])
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(20)).state, "sent")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(22)).state, "failed")

    def test_notice_is_delivered_to_a_shell_caller(self):
        d = self.submit(notify=True)
        self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("answer")])
        self.set_row("a", agent_status="shell")
        run = tmux_runner()
        d = self.advance(run, d.id, later(5))
        self.assertTrue(d.notified)
        self.assertEqual([w[2] for w in tmux_writes(run, "%1")], ["paste-buffer", "send-keys"])


class MergedNoticeTest(Base):
    """Several notices owed to one caller at once go out as one message."""

    def setUp(self):
        super().setUp()
        self.rows["d"] = status_row("d", sid=SID_D, pane="%4")
        self.rows["z"] = status_row("z", sid=SID_Z, pane="%6")
        self.recs = {}  # transcript records per target

    def finished(self, to, message, caller=CALLER_A, reply="answer"):
        """A dispatch from caller to `to`, delivered and done (caller busy, so its notice is still owed)."""
        self.set_row(caller.name, agent_status="busy")
        d = self.submit(to=to, message=message, caller=caller, notify=True)
        self.assertEqual(self.advance(tmux_runner(), d.id).state, "sent")
        self.recs.setdefault(to, []).extend([user(self.stored(d.id).text), assistant(reply)])
        self.transcript(self.rows[to].sid, self.recs[to])
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "done")
        return d

    def failed(self, to, message, caller=CALLER_A):
        self.set_row(caller.name, agent_status="busy")
        d = self.submit(to=to, message=message, caller=caller, notify=True)
        self.assertEqual(self.advance(tmux_runner(), d.id).state, "sent")
        self.set_row(to, state=State.STOPPED, agent_status="-", pane=None)
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "failed")
        return d

    def test_three_owed_notices_go_out_as_one_paste(self):
        d1, d2 = self.finished("b", "one"), self.finished("b", "two")
        d3 = self.finished("d", "three")
        self.set_row("a", agent_status="idle")
        run = tmux_runner()
        self.advance(run, d2.id, later(10))
        writes = tmux_writes(run, "%1")
        self.assertEqual([w[2] for w in writes], ["paste-buffer", "send-keys"])
        text = Path(tmux_writes(run)[0][-1]).read_text()
        self.assertEqual(text, f"〔sampan：b 已回覆派工 {d1.id}、{d2.id}；d 已回覆派工 {d3.id}；"
                               "用 sampan 工具 dispatch_result 看內容〕")
        self.assertTrue(all(self.stored(x.id).notified for x in (d1, d2, d3)))
        self.assertEqual(send.pending(self.cfg), [])
        for x in (d1, d2, d3):  # a second advance sends nothing more
            run = FakeRunner(strict=True)
            self.advance(run, x.id, later(20))
            self.assertEqual(run.calls, [])

    def test_a_failure_is_listed_with_its_reason(self):
        d1 = self.finished("b", "one")
        d2 = self.failed("d", "two")
        self.set_row("a", agent_status="idle")
        run = tmux_runner()
        self.advance(run, d1.id, later(10))
        text = Path(tmux_writes(run)[0][-1]).read_text()
        self.assertEqual(text, f"〔sampan：b 已回覆派工 {d1.id}；給 d 的派工 {d2.id} 失敗：{send.REASON_STOPPED}；"
                               "用 sampan 工具 dispatch_result 看內容〕")
        self.assertTrue(self.stored(d2.id).notified)

    def test_one_owed_notice_keeps_the_single_text(self):
        d1 = self.finished("b", "one")
        self.set_row("a", agent_status="idle")
        run = tmux_runner()
        self.advance(run, d1.id, later(10))
        self.assertEqual(Path(tmux_writes(run)[0][-1]).read_text(), send.notice_text(self.stored(d1.id)))

    def test_two_callers_get_one_notice_each(self):
        caller_z = Caller("z", "alice", "claude", "%6")
        a1, a2 = self.finished("b", "one"), self.finished("b", "two")
        z1 = self.finished("d", "three", caller=caller_z)
        self.set_row("a", agent_status="idle")
        self.set_row("z", agent_status="idle")
        run = tmux_runner()
        self.advance(run, a1.id, later(10))
        self.assertEqual(len(tmux_writes(run, "%1")), 2)
        self.assertEqual(tmux_writes(run, "%6"), [])
        self.assertFalse(self.stored(z1.id).notified)
        self.assertTrue(self.stored(a2.id).notified)
        run = tmux_runner()
        self.advance(run, z1.id, later(11))
        self.assertEqual(len(tmux_writes(run, "%6")), 2)
        self.assertEqual(Path(tmux_writes(run)[0][-1]).read_text(), send.notice_text(self.stored(z1.id)))
        self.assertEqual(send.pending(self.cfg), [])

    def test_nothing_sent_puts_every_merged_notice_back(self):
        d1, d2 = self.finished("b", "one"), self.finished("b", "two")
        self.set_row("a", agent_status="idle")
        run = tmux_runner().on(["tmux", "-u", "load-buffer"], cp(1, "", "no buffer"))
        self.advance(run, d1.id, later(10))
        self.assertEqual([(self.stored(x.id).notified) for x in (d1, d2)], [False, False])
        self.assertEqual(sorted(send.pending(self.cfg)), sorted([d1.id, d2.id]))

    def test_unsure_delivery_counts_every_merged_notice_as_given(self):
        d1, d2 = self.finished("b", "one"), self.finished("b", "two")
        self.set_row("a", agent_status="idle")
        run = tmux_runner().on(["tmux", "-u", "send-keys"], cp(1, "", "lost pane"))
        self.advance(run, d1.id, later(10))
        for x in (d1, d2):
            self.assertTrue(self.stored(x.id).notified)
            self.assertIn("lost pane", self.stored(x.id).reason)
        run = FakeRunner(strict=True)
        self.advance(run, d2.id, later(20))
        self.assertEqual(run.calls, [])


class PendingCancelTest(Base):
    def test_pending_lists_queued_and_sent(self):
        q = self.submit()
        s = self.submit()
        self.advance(tmux_runner(), s.id)
        f = self.submit()
        send.cancel(self.cfg, f.id, now=NOW)
        self.assertEqual(sorted(send.pending(self.cfg)), sorted([q.id, s.id]))

    def test_cancel_queued_or_stop_waiting_for_sent(self):  # final review 4
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        lines = []
        self.assertEqual(send.cmd_cancel(self.cfg, lines.append, d.id), 0)
        self.assertIn(f"不再等派工 {d.id} 的回覆", lines[-1])
        self.assertEqual((self.stored(d.id).state, self.stored(d.id).reason), ("cancelled", "不再等回覆"))
        self.assertNotIn(d.id, send.pending(self.cfg))
        self.assertIn("不再等回覆", (self.cfg.state_dir / "actions.log").read_text())
        self.assertEqual(send.cmd_cancel(self.cfg, lines.append, d.id), 2)  # finished now
        self.assertIn("只能取消排隊中的", lines[-1])
        self.assertEqual(send.cmd_cancel(self.cfg, lines.append, "zzzz"), 2)
        q = self.submit()
        self.assertEqual(send.cmd_cancel(self.cfg, lines.append, q.id), 0)
        self.assertEqual(self.stored(q.id).state, "cancelled")
        self.assertIn("[cancel]", (self.cfg.state_dir / "actions.log").read_text())


class CmdSendTest(Base):
    def cmd(self, run, *, wait, timeout=600, message="hi"):
        self.lines = []
        clock = iter(range(0, 10_000, 2))
        return send.cmd_send(self.cfg, run, self.lines.append, "b", message, wait=wait, timeout=timeout,
                             sleep=self.sleeps.append, clock=lambda: next(clock), status_fn=self.status_fn,
                             now_fn=lambda: NOW)

    def test_no_wait_prints_the_id(self):
        self.assertEqual(self.cmd(tmux_runner(), wait=False), 0)
        d = dispatch.load(self.cfg.state_dir)[0]
        self.assertEqual(d.state, "sent")
        self.assertIn(d.id, self.lines[-1])

    def test_raw_creates_a_raw_dispatch(self):
        self.lines = []
        rc = send.cmd_send(self.cfg, tmux_runner(), self.lines.append, "b", "/compact", wait=False,
                           status_fn=self.status_fn, now_fn=lambda: NOW, raw=True)
        self.assertEqual(rc, 0)
        d = dispatch.load(self.cfg.state_dir)[0]
        self.assertEqual((d.raw, d.text, d.state), (True, "/compact", "sent"))
        self.assertEqual(send.cmd_send(self.cfg, FakeRunner(strict=True), self.lines.append, "b", "hi", wait=False,
                                       status_fn=self.status_fn, now_fn=lambda: NOW, raw=True), 2)
        self.assertEqual(self.lines[-1], "❌ " + send.raw_problem("hi"))

    def test_wait_prints_the_reply(self):
        def capture(args):
            d = dispatch.load(self.cfg.state_dir)[0]
            self.transcript(SID_B, [user(d.text), assistant("multi\nline reply")])
            return cp()
        run = tmux_runner().on(["tmux", "-u", "send-keys"], capture)
        self.assertEqual(self.cmd(run, wait=True), 0)
        self.assertEqual(self.lines[-1], "multi\nline reply")

    def test_wait_binds_reply_with_new_source_line(self):
        def capture(args):
            d = dispatch.load(self.cfg.state_dir)[0]
            self.assertIn("直接回覆文字即可，sampan 會帶回", d.text.splitlines()[0])
            self.transcript(SID_B, [user("別則訊息"), assistant("not mine"), user(d.text), assistant("mine")])
            return cp()
        run = tmux_runner().on(["tmux", "-u", "send-keys"], capture)
        self.assertEqual(self.cmd(run, wait=True), 0)
        self.assertEqual(self.lines[-1], "mine")

    def test_wait_timeout_is_124_and_the_dispatch_stays(self):
        self.set_row("b", agent_status="busy")
        self.assertEqual(self.cmd(FakeRunner(strict=True), wait=True, timeout=10), 124)
        d = dispatch.load(self.cfg.state_dir)[0]
        self.assertEqual(d.state, "queued")
        self.assertIn(f"還在做，派工 {d.id}", self.lines[-1])
        self.assertTrue(all(s == 2 for s in self.sleeps))

    def test_wait_failed_is_1(self):
        def stop_after_enter(args):
            self.set_row("b", state=State.STOPPED, agent_status="-", pane=None)
            return cp()
        run = tmux_runner().on(["tmux", "-u", "send-keys"], stop_after_enter)
        self.assertEqual(self.cmd(run, wait=True), 1)
        self.assertIn("停止", self.lines[-1])

    def test_refused_is_2(self):
        self.set_row("b", state=State.STOPPED)
        self.assertEqual(self.cmd(FakeRunner(strict=True), wait=False), 2)
        self.assertTrue(self.lines[-1].startswith("❌"))


class CmdDispatchesTest(Base):
    def test_lists_active_by_default_and_all_with_flag(self):
        q = self.submit()
        c = self.submit(caller=send.TERMINAL)
        send.cancel(self.cfg, c.id, now=NOW)
        lines = []
        self.assertEqual(send.cmd_dispatches(self.cfg, lines.append, all_=False), 0)
        text = "\n".join(lines)
        self.assertIn(q.id, text)
        self.assertNotIn(c.id, text)
        lines = []
        send.cmd_dispatches(self.cfg, lines.append, all_=True)
        text = "\n".join(lines)
        self.assertIn(c.id, text)
        self.assertIn("終端機", text)
        self.assertIn("已取消", text)

    def test_empty(self):
        lines = []
        self.assertEqual(send.cmd_dispatches(self.cfg, lines.append, all_=False), 0)
        self.assertIn("沒有", lines[-1])


class StartupDispatchTest(Base):
    """final review 1: a `sampan new --message` dispatch is `sent` before its session is registered or seen."""

    def startup(self, to, agent, profile, caller=CALLER_A, notify=False):
        return send.submit_new(self.cfg, caller, to, agent, profile, "開工", notify=notify, now=NOW)

    def test_claude_waits_while_stopped_with_its_tmux_then_binds_the_reply(self):
        d = self.startup("n", "claude", "alice")
        self.assertTrue(d.startup)
        # registered before launch, process not seen yet: STOPPED, but its tmux session is there
        self.rows["n"] = status_row("n", State.STOPPED, sid=SID_B, agent_status="-", tmux_alive=True)
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reason), ("sent", "對方 session 還在啟動"))
        self.rows["n"] = status_row("n", State.AWAIT_TRUST, sid=SID_B, agent_status="-")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(200)).state, "sent")  # at the trust prompt
        self.rows["n"] = status_row("n", sid=SID_B, pane="%7")  # running and idle, transcript not written yet
        d = self.advance(FakeRunner(strict=True), d.id, later(203))
        self.assertEqual((d.state, d.reason), ("sent", ""))  # the first look after starting never judges
        self.transcript(SID_B, [user(d.text), assistant("好")])
        d = self.advance(FakeRunner(strict=True), d.id, later(206))
        self.assertEqual((d.state, d.reply), ("done", "好"))

    def test_claude_stopped_after_the_grace_fails(self):
        d = self.startup("n", "claude", "alice")
        self.rows["n"] = status_row("n", State.STOPPED, sid=SID_B, agent_status="-", tmux_alive=True)
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(60)).state, "sent")
        d = self.advance(FakeRunner(strict=True), d.id, later(send.STARTUP_GRACE_S + 1))
        self.assertEqual(d.state, "failed")
        self.assertIn("停止", d.reason)

    def test_tmux_gone_fails_at_once(self):
        d = self.startup("n", "claude", "alice")
        self.rows["n"] = status_row("n", State.STOPPED, sid=SID_B, agent_status="-", tmux_alive=False)
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "failed")

    def test_codex_waits_while_not_registered_then_binds_the_reply(self):
        d = self.startup("nc", "codex", "codex")
        self.rows["nc"] = status_row("nc", State.NO_AGENT, agent="-", profile="-", sid="", agent_status="-")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(3)).reason, "對方 session 還在啟動")
        # Codex runs (maybe at a trust prompt) but has no thread, so no registry row: as long as it runs there
        self.rows["nc"] = status_row("nc", State.UNREGISTERED, agent="codex", profile="codex", sid="",
                                     agent_status="-")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(900)).state, "sent")
        self.rows["nc"] = status_row("nc", agent="codex", profile="codex", sid=TID_C, pane="%8")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text),
                             codex_item("t1", "AgentMessage", "codex 開工", "final_answer"), codex_done("t1", "codex 開工")])
        d = self.advance(FakeRunner(strict=True), d.id, later(903))
        self.assertEqual((d.state, d.reply, d.thread_id), ("done", "codex 開工", TID_C))

    def test_codex_unregistered_with_another_agent_fails(self):
        d = self.startup("nc", "codex", "codex")
        self.rows["nc"] = status_row("nc", State.UNREGISTERED, agent="claude", sid="", agent_status="-")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(3)).state, "failed")

    def test_an_ordinary_dispatch_gets_no_startup_grace(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None, tmux_alive=True)
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "failed")

    def test_abort_new_owes_no_failure_notice(self):
        d = self.startup("n", "claude", "alice", notify=True)
        self.assertTrue(d.notify)
        send.abort_new(self.cfg, d.id, "tmux session 沒開起來", now=NOW)
        stored = self.stored(d.id)
        self.assertEqual((stored.state, stored.notify), ("failed", False))
        self.assertNotIn(d.id, send.pending(self.cfg))


class TwoPhaseDeliveryTest(Base):
    """final review 2: the record says `sent` before any key is pressed, so a killed process never sends twice."""

    def test_record_is_sent_before_the_paste(self):
        d = self.submit()
        seen = []

        def paste(args):
            seen.append((self.stored(d.id).state, self.stored(d.id).reason))
            return cp()
        self.advance(tmux_runner().on(["tmux", "-u", "paste-buffer"], paste), d.id)
        self.assertEqual(seen, [("sent", "送出中")])
        self.assertEqual(self.stored(d.id).reason, "")

    def test_killed_mid_delivery_is_never_pasted_again(self):
        d = self.submit()

        def killed(*_a, **_k):
            raise KeyboardInterrupt  # the process dies between the paste and the end of the transaction
        with mock.patch.object(deliver, "to_claude", side_effect=killed):
            with self.assertRaises(KeyboardInterrupt):
                self.advance(FakeRunner(strict=True), d.id)
        stored = self.stored(d.id)
        self.assertEqual((stored.state, stored.reason), ("sent", "送出中"))
        run = tmux_runner()
        self.advance(run, d.id, later(20))
        d = self.advance(run, d.id, later(23))
        self.assertEqual(tmux_writes(run), [])  # at most once
        self.assertEqual(d.state, "failed")
        self.assertIn("找不到回覆", d.reason)

    def test_killed_mid_codex_queue_is_never_queued_again(self):
        d = self.submit(to="c")

        def killed(_args):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.advance(FakeRunner(strict=True).on(["codex", "queue"], killed), d.id)
        self.assertEqual(self.stored(d.id).state, "sent")
        run = FakeRunner(strict=True)
        self.advance(run, d.id, later(5))
        self.assertEqual(run.calls, [])

    def test_nothing_sent_goes_back_to_queued(self):
        d = self.submit()
        d = self.advance(tmux_runner(DRAFT), d.id)
        self.assertEqual((d.state, d.sent_at, d.reason), ("queued", None, "對方輸入框有未送出的字"))
        self.assertEqual(self.stored(d.id).state, "queued")

    def test_notice_is_marked_given_before_it_is_pasted(self):
        d = self.submit(notify=True)
        self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("answer")])
        seen = []

        def paste(args):
            seen.append(self.stored(d.id).notified)
            return cp()
        self.advance(tmux_runner().on(["tmux", "-u", "paste-buffer"], paste), d.id, later(5))
        self.assertEqual(seen, [True])

    def test_notice_not_sent_is_tried_again(self):
        d = self.submit(notify=True)
        self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("answer")])
        d = self.advance(tmux_runner(DRAFT), d.id, later(5))
        self.assertFalse(d.notified)
        self.assertIn(d.id, send.pending(self.cfg))


class CodexQueueTimeoutTest(Base):
    def test_timeout_fails_as_maybe_sent_and_is_never_retried(self):  # final review 3
        d = self.submit(to="c")
        d = self.advance(FakeRunner(strict=True).on(["codex", "queue"], cp(124, "", "timeout")), d.id)
        self.assertEqual(d.state, "failed")
        self.assertIn("可能已送出", d.reason)
        run = FakeRunner(strict=True)
        self.advance(run, d.id, later(5))
        self.assertEqual(run.calls, [])

    def test_notice_timeout_counts_as_given(self):
        self.rows["x"] = status_row("x", agent="codex", profile="codex", sid=TID_X, pane="%5")
        d = self.submit(caller=Caller("x", "codex", "codex", "%5"), notify=True)
        self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("answer")])
        d = self.advance(FakeRunner(strict=True).on(["codex", "queue"], cp(124, "", "timeout")), d.id, later(5))
        self.assertTrue(d.notified)
        self.assertIn("可能已送出", d.reason)
        self.assertNotIn(d.id, send.pending(self.cfg))


class StuckSentTest(Base):
    """final review 4: a bound message whose reply window closed, or a turn that ended without text, finishes."""

    def sent(self, to="b"):
        d = self.submit(to=to)
        self.advance(tmux_runner() if to == "b" else FakeRunner(strict=True).on(["codex", "queue"], cp()), d.id)
        return d

    def test_claude_next_message_typed_without_a_reply_fails(self):
        d = self.sent()
        self.transcript(SID_B, [user(d.text), user("never mind, do this"), assistant("ok, this")])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual(d.state, "failed")
        self.assertEqual(d.reason, "對方沒回這則就換了下一則")
        self.assertIsNone(d.reply)  # never the next message's reply

    def test_claude_window_closed_while_busy_waits_for_idle(self):
        d = self.sent()
        self.transcript(SID_B, [user(d.text), user("next")])
        self.set_row("b", agent_status="busy")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "sent")

    def test_claude_turn_without_text_fails_after_two_idle_looks(self):
        d = self.sent()
        self.transcript(SID_B, [user(d.text)])
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).reason, "")  # within the grace
        d = self.advance(FakeRunner(strict=True), d.id, later(20))
        self.assertEqual((d.state, d.reason), ("sent", "對方閒著，但還沒看到回覆文字"))
        self.set_row("b", agent_status="busy")
        d = self.advance(FakeRunner(strict=True), d.id, later(23))
        self.assertEqual((d.state, d.reason), ("sent", ""))  # a busy look starts over
        self.set_row("b", agent_status="idle")
        self.advance(FakeRunner(strict=True), d.id, later(26))
        d = self.advance(FakeRunner(strict=True), d.id, later(29))
        self.assertEqual((d.state, d.reason), ("failed", "對方這輪結束了，但沒有回覆文字"))

    def test_codex_turn_complete_without_text_fails(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_done("t1", None)])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reason), ("failed", "對方這輪結束了，但沒有回覆文字"))

    def test_codex_with_text_but_no_task_complete_is_not_failed_by_idle_looks(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_item("t1", "AgentMessage", "working on it", "commentary")])
        for i in range(5):  # idle looks misread mid-turn: only the rollout says the turn is over
            d = self.advance(FakeRunner(strict=True), d.id, later(20 + 3 * i))
            self.assertEqual(d.state, "sent")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_item("t1", "AgentMessage", "working on it", "commentary"),
                             codex_item("t1", "AgentMessage", "the final", "final_answer"), codex_done("t1", "the final")])
        d = self.advance(FakeRunner(strict=True), d.id, later(60))
        self.assertEqual((d.state, d.reply), ("done", "the final"))

    def test_codex_task_complete_finishes_even_while_the_screen_reads_working(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_item("t1", "AgentMessage", "the final", "final_answer"),
                             codex_done("t1", "the final")])
        self.set_row("c", agent_status="working")
        d = self.advance(FakeRunner(strict=True), d.id, later(20))
        self.assertEqual((d.state, d.reply), ("done", "the final"))

    def test_codex_turn_aborted_fails_even_while_the_screen_reads_working(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text),
                             {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "t1"}}])
        self.set_row("c", agent_status="working")
        d = self.advance(FakeRunner(strict=True), d.id, later(20))
        self.assertEqual((d.state, d.reason), ("failed", send.REASON_ABORTED))

    def test_codex_turn_aborted_fails_with_reason(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_item("t1", "AgentMessage", "half", "commentary"),
                             {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "t1", "reason": "interrupted"}}])
        d = self.advance(FakeRunner(strict=True), d.id, later(20))
        self.assertEqual((d.state, d.reason), ("failed", send.REASON_ABORTED))
        self.assertEqual(send.REASON_ABORTED, "對方這輪被中斷了")

    def test_codex_task_complete_without_text_still_fails_no_text(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_done("t1", None)])
        d = self.advance(FakeRunner(strict=True), d.id, later(20))
        self.assertEqual((d.state, d.reason), ("failed", send.REASON_NO_TEXT))

    def test_codex_interrupted_then_next_message_fails(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_item("t2", "UserMessage", "other"),
                             codex_item("t2", "AgentMessage", "for other", "final_answer"), codex_done("t2", "for other")])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reason, d.reply), ("failed", "對方沒回這則就換了下一則", None))

    def test_codex_interrupted_with_partial_text_is_done_with_it(self):
        d = self.sent("c")
        self.rollout(TID_C, [codex_item("t1", "UserMessage", d.text), codex_item("t1", "AgentMessage", "partial"),
                             codex_item("t2", "UserMessage", "other")])
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reply), ("done", "partial"))


class NotSeenBusyTest(Base):
    def test_codex_busy_look_clears_the_not_seen_mark(self):  # final review 5
        d = self.submit(to="c")
        self.advance(FakeRunner(strict=True).on(["codex", "queue"], cp()), d.id)
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(20)).reason, send.REASON_NOT_SEEN)
        self.set_row("c", agent_status="working")  # it took the queued message and is working on it
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(23)).reason, "")
        self.set_row("c", agent_status="idle")
        d = self.advance(FakeRunner(strict=True), d.id, later(26))
        self.assertEqual((d.state, d.reason), ("sent", send.REASON_NOT_SEEN))  # one strike again, not failed

    def test_claude_busy_look_clears_the_not_seen_mark(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.advance(FakeRunner(strict=True), d.id, later(20))
        self.set_row("b", agent_status="busy")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(23)).reason, "")
        self.set_row("b", agent_status="idle")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(26)).state, "sent")


class FailureNoticeTest(Base):
    """final review 6: a failed dispatch tells its caller too, through the same once-only path."""

    def failed(self, notify=True):
        d = self.submit(notify=notify)
        self.advance(tmux_runner(), d.id)
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None)
        self.set_row("a", agent_status="busy")
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual(d.state, "failed")
        self.set_row("a", agent_status="idle")
        return d

    def test_idle_caller_gets_the_failure_notice_with_enter(self):
        d = self.failed()
        self.assertIn(d.id, send.pending(self.cfg))
        run = tmux_runner()
        d = self.advance(run, d.id, later(10))
        self.assertTrue(d.notified)
        writes = tmux_writes(run)
        self.assertEqual(writes[2][-2:], ["%1", "Enter"])
        self.assertEqual(Path(writes[0][-1]).read_text(), f"〔sampan：給 b 的派工 {d.id} 失敗：對方 session 已停止〕")
        self.assertEqual(d.reason, "對方 session 已停止")
        self.assertNotIn(d.id, send.pending(self.cfg))
        run = tmux_runner()
        self.advance(run, d.id, later(15))
        self.assertEqual(run.calls, [])  # once only

    def test_pasted_failure_notice_keeps_the_failure_reason(self):
        d = self.failed()
        d = self.advance(tmux_runner().on(["tmux", "-u", "send-keys"], cp(1, "", "lost pane")), d.id, later(10))
        self.assertTrue(d.notified)
        self.assertTrue(d.reason.startswith("對方 session 已停止；完成通知"))

    def test_no_notice_when_not_asked(self):
        d = self.failed(notify=False)
        self.assertNotIn(d.id, send.pending(self.cfg))

    def test_failure_notice_text_is_one_line(self):
        d = dataclasses.replace(self.submit(), state="failed", reason="a\nb")
        self.assertEqual(send.notice_text(d), f"〔sampan：給 b 的派工 {d.id} 失敗：a b〕")


class QueueLimitTest(Base):
    def test_queued_too_long_fails_with_its_last_reason_and_tells_the_caller(self):
        d = self.submit(notify=True)
        run = tmux_runner(DRAFT)
        self.assertEqual(self.advance(run, d.id, later(60)).state, "queued")
        d = self.advance(tmux_runner(DRAFT), d.id, later(send.QUEUE_LIMIT_S + 1))
        self.assertEqual(d.state, "failed")
        self.assertEqual(d.reason, "排隊 30 分鐘還送不出去（最後一次：對方輸入框有未送出的字）")
        self.assertIn(d.id, send.pending(self.cfg))  # the caller is owed the failure notice

    def test_ready_at_the_limit_is_still_sent(self):
        d = self.submit()
        d = self.advance(tmux_runner(), d.id, later(send.QUEUE_LIMIT_S + 1))
        self.assertEqual(d.state, "sent")

    def test_waiting_for_a_busy_or_stopped_target_has_no_limit(self):
        d = self.submit()
        self.set_row("b", agent_status="busy")
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(send.QUEUE_LIMIT_S * 4)).state, "queued")
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None)
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(send.QUEUE_LIMIT_S * 5)).state, "queued")

    def test_a_sent_dispatch_waiting_for_its_reply_has_no_limit(self):
        d = self.submit()
        self.advance(tmux_runner(), d.id)
        self.set_row("b", agent_status="busy")
        d = self.advance(FakeRunner(strict=True), d.id, later(send.QUEUE_LIMIT_S * 4))
        self.assertEqual(d.state, "sent")


class CopyModeTest(Base):
    def test_target_in_copy_mode_stays_queued_and_gets_nothing(self):  # final review 8
        d = self.submit()
        run = tmux_runner().on(["tmux", "-u", "display-message"], cp(0, "1\n"))
        d = self.advance(run, d.id)
        self.assertEqual((d.state, d.reason), ("queued", "對方畫面在捲動模式（tmux copy mode）"))
        self.assertEqual(tmux_writes(run), [])
        self.assertEqual(self.advance(tmux_runner(), d.id).state, "sent")


class CmdShowTest(Base):
    """final review 7: `sampan dispatches show <id> [--wait N]`."""

    def show(self, did, run=None, wait=0):
        self.lines = []
        clock = iter(range(0, 10_000, 2))
        return send.cmd_show(self.cfg, run or FakeRunner(strict=True), self.lines.append, did, wait=wait,
                             sleep=self.sleeps.append, clock=lambda: next(clock), status_fn=self.status_fn,
                             now_fn=lambda: later(5))

    def test_done_prints_the_full_reply(self):
        d = self.submit(caller=send.TERMINAL)
        self.advance(tmux_runner(), d.id)
        self.transcript(SID_B, [user(d.text), assistant("第一行\n第二行")])
        self.assertEqual(self.show(d.id), 0)  # advanced once: done now
        self.assertEqual(self.lines[0], f"派工 {d.id}：已回覆")
        self.assertIn("從 終端機 給 b（claude）", self.lines[1])
        self.assertEqual(self.lines[-2:], ["──── 回覆 ────", "第一行\n第二行"])

    def test_queued_without_wait_is_0_with_the_reason(self):
        self.set_row("b", agent_status="busy")
        d = self.submit()
        self.assertEqual(self.show(d.id), 0)
        self.assertIn("說明：對方正在忙", self.lines)

    def test_wait_timeout_is_124(self):
        self.set_row("b", agent_status="busy")
        d = self.submit()
        self.assertEqual(self.show(d.id, wait=6), 124)
        self.assertTrue(all(x == 2 for x in self.sleeps) and self.sleeps)

    def test_failed_is_1_and_unknown_is_2(self):
        d = self.submit()
        send.cancel(self.cfg, d.id, now=NOW)
        self.assertEqual(self.show(d.id), 1)
        self.assertIn("說明：已取消", self.lines)
        self.assertEqual(self.show("zzzz"), 2)
        self.assertTrue(self.lines[-1].startswith("❌"))


class CommandCallerTest(Base):
    """final review 9: who runs `sampan send` / `sampan new --message`."""

    PROCS = ["{:>6} {:>6} S launchd launchd".format(1, 0), "{:>6} {:>6} S zsh -zsh".format(100, 1),
             "{:>6} {:>6} S claude claude".format(200, 100), "{:>6} {:>6} S zsh /bin/zsh -c sampan".format(250, 200),
             "{:>6} {:>6} S sampan sampan send b hi".format(300, 250)]

    def runner(self, procs, panes_rc=0):
        from tests.fakes import pane_line
        r = FakeRunner(strict=True)
        r.on(["ps", "-A"], cp(out="\n".join(procs) + "\n"))
        r.on(["tmux", "-u", "list-panes"], cp(panes_rc, pane_line("a", "%1", 100, "claude", "/w") + "\n",
                                              "" if panes_rc == 0 else "permission denied"))
        return r

    def test_an_agent_running_send_through_bash_is_its_session(self):
        from sampan.registry import Row, write_rows
        write_rows(self.cfg.registry_path, [Row("a", "claude", "alice", SID_A, "/w")], "t", self.cfg.history_dir)
        c = send.command_caller(self.cfg, self.runner(self.PROCS), 300)
        self.assertEqual(c, Caller("a", "alice", "claude", "%1"))

    def test_no_row_is_unregistered_and_no_agent_is_the_terminal(self):
        self.assertEqual(send.caller_kind(send.command_caller(self.cfg, self.runner(self.PROCS), 300)), "unregistered")
        person = ["{:>6} {:>6} S zsh -zsh".format(100, 1), "{:>6} {:>6} S sampan sampan send".format(300, 100)]
        self.assertIs(send.command_caller(self.cfg, self.runner(person), 300), send.TERMINAL)

    def test_unsure_lookup_is_unregistered_never_the_terminal(self):
        c = send.command_caller(self.cfg, self.runner(self.PROCS, panes_rc=1), 300)
        self.assertEqual((c.agent, send.caller_kind(c)), ("claude", "unregistered"))
        c = send.command_caller(self.cfg, FakeRunner(strict=True).on(["ps", "-A"], cp(1)), 300)
        self.assertEqual((send.caller_label(c), send.caller_kind(c)), ("未登記的 session", "unregistered"))

    def test_cmd_send_labels_the_agent_session_and_refuses_itself(self):
        lines = []
        rc = send.cmd_send(self.cfg, tmux_runner(), lines.append, "b", "hi", wait=False, caller=CALLER_A,
                           sleep=self.sleeps.append, status_fn=self.status_fn, now_fn=lambda: NOW)
        self.assertEqual(rc, 0)
        [d] = dispatch.load(self.cfg.state_dir)
        self.assertEqual((d.from_name, d.from_kind), ("a", "session"))
        self.assertTrue(d.text.startswith(f"〔sampan 轉送｜來自 a（alice）｜派工 {d.id}｜直接回覆文字即可，sampan 會帶回〕"))
        rc = send.cmd_send(self.cfg, FakeRunner(strict=True), lines.append, "a", "hi", wait=True, caller=CALLER_A,
                           sleep=self.sleeps.append, status_fn=self.status_fn, now_fn=lambda: NOW)
        self.assertEqual(rc, 2)  # an agent sending to its own session with --wait would wait for itself forever
        self.assertIn("不能送給自己", lines[-1])

    def test_cmd_send_unregistered_label(self):
        lines = []
        send.cmd_send(self.cfg, tmux_runner(), lines.append, "b", "hi", wait=False,
                      caller=Caller(None, "", "codex", None), sleep=self.sleeps.append, status_fn=self.status_fn,
                      now_fn=lambda: NOW)
        [d] = dispatch.load(self.cfg.state_dir)
        self.assertEqual((d.from_name, d.from_kind), ("未登記的 session（codex）", "unregistered"))


class DispatchesTableTest(Base):
    def test_columns_are_padded_by_display_width(self):  # final review minor
        self.rows["中文名稱"] = status_row("中文名稱", sid=SID_B, pane="%9", agent_status="busy")
        self.submit(to="中文名稱")
        self.set_row("b", agent_status="busy")
        self.submit(to="b")
        lines = []
        send.cmd_dispatches(self.cfg, lines.append, all_=False)
        from sampan.textwidth import text_width
        status_col = [text_width(line.split("排隊中")[0]) for line in lines[1:]]
        self.assertEqual(len(set(status_col)), 1)  # the state column starts at the same cell in every row
        self.assertEqual(text_width(lines[0].split("狀態")[0]), status_col[0])


if __name__ == "__main__":
    unittest.main()


SID_ME = "66666666-7777-8888-9999-aaaaaaaaaaaa"  # Claude caller "me" of a batch
SID_C2 = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"  # Claude target "c" of a batch
CALLER_ME = Caller("me", "alice", "claude", "%9")


class BatchBase(Base):
    def setUp(self):
        super().setUp()
        self.rows = {
            "me": status_row("me", sid=SID_ME, pane="%9"),
            "a": status_row("a", sid=SID_A, pane="%1"),
            "b": status_row("b", sid=SID_B, pane="%2", agent_status="busy"),
            "c": status_row("c", sid=SID_C2, pane="%3"),
            "x": status_row("x", agent="codex", profile="codex", sid=TID_C, pane="%4"),
        }

    def batch(self, names, message="hello", *, notify=True, raw=False, caller=CALLER_ME):
        return send.submit_batch(self.cfg, FakeRunner(strict=True), caller, names, message, raw=raw, notify=notify,
                                 now=NOW, status_fn=self.status_fn)

    def sent(self, names=("a", "b", "c")):
        """A batch to a, b (busy), c with a and c delivered."""
        b = self.batch(list(names))
        for d in b.items:
            self.assertEqual(self.advance(tmux_runner(), d.id).state, "sent")
        return b


class BatchTest(BatchBase):
    def test_batch_sends_to_idle_and_skips_busy(self):
        b = self.batch(["a", "b", "c"])
        self.assertIsInstance(b, send.Batch)
        self.assertRegex(b.id, r"^b[0-9a-f]{4}$")
        self.assertEqual([d.to_name for d in b.items], ["a", "c"])
        self.assertTrue(all(d.state in ("queued", "sent") for d in b.items))
        self.assertEqual(b.skipped, [("b", "對方正在忙")])
        stored = dispatch.load(self.cfg.state_dir)
        self.assertEqual([(d.to_name, d.batch) for d in stored], [("a", b.id), ("c", b.id)])
        self.assertEqual([d.batch_skipped for d in stored], [1, 0])  # only on the first item
        self.assertEqual(send.batch_items(self.cfg, b.id), stored)

    def test_batch_sends_to_a_shell_session(self):  # a background shell task runs, the prompt takes input
        self.set_row("c", agent_status="shell")
        b = self.batch(["a", "c"])
        self.assertEqual([d.to_name for d in b.items], ["a", "c"])
        self.assertEqual(b.skipped, [])

    def test_batch_skips_stopped_and_unknown_with_reasons(self):
        self.rows["s"] = status_row("s", State.STOPPED, sid="s")
        self.rows["w"] = status_row("w", sid="w", pane="%6", agent_status="waiting")
        b = self.batch(["a", "s", "nope", "me", "w", "a"])
        self.assertEqual([d.to_name for d in b.items], ["a"])  # a name given twice is sent once
        self.assertEqual(b.skipped, [
            ("s", send.target_problem(self.rows["s"], "s")),
            ("nope", send.target_problem(None, "nope")),
            ("me", "不能送給自己（me）"),
            ("w", "對方在等使用者回應"),
        ])

    def test_batch_raw_skips_codex(self):
        b = self.batch(["a", "x"], "/compact", raw=True)
        self.assertEqual(b.skipped, [("x", "直接送目前只支援 Claude")])
        self.assertEqual([(d.to_name, d.raw, d.text) for d in b.items], [("a", True, "/compact")])
        self.assertEqual(self.batch(["a"], "hello", raw=True), send.raw_problem("hello"))

    def test_batch_with_nobody_to_send_returns_why(self):
        r = self.batch(["b", "nope"])
        self.assertIsInstance(r, str)
        self.assertIn("對方正在忙", r)
        self.assertIn("nope", r)
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])
        self.assertEqual(self.batch(["a"], "  "), "訊息是空的")

    def test_batch_notice_once_after_all_finish(self):
        b = self.sent()
        a, c = b.items
        self.transcript(SID_A, [user(self.stored(a.id).text), assistant("from a")])
        run = FakeRunner(strict=True)
        self.assertEqual(self.advance(run, a.id, later(5)).state, "done")
        self.assertEqual(run.calls, [])  # c is not done yet: no notice
        self.assertNotIn(a.id, send.pending(self.cfg))
        self.transcript(SID_C2, [user(self.stored(c.id).text), assistant("from c")])
        run = tmux_runner()
        self.assertEqual(self.advance(run, c.id, later(10)).state, "done")
        writes = tmux_writes(run)
        self.assertEqual([w[2] for w in writes], ["load-buffer", "paste-buffer", "send-keys"])
        self.assertEqual(writes[2][-2:], ["%9", "Enter"])
        items = send.batch_items(self.cfg, b.id)
        text = send.batch_notice_text(b.id, items, 1)
        self.assertEqual(text, f"〔sampan：批次 {b.id} 完成：2 完成、0 失敗、1 跳過，用 dispatch_result 看〕")
        self.assertEqual(Path(writes[0][-1]).read_text(), text)
        self.assertTrue(all(d.notified for d in items))
        self.assertEqual(send.pending(self.cfg), [])
        for d in items:
            run = FakeRunner(strict=True)
            self.advance(run, d.id, later(20))
            self.assertEqual(run.calls, [])  # once only

    def test_batch_notice_is_merged_with_a_plain_one(self):
        self.rows["me"] = dataclasses.replace(self.rows["me"], agent_status="busy")
        b = self.sent(("a", "c"))
        p = send.submit(self.cfg, FakeRunner(strict=True), CALLER_ME, "a", "plain", notify=True, now=NOW,
                        status_fn=self.status_fn)
        a, c = b.items
        self.transcript(SID_A, [user(self.stored(a.id).text), assistant("from a"),
                                user(self.stored(p.id).text), assistant("p")])
        self.transcript(SID_C2, [user(self.stored(c.id).text), assistant("from c")])
        self.assertEqual(self.advance(tmux_runner(), p.id).state, "sent")
        for x in (a.id, c.id, p.id):
            self.assertEqual(self.advance(FakeRunner(strict=True), x, later(5)).state, "done")
        self.set_row("me", agent_status="idle")
        run = tmux_runner()
        self.advance(run, p.id, later(10))
        self.assertEqual(len(tmux_writes(run, "%9")), 2)  # one paste + Enter
        self.assertEqual(Path(tmux_writes(run)[0][-1]).read_text(),
                         f"〔sampan：a 已回覆派工 {p.id}；批次 {b.id} 完成：2 完成、0 失敗、0 跳過；"
                         "用 sampan 工具 dispatch_result 看內容〕")
        self.assertTrue(all(d.notified for d in dispatch.load(self.cfg.state_dir)))
        self.assertEqual(send.pending(self.cfg), [])
        for d in dispatch.load(self.cfg.state_dir):
            run = FakeRunner(strict=True)
            self.advance(run, d.id, later(20))
            self.assertEqual(run.calls, [])

    def test_batch_finishes_with_a_cancelled_item(self):  # Review Focus 4
        b = self.sent()
        a, c = b.items
        self.assertEqual(send.cancel(self.cfg, c.id, now=later(1)).state, "cancelled")
        self.transcript(SID_A, [user(self.stored(a.id).text), assistant("from a")])
        run = tmux_runner()
        self.advance(run, a.id, later(5))
        writes = tmux_writes(run, "%9")
        self.assertEqual(len(writes), 2)  # paste + Enter
        notice = Path(tmux_writes(run)[0][-1]).read_text()
        self.assertEqual(notice, f"〔sampan：批次 {b.id} 完成：1 完成、1 失敗、1 跳過，用 dispatch_result 看〕")
        self.assertEqual(send.pending(self.cfg), [])

    def test_batch_whose_last_item_is_cancelled_is_noticed_by_the_watcher(self):
        b = self.sent()
        a, c = b.items
        self.transcript(SID_A, [user(self.stored(a.id).text), assistant("from a")])
        self.advance(FakeRunner(strict=True), a.id, later(5))
        send.cancel(self.cfg, c.id, now=later(6))
        self.assertEqual(send.pending(self.cfg), [a.id])  # the first item owes the batch notice
        run = tmux_runner()
        self.advance(run, a.id, later(7))
        self.assertEqual(len(tmux_writes(run, "%9")), 2)
        self.assertEqual(send.pending(self.cfg), [])

    def test_batch_without_notify_never_tells(self):
        b = self.batch(["a"], notify=False)
        self.advance(tmux_runner(), b.items[0].id)
        self.transcript(SID_A, [user(self.stored(b.items[0].id).text), assistant("from a")])
        run = FakeRunner(strict=True)
        self.advance(run, b.items[0].id, later(5))
        self.assertEqual(run.calls, [])
        self.assertEqual(send.pending(self.cfg), [])

    def test_set_notify_takes_a_batch_id(self):
        b = self.batch(["a", "c"], notify=False)
        send.set_notify(self.cfg, b.id, True)
        self.assertTrue(all(d.notify for d in send.batch_items(self.cfg, b.id)))

    def test_is_batch_id(self):
        self.assertTrue(send.is_batch_id("b0f3a"))
        for s in ("b0f3", "beef", "bxyzw", "a0f3a", "b0f3a1"):
            self.assertFalse(send.is_batch_id(s), s)


class CmdSendBatchTest(BatchBase):
    def cmd(self, run, names, *, wait, timeout=600, message="hi"):
        self.lines = []
        clock = iter(range(0, 10_000, 2))
        return send.cmd_send_batch(self.cfg, run, self.lines.append, names, message, wait=wait, timeout=timeout,
                                   sleep=self.sleeps.append, clock=lambda: next(clock), status_fn=self.status_fn,
                                   now_fn=lambda: NOW, caller=CALLER_ME)

    def test_no_wait_prints_the_batch_and_the_skipped(self):
        self.assertEqual(self.cmd(tmux_runner(), ["a", "b"], wait=False), 0)
        [d] = dispatch.load(self.cfg.state_dir)
        self.assertEqual(d.state, "sent")
        text = "\n".join(self.lines)
        self.assertIn(d.batch, text)
        self.assertIn(d.id, text)
        self.assertIn("b：對方正在忙", text)

    def test_wait_prints_each_reply(self):
        def capture(args):
            for d in dispatch.load(self.cfg.state_dir):
                sid = SID_A if d.to_name == "a" else SID_C2
                self.transcript(sid, [user(d.text), assistant(f"reply of {d.to_name}")])
            return cp()
        run = tmux_runner().on(["tmux", "-u", "send-keys"], capture)
        self.assertEqual(self.cmd(run, ["a", "c"], wait=True), 0)
        text = "\n".join(self.lines)
        self.assertIn("reply of a", text)
        self.assertIn("reply of c", text)

    def test_wait_timeout_is_124(self):
        self.set_row("a", agent_status="idle")
        self.assertEqual(self.cmd(tmux_runner(), ["a"], wait=True, timeout=10), 124)

    def test_nobody_to_send_is_2(self):
        self.assertEqual(self.cmd(FakeRunner(strict=True), ["b"], wait=False), 2)
        self.assertTrue(self.lines[-1].startswith("❌"))
