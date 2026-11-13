"""Sending a message to a session: submit a dispatch, advance it (queued → sent → done / failed), notify the caller.

Actions layer. Every state change happens inside dispatch.transaction: the latest record is read, judged and written
back under one lock, so two watchers (an MCP server and the sampan screen) never deliver the same dispatch twice.
The target's conversation id is read from status_rows on every advance; Dispatch.thread_id is only a cache (for a
direct send to Claude: the conversation it went to, so a /clear that starts a new one is told from a lost message).
"""
import os
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

from . import actionlog, deliver, deliveries, dispatch, doorbell, heartbeat, procs, store
from .claude_state import Reply, TurnInfo, after_marker, command_reply, find_transcript
from .claude_state import reply_to as claude_reply_to
from .clock import fmt_local, parse_iso
from .codex_state import _is_codex, find_rollout
from .codex_state import reply_to as codex_reply_to
from .machine import registry_machine
from .procs import is_descendant, list_procs
from .registry import RegistryError, load_rows
from .screens import claude_input_state
from .status import State, status_rows
from .textwidth import text_width
from .tmux import Tmux
from .update import current_version

POLL_S = 2
NOT_SEEN_GRACE_S = 15  # right after delivery the transcript may not have the message yet
QUEUE_LIMIT_S = 30 * 60  # a dispatch that still cannot be delivered after this long fails (with its last reason)
STARTUP_GRACE_S = 120  # `sampan new --message`: how long a session that is not seen yet may take to come up
TERMINAL_NAME = dispatch.TERMINAL_NAME  # only a person at the terminal
UNREGISTERED_NAME = "未登記的 session"  # an agent that is not a sampan session (e.g. an MCP caller outside tmux)
GONE = (State.STOPPED, State.RETIRED, State.UNREGISTERED, State.NO_AGENT)  # the session is not there to answer
REASON_STOPPED = "對方 session 已停止"
REASON_NOT_RUNNING = "對方 session 沒在執行"
REASON_NOT_SEEN = "還沒在對方的對話裡看到這則訊息"
REASON_NOT_FOUND = "找不到回覆：對方的對話裡沒有這則訊息（可能 /clear、換了對話）"
REASON_NO_THREAD = "對方還沒有對話編號"
REASON_AGENT_CHANGED = "對方的 agent 換了"
REASON_PASTED = "已貼進輸入框但沒按到 Enter（請自己到那個 session 確認）"
REASON_SENDING = "送出中"  # written before any key is pressed: if sampan dies now, the dispatch is never sent again
REASON_STARTING = "對方 session 還在啟動"
REASON_SKIPPED = "對方沒回這則就換了下一則"
REASON_NO_TEXT_YET = "對方閒著，但還沒看到回覆文字"
REASON_NO_TEXT = "對方這輪結束了，但沒有回覆文字"
REASON_ABORTED = "對方這輪被中斷了"
REASON_STOP_WATCHING = "不再等回覆"  # what a 0.16 cancel of a sent dispatch wrote (still recognised on old rows)
REASON_CANCELLED = "寄件者已取消"  # a cancelled message that had reached the target (taken, partial, pasted)
REASON_RAN = "指令已執行（沒有文字回覆）"  # a direct send done without reply text: its reply
REASON_NEW_THREAD = "對方換成新對話（{sid8}）"  # a direct send (/clear…) after which the target's conversation id changed
REASON_MENU = "指令打開了選單，請自己到那個 session 處理"
REASON_MENU_SEEN = deliver.REASON_BLOCKED  # one look at a menu after a direct send (two in a row fail it)
READY = ("idle", "shell")  # Claude statuses where the prompt takes input ("shell": the turn is over, a background
# shell task still runs; Claude 2.1.287 reports it until the task ends, so waiting for "idle" would hold messages)
BUSY = {"busy": "對方正在忙", "working": "對方正在忙", "waiting": "對方在等使用者回應"}
REASON_TAKEN = "已取走，對方還在做"  # inbox_take took it (status taken)
REASON_PARTIAL = "已有回覆段落，對方還在做"  # it has a reply part, the turn goes on (status partial)
REASON_UNANSWERED = "已取走，這一輪結束卻沒有回覆"  # status unanswered
REASON_BEHIND = "排在前面的派工還沒送完"  # a direct send waits for those before it, and the ones after it for it
WAITING = {*BUSY.values(), REASON_NOT_RUNNING, REASON_NO_THREAD, REASON_BEHIND}  # queued reasons that clear by themselves: no limit
RUNNING_VERSION = current_version()  # read once at import: the code this process runs, even after an upgrade on disk


def process_tag():
    """Which sampan process wrote a log line (several run at once: the screen, each agent's MCP server, commands)."""
    return f"〔sampan {RUNNING_VERSION}，pid {os.getpid()}〕"


def _log(cfg, label, line):
    actionlog.append(cfg, label, [line + process_tag()])


STATE_LABELS = {"queued": "排隊中", "sent": "已送出、等回覆", "done": "已回覆", "failed": "失敗", "cancelled": "已取消"}
STATUS_LABELS = {"queued": "排隊中", "rung": "已按門鈴", "taken": "已取走", "partial": "已有回覆段落", "pasted": "已貼全文",
                 "replied": "已回覆", "unanswered": "已取走沒回覆", "failed": "失敗", "cancelled": "已取消"}
HOW_LABELS = {"tool": "工具回覆", "marker": "標記分段", "whole": "整段", "fill": "整段補給"}
KIND_LABELS = {"inbox": "取信", "paste": "貼全文", "raw": "直接送"}
RING_LABELS = {"ringing": "等對方取信", "taken": "已取信", "no_answer": "沒人應", "paste_failed": "貼不進去",
               "superseded": "已取代"}
RESENDABLE = ("failed", "cancelled", "done")
REQUEUED_AT = "requeued_at"  # extra key: when resend put it back in line (created stays: line order, batch order)
REPLY_PREVIEW = 2_000  # dispatch_result: the text of each reply part, up to this many characters
LOG_PREVIEW = 100


@dataclass
class Caller:
    """Who sends: a sampan session, or the terminal / an unregistered session (name None, never notified).
    kind: force a dispatch.KINDS value; "unregistered" with agent "" is an agent sampan could not identify."""
    name: str | None
    profile: str
    agent: str
    pane: str | None
    kind: str | None = None
    id: str = ""  # the registry row's id, "" when not a session


TERMINAL = Caller(None, "", "", None)

AGENT_REFUSED = "agent 請改用 sampan MCP 的工具（會先問使用者）；要讓 agent 直接用指令，在 config 設 [mcp] confirm = false"


def caller_label(caller):
    """The from_name recorded for a caller: its session name, 「未登記的 session（<agent>）」 for an agent that is not a
    sampan session (「未登記的 session」 when its agent is unknown), or 「終端機」 for a person at the terminal."""
    if caller.name is not None:
        return caller.name
    if caller_kind(caller) == "unregistered":
        return f"{UNREGISTERED_NAME}（{caller.agent}）" if caller.agent else UNREGISTERED_NAME
    return TERMINAL_NAME


def caller_kind(caller):
    """dispatch.KINDS value of a caller: recorded with the dispatch, so a session that happens to be named like a
    label can never be taken for the terminal or an unregistered agent (or the other way round)."""
    if caller.name is not None:
        return "session"
    if caller.kind is not None:
        return caller.kind
    return "unregistered" if caller.agent else "terminal"


class CallerUnsure(Exception):
    """The caller could not be looked up right now (ps or the tmux pane list unreadable, registry unreadable): this
    says nothing about whether it is a session. args: (reason, agent found or "")."""


def lookup_caller(cfg, run, pid):
    """Who runs pid: the nearest claude / codex process at or above it, the tmux pane it runs in, and the registry row
    of that tmux session (same agent). No agent process → TERMINAL (a person); no pane, no row or a row of another
    agent → an unregistered Caller; raises CallerUnsure when ps, tmux or the registry cannot be read."""
    procs = list_procs(run)
    if not procs:
        raise CallerUnsure("讀不到行程表", "")
    agent_proc, seen, cur = None, set(), procs.get(pid)
    while cur is not None and cur.pid not in seen:
        seen.add(cur.pid)
        if cur.comm == "claude" and not cur.zombie:
            agent_proc, agent = cur, "claude"
            break
        if _is_codex(cur):
            agent_proc, agent = cur, "codex"
            break
        cur = procs.get(cur.ppid)
    if agent_proc is None:
        return TERMINAL
    unknown = Caller(None, "", agent, None)
    tmux = Tmux(run)
    panes = tmux.panes()
    if panes is None:
        raise CallerUnsure(tmux.unreadable_reason or "讀不到 tmux", agent)
    pane = next((p for p in panes if is_descendant(procs, agent_proc.pid, p.pane_pid)), None)
    if pane is None:
        return unknown
    try:
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except RegistryError as e:
        raise CallerUnsure(str(e), agent) from e
    row = next((r for r in rows if r.tmux == pane.session), None)
    if row is None or row.agent != agent:
        return unknown
    return Caller(row.tmux, row.profile, agent, pane.pane_id, id=row.id)


def unsure_caller(e):
    """The Caller to use when lookup_caller raised CallerUnsure e: an unregistered agent, never the terminal (it
    cannot be told that nobody but a person is there)."""
    return Caller(None, "", e.args[1] if len(e.args) > 1 else "", None, kind="unregistered")


def command_caller(cfg, run, pid):
    """Who runs a `sampan send` / `sampan new --message` command: a sampan session when an agent runs it through its
    shell tool, an unregistered agent, or the terminal only when no agent process is above it."""
    try:
        return lookup_caller(cfg, run, pid)
    except CallerUnsure as e:
        return unsure_caller(e)


def from_session(d, name, id=""):
    """Whether dispatch d was sent by the sampan session called name (name None → never). With an id (and a dispatch
    that recorded its sender's id) the id decides, so a renamed caller still owns its dispatches."""
    if name is None or d.from_kind != "session":
        return False
    if id and d.from_id:
        return d.from_id == id
    return d.from_name == name


def notice_text(d):
    if d.state == "failed":
        reason = " ".join((d.reason or "").split())
        return f"〔sampan：給 {d.to_name} 的派工 {d.id} 失敗：{reason}〕"
    return f"〔sampan：{d.to_name} 已回覆派工 {d.id}，用 sampan 工具 dispatch_result 看內容〕"


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(cfg, run, snapshot_fn, status_fn):
    return {r.name: r for r in (status_fn or status_rows)(cfg, run, snapshot_fn=snapshot_fn)}


def _row_of(rows, id, name):
    """The row of rows (by name) for a dispatch end: found by id; by name only for an old dispatch with no id. An id
    nobody has any more is gone, never a name that may now belong to another session."""
    if id:
        return next((r for r in rows.values() if r.id == id), None)
    return rows.get(name)


def _preview(message):
    line = message.strip().splitlines()[0] if message.strip() else ""
    return line if len(line) <= LOG_PREVIEW else line[:LOG_PREVIEW] + "…"


def target_problem(row, to):
    """Why `to` (its StatusRow, or None) cannot be sent to, or None."""
    if row is None or row.state in (State.UNREGISTERED, State.NO_AGENT):
        return f"找不到 session「{to}」：不在 sampan 登記表裡（sampan status 看名稱）"
    if row.state != State.RUNNING:
        return f"「{to}」不是執行中（{row.display}），不送"
    return None


RAW_MAX = 500  # longest direct send, in characters
REASON_RAW_CODEX = "直接送目前只支援 Claude"


def raw_problem(message):
    """Why message cannot be a direct send (one line starting with /, at most RAW_MAX characters, no control
    characters: an ESC would end the bracketed paste early and the rest would be keys), or None. It is judged after
    strip, which is what gets sent."""
    m = message.strip()
    if "\n" in m or "\r" in m:
        return "直接送只能是一行"
    if any(unicodedata.category(c) == "Cc" for c in m):
        return "直接送不能含控制字元（例如 Tab、Esc）"
    if not m.startswith("/"):
        return "直接送要以 / 開頭（例如 /compact）"
    if len(m) > RAW_MAX:
        return f"直接送最多 {RAW_MAX} 字（這則 {len(m)} 字）"
    return None


def submit(cfg, run, caller, to, message, *, notify, now, snapshot_fn=None, status_fn=None, raw=False, batch=None):
    """Check the target and create a queued dispatch (outbox written, actions.log line). Dispatch, or why not (str).
    raw: send the stripped message as is, with no source line (Claude only; see raw_problem)."""
    if problem := store.mnt_problem(cfg.state_dir):
        return problem
    if not message.strip():
        return "訊息是空的"
    if raw:
        message = message.strip()
        if problem := raw_problem(message):
            return problem
    if caller.name is not None and caller.name == to:
        return f"不能送給自己（{to}）"
    try:
        row = _rows(cfg, run, snapshot_fn, status_fn).get(to)
    except RegistryError as e:
        return str(e)
    problem = target_problem(row, to)
    if problem:
        return problem
    if raw and row.agent == "codex":
        return REASON_RAW_CODEX
    r = _create(cfg, caller, to, row.agent, row.profile, message, "queued", now, notify=notify,
                thread_id=(row.sid or None) if row.agent == "codex" else None, raw=raw, batch=batch, to_id=row.id)
    if isinstance(r, str):
        return r
    kind = f"{row.agent}，直接送" if raw else row.agent
    _log(cfg, "send", f"派工 {r.id}：{_who(r)} → {to}（{kind}）：{_preview(message)}")
    return r


def _who(d):
    return d.from_name if d.from_profile == "" else f"{d.from_name}（{d.from_profile}）"


def _create(cfg, caller, to, to_agent, to_profile, message, state, now, *, notify, thread_id=None, strip=False,
            startup=False, raw=False, batch=None, to_id=""):
    """Write the outbox and record a dispatch under the lock. A copy of it, or why not (str).
    strip: drop trailing newlines from the text (a startup prompt goes through `$(cat …)`, which drops them)."""
    try:
        with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days) as items:
            d = _record(cfg, caller, to, to_agent, to_profile, message, state, now, items.ids,
                        notify=notify, thread_id=thread_id, strip=strip, startup=startup, raw=raw, batch=batch,
                        to_id=to_id)
            _keep(items, [d])
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    return replace(d)


def _record(cfg, caller, to, to_agent, to_profile, message, state, now, taken, *, notify, thread_id=None, strip=False,
            startup=False, raw=False, batch=None, to_id=""):
    """A new Dispatch with an id not in taken, its outbox written (call inside dispatch.transaction)."""
    did = dispatch.new_id(taken)
    text = message if raw else deliver.compose(caller_label(caller), caller.profile, did, message)
    if strip or state == "queued":  # a trailing newline (stdin) is never part of the message
        text = text.rstrip("\n")
    dispatch.write_outbox(cfg.state_dir, did, text)
    return dispatch.Dispatch(
        did, caller_label(caller), caller.profile, caller.agent,
        to, to_agent, to_profile, text, state, _iso(now),
        sent_at=_iso(now) if state == "sent" else None,
        notify=bool(notify and caller.name is not None), thread_id=thread_id, from_kind=caller_kind(caller),
        startup=startup, raw=raw, batch=batch, to_id=to_id, from_id=caller.id if caller.name is not None else "",
        machine=registry_machine(cfg))


def _keep(items, new):
    """Add the new dispatches at the end (nothing is dropped here: the transaction clears the bodies of old ones)."""
    items.extend(new)


@dataclass
class Batch:
    """One batch send: the dispatches it created (in the order given) and the sessions it skipped, (name, reason)."""
    id: str
    items: list
    skipped: list


def is_batch_id(text):
    """"b" + four lowercase hex digits. A dispatch id is four hex digits, so one that starts with b is not a batch."""
    return len(text) == 5 and text[0] == "b" and all(c in "0123456789abcdef" for c in text[1:])


def _new_batch_id(items):
    """A batch id no message of the table has (purged and unloaded ones included)."""
    used = {r[0][1:] for r in items.conn.execute("SELECT DISTINCT batch FROM messages WHERE batch IS NOT NULL")}
    return "b" + dispatch.new_id(used)


def _skip_reason(caller, name, row, raw):
    """Why a batch does not send to name (its StatusRow, or None) now, or None. Only a running, idle session is sent
    to: a batch never queues."""
    if caller.name is not None and caller.name == name:
        return f"不能送給自己（{name}）"
    problem = target_problem(row, name)
    if problem:
        return problem
    if raw and row.agent == "codex":
        return REASON_RAW_CODEX
    if row.agent_status not in READY:
        return BUSY.get(row.agent_status, "看不出對方是否閒著")
    return None


def submit_batch(cfg, run, caller, names, message, *, raw, notify, now, snapshot_fn=None, status_fn=None):
    """Send the same message to several sessions: a queued dispatch for each running, idle one (one transaction, one
    batch id); the others are skipped with why, never queued. A name given twice counts once. Batch, or why not
    (str: a bad message, or nobody to send to; nothing is recorded). notify: one notice for the whole batch, when
    every item is finished (see _owes_notice)."""
    if problem := store.mnt_problem(cfg.state_dir):
        return problem
    if not message.strip():
        return "訊息是空的"
    if raw:
        message = message.strip()
        if problem := raw_problem(message):
            return problem
    try:
        rows = _rows(cfg, run, snapshot_fn, status_fn)
    except RegistryError as e:
        return str(e)
    targets, skipped = [], []
    for name in dict.fromkeys(names):
        why = _skip_reason(caller, name, rows.get(name), raw)
        if why:
            skipped.append((name, why))
        else:
            targets.append(rows[name])
    if not targets:
        return "沒有可以送的 session：" + "；".join(f"{n}：{why}" for n, why in skipped)
    try:
        with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days) as items:
            bid, taken, made = _new_batch_id(items), items.ids, []
            for row in targets:
                d = _record(cfg, caller, row.name, row.agent, row.profile, message, "queued", now, taken,
                            notify=notify, thread_id=(row.sid or None) if row.agent == "codex" else None, raw=raw,
                            batch=bid, to_id=row.id)
                taken.add(d.id)
                made.append(d)
            made[0].batch_skipped = len(skipped)
            _keep(items, made)
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    for d in made:
        kind = f"{d.to_agent}，直接送" if raw else d.to_agent
        _log(cfg, "send", f"派工 {d.id}：{_who(d)} → {d.to_name}（{kind}，批次 {bid}）：{_preview(message)}")
    if skipped:
        _log(cfg, "send", f"批次 {bid}：跳過 " + "、".join(f"{n}（{why}）" for n, why in skipped))
    return Batch(bid, [replace(d) for d in made], skipped)


def batch_items(cfg, bid):
    """The dispatches of batch bid, in the order they were created (the first one carries batch_skipped)."""
    return [d for d in dispatch.load(cfg.state_dir, ids=[bid]) if d.batch == bid]


def _group(items, bid):
    return [x for x in items if x.batch == bid]


def batch_notice_text(bid, items, skipped_n):
    done = sum(d.state == "done" for d in items)
    failed = sum(d.state in ("failed", "cancelled") for d in items)
    return f"〔sampan：批次 {bid} 完成：{done} 完成、{failed} 失敗、{skipped_n} 跳過，用 dispatch_result 看〕"


def submit_new(cfg, caller, to, to_agent, to_profile, message, *, notify, now):
    """Record the dispatch for `sampan new --message`: the message is the new session's startup prompt, so it is
    `sent` at once (the session does not exist yet to be checked) and the outbox file is what `$(cat …)` reads.
    Dispatch, or why not (str)."""
    if problem := store.mnt_problem(cfg.state_dir):
        return problem
    if not message.strip():
        return "訊息是空的"
    r = _create(cfg, caller, to, to_agent, to_profile, message, "sent", now, notify=notify, strip=True, startup=True)
    if isinstance(r, str):
        return r
    _log(cfg, "send", f"派工 {r.id}：{_who(r)} → {to}（{to_agent}，開場訊息）：{_preview(message)}")
    return r


def abort_new(cfg, did, reason, *, now):
    """`sampan new --message` could not start the session: mark its dispatch failed so it is not left `sent`."""
    try:
        with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days, ids=[did]) as items:
            d = next((x for x in items if x.id == did), None)
            if d is not None and d.state == "sent":
                _finish(d, "failed", now, reason)
                d.notify = False  # the caller is told right away by `sampan new` / new_session
    except (dispatch.DispatchError, OSError):
        return
    _log(cfg, "send", f"派工 {did}：{reason}")


def _notice_id(did):
    return f"{did}-notice"


def _owes_notice(d, items=()):
    """Whether d's caller is still owed a notice. A batch item never owes one of its own: the batch's first item owes
    the one batch notice, once every item of the batch (looked up in items) is finished, cancelled ones included."""
    if not d.notify or d.notified:
        return False
    if d.batch is None:
        return d.state in ("done", "failed")
    group = _group(items, d.batch)
    return bool(group) and group[0] is d and all(x.state in dispatch.FINISHED for x in group)


def advance(cfg, run, did, *, sleep, snapshot_fn=None, status_fn=None, now=None, report=False, report_batch=False):
    """Move one dispatch forward as far as it can go now; returns a copy of it. Finished ones are left alone.
    report: the caller gets the returned copy right away (an MCP tool result): if it is finished, its notice is
    dropped in the same transaction, so the caller is never told twice. For a batch item: report_batch drops the
    batch notice once the whole batch is finished (the caller gets every item); the batch notice is sent from here
    when this advance finishes the batch.
    Raises DispatchError for an unknown id or an unreadable dispatches.json, RegistryError for a bad registry."""
    now = now or _utcnow()
    with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days, ids=[did]) as items:
        d = next((x for x in items if x.id == did), None)
        if d is None:
            raise dispatch.DispatchError(f"沒有派工 {did}")
        seen = {}

        def rows():
            if "rows" not in seen:
                seen["rows"] = _rows(cfg, run, snapshot_fn, status_fn)
            return seen["rows"]

        def save():
            dispatch.checkpoint(cfg.state_dir, items)
        # deliveries, rings and the notice's reply parts are read and written through items.conn
        with dispatch._sqlite_errors(cfg.state_dir):
            if d.state == "queued":
                _deliver(cfg, run, d, _row_of(rows(), d.to_id, d.to_name), sleep, now, save, items)
                _expire(cfg, d, now)
            elif d.state == "sent":
                _check(cfg, d, _row_of(rows(), d.to_id, d.to_name), now, run, items)
            if d.batch is None:
                if report and d.state in dispatch.FINISHED:
                    d.notify = False  # the tool result tells the caller
                owed = d
            else:
                group = _group(items, d.batch)
                if report_batch and all(x.state in dispatch.FINISHED for x in group):
                    for x in group:
                        x.notify = False  # the tool result tells the caller about every item
                owed = group[0]
            if _owes_notice(owed, items):
                notify_caller(cfg, run, owed, sleep, rows=rows(), save=save, items=items)
        result = replace(d)
    return result


def advance_batch(cfg, run, bid, *, sleep, snapshot_fn=None, status_fn=None, now=None, report=False):
    """advance every item of batch bid once (report: report_batch). Copies of the items; DispatchError when the
    batch has none."""
    ids = [d.id for d in batch_items(cfg, bid)]
    if not ids:
        raise dispatch.DispatchError(f"沒有批次 {bid}")
    return [advance(cfg, run, i, sleep=sleep, snapshot_fn=snapshot_fn, status_fn=status_fn, now=now,
                    report_batch=report) for i in ids]


def _deliver(cfg, run, d, row, sleep, now, save, items):
    """Two-phase: the record is written as `sent` (「送出中」) before any key is pressed or `codex queue` runs, so a
    process killed in the middle never sends it again (at most once: it later fails as 找不到回覆). It goes back to
    queued only when the step says nothing was sent.
    Every message queued for the same target before its first direct send goes with it, as one paste (deliveries:
    one active delivery per target at a time); a direct send goes alone, once nothing is before it. Whoever advances
    a message moves the target's whole line: an active delivery is judged first (its own senders may never look
    again), and the head of the line is sent even when d is behind it (d then stays queued)."""
    if d.status == "rung" and (row is None or row.state != State.RUNNING or row.agent != d.to_agent):
        rung = [x for x in items if x.state == "queued" and x.status == "rung"
                and (x.to_id == d.to_id if d.to_id else x.to_name == d.to_name)]
        doorbell.give_up_if_old(items.conn, d.to_id or d.to_name, rung, now, lambda line: _log(cfg, "send", line))
    if row is None or row.state in GONE:
        d.reason = REASON_NOT_RUNNING
        return
    if row.state != State.RUNNING:
        d.reason = f"對方狀態：{row.display}"
        return
    if row.agent != d.to_agent:
        d.reason = REASON_AGENT_CHANGED
        return
    if row.agent == "codex":
        if not row.sid:
            d.reason = REASON_NO_THREAD
            return
    else:
        _settle_ring(cfg, run, items, row, now, sleep)  # before the busy check: a permission prompt is "waiting"
        if d.status == "rung":  # it waits for the target to take it (settle_ring keeps its reason)
            return
        if row.agent_status not in READY:
            d.reason = BUSY.get(row.agent_status, "看不出對方是否閒著")
            return
        if row.pane is None:
            d.reason = REASON_NOT_RUNNING
            return
    target = row.id or row.name
    if _busy_with_delivery(cfg, run, items, row, target, now):
        d.reason = BUSY["busy"]
        return
    group = _next_group(items, d, row)
    if not group:
        return
    behind = not any(x is d for x in group)  # d waits for the head of the line, which goes on its behalf
    plan = doorbell.plan_for(items.conn, row, now, group, start_of=lambda pid: procs.start_time(pid, run))
    if plan == "wait":  # a ring is still waiting to be answered: the messages after it go with it
        d.reason = REASON_BEHIND
        return
    if plan == "ring":
        why = doorbell.ring_for(items, cfg, run, row, group, now, sleep, save=save,
                                log=lambda line: _log(cfg, "send", line))
        if behind:
            d.reason = why or REASON_BEHIND
        return
    lead = group[0]
    text = lead.text if len(group) == 1 else deliver.compose_group(group)
    try:
        did = deliveries.open_delivery(items.conn, kind="raw" if lead.raw else "paste", to_id=target, to_name=row.name,
                                       messages=[x.id for x in group], text=text, conversation=row.sid or None,
                                       cursor=_transcript_size(cfg, row), now=now)
    except deliveries.ActiveDelivery:
        d.reason = BUSY["busy"]
        return
    name = lead.id if len(group) == 1 else did  # one message: its own outbox and tmux buffer, as before
    if row.agent != "codex":  # written before the record says 送出中: a failed write leaves it queued
        outbox = dispatch.outbox_path(cfg.state_dir, name)
        if len(group) > 1 or not outbox.exists():
            outbox = dispatch.write_outbox(cfg.state_dir, name, text)
    for x in group:
        if row.agent == "codex":
            x.thread_id = row.sid
        elif x.raw:
            x.thread_id = row.sid or None  # the conversation it went to: a /clear changes it
        x.state, x.status, x.sent_at, x.reason = "sent", "", _iso(now), REASON_SENDING
    save()
    if row.agent == "codex":
        why = deliver.to_codex(cfg, run, row.sid, text)  # `codex queue` waits for the turn by itself
    else:
        why = deliver.to_claude(Tmux(run), row.pane, outbox, name, sleep, raw=lead.raw)
    ids = "、".join(x.id for x in group)
    if isinstance(why, deliver.Unsure):  # it may have reached the target: never send the same dispatch twice
        for x in group:
            _finish(x, "failed", now, f"{REASON_PASTED}：{why}" if isinstance(why, deliver.Pasted) else str(why))
        deliveries.end_delivery(items.conn, did, now)
        _log(cfg, "send", f"派工 {ids}：{lead.reason}")
        if behind:
            d.reason = REASON_BEHIND
        return
    if why:
        for x in group:
            x.state, x.sent_at, x.reason = "queued", None, why  # nothing was sent: try again later
        deliveries.drop_delivery(items.conn, did)
        if name == did:  # a group paste's own outbox: nothing of it was pasted
            try:
                dispatch.outbox_path(cfg.state_dir, did).unlink(missing_ok=True)
            except OSError:
                pass
        if behind:
            d.reason = why  # what stops the head stops d too
        return
    for x in group:
        x.reason = plan.reason  # a Claude: why it got the full text, not a ring
    if behind:
        d.reason = REASON_BEHIND
    if plan.reason:
        _log(cfg, "send", f"派工 {ids}：{plan.reason}")
    if len(group) == 1:
        _log(cfg, "send", f"派工 {lead.id}：已送到 {lead.to_name}")
    else:
        _log(cfg, "send", f"派工 {ids}：一起送到 {lead.to_name}（送達 {did}，{len(group)} 則）")


def _transcript_size(cfg, row):
    """The size of the target's transcript (Claude) or rollout (Codex) before a paste, or None: its reply is read only
    from there on, so a text pasted before (the same message, resent) never counts."""
    if not row.sid:
        return None
    if row.agent == "codex":
        path = find_rollout(cfg.codex.home, row.sid)
    else:
        path = find_transcript(cfg.profiles.values(), row.sid, prefer=row.profile)
    try:
        return path.stat().st_size if path else None
    except OSError:
        return None


def _for_target(x, row):
    """Whether dispatch x is addressed to row's session (by id; by name only for an old dispatch with no id)."""
    return x.to_id == row.id if x.to_id else x.to_name == row.name


def _next_group(items, d, row):
    """The head of the target's line (d is in it, queued): every dispatch still queued for the same target, by
    created, up to (not including) the first direct send, whoever sent it; a direct send alone when it is first.
    It may not contain d (d is behind it), and is empty when nothing is queued (d is rung: it waits for a take)."""
    line = sorted((x for x in items if x.state == "queued" and (x.status or "queued") == "queued"
                   and x.to_agent == row.agent and _for_target(x, row)), key=_created_key)
    if not line:
        return []
    if line[0].raw:
        return line[:1]
    group = []
    for x in line:
        if x.raw:
            break
        group.append(x)
    return group


def _settle_ring(cfg, run, items, row, now, sleep, keys=True):
    """Judge the target's ringing ring on everyone's behalf (its messages' own watchers may never look again)."""
    target = row.id or row.name
    rung = [x for x in items if x.state == "queued" and x.status == "rung" and _for_target(x, row)]
    ring = doorbell.ringing(items.conn, target)
    if ring is not None or rung:
        doorbell.settle_ring(items.conn, cfg, run, ring, row, rung, now, sleep=sleep,
                             log=lambda line: _log(cfg, "send", line), keys=keys)


def needs_settling(d, target_id):
    """Whether d is mail to target_id that its receiver's watcher settles: rung (its ring waits to be judged) or sent
    (its delivery waits for the turn to end)."""
    return bool(target_id) and d.to_id == target_id and (d.state == "sent" or (d.state == "queued"
                                                                               and d.status == "rung"))


def settle_mail(cfg, run, target_id, ids, *, sleep, snapshot_fn=None, status_fn=None, now=None):
    """The receiving side of advance, for the watcher of target_id's own session (ids: its mail that needs_settling):
    a sender that advanced once and exited (a terminal `sampan send`) never looks again. Its ringing ring is judged
    (no_answer once the turn after it ended without a take, or after RING_LIMIT_S) and its active delivery is judged
    (an inbox one ends after two idle looks once its turn is over); the original senders get their notices as from
    advance. No key is ever pressed into target_id's own pane: a ring left in the box gets no Enter, and messages that
    go back in line are left for an advancer that is not the target (a sender's watcher, the ui, the next send)."""
    now = now or _utcnow()
    with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days, ids=ids) as items:
        rows = _rows(cfg, run, snapshot_fn, status_fn)
        row = _row_of(rows, target_id, "")
        if row is None or row.state != State.RUNNING:
            return

        def save():
            dispatch.checkpoint(cfg.state_dir, items)
        with dispatch._sqlite_errors(cfg.state_dir):
            if row.agent != "codex":
                _settle_ring(cfg, run, items, row, now, sleep, keys=False)
            _busy_with_delivery(cfg, run, items, row, target_id, now)
            for x in [x for x in items if x.to_id == target_id and x.state in dispatch.FINISHED]:
                owed = x if x.batch is None else _group(items, x.batch)[0]
                if owed.from_id != target_id and _owes_notice(owed, items):
                    notify_caller(cfg, run, owed, sleep, rows=rows, save=save, items=items)


def _created_key(x):
    return parse_iso(x.created) or datetime.max.replace(tzinfo=timezone.utc)


def _busy_with_delivery(cfg, run, items, row, target, now):
    """Whether target still has an active delivery that a message is waiting on. It is judged here first (_check on
    a sent message of it settles them all), so a delivery whose senders never look again does not hold the target;
    one whose messages are all finished is ended."""
    active = deliveries.active_delivery(items.conn, target)
    if active is None:
        return False
    ids = set(deliveries.members(items.conn, active["id"]))
    sent = [x for x in items if x.id in ids and x.state == "sent"]
    if sent:
        _check(cfg, sent[0], row, now, run, items)
        if any(x.state == "sent" for x in sent):
            return True
    deliveries.end_delivery(items.conn, active["id"], now)
    return False


def _expire(cfg, d, now):
    """A dispatch still queued QUEUE_LIMIT_S after it was created, for a reason that does not go away by itself (a
    screen sampan cannot use: a menu, a draft, copy mode, a status it cannot read; a `codex queue` error), fails, so it
    does not wait forever; the caller is told like any failure. Waiting for the target (busy, waiting for its user,
    not running, no thread yet) has no limit, as before."""
    created = parse_iso(d.extra.get(REQUEUED_AT)) or parse_iso(d.created)  # a resent one: since it was requeued
    if d.state != "queued" or created is None or (now - created).total_seconds() <= QUEUE_LIMIT_S:
        return
    if d.status == "rung":  # rung: the target has it; settle_ring decides when it goes back in line
        return
    if d.reason in WAITING or d.reason.startswith("對方狀態："):
        return
    last = f"（最後一次：{d.reason}）" if d.reason else ""
    _finish(d, "failed", now, f"排隊 {QUEUE_LIMIT_S // 60} 分鐘還送不出去{last}")
    _log(cfg, "send", f"派工 {d.id}：{d.reason}")


def _finish(d, state, now, reason="", reply=None):
    d.state, d.done_at, d.reason, d.reply = state, _iso(now), reason, reply


def _reply(cfg, row, text, start=None):
    if row.agent == "codex":
        path = find_rollout(cfg.codex.home, row.sid)
        return codex_reply_to(path, text, start) if path else Reply(False, False, "")
    path = find_transcript(cfg.profiles.values(), row.sid, prefer=row.profile)
    return claude_reply_to(path, text, start) if path else Reply(False, False, "")


def _starting(d, row, now):
    """A startup dispatch whose new session is not seen as a running sampan session yet: a Codex that has no thread
    (UNREGISTERED: as long as that agent runs there), or a tmux session without the agent seen (NO_AGENT, or a Claude
    row STOPPED while its tmux is there) for STARTUP_GRACE_S after it was sent."""
    if not d.startup or row is None:
        return False
    if row.state == State.UNREGISTERED:
        return row.agent == d.to_agent
    if row.state == State.NO_AGENT or (row.state == State.STOPPED and row.tmux_alive):
        sent = parse_iso(d.sent_at)
        return sent is None or (now - sent).total_seconds() < STARTUP_GRACE_S
    return False


def _check(cfg, d, row, now, run=None, items=None):
    """Judge a sent dispatch by the text of its delivery (a group paste: the whole pasted text). When it finishes, the
    other messages of that delivery finish with it: a reply is split by 〔回覆 id〕 markers into one part each."""
    conn = getattr(items, "conn", None)
    delivery = deliveries.delivery_of(conn, d.id) if conn is not None else None
    if delivery is not None and delivery["kind"] == "inbox":
        _check_inbox(cfg, d, row, now, items, delivery)
        return
    start = delivery["cursor"] if delivery is not None and row is not None and delivery["conversation"] == row.sid else None
    _judge(cfg, d, row, now, run, delivery["text"] if delivery is not None and delivery["text"] else d.text, start)
    if delivery is not None:
        _settle(conn, items, d, delivery, now)


def _settle(conn, items, d, delivery, now):
    """Carry d's judgement to the other messages of its delivery (only while it is active: a delivery that already
    ended, e.g. from a 0.16 migration, settles d alone). Done: each gets its marked section, or the whole reply."""
    active = delivery["state"] == deliveries.ACTIVE
    ids = deliveries.members(conn, delivery["id"]) if active else [d.id]
    others = [x for x in items if x is not d and x.id in ids and x.state == "sent"]
    if d.state == "sent":
        for x in others:
            x.reason = d.reason  # the two-look marks are the delivery's
            if d.thread_id:
                x.thread_id = d.thread_id
        return
    if d.state == "done":
        whole = d.reply
        every = deliveries.members(conn, delivery["id"], archived=True)  # a cancelled one's section is not another's
        marked = deliveries.split_markers(whole, every) if len(every) > 1 else {}
        for x in [d, *others]:
            text, how = (marked[x.id], "marker") if x.id in marked else (whole, "whole")
            deliveries.add_part(conn, delivery_id=delivery["id"], message=x.id, text=text, how=how, now=now)
            _finish(x, "done", now, reply=text)
    else:
        for x in others:
            _finish(x, d.state, now, d.reason)
    if active:
        deliveries.end_delivery(conn, delivery["id"], now)


def receipt_marker(delivery_id):
    """How an inbox delivery shows up in the target's transcript: the first line of the inbox_take result."""
    return f"收據 {delivery_id}"


def _check_inbox(cfg, d, row, now, items, delivery):
    """Judge every message of an inbox delivery (taken through inbox_take) at once. Once the target is idle and the
    turn after the take (the receipt in its transcript, read from the delivery's cursor) has ended, the delivery
    ends: a message with a reply part is replied, one without gets the turn's last text (fill), or is unanswered when
    there is none. A new conversation, or a take that is not in the transcript, ends it with no fill."""
    conn = items.conn
    if delivery["state"] != deliveries.ACTIVE:  # it ended without d (should not happen): d alone
        _end_inbox(cfg, conn, delivery, [d], now, "")
        return
    ids = deliveries.members(conn, delivery["id"])
    waiting = [x for x in items if x.id in ids and x.state == "sent"]
    if row is None or row.state in GONE or row.agent != d.to_agent:
        why = REASON_AGENT_CHANGED if row is not None and row.state not in GONE else REASON_STOPPED
        _end_inbox(cfg, conn, delivery, waiting, now, "", status="failed", reason=why)
        return
    if row.state != State.RUNNING:  # tmux unreadable, a trust prompt…: wait, do not judge
        return
    if not row.sid:  # not read this time (a /clear gives a new id, never none): wait
        return
    if delivery["conversation"] and row.sid != delivery["conversation"]:  # /clear: that turn is gone
        _end_inbox(cfg, conn, delivery, waiting, now, "")
        return
    for x in waiting:
        x.reason = REASON_PARTIAL if x.status == "partial" else REASON_TAKEN
    if row.agent_status not in READY:  # a busy look in between: the two idle looks start over
        _mark_turn_end(conn, delivery, "")
        return
    path = find_transcript(cfg.profiles.values(), row.sid, prefer=row.profile)
    info = after_marker(path, receipt_marker(delivery["id"]), start=delivery["cursor"]) if path else TurnInfo(False)
    created = parse_iso(delivery["created"])
    if (info.found and not info.ended) or (not info.found and created is not None
                                           and (now - created).total_seconds() < NOT_SEEN_GRACE_S):
        _mark_turn_end(conn, delivery, "")  # still going, or the take's result may not be written yet
        return
    if delivery["reason"] != TURN_END_SEEN:  # as a paste: the turn looks over on two idle looks in a row
        _mark_turn_end(conn, delivery, TURN_END_SEEN)
        return
    _end_inbox(cfg, conn, delivery, waiting, now, info.last_text if info.found else "")


TURN_END_SEEN = "這一輪看起來結束了"  # the mark of one idle look at an inbox delivery whose turn looked over (internal)


def _mark_turn_end(conn, delivery, mark):
    if (delivery["reason"] or "") != mark:
        conn.execute("UPDATE deliveries SET reason=? WHERE id=?", (mark, delivery["id"]))


def _end_inbox(cfg, conn, delivery, waiting, now, last_text, *, status="unanswered", reason=REASON_UNANSWERED):
    """End an inbox delivery: each waiting message with a part of it is replied (its latest part), one without gets
    last_text as a fill part, or (no text) fails with status / reason."""
    done, missed = [], []
    for x in waiting:
        part = deliveries.latest_part(conn, x.id, delivery=delivery["id"])
        if part is None and last_text:
            deliveries.add_part(conn, delivery_id=delivery["id"], message=x.id, text=last_text, how="fill", now=now)
            part = {"text": last_text}
        if part is not None:
            _finish(x, "done", now, reply=part["text"])
            x.status = "replied"
            done.append(x.id)
        else:
            _finish(x, "failed", now, reason)
            x.status = status
            missed.append(x.id)
    deliveries.end_delivery(conn, delivery["id"], now)
    said = [f"已回覆 {'、'.join(done)}"] if done else []
    said += [f"{reason} {'、'.join(missed)}"] if missed else []
    _log(cfg, "send", f"送達 {delivery['id']}：這一輪結束" + (f"（{'；'.join(said)}）" if said else ""))


def _judge(cfg, d, row, now, run, text, start=None):
    was_starting = d.reason == REASON_STARTING
    if _starting(d, row, now) or (d.startup and row is not None and row.state == State.AWAIT_TRUST):
        d.reason = REASON_STARTING  # wait, do not judge
        return
    if was_starting:
        d.reason = ""
    if row is None or row.state in GONE or row.agent != d.to_agent:
        _finish(d, "failed", now, REASON_AGENT_CHANGED if row is not None and row.state not in GONE else REASON_STOPPED)
        return
    if row.state != State.RUNNING:  # tmux unreadable, a trust prompt…: wait, do not judge
        return
    if not row.sid:
        d.reason = REASON_NO_THREAD
        return
    if row.agent == "codex":
        d.thread_id = row.sid
    if d.raw:
        _check_raw(cfg, d, row, now, run)
        return
    reply = _reply(cfg, row, text, start)
    if reply.found and row.agent == "codex":
        # Codex: only the rollout says the turn is over; idle looks (the screen can be misread mid-turn) never do
        if reply.text and (reply.done or reply.closed):
            _finish(d, "done", now, reply=reply.text)
        elif reply.aborted:
            _finish(d, "failed", now, REASON_ABORTED)
        elif reply.closed:
            _finish(d, "failed", now, REASON_SKIPPED)
        elif reply.done:  # the turn is complete and said nothing
            _finish(d, "failed", now, REASON_NO_TEXT)
        else:  # no task_complete and no turn_aborted yet: still working
            d.reason = ""
        return
    idle = row.agent_status in READY  # Claude's reply.done only means "some assistant text exists"
    if not idle:  # a mark is about two idle looks in a row: a busy look in between starts over
        if reply.found or d.reason in (REASON_NOT_SEEN, REASON_NO_TEXT_YET, REASON_SENDING, REASON_NO_THREAD):
            d.reason = ""
        return
    sent = parse_iso(d.sent_at)
    grace = STARTUP_GRACE_S if d.startup else NOT_SEEN_GRACE_S
    # the first look at a new session that has just come up never judges either
    early = was_starting or (sent is not None and (now - sent).total_seconds() < grace)
    if reply.found:
        if reply.text and (reply.done or reply.closed):
            _finish(d, "done", now, reply=reply.text)
        elif reply.closed:  # another message was typed after it, with no reply text in between
            _finish(d, "failed", now, REASON_SKIPPED)
        elif row.agent == "codex" and reply.done:  # the turn is complete and said nothing
            _finish(d, "failed", now, REASON_NO_TEXT)
        elif early:
            d.reason = ""
        elif d.reason == REASON_NO_TEXT_YET:  # idle twice in a row with no text: the turn ended without one
            _finish(d, "failed", now, REASON_NO_TEXT)
        else:
            d.reason = REASON_NO_TEXT_YET
        return
    if early:
        return
    if d.reason == REASON_NOT_SEEN:  # seen idle without the message twice in a row: it is not coming
        _finish(d, "failed", now, REASON_NOT_FOUND)
    else:
        d.reason = REASON_NOT_SEEN


def _check_raw(cfg, d, row, now, run):
    """A direct send (a / command Claude ran itself): done when its record is in the transcript at or after sent_at
    and the target was idle on two looks in a row (most commands write no reply text), or at once with reply text;
    done too when the conversation id changed (/clear). Not seen, but a menu or dialog on the screen on two looks in a
    row (e.g. /model with no argument): failed; sampan never presses a key on it."""
    if d.thread_id and row.sid != d.thread_id:
        _finish(d, "done", now, reply=REASON_NEW_THREAD.format(sid8=row.sid[:8]))
        return
    sent = parse_iso(d.sent_at)
    path = find_transcript(cfg.profiles.values(), row.sid, prefer=row.profile)
    reply = command_reply(path, d.text, sent) if path and sent else Reply(False, False, "")
    idle = row.agent_status in READY
    early = sent is not None and (now - sent).total_seconds() < NOT_SEEN_GRACE_S
    if reply.found:
        if reply.text and (reply.done or reply.closed) and idle:
            _finish(d, "done", now, reply=reply.text)
        elif not idle or early:
            d.reason = ""
        elif reply.closed or d.reason == REASON_NO_TEXT_YET:  # idle twice in a row: the command ran, no text
            _finish(d, "done", now, reply=REASON_RAN)
        else:
            d.reason = REASON_NO_TEXT_YET
        return
    if early:
        return
    if row.agent_status == "busy":  # still working (/compact's spinner may hide the box): never a menu, start over
        d.reason = ""
        return
    screen = Tmux(run).capture(row.pane, escapes=True) if run is not None and row.pane else ""
    if screen.strip() and claude_input_state(screen) not in ("empty", "draft"):
        if d.reason == REASON_MENU_SEEN:
            _finish(d, "failed", now, REASON_MENU)
        else:
            d.reason = REASON_MENU_SEEN
        return
    if not idle:
        d.reason = ""
    elif d.reason == REASON_NOT_SEEN:
        _finish(d, "failed", now, REASON_NOT_FOUND)
    else:
        d.reason = REASON_NOT_SEEN


def notify_caller(cfg, run, d, sleep, *, rows=None, snapshot_fn=None, status_fn=None, save=None, items=()):
    """Send the completion (or failure) notice to the caller (with Enter) if it is a running sampan session that can
    take it. Every other dispatch in items whose notice that same caller session is owed now goes into the same
    message (merged_notice_text), so a caller owed several notices gets one message, not one turn per dispatch.
    Changes d and the others merged with it (notified; notified plus a reason when the notice may have arrived but
    Enter failed or `codex queue` timed out, never sent twice; notify=False when the caller is gone); call it inside
    dispatch.transaction. save: write the records now, before any key (notified is set first, so a crash in the
    middle never sends the notice again). items: the transaction's list (the other notices owed, and for a batch's
    first item the batch: every item then gets the first one's notify / notified). True if sent."""
    if not _owes_notice(d, items):
        return False
    owers = [d]
    if d.from_kind == "session":
        owers += [x for x in items if x is not d and x.from_kind == "session" and _same_caller(x, d)
                  and _owes_notice(x, items)]
    members = [m for x in owers for m in (_group(items, x.batch) if x.batch is not None else [x])]
    if len(owers) == 1 and d.batch is not None:
        text = batch_notice_text(d.batch, members, d.batch_skipped)
    elif len(owers) == 1 and _inbox_note(getattr(items, "conn", None), d) is None:
        text = notice_text(d)
    else:
        text = merged_notice_text(sorted(owers, key=_position(items)), items)
    what = "、".join(f"批次 {x.batch}" if x.batch is not None else f"派工 {x.id}" for x in owers)
    return _notify(cfg, run, d, owers, members, text, what, sleep, rows, snapshot_fn, status_fn, save)


def _same_caller(x, d):
    return x.from_id == d.from_id if x.from_id and d.from_id else x.from_name == d.from_name


def _position(items):
    index = {id(x): i for i, x in enumerate(items)}
    return lambda x: index.get(id(x), len(index))


def merged_notice_text(owers, items):
    """One line for several notices owed to one caller, in this order: replies grouped by target, then each failure
    with its reason, then each batch, then where to read them, e.g.
    〔sampan：b 已回覆派工 a1b2、c3d4；c 已回覆派工 e5f6；給 d 的派工 0a0b 失敗：對方 session 已停止；
    批次 b1234 完成：2 完成、0 失敗、1 跳過；用 sampan 工具 dispatch_result 看內容〕 (without the line break).
    Messages answered through the inbox are told by their delivery: 給 b 的派工 a1b2、c3d4（一起送出）已回覆, 給 b 的派工
    a1b2：已取走，這一輪結束卻沒有回覆, or 給 b 的派工 a1b2 又多了一段回覆 (a part after the turn ended).
    owers: plain dispatches (done or failed) and batches' first items, in creation order."""
    replied, failed, batches, taken = {}, [], [], {}
    conn = getattr(items, "conn", None)
    for x in owers:
        note = _inbox_note(conn, x) if x.batch is None else None
        if note is not None:
            taken.setdefault((x.to_name, *note), []).append(x.id)
        elif x.batch is not None:
            group = _group(items, x.batch)
            done = sum(g.state == "done" for g in group)
            bad = sum(g.state in ("failed", "cancelled") for g in group)
            batches.append(f"批次 {x.batch} 完成：{done} 完成、{bad} 失敗、{x.batch_skipped} 跳過")
        elif x.state == "failed":
            failed.append(f"給 {x.to_name} 的派工 {x.id} 失敗：{' '.join((x.reason or '').split())}")
        else:
            replied.setdefault(x.to_name, []).append(x.id)
    parts = [f"{to} 已回覆派工 {'、'.join(ids)}" for to, ids in replied.items()]
    for (to, _delivery, how), ids in taken.items():
        together = "（一起送出）" if len(ids) > 1 and how != "more" else ""
        gap = together or (" " if how == "replied" else "")
        parts.append(f"給 {to} 的派工 {'、'.join(ids)}{gap}{INBOX_NOTES[how]}")
    parts += failed + batches
    return "〔sampan：" + "；".join(parts) + "；用 sampan 工具 dispatch_result 看內容〕"


INBOX_NOTES = {"replied": "已回覆", "unanswered": f"：{REASON_UNANSWERED}", "more": " 又多了一段回覆"}


def _inbox_note(conn, x):
    """For a finished message taken through inbox_take: (its delivery id, "replied" / "unanswered" / "more": a reply
    part came after its turn ended); None for any other message."""
    status = x.status or dispatch.STATUS_OF_STATE.get(x.state)
    if conn is None or status not in ("replied", "unanswered"):
        return None
    delivery = deliveries.delivery_of(conn, x.id)
    if delivery is None or delivery["kind"] != "inbox":
        return None
    if status == "replied":
        latest, end = deliveries.latest_part(conn, x.id, delivery=delivery["id"]), parse_iso(x.done_at)
        at = parse_iso(latest["at"]) if latest is not None else None
        if at is not None and end is not None and at > end:
            return delivery["id"], "more"
    return delivery["id"], status


def _notify(cfg, run, d, owers, members, text, what, sleep, rows, snapshot_fn, status_fn, save):
    """d: the dispatch the notice is sent for (its caller; its id names the outbox); owers: every dispatch the notice
    covers (d first), which get the note when it may have arrived; members: every record whose notify / notified
    follows (owers plus their batches' items)."""
    def mark(**changes):
        for m in members:
            for k, v in changes.items():
                setattr(m, k, v)
    if d.from_kind != "session":
        mark(notify=False)  # the terminal or an unregistered agent: there is no session to tell
        return False
    rows = rows if rows is not None else _rows(cfg, run, snapshot_fn, status_fn)
    row = _row_of(rows, d.from_id, d.from_name)
    if row is None or row.state in (State.RETIRED, State.UNREGISTERED, State.NO_AGENT):
        mark(notify=False)  # nobody left to tell: the reply stays in the record
        return False
    if row.state != State.RUNNING or (row.agent == "codex" and not row.sid):
        return False
    if row.agent != "codex" and (row.agent_status not in READY or row.pane is None):
        return False
    mark(notified=True)
    if save is not None:
        save()
    if row.agent == "codex":
        why = deliver.to_codex(cfg, run, row.sid, text)
    else:
        nid = _notice_id(d.id)
        why = deliver.to_claude(Tmux(run), row.pane, dispatch.write_outbox(cfg.state_dir, nid, text), nid, sleep)
    if isinstance(why, deliver.Unsure):  # the notice may be with the caller: count it as given, never send it again
        note = f"完成通知{REASON_PASTED}：{why}" if isinstance(why, deliver.Pasted) else f"完成通知{why}"
        for x in owers:
            x.reason = f"{x.reason}；{note}" if x.state == "failed" and x.reason else note
        _log(cfg, "send", f"{what}：{note}")
        return False
    if why is not None:
        mark(notified=False)  # nothing was sent: try again later
        return False
    _log(cfg, "send", f"{what}：已通知 {d.from_name}")
    return True


def set_notify(cfg, did, on):
    """Turn the completion notice of a dispatch from a sampan session on or off (an MCP tool that waited: off while
    it waits, on again when it gives up; off once the caller has read the reply). Never for one already notified.
    did may be a batch id: every item of the batch."""
    try:
        with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days, ids=[did]) as items:
            for d in items:
                if (d.id == did or d.batch == did) and not d.notified and d.from_kind == "session":
                    d.notify = bool(on)
    except (dispatch.DispatchError, OSError):
        pass


def pending(cfg, items=None):
    """Ids a watcher should keep advancing: queued, sent, and finished ones whose notice is still owed. items: the
    live messages already read (dispatch.load with ids), else they are read here (only those, never every message)."""
    items = dispatch.load(cfg.state_dir, ids=()) if items is None else items
    return [d.id for d in items if d.state in ("queued", "sent") or _owes_notice(d, items)]


def cancel_refusal(d):
    """Why dispatch d cannot be cancelled, or None (queued: not sent yet; sent: stop waiting for its reply)."""
    if d.state in ("queued", "sent"):
        return None
    return f"派工 {d.id} 是「{STATE_LABELS[d.state]}」，只能取消排隊中的，或不再等已送出的"


def cancel(cfg, did, *, now=None, wait_s=None):
    """Cancel a queued dispatch (it is never sent; a rung one leaves its ring, so a take never hands it over), or stop
    waiting for the reply of a sent one (taken, partial, pasted: the message is already with the target; its link to
    the delivery is archived, the others of that delivery go on; reason 「寄件者已取消」, no notice). A reply given to
    it after this is kept as late history. The cancelled Dispatch, or why not (str).
    wait_s: give up on a busy lock after that many seconds (the ui, which must not stall behind a delivery); None waits."""
    try:
        with dispatch.transaction(cfg.state_dir, wait_s=wait_s, keep_days=cfg.keep_days, ids=[did]) as items:
            d = next((x for x in items if x.id == did), None)
            if d is None:
                return f"沒有派工 {did}"
            refusal = cancel_refusal(d)
            if refusal:
                return refusal
            was, rung = d.state, d.status == "rung"
            _finish(d, "cancelled", now or _utcnow(), "已取消" if was == "queued" else REASON_CANCELLED)
            d.status = "cancelled"
            with dispatch._sqlite_errors(cfg.state_dir):
                if rung:
                    doorbell.drop_from_ring(items.conn, d.to_id or d.to_name, d.id)
                if was == "sent":
                    _leave_deliveries(items, d, now or _utcnow())
            result = replace(d)
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    what = "取消" if was == "queued" else REASON_CANCELLED
    _log(cfg, "cancel", f"派工 {did}：{what}（給 {result.to_name}）")
    return result


def _leave_deliveries(items, d, now):
    """d no longer waits for any delivery it was in (cancelled or resent): those links are archived (the others of
    each go on), and an active delivery nobody waits for any more ends, so the target's line moves."""
    conn = items.conn
    for r in conn.execute("SELECT delivery FROM delivery_messages WHERE message=? AND archived=0", (d.id,)).fetchall():
        deliveries.archive_member(conn, r["delivery"], d.id)
        delivery = conn.execute("SELECT state FROM deliveries WHERE id=?", (r["delivery"],)).fetchone()
        if delivery is None or delivery["state"] != deliveries.ACTIVE:
            continue
        ids = set(deliveries.members(conn, r["delivery"]))
        if not any(x.id in ids and x.state == "sent" for x in items):
            deliveries.end_delivery(conn, r["delivery"], now)


def cancel_done_text(d):
    """What to tell the user after cancel returned d."""
    if d.reason in (REASON_CANCELLED, REASON_STOP_WATCHING):
        return f"不再等派工 {d.id} 的回覆（訊息已送到 {d.to_name}）"
    return f"已取消派工 {d.id}"


def resend_refusal(d):
    """Why dispatch d cannot be resent, or None (failed, cancelled, or done and to be asked again)."""
    if d.state in RESENDABLE:
        return None
    return f"派工 {d.id} 是「{STATE_LABELS[d.state]}」，只能重送失敗、已取消或已回覆的"


def resend(cfg, did, *, now=None, notify=None):
    """Put a failed, cancelled or done dispatch back in line, as one transaction: its links to the deliveries it was
    in are archived (a reply to one of those is late history and never finishes it), and it is queued again with no
    reason, sent / done times or cached reply. It keeps its id, created (its place in line and in its batch) and every
    reply part it got; the queue limit counts from extra[REQUEUED_AT]. notify: None keeps whether its sender is told when it finishes (a notice already
    given is owed again). The queued Dispatch, or why not (str)."""
    now = now or _utcnow()
    try:
        with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days, ids=[did]) as items:
            d = next((x for x in items if x.id == did), None)
            if d is None:
                return f"沒有派工 {did}"
            refusal = resend_refusal(d)
            if refusal:
                return refusal
            with dispatch._sqlite_errors(cfg.state_dir):
                _leave_deliveries(items, d, now)
            d.state, d.status, d.extra = "queued", "queued", {**d.extra, REQUEUED_AT: _iso(now)}
            d.reason, d.sent_at, d.done_at, d.reply = "", None, None, None
            d.startup = False  # its session is up by now: an ordinary message
            if notify is not None:
                d.notify = bool(notify and d.from_kind == "session")
            d.notified = False
            result = replace(d)
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    _log(cfg, "send", f"派工 {did}：重送（給 {result.to_name}）")
    return result


def cmd_resend(cfg, run, out, did, *, sleep=time.sleep, snapshot_fn=None, status_fn=None, now_fn=_utcnow):
    """`sampan dispatches resend <id>`: queue it again and try once (a watcher moves it on, as any queued one)."""
    r = resend(cfg, did, now=now_fn())
    if isinstance(r, str):
        out(f"❌ {r}")
        return 2
    try:
        d = advance(cfg, run, did, sleep=sleep, snapshot_fn=snapshot_fn, status_fn=status_fn, now=now_fn())
    except (dispatch.DispatchError, RegistryError) as e:
        out(f"❌ {e}")
        return 2
    out(f"已重送派工 {did}：{STATE_LABELS[d.state]}" + (f"（{d.reason}）" if d.reason else ""))
    return 0


def status_of(d):
    """d's fine status (spec §6), also when it is left to be derived from its state."""
    return d.status or dispatch.STATUS_OF_STATE.get(d.state, d.state)


def history(cfg, did):
    """What a dispatch went through, read from dispatches.db (no lock: read only): its reply parts (deliveries.parts),
    the deliveries it was in (deliveries.history) and its last ring (or None)."""
    conn = dispatch._open(cfg.state_dir, fresh_ok=False)
    if conn is None:
        return {"parts": [], "deliveries": [], "ring": None}
    try:
        with dispatch._sqlite_errors(cfg.state_dir):
            ring = doorbell.last_ring_of(conn, did)
            return {"parts": deliveries.parts(conn, did), "deliveries": deliveries.history(conn, did),
                    "ring": {"code": ring["id"], "at": ring["at"], "result": ring["result"]} if ring else None}
    finally:
        conn.close()


def cmd_cancel(cfg, out, did):
    r = cancel(cfg, did)
    if isinstance(r, str):
        out(f"❌ {r}")
        return 2
    out(cancel_done_text(r))
    return 0


def _advance_until(cfg, run, did, wait_s, *, sleep, clock, snapshot_fn, status_fn, now_fn):
    """Advance once, then every 2 s while it is not finished and wait_s has not passed. The last copy."""
    start = clock()
    while True:
        d = advance(cfg, run, did, sleep=sleep, snapshot_fn=snapshot_fn, status_fn=status_fn, now=now_fn())
        if d.state in dispatch.FINISHED or clock() - start >= wait_s:
            return d
        sleep(POLL_S)


def cmd_send(cfg, run, out, to, message, *, wait, timeout=600, sleep=time.sleep, clock=time.monotonic,
             snapshot_fn=None, status_fn=None, now_fn=_utcnow, caller=TERMINAL, raw=False):
    """`sampan send`. caller: who runs it (command_caller: an agent running it through its shell tool is that session
    or an unregistered agent, never the terminal). Without wait: try once, print the id (rc 0, also when it is only
    queued). With wait: advance every 2 s; done → the reply (rc 0), failed → the reason (rc 1), timeout → rc 124
    (the dispatch stays), Ctrl-C → rc 130 (the dispatch stays). raw: a direct send (see submit)."""
    r = submit(cfg, run, caller, to, message, notify=False, now=now_fn(), snapshot_fn=snapshot_fn, status_fn=status_fn,
               raw=raw)
    if isinstance(r, str):
        out(f"❌ {r}")
        return 2
    did = r.id
    try:
        try:
            d = _advance_until(cfg, run, did, timeout if wait else 0, sleep=sleep, clock=clock,
                               snapshot_fn=snapshot_fn, status_fn=status_fn, now_fn=now_fn)
        except (dispatch.DispatchError, RegistryError) as e:
            out(f"❌ {e}")
            return 2
        if not wait:
            out(f"派工 {did}：{STATE_LABELS[d.state]}" + (f"（{d.reason}）" if d.reason else "")
                + (f"；sampan dispatches show {did} 看回覆" if d.state in ("queued", "sent") else ""))
            return 0
        if d.state == "done":
            out(d.reply or "")
            return 0
        if d.state in ("failed", "cancelled"):
            out(f"❌ 派工 {did}{STATE_LABELS[d.state]}：{d.reason}")
            return 1
        out(f"還在做，派工 {did}（sampan dispatches show {did} 看狀態）")
        return 124
    except KeyboardInterrupt:
        out(f"\n不等了，派工 {did} 照樣留著（sampan dispatches show {did} 看狀態）")
        return 130


def _advance_batch_until(cfg, run, bid, wait_s, *, sleep, clock, snapshot_fn, status_fn, now_fn, report=False):
    """advance_batch once, then every 2 s while an item is not finished and wait_s has not passed. The last copies."""
    start = clock()
    while True:
        items = advance_batch(cfg, run, bid, sleep=sleep, snapshot_fn=snapshot_fn, status_fn=status_fn, now=now_fn(),
                              report=report)
        if all(d.state in dispatch.FINISHED for d in items) or clock() - start >= wait_s:
            return items
        sleep(POLL_S)


def cmd_send_batch(cfg, run, out, names, message, *, wait, timeout=600, raw=False, sleep=time.sleep,
                   clock=time.monotonic, snapshot_fn=None, status_fn=None, now_fn=_utcnow, caller=TERMINAL):
    """`sampan send-batch`: the same message to every running, idle session of names (the others are skipped and
    listed). Without wait: try once, print each item (rc 0). With wait: advance every 2 s; every item done → the
    replies (rc 0), any failed / cancelled → rc 1, timeout → rc 124, Ctrl-C → rc 130 (the dispatches stay); nobody to
    send to → rc 2."""
    b = submit_batch(cfg, run, caller, names, message, raw=raw, notify=False, now=now_fn(), snapshot_fn=snapshot_fn,
                     status_fn=status_fn)
    if isinstance(b, str):
        out(f"❌ {b}")
        return 2
    out(f"批次 {b.id}：送給 {len(b.items)} 個 session" + (f"，跳過 {len(b.skipped)} 個" if b.skipped else ""))
    for name, why in b.skipped:
        out(f"  跳過 {name}：{why}")
    try:
        try:
            items = _advance_batch_until(cfg, run, b.id, timeout if wait else 0, sleep=sleep, clock=clock,
                                         snapshot_fn=snapshot_fn, status_fn=status_fn, now_fn=now_fn)
        except (dispatch.DispatchError, RegistryError) as e:
            out(f"❌ {e}")
            return 2
        if not wait:
            for d in items:
                out(f"  派工 {d.id} → {d.to_name}：{STATE_LABELS[d.state]}" + (f"（{d.reason}）" if d.reason else ""))
            out("sampan dispatches show <編號> 看回覆")
            return 0
        for d in items:
            if d.state == "done":
                out(f"──── {d.to_name}（派工 {d.id}）────")
                out(d.reply or "")
            elif d.state in ("failed", "cancelled"):
                out(f"❌ {d.to_name}（派工 {d.id}）{STATE_LABELS[d.state]}：{d.reason}")
            else:
                out(f"還在做：{d.to_name}（派工 {d.id}，sampan dispatches show {d.id} 看狀態）")
        if any(d.state not in dispatch.FINISHED for d in items):
            return 124
        return 1 if any(d.state != "done" for d in items) else 0
    except KeyboardInterrupt:
        out(f"\n不等了，批次 {b.id} 的派工照樣留著（sampan dispatches 看狀態）")
        return 130


def cmd_show(cfg, run, out, did, *, wait=0, sleep=time.sleep, clock=time.monotonic, snapshot_fn=None, status_fn=None,
             now_fn=_utcnow):
    """`sampan dispatches show <id> [--wait N]`: advance it once (or every 2 s for up to N seconds until it is
    finished), then print its state, who to whom, the reason and the full reply. rc 0 done or still going (no wait),
    1 failed / cancelled, 2 unknown id or unreadable, 124 still going after --wait, 130 Ctrl-C."""
    try:
        try:
            d = _advance_until(cfg, run, did, max(0, wait), sleep=sleep, clock=clock, snapshot_fn=snapshot_fn,
                               status_fn=status_fn, now_fn=now_fn)
        except (dispatch.DispatchError, RegistryError) as e:
            out(f"❌ {e}")
            return 2
    except KeyboardInterrupt:
        out(f"\n不等了，派工 {did} 照樣留著")
        return 130
    try:
        h = history(cfg, d.id)
    except dispatch.DispatchError as e:
        out(f"❌ {e}")
        return 2
    label, fine = STATE_LABELS[d.state], STATUS_LABELS.get(status_of(d), status_of(d))
    out(f"派工 {d.id}：{label}" + (f"（{fine}）" if fine != label else ""))
    out(f"從 {_who(d)} 給 {d.to_name}（{d.to_agent}），建立 {fmt_local(parse_iso(d.created))}")
    if d.reason:
        out(f"說明：{d.reason}")
    _show_history(out, d, h)
    if d.state == "done":
        out("──── 回覆 ────")
        out(d.reply or "")
    if d.state in ("failed", "cancelled"):
        return 1
    return 124 if wait > 0 and d.state not in dispatch.FINISHED else 0


def _show_history(out, d, h):
    """The reply parts, deliveries and last ring of d for `dispatches show`. The text of the part printed below as the
    reply (done: the latest) is not repeated."""
    if h["parts"]:
        out("回覆段落：")
        for p in h["parts"]:
            flags = "、".join(f for f, on in (("最新", p["latest"]), ("遲到", p["late"])) if on)
            out(f"  {p['id']} {HOW_LABELS.get(p['how'], p['how'])}，{fmt_local(parse_iso(p['at']))}"
                + (f"（{flags}）" if flags else ""))
            if p["text"] is None:
                out("（已清除）")
            elif not (d.state == "done" and p["latest"] and p["text"] == d.reply):
                out(p["text"])
    if h["deliveries"]:
        out("送達：")
        for x in h["deliveries"]:
            out(f"  {x['id']} {KIND_LABELS.get(x['kind'], x['kind'])}，{fmt_local(parse_iso(x['created']))}"
                + (f"，一起送的：{'、'.join(x['with'])}" if x["with"] else "") + ("（已封存）" if x["archived"] else ""))
    if h["ring"]:
        r = h["ring"]
        out(f"門鈴 {r['code']}：{fmt_local(parse_iso(r['at']))}，{RING_LABELS.get(r['result'], r['result'])}")


def _ljust(text, width):
    """Pad by display width (CJK takes two cells); a longer text is kept whole."""
    return text + " " * max(0, width - text_width(text))


TAIPEI = timezone(timedelta(hours=8))  # search days are Taipei days whatever the machine's timezone is (no DST there)
PREVIEW_CHARS = 60
PURGED_TEXT = "（本文已清除）"
INBOX_RECENT = 20
MESSAGE_COLUMNS = "id, created, from_name, to_name, state, status, text, purged"


def _day_start_utc(day):
    """The UTC instant a Taipei day starts, as the 'YYYY-MM-DD HH:MM:SS' text sqlite's datetime() gives."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return datetime(day.year, day.month, day.day, tzinfo=TAIPEI).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _like_literal(keyword):
    """keyword as a LIKE pattern that matches it literally: backslash, % and _ escaped (ESCAPE '\\'), wrapped in %...%."""
    return "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(conn, *, keyword=None, from_ids=(), to_ids=(), since=None, until=None, state=None, limit=50):
    """Messages (newest first, at most limit; None = all) matching every given filter. keyword is literal text looked
    for in the text and the reply (a purged message has neither: only metadata finds it); from_ids / to_ids are
    session ids or names (any one matches); since / until are Taipei days (a date or YYYY-MM-DD), until included;
    state is a state or a fine status."""
    where, args = [], []
    if keyword:
        where.append("(text LIKE ? ESCAPE '\\' OR reply LIKE ? ESCAPE '\\')")
        args += [_like_literal(keyword)] * 2
    for column, values in (("from", from_ids), ("to", to_ids)):
        if values:
            marks = ",".join("?" * len(values))
            where.append(f"({column}_id IN ({marks}) OR {column}_name IN ({marks}))")
            args += [*values, *values]
    if since:
        where.append("datetime(created) >= ?")
        args.append(_day_start_utc(since))
    if until:
        last = date.fromisoformat(until) if isinstance(until, str) else until
        where.append("datetime(created) < ?")
        args.append(_day_start_utc(last + timedelta(days=1)))
    if state:
        where.append("(state = ? OR status = ?)")
        args += [state, state]
    sql = (f"SELECT {MESSAGE_COLUMNS} FROM messages" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY datetime(created) DESC, rowid DESC")
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def _message_line(m):
    flat = " ".join(deliver.body(m["text"] or "").split())
    body = PURGED_TEXT if m["purged"] or m["text"] is None else (flat if len(flat) <= PREVIEW_CHARS else flat[:PREVIEW_CHARS] + "…")
    state = STATUS_LABELS.get(m["status"]) or STATE_LABELS.get(m["state"], m["state"])
    cells = (m["id"], fmt_local(parse_iso(m["created"])), m["from_name"] or "", m["to_name"] or "", state)
    return " ".join(_ljust(c, w) for c, w in zip(cells, (5, 11, 16, 16, 12))) + f" {body}"


@contextmanager
def _read_db(cfg):
    """The db opened for reading (never created): the connection, or None when there are no dispatches yet."""
    conn = dispatch._open(cfg.state_dir, fresh_ok=False)
    try:
        with dispatch._sqlite_errors(cfg.state_dir):
            yield conn
    finally:
        if conn is not None:
            conn.close()


def _registry_rows(cfg):
    try:
        return load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except RegistryError:
        return []


def _ids_of(cfg, names):
    """Each name as given plus, when it names a registered session (name, alias, id or id prefix), its id and name."""
    rows, out = _registry_rows(cfg), []
    for n in names:
        out.append(n)
        for r in rows:
            if n in (r.tmux, r.alias, r.id) or (len(n) >= 4 and r.id.startswith(n)):
                out += [r.id, r.tmux]
    return tuple(dict.fromkeys(out))


def cmd_search(cfg, out, keyword=None, *, from_=(), to=(), since=None, until=None, state=None, limit=50):
    """`sampan dispatches search`: one line per message (id, time, from, to, state, first 60 characters). rc 0, 2 on a
    bad date or an unreadable db. limit 0 = all."""
    try:
        for day in (since, until):
            if day:
                date.fromisoformat(day)
    except ValueError:
        out("❌ 日期要寫成 YYYY-MM-DD（台北時間）")
        return 2
    try:
        with _read_db(cfg) as conn:
            if conn is None:
                out("沒有派工")
                return 0
            found = search(conn, keyword=keyword, from_ids=_ids_of(cfg, from_), to_ids=_ids_of(cfg, to), since=since,
                           until=until, state=state, limit=limit or None)
    except dispatch.DispatchError as e:
        out(f"❌ {e}")
        return 2
    if not found:
        out("沒有符合的派工")
        return 0
    for m in found:
        out(_message_line(m))
    return 0


def _ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} 秒前"
    return f"{seconds // 60} 分鐘前" if seconds < 7200 else f"{seconds // 3600} 小時前"


def _beat_text(conn, row, now, alive, start_of):
    """The target's heartbeat in words: valid (its newest beat), expired / not matching, none, or Codex (has none)."""
    if row is None:
        return "不在登記表"
    if row.agent == "codex":
        return "Codex 沒有心跳（一律貼全文）"
    ok = heartbeat.valid(conn, session_id=row.id, conversation=row.session_id, now=now, alive=alive, start_of=start_of)
    if ok is not None:
        return f"心跳 {_ago((now - parse_iso(ok['at'])).total_seconds())}"
    last = conn.execute("SELECT at FROM inboxes WHERE session_id=? ORDER BY at DESC LIMIT 1", (row.id,)).fetchone()
    at = parse_iso(last["at"]) if last else None
    return f"心跳已失效（最後一次 {_ago((now - at).total_seconds())}）" if at else "沒有心跳"


def _find_session(cfg, name):
    """The registered session `name` means (name, alias, id or a unique id prefix), or a str saying why not."""
    rows = _registry_rows(cfg)
    hits = [r for r in rows if name in (r.tmux, r.alias, r.id)] or [r for r in rows if len(name) >= 4 and r.id.startswith(name)]
    if len(hits) == 1:
        return hits[0]
    return f"找不到 session「{name}」：不在 sampan 登記表裡" if not hits else f"「{name}」對到不只一個 session，請寫完整名稱"


def cmd_inbox(cfg, out, name, *, now_fn=_utcnow, alive=os.kill, start_of=procs.start_time):
    """`sampan dispatches inbox <session>`: what that session was sent: how many are queued, rung and waiting for the
    pick-up, taken without a reply; its last heartbeat and last doorbell; the INBOX_RECENT newest messages. Read only."""
    row = _find_session(cfg, name)
    if isinstance(row, str):
        out(f"❌ {row}")
        return 2
    now = now_fn()
    mine = "(to_id = :id OR (to_id = '' AND to_name = :name))"
    who = {"id": row.id, "name": row.tmux}
    try:
        with _read_db(cfg) as conn:
            if conn is None:
                out(f"{row.tmux}：沒有收到過派工")
                return 0
            n = dict(conn.execute(f"SELECT SUM(status = 'queued') AS queued, SUM(status = 'rung') AS rung, "
                                  f"SUM(status IN ('taken', 'partial')) AS taken FROM messages WHERE {mine}", who).fetchone())
            out(f"{row.tmux}（{row.agent}）的收件匣")
            out(f"排隊中 {n['queued'] or 0} 則、已按門鈴等取信 {n['rung'] or 0} 則、已取走沒回覆 {n['taken'] or 0} 則")
            out(_beat_text(conn, row, now, alive, start_of))
            ring = doorbell.latest_ring(conn, row.id)
            out(f"最後一次門鈴：{fmt_local(parse_iso(ring['at']))}，{RING_LABELS.get(ring['result'], ring['result'])}"
                if ring else "還沒按過門鈴")
            recent = conn.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE {mine} "
                                  f"ORDER BY datetime(created) DESC, rowid DESC LIMIT {INBOX_RECENT}", who).fetchall()
    except dispatch.DispatchError as e:
        out(f"❌ {e}")
        return 2
    if recent:
        out(f"最近 {len(recent)} 則：")
        for m in recent:
            out("  " + _message_line(m))
    return 0


def _by_target(cfg, out, now, alive, start_of):
    """`sampan dispatches --by-target`: the targets with queued or sent messages, how many, and their heartbeat."""
    try:
        with _read_db(cfg) as conn:
            groups = [] if conn is None else conn.execute(
                "SELECT to_id, to_name, SUM(state = 'queued') AS queued, SUM(state = 'sent') AS sent FROM messages "
                "WHERE state IN ('queued', 'sent') GROUP BY to_id, to_name ORDER BY to_name").fetchall()
            if not groups:
                out("沒有進行中的派工")
                return 0
            registered = {r.id: r for r in _registry_rows(cfg)}
            for g in groups:
                out(f"{_ljust(g['to_name'], 24)} 排隊 {g['queued']}、等回覆 {g['sent']}；"
                    + _beat_text(conn, registered.get(g["to_id"]), now, alive, start_of))
    except dispatch.DispatchError as e:
        out(f"❌ {e}")
        return 2
    return 0


def cmd_dispatches(cfg, out, *, all_, by_target=False, now_fn=_utcnow, alive=os.kill, start_of=procs.start_time):
    if by_target:
        return _by_target(cfg, out, now_fn(), alive, start_of)
    try:
        items = dispatch.load(cfg.state_dir, None if all_ else ())  # without --all only the live ones are read
    except dispatch.DispatchError as e:
        out(f"❌ {e}")
        return 2
    shown = items if all_ else [d for d in items if d.state in ("queued", "sent")]
    if not shown:
        out("沒有派工" if all_ else "沒有進行中的派工（--all 看全部）")
        return 0
    widths = (6, 24, 24, 14, 11)
    out(" ".join(_ljust(h, w) for h, w in zip(("編號", "從", "給", "狀態", "建立"), widths)) + " 說明")
    for d in shown:
        note = d.reason or (_preview(d.reply) if d.state == "done" and d.reply else "")
        cells = (d.id, d.from_name, d.to_name, STATE_LABELS[d.state], fmt_local(parse_iso(d.created)))
        out(" ".join(_ljust(c, w) for c, w in zip(cells, widths)) + f" {note}")
    return 0
