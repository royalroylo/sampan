"""The inbox a doorbelled Claude uses through MCP: inbox_take hands it every queued message at once, reply answers each;
the turn after the take ending fills the unanswered ones from its last text and tells each sender once."""
import json
import re
import unittest

from sampan import deliveries, dispatch, inbox, send, store
from sampan.clock import fmt_local
from sampan.send import Caller
from tests import test_send as ts
from tests.fakes import FakeRunner
from tests.helpers import status_row
from tests.test_doorbell import ID_B, SID_B, DoorbellBase, later, runner, tool_use

CALLER_B = Caller("b", "alice", "claude", "%2", id=ID_B)
ID_Z = ts.CALLER_Z.id
FIXTURE = ts.FIXTURES / "transcript-inbox-take-reply.jsonl"
NEW_SID = "99999999-2222-3333-4444-555555555555"


def receipt_of(text):
    return re.match(r"收據 (d[0-9a-f]{6})；", text).group(1)


class InboxBase(DoorbellBase):
    def setUp(self):
        super().setUp()
        self.rows["z"] = status_row("z", profile="work", sid=ts.SID_Z, pane="%9", id=ID_Z)

    def send_at(self, seconds, message, caller=ts.CALLER_A, notify=True, to="b"):
        return send.submit(self.cfg, FakeRunner(strict=True), caller, to, message, notify=notify, now=later(seconds),
                           status_fn=self.status_fn)

    def take(self, caller=CALLER_B, at=5):
        return inbox.take(self.cfg, caller, rows=list(self.rows.values()), now=later(at))

    def reply(self, did, text, receipt=None, caller=CALLER_B, at=6):
        return inbox.reply(self.cfg, caller, did, text, receipt, now=later(at))

    def path(self):
        return self.cfg.home / ".claude" / "projects" / "-proj" / f"{SID_B}.jsonl"

    def append(self, recs):
        with self.path().open("a") as fh:
            fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))

    def rung(self, *msgs):
        """Ring b for msgs; the ring line is the start of b's transcript (the take's cursor lands after it)."""
        ring = self.ring(*msgs)
        line = dispatch.outbox_path(self.cfg.state_dir, f"ring-{ring['id']}").read_text()
        self.transcript(SID_B, [ts.user(line)])
        return ring

    def taken(self, *msgs, at=5):
        """Ring for msgs and take them; the receipt."""
        self.rung(*msgs)
        text = self.take(at=at)
        self.append([tool_use("mcp__sampan__inbox_take", "tk"), _result(text, "tk")])
        return receipt_of(text)

    def conn(self):
        conn = store.connect(self.cfg.state_dir)
        self.addCleanup(conn.close)
        return conn

    def parts(self, did):
        return [(p["how"], p["text"], p["late"]) for p in deliveries.parts(self.conn(), did)]

    def notices(self, run, pane):
        """The notice texts pasted into pane (read back from their outbox files)."""
        out = []
        for a in ts.tmux_writes(run, pane):
            if a[2] == "paste-buffer":
                name = a[a.index("-b") + 1].removeprefix("sampan-")
                out.append(dispatch.outbox_path(self.cfg.state_dir, name).read_text())
        return out


def _result(text, uid):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": uid, "content": [{"type": "text", "text": text}]}]}}


class TakeTest(InboxBase):
    def test_take_returns_plain_text_in_time_order_with_receipt(self):
        a, t, z = self.three()
        ring = self.rung(a)
        size = self.path().stat().st_size
        text = self.take()
        did = receipt_of(text)
        lines = text.split("\n")
        self.assertEqual(lines[0], f"收據 {did}；3 則，依時間順序；前後衝突時以後面的為準")
        blocks = text.split("\n\n")
        for i, (x, who, msg) in enumerate(((a, "a（alice）", "from a"), (t, "終端機", "from terminal"),
                                          (z, "z（work）", "from z")), 1):
            self.assertEqual(blocks[i], f"【{i}／3｜派工 {x.id}｜來自 {who}｜{fmt_local(later(i - 1))}】\n{msg}")
        self.assertEqual(blocks[-1], f"每則做完用 mcp__sampan__reply 回一次（dispatch_id、text、receipt={did}），"
                                     "那是最終回覆，不要用來報進度。")
        self.assertNotIn("〔sampan 轉送", text)  # the bodies only, without the paste instructions
        for x in (a, t, z):
            s = self.stored(x.id)
            self.assertEqual((s.state, s.status, s.reason), ("sent", "taken", "已取走，對方還在做"))
        r = self.rings()[0]
        self.assertEqual((r["id"], r["result"], r["delivery"]), (ring["id"], "taken", did))
        d = dict(self.conn().execute("SELECT * FROM deliveries WHERE id=?", (did,)).fetchone())
        self.assertEqual((d["kind"], d["state"], d["to_id"], d["conversation"], d["cursor"], d["text"]),
                         ("inbox", "active", ID_B, SID_B, size, text))
        self.assertEqual(deliveries.members(self.conn(), did), [a.id, t.id, z.id])
        self.assertIn(f"送達 {did}", self.log())

    def test_take_without_a_ring_and_with_nothing_queued(self):
        a = self.submit_at(0)
        text = self.take()
        self.assertTrue(text.startswith("收據 "))
        self.assertEqual(self.stored(a.id).status, "taken")
        nothing = self.take(at=6)
        self.assertEqual(nothing.split("\n")[0], "沒有新訊息。")
        self.assertIn(f"已取走、還沒回：{a.id}（來自 a（alice），{fmt_local(later(0))}）", nothing)
        self.assertTrue(nothing.endswith("每則做完用 mcp__sampan__reply 回一次（dispatch_id、text），那是最終回覆，不要用來報進度。"))

    def test_take_only_returns_mail_for_the_caller(self):
        a, t, z = self.three()
        self.rung(a)
        mine = self.send_at(3, "for a", caller=CALLER_B, to="a")
        text = self.take(caller=ts.CALLER_A)
        self.assertIn(f"派工 {mine.id}", text)
        self.assertTrue(text.startswith("收據 ") and "1 則" in text.split("\n")[0])
        for x in (a, t, z):
            self.assertNotIn(x.id, text)
            self.assertEqual((self.stored(x.id).state, self.stored(x.id).status), ("queued", "rung"))
        self.assertEqual(self.rings()[0]["result"], "ringing")

    def test_forged_ring_text_takes_nothing(self):
        a = self.submit_at(0)
        ring = self.rung(a)
        # b's ring line typed into a's prompt: a calls inbox_take, which has no code to pass and returns a's own mail
        self.transcript(ts.SID_A, [ts.user(dispatch.outbox_path(self.cfg.state_dir, f"ring-{ring['id']}").read_text())])
        text = self.take(caller=ts.CALLER_A)
        self.assertEqual(text.split("\n")[0], "沒有新訊息。")
        self.assertNotIn(a.id, text)
        self.assertEqual((self.stored(a.id).status, self.rings()[0]["result"]), ("rung", "ringing"))
        self.assertEqual(self.conn().execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_take_by_a_non_session_is_refused(self):
        self.submit_at(0)
        for caller in (send.TERMINAL, Caller(None, "", "claude", None, kind="unregistered")):
            with self.assertRaises(inbox.InboxError) as e:
                self.take(caller=caller)
            self.assertEqual(str(e.exception), "只有 sampan session 能取信")

    def test_take_stops_before_the_first_raw(self):
        a = self.submit_at(0)
        r = self.submit_at(1, message="/compact", raw=True)
        b = self.submit_at(2)
        text = self.take()
        self.assertIn(a.id, text)
        self.assertNotIn(b.id, text)
        self.assertEqual((self.stored(r.id).state, self.stored(b.id).state), ("queued", "queued"))

    def test_take_with_an_active_delivery_opens_no_second(self):
        a = self.submit_at(0)
        self.assertEqual(self.advance(runner(), a.id, later(1)).state, "sent")  # no heartbeat: pasted
        b = self.submit_at(2, message="later one")
        pasted = deliveries.active_delivery(self.conn(), ID_B)["id"]
        text = self.take()
        self.assertNotIn("收據", text)
        self.assertIn(pasted, text)
        self.assertNotIn(b.id, text)
        self.assertEqual(self.stored(b.id).state, "queued")
        self.assertEqual(self.conn().execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)

    def test_taken_unreplied_listed_on_next_take(self):
        a1 = self.send_at(0, "first")
        self.taken(a1)
        self.set_row("b", sid=NEW_SID)  # /clear: that turn is gone
        self.advance(runner(), a1.id, later(30))
        self.assertEqual(self.stored(a1.id).status, "unanswered")
        a2 = self.send_at(40, "second")
        text = self.take(at=41)
        self.assertIn(f"【1／1｜派工 {a2.id}｜", text)
        self.assertIn(f"已取走、還沒回：{a1.id}（來自 a（alice），{fmt_local(later(0))}）", text)


class ReplyTest(InboxBase):
    def test_reply_records_a_tool_part(self):
        a1, a2 = self.send_at(0, "one"), self.send_at(1, "two")
        did = self.taken(a1, a2)
        self.assertEqual(self.reply(a1.id, "answer", did), f"已記下，派工 {a1.id} 的回覆（第 1 段）")
        self.assertEqual(self.reply(a1.id, "answer, more", at=7), f"已記下，派工 {a1.id} 的回覆（第 2 段）")
        self.assertEqual(self.parts(a1.id), [("tool", "answer", False), ("tool", "answer, more", False)])
        self.assertEqual(self.stored(a1.id).reply, "answer, more")

    def test_reply_by_another_session_is_refused(self):
        a1 = self.send_at(0, "one")
        self.taken(a1)
        for caller in (ts.CALLER_A, send.TERMINAL):
            with self.assertRaises(inbox.InboxError) as e:
                self.reply(a1.id, "not mine", caller=caller)
            self.assertEqual(str(e.exception), "這則不是寄給你的")
        with self.assertRaises(inbox.InboxError) as e:
            self.reply("ffff", "no such")
        self.assertEqual(str(e.exception), "這則不是寄給你的")
        self.assertEqual(self.parts(a1.id), [])

    def test_wrong_receipt_refused(self):
        a1 = self.send_at(0, "one")
        with self.assertRaises(inbox.InboxError) as e:
            self.reply(a1.id, "too early")
        self.assertEqual(str(e.exception), "這則還沒被取走")
        self.taken(a1)
        with self.assertRaises(inbox.InboxError) as e:
            self.reply(a1.id, "answer", "d000000")
        self.assertEqual(str(e.exception), "收據不符")
        self.assertEqual(self.parts(a1.id), [])

    def test_empty_and_oversized_reply_refused(self):
        a1 = self.send_at(0, "one")
        did = self.taken(a1)
        for text, why in (("", "回覆是空的"), ("  \n ", "回覆是空的"), ("x" * 100_001, "回覆超過 100,000 字")):
            with self.assertRaises(inbox.InboxError) as e:
                self.reply(a1.id, text, did)
            self.assertEqual(str(e.exception), why)
        self.assertTrue(self.reply(a1.id, "x" * 100_000, did).startswith("已記下"))

    def test_reply_after_cancel_is_late_and_changes_nothing(self):
        a1 = self.send_at(0, "one")
        did = self.taken(a1)
        send.cancel(self.cfg, a1.id, now=later(7))
        self.assertEqual(self.reply(a1.id, "too late", did, at=8), "已記下；寄件者已取消（或已重送），不會通知對方")
        s = self.stored(a1.id)
        self.assertEqual((s.state, s.reply, s.notified), ("cancelled", None, False))
        self.assertEqual(self.parts(a1.id), [("tool", "too late", True)])


class TurnEndTest(InboxBase):
    def test_partial_until_the_turn_ends(self):
        a1, a2 = self.send_at(0, "one"), self.send_at(1, "two")
        did = self.taken(a1, a2)
        self.reply(a1.id, "answer one", did)
        self.append([tool_use("mcp__sampan__reply", "rp")])  # the reply call has not returned yet
        got = self.advance(runner(), a1.id, later(30))
        self.assertEqual((got.state, got.status, got.reason), ("sent", "partial", "已有回覆段落，對方還在做"))
        s = self.stored(a2.id)
        self.assertEqual((s.state, s.status, s.reason), ("sent", "taken", "已取走，對方還在做"))
        self.assertEqual(deliveries.active_delivery(self.conn(), ID_B)["id"], did)

    def test_turn_end_fills_the_unreplied_ones(self):
        a1, a2 = self.send_at(0, "first job"), self.send_at(1, "second job")
        self.rung(a1, a2)
        text = self.take()
        did = receipt_of(text)
        self.reply(a1.id, "answer one", did)
        fixture = FIXTURE.read_text().replace("{receipt}", did).replace("{first}", a1.id).replace("{second}", a2.id)
        with self.path().open("a") as fh:
            fh.write(fixture)
        self.assertEqual(self.advance(runner(), a2.id, later(10)).state, "done")
        self.assertEqual(self.parts(a1.id), [("tool", "answer one", False)])
        self.assertEqual(self.parts(a2.id), [("fill", "第二則也做完了：answer two", False)])
        for x, reply in ((a1, "answer one"), (a2, "第二則也做完了：answer two")):
            s = self.stored(x.id)
            self.assertEqual((s.state, s.status, s.reason, s.reply), ("done", "", "", reply))
        row = self.conn().execute("SELECT state FROM deliveries WHERE id=?", (did,)).fetchone()
        self.assertEqual(row["state"], "ended")

    def test_a_row_without_a_conversation_id_waits(self):
        a1 = self.send_at(0, "one")
        self.taken(a1)
        self.set_row("b", sid="")
        self.assertEqual(self.advance(runner(), a1.id, later(30)).status, "taken")

    def test_second_reply_before_the_notice_is_told_as_the_reply(self):
        a1 = self.send_at(0, "one")
        did = self.taken(a1)
        self.append([ts.assistant("done")])
        self.set_row("a", agent_status="busy")  # the notice has to wait
        self.advance(runner(), a1.id, later(30))
        self.assertEqual((self.stored(a1.id).state, self.stored(a1.id).notified), ("done", False))
        self.reply(a1.id, "better answer", did, at=40)
        self.set_row("a", agent_status="idle")
        run = runner()
        self.advance(run, a1.id, later(45))
        self.assertEqual(self.notices(run, "%1"),
                         [f"〔sampan：給 b 的派工 {a1.id} 已回覆；用 sampan 工具 dispatch_result 看內容〕"])

    def test_busy_target_is_not_judged(self):
        a1 = self.send_at(0, "one")
        self.taken(a1)
        self.append([ts.assistant("done")])
        self.set_row("b", agent_status="busy")
        self.assertEqual(self.advance(runner(), a1.id, later(30)).status, "taken")

    def test_turn_end_without_any_text_is_unanswered(self):
        a1 = self.send_at(0, "one", notify=False)
        self.taken(a1)
        self.append([tool_use("Bash", "b1"), _result("[Request interrupted by user for tool use]", "b1")])
        self.append([ts.user([{"type": "text", "text": "[Request interrupted by user]"}])])
        got = self.advance(runner(), a1.id, later(30))
        self.assertEqual((got.state, got.status, got.reason), ("failed", "unanswered", "已取走，這一輪結束卻沒有回覆"))

    def test_take_not_in_the_transcript_is_unanswered_after_the_grace(self):
        a1 = self.send_at(0, "one", notify=False)
        self.rung(a1)
        self.take()  # its result never reaches the transcript
        self.assertEqual(self.advance(runner(), a1.id, later(10)).status, "taken")  # within the grace
        got = self.advance(runner(), a1.id, later(30))
        self.assertEqual((got.state, got.status), ("failed", "unanswered"))

    def test_stopped_target_fails_the_unreplied(self):
        a1, a2 = self.send_at(0, "one", notify=False), self.send_at(1, "two", notify=False)
        did = self.taken(a1, a2)
        self.reply(a1.id, "answer one", did)
        self.set_row("b", state=ts.State.STOPPED)
        self.advance(FakeRunner(strict=True), a1.id, later(30))
        self.assertEqual((self.stored(a1.id).state, self.stored(a1.id).reply), ("done", "answer one"))
        s = self.stored(a2.id)
        self.assertEqual((s.state, s.status, s.reason), ("failed", "", send.REASON_STOPPED))

    def test_clear_after_take_is_unanswered_and_notified(self):
        a1 = self.send_at(0, "one")
        did = self.taken(a1)
        self.set_row("b", sid=NEW_SID)
        run = runner()
        got = self.advance(run, a1.id, later(30))
        self.assertEqual((got.state, got.status, got.reason, got.notified),
                         ("failed", "unanswered", "已取走，這一輪結束卻沒有回覆", True))
        self.assertEqual(self.notices(run, "%1"),
                         [f"〔sampan：給 b 的派工 {a1.id}：已取走，這一輪結束卻沒有回覆；用 sampan 工具 dispatch_result 看內容〕"])
        self.assertEqual(self.reply(a1.id, "late answer", did, at=40), f"已記下，派工 {a1.id} 的回覆（第 1 段）")
        s = self.stored(a1.id)
        self.assertEqual((s.state, s.status, s.reply, s.notify, s.notified), ("done", "", "late answer", True, False))
        self.assertIn(a1.id, send.pending(self.cfg))
        run = runner()
        self.advance(run, a1.id, later(45))
        self.assertEqual(self.notices(run, "%1"),
                         [f"〔sampan：給 b 的派工 {a1.id} 又多了一段回覆；用 sampan 工具 dispatch_result 看內容〕"])
        self.assertTrue(self.stored(a1.id).notified)

    def test_one_notice_per_sender_per_delivery(self):
        a1, a2 = self.send_at(0, "one"), self.send_at(1, "two")
        z1 = self.send_at(2, "three", caller=ts.CALLER_Z)
        self.taken(a1, a2, z1)
        self.append([ts.assistant("all three done")])
        run = runner()
        self.advance(run, a1.id, later(30))
        self.assertEqual(self.notices(run, "%1"),
                         [f"〔sampan：給 b 的派工 {a1.id}、{a2.id}（一起送出）已回覆；用 sampan 工具 dispatch_result 看內容〕"])
        self.assertEqual(self.notices(run, "%9"), [])  # z is told by its own watcher
        run = runner()
        self.advance(run, z1.id, later(31))
        self.advance(run, a2.id, later(32))
        self.assertEqual(self.notices(run, "%1"), [])
        self.assertEqual(self.notices(run, "%9"),
                         [f"〔sampan：給 b 的派工 {z1.id} 已回覆；用 sampan 工具 dispatch_result 看內容〕"])

    def test_terminal_sender_gets_no_notice(self):
        t = self.send_at(0, "from the terminal", caller=send.TERMINAL)
        did = self.taken(t)
        self.append([ts.assistant("done")])
        run = runner()
        got = self.advance(run, t.id, later(30))
        self.assertEqual((got.state, got.notify), ("done", False))
        self.assertEqual(ts.tmux_writes(run), [])
        self.reply(t.id, "one more", did, at=40)
        self.assertEqual((self.stored(t.id).notify, send.pending(self.cfg)), (False, []))

    def test_next_message_waits_for_the_inbox_delivery(self):
        a1 = self.send_at(0, "one")
        self.taken(a1)
        a2 = self.send_at(10, "two")
        run = runner()
        got = self.advance(run, a2.id, later(11))
        self.assertEqual((got.state, got.reason), ("queued", send.BUSY["busy"]))
        self.assertEqual(ts.tmux_writes(run, "%2"), [])
        self.append([ts.assistant("done one")])
        got = self.advance(runner(), a2.id, later(30))  # the turn ended: the delivery is over, a2 gets its ring
        self.assertEqual((self.stored(a1.id).state, got.status), ("done", "rung"))


if __name__ == "__main__":
    unittest.main()
