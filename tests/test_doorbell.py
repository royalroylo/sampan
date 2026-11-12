"""The doorbell: one short line that tells an idle Claude with a live sampan MCP it has mail; no answer falls back to
pasting the full text. Also claude_state.after_marker, which reads the turn after a marker in a transcript."""
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from sampan import deliver, deliveries, dispatch, doorbell, send, store
from sampan.claude_state import after_marker
from sampan.screens import claude_prompt_text
from tests import test_send as ts
from tests.fakes import FakeRunner, cp

NOW, ID_B, SID_B = ts.NOW, ts.ID_B, ts.SID_B
STARTED = "2026-10-26T08:00:00"
PS_LSTART = "Mon Oct 26 08:00:00 2026\n"  # what `ps -o lstart=` prints for STARTED
ID_C = "dddddddd-0000-4000-8000-000000000004"
WAITING = "  Do you want to proceed?\n  sampan - inbox_take (MCP) mcp__sampan__inbox_take\n  ❯ 1. Yes\n    2. No\n"


def later(seconds):
    return NOW + timedelta(seconds=seconds)


def runner(screen=ts.EMPTY):
    run = ts.tmux_runner(screen)
    run.on(["ps", "-o", "lstart="], cp(0, PS_LSTART))
    return run


def ring_screen(text):
    """The draft fixture with text typed on the prompt line instead of its draft."""
    return ts.DRAFT.replace(claude_prompt_text(ts.DRAFT).strip(), text)


def tool_use(name, uid="t1"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": uid, "name": name, "input": {}}]}}


def tool_result(text, uid="t1", error=False):
    part = {"type": "tool_result", "tool_use_id": uid, "content": text}
    if error:
        part["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [part]}}


INTERRUPTED = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "[Request interrupted by user]"}]}}
USER_PASTE = "[Pasted text #1 +12 lines]"  # how Claude shows a user's own multi-line paste on the prompt line


class DoorbellBase(ts.Base):
    def submit_at(self, seconds, to="b", message="hello", caller=ts.CALLER_A, raw=False):
        return send.submit(self.cfg, FakeRunner(strict=True), caller, to, message, notify=False, now=later(seconds),
                           status_fn=self.status_fn, raw=raw)

    def heartbeat(self, session_id=ID_B, conversation=SID_B, agent="claude", at=NOW):
        with dispatch.transaction(self.cfg.state_dir) as items:
            items.conn.execute("INSERT OR REPLACE INTO inboxes (session_id, nonce, agent, conversation, pid, started, version, at) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (session_id, f"n-{session_id}", agent, conversation, os.getpid(), STARTED, "0.17.0",
                                at.isoformat(timespec="seconds")))

    def three(self):
        return [self.submit_at(0, message="from a"), self.submit_at(1, message="from terminal", caller=send.TERMINAL),
                self.submit_at(2, message="from z", caller=ts.CALLER_Z)]

    def rings(self):
        conn = store.connect(self.cfg.state_dir)
        self.addCleanup(conn.close)
        return [dict(r) for r in conn.execute("SELECT * FROM rings ORDER BY rowid")]

    def log(self):
        path = self.cfg.state_dir / "actions.log"
        return path.read_text() if path.exists() else ""

    def ring(self, *msgs, at=3):
        """Ring for the messages queued to b; returns the ring's row."""
        self.heartbeat(at=later(at))
        self.advance(runner(), msgs[0].id, later(at))
        return self.rings()[-1]


class FixRoundOneTest(DoorbellBase):
    def ring_line(self, ring):
        return dispatch.outbox_path(self.cfg.state_dir, f"ring-{ring['id']}").read_text()

    def test_interrupted_ring_turn_then_a_typed_message_is_no_answer(self):
        a = self.submit_at(0)
        ring = self.ring(a)
        self.transcript(SID_B, [ts.user(self.ring_line(ring)), INTERRUPTED, ts.user("something else"),
                                ts.assistant("other answer")])
        got = self.advance(runner(), a.id, later(40))
        self.assertEqual((self.rings()[0]["result"], got.state), ("no_answer", "sent"))

    def test_pending_tool_then_a_typed_message_is_no_answer(self):
        a = self.submit_at(0)
        ring = self.ring(a)
        self.transcript(SID_B, [ts.user(self.ring_line(ring)), tool_use("Bash"), ts.user("never mind")])
        self.advance(runner(), a.id, later(40))
        self.assertEqual(self.rings()[0]["result"], "no_answer")

    def test_ended_turn_with_a_busy_target_is_not_no_answer(self):
        a = self.submit_at(0)
        ring = self.ring(a)
        self.transcript(SID_B, [ts.user(self.ring_line(ring)), ts.assistant("not now")])
        self.set_row("b", agent_status="busy")
        got = self.advance(FakeRunner(strict=True), a.id, later(40))
        self.assertEqual((got.status, self.rings()[0]["result"]), ("rung", "ringing"))

    def test_user_paste_draft_never_gets_enter_from_a_new_ring(self):
        a = self.submit_at(0)
        self.heartbeat(at=later(1))
        run = runner(ring_screen(USER_PASTE))
        got = self.advance(run, a.id, later(1))
        self.assertEqual(run.sent_keys(), [])
        self.assertEqual((got.status, got.reason, self.rings()[0]["result"]), ("", deliver.REASON_DRAFT, "paste_failed"))

    def test_user_paste_draft_never_gets_enter_from_settle(self):
        a = self.submit_at(0)
        self.ring(a)  # never reaches the transcript
        run = runner(ring_screen(USER_PASTE))
        got = self.advance(run, a.id, later(40))
        self.assertEqual(run.sent_keys(), [])
        self.assertEqual(self.rings()[0]["result"], "no_answer")
        self.assertEqual((got.state, got.reason), ("queued", deliver.REASON_DRAFT))  # the paste waits for an empty box

    def test_ringing_too_long_with_an_idle_target_is_no_answer(self):
        a = self.submit_at(0)
        ring = self.ring(a)
        self.transcript(SID_B, [ts.user(self.ring_line(ring)), tool_use("Bash")])  # looks mid-turn forever
        self.heartbeat(at=later(doorbell.RING_LIMIT_S + 10))
        self.assertEqual(self.advance(runner(), a.id, later(doorbell.RING_LIMIT_S - 10)).status, "rung")
        got = self.advance(runner(), a.id, later(doorbell.RING_LIMIT_S + 10))
        self.assertEqual((self.rings()[0]["result"], got.state, got.reason),
                         ("no_answer", "sent", doorbell.REASON_NO_ANSWER))

    def test_ringing_too_long_with_a_stopped_target_goes_back_in_line(self):
        a = self.submit_at(0)
        self.ring(a)
        self.set_row("b", state=ts.State.STOPPED)
        self.assertEqual(self.advance(FakeRunner(strict=True), a.id, later(60)).status, "rung")
        got = self.advance(FakeRunner(strict=True), a.id, later(doorbell.RING_LIMIT_S + 10))
        self.assertEqual((got.state, got.status, got.reason), ("queued", "", send.REASON_NOT_RUNNING))
        self.assertEqual(self.rings()[0]["result"], "no_answer")


class RingTest(DoorbellBase):
    def test_ring_text(self):
        self.assertEqual(
            doorbell.ring_text("r3f9", [("alpha-pm", 2), ("beta-dev", 1)], 3),
            "〔sampan 門鈴 r3f9〕你有 3 則新訊息（來自 alpha-pm 2、beta-dev 1）。請呼叫 mcp__sampan__inbox_take 一次取完、"
            "全部看完再動手；每則做完用 mcp__sampan__reply 回一次（那是最終回覆，不要用來報進度）。工具沒載入時先用 ToolSearch 找 sampan。")

    def test_rings_once_for_all_queued_senders(self):
        a, t, z = self.three()
        self.heartbeat(at=later(3))
        run = runner()
        got = self.advance(run, z.id, later(3))
        rings = self.rings()
        self.assertEqual(len(rings), 1)
        code = rings[0]["id"]
        self.assertEqual((rings[0]["result"], rings[0]["to_id"], rings[0]["conversation"]), ("ringing", ID_B, SID_B))
        self.assertEqual(json.loads(rings[0]["messages"]), [a.id, t.id, z.id])
        writes = ts.tmux_writes(run)
        self.assertEqual([w[2] for w in writes], ["load-buffer", "paste-buffer", "send-keys"])
        outbox = dispatch.outbox_path(self.cfg.state_dir, f"ring-{code}")
        self.assertEqual(writes[0], ["tmux", "-u", "load-buffer", "-b", f"sampan-ring-{code}", str(outbox)])
        self.assertEqual(outbox.read_text(), doorbell.ring_text(code, [("a", 1), ("終端機", 1), ("z", 1)], 3))
        hhmm = later(3).astimezone().strftime("%H:%M")
        for x in (a, t, z):
            s = self.stored(x.id)
            self.assertEqual((s.state, s.status, s.reason), ("queued", "rung", f"已按門鈴，等對方取信（{code}，{hhmm}）"))
        self.assertEqual((got.state, got.status), ("queued", "rung"))
        conn = store.connect(self.cfg.state_dir)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertIn(f"按門鈴 {code}", self.log())

    def test_a_ringing_ring_waits_and_rings_no_more(self):
        a, t, z = self.three()
        self.ring(a)
        late = self.submit_at(4, message="late")
        run = runner()
        got = self.advance(run, late.id, later(5))
        self.assertEqual((got.state, got.status, got.reason), ("queued", "", send.REASON_BEHIND))
        self.assertEqual(ts.tmux_writes(run), [])
        got = self.advance(run, a.id, later(6))
        self.assertEqual(got.status, "rung")
        self.assertEqual((len(self.rings()), ts.tmux_writes(run)), (1, []))

    def test_rung_message_does_not_expire(self):
        a = self.submit_at(0)
        self.ring(a, at=1)
        self.set_row("b", agent_status="busy")
        got = self.advance(FakeRunner(strict=True), a.id, later(send.QUEUE_LIMIT_S + 60))
        self.assertEqual((got.state, got.status), ("queued", "rung"))
        self.assertTrue(got.reason.startswith("已按門鈴"))

    def test_two_watchers_ring_once(self):
        a, t, z = self.three()
        self.heartbeat(at=later(3))
        runs, errors = [runner(), runner()], []
        go = threading.Barrier(2)

        def watch(run, did):
            try:
                go.wait()
                send.advance(self.cfg, run, did, sleep=lambda s: None, status_fn=self.status_fn, now=later(3))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=watch, args=(run, did)) for run, did in zip(runs, (a.id, z.id))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.rings()), 1)
        self.assertEqual(sum(len(r.sent_keys()) for r in runs), 1)

    def test_no_heartbeat_pastes(self):
        d = self.submit_at(0)
        run = runner()
        got = self.advance(run, d.id, later(1))
        self.assertEqual((got.state, got.reason), ("sent", doorbell.REASON_NO_MCP))
        self.assertEqual(ts.tmux_writes(run)[0][4], f"sampan-{d.id}")
        self.assertEqual(self.rings(), [])
        self.assertIn(f"派工 {d.id}：{doorbell.REASON_NO_MCP}", self.log())

    def test_stale_or_other_conversation_heartbeat_pastes(self):
        d = self.submit_at(0)
        self.heartbeat(conversation="99999999-2222-3333-4444-555555555555", at=later(1))
        self.assertEqual(self.advance(runner(), d.id, later(1)).state, "sent")
        e = self.submit_at(2)
        self.heartbeat(at=later(-300))
        self.transcript(SID_B, [ts.user(d.text), ts.assistant("ok")])
        self.advance(runner(), d.id, later(30))
        self.assertEqual(self.advance(runner(), e.id, later(31)).state, "sent")
        self.assertEqual(self.rings(), [])

    def test_codex_target_pastes(self):
        self.set_row("c", id=ID_C)
        d = self.submit_at(0, to="c")
        self.heartbeat(session_id=ID_C, conversation=ts.TID_C, agent="codex", at=later(1))
        run = FakeRunner(strict=True).on(["codex", "queue"], cp())
        got = self.advance(run, d.id, later(1))
        self.assertEqual((got.state, got.reason), ("sent", ""))
        self.assertEqual(run.called(["codex", "queue"])[0]["args"][-1], d.text)
        self.assertEqual(self.rings(), [])

    def test_raw_at_the_head_is_pasted_not_rung(self):
        r = self.submit_at(0, message="/compact", raw=True)
        b = self.submit_at(1)
        self.heartbeat(at=later(2))
        run = runner()
        self.assertEqual(self.advance(run, b.id, later(2)).reason, send.REASON_BEHIND)
        self.assertEqual(self.stored(r.id).state, "sent")
        self.assertEqual(self.rings(), [])

    def test_ring_stops_before_the_first_raw(self):
        a = self.submit_at(0)
        r = self.submit_at(1, message="/compact", raw=True)
        b = self.submit_at(2)
        ring = self.ring(b)
        self.assertEqual(json.loads(ring["messages"]), [a.id])
        self.assertEqual((self.stored(r.id).status, self.stored(b.id).status), ("", ""))
        self.assertEqual(self.stored(b.id).reason, send.REASON_BEHIND)


class NoAnswerTest(DoorbellBase):
    def test_no_answer_falls_back_to_paste(self):
        a, t, z = self.three()
        ring = self.ring(a)
        text = Path(dispatch.outbox_path(self.cfg.state_dir, f"ring-{ring['id']}")).read_text()
        self.transcript(SID_B, [ts.user(text), ts.assistant("not now")])
        run = runner()
        got = self.advance(run, t.id, later(40))
        self.assertEqual(self.rings()[0]["result"], "no_answer")
        self.assertEqual(got.state, "sent")
        for x in (a, t, z):
            s = self.stored(x.id)
            self.assertEqual((s.state, s.reason), ("sent", doorbell.REASON_NO_ANSWER))
        conn = store.connect(self.cfg.state_dir)
        self.addCleanup(conn.close)
        did = deliveries.delivery_of(conn, a.id)["id"]
        self.assertEqual(deliveries.members(conn, did), [a.id, t.id, z.id])
        self.assertEqual(ts.tmux_writes(run)[0][4], f"sampan-{did}")
        self.assertEqual(conn.execute("SELECT status FROM messages WHERE id=?", (a.id,)).fetchone()[0], "pasted")
        self.assertIn(f"門鈴 {ring['id']}：{doorbell.REASON_NO_ANSWER}", self.log())
        self.assertEqual(len(self.rings()), 1)

    def test_turn_still_going_is_not_no_answer(self):
        a = self.submit_at(0)
        ring = self.ring(a)
        text = dispatch.outbox_path(self.cfg.state_dir, f"ring-{ring['id']}").read_text()
        self.transcript(SID_B, [ts.user(text), ts.assistant("looking"), tool_use("Bash")])
        got = self.advance(runner(), a.id, later(40))
        self.assertEqual((got.status, self.rings()[0]["result"]), ("rung", "ringing"))

    def test_ring_never_seen_counts_as_no_answer_after_the_grace(self):  # e.g. sampan died before pressing a key
        a = self.submit_at(0)
        self.ring(a)
        self.transcript(SID_B, [ts.user("something else"), ts.assistant("other")])
        self.assertEqual(self.advance(runner(), a.id, later(5)).status, "rung")  # within the grace
        got = self.advance(runner(), a.id, later(40))
        self.assertEqual((got.state, self.rings()[0]["result"]), ("sent", "no_answer"))

    def test_ring_left_in_the_box_gets_another_enter(self):
        a = self.submit_at(0)
        ring = self.ring(a)
        text = dispatch.outbox_path(self.cfg.state_dir, f"ring-{ring['id']}").read_text()
        run = FakeRunner(strict=True)
        run.on(["tmux", "-u", "capture-pane"], cp(0, ring_screen(text)))
        run.on(["tmux", "-u", "display-message"], cp(0, "0\n"))
        run.on(["tmux", "-u", "send-keys"], cp())
        run.on(["ps", "-o", "lstart="], cp(0, PS_LSTART))
        got = self.advance(run, a.id, later(40))
        self.assertEqual(got.status, "rung")
        self.assertEqual([w[2] for w in ts.tmux_writes(run)][:1], ["send-keys"])  # no second paste
        self.assertNotIn("paste-buffer", [w[2] for w in ts.tmux_writes(run)])
        self.assertEqual(self.rings()[0]["result"], "ringing")

    def test_new_conversation_supersedes_the_ring(self):
        a = self.submit_at(0)
        self.ring(a)
        new = "99999999-2222-3333-4444-555555555555"
        self.set_row("b", sid=new)
        self.heartbeat(conversation=new, at=later(40))
        run = runner()
        self.advance(run, a.id, later(40))
        rings = self.rings()
        self.assertEqual([r["result"] for r in rings], ["superseded", "ringing"])
        self.assertEqual(json.loads(rings[1]["messages"]), [a.id])


class FailureTest(DoorbellBase):
    def test_paste_failed_keeps_them_queued_and_rerings(self):
        a, t, z = self.three()
        self.heartbeat(at=later(3))
        got = self.advance(runner(ts.DRAFT), a.id, later(3))
        self.assertEqual((got.state, got.status, got.reason), ("queued", "", deliver.REASON_DRAFT))
        self.assertEqual([r["result"] for r in self.rings()], ["paste_failed"])
        self.assertEqual([self.stored(x.id).status for x in (a, t, z)], ["", "", ""])
        run = runner()
        got = self.advance(run, z.id, later(4))
        self.assertEqual(got.status, "rung")
        self.assertEqual([r["result"] for r in self.rings()], ["paste_failed", "ringing"])
        self.assertEqual(len(run.sent_keys()), 1)

    def test_permission_prompt_reason(self):
        a = self.submit_at(0)
        self.ring(a)
        self.set_row("b", agent_status="waiting")
        run = runner(WAITING)
        got = self.advance(run, a.id, later(10))
        self.assertEqual((got.state, got.status, got.reason), ("queued", "rung", doorbell.REASON_PERMISSION))
        self.assertEqual(ts.tmux_writes(run), [])

    def test_unsure_ring_stays_ringing(self):
        a = self.submit_at(0)
        self.heartbeat(at=later(1))
        run = runner()
        run.on(["tmux", "-u", "send-keys"], cp(1, err="no pane"))
        got = self.advance(run, a.id, later(1))
        self.assertEqual((got.status, self.rings()[0]["result"]), ("rung", "ringing"))


class NextGroupTest(DoorbellBase):
    def test_next_group_with_a_rung_message_and_nothing_queued(self):
        a = self.submit_at(0)
        with dispatch.transaction(self.cfg.state_dir) as items:
            items[0].status = "rung"
            self.assertEqual(send._next_group(items, items[0], self.rows["b"]), [])
        # a rung message no ringing ring holds goes back in line (no IndexError), and is pasted: no heartbeat
        self.assertEqual(self.advance(runner(), a.id, later(1)).state, "sent")


class AfterMarkerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "t.jsonl"

    def write(self, recs):
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))

    def test_not_found(self):
        self.write([ts.user("hello"), ts.assistant("hi")])
        self.assertFalse(after_marker(self.path, "門鈴 abcd").found)
        self.assertFalse(after_marker(self.path.with_name("missing.jsonl"), "x").found)

    def test_turn_ended_without_take(self):
        self.write([ts.user("〔sampan 門鈴 abcd〕你有 1 則新訊息"), ts.assistant("first"), ts.assistant("last")])
        info = after_marker(self.path, "門鈴 abcd")
        self.assertEqual((info.found, info.ended, info.last_text, info.take_seen), (True, True, "last", False))

    def test_pending_tool_use_is_not_ended(self):
        self.write([ts.user("門鈴 abcd"), ts.assistant("checking"), tool_use("mcp__sampan__inbox_take")])
        info = after_marker(self.path, "門鈴 abcd")
        self.assertEqual((info.found, info.ended, info.take_seen), (True, False, True))

    def test_take_and_answer(self):
        self.write([ts.user("門鈴 abcd"), tool_use("mcp__sampan__inbox_take"), tool_result("收據 d1c2；1 則"),
                    ts.assistant("done")])
        info = after_marker(self.path, "門鈴 abcd")
        self.assertEqual((info.ended, info.last_text, info.take_seen), (True, "done", True))
        info = after_marker(self.path, "收據 d1c2")  # found in a tool_result
        self.assertEqual((info.found, info.ended, info.last_text, info.take_seen), (True, True, "done", False))

    def test_turn_stops_at_the_next_typed_message(self):
        self.write([ts.user("門鈴 abcd"), ts.assistant("answer"), ts.user("next question"), tool_use("Bash")])
        info = after_marker(self.path, "門鈴 abcd")
        self.assertEqual((info.ended, info.last_text), (True, "answer"))

    def test_last_marker_counts(self):
        self.write([ts.user("門鈴 abcd"), ts.assistant("old"), ts.user("門鈴 abcd again")])
        info = after_marker(self.path, "門鈴 abcd")
        self.assertEqual((info.found, info.ended, info.last_text), (True, False, ""))

    def test_typed_message_ends_the_turn_whatever_came_before(self):
        self.write([ts.user("門鈴 abcd"), INTERRUPTED, ts.user("other")])
        self.assertTrue(after_marker(self.path, "門鈴 abcd").ended)
        self.write([ts.user("門鈴 abcd"), tool_use("Bash"), ts.user("other")])
        self.assertTrue(after_marker(self.path, "門鈴 abcd").ended)

    def test_successful_tool_result_last_is_mid_turn(self):
        self.write([ts.user("門鈴 abcd"), ts.assistant("checking"), tool_use("Bash"), tool_result("ok")])
        self.assertFalse(after_marker(self.path, "門鈴 abcd").ended)

    def test_a_failed_tool_is_mid_turn(self):
        self.write([ts.user("門鈴 abcd"), tool_use("Bash"), tool_result("Exit code 1", error=True)])
        self.assertFalse(after_marker(self.path, "門鈴 abcd").ended)

    def test_denied_or_interrupted_tool_ends_the_turn(self):
        self.write([ts.user("門鈴 abcd"), tool_use("mcp__sampan__inbox_take"), tool_result(
            "The user doesn't want to proceed with this tool use. The tool use was rejected.", error=True)])
        info = after_marker(self.path, "門鈴 abcd")
        self.assertEqual((info.ended, info.take_seen), (True, True))
        self.write([ts.user("門鈴 abcd"), tool_use("Bash"), INTERRUPTED])
        self.assertTrue(after_marker(self.path, "門鈴 abcd").ended)

    def test_start_offset(self):
        self.write([ts.user("門鈴 abcd"), ts.assistant("old")])
        start = self.path.stat().st_size
        with self.path.open("a") as fh:
            fh.write(json.dumps(ts.user("other")) + "\n")
        self.assertFalse(after_marker(self.path, "門鈴 abcd", start).found)
        self.assertTrue(after_marker(self.path, "門鈴 abcd", 0).found)


if __name__ == "__main__":
    unittest.main()
