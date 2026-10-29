"""sampan mcp serve: JSON-RPC over stdio, the tools, the user-interaction marks on acting tools, who the caller is."""
import dataclasses
import io
import json
import os
import queue
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sampan import codex_approval, dispatch, folders, mcp_server, send
from sampan.registry import Row, write_rows
from sampan.send import Caller
from sampan.status import State
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


def init(rid=0, version="2025-06-18", elicitation=True, client="claude-code"):
    caps = {"elicitation": {}} if elicitation else {}
    return req(rid, "initialize", {"protocolVersion": version, "capabilities": caps,
                                   "clientInfo": {"name": client, "version": "1"}} if client else
                                  {"protocolVersion": version, "capabilities": caps})


def call(rid, name, args):
    return req(rid, "tools/call", {"name": name, "arguments": args})


def tmux_runner(screen=EMPTY):
    run = FakeRunner(strict=True)
    run.on(["tmux", "-u", "capture-pane"], cp(0, screen))
    run.on(["tmux", "-u", "display-message"], cp(0, "0\n"))
    run.on(["tmux", "-u", "load-buffer"], cp())
    run.on(["tmux", "-u", "paste-buffer"], cp())
    run.on(["tmux", "-u", "send-keys"], cp())
    return run


def tmux_writes(run):
    return [c["args"] for c in run.calls if c["args"][0] == "tmux" and not {"capture-pane", "display-message"} & set(c["args"])]


class Client:
    """The client side of the stdio pipe. The server reads stdin through readline() and writes stdout through write().
    Any server→client `elicitation/create` request is recorded in `elicits` (there should be none); the pipe closes
    (EOF) once the response to `last_id` has been written."""

    def __init__(self, lines, *, last_id=None):
        self.q = queue.Queue()
        for line in lines:
            self.q.put(line)
        if last_id is None:
            self.q.put("")
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
            elif self.last_id is not None and msg.get("id") == self.last_id and "method" not in msg:
                self.q.put("")

    def flush(self):
        pass

    def response(self, rid):
        return next(m for m in self.out if m.get("id") == rid and "method" not in m)


def tool_text(resp):
    return resp["result"]["content"][0]["text"]


def tool_json(resp):
    return json.loads(tool_text(resp))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_env(Path(self._tmp.name))
        env = mock.patch.dict("os.environ", {"CODEX_HOME": str(self.cfg.codex.home)})  # never the real ~/.codex
        env.start()
        self.addCleanup(env.stop)
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
                              watch_interval=kw.pop("watch_interval", NO_WATCH),
                              parent_agent=kw.pop("parent_agent", True), **kw)
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

    def test_tools_list_has_six_tools_with_schemas(self):
        c = self.serve(Client([init(1), json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n",
                               req(2, "tools/list")]))
        tools = {t["name"]: t for t in c.response(2)["result"]["tools"]}
        self.assertEqual(set(tools), {"list_sessions", "send", "new_session", "dispatch_result", "cancel_dispatch",
                                      "folder_move"})
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


class ToolsListTest(Base):
    META = {"anthropic/requiresUserInteraction": True}

    def test_tools_list_marks_acting_tools_when_confirm(self):
        tools = {t["name"]: t for t in mcp_server.tools_list(True)}
        for name in ("send", "new_session", "cancel_dispatch"):
            self.assertIs(tools[name]["_meta"]["anthropic/requiresUserInteraction"], True)
            self.assertEqual(tools[name]["_meta"], self.META)
        for name in ("list_sessions", "dispatch_result"):
            self.assertNotIn("_meta", tools[name])

    def test_tools_list_unmarked_without_confirm(self):
        before = json.dumps(mcp_server.TOOLS, sort_keys=True)
        mcp_server.tools_list(True)
        self.assertEqual(json.dumps(mcp_server.TOOLS, sort_keys=True), before)  # a copy: TOOLS itself is untouched
        for t in mcp_server.tools_list(False):
            self.assertNotIn("_meta", t)

    def test_tools_list_response_follows_config(self):
        for confirm in (True, False):
            with self.subTest(confirm=confirm):
                c = self.serve(Client([init(1), req(2, "tools/list")]), confirm=confirm)
                tools = {t["name"]: t for t in c.response(2)["result"]["tools"]}
                for name, t in tools.items():
                    if confirm and name in mcp_server.ACTING_TOOLS:
                        self.assertEqual(t["_meta"], self.META)
                    else:
                        self.assertNotIn("_meta", t)
                self.assertEqual(set(tools), mcp_server.TOOL_NAMES)
                marked = '"_meta": {"anthropic/requiresUserInteraction": true}' in json.dumps(c.response(2))
                self.assertIs(marked, confirm)


class ParentAgentTest(Base):
    """With confirm on, only a server started by an agent (claude / codex as its direct parent) may act: a shell piping
    JSON-RPC into `sampan mcp serve` and claiming clientInfo claude-code is refused."""
    SHELL = [ps_line(1, 0, "launchd", "launchd"), ps_line(100, 1, "/bin/zsh", "-zsh"),
             ps_line(200, 100, "claude", "claude"), ps_line(300, 200, "/bin/zsh", "zsh -c x"),
             ps_line(400, 300, "sampan", "sampan mcp serve")]

    def ps(self, lines):
        r = FakeRunner(strict=True)
        r.on(["ps", "-A"], cp(out="\n".join(lines) + "\n"))
        return r

    def test_parent_is_agent(self):
        codex = "/Users/u/.codex/packages/app-server-daemon/releases/0.164.0-aarch64-apple-darwin/bin/codex"
        cases = [(300, self.SHELL, False), (200, self.SHELL, True), (999, self.SHELL, False),
                 (500, [ps_line(500, 1, codex, codex + " app-server")], True),
                 (500, [ps_line(500, 1, "/opt/bin/claude", "claude")], True),
                 (500, [ps_line(500, 1, "claude", "claude", stat="Z")], False),
                 (500, [ps_line(500, 1, "node", "node claude")], False)]
        for ppid, lines, want in cases:
            with self.subTest(ppid=ppid, lines=lines):
                self.assertIs(mcp_server.parent_is_agent(self.ps(lines), ppid), want)
        failing = FakeRunner(strict=True)
        failing.on(["ps", "-A"], cp(1))
        self.assertIs(mcp_server.parent_is_agent(failing, 200), False)  # lookup failure: fail closed
        boom = FakeRunner(strict=True)
        boom.on(["ps", "-A"], lambda _a: (_ for _ in ()).throw(OSError("no ps")))
        with mock.patch.object(mcp_server, "_log") as log:
            self.assertIs(mcp_server.parent_is_agent(boom, 200), False)
        self.assertEqual(log.call_count, 1)

    def test_shell_parent_refused_even_as_claude_code(self):
        calls = {"send": {"session": "b", "message": "hi"}, "cancel_dispatch": {"id": "ffff"},
                 "new_session": {"name": "n1", "target": "claude:work", "cwd": self._tmp.name, "message": "x"}}
        run = tmux_runner()
        with mock.patch.object(mcp_server.session_run, "cmd_new", side_effect=AssertionError("no")):
            c = self.serve(Client([init(1, client="claude-code")] + [call(i + 2, n, a) for i, (n, a) in
                                                                     enumerate(calls.items())]),
                           run=run, parent_agent=False)
        for i in range(len(calls)):
            self.assertTrue(c.response(i + 2)["result"]["isError"])
            self.assertEqual(tool_text(c.response(i + 2)), mcp_server.NOT_ASKING)
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(tmux_writes(run), [])

    def test_agent_parent_allowed(self):
        c = self.serve(Client([init(1, client="claude-code"), call(2, "send", {"session": "b", "message": "hi"})]),
                       run=tmux_runner(), parent_agent=True)
        self.assertFalse(c.response(2)["result"]["isError"])

    def test_confirm_off_needs_no_agent_parent(self):
        c = self.serve(Client([init(1, client="other"), call(2, "send", {"session": "b", "message": "hi"})]),
                       run=tmux_runner(), confirm=False, parent_agent=False)
        self.assertFalse(c.response(2)["result"]["isError"])

    def test_read_only_tools_allowed_without_agent_parent(self):
        c = self.serve(Client([init(1, client="other"), call(2, "list_sessions", {})]), parent_agent=False)
        self.assertFalse(c.response(2)["result"]["isError"])

    def test_server_defaults_to_no_agent_parent(self):
        server = mcp_server.Server(self.cfg, FakeRunner(strict=True), io.StringIO(), io.StringIO(), caller=CALLER_A)
        server.client_name = "claude-code"
        self.assertEqual(server._client_asks("send"), mcp_server.NOT_ASKING)


class FailClosedTest(Base):
    ACTING = {"send": {"session": "b", "message": "hi"}, "cancel_dispatch": {"id": "ffff"}}

    def new_args(self):
        d = Path(self._tmp.name) / "w"
        d.mkdir(exist_ok=True)
        return {"name": "n1", "target": "claude:work", "cwd": str(d), "message": "x"}

    def test_unknown_client_is_refused_for_every_acting_tool(self):
        for client in ("other", None):
            with self.subTest(client=client):
                run = tmux_runner()
                calls = {**self.ACTING, "new_session": self.new_args()}
                with mock.patch.object(mcp_server.session_run, "cmd_new", side_effect=AssertionError("no")):
                    c = self.serve(Client([init(1, client=client)] + [call(i + 2, n, a) for i, (n, a) in
                                                                      enumerate(calls.items())]), run=run)
                for i in range(len(calls)):
                    self.assertTrue(c.response(i + 2)["result"]["isError"])
                    self.assertEqual(tool_text(c.response(i + 2)), mcp_server.NOT_ASKING)
                self.assertEqual(self.dispatches(), [])
                self.assertEqual(tmux_writes(run), [])

    def codex_toml(self, text):
        self.cfg.codex.home.mkdir(parents=True, exist_ok=True)
        (self.cfg.codex.home / "config.toml").write_text(text)

    def test_asking_clients_are_allowed(self):
        self.codex_toml(codex_approval.with_prompt('[mcp_servers.sampan]\ncommand = "sampan"\n'))
        for client in ("claude-code", "codex-mcp-client"):
            with self.subTest(client=client):
                c = self.serve(Client([init(1, client=client), call(2, "send", {"session": "b", "message": "hi"})]),
                               run=tmux_runner())
                self.assertFalse(c.response(2)["result"]["isError"])

    def test_codex_without_prompt_tables_is_refused(self):
        installed = '[mcp_servers.sampan]\ncommand = "sampan"\n'
        approve_send = codex_approval.with_prompt(installed).replace(
            '[mcp_servers.sampan.tools.send]\napproval_mode = "prompt"', '[mcp_servers.sampan.tools.send]\napproval_mode = "approve"')
        for name, text in (("missing", None), ("not set", installed), ("always allow", approve_send), ("broken", "[x")):
            with self.subTest(name):
                path = self.cfg.codex.home / "config.toml"
                path.unlink(missing_ok=True)
                if text is not None:
                    self.codex_toml(text)
                run = tmux_runner()
                calls = {**self.ACTING, "new_session": self.new_args()}
                with mock.patch.object(mcp_server.session_run, "cmd_new", side_effect=AssertionError("no")):
                    c = self.serve(Client([init(1, client="codex-mcp-client")] + [
                        call(i + 2, n, a) for i, (n, a) in enumerate(calls.items())]), run=run)
                for i, tool in enumerate(calls):
                    refused = text is not approve_send or tool == "send"
                    resp = c.response(i + 2)
                    if refused:
                        self.assertTrue(resp["result"]["isError"], tool)
                        self.assertEqual(tool_text(resp), mcp_server.CODEX_NOT_PROMPT.format(path=path))
                    else:
                        self.assertNotIn("沒設成每次詢問", tool_text(resp))
                self.assertEqual(self.dispatches(), [])  # send never went out
                if text is not approve_send:
                    self.assertEqual(tmux_writes(run), [])

    def test_codex_check_reads_the_codex_home_codex_runs_with(self):
        prompt = codex_approval.with_prompt('[mcp_servers.sampan]\ncommand = "sampan"\n')
        other = Path(self._tmp.name) / "other-codex"
        other.mkdir()
        send_b = call(2, "send", {"session": "b", "message": "hi"})
        self.codex_toml(prompt)  # sampan's configured home says prompt, the one Codex runs with does not
        (other / "config.toml").write_text('[mcp_servers.sampan]\ncommand = "sampan"\n')
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(other)}):
            c = self.serve(Client([init(1, client="codex-mcp-client"), send_b]), run=tmux_runner())
        self.assertEqual(tool_text(c.response(2)), mcp_server.CODEX_NOT_PROMPT.format(path=other / "config.toml"))
        self.codex_toml('[mcp_servers.sampan]\ncommand = "sampan"\n')  # and the reverse: refused, naming sampan's home
        (other / "config.toml").write_text(prompt)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(other)}):
            c = self.serve(Client([init(1, client="codex-mcp-client"), send_b]), run=tmux_runner())
        self.assertEqual(tool_text(c.response(2)),
                         mcp_server.CODEX_NOT_PROMPT.format(path=self.cfg.codex.home / "config.toml"))

    def test_codex_check_without_codex_home_uses_dot_codex_in_home(self):
        prompt = codex_approval.with_prompt('[mcp_servers.sampan]\ncommand = "sampan"\n')
        self.codex_toml(prompt)
        home = Path(self._tmp.name) / "fakehome"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text('[mcp_servers.sampan]\ncommand = "sampan"\n')
        with mock.patch.dict("os.environ"), mock.patch("pathlib.Path.home", return_value=home):
            os.environ.pop("CODEX_HOME", None)
            c = self.serve(Client([init(1, client="codex-mcp-client"), call(2, "send", {"session": "b", "message": "hi"})]),
                           run=tmux_runner())
        self.assertEqual(tool_text(c.response(2)), mcp_server.CODEX_NOT_PROMPT.format(path=home / ".codex" / "config.toml"))
        (home / ".codex" / "config.toml").write_text(prompt)
        with mock.patch.dict("os.environ"), mock.patch("pathlib.Path.home", return_value=home):
            os.environ.pop("CODEX_HOME", None)
            c = self.serve(Client([init(1, client="codex-mcp-client"), call(2, "send", {"session": "b", "message": "hi"})]),
                           run=tmux_runner())
        self.assertFalse(c.response(2)["result"]["isError"])

    def test_codex_needs_prompt_in_both_homes(self):
        prompt = codex_approval.with_prompt('[mcp_servers.sampan]\ncommand = "sampan"\n')
        other = Path(self._tmp.name) / "env-codex"
        other.mkdir()
        (other / "config.toml").write_text(prompt)
        send_b = call(2, "send", {"session": "b", "message": "hi"})
        self.codex_toml(prompt)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(other)}):
            c = self.serve(Client([init(1, client="codex-mcp-client"), send_b]), run=tmux_runner())
        self.assertFalse(c.response(2)["result"]["isError"])  # both prompt
        self.codex_toml('[mcp_servers.sampan]\ncommand = "sampan"\n')
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(other)}):
            c = self.serve(Client([init(1, client="codex-mcp-client"), send_b]), run=tmux_runner())
        self.assertEqual(tool_text(c.response(2)),
                         mcp_server.CODEX_NOT_PROMPT.format(path=self.cfg.codex.home / "config.toml"))

    def test_codex_same_home_is_checked_once(self):
        self.codex_toml(codex_approval.with_prompt('[mcp_servers.sampan]\ncommand = "sampan"\n'))
        with mock.patch.object(codex_approval, "tool_prompts", wraps=codex_approval.tool_prompts) as tp:
            c = self.serve(Client([init(1, client="codex-mcp-client"), call(2, "send", {"session": "b", "message": "hi"})]),
                           run=tmux_runner())
        self.assertFalse(c.response(2)["result"]["isError"])
        self.assertEqual(tp.call_count, 1)

    def test_codex_read_only_tools_and_confirm_off_need_no_tables(self):
        c = self.serve(Client([init(1, client="codex-mcp-client"), call(2, "list_sessions", {})]))
        self.assertFalse(c.response(2)["result"]["isError"])
        c = self.serve(Client([init(1, client="codex-mcp-client"), call(2, "send", {"session": "b", "message": "hi"})]),
                       run=tmux_runner(), confirm=False)
        self.assertFalse(c.response(2)["result"]["isError"])

    def test_confirm_off_needs_no_asking_client(self):
        c = self.serve(Client([init(1, client="other"), call(2, "send", {"session": "b", "message": "hi"})]),
                       run=tmux_runner(), confirm=False)
        self.assertFalse(c.response(2)["result"]["isError"])

    def test_read_only_tools_are_allowed_for_an_unknown_client(self):
        c = self.serve(Client([init(1, client="other"), call(2, "list_sessions", {}),
                               call(3, "dispatch_result", {"id": "ffff"})]))
        self.assertFalse(c.response(2)["result"]["isError"])
        self.assertNotIn(mcp_server.NOT_ASKING, tool_text(c.response(3)))


class ActingToolTest(Base):
    def test_send_never_sends_elicitation(self):
        for elicitation in (True, False):
            with self.subTest(elicitation=elicitation):
                for d in self.dispatches():
                    send.cancel(self.cfg, d.id, now=NOW)
                run = tmux_runner()
                c = self.serve(Client([init(1, elicitation=elicitation),
                                       call(2, "send", {"session": "b", "message": "原本的\n訊息"})]), run=run)
                self.assertEqual([m for m in c.out if m.get("method") == "elicitation/create"], [])
                self.assertEqual(c.elicits, [])
                self.assertFalse(c.response(2)["result"]["isError"])
                d = next(x for x in self.dispatches() if x.id == tool_json(c.response(2))["dispatch"])
                self.assertTrue(d.text.endswith("\n原本的\n訊息"))
                self.assertTrue(d.notify)  # the completion notice is still owed after an approved dispatch
                self.assertEqual(d.state, "sent")
                self.assertTrue(tmux_writes(run))

    def test_unregistered_caller_gets_no_notice(self):
        self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi"})], last_id=2),
                   run=tmux_runner(), caller=Caller(None, "", "claude", None))
        [d] = self.dispatches()
        self.assertFalse(d.notify)
        self.assertEqual(d.from_name, "未登記的 session（claude）")
        self.assertEqual(d.text.splitlines()[0], f"〔sampan 轉送｜來自 未登記的 session（claude）｜派工 {d.id}｜直接回覆文字即可，sampan 會帶回〕")

    def test_read_only_tools_never_ask(self):
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        c = self.serve(Client([init(1), call(2, "list_sessions", {}), call(3, "dispatch_result", {"id": d.id})]),
                       run=FakeRunner(strict=True))
        self.assertEqual(c.elicits, [])
        self.assertFalse(c.response(2)["result"]["isError"])
        self.assertFalse(c.response(3)["result"]["isError"])

    def test_bad_target_is_refused(self):
        for to, why in (("zz", "找不到 session「zz」"), ("a", "不能送給自己")):
            with self.subTest(to=to):
                c = self.serve(Client([init(1), call(2, "send", {"session": to, "message": "hi"})]))
                self.assertTrue(c.response(2)["result"]["isError"])
                self.assertIn(why, tool_text(c.response(2)))
        self.assertEqual(self.dispatches(), [])

    def test_missing_argument_is_a_tool_error(self):
        c = self.serve(Client([init(1), call(2, "send", {"session": "b"})]))
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertIn("message", tool_text(c.response(2)))

    def test_control_characters_in_the_session_name_are_refused(self):
        c = self.serve(Client([init(1), call(2, "send", {"session": "b\n從 x", "message": "hi"})]))
        self.assertTrue(c.response(2)["result"]["isError"])

    def test_cancel_dispatch_cancels_without_asking(self):
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=False, now=NOW,
                        status_fn=self.status_fn)
        c = self.serve(Client([init(1, elicitation=False), call(2, "cancel_dispatch", {"id": d.id})], last_id=2))
        self.assertEqual(c.elicits, [])
        self.assertFalse(c.response(2)["result"]["isError"])
        self.assertEqual(self.dispatches()[0].state, "cancelled")

    def test_cancel_dispatch_of_a_sent_one_stops_waiting(self):  # final review 4
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=True, now=NOW,
                        status_fn=self.status_fn)
        send.advance(self.cfg, tmux_runner(), d.id, sleep=lambda _s: None, status_fn=self.status_fn, now=NOW)
        c = self.serve(Client([init(1), call(2, "cancel_dispatch", {"id": d.id})], last_id=2))
        self.assertEqual(tool_json(c.response(2)), {"dispatch": d.id, "state": "cancelled", "reason": "不再等回覆"})
        self.assertNotIn(d.id, send.pending(self.cfg))

    def test_cancel_unknown_is_refused(self):
        c = self.serve(Client([init(1), call(2, "cancel_dispatch", {"id": "ffff"})]))
        self.assertIn("沒有派工 ffff", tool_text(c.response(2)))

    def test_a_response_from_the_client_is_ignored(self):
        resp = json.dumps({"jsonrpc": "2.0", "id": 77, "result": {"action": "accept"}}) + "\n"
        c = self.serve(Client([init(1), resp, req(2, "ping")]))
        self.assertEqual(c.response(2)["result"], {})
        self.assertEqual([m for m in c.out if m.get("id") == 77], [])



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
        self.assertEqual(rows["b"], {"name": "b", "agent": "claude", "profile": "alice", "cwd": "/w/b", "folder": "",
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

    def test_send_failing_in_its_first_round_is_reported_once(self):  # re-review: no second failure notice
        run = tmux_runner().on(["tmux", "-u", "send-keys"], cp(1, "", "lost pane"))  # pasted, Enter failed
        c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi"})]), run=run, confirm=False)
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertIn("失敗", tool_text(c.response(2)))
        [d] = self.dispatches()
        self.assertEqual((d.state, d.notify, d.notified), ("failed", False, False))
        self.assertEqual([w for w in tmux_writes(run) if "%1" in w], [])  # nothing pasted into the caller's pane
        self.assertNotIn(d.id, send.pending(self.cfg))
        run2 = tmux_runner()
        server = mcp_server.Server(self.cfg, run2, io.StringIO(), io.StringIO(), caller=CALLER_A,
                                   sleep=lambda _s: None, clock=lambda: 0.0, status_fn=self.status_fn,
                                   now_fn=lambda: NOW)
        server.mine.add(d.id)
        server.watch_once()  # a later watcher round sends nothing either
        self.assertEqual(run2.calls, [])

    def test_codex_caller_with_a_queue_timeout_gets_no_notice(self):
        self.rows["x"] = status_row("x", agent="codex", profile="codex", sid="sx", pane="%5")
        run = FakeRunner(strict=True).on(["codex", "queue"], cp(124, "", "timeout"))
        c = self.serve(Client([init(1), call(2, "send", {"session": "c", "message": "hi"})]), run=run, confirm=False,
                       caller=Caller("x", "codex", "codex", "%5"))
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertIn("可能已送出", tool_text(c.response(2)))
        [d] = self.dispatches()
        self.assertEqual((d.state, d.notify), ("failed", False))
        self.assertEqual([x["args"][3] for x in run.called(["codex", "queue"])], ["sc"])  # only the dispatch itself

    def test_dispatch_result_of_a_failed_one_turns_its_notice_off(self):  # final review 6
        d = send.submit(self.cfg, FakeRunner(strict=True), CALLER_A, "b", "hi", notify=True, now=NOW,
                        status_fn=self.status_fn)
        send.advance(self.cfg, tmux_runner(), d.id, sleep=lambda _s: None, status_fn=self.status_fn, now=NOW)
        self.rows["b"] = dataclasses.replace(self.rows["b"], state=State.STOPPED, agent_status="-", pane=None)
        self.rows["a"] = dataclasses.replace(self.rows["a"], agent_status="busy")  # no notice yet
        c = self.serve(Client([init(1), call(2, "dispatch_result", {"id": d.id})]), run=FakeRunner(strict=True))
        body = tool_json(c.response(2))
        self.assertEqual(body["state"], "failed")
        self.assertIn("停止", body["reason"])
        self.assertFalse(self.dispatches()[0].notify)  # the caller has read it: no failure notice
        self.assertNotIn(d.id, send.pending(self.cfg))

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


def new_runner():
    run = FakeRunner(strict=True)
    run.on(["tmux", "-u", "has-session"], cp(rc=1))
    return run


class NewSessionToolTest(Base):
    def setUp(self):
        super().setUp()
        self.cwd = Path(self._tmp.name) / "w" / "n1"
        self.cwd.mkdir(parents=True)

    def args(self, **kw):
        return {"name": "n1", "target": "claude:work", "cwd": str(self.cwd) + "/./", "message": "開工", **kw}

    def test_new_session_creates_without_asking_and_returns_the_dispatch(self):
        seen = {}

        def fake_new(cfg, run, out, name, target, cwd, *, message=None, caller=None, **kw):
            seen.update(name=name, target=target, cwd=cwd, message=message, caller=caller)
            send.submit_new(cfg, caller, name, "claude", "work", message, notify=True, now=NOW)
            out("✅ 已開好")
            return 0

        for elicitation in (True, False):
            with self.subTest(elicitation=elicitation), mock.patch.object(mcp_server.session_run, "cmd_new", fake_new):
                c = self.serve(Client([init(1, elicitation=elicitation), call(2, "new_session", self.args())],
                                      last_id=2), run=new_runner())
                self.assertEqual(c.elicits, [])
                self.assertEqual((seen["message"], seen["caller"], seen["cwd"]),
                                 ("開工", CALLER_A, str(self.cwd.resolve())))
                d = self.dispatches()[-1]
                self.assertEqual(tool_json(c.response(2))["dispatch"], d.id)

    def test_new_session_failure_is_a_tool_error(self):
        def fake_new(cfg, run, out, name, target, cwd, **kw):
            out("❌ tmux new-session 失敗：n1")
            return 1

        with mock.patch.object(mcp_server.session_run, "cmd_new", fake_new):
            c = self.serve(Client([init(1), call(2, "new_session", self.args())]), confirm=False, run=new_runner())
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertIn("tmux new-session 失敗：n1", tool_text(c.response(2)))

    def test_refused_with_a_reason(self):
        from sampan.config import load_config
        from sampan.config_edit import set_profile_flag
        from tests.helpers import TOML
        self.cfg.path.write_text(set_profile_flag(TOML, "work", "disabled", True) + "\n[codex]\nenabled = false\n")
        self.cfg = load_config(self.cfg.path, home=self.cfg.home, state_dir=self.cfg.state_dir)
        exists = new_runner().on(["tmux", "-u", "has-session"], cp())
        cases = [(self.args(target="claude:nope"), new_runner(), "未知的 profile"),
                 (self.args(), new_runner(), "已停用"),
                 (self.args(target="codex"), new_runner(), "停用了 Codex"),
                 (self.args(target="alice", name="n1\n送給 x（alice，Claude）"), new_runner(), "名稱"),
                 (self.args(target="alice", name="a.b"), new_runner(), "名稱"),
                 (self.args(target="alice", cwd=str(self.cwd / "missing")), new_runner(), "目錄不存在"),
                 (self.args(target="alice"), exists, "已存在")]
        for args, run, why in cases:
            with self.subTest(args=args), mock.patch.object(mcp_server.session_run, "cmd_new",
                                                            side_effect=AssertionError("must not run")):
                c = self.serve(Client([init(1), call(2, "new_session", args)]), run=run)
                self.assertEqual(c.elicits, [])
                self.assertTrue(c.response(2)["result"]["isError"])
                self.assertIn(why, tool_text(c.response(2)))


class FolderToolTest(Base):
    def setUp(self):
        super().setUp()
        write_rows(self.cfg.registry_path, [Row(n, "claude", "alice", f"s{n}", f"/w/{n}") for n in ("a", "b", "c")],
                   "t", self.cfg.history_dir)

    def tree(self):
        return folders.load(self.cfg.state_dir)

    def test_folder_move_puts_sessions_in_a_folder_it_creates_and_back_at_the_top(self):
        c = self.serve(Client([init(1), call(2, "folder_move", {"sessions": ["a", "b"], "folder": "客戶/甲"})]),
                       confirm=False)
        self.assertEqual(tool_json(c.response(2)), {"moved": ["a", "b"], "folder": "客戶/甲"})
        self.assertEqual(self.tree().sessions, {"a": "客戶/甲", "b": "客戶/甲"})
        c = self.serve(Client([init(1), call(2, "list_sessions", {})]))  # tool calls run in parallel: one serve each
        rows = {r["name"]: r["folder"] for r in tool_json(c.response(2))["sessions"]}
        self.assertEqual(rows, {"a": "客戶/甲", "b": "客戶/甲", "c": ""})
        c = self.serve(Client([init(1), call(2, "folder_move", {"sessions": ["a"], "folder": "/"})]), confirm=False)
        self.assertEqual(tool_json(c.response(2))["folder"], "")
        self.assertEqual(self.tree().sessions, {"b": "客戶/甲"})

    def test_folder_move_refuses_and_writes_nothing(self):
        cases = [({"sessions": ["a", "nope"], "folder": "x"}, "找不到 session nope"),
                 ({"sessions": [], "folder": "x"}, "sessions"),
                 ({"sessions": "a", "folder": "x"}, "sessions"),
                 ({"sessions": ["a"], "folder": "x/a\x07b"}, "控制字元"),
                 ({"sessions": ["a"]}, "folder")]
        for args, why in cases:
            with self.subTest(args=args):
                c = self.serve(Client([init(1), call(2, "folder_move", args)]), confirm=False)
                self.assertTrue(c.response(2)["result"]["isError"])
                self.assertIn(why, tool_text(c.response(2)))
                self.assertFalse((self.cfg.state_dir / folders.FOLDERS_FILE).exists())

    def test_folder_move_never_writes_over_a_broken_file(self):
        (self.cfg.state_dir / folders.FOLDERS_FILE).write_text("{broken")
        c = self.serve(Client([init(1), call(2, "folder_move", {"sessions": ["a"], "folder": "x"})]), confirm=False)
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertEqual((self.cfg.state_dir / folders.FOLDERS_FILE).read_text(), "{broken")
        c = self.serve(Client([init(1), call(2, "list_sessions", {})]))
        body = tool_json(c.response(2))  # still lists the sessions, says why the folders are missing
        self.assertEqual({r["folder"] for r in body["sessions"]}, {""})
        self.assertIn("folders.json", body["warning"])

    def test_folder_move_is_an_acting_tool(self):
        self.assertIn("folder_move", mcp_server.ACTING_TOOLS)

    def test_new_session_passes_the_folder_on_and_checks_it_first(self):
        cwd = Path(self._tmp.name) / "w" / "n1"
        cwd.mkdir(parents=True)
        seen = {}

        def fake_new(cfg, run, out, name, target, cwd, *, message=None, caller=None, folder=None, **kw):
            seen["folder"] = folder
            send.submit_new(cfg, caller, name, "claude", "work", message, notify=True, now=NOW)
            return 0

        args = {"name": "n1", "target": "claude:work", "cwd": str(cwd), "message": "開工"}
        with mock.patch.object(mcp_server.session_run, "cmd_new", fake_new):
            self.serve(Client([init(1), call(2, "new_session", {**args, "folder": "客戶甲"})], last_id=2),
                       run=new_runner(), confirm=False)
            self.assertEqual(seen["folder"], "客戶甲")
            seen.clear()
            self.serve(Client([init(1), call(2, "new_session", args)], last_id=2), run=new_runner(), confirm=False)
            self.assertIsNone(seen["folder"])
        with mock.patch.object(mcp_server.session_run, "cmd_new", side_effect=AssertionError("must not run")):
            c = self.serve(Client([init(1), call(2, "new_session", {**args, "folder": "a/\x07"})]), run=new_runner(),
                           confirm=False)
        self.assertTrue(c.response(2)["result"]["isError"])
        self.assertIn("控制字元", tool_text(c.response(2)))


class NewSessionTrustTest(Base):
    """MCP new_session in a directory Claude has not trusted (派工 0516): it used to sit at the prompt and say success."""

    def setUp(self):
        super().setUp()
        self.cwd = Path(self._tmp.name) / "w" / "n1"
        self.cwd.mkdir(parents=True)
        self.args = {"name": "n1", "target": "claude:work", "cwd": str(self.cwd), "message": "開工"}

    def fake_new(self, stops_at=None):
        seen = {}

        def fake(cfg, run, out, name, target, cwd, *, message=None, caller=None, on_prompt=None, trust=False, **kw):
            seen.update(on_prompt=on_prompt, trust=trust)
            send.submit_new(cfg, caller, name, "claude", "work", message, notify=True, now=NOW)
            if stops_at is not None:
                from sampan.prompts import SKIP
                self.assertEqual(on_prompt(name, "claude", stops_at), SKIP)  # no one to ask: never a key
                out(f"⚠ {name} 停在提示，進去處理")
                return 1
            return 0
        return fake, seen

    def test_confirm_lets_new_trust_its_own_directory(self):
        for confirm in (True, False):
            with self.subTest(confirm=confirm):
                fake, seen = self.fake_new()
                with mock.patch.object(mcp_server.session_run, "cmd_new", fake):
                    c = self.serve(Client([init(1), call(2, "new_session", self.args)], last_id=2), run=new_runner(),
                                   confirm=confirm, parent_agent=True)
                self.assertFalse(c.response(2)["result"]["isError"], tool_text(c.response(2)))
                self.assertIs(seen["trust"], confirm)
                self.assertIsNotNone(seen["on_prompt"])

    def test_stuck_at_a_prompt_is_an_error_that_says_where(self):
        from sampan.screens import Prompt
        p = Prompt("claude", "Quick safety check", ("Yes, I trust this folder", "No, exit"), 0, (),
                   trust_path="/elsewhere")
        fake, _ = self.fake_new(stops_at=p)
        with mock.patch.object(mcp_server.session_run, "cmd_new", fake):
            c = self.serve(Client([init(1), call(2, "new_session", self.args)], last_id=2), run=new_runner(),
                           confirm=False)
        self.assertTrue(c.response(2)["result"]["isError"])
        text = tool_text(c.response(2))
        d = self.dispatches()[-1]
        self.assertIn("/elsewhere", text)
        self.assertIn("tmux attach -t n1", text)
        self.assertIn(d.id, text)  # the opening message waits in the window; dispatch_result still works
        c = self.serve(Client([init(1), call(2, "dispatch_result", {"id": d.id})]))
        self.assertFalse(c.response(2)["result"]["isError"], tool_text(c.response(2)))


class OwnershipTest(Base):
    def other(self):
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        return send.submit(self.cfg, FakeRunner(strict=True), Caller("c", "codex", "codex", "%3"), "b", "secret",
                           notify=False, now=NOW, status_fn=self.status_fn)

    def test_dispatch_result_and_cancel_of_another_callers_dispatch_are_refused(self):
        d = self.other()
        with mock.patch.object(send, "advance", side_effect=AssertionError("must not advance")):
            c = self.serve(Client([init(1), call(2, "dispatch_result", {"id": d.id}),
                                   call(3, "cancel_dispatch", {"id": d.id})]))
        for rid in (2, 3):
            self.assertTrue(c.response(rid)["result"]["isError"])
            self.assertNotIn("secret", tool_text(c.response(rid)))
        self.assertEqual(tool_text(c.response(2)), tool_text(c.response(3)))
        self.assertIn(d.id, tool_text(c.response(2)))
        self.assertEqual(c.elicits, [])
        self.assertEqual(self.dispatches()[0].state, "queued")

    def test_a_session_named_like_a_reserved_label_owns_nothing_of_theirs(self):
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        term = send.submit(self.cfg, FakeRunner(strict=True), send.TERMINAL, "b", "t", notify=False, now=NOW,
                           status_fn=self.status_fn)
        unreg = send.submit(self.cfg, FakeRunner(strict=True), Caller(None, "", "claude", None), "b", "u",
                            notify=False, now=NOW, status_fn=self.status_fn)
        for name, d in (("終端機", term), ("未登記的 session（claude）", unreg)):
            with self.subTest(name=name), mock.patch.object(send, "advance", side_effect=AssertionError("no")):
                c = self.serve(Client([init(1), call(2, "dispatch_result", {"id": d.id}),
                                       call(3, "cancel_dispatch", {"id": d.id})]),
                               confirm=False, caller=Caller(name, "alice", "claude", "%9"))
                self.assertTrue(c.response(2)["result"]["isError"])
                self.assertTrue(c.response(3)["result"]["isError"])
        self.assertEqual({d.state for d in self.dispatches()}, {"queued"})

    def test_an_unregistered_caller_sees_only_what_this_server_sent(self):
        d = self.other()
        c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "hi"})],
                              last_id=2), run=tmux_runner(), caller=Caller(None, "", "claude", None))
        mine = tool_json(c.response(2))["dispatch"]
        c = self.serve(Client([init(1), call(3, "dispatch_result", {"id": d.id})]),
                       caller=Caller(None, "", "claude", None))
        self.assertTrue(c.response(3)["result"]["isError"])
        self.assertNotEqual(mine, d.id)


class ResolveTest(Base):
    def test_caller_is_resolved_again_for_each_acting_tool(self):
        callers = iter([Caller(None, "", "codex", None), Caller("a", "alice", "claude", "%1"),
                        Caller("a", "alice", "claude", "%1")])
        c = self.serve(Client([init(1), call(2, "send", {"session": "b", "message": "one"})], last_id=2),
                       run=tmux_runner(), confirm=False, caller=Caller(None, "", "", None), resolve=lambda: next(callers))
        first = self.dispatches()[0]
        self.assertEqual((first.from_name, first.notify), ("未登記的 session（codex）", False))
        self.assertIn("來自 未登記的 session（codex）", first.text)
        self.rows["b"] = dataclasses.replace(self.rows["b"], agent_status="busy")
        c = self.serve(Client([init(1), call(3, "send", {"session": "a", "message": "me"})]),
                       confirm=False, caller=Caller(None, "", "", None), resolve=lambda: next(callers))
        self.assertIn("不能送給自己", tool_text(c.response(3)))
        c = self.serve(Client([init(1), call(4, "send", {"session": "b", "message": "two"})]),
                       confirm=False, caller=Caller(None, "", "", None), resolve=lambda: next(callers))
        second = next(d for d in self.dispatches() if d.id != first.id)
        self.assertEqual((second.from_name, second.notify), ("a", True))

    def test_an_unsure_lookup_keeps_the_known_name(self):
        def unsure():
            raise mcp_server.CallerUnsure("tmux unreadable")

        c = self.serve(Client([init(1), call(2, "send", {"session": "a", "message": "me"})]), confirm=False,
                       resolve=unsure)
        self.assertIn("不能送給自己", tool_text(c.response(2)))

    def test_a_session_without_a_row_drops_to_unregistered(self):
        self.rows["a"] = dataclasses.replace(self.rows["a"], agent_status="busy")
        c = self.serve(Client([init(1), call(2, "send", {"session": "a", "message": "me"})]), confirm=False,
                       resolve=lambda: Caller(None, "", "claude", None))
        self.assertFalse(c.response(2)["result"]["isError"])
        [d] = self.dispatches()
        self.assertEqual((d.from_name, d.from_kind, d.notify), ("未登記的 session（claude）", "unregistered", False))


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
        self.assertEqual(send.lookup_caller(self.cfg, run, 300), send.TERMINAL)  # a command run by a person
        # final review 9: an MCP caller is never the terminal
        c = mcp_server.identify_caller(self.cfg, run, 300)
        self.assertEqual(c, Caller(None, "", "", None, kind="unregistered"))
        self.assertEqual((send.caller_label(c), send.caller_kind(c)), ("未登記的 session", "unregistered"))

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

    def test_lookup_tells_unsure_from_not_registered(self):
        PROCS = self.PROCS
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-panes"], cp(rc=1, err="permission denied"))
        r.on(["ps", "-A"], cp(out="\n".join(PROCS) + "\n"))
        with self.assertRaises(mcp_server.CallerUnsure):
            mcp_server.lookup_caller(self.cfg, r, 300)
        r = FakeRunner(strict=True).on(["ps", "-A"], cp(rc=1))
        with self.assertRaises(mcp_server.CallerUnsure):
            mcp_server.lookup_caller(self.cfg, r, 300)
        self.cfg.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.registry_path.write_text("{broken")
        run = self.runner([pane_line("1005-x", "%5", 100, "claude", "/w")], PROCS)
        with self.assertRaises(mcp_server.CallerUnsure):
            mcp_server.lookup_caller(self.cfg, run, 300)
        self.cfg.registry_path.unlink()
        self.assertEqual(mcp_server.lookup_caller(self.cfg, run, 300), Caller(None, "", "claude", None))

    def test_unreadable_tmux_is_unregistered(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-panes"], cp(rc=1, err="permission denied"))
        r.on(["ps", "-A"], cp(out="\n".join(self.PROCS) + "\n"))
        self.assertEqual(mcp_server.identify_caller(self.cfg, r, 300), Caller(None, "", "claude", None, kind="unregistered"))

    def test_unreadable_ps_at_startup_is_unregistered_not_the_terminal(self):  # final review 9
        r = FakeRunner(strict=True).on(["ps", "-A"], cp(rc=1))
        c = mcp_server.identify_caller(self.cfg, r, 300)
        self.assertEqual(send.caller_kind(c), "unregistered")
        self.assertEqual(send.caller_label(c), "未登記的 session")


if __name__ == "__main__":
    unittest.main()
