"""sampan mcp serve: JSON-RPC over stdio, the tools, elicitation approval, who the caller is."""
import dataclasses
import io
import json
import queue
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sampan import dispatch, mcp_server, send
from sampan.registry import Row, write_rows
from sampan.send import Caller
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env, status_row

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EMPTY = (FIXTURES / "claude_empty_placeholder.ansi").read_text()
NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
CALLER_A = Caller("a", "alice", "claude", "%1")
NO_WATCH = None


def req(rid, method, params=None):
    m = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        m["params"] = params
    return json.dumps(m, ensure_ascii=False) + "\n"


def init(rid=0, version="2025-06-18", elicitation=True):
    caps = {"elicitation": {}} if elicitation else {}
    return req(rid, "initialize", {"protocolVersion": version, "capabilities": caps,
                                   "clientInfo": {"name": "test", "version": "1"}})


def call(rid, name, args):
    return req(rid, "tools/call", {"name": name, "arguments": args})


def tmux_runner(screen=EMPTY):
    run = FakeRunner(strict=True)
    run.on(["tmux", "-u", "capture-pane"], cp(0, screen))
    run.on(["tmux", "-u", "load-buffer"], cp())
    run.on(["tmux", "-u", "paste-buffer"], cp())
    run.on(["tmux", "-u", "send-keys"], cp())
    return run


def tmux_writes(run):
    return [c["args"] for c in run.calls if c["args"][0] == "tmux" and "capture-pane" not in c["args"]]


class Client:
    """The client side of the stdio pipe. The server reads stdin through readline() and writes stdout through write().
    Server→client elicitation requests are answered by `answer(request) -> result dict`; the pipe closes (EOF) once
    the response to `last_id` has been written."""

    def __init__(self, lines, *, answer=None, last_id=None):
        self.q = queue.Queue()
        for line in lines:
            self.q.put(line)
        if last_id is None:
            self.q.put("")
        self.answer = answer
        self.last_id = last_id
        self.out = []
        self.elicits = []
        self._buf = ""

    def readline(self):
        return self.q.get(timeout=10)

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            msg = json.loads(line)
            self.out.append(msg)
            if msg.get("method") == "elicitation/create":
                self.elicits.append(msg)
                result = self.answer(msg)
                self.q.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
            elif self.last_id is not None and msg.get("id") == self.last_id and "method" not in msg:
                self.q.put("")

    def flush(self):
        pass

    def response(self, rid):
        return next(m for m in self.out if m.get("id") == rid and "method" not in m)


def accept(message=None):
    def answer(_msg):
        return {"action": "accept", "content": {} if message is None else {"message": message}}
    return answer


def decline(_msg):
    return {"action": "decline"}


def tool_text(resp):
    return resp["result"]["content"][0]["text"]


def tool_json(resp):
    return json.loads(tool_text(resp))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_env(Path(self._tmp.name))
        self.rows = {
            "a": status_row("a", sid="sa", pane="%1"),
            "b": status_row("b", sid="sb", pane="%2"),
            "c": status_row("c", agent="codex", profile="codex", sid="sc", pane="%3"),
        }

    def status_fn(self, cfg, run, snapshot_fn=None):
        return list(self.rows.values())

    def serve(self, client, *, run=None, confirm=True, caller=CALLER_A, sleep=None, clock=None, **kw):
        cfg = dataclasses.replace(self.cfg, mcp_confirm=confirm)
        rc = mcp_server.serve(cfg, run or FakeRunner(strict=True), client, client, caller=caller,
                              sleep=sleep or (lambda _s: None), clock=clock or (lambda: 0.0),
                              status_fn=self.status_fn, now_fn=lambda: NOW,
                              watch_interval=kw.pop("watch_interval", NO_WATCH), **kw)
        self.assertEqual(rc, 0)
        return client

    def dispatches(self):
        return dispatch.load(self.cfg.state_dir)


class ProtocolTest(Base):
    def test_initialize_echoes_a_known_version_else_answers_the_newest(self):
        for asked, answered in (("2025-03-26", "2025-03-26"), ("2025-06-18", "2025-06-18"),
                                ("2024-11-05", "2025-06-18"), ("1999", "2025-06-18")):
            with self.subTest(asked=asked):
                c = self.serve(Client([init(1, asked)]))
                r = c.response(1)["result"]
                self.assertEqual(r["protocolVersion"], answered)
                self.assertEqual(r["capabilities"], {"tools": {}})
                self.assertEqual(r["serverInfo"]["name"], "sampan")

    def test_tools_list_has_five_tools_with_schemas(self):
        c = self.serve(Client([init(1), json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n",
                               req(2, "tools/list")]))
        tools = {t["name"]: t for t in c.response(2)["result"]["tools"]}
        self.assertEqual(set(tools), {"list_sessions", "send", "new_session", "dispatch_result", "cancel_dispatch"})
        for t in tools.values():
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
        self.assertEqual(tools["send"]["inputSchema"]["required"], ["session", "message"])
        self.assertEqual(tools["send"]["inputSchema"]["properties"]["wait"]["type"], "boolean")
        self.assertEqual(sorted(tools["new_session"]["inputSchema"]["required"]), ["cwd", "message", "name", "target"])
        self.assertEqual(tools["dispatch_result"]["inputSchema"]["properties"]["wait"]["maximum"], 60)
        self.assertEqual(tools["cancel_dispatch"]["inputSchema"]["required"], ["id"])
        self.assertNotIn("required", tools["list_sessions"]["inputSchema"])

    def test_notification_gets_no_response(self):
        c = self.serve(Client([json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"]))
        self.assertEqual(c.out, [])

    def test_unknown_method_is_32601(self):
        c = self.serve(Client([init(1), req(2, "resources/list")]))
        self.assertEqual(c.response(2)["error"]["code"], -32601)

    def test_bad_json_is_32700_and_the_server_keeps_going(self):
        c = self.serve(Client(["{not json\n", "\n", req(3, "ping")]))
        self.assertEqual(c.out[0]["error"]["code"], -32700)
        self.assertIsNone(c.out[0]["id"])
        self.assertEqual(c.response(3)["result"], {})

    def test_not_a_request_object_is_32600(self):
        c = self.serve(Client(["[1, 2]\n", '{"jsonrpc": "2.0", "id": 4}\n', req(5, "ping")]))
        self.assertEqual([m["error"]["code"] for m in c.out[:2]], [-32600, -32600])
        self.assertEqual(c.response(5)["result"], {})

    def test_unknown_tool_is_32602(self):
        c = self.serve(Client([init(1), call(2, "rm_rf", {})]))
        self.assertEqual(c.response(2)["error"]["code"], -32602)

    def test_bytes_stdin_is_read_as_utf8(self):
        lines = [init(1).encode(), "{\"jsonrpc\": \"2.0\", \"id\": 2, \"method\": \"ping\", \"x\": \"中\"}\n".encode(), b"\xff\n"]
        stdout = io.StringIO()
        stdin = io.BytesIO(b"".join(lines))
        cfg = dataclasses.replace(self.cfg, mcp_confirm=True)
        mcp_server.serve(cfg, FakeRunner(strict=True), stdin, stdout, caller=CALLER_A, watch_interval=NO_WATCH)
        out = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(out[1], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertEqual(out[2]["error"]["code"], -32700)
        self.assertTrue(stdout.getvalue().isascii())  # only JSON escapes: any stdout encoding carries it


class ApprovalTest(Base):
    def test_send_asks_first_and_uses_the_edited_message(self):
        run = tmux_runner()
        c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "原本的\n訊息"})],
                              answer=accept("改過的訊息"), last_id=2), run=run)
        [e] = c.elicits
        p = e["params"]
        self.assertIn("送給 b（alice，Claude）／從 a（alice）", p["message"])
        self.assertIn("原本的\n訊息", p["message"])
        self.assertIn("完成後會在你的 session 送出一則通知", p["message"])
        schema = p["requestedSchema"]
        self.assertEqual(list(schema["properties"]), ["message"])
        self.assertEqual(schema["properties"]["message"]["type"], "string")
        self.assertEqual(schema["properties"]["message"]["default"], "原本的\n訊息")
        r = c.response(2)["result"]
        self.assertFalse(r["isError"])
        [d] = self.dispatches()
        self.assertEqual(tool_json(c.response(2))["dispatch"], d.id)
        self.assertTrue(d.text.endswith("\n改過的訊息"))
        self.assertTrue(d.notify)
        self.assertEqual(d.state, "sent")  # b idle with an empty box: delivered at once
        # the elicitation went out before any tmux write
        self.assertLess(c.out.index(e), len(c.out))

    def test_accept_without_content_sends_the_original(self):
        c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi"})],
                              answer=accept(), last_id=2), run=tmux_runner())
        [d] = self.dispatches()
        self.assertTrue(d.text.endswith("\nhi"))
        self.assertFalse(c.response(2)["result"]["isError"])

    def test_decline_or_cancel_creates_nothing_and_types_nothing(self):
        for action in ("decline", "cancel"):
            with self.subTest(action=action):
                run = tmux_runner()
                c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi"})],
                                      answer=lambda _m, a=action: {"action": a}, last_id=2), run=run)
                r = c.response(2)["result"]
                self.assertTrue(r["isError"])
                self.assertIn("使用者拒絕", tool_text(c.response(2)))
                self.assertEqual(self.dispatches(), [])
                self.assertEqual(tmux_writes(run), [])

    def test_client_without_elicitation_is_refused(self):
        run = tmux_runner()
        c = self.serve(Client([init(1, elicitation=False), call(2, "send", {"session": "b", "message": "hi"})]), run=run)
        r = c.response(2)["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(tool_text(c.response(2)), "這個 client 不支援核准表單；要用請在 config 設 [mcp] confirm = false")
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(tmux_writes(run), [])

    def test_confirm_false_sends_without_asking(self):
        c = self.serve(Client([init(1, elicitation=False), call(2, "send", {"session": "b", "message": "hi"})]),
                       run=tmux_runner(), confirm=False)
        self.assertEqual(c.elicits, [])
        self.assertFalse(c.response(2)["result"]["isError"])
        [d] = self.dispatches()
        self.assertEqual(tool_json(c.response(2)), {"dispatch": d.id, "state": "sent"})

    def test_read_only_tools_never_ask(self):
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        c = self.serve(Client([init(1), call(2, "list_sessions", {}), call(3, "dispatch_result", {"id": d.id})],
                              answer=decline), run=FakeRunner(strict=True))
        self.assertEqual(c.elicits, [])
        self.assertFalse(c.response(2)["result"]["isError"])
        self.assertFalse(c.response(3)["result"]["isError"])

    def test_bad_target_is_refused_before_asking(self):
        for to, why in (("zz", "找不到 session「zz」"), ("a", "不能送給自己")):
            with self.subTest(to=to):
                c = self.serve(Client([init(1), call(2, "send", {"session": to, "message": "hi"})], answer=accept()))
                self.assertEqual(c.elicits, [])
                self.assertTrue(c.response(2)["result"]["isError"])
                self.assertIn(why, tool_text(c.response(2)))

    def test_missing_argument_is_a_tool_error(self):
        c = self.serve(Client([init(1), call(2, "send", {"session": "b"})], answer=accept()))
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertIn("message", tool_text(c.response(2)))
        self.assertEqual(c.elicits, [])

    def test_unregistered_caller_is_named_and_gets_no_notice(self):
        c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi"})],
                              answer=accept(), last_id=2), run=tmux_runner(), caller=Caller(None, "", "claude", None))
        [e] = c.elicits
        self.assertIn("從 未登記的 session", e["params"]["message"])
        self.assertNotIn("通知", e["params"]["message"])
        [d] = self.dispatches()
        self.assertFalse(d.notify)

    def test_cancel_dispatch_asks_then_cancels(self):
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        c = self.serve(Client([init(1), call(2, "cancel_dispatch", {"id": d.id})], answer=accept(), last_id=2))
        [e] = c.elicits
        self.assertIn(f"取消派工 {d.id}（給 b）", e["params"]["message"])
        self.assertFalse(c.response(2)["result"]["isError"])
        self.assertEqual(self.dispatches()[0].state, "cancelled")

    def test_cancel_dispatch_declined_keeps_it(self):
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        c = self.serve(Client([init(1), call(2, "cancel_dispatch", {"id": d.id})], answer=decline, last_id=2))
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertEqual(self.dispatches()[0].state, "queued")

    def test_cancel_unknown_is_refused_without_asking(self):
        c = self.serve(Client([init(1), call(2, "cancel_dispatch", {"id": "ffff"})], answer=accept()))
        self.assertEqual(c.elicits, [])
        self.assertIn("沒有派工 ffff", tool_text(c.response(2)))

    def test_other_requests_are_answered_while_waiting_for_approval(self):
        def slow_accept(_msg):
            client.q.put(req(9, "ping"))  # arrives before the approval answer
            return {"action": "accept", "content": {"message": "hi"}}

        client = Client([init(1), call(2, "send", {"session": "b", "message": "hi"})], answer=slow_accept, last_id=2)
        self.serve(client, run=tmux_runner())
        ids = [m.get("id") for m in client.out if "method" not in m]
        self.assertLess(ids.index(9), ids.index(2))

    def test_eof_while_waiting_for_approval_creates_nothing(self):
        client = Client([init(1), call(2, "send", {"session": "b", "message": "hi"})],
                        answer=lambda _m: None)  # never answered; EOF follows

        def write(s, _w=client.write):
            msg = json.loads(s)
            if msg.get("method") == "elicitation/create":
                client.out.append(msg)
                client.elicits.append(msg)
                client.q.put("")
                return
            _w(s)
        client.write = write
        self.serve(client, run=tmux_runner())
        self.assertEqual(self.dispatches(), [])


class SendToolTest(Base):
    def test_wait_gives_up_after_60_seconds_with_the_dispatch_still_sent(self):
        t = [0.0]

        def sleep(s):
            t[0] += s

        run = tmux_runner()

        def enter(_args):  # idle when delivered, then busy with it
            self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
            return cp()
        run.on(["tmux", "-u", "send-keys"], enter)
        c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi", "wait": True})]),
                       run=run, confirm=False, sleep=sleep, clock=lambda: t[0])
        r = c.response(2)["result"]
        self.assertFalse(r["isError"])
        body = tool_json(c.response(2))
        [d] = self.dispatches()
        self.assertEqual((body["dispatch"], body["state"]), (d.id, "sent"))
        self.assertNotIn("reply", body)
        self.assertGreaterEqual(t[0], 60)
        self.assertLess(t[0], 70)
        self.assertTrue(d.notify)  # nobody got the reply: the completion notice is owed after all

    def test_wait_returns_the_reply_when_done(self):
        replies = iter([send.Reply(True, True, "答案")])
        with mock.patch.object(send, "_reply", lambda cfg, row, text: next(replies)):
            c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi", "wait": True})]),
                           run=tmux_runner(), confirm=False)
        body = tool_json(c.response(2))
        self.assertEqual((body["state"], body["reply"]), ("done", "答案"))
        [d] = self.dispatches()
        self.assertFalse(d.notify)  # the reply came back in the tool result: no notice

    def test_list_sessions_rows(self):
        write_rows(self.cfg.registry_path, [Row("b", "claude", "alice", "sb", "/w/b")], "t", self.cfg.history_dir)
        self.rows["b"] = dataclasses.replace(self.rows["b"], last=NOW, agent_status="busy")
        c = self.serve(Client([init(1), call(2, "list_sessions", {})]))
        rows = {r["name"]: r for r in tool_json(c.response(2))["sessions"]}
        self.assertEqual(rows["b"], {"name": "b", "agent": "claude", "profile": "alice", "cwd": "/w/b",
                                     "state": "running", "activity": "busy", "last": "2026-10-26T12:00:00+00:00"})
        self.assertEqual(rows["a"]["cwd"], "")
        self.assertIsNone(rows["a"]["last"])

    def test_dispatch_result_reports_state_and_reply(self):
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        c = self.serve(Client([init(1), call(2, "dispatch_result", {"id": d.id}),
                               call(3, "dispatch_result", {"id": "ffff"}),
                               call(4, "dispatch_result", {"id": d.id, "wait": 61})]))
        body = tool_json(c.response(2))
        self.assertEqual((body["dispatch"], body["state"], body["to"]), (d.id, "queued", "b"))
        self.assertEqual(body["reason"], "對方正在忙")
        self.assertTrue(c.response(3)["result"]["isError"])
        self.assertTrue(c.response(4)["result"]["isError"])  # wait above 60 is refused
        self.assertIn("60", tool_text(c.response(4)))

    def test_dispatch_result_wait_polls_until_the_limit(self):
        t = [0.0]
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        c = self.serve(Client([init(1), call(2, "dispatch_result", {"id": d.id, "wait": 10})]),
                       sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertEqual(tool_json(c.response(2))["state"], "queued")
        self.assertGreaterEqual(t[0], 10)
        self.assertLess(t[0], 14)


class NewSessionToolTest(Base):
    def test_new_session_asks_then_creates_and_returns_the_dispatch(self):
        seen = {}

        def fake_new(cfg, run, out, name, target, cwd, *, message=None, caller=None, **kw):
            seen.update(name=name, target=target, cwd=cwd, message=message, caller=caller)
            send.submit_new(cfg, caller, name, "claude", "work", message, notify=True, now=NOW)
            out("✅ 已開好")
            return 0

        with mock.patch.object(mcp_server.session_run, "cmd_new", fake_new):
            c = self.serve(Client([init(1), call(2, "new_session", {"name": "n1", "target": "claude:work",
                                                                     "cwd": "/w/n1", "message": "開工"})],
                                  answer=accept("開工吧"), last_id=2))
        [e] = c.elicits
        self.assertIn("n1（work，Claude）", e["params"]["message"])
        self.assertIn("/w/n1", e["params"]["message"])
        self.assertIn("完成後會在你的 session 送出一則通知", e["params"]["message"])
        self.assertEqual((seen["message"], seen["caller"]), ("開工吧", CALLER_A))
        [d] = self.dispatches()
        self.assertEqual(tool_json(c.response(2))["dispatch"], d.id)

    def test_new_session_failure_is_a_tool_error(self):
        def fake_new(cfg, run, out, name, target, cwd, **kw):
            out("❌ tmux session n1 已存在")
            return 2

        with mock.patch.object(mcp_server.session_run, "cmd_new", fake_new):
            c = self.serve(Client([init(1), call(2, "new_session", {"name": "n1", "target": "work",
                                                                     "cwd": "/w", "message": "hi"})]), confirm=False)
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertIn("tmux session n1 已存在", tool_text(c.response(2)))

    def test_new_session_unknown_profile_is_refused_before_asking(self):
        c = self.serve(Client([init(1), call(2, "new_session", {"name": "n1", "target": "claude:nope",
                                                                 "cwd": "/w", "message": "hi"})], answer=accept()))
        self.assertEqual(c.elicits, [])
        self.assertTrue(c.response(2)["result"]["isError"])


class WatchTest(Base):
    def test_watch_once_advances_only_this_callers_dispatches(self):
        mine = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                           status_fn=self.status_fn)
        other = send.submit(self.cfg, FakeRunner(strict=True), Caller("c", "codex", "codex", "%3"), "b", "yo",
                            notify=False, now=NOW, status_fn=self.status_fn)
        server = mcp_server.Server(self.cfg, tmux_runner(), io.StringIO(), io.StringIO(), caller=CALLER_A,
                                   sleep=lambda _s: None, clock=lambda: 0.0, status_fn=self.status_fn,
                                   now_fn=lambda: NOW)
        server.watch_once()
        states = {d.id: d.state for d in self.dispatches()}
        self.assertEqual((states[mine.id], states[other.id]), ("sent", "queued"))

    def test_watcher_thread_runs_while_serving(self):
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        client = Client([init(1)], last_id=99)  # stays open until we close it
        done = threading.Event()

        def closer():
            for _ in range(200):
                if dispatch.load(self.cfg.state_dir)[0].state == "sent":
                    break
                threading.Event().wait(0.01)
            done.set()
            client.q.put("")

        threading.Thread(target=closer).start()
        self.serve(client, run=tmux_runner(), watch_interval=0.01)
        self.assertTrue(done.is_set())
        self.assertEqual(self.dispatches()[0].state, "sent")
        self.assertEqual(d.state, "queued")


class IdentifyCallerTest(Base):
    def runner(self, panes, procs):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        r.on(["ps", "-A"], cp(out="\n".join(procs) + "\n"))
        return r

    PROCS = [ps_line(1, 0, "launchd", "launchd"), ps_line(100, 1, "zsh", "-zsh"),
             ps_line(200, 100, "claude", "claude"), ps_line(300, 200, "sampan", "sampan mcp serve")]

    def test_finds_the_pane_and_the_registry_row(self):
        write_rows(self.cfg.registry_path, [Row("1005-x", "claude", "alice", "s1", "/w")], "t", self.cfg.history_dir)
        run = self.runner([pane_line("other", "%4", 50, "zsh", "/"), pane_line("1005-x", "%5", 100, "claude", "/w")],
                          self.PROCS)
        self.assertEqual(mcp_server.identify_caller(self.cfg, run, 300), Caller("1005-x", "alice", "claude", "%5"))

    def test_no_registry_row_is_unregistered_but_keeps_the_agent(self):
        run = self.runner([pane_line("1005-x", "%5", 100, "claude", "/w")], self.PROCS)
        self.assertEqual(mcp_server.identify_caller(self.cfg, run, 300), Caller(None, "", "claude", None))

    def test_outside_tmux_is_unregistered(self):
        run = self.runner([pane_line("other", "%4", 50, "zsh", "/")], self.PROCS)
        self.assertEqual(mcp_server.identify_caller(self.cfg, run, 300), Caller(None, "", "claude", None))

    def test_no_agent_ancestor(self):
        run = self.runner([], [ps_line(1, 0, "launchd", "launchd"), ps_line(300, 1, "sampan", "sampan mcp serve")])
        self.assertEqual(mcp_server.identify_caller(self.cfg, run, 300), Caller(None, "", "", None))

    def test_codex_caller(self):
        write_rows(self.cfg.registry_path, [Row("cx", "codex", "codex", "t1", "/w")], "t", self.cfg.history_dir)
        procs = [ps_line(1, 0, "launchd", "launchd"), ps_line(100, 1, "zsh", "-zsh"),
                 ps_line(200, 100, "node", "node /opt/bin/codex"), ps_line(210, 200, "codex", "codex"),
                 ps_line(300, 210, "sampan", "sampan mcp serve")]
        run = self.runner([pane_line("cx", "%7", 100, "node", "/w")], procs)
        self.assertEqual(mcp_server.identify_caller(self.cfg, run, 300), Caller("cx", "codex", "codex", "%7"))

    def test_registry_row_of_another_agent_does_not_count(self):
        write_rows(self.cfg.registry_path, [Row("1005-x", "codex", "codex", "t1", "/w")], "t", self.cfg.history_dir)
        run = self.runner([pane_line("1005-x", "%5", 100, "claude", "/w")], self.PROCS)
        self.assertEqual(mcp_server.identify_caller(self.cfg, run, 300), Caller(None, "", "claude", None))

    def test_unreadable_tmux_is_unregistered(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-panes"], cp(rc=1, err="permission denied"))
        r.on(["ps", "-A"], cp(out="\n".join(self.PROCS) + "\n"))
        self.assertEqual(mcp_server.identify_caller(self.cfg, r, 300), Caller(None, "", "claude", None))


if __name__ == "__main__":
    unittest.main()
