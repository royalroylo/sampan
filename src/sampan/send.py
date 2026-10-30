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
REASON_STOP_WATCHING = "不再等回覆"
REASON_RAN = "指令已執行（沒有文字回覆）"  # a direct send done without reply text: its reply
REASON_NEW_THREAD = "對方換成新對話（{sid8}）"  # a direct send (/clear…) after which the target's conversation id changed
REASON_MENU = "指令打開了選單，請自己到那個 session 處理"
REASON_MENU_SEEN = deliver.REASON_BLOCKED  # one look at a menu after a direct send (two in a row fail it)
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
    return Caller(row.tmux, row.profile, agent, pane.pane_id)


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


def from_session(d, name):
    """Whether dispatch d was sent by the sampan session called name (name None → never)."""
    return name is not None and d.from_kind == "session" and d.from_name == name


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
                thread_id=(row.sid or None) if row.agent == "codex" else None, raw=raw, batch=batch)
    if isinstance(r, str):
        return r
    kind = f"{row.agent}，直接送" if raw else row.agent
    _log(cfg, "send", f"派工 {r.id}：{_who(r)} → {to}（{kind}）：{_preview(message)}")
    return r


def _who(d):
    return d.from_name if d.from_profile == "" else f"{d.from_name}（{d.from_profile}）"


def _create(cfg, caller, to, to_agent, to_profile, message, state, now, *, notify, thread_id=None, strip=False,
            startup=False, raw=False, batch=None):
    """Write the outbox and record a dispatch under the lock. A copy of it, or why not (str).
    strip: drop trailing newlines from the text (a startup prompt goes through `$(cat …)`, which drops them)."""
    try:
        with dispatch.transaction(cfg.state_dir) as items:
            did = dispatch.new_id({d.id for d in items})
            text = message if raw else deliver.compose(caller_label(caller), caller.profile, did, message)
            if strip or state == "queued":  # a trailing newline (stdin) is never part of the message
                text = text.rstrip("\n")
            dispatch.write_outbox(cfg.state_dir, did, text)
            d = dispatch.Dispatch(
                did, caller_label(caller), caller.profile, caller.agent,
                to, to_agent, to_profile, text, state, _iso(now),
                sent_at=_iso(now) if state == "sent" else None,
                notify=bool(notify and caller.name is not None), thread_id=thread_id, from_kind=caller_kind(caller),
                startup=startup, raw=raw, batch=batch)
            kept = dispatch.prune(items, now)
            _drop_outboxes(cfg, {x.id for x in items} - {x.id for x in kept})
            items[:] = kept + [d]
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    return replace(d)


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


def _owes_notice(d):
    return d.state in ("done", "failed") and d.notify and not d.notified


def advance(cfg, run, did, *, sleep, snapshot_fn=None, status_fn=None, now=None, report=False):
    """Move one dispatch forward as far as it can go now; returns a copy of it. Finished ones are left alone.
    report: the caller gets the returned copy right away (an MCP tool result): if it is finished, its notice is
    dropped in the same transaction, so the caller is never told twice.
    Raises DispatchError for an unknown id or an unreadable dispatches.json, RegistryError for a bad registry."""
    now = now or _utcnow()
    with dispatch.transaction(cfg.state_dir) as items:
        d = next((x for x in items if x.id == did), None)
        if d is None:
            raise dispatch.DispatchError(f"沒有派工 {did}")
        if d.state in ("queued", "sent") or _owes_notice(d):
            rows = _rows(cfg, run, snapshot_fn, status_fn)

            def save():
                dispatch.checkpoint(cfg.state_dir, items)
            if d.state == "queued":
                _deliver(cfg, run, d, rows.get(d.to_name), sleep, now, save)
                _expire(cfg, d, now)
            elif d.state == "sent":
                _check(cfg, d, rows.get(d.to_name), now, run)
            if report and d.state in dispatch.FINISHED:
                d.notify = False  # the tool result tells the caller
            if _owes_notice(d):
                notify_caller(cfg, run, d, sleep, rows=rows, save=save)
        result = replace(d)
    return result


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
        if row.agent_status != "idle":
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
    idle = row.agent_status == "idle"  # Claude's reply.done only means "some assistant text exists"
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
    idle = row.agent_status == "idle"
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


def notify_caller(cfg, run, d, sleep, *, rows=None, snapshot_fn=None, status_fn=None, save=None):
    """Send the completion (or failure) notice to the caller (with Enter) if it is a running sampan session that can
    take it. Changes d (notified; notified plus a reason when the notice may have arrived but Enter failed or
    `codex queue` timed out, never sent twice; notify=False when the caller is gone); call it inside
    dispatch.transaction. save: write the record now, before any key (notified is set first, so a crash in the middle
    never sends the notice again). True if sent."""
    if not _owes_notice(d):
        return False
    if d.from_kind != "session":
        d.notify = False  # the terminal or an unregistered agent: there is no session to tell
        return False
    rows = rows if rows is not None else _rows(cfg, run, snapshot_fn, status_fn)
    row = rows.get(d.from_name)
    if row is None or row.state in (State.RETIRED, State.UNREGISTERED, State.NO_AGENT):
        d.notify = False  # nobody left to tell: the reply stays in the record
        return False
    if row.state != State.RUNNING or (row.agent == "codex" and not row.sid):
        return False
    text = notice_text(d)
    if row.agent != "codex" and (row.agent_status != "idle" or row.pane is None):
        return False
    d.notified = True
    if save is not None:
        save()
    if row.agent == "codex":
        why = deliver.to_codex(cfg, run, row.sid, text)
    else:
        nid = _notice_id(d.id)
        why = deliver.to_claude(Tmux(run), row.pane, dispatch.write_outbox(cfg.state_dir, nid, text), nid, sleep)
    if isinstance(why, deliver.Unsure):  # the notice may be with the caller: count it as given, never send it again
        note = f"完成通知{REASON_PASTED}：{why}" if isinstance(why, deliver.Pasted) else f"完成通知{why}"
        d.reason = f"{d.reason}；{note}" if d.state == "failed" and d.reason else note
        _log(cfg, "send", f"派工 {d.id}：{note}")
        return False
    if why is not None:
        d.notified = False  # nothing was sent: try again later
        return False
    d.notified = True
    _log(cfg, "send", f"派工 {d.id}：已通知 {d.from_name}")
    return True


def set_notify(cfg, did, on):
    """Turn the completion notice of a dispatch from a sampan session on or off (an MCP tool that waited: off while
    it waits, on again when it gives up; off once the caller has read the reply). Never for one already notified."""
    try:
        with dispatch.transaction(cfg.state_dir) as items:
            d = next((x for x in items if x.id == did), None)
            if d is not None and not d.notified and d.from_kind == "session":
                d.notify = bool(on)
    except (dispatch.DispatchError, OSError):
        pass


def pending(cfg):
    """Ids a watcher should keep advancing: queued, sent, and finished ones whose notice is still owed."""
    return [d.id for d in dispatch.load(cfg.state_dir) if d.state in ("queued", "sent") or _owes_notice(d)]


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
