"""`sampan mcp serve`: a stdio MCP server that lets an agent use sampan's send core as tools.

Interface layer. The transport is newline-delimited JSON-RPC 2.0: one message per line, UTF-8 in, ASCII-only JSON out
(stdout carries protocol messages and nothing else). The main thread reads stdin; each tools/call runs in its own
thread, because a tool may wait for a reply. The tools that act (ACTING_TOOLS) are not asked about here: with
`[mcp] confirm` on, tools/list marks them `_meta["anthropic/requiresUserInteraction"] = true`, so the agent's own
permission prompt asks the user, even in auto mode. A background thread advances this caller's pending dispatches
every 3 seconds while the server is alive. Every write to stdout holds one lock.
"""
import copy
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import dispatch, folders, heartbeat, ids, inbox, machine, remote, send, send_restore, session_run
from . import hubproto as hp
from .procs import list_procs
from .prompts import SKIP
from .registry import RegistryError, load_rows
from .send import Caller, CallerUnsure, lookup_caller  # noqa: F401  (CallerUnsure, lookup_caller re-exported)
from .status import status_rows, status_rows_light
from .update import current_version

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")  # the first is answered when the client asks for another one
BEAT_S = 30  # the heartbeat is written at most this often (it must stay well under heartbeat.HEARTBEAT_S)
WATCH_S = 3
WAIT_MAX_S = 60  # a tool call may not block longer than this (MCP clients time tool calls out themselves)
INTERACTION_META = "anthropic/requiresUserInteraction"
ACTING_TOOLS = ("send", "send_batch", "new_session", "cancel_dispatch", "folder_move", "resend")
# clientInfo.name of the clients known to ask the user for a tool marked INTERACTION_META (checked 2026-10-27:
# Claude Code 2.1.287 sends "claude-code", Codex 0.164.0 sends "codex-mcp-client")
ASKING_CLIENTS = {"claude-code", "codex-mcp-client"}  # Codex: only with approval_mode = "prompt" (codex_approval)
NOT_ASKING = "這個 client 不會先問使用者；要用請在 config 設 [mcp] confirm = false"
CODEX = "codex-mcp-client"
CLAUDE_CODE = "claude-code"  # its permission prompt shows a tool's whole arguments (the cwd new_session will trust)
# basename of the command of the process that must have started the server (checked 2026-10-27: Claude Code runs
# `claude`, Codex runs `…/.codex/packages/app-server-daemon/releases/<version>/bin/codex`)
AGENT_PARENTS = ("claude", "codex")
CODEX_NOT_PROMPT = "Codex 的 sampan 工具沒設成每次詢問（{path}）：跑 sampan mcp install"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _str(desc):
    return {"type": "string", "description": desc}


TOOLS = [
    {"name": "list_sessions",
     "description": "列出 sampan 登記的 session：名稱、id（完整；其他工具可用名稱或 id 前 8 碼以上指定）、machine（這台機器的 id）、agent、profile、目錄、所在資料夾（folder，\"\" 是最上層）、別名（alias，沒設是 \"\"）、"
                    "狀態（state，舊版的合併顯示值：running 才收得到訊息，retired 的一律是 retired）、registry（登記表怎麼標：active／retired／空字串＝沒登記）、live（實際有沒有在跑：running／stopped／unknown）、忙／閒（activity）、最後活動。頂層另有 machine（id 與名稱）。唯讀。"
                    "remote=true 時另列你自己其他機器上已發布的 session（remote_machines：機器、session、忙閒；要用「機器/session」寫法 send）。",
     "inputSchema": {"type": "object", "properties": {
         "remote": {"type": "boolean", "default": False,
                    "description": "也列出自己其他機器上已發布的 session（來自 hub，要 sampan connect 在跑）"}}}},
    {"name": "send",
     "description": "傳一則訊息給另一個執行中的 sampan session（對方閒著才送，忙就排隊）。回傳派工編號；"
                    "wait=true 時最多等 60 秒，做完就連回覆一起回。沒等到的，完成或失敗時會在你的 session 送出一則通知，"
                    "或用 dispatch_result 查。對方已停止時，restore=true 會先接回它再送（預設不接回）。"
                    "session 也可以寫「機器/session」（例如 laptop/proj-a），送到你自己另一台機器上已發布的 session（經 hub；"
                    "不支援 restore、profile、raw）。",
     "inputSchema": {"type": "object", "properties": {
         "session": _str("對方的 session 名稱或 id（前 8 碼以上；list_sessions 看）"),
         "message": _str("要送的訊息"),
         "wait": {"type": "boolean", "description": "等回覆（最多 60 秒）", "default": False},
         "raw": {"type": "boolean", "default": False,
                 "description": "直接送：不加 sampan 的轉送標頭，讓對方的 Claude 當作指令執行（例如 /compact）；"
                                "只限 Claude，一行、以 / 開頭、最多 500 字"},
         "restore": {"type": "boolean", "default": False,
                     "description": "對方已停止（不是 retired）時，先照 sampan restore 接回同一段對話、等它閒下來再送；接回會多吃記憶體"
                                "（Mac 16G 同時最多接回 2 個，其餘排隊），所以預設 false。retired 的照舊拒絕，要先 sampan activate。"
                                "接回與等待都在背景做，呼叫會立刻回派工編號；接回失敗（停在信任提示、逾時…）派工標 failed 並寫原因。不能跟 raw 一起用"},
         "profile": {"type": "string",
                     "description": "對方已停止、要接回時改用這個 profile（等同接回前先 sampan switch：逐字稿搬過去、登記表永久改成它）；"
                                    "必須搭配 restore=true；只限已停止的 Claude session，執行中、retired、Codex 都拒絕。核准畫面看得到這個參數"}},
         "required": ["session", "message"]}},
    {"name": "send_batch",
     "description": "同一則訊息一次送給多個 session。只送給執行中而且閒著的；忙的、沒在跑的（還有 raw 給 Codex 的）"
                    "不排隊，列在 skipped 附原因（restore=true 時，已停止的不跳過：排隊、接回後再送，同時最多接回 2 個）。回傳批次編號與每一筆派工；wait=true 時最多等 60 秒等整批做完。"
                    "沒等到的，整批做完時會在你的 session 送出一則通知，或用 dispatch_result 帶批次編號查。",
     "inputSchema": {"type": "object", "properties": {
         "sessions": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "對方的 session 名稱或 id（前 8 碼以上；list_sessions 看；不能重複）"},
         "message": _str("要送的訊息"),
         "raw": {"type": "boolean", "default": False,
                 "description": "直接送：不加 sampan 的轉送標頭，讓對方的 Claude 當作指令執行（例如 /compact）；"
                                "只限 Claude，一行、以 / 開頭、最多 500 字"},
         "wait": {"type": "boolean", "description": "等整批做完（最多 60 秒）", "default": False},
         "restore": {"type": "boolean", "default": False,
                     "description": "對方已停止（不是 retired）時，先照 sampan restore 接回同一段對話、等它閒下來再送；接回會多吃記憶體"
                                "（Mac 16G 同時最多接回 2 個，其餘排隊），所以預設 false。retired 的照舊拒絕，要先 sampan activate。"
                                "接回與等待都在背景做，呼叫會立刻回派工編號；接回失敗（停在信任提示、逾時…）派工標 failed 並寫原因。不能跟 raw 一起用"},
         "profile": {"type": "string",
                     "description": "對方已停止、要接回時改用這個 profile（等同接回前先 sampan switch：逐字稿搬過去、登記表永久改成它）；"
                                    "必須搭配 restore=true；只限已停止的 Claude session，執行中、retired、Codex 都拒絕。核准畫面看得到這個參數"}},
         "required": ["sessions", "message"]}},
    {"name": "new_session",
     "description": "開一個新的 sampan session，並把 message 當作它的第一個 prompt。回傳派工編號，之後用 dispatch_result 看回覆。",
     "inputSchema": {"type": "object", "properties": {
         "name": _str("新 session 的名稱（不能含 . 或 :）"),
         "target": _str("claude:<profile>、<profile>（當成 claude）或 codex"),
         "cwd": _str("工作目錄"),
         "message": _str("開場訊息"),
         "folder": _str("選填：放進哪個 sampan 資料夾（例如 客戶/甲），沒有就建立"),
         "model": _str("選填：這個 Claude session 用的模型（--model 的值，例如 opus[1m]、sonnet）；記在登記表、之後每次啟動都帶，"
                       "只影響這個 session，不改 profile 的設定；Codex 不適用；核准畫面看得到")},
         "required": ["name", "target", "cwd", "message"]}},
    {"name": "folder_move",
     "description": "把 session 搬進 sampan 畫面上的資料夾（沒有就建立；folder 給 \"/\" 是搬回最上層）。"
                    "只改畫面上的分組，不影響 session 本身。",
     "inputSchema": {"type": "object", "properties": {
         "sessions": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "要搬的 session 名稱或 id（前 8 碼以上；list_sessions 看）"},
         "folder": _str("目的資料夾路徑，例如 客戶/甲；\"/\" 是最上層")},
         "required": ["sessions", "folder"]}},
    {"name": "dispatch_result",
     "description": "看一筆派工的狀態（queued／sent／done／failed／cancelled）、回覆（最新那段）與原因；wait 秒數內（最多 60）等它做完。"
                    "也列細分狀態（status）、每段回覆（replies）、每次送達（deliveries）、最後一次門鈴（last_ring）、"
                    "和哪些派工一起送（merged_with）。id 給批次編號（b 開頭、5 碼）時看整批。唯讀。",
     "inputSchema": {"type": "object", "properties": {
         "id": _str("派工編號，或 send_batch 的批次編號"),
         "wait": {"type": "integer", "description": "最多等幾秒（0–60）", "minimum": 0, "maximum": WAIT_MAX_S,
                  "default": 0}},
         "required": ["id"]}},
    {"name": "cancel_dispatch",
     "description": "取消一筆還沒送出的（排隊中）派工；已送出、等回覆的，改成不再等它的回覆（訊息已在對方那裡，收不回）。",
     "inputSchema": {"type": "object", "properties": {"id": _str("派工編號")}, "required": ["id"]}},
    {"name": "resend",
     "description": "重送一筆失敗、已取消或已回覆的派工（同一個編號，重新排隊、照常送出）。舊的送達封存、舊的回覆段落留著；"
                    "對方對舊送達的遲到回覆只記成歷史，不算這次的回覆。",
     "inputSchema": {"type": "object", "properties": {"dispatch_id": _str("派工編號")}, "required": ["dispatch_id"]}},
    {"name": "inbox_take",
     "description": "收到 sampan 門鈴時呼叫：一次取走寄給你的所有新訊息（純文字，依時間順序，附收據編號），也列出之前取走、"
                    "還沒回的。全部看完再動手；每則做完用 reply 回一次，那是最終回覆，做完再回，不要用來報進度。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "reply",
     "description": "回覆一則用 inbox_take 取走的訊息。這是給寄件者的最終回覆：做完再回，每則一次，不要用來報進度"
                    "（這一輪結束時，還沒回的會拿你最後一段回答補上）。",
     "inputSchema": {"type": "object", "properties": {
         "dispatch_id": _str("派工編號（inbox_take 列出的）"),
         "text": _str("回覆內容（最多 100,000 字）"),
         "receipt": _str("選填：inbox_take 給的收據編號")},
         "required": ["dispatch_id", "text"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def tools_list(confirm):
    """A copy of TOOLS for tools/list; with confirm, the acting tools carry the mark that makes the agent ask the user."""
    tools = copy.deepcopy(TOOLS)
    if confirm:
        for t in tools:
            if t["name"] in ACTING_TOOLS:
                t["_meta"] = {INTERACTION_META: True}
    return tools


def _running_codex_home():
    """The CODEX_HOME of the Codex that started this server (its child, same environment), not sampan's configured one."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


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


def parent_is_agent(run, ppid):
    """True when process ppid is a live claude / codex: the server was started by an agent, not piped into from a
    shell (where clientInfo could claim anything). Unknown or unreadable → False (fail closed)."""
    try:
        p = list_procs(run).get(ppid)
    except Exception as e:  # noqa: BLE001  a failed lookup refuses, it never allows
        _log(f"讀不到啟動 sampan mcp serve 的行程：{e}")
        return False
    return p is not None and not p.zombie and p.comm in AGENT_PARENTS


def _ok(obj):
    """A tool result: a str as plain text (inbox_take, reply), anything else as JSON."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _arg_str(args, key):
    v = args.get(key)
    if not isinstance(v, str) or not v:
        raise ToolError(f"參數 {key} 要是非空字串")
    return v


def _folder(text):
    """A folder path from a tool argument ("" or "/" is the top level); ToolError for a name folders refuses."""
    path = text.strip().strip("/")
    if path:
        try:
            folders.add(folders.Tree(), path)
        except folders.FoldersError as e:
            raise ToolError(str(e)) from e
    return path


class _Ref:
    """ids.resolve wants rows with .tmux and .id; a status row has .name."""
    def __init__(self, name, id):
        self.tmux, self.id = name, id


def _resolve_ref(rows, ref):
    """The session name that ref (name, full id or unique id prefix) names in rows (status rows or registry rows).
    A ref that matches nothing comes back as typed (the tool's own "not found" follows); an ambiguous one is a
    ToolError with ids.resolve's text."""
    r = ids.resolve([_Ref(getattr(x, "tmux", None) or getattr(x, "name", ""), x.id) for x in rows], ref)
    if isinstance(r, str):
        if r.startswith("找不到"):
            return ref
        raise ToolError(r)
    return r.tmux


def _batch_item(d):
    out = {"session": d.to_name, "dispatch": d.id, "state": d.state}
    if d.state == "done":
        out["reply"] = d.reply or ""
    elif d.reason:
        out["reason"] = d.reason
    return out


def _profile_arg(args):
    """The optional `profile` of send / send_batch: a non-empty string, or "" when not given (ToolError otherwise)."""
    profile = args.get("profile", "")
    if profile is None:
        return ""
    if not isinstance(profile, str) or profile != profile.strip():
        raise ToolError("參數 profile 要是 profile 名稱的字串")
    return profile


class _Round:
    """One watcher round's rows, read at most once and only when asked for (an idle round reads none, a round
    with a due heartbeat or something to advance reads one). get() is the send.RoundRows, None when the rows
    cannot be read (the look then reads its own inside its lock)."""

    def __init__(self, server):
        self.server, self._rr, self._tried = server, None, False

    def get(self):
        if not self._tried:
            self._tried = True
            s = self.server
            try:
                self._rr = send.round_rows(s.cfg, s.run, snapshot_fn=s.snapshot_fn, status_fn=s.watch_status_fn)
            except (RegistryError, OSError):
                self._rr = None
        return self._rr

    def rows(self):
        rr = self.get()
        if rr is None:
            raise RegistryError("status rows unreadable")
        return rr.rows

    def by_name(self):
        rr = self.get()
        return rr.by_name if rr is not None else None


class Server:
    def __init__(self, cfg, run, stdin, stdout, *, caller, sleep=time.sleep, clock=time.monotonic, status_fn=None,
                 snapshot_fn=None, now_fn=_utcnow, resolve=None, parent_agent=False, start_of=None):
        self.cfg, self.run, self.stdin, self.stdout, self.caller = cfg, run, stdin, stdout, caller
        self.parent_agent = parent_agent  # started by claude / codex (parent_is_agent); False refuses the acting tools
        self.resolve = resolve  # () -> Caller, asked again before every tool that acts or reads a dispatch
        self.sleep, self.clock, self.now_fn = sleep, clock, now_fn
        # two row functions: status_fn (list_sessions shows last activity: the full status_rows) and watch_status_fn
        # (sending, watching, settling, heartbeats: None = send's own default, status_rows_light). An injected
        # status_fn (tests) serves both.
        self.owe_throttle = send.OweThrottle()
        self.status_fn, self.snapshot_fn = status_fn or status_rows, snapshot_fn
        self.watch_status_fn = status_fn
        self.client_name = None  # clientInfo.name from initialize
        self.start_of = start_of  # pid -> start time (procs.start_time); None writes no heartbeat
        self.nonce = secrets.token_hex(8)  # this instance's key in the inboxes table
        self.started = None
        self._last_beat = None  # self.clock() of the last attempt
        self.mine = set()  # dispatch ids created by this server (an unregistered caller's have no from_name to match)
        self._write_lock = threading.Lock()
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
        self.beat()
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
            for t in self._threads:
                t.join()
            self._stop.set()
            if watcher is not None:
                watcher.join()
            self.forget()
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
            if "result" not in msg and "error" not in msg:
                self._error(rid, INVALID_REQUEST, "缺少 method")  # a client's response to us is ignored
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
            self._result(rid, {"tools": tools_list(self.cfg.mcp_confirm)})
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
        info = params.get("clientInfo")
        name = info.get("name") if isinstance(info, dict) else None
        self.client_name = name if isinstance(name, str) else None
        self._result(rid, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sampan", "version": current_version()},
            "instructions": "sampan 管理 tmux 裡的 Claude Code／Codex session。用 send 傳訊息給另一個 session，"
                            "用 dispatch_result 看回覆。",
        })

    # --- tools -----------------------------------------------------------------------------------------------

    def _client_asks(self, tool):
        """None when the user will be asked before `tool` acts (or confirm is off); else the refusal text. Fail closed:
        the mark on the tool only works if the client honours it, so an unknown client is refused, and so is any
        client when the server was not started by an agent (clientInfo is only what the other end says)."""
        if not self.cfg.mcp_confirm or tool not in ACTING_TOOLS:
            return None
        if not self.parent_agent or self.client_name not in ASKING_CLIENTS:
            return NOT_ASKING
        if self.client_name == CODEX:  # Codex asks only when its config.toml says so; read now, the user may change it.
            from . import codex_approval  # imports this module (ACTING_TOOLS)
            # the home of the Codex that started us, and sampan's configured one (what mcp install edits): both must ask
            seen = set()
            for path in (_running_codex_home() / "config.toml", Path(self.cfg.codex.home) / "config.toml"):
                key = path.resolve()
                if key in seen:
                    continue
                seen.add(key)
                if not codex_approval.tool_prompts(path, tool):
                    return CODEX_NOT_PROMPT.format(path=path)
        return None

    def _call(self, rid, name, args):
        try:
            refusal = self._client_asks(name)
            if refusal:
                raise ToolError(refusal)
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
        session); ToolError otherwise, with the same text whether it exists or not. For a batch id: the batch's
        first item, under the same rule."""
        caller = self._refresh_caller()
        batch = send.is_batch_id(did)
        try:
            d = next((x for x in dispatch.load(self.cfg.state_dir, ids=[did]) if (x.batch if batch else x.id) == did),
                     None)
        except dispatch.DispatchError as e:
            raise ToolError(str(e)) from e
        if d is None or not (did in self.mine or send.from_session(d, caller.name, caller.id)):
            raise ToolError(f"沒有派工 {did}（或不是這個 session 發出的）")
        return d

    def _rows(self, light=True):
        """The status rows; light (no last activity / context) unless the tool shows them (list_sessions)."""
        fn = self.status_fn if not light else (self.watch_status_fn or status_rows_light)
        try:
            return fn(self.cfg, self.run, snapshot_fn=self.snapshot_fn)
        except RegistryError as e:
            raise ToolError(str(e)) from e

    def _advance(self, did):
        """One round for a tool that returns the result: a finished dispatch owes no notice any more (report)."""
        try:
            return send.advance(self.cfg, self.run, did, sleep=self.sleep, snapshot_fn=self.snapshot_fn,
                                status_fn=self.watch_status_fn, now=self.now_fn(), report=True,
                                restore=False)  # a tool result returns at once: the watcher does the reattaching
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

    def _wait_batch(self, bid, seconds):
        """_wait for a whole batch (report: a batch finished in the result owes no notice). The last copies."""
        start = self.clock()
        while True:
            try:
                items = send.advance_batch(self.cfg, self.run, bid, sleep=self.sleep, snapshot_fn=self.snapshot_fn,
                                           status_fn=self.watch_status_fn, now=self.now_fn(), report=True, restore=False)
            except (dispatch.DispatchError, RegistryError) as e:
                raise ToolError(str(e)) from e
            if all(d.state in dispatch.FINISHED for d in items) or self.clock() - start >= seconds:
                return items
            self.sleep(send.POLL_S)

    def tool_list_sessions(self, args):
        rows = self._rows(light=False)
        try:
            reg = {r.tmux: r for r in load_rows(self.cfg.registry_path, codex_profile=self.cfg.codex.profile)}
        except RegistryError:
            reg = {}
        warnings = [getattr(rows, "warning", None)]
        try:
            where = folders.load(self.cfg.state_dir).sessions
        except folders.FoldersError as e:
            where = {}
            warnings.append(f"讀不到資料夾設定，folder 全部當成最上層：{e}")
        mach = machine.load_or_create(self.cfg.path)
        out = {"machine": {"id": mach.id, "name": mach.name},
               "sessions": [{"name": r.name, "id": r.id, "machine": mach.id, "agent": r.agent, "profile": r.profile, "cwd": reg[r.name].cwd if r.name in reg else "",
                             "folder": where.get(r.name, ""), "alias": reg[r.name].alias if r.name in reg else "", "model": reg[r.name].model if r.name in reg else "",
                             "state": r.state.value, "registry": r.registry, "live": r.live, "activity": r.agent_status,
                             "last": r.last.astimezone(timezone.utc).isoformat(timespec="seconds") if r.last else None}
                            for r in rows]}
        if args.get("remote") is True:
            try:
                store = remote.open_store(self.cfg)
                out["remote_machines"] = [
                    {"name": p["name"], "machine": p["machine_id"],
                     "sessions": [{"name": x["session"], "alias": x.get("alias") or "", "activity": x.get("status") or ""}
                                  for x in p["sessions"]]} for p in store.peers_get()]
                store.close()
            except Exception as e:  # noqa: BLE001  the hub's list is optional: say why it is missing
                warnings.append(f"讀不到遠端機器清單：{e}")
        warning = "；".join(w for w in warnings if w)
        if warning:
            out["warning"] = warning
        return out

    def tool_folder_move(self, args):
        names, dest = args.get("sessions"), args.get("folder")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise ToolError("參數 sessions 要是 session 名稱的清單（至少一個）")
        if not isinstance(dest, str):
            raise ToolError("參數 folder 要是字串（\"/\" 是最上層）")
        dest = _folder(dest)

        def apply(tree, reg_rows):  # names are checked against the registry read under edit's lock
            known = {r.tmux for r in reg_rows}
            moved = list(dict.fromkeys(n if n in known else _resolve_ref(reg_rows, n) for n in names))
            for n in moved:
                if n not in known:
                    raise ToolError(f"找不到 session {n}（list_sessions 看名稱）")
            folders.move(tree, [("session", n) for n in moved], dest)
            return moved
        try:  # edit: loads first, and never writes over a file it cannot trust
            names = folders.edit(self.cfg.state_dir, apply, with_rows=True)
        except folders.FoldersError as e:
            raise ToolError(f"{e}（資料夾沒有改動）") from e
        except OSError as e:
            raise ToolError(f"資料夾設定寫不進去：{e}") from e
        return {"moved": names, "folder": dest}

    def tool_send(self, args):
        to, message = _arg_str(args, "session"), _arg_str(args, "message")
        wait, raw, restore = args.get("wait", False), args.get("raw", False), args.get("restore", False)
        profile = _profile_arg(args)
        if not isinstance(wait, bool):
            raise ToolError("參數 wait 要是 true 或 false")
        if not isinstance(raw, bool):
            raise ToolError("參數 raw 要是 true 或 false")
        if not isinstance(restore, bool):
            raise ToolError("參數 restore 要是 true 或 false")
        if profile and not restore:
            raise ToolError(send_restore.NEEDS_RESTORE)
        if restore and raw:
            raise ToolError(send.REASON_RESTORE_RAW)
        if session_run.has_control(to):
            raise ToolError(f"session 名稱不能含換行或控制字元：{to!r}")
        status = self._rows()
        if not any(r.name == to for r in status) and hp.parse_target(to) is not None:
            return self._send_remote(to, message, wait, raw, restore, profile)
        if not any(r.name == to for r in status):
            to = _resolve_ref(status, to)  # an id (prefix) names the session; the name goes on from here
        caller = self._refresh_caller()
        if caller.name is not None and caller.name == to:
            raise ToolError(f"不能送給自己（{to}）")
        row = next((r for r in status if r.name == to), None)
        problem = send.target_problem(row, to, restore)
        if problem:
            raise ToolError(problem)
        d = send.submit(self.cfg, self.run, caller, to, message, notify=not wait, now=self.now_fn(),
                        snapshot_fn=self.snapshot_fn, status_fn=self.watch_status_fn, raw=raw, restore=restore,
                        profile=profile)
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

    def _send_remote(self, to, message, wait, raw, restore, profile):
        """send to a session on another of the owner's machines (through the hub): same answer as a local send."""
        caller = self._refresh_caller()
        d = remote.submit_remote(self.cfg, caller, to, message, notify=not wait, now=self.now_fn(), raw=raw,
                                 restore=restore, profile=profile)
        if isinstance(d, str):
            raise ToolError(d)
        self.mine.add(d.id)
        d = self._wait(d.id, WAIT_MAX_S if wait else 0)
        out = {"dispatch": d.id, "state": d.state, "remote": remote.summary(d)}
        if d.state == "done":
            out["reply"] = d.reply or ""
        elif d.reason:
            out["reason"] = d.reason
        if wait and d.state not in dispatch.FINISHED and caller.name is not None:
            send.set_notify(self.cfg, d.id, True)
        if d.state == "failed":
            raise ToolError(f"派工 {d.id} 失敗：{d.reason}")
        return out

    def tool_send_batch(self, args):
        names, message = args.get("sessions"), _arg_str(args, "message")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise ToolError("參數 sessions 要是 session 名稱的清單（至少一個）")
        status = self._rows()
        if any(not any(r.name == n for r in status) and hp.parse_target(n) is not None for n in names):
            raise ToolError("send_batch 還不能送到別台機器的 session：遠端的請用 send 一個一個送")
        names = [n if any(r.name == n for r in status) or session_run.has_control(n) else _resolve_ref(status, n)
                 for n in names]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ToolError(f"參數 sessions 裡有重複的名稱：{'、'.join(twice)}")
        for n in names:
            if session_run.has_control(n):
                raise ToolError(f"session 名稱不能含換行或控制字元：{n!r}")
        wait, raw, restore = args.get("wait", False), args.get("raw", False), args.get("restore", False)
        profile = _profile_arg(args)
        if not isinstance(wait, bool):
            raise ToolError("參數 wait 要是 true 或 false")
        if not isinstance(raw, bool):
            raise ToolError("參數 raw 要是 true 或 false")
        if not isinstance(restore, bool):
            raise ToolError("參數 restore 要是 true 或 false")
        if profile and not restore:
            raise ToolError(send_restore.NEEDS_RESTORE)
        caller = self._refresh_caller()
        b = send.submit_batch(self.cfg, self.run, caller, names, message, raw=raw, notify=not wait, now=self.now_fn(),
                              snapshot_fn=self.snapshot_fn, status_fn=self.watch_status_fn, restore=restore, profile=profile)
        if isinstance(b, str):
            raise ToolError(b)
        self.mine.update([b.id] + [d.id for d in b.items])
        items = self._wait_batch(b.id, WAIT_MAX_S if wait else 0)
        if wait and not all(d.state in dispatch.FINISHED for d in items) and caller.name is not None:
            send.set_notify(self.cfg, b.id, True)  # nobody has every reply yet: tell the caller when the batch is done
        return {"batch": b.id, "items": [_batch_item(d) for d in items],
                "skipped": [{"session": n, "reason": why} for n, why in b.skipped]}

    def tool_new_session(self, args):
        name, target = _arg_str(args, "name"), _arg_str(args, "target")
        cwd, message = _arg_str(args, "cwd"), _arg_str(args, "message")
        folder = args.get("folder")
        if folder is not None:
            if not isinstance(folder, str):
                raise ToolError("參數 folder 要是字串")
            folder = _folder(folder) or None
        model = args.get("model")
        if model is not None:
            if not isinstance(model, str):
                raise ToolError("參數 model 要是字串")
            model = model.strip()
            if problem := session_run.model_problem(model):
                raise ToolError(problem)
        checked = session_run.check_new(self.cfg, self.run, name, target, cwd)
        if isinstance(checked, str):
            raise ToolError(checked)
        agent, cwd = checked[0], checked[2]
        caller = self._refresh_caller()
        try:
            before = {d.id for d in dispatch.load(self.cfg.state_dir)}
        except dispatch.DispatchError as e:
            raise ToolError(str(e)) from e
        lines, stuck = [], []

        def stop(_name, _agent, p):  # nobody to ask from here: never a key, the user goes in
            stuck.append(p)
            return SKIP
        # with confirm, Claude Code showed the user this cwd in the tool's permission prompt: its own trust prompt is
        # answered. Not for Codex: whether its approval shows the whole arguments is not verified
        rc = session_run.cmd_new(self.cfg, self.run, lines.append, name, target, cwd, message=message,
                                 caller=caller, sleep=self.sleep, folder=folder, on_prompt=stop, model=model,
                                 trust=self.cfg.mcp_confirm and self.client_name == CLAUDE_CODE)  # checks everything again before creating
        said = "\n".join(str(x).removeprefix("❌ ") for x in lines).strip()
        new = [d for d in dispatch.load(self.cfg.state_dir) if d.id not in before and d.to_name == name]
        if not new or rc not in (0, 1):
            raise ToolError(said or f"開不了 session {name}")
        d = new[-1]
        if d.state == "failed":
            raise ToolError(d.reason or said)
        self.mine.add(d.id)
        later = f"請使用者進去處理：tmux attach -t {name}。開場訊息（派工 {d.id}）處理完才會開始，之後用 dispatch_result 看"
        trusted = next((x.strip() for x in said.splitlines() if x.strip().startswith("已信任 ")), None)
        done = f"（{trusted}）" if trusted else ""
        if stuck:
            p = stuck[-1]
            if p.trust_path and cwd.startswith(p.trust_path) and p.trust_path != cwd:
                what = f"信任資料夾的提示，路徑被畫面寬度截斷（{p.trust_path}），無法確認是 {cwd}"
            else:
                what = f"信任 {p.trust_path} 的提示" if p.trust_path else (p.question or "認不出的選單")
            raise ToolError(f"{name} 已開啟{done}，但停在 {agent} 的提示（{what}），這個提示 sampan 不替使用者回答。{later}")
        if rc == 1 and agent == "claude":  # no session file in time and no prompt sampan recognises: not a success
            raise ToolError(f"{name} 已開啟{done}，但 Claude 沒有啟動完成（可能停在登入、主題或其他 sampan 認不出的畫面）。{later}")
        out = {"dispatch": d.id}
        if said:
            out["note"] = said
        return out

    def tool_dispatch_result(self, args):
        did = _arg_str(args, "id")
        wait = args.get("wait", 0)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= WAIT_MAX_S:
            raise ToolError(f"參數 wait 要是 0 到 {WAIT_MAX_S} 的秒數")
        first = self._own(did)  # before advancing anything
        if send.is_batch_id(did):
            items = self._wait_batch(did, wait)
            return {"batch": did, "items": [_batch_item(d) for d in items], "skipped": first.batch_skipped}
        d = self._wait(did, wait)
        out = {"dispatch": d.id, "state": d.state, "label": send.STATE_LABELS[d.state], "to": d.to_name,
               "reply": d.reply, "reason": d.reason}
        try:
            h = send.history(self.cfg, d.id)
        except dispatch.DispatchError as e:
            raise ToolError(str(e)) from e
        status = send.status_of(d)
        out["status"], out["status_label"] = status, send.STATUS_LABELS.get(status, status)
        out["replies"] = [{"id": p["id"], "how": p["how"], "how_label": send.HOW_LABELS.get(p["how"], p["how"]),
                           "at": p["at"], "latest": p["latest"], "late": p["late"],
                           "text": p["text"][:send.REPLY_PREVIEW] if p["text"] is not None else None}
                          for p in h["parts"]]
        out["deliveries"] = h["deliveries"]
        out["last_ring"] = h["ring"]
        out["merged_with"] = h["deliveries"][-1]["with"] if h["deliveries"] else []
        if (rem := remote.summary(d)) is not None:
            out["remote"] = rem
        return out

    def tool_cancel_dispatch(self, args):
        did = _arg_str(args, "id")
        d = self._own(did)
        if send.is_batch_id(did):
            raise ToolError(f"{did} 是批次編號：請用 dispatch_result 看各筆的派工編號，逐筆取消")
        refusal = send.cancel_refusal(d)
        if refusal:
            raise ToolError(refusal)
        r = send.cancel(self.cfg, did, now=self.now_fn())
        if isinstance(r, str):
            raise ToolError(r)
        out = {"dispatch": did, "state": r.state}
        if r.reason == send.REASON_CANCELLED:
            out["reason"] = r.reason
        return out

    def tool_resend(self, args):
        did = _arg_str(args, "dispatch_id")
        if send.is_batch_id(did):
            raise ToolError(f"{did} 是批次編號：請用 dispatch_result 看各筆的派工編號，逐筆重送")
        d = self._own(did)
        refusal = send.resend_refusal(d)
        if refusal:
            raise ToolError(refusal)
        r = send.resend(self.cfg, did, now=self.now_fn(), notify=self.caller.name is not None)  # _own looked it up
        if isinstance(r, str):
            raise ToolError(r)
        self.mine.add(did)
        d = self._wait(did, 0)
        out = {"dispatch": d.id, "state": d.state}
        if d.state == "done":
            out["reply"] = d.reply or ""
        elif d.reason:
            out["reason"] = d.reason
        return out

    def tool_inbox_take(self, args):
        caller = self._refresh_caller()
        try:
            return inbox.take(self.cfg, caller, rows=self._rows(), now=self.now_fn())
        except (inbox.InboxError, dispatch.DispatchError) as e:
            raise ToolError(str(e)) from e

    def tool_reply(self, args):
        did, text, receipt = args.get("dispatch_id"), args.get("text"), args.get("receipt")
        if not isinstance(did, str) or not did:
            raise ToolError("參數 dispatch_id 要是非空字串")
        if not isinstance(text, str):
            raise ToolError("參數 text 要是字串")
        if receipt is not None and not isinstance(receipt, str):
            raise ToolError("參數 receipt 要是字串")
        caller = self._refresh_caller()
        try:
            return inbox.reply(self.cfg, caller, did, text, receipt or None, now=self.now_fn())
        except (inbox.InboxError, dispatch.DispatchError) as e:
            raise ToolError(str(e)) from e

    # --- watcher ---------------------------------------------------------------------------------------------

    def beat(self, round_=None):
        """Tell senders which session and conversation this server serves. Never raises: a heartbeat that cannot
        be written is logged and skipped. round_: the watcher round's rows (read once), used instead of reading again."""
        caller = self.caller
        if self.start_of is None or not caller.id or not caller.agent:
            return
        if self._last_beat is not None and self.clock() - self._last_beat < BEAT_S:
            return
        self._last_beat = self.clock()
        try:
            if self.started is None:
                self.started = self.start_of(os.getpid())
            rows = round_.rows() if round_ is not None else (self.watch_status_fn or status_rows_light)(
                self.cfg, self.run, snapshot_fn=self.snapshot_fn)
            row = next((r for r in rows if r.id == caller.id), None)
            if self.started is None or row is None or not row.sid:
                return
            with dispatch.transaction(self.cfg.state_dir, wait_s=2) as items:
                heartbeat.beat(items.conn, session_id=caller.id, agent=caller.agent, conversation=row.sid,
                               nonce=self.nonce, pid=os.getpid(), started=self.started, version=current_version(),
                               now=self.now_fn(), alive=os.kill, start_of=self.start_of)
        except Exception as e:  # noqa: BLE001  (a heartbeat must never stop the server or its watcher)
            _log(f"心跳寫不進去：{e!r}")

    def forget(self):
        if self.start_of is None or not self.caller.id:
            return
        try:
            with dispatch.transaction(self.cfg.state_dir, wait_s=2) as items:
                heartbeat.forget(items.conn, self.nonce)
        except Exception as e:  # noqa: BLE001
            _log(f"心跳刪不掉：{e!r}")

    def watch_once(self):
        """Advance this caller's pending dispatches (and the ones this server created) once, then settle the mail
        addressed to the caller (send.settle_mail: its sender may never look again; no key into its own pane).
        The status rows are read once for the whole round (before any lock), and only when something needs them."""
        rnd = _Round(self)
        self.beat(rnd)
        try:
            items = dispatch.load(self.cfg.state_dir, ids=())  # the live ones only: every 3 s in every server
            owed = set(send.pending(self.cfg, items))
        except (dispatch.DispatchError, OSError) as e:
            _log(str(e))
            return
        self.owe_throttle.prune({d.id for d in items})
        name = self.caller.name
        for d in items:
            if d.id in owed and (d.id in self.mine or send.from_session(d, name, self.caller.id)):
                if self.owe_throttle.skip(d.id):
                    continue  # only a notice for a sender that is not running is owed: tried every send.OWED_RETRY_S
                self.beat(rnd)  # a reattach inside an advance takes a minute: keep the heartbeat fresh between dispatches
                try:
                    send.advance(self.cfg, self.run, d.id, sleep=self.sleep, snapshot_fn=self.snapshot_fn,
                                 status_fn=self.watch_status_fn, now=self.now_fn(), rows=rnd.get())
                except (dispatch.DispatchError, RegistryError, OSError) as e:
                    _log(f"派工 {d.id}：{e}")
                self.owe_throttle.note(d.id, items, rnd.by_name())
        inbound = [d.id for d in items if send.needs_settling(d, self.caller.id)]
        if inbound:
            try:
                send.settle_mail(self.cfg, self.run, self.caller.id, inbound, sleep=self.sleep,
                                 snapshot_fn=self.snapshot_fn, status_fn=self.watch_status_fn, now=self.now_fn(),
                                 rows=rnd.get())
            except (dispatch.DispatchError, RegistryError, OSError) as e:
                _log(f"收件 {'、'.join(inbound)}：{e}")

    def _watch(self, interval):
        while not self._stop.wait(interval):
            try:
                self.watch_once()
            except Exception as e:  # noqa: BLE001  the watcher thread must outlive any one bad pass
                _log(f"派工監看這一輪失敗：{e!r}")


def serve(cfg, run, stdin, stdout, *, caller, sleep=time.sleep, clock=time.monotonic, status_fn=None,
          snapshot_fn=None, now_fn=_utcnow, watch_interval=WATCH_S, resolve=None, parent_agent=False, start_of=None):
    """Serve MCP on stdin/stdout until stdin closes. Returns 0. resolve: () -> Caller, to look the caller up again
    before each tool that acts or reads a dispatch (`caller` is the identity found at start). parent_agent: the
    server's parent is claude / codex (parent_is_agent, found once at start). start_of: pid -> start time; given, the
    server writes heartbeats."""
    return Server(cfg, run, stdin, stdout, caller=caller, sleep=sleep, clock=clock, status_fn=status_fn,
                  snapshot_fn=snapshot_fn, now_fn=now_fn, resolve=resolve,
                  parent_agent=parent_agent, start_of=start_of).serve(watch_interval)
