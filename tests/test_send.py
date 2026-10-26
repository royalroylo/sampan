"""Sending: submit a dispatch, advance it (queued → sent → done / failed), notify the caller, the send / dispatches commands."""
import dataclasses
import json
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

    def submit(self, to="b", message="hello", caller=CALLER_A, notify=False, run=None):
        return send.submit(self.cfg, run or FakeRunner(strict=True), caller, to, message, notify=notify, now=NOW,
                           status_fn=self.status_fn)

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


class ConcurrentAdvanceTest(Base):
    def test_two_watchers_deliver_once(self):  # Review Focus 3
        d = self.submit()
        second_started = threading.Event()
        calls = []

        def slow_delivery(tmux, pane_id, outbox, did, sleep):
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
        self.assertEqual(self.advance(FakeRunner(strict=True), d.id, later(5)).state, "sent")  # still working
        self.set_row("c", agent_status="idle")
        d = self.advance(FakeRunner(strict=True), d.id, later(10))
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
