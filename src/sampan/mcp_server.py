"""`sampan mcp serve`: a stdio MCP server that lets an agent use sampan's send core as tools.

Interface layer. The transport is newline-delimited JSON-RPC 2.0: one message per line, UTF-8 in, ASCII-only JSON out
(stdout carries protocol messages and nothing else). The main thread reads stdin; each tools/call runs in its own
thread, because a tool may wait for the user's approval (an `elicitation/create` request this server sends with its
own id, answered by the client among other messages) or for a reply. A background thread advances this caller's
pending dispatches every 3 seconds while the server is alive. Every write to stdout holds one lock.
"""
import json
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from . import dispatch, send, session_run
from .registry import RegistryError, load_rows
from .send import Caller, CallerUnsure, lookup_caller  # noqa: F401  (CallerUnsure, lookup_caller re-exported)
from .status import status_rows
from .update import current_version

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")  # the first is answered when the client asks for another one
WATCH_S = 3
WAIT_MAX_S = 60  # a tool call may not block longer than this (MCP clients time tool calls out themselves)
ELICIT_TIMEOUT_S = 600
SEPARATOR = "──── 訊息內容（由 agent 提供）────"  # everything below it in a form is the agent's text
NOTICE = "完成或失敗後會在你的 session 送出一則通知"
NO_ELICITATION = "這個 client 不支援核准表單；要用請在 config 設 [mcp] confirm = false"
DECLINED = "使用者拒絕"
AGENT_LABELS = {"claude": "Claude", "codex": "Codex"}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _str(desc):
    return {"type": "string", "description": desc}


TOOLS = [
    {"name": "list_sessions",
     "description": "列出 sampan 登記的 session：名稱、agent、profile、目錄、狀態（running 才收得到訊息）、忙／閒（activity）、最後活動。唯讀。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "send",
     "description": "傳一則訊息給另一個執行中的 sampan session（對方閒著才送，忙就排隊）。回傳派工編號；"
                    "wait=true 時最多等 60 秒，做完就連回覆一起回。沒等到的，完成或失敗時會在你的 session 送出一則通知，"
                    "或用 dispatch_result 查。",
     "inputSchema": {"type": "object", "properties": {
         "session": _str("對方的 session 名稱（list_sessions 看）"),
         "message": _str("要送的訊息"),
         "wait": {"type": "boolean", "description": "等回覆（最多 60 秒）", "default": False}},
         "required": ["session", "message"]}},
    {"name": "new_session",
     "description": "開一個新的 sampan session，並把 message 當作它的第一個 prompt。回傳派工編號，之後用 dispatch_result 看回覆。",
     "inputSchema": {"type": "object", "properties": {
         "name": _str("新 session 的名稱（不能含 . 或 :）"),
         "target": _str("claude:<profile>、<profile>（當成 claude）或 codex"),
         "cwd": _str("工作目錄"),
         "message": _str("開場訊息")},
         "required": ["name", "target", "cwd", "message"]}},
    {"name": "dispatch_result",
     "description": "看一筆派工的狀態（queued／sent／done／failed／cancelled）、回覆與原因；wait 秒數內（最多 60）等它做完。唯讀。",
     "inputSchema": {"type": "object", "properties": {
         "id": _str("派工編號"),
         "wait": {"type": "integer", "description": "最多等幾秒（0–60）", "minimum": 0, "maximum": WAIT_MAX_S,
                  "default": 0}},
         "required": ["id"]}},
    {"name": "cancel_dispatch",
     "description": "取消一筆還沒送出的（排隊中）派工；已送出、等回覆的，改成不再等它的回覆（訊息已在對方那裡，收不回）。",
     "inputSchema": {"type": "object", "properties": {"id": _str("派工編號")}, "required": ["id"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def _utcnow():
    return datetime.now(timezone.utc)


def _log(msg):
    print(f"sampan mcp: {msg}", file=sys.stderr, flush=True)


class ToolError(Exception):
    """A refusal returned to the agent as an isError tool result (not a JSON-RPC error)."""


def mcp_caller(caller):
    """An MCP caller is an agent, never a person at the terminal: no agent process found above the server still
    makes it an unregistered caller."""
    return replace(caller, kind="unregistered") if send.caller_kind(caller) == "terminal" else caller


def identify_caller(cfg, run, pid):
    """lookup_caller, with an unsure lookup taken as an unregistered caller (what the server starts with)."""
    try:
        return mcp_caller(lookup_caller(cfg, run, pid))
    except CallerUnsure as e:
        _log(f"認不出呼叫者：{e.args[0] if e.args else e}")
        return send.unsure_caller(e)


def _who(caller):
    label = send.caller_label(caller)
    return f"{label}（{caller.profile}）" if caller.name is not None and caller.profile else label


def _form(header_lines, body):
    """An approval form: fixed header lines from sampan, then the separator, then the agent's text verbatim."""
    return "\n".join(header_lines) + "\n" + SEPARATOR + "\n" + body


def _ok(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], "isError": False}


def _err(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _arg_str(args, key):
    v = args.get(key)
    if not isinstance(v, str) or not v:
        raise ToolError(f"參數 {key} 要是非空字串")
    return v


class Server:
    def __init__(self, cfg, run, stdin, stdout, *, caller, sleep=time.sleep, clock=time.monotonic, status_fn=None,
                 snapshot_fn=None, now_fn=_utcnow, resolve=None):
        self.cfg, self.run, self.stdin, self.stdout, self.caller = cfg, run, stdin, stdout, caller
        self.resolve = resolve  # () -> Caller, asked again before every tool that acts or reads a dispatch
        self.sleep, self.clock, self.now_fn = sleep, clock, now_fn
        self.status_fn, self.snapshot_fn = status_fn or status_rows, snapshot_fn
        self.elicitation = False
        self.mine = set()  # dispatch ids created by this server (an unregistered caller's have no from_name to match)
        self._write_lock = threading.Lock()
        self._cv = threading.Condition()
        self._next_id = 0
        self._waiting = set()
        self._responses = {}
        self._eof = False
        self._threads = []
        self._stop = threading.Event()

    # --- transport -------------------------------------------------------------------------------------------

    def _write(self, msg):
        line = json.dumps(msg) + "\n"  # ASCII only: whatever stdout's encoding, the bytes are valid UTF-8 JSON
        with self._write_lock:
            try:
                self.stdout.write(line)
                self.stdout.flush()
            except (OSError, ValueError) as e:
                _log(f"寫不出回應：{e}")

    def _result(self, rid, result):
        self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    def _error(self, rid, code, message):
        self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def serve(self, watch_interval=WATCH_S):
        watcher = None
        if watch_interval is not None:
            watcher = threading.Thread(target=self._watch, args=(watch_interval,), daemon=True, name="sampan-mcp-watch")
            watcher.start()
        try:
            while True:
                line = self.stdin.readline()
                if not line:
                    break
                self._handle_line(line)
        finally:
            with self._cv:
                self._eof = True  # approvals still waiting will never be answered
                self._cv.notify_all()
            for t in self._threads:
                t.join()
            self._stop.set()
            if watcher is not None:
                watcher.join()
        return 0

    def _handle_line(self, line):
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8")
            except UnicodeDecodeError:
                self._error(None, PARSE_ERROR, "不是 UTF-8")
                return
        if not line.strip():
            return
        try:
            msg = json.loads(line)
        except ValueError:
            self._error(None, PARSE_ERROR, "不是 JSON")
            return
        if not isinstance(msg, dict):
            self._error(None, INVALID_REQUEST, "要是一個 JSON-RPC 物件")
            return
        method, rid = msg.get("method"), msg.get("id")
        if method is None:
            if "result" in msg or "error" in msg:
                self._take_response(msg)
            else:
                self._error(rid, INVALID_REQUEST, "缺少 method")
            return
        if not isinstance(method, str):
            self._error(rid, INVALID_REQUEST, "method 要是字串")
            return
        if "id" not in msg:
            return  # a notification (notifications/initialized, cancelled, …): nothing to answer
        params = msg.get("params") or {}
        if method == "initialize":
            self._initialize(rid, params)
        elif method == "ping":
            self._result(rid, {})
        elif method == "tools/list":
            self._result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if name not in TOOL_NAMES:
                self._error(rid, INVALID_PARAMS, f"沒有這個工具：{name}")
                return
            args = params.get("arguments") or {}
            t = threading.Thread(target=self._call, args=(rid, name, args), name=f"sampan-mcp-{name}")
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
            t.start()
        else:
            self._error(rid, METHOD_NOT_FOUND, f"不支援的方法：{method}")

    def _initialize(self, rid, params):
        params = params if isinstance(params, dict) else {}
        asked = params.get("protocolVersion")
        caps = params.get("capabilities")
        self.elicitation = isinstance(caps, dict) and isinstance(caps.get("elicitation"), dict)
        self._result(rid, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sampan", "version": current_version()},
            "instructions": "sampan 管理 tmux 裡的 Claude Code／Codex session。用 send 傳訊息給另一個 session，"
                            "用 dispatch_result 看回覆。",
        })

    def _take_response(self, msg):
        rid = msg.get("id")
        with self._cv:
            if rid in self._waiting:
                self._responses[rid] = msg
                self._cv.notify_all()
                return
        _log(f"收到不認得的回應 id {rid!r}")

    def _elicit(self, message, properties, required=()):
        """Ask the user through the client's form. The accepted content (dict); ToolError when not accepted."""
        if not self.elicitation:
            raise ToolError(NO_ELICITATION)
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = list(required)
        with self._cv:
            self._next_id += 1
            rid = self._next_id
            self._waiting.add(rid)
        self._write({"jsonrpc": "2.0", "id": rid, "method": "elicitation/create",
                     "params": {"message": message, "requestedSchema": schema}})
        with self._cv:
            self._cv.wait_for(lambda: rid in self._responses or self._eof, timeout=ELICIT_TIMEOUT_S)
            self._waiting.discard(rid)
            resp = self._responses.pop(rid, None)
        if resp is None:
            raise ToolError(f"{DECLINED}（沒有收到核准表單的回應）")
        if "error" in resp:
            err = resp["error"]
            raise ToolError(f"核准表單失敗：{err.get('message') if isinstance(err, dict) else err}")
        result = resp.get("result") if isinstance(resp.get("result"), dict) else {}
        if result.get("action") != "accept":
            raise ToolError(DECLINED)
        content = result.get("content")
        return content if isinstance(content, dict) else {}

    def _approve_message(self, text, message):
        """With [mcp] confirm: ask, and return the message as the user left it. The field is optional (a client
        need not prefill `default`): empty or absent means unchanged, a non-empty value is the edited message."""
        if not self.cfg.mcp_confirm:
            return message
        content = self._elicit(text, {"message": {"type": "string", "title": "訊息",
                                                  "description": "可以改了再送；留空＝照原訊息送", "default": message}})
        edited = content.get("message")
        return edited if isinstance(edited, str) and edited.strip() else message

    # --- tools -----------------------------------------------------------------------------------------------

    def _call(self, rid, name, args):
        try:
            if not isinstance(args, dict):
                raise ToolError("arguments 要是物件")
            result = _ok(getattr(self, f"tool_{name}")(args))
        except ToolError as e:
            result = _err(str(e))
        except Exception as e:  # a tool must answer; the reason goes back to the agent
            _log(f"{name} 失敗：{e!r}")
            result = _err(f"{name} 失敗：{e}")
        self._result(rid, result)

    def _refresh_caller(self):
        """Look the caller up again (a Codex row is registered only once its thread exists, a session may be adopted,
        renamed or forgotten). Only an unsure lookup (CallerUnsure: tmux, ps or the registry unreadable) keeps the
        identity already known; a definite answer, unregistered included, replaces it."""
        if self.resolve is None:
            return self.caller
        try:
            self.caller = mcp_caller(self.resolve())
        except CallerUnsure as e:
            _log(f"這次認不出呼叫者，沿用 {send.caller_label(self.caller)}：{e.args[0] if e.args else e}")
        return self.caller

    def _own(self, did):
        """The record of a dispatch this caller may see or cancel (sent by this server, or from this caller's
        session); ToolError otherwise, with the same text whether it exists or not."""
        caller = self._refresh_caller()
        try:
            d = next((x for x in dispatch.load(self.cfg.state_dir) if x.id == did), None)
        except dispatch.DispatchError as e:
            raise ToolError(str(e)) from e
        if d is None or not (did in self.mine or send.from_session(d, caller.name)):
            raise ToolError(f"沒有派工 {did}（或不是這個 session 發出的）")
        return d

    def _rows(self):
        try:
            return self.status_fn(self.cfg, self.run, snapshot_fn=self.snapshot_fn)
        except RegistryError as e:
            raise ToolError(str(e)) from e

    def _advance(self, did):
        try:
            return send.advance(self.cfg, self.run, did, sleep=self.sleep, snapshot_fn=self.snapshot_fn,
                                status_fn=self.status_fn, now=self.now_fn())
        except (dispatch.DispatchError, RegistryError) as e:
            raise ToolError(str(e)) from e

    def _wait(self, did, seconds):
        """Advance every 2 s until the dispatch is finished or `seconds` have passed. The last copy."""
        start = self.clock()
        while True:
            d = self._advance(did)
            if d.state in dispatch.FINISHED or self.clock() - start >= seconds:
                return d
            self.sleep(send.POLL_S)

    def tool_list_sessions(self, args):
        rows = self._rows()
        try:
            cwd = {r.tmux: r.cwd for r in load_rows(self.cfg.registry_path, codex_profile=self.cfg.codex.profile)}
        except RegistryError:
            cwd = {}
        out = {"sessions": [{"name": r.name, "agent": r.agent, "profile": r.profile, "cwd": cwd.get(r.name, ""),
                             "state": r.state.value, "activity": r.agent_status,
                             "last": r.last.astimezone(timezone.utc).isoformat(timespec="seconds") if r.last else None}
                            for r in rows]}
        warning = getattr(rows, "warning", None)
        if warning:
            out["warning"] = warning
        return out

    def tool_send(self, args):
        to, message = _arg_str(args, "session"), _arg_str(args, "message")
        wait = args.get("wait", False)
        if not isinstance(wait, bool):
            raise ToolError("參數 wait 要是 true 或 false")
        if session_run.has_control(to):
            raise ToolError(f"session 名稱不能含換行或控制字元：{to!r}")
        caller = self._refresh_caller()
        if caller.name is not None and caller.name == to:
            raise ToolError(f"不能送給自己（{to}）")
        row = next((r for r in self._rows() if r.name == to), None)
        problem = send.target_problem(row, to)
        if problem:
            raise ToolError(problem)
        header = [f"送給 {to}（{row.profile}，{AGENT_LABELS.get(row.agent, row.agent)}）／從 {_who(caller)}"]
        if caller.name is not None:
            header.append(f"（60 秒內沒回完的話）{NOTICE}" if wait else NOTICE)
        message = self._approve_message(_form(header, message), message)
        # submit checks the target again: it may have stopped while the form was open
        d = send.submit(self.cfg, self.run, caller, to, message, notify=not wait, now=self.now_fn(),
                        snapshot_fn=self.snapshot_fn, status_fn=self.status_fn)
        if isinstance(d, str):
            raise ToolError(d)
        self.mine.add(d.id)
        d = self._wait(d.id, WAIT_MAX_S if wait else 0)
        out = {"dispatch": d.id, "state": d.state}
        if d.state == "done":
            out["reply"] = d.reply or ""
        elif d.reason:
            out["reason"] = d.reason
        if wait and d.state not in dispatch.FINISHED and caller.name is not None:
            send.set_notify(self.cfg, d.id, True)  # nobody has the reply yet: tell the caller when it comes
        if d.state == "failed":
            raise ToolError(f"派工 {d.id} 失敗：{d.reason}")
        return out

    def tool_new_session(self, args):
        name, target = _arg_str(args, "name"), _arg_str(args, "target")
        cwd, message = _arg_str(args, "cwd"), _arg_str(args, "message")
        checked = session_run.check_new(self.cfg, self.run, name, target, cwd)
        if isinstance(checked, str):
            raise ToolError(checked)
        agent, profile, cwd = checked
        caller = self._refresh_caller()
        header = [f"開新 session 並送給 {name}（{profile or self.cfg.codex.profile}，{AGENT_LABELS[agent]}）／從 {_who(caller)}",
                  f"目錄：{cwd}"]
        if caller.name is not None:
            header.append(NOTICE)
        message = self._approve_message(_form(header, message), message)
        try:
            before = {d.id for d in dispatch.load(self.cfg.state_dir)}
        except dispatch.DispatchError as e:
            raise ToolError(str(e)) from e
        lines = []
        rc = session_run.cmd_new(self.cfg, self.run, lines.append, name, target, cwd, message=message,
                                 caller=caller, sleep=self.sleep)  # checks everything again before creating
        said = "\n".join(str(x).removeprefix("❌ ") for x in lines).strip()
        new = [d for d in dispatch.load(self.cfg.state_dir) if d.id not in before and d.to_name == name]
        if not new or rc not in (0, 1):
            raise ToolError(said or f"開不了 session {name}")
        d = new[-1]
        if d.state == "failed":
            raise ToolError(d.reason or said)
        self.mine.add(d.id)
        out = {"dispatch": d.id}
        if said:
            out["note"] = said
        return out

    def tool_dispatch_result(self, args):
        did = _arg_str(args, "id")
        wait = args.get("wait", 0)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= WAIT_MAX_S:
            raise ToolError(f"參數 wait 要是 0 到 {WAIT_MAX_S} 的秒數")
        self._own(did)  # before advancing anything
        d = self._wait(did, wait)
        out = {"dispatch": d.id, "state": d.state, "label": send.STATE_LABELS[d.state], "to": d.to_name,
               "reply": d.reply, "reason": d.reason}
        if d.state in ("done", "failed") and d.notify and send.from_session(d, self.caller.name):
            send.set_notify(self.cfg, d.id, False)  # the caller has the reply now: no notice needed
        return out

    def tool_cancel_dispatch(self, args):
        did = _arg_str(args, "id")
        d = self._own(did)
        refusal = send.cancel_refusal(d)
        if refusal:
            raise ToolError(refusal)
        if self.cfg.mcp_confirm:
            what = "取消派工" if d.state == "queued" else "不再等回覆：派工"
            self._elicit(_form([f"{what} {did}（給 {d.to_name}）／從 {_who(self.caller)}"], d.text), {})
        r = send.cancel(self.cfg, did, now=self.now_fn())
        if isinstance(r, str):
            raise ToolError(r)
        out = {"dispatch": did, "state": r.state}
        if r.reason == send.REASON_STOP_WATCHING:
            out["reason"] = r.reason
        return out

    # --- watcher ---------------------------------------------------------------------------------------------

    def watch_once(self):
        """Advance this caller's pending dispatches (and the ones this server created) once."""
        try:
            owed = set(send.pending(self.cfg))
            items = dispatch.load(self.cfg.state_dir)
        except (dispatch.DispatchError, OSError) as e:
            _log(str(e))
            return
        name = self.caller.name
        for d in items:
            if d.id in owed and (d.id in self.mine or send.from_session(d, name)):
                try:
                    send.advance(self.cfg, self.run, d.id, sleep=self.sleep, snapshot_fn=self.snapshot_fn,
                                 status_fn=self.status_fn, now=self.now_fn())
                except (dispatch.DispatchError, RegistryError, OSError) as e:
                    _log(f"派工 {d.id}：{e}")

    def _watch(self, interval):
        while not self._stop.wait(interval):
            self.watch_once()


def serve(cfg, run, stdin, stdout, *, caller, sleep=time.sleep, clock=time.monotonic, status_fn=None,
          snapshot_fn=None, now_fn=_utcnow, watch_interval=WATCH_S, resolve=None):
    """Serve MCP on stdin/stdout until stdin closes. Returns 0. resolve: () -> Caller, to look the caller up again
    before each tool that acts or reads a dispatch (`caller` is the identity found at start)."""
    return Server(cfg, run, stdin, stdout, caller=caller, sleep=sleep, clock=clock, status_fn=status_fn,
                  snapshot_fn=snapshot_fn, now_fn=now_fn, resolve=resolve).serve(watch_interval)
