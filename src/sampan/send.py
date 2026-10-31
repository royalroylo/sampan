"""Sending a message to a session: submit a dispatch, advance it (queued → sent → done / failed), notify the caller.

Actions layer. Every state change happens inside dispatch.transaction: the latest record is read, judged and written
back under one lock, so two watchers (an MCP server and the sampan screen) never deliver the same dispatch twice.
The target's conversation id is read from status_rows on every advance; Dispatch.thread_id is only a cache (for a
direct send to Claude: the conversation it went to, so a /clear that starts a new one is told from a lost message).
"""
import os
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import actionlog, deliver, dispatch
from .claude_state import Reply, command_reply, find_transcript
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
REASON_STOP_WATCHING = "不再等回覆"
REASON_RAN = "指令已執行（沒有文字回覆）"  # a direct send done without reply text: its reply
REASON_NEW_THREAD = "對方換成新對話（{sid8}）"  # a direct send (/clear…) after which the target's conversation id changed
REASON_MENU = "指令打開了選單，請自己到那個 session 處理"
REASON_MENU_SEEN = deliver.REASON_BLOCKED  # one look at a menu after a direct send (two in a row fail it)
READY = ("idle", "shell")  # Claude statuses where the prompt takes input ("shell": the turn is over, a background
# shell task still runs; Claude 2.1.287 reports it until the task ends, so waiting for "idle" would hold messages)
BUSY = {"busy": "對方正在忙", "working": "對方正在忙", "waiting": "對方在等使用者回應"}
WAITING = {*BUSY.values(), REASON_NOT_RUNNING, REASON_NO_THREAD}  # queued reasons that clear by themselves: no limit
RUNNING_VERSION = current_version()  # read once at import: the code this process runs, even after an upgrade on disk


def process_tag():
    """Which sampan process wrote a log line (several run at once: the screen, each agent's MCP server, commands)."""
    return f"〔sampan {RUNNING_VERSION}，pid {os.getpid()}〕"


def _log(cfg, label, line):
    actionlog.append(cfg, label, [line + process_tag()])


STATE_LABELS = {"queued": "排隊中", "sent": "已送出、等回覆", "done": "已回覆", "failed": "失敗", "cancelled": "已取消"}
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
        with dispatch.transaction(cfg.state_dir) as items:
            d = _record(cfg, caller, to, to_agent, to_profile, message, state, now, {d.id for d in items},
                        notify=notify, thread_id=thread_id, strip=strip, startup=startup, raw=raw, batch=batch,
                        to_id=to_id)
            _keep(cfg, items, now, [d])
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


def _keep(cfg, items, now, new):
    """Prune items (dropping the outboxes of what goes) and add the new dispatches at the end."""
    kept = dispatch.prune(items, now)
    _drop_outboxes(cfg, {x.id for x in items} - {x.id for x in kept})
    items[:] = kept + new


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
    return "b" + dispatch.new_id({d.batch[1:] for d in items if d.batch})


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
        with dispatch.transaction(cfg.state_dir) as items:
            bid, taken, made = _new_batch_id(items), {d.id for d in items}, []
            for row in targets:
                d = _record(cfg, caller, row.name, row.agent, row.profile, message, "queued", now, taken,
                            notify=notify, thread_id=(row.sid or None) if row.agent == "codex" else None, raw=raw,
                            batch=bid, to_id=row.id)
                taken.add(d.id)
                made.append(d)
            made[0].batch_skipped = len(skipped)
            _keep(cfg, items, now, made)
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
    return [d for d in dispatch.load(cfg.state_dir) if d.batch == bid]


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
        with dispatch.transaction(cfg.state_dir) as items:
            d = next((x for x in items if x.id == did), None)
            if d is not None and d.state == "sent":
                _finish(d, "failed", now, reason)
                d.notify = False  # the caller is told right away by `sampan new` / new_session
    except (dispatch.DispatchError, OSError):
        return
    _log(cfg, "send", f"派工 {did}：{reason}")


def _drop_outboxes(cfg, ids):
    for did in ids:
        for name in (did, _notice_id(did)):
            try:
                dispatch.outbox_path(cfg.state_dir, name).unlink(missing_ok=True)
            except OSError:
                pass


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
    with dispatch.transaction(cfg.state_dir) as items:
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
        if d.state == "queued":
            _deliver(cfg, run, d, _row_of(rows(), d.to_id, d.to_name), sleep, now, save)
            _expire(cfg, d, now)
        elif d.state == "sent":
            _check(cfg, d, _row_of(rows(), d.to_id, d.to_name), now, run)
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


def _deliver(cfg, run, d, row, sleep, now, save):
    """Two-phase: the record is written as `sent` (「送出中」) before any key is pressed or `codex queue` runs, so a
    process killed in the middle never sends it again (at most once: it later fails as 找不到回覆). It goes back to
    queued only when the step says nothing was sent."""
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
        d.thread_id = row.sid
        _sending(d, now, save)
        why = deliver.to_codex(cfg, run, row.sid, d.text)  # `codex queue` waits for the turn by itself
    else:
        if row.agent_status not in READY:
            d.reason = BUSY.get(row.agent_status, "看不出對方是否閒著")
            return
        if row.pane is None:
            d.reason = REASON_NOT_RUNNING
            return
        outbox = dispatch.outbox_path(cfg.state_dir, d.id)
        if not outbox.exists():
            outbox = dispatch.write_outbox(cfg.state_dir, d.id, d.text)
        if d.raw:
            d.thread_id = row.sid or None  # the conversation it went to: a /clear changes it
        _sending(d, now, save)
        why = deliver.to_claude(Tmux(run), row.pane, outbox, d.id, sleep, raw=d.raw)
    if isinstance(why, deliver.Unsure):  # it may have reached the target: never send the same dispatch twice
        _finish(d, "failed", now, f"{REASON_PASTED}：{why}" if isinstance(why, deliver.Pasted) else str(why))
        _log(cfg, "send", f"派工 {d.id}：{d.reason}")
        return
    if why:
        d.state, d.sent_at, d.reason = "queued", None, why  # nothing was sent: try again later
        return
    d.reason = ""
    _log(cfg, "send", f"派工 {d.id}：已送到 {d.to_name}")


def _expire(cfg, d, now):
    """A dispatch still queued QUEUE_LIMIT_S after it was created, for a reason that does not go away by itself (a
    screen sampan cannot use: a menu, a draft, copy mode, a status it cannot read; a `codex queue` error), fails, so it
    does not wait forever; the caller is told like any failure. Waiting for the target (busy, waiting for its user,
    not running, no thread yet) has no limit, as before."""
    created = parse_iso(d.created)
    if d.state != "queued" or created is None or (now - created).total_seconds() <= QUEUE_LIMIT_S:
        return
    if d.reason in WAITING or d.reason.startswith("對方狀態："):
        return
    last = f"（最後一次：{d.reason}）" if d.reason else ""
    _finish(d, "failed", now, f"排隊 {QUEUE_LIMIT_S // 60} 分鐘還送不出去{last}")
    _log(cfg, "send", f"派工 {d.id}：{d.reason}")


def _sending(d, now, save):
    d.state, d.sent_at, d.reason = "sent", _iso(now), REASON_SENDING
    save()


def _finish(d, state, now, reason="", reply=None):
    d.state, d.done_at, d.reason, d.reply = state, _iso(now), reason, reply


def _reply(cfg, row, text):
    if row.agent == "codex":
        path = find_rollout(cfg.codex.home, row.sid)
        return codex_reply_to(path, text) if path else Reply(False, False, "")
    path = find_transcript(cfg.profiles.values(), row.sid, prefer=row.profile)
    return claude_reply_to(path, text) if path else Reply(False, False, "")


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


def _check(cfg, d, row, now, run=None):
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
    reply = _reply(cfg, row, d.text)
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
    if len(owers) == 1:
        text = notice_text(d) if d.batch is None else batch_notice_text(d.batch, members, d.batch_skipped)
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
    owers: plain dispatches (done or failed) and batches' first items, in creation order."""
    replied, failed, batches = {}, [], []
    for x in owers:
        if x.batch is not None:
            group = _group(items, x.batch)
            done = sum(g.state == "done" for g in group)
            bad = sum(g.state in ("failed", "cancelled") for g in group)
            batches.append(f"批次 {x.batch} 完成：{done} 完成、{bad} 失敗、{x.batch_skipped} 跳過")
        elif x.state == "failed":
            failed.append(f"給 {x.to_name} 的派工 {x.id} 失敗：{' '.join((x.reason or '').split())}")
        else:
            replied.setdefault(x.to_name, []).append(x.id)
    parts = [f"{to} 已回覆派工 {'、'.join(ids)}" for to, ids in replied.items()] + failed + batches
    return "〔sampan：" + "；".join(parts) + "；用 sampan 工具 dispatch_result 看內容〕"


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
        with dispatch.transaction(cfg.state_dir) as items:
            for d in items:
                if (d.id == did or d.batch == did) and not d.notified and d.from_kind == "session":
                    d.notify = bool(on)
    except (dispatch.DispatchError, OSError):
        pass


def pending(cfg):
    """Ids a watcher should keep advancing: queued, sent, and finished ones whose notice is still owed."""
    items = dispatch.load(cfg.state_dir)
    return [d.id for d in items if d.state in ("queued", "sent") or _owes_notice(d, items)]


def cancel_refusal(d):
    """Why dispatch d cannot be cancelled, or None (queued: not sent yet; sent: stop waiting for its reply)."""
    if d.state in ("queued", "sent"):
        return None
    return f"派工 {d.id} 是「{STATE_LABELS[d.state]}」，只能取消排隊中的，或不再等已送出的"


def cancel(cfg, did, *, now=None, wait_s=None):
    """Cancel a queued dispatch (it is never sent), or stop waiting for the reply of a sent one (the message is
    already with the target; reason 「不再等回覆」, no notice). The cancelled Dispatch, or why not (str).
    wait_s: give up on a busy lock after that many seconds (the ui, which must not stall behind a delivery); None waits."""
    try:
        with dispatch.transaction(cfg.state_dir, wait_s=wait_s) as items:
            d = next((x for x in items if x.id == did), None)
            if d is None:
                return f"沒有派工 {did}"
            refusal = cancel_refusal(d)
            if refusal:
                return refusal
            was = d.state
            _finish(d, "cancelled", now or _utcnow(), "已取消" if was == "queued" else REASON_STOP_WATCHING)
            result = replace(d)
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    what = "取消" if was == "queued" else REASON_STOP_WATCHING
    _log(cfg, "cancel", f"派工 {did}：{what}（給 {result.to_name}）")
    return result


def cancel_done_text(d):
    """What to tell the user after cancel returned d."""
    if d.reason == REASON_STOP_WATCHING:
        return f"不再等派工 {d.id} 的回覆（訊息已送到 {d.to_name}）"
    return f"已取消派工 {d.id}"


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
    out(f"派工 {d.id}：{STATE_LABELS[d.state]}")
    out(f"從 {_who(d)} 給 {d.to_name}（{d.to_agent}），建立 {fmt_local(parse_iso(d.created))}")
    if d.reason:
        out(f"說明：{d.reason}")
    if d.state == "done":
        out("──── 回覆 ────")
        out(d.reply or "")
    if d.state in ("failed", "cancelled"):
        return 1
    return 124 if wait > 0 and d.state not in dispatch.FINISHED else 0


def _ljust(text, width):
    """Pad by display width (CJK takes two cells); a longer text is kept whole."""
    return text + " " * max(0, width - text_width(text))


def cmd_dispatches(cfg, out, *, all_):
    try:
        items = dispatch.load(cfg.state_dir)
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
