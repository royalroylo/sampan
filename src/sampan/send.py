"""Sending a message to a session: submit a dispatch, advance it (queued → sent → done / failed), notify the caller.

Actions layer. Every state change happens inside dispatch.transaction: the latest record is read, judged and written
back under one lock, so two watchers (an MCP server and the sampan screen) never deliver the same dispatch twice.
The target's conversation id is read from status_rows on every advance; Dispatch.thread_id is only a cache.
"""
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import actionlog, deliver, dispatch
from .claude_state import Reply, find_transcript
from .claude_state import reply_to as claude_reply_to
from .clock import fmt_local, parse_iso
from .codex_state import find_rollout
from .codex_state import reply_to as codex_reply_to
from .registry import RegistryError
from .status import State, status_rows
from .tmux import Tmux

POLL_S = 2
NOT_SEEN_GRACE_S = 15  # right after delivery the transcript may not have the message yet
TERMINAL_NAME = "終端機"
GONE = (State.STOPPED, State.RETIRED, State.UNREGISTERED, State.NO_AGENT)  # the session is not there to answer
REASON_STOPPED = "對方 session 已停止"
REASON_NOT_RUNNING = "對方 session 沒在執行"
REASON_NOT_SEEN = "還沒在對方的對話裡看到這則訊息"
REASON_NOT_FOUND = "找不到回覆：對方的對話裡沒有這則訊息（可能 /clear、換了對話）"
REASON_NO_THREAD = "對方還沒有對話編號"
REASON_AGENT_CHANGED = "對方的 agent 換了"
BUSY = {"busy": "對方正在忙", "working": "對方正在忙", "waiting": "對方在等使用者回應"}
STATE_LABELS = {"queued": "排隊中", "sent": "已送出、等回覆", "done": "已回覆", "failed": "失敗", "cancelled": "已取消"}
LOG_PREVIEW = 100


@dataclass
class Caller:
    """Who sends: a sampan session, or the terminal / an unregistered session (name None, never notified)."""
    name: str | None
    profile: str
    agent: str
    pane: str | None


TERMINAL = Caller(None, "", "", None)


def notice_text(d):
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


def _target_problem(row, to):
    if row is None or row.state in (State.UNREGISTERED, State.NO_AGENT):
        return f"找不到 session「{to}」：不在 sampan 登記表裡（sampan status 看名稱）"
    if row.state != State.RUNNING:
        return f"「{to}」不是執行中（{row.display}），不送"
    return None


def submit(cfg, run, caller, to, message, *, notify, now, snapshot_fn=None, status_fn=None):
    """Check the target and create a queued dispatch (outbox written, actions.log line). Dispatch, or why not (str)."""
    if not message.strip():
        return "訊息是空的"
    if caller.name is not None and caller.name == to:
        return f"不能送給自己（{to}）"
    try:
        row = _rows(cfg, run, snapshot_fn, status_fn).get(to)
    except RegistryError as e:
        return str(e)
    problem = _target_problem(row, to)
    if problem:
        return problem
    try:
        with dispatch.transaction(cfg.state_dir) as items:
            did = dispatch.new_id({d.id for d in items})
            text = deliver.compose(caller.name, caller.profile, did, message)
            dispatch.write_outbox(cfg.state_dir, did, text)
            d = dispatch.Dispatch(
                did, caller.name if caller.name is not None else TERMINAL_NAME, caller.profile, caller.agent,
                to, row.agent, row.profile, text, "queued", _iso(now),
                notify=bool(notify and caller.name is not None),
                thread_id=(row.sid or None) if row.agent == "codex" else None)
            kept = dispatch.prune(items, now)
            _drop_outboxes(cfg, {x.id for x in items} - {x.id for x in kept})
            items[:] = kept + [d]
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    who = d.from_name if caller.name is None else f"{d.from_name}（{d.from_profile}）"
    actionlog.append(cfg, "send", [f"派工 {did}：{who} → {to}（{row.agent}）：{_preview(message)}"])
    return replace(d)


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
    return d.state == "done" and d.notify and not d.notified


def advance(cfg, run, did, *, sleep, snapshot_fn=None, status_fn=None, now=None):
    """Move one dispatch forward as far as it can go now; returns a copy of it. Finished ones are left alone.
    Raises DispatchError for an unknown id or an unreadable dispatches.json, RegistryError for a bad registry."""
    now = now or _utcnow()
    with dispatch.transaction(cfg.state_dir) as items:
        d = next((x for x in items if x.id == did), None)
        if d is None:
            raise dispatch.DispatchError(f"沒有派工 {did}")
        if d.state in ("queued", "sent") or _owes_notice(d):
            rows = _rows(cfg, run, snapshot_fn, status_fn)
            if d.state == "queued":
                _deliver(cfg, run, d, rows.get(d.to_name), sleep, now)
            elif d.state == "sent":
                _check(cfg, d, rows.get(d.to_name), now)
            if _owes_notice(d):
                notify_caller(cfg, run, d, sleep, rows=rows)
        result = replace(d)
    return result


def _deliver(cfg, run, d, row, sleep, now):
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
        why = deliver.to_claude(Tmux(run), row.pane, outbox, d.id, sleep)
    if why:
        d.reason = why
        return
    d.state, d.sent_at, d.reason = "sent", _iso(now), ""
    actionlog.append(cfg, "send", [f"派工 {d.id}：已送到 {d.to_name}"])


def _finish(d, state, now, reason="", reply=None):
    d.state, d.done_at, d.reason, d.reply = state, _iso(now), reason, reply


def _reply(cfg, row, text):
    if row.agent == "codex":
        path = find_rollout(cfg.codex.home, row.sid)
        return codex_reply_to(path, text) if path else Reply(False, False, "")
    path = find_transcript(cfg.profiles.values(), row.sid, prefer=row.profile)
    return claude_reply_to(path, text) if path else Reply(False, False, "")


def _check(cfg, d, row, now):
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
    reply = _reply(cfg, row, d.text)
    idle = row.agent_status == "idle"  # Claude's reply.done only means "some assistant text exists"
    if reply.found:
        if reply.done and idle:
            _finish(d, "done", now, reply=reply.text)
        else:
            d.reason = ""
        return
    if not idle:
        return
    sent = parse_iso(d.sent_at)
    if sent is not None and (now - sent).total_seconds() < NOT_SEEN_GRACE_S:
        return
    if d.reason == REASON_NOT_SEEN:  # seen idle without the message twice in a row: it is not coming
        _finish(d, "failed", now, REASON_NOT_FOUND)
    else:
        d.reason = REASON_NOT_SEEN


def notify_caller(cfg, run, d, sleep, *, rows=None, snapshot_fn=None, status_fn=None):
    """Send the completion notice to the caller (with Enter) if it is a running sampan session that can take it.
    Changes d (notified, or notify=False when the caller is gone); call it inside dispatch.transaction. True if sent."""
    if not _owes_notice(d):
        return False
    rows = rows if rows is not None else _rows(cfg, run, snapshot_fn, status_fn)
    row = rows.get(d.from_name)
    if row is None or row.state in (State.RETIRED, State.UNREGISTERED, State.NO_AGENT):
        d.notify = False  # nobody left to tell: the reply stays in the record
        return False
    if row.state != State.RUNNING or (row.agent == "codex" and not row.sid):
        return False
    text = notice_text(d)
    if row.agent == "codex":
        why = deliver.to_codex(cfg, run, row.sid, text)
    else:
        if row.agent_status != "idle" or row.pane is None:
            return False
        nid = _notice_id(d.id)
        why = deliver.to_claude(Tmux(run), row.pane, dispatch.write_outbox(cfg.state_dir, nid, text), nid, sleep)
    if why is not None:
        return False
    d.notified = True
    actionlog.append(cfg, "send", [f"派工 {d.id}：已通知 {d.from_name}"])
    return True


def pending(cfg):
    """Ids a watcher should keep advancing: queued, sent, and done ones whose notice is still owed."""
    return [d.id for d in dispatch.load(cfg.state_dir) if d.state in ("queued", "sent") or _owes_notice(d)]


def cancel(cfg, did, *, now=None):
    """Cancel a queued dispatch. The cancelled Dispatch, or why not (str)."""
    try:
        with dispatch.transaction(cfg.state_dir) as items:
            d = next((x for x in items if x.id == did), None)
            if d is None:
                return f"沒有派工 {did}"
            if d.state != "queued":
                return f"派工 {did} 是「{STATE_LABELS[d.state]}」，只能取消還沒送出的（排隊中）"
            _finish(d, "cancelled", now or _utcnow(), "已取消")
            result = replace(d)
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    actionlog.append(cfg, "cancel", [f"派工 {did}：取消（給 {result.to_name}）"])
    return result


def cmd_cancel(cfg, out, did):
    r = cancel(cfg, did)
    if isinstance(r, str):
        out(f"❌ {r}")
        return 2
    out(f"已取消派工 {did}")
    return 0


def cmd_send(cfg, run, out, to, message, *, wait, timeout=600, sleep=time.sleep, clock=time.monotonic,
             snapshot_fn=None, status_fn=None, now_fn=_utcnow):
    """`sampan send`: the caller is the terminal. Without wait: try once, print the id (rc 0). With wait: advance every
    2 s; done → the reply (rc 0), failed → the reason (rc 1), timeout → rc 124 (the dispatch stays)."""
    r = submit(cfg, run, TERMINAL, to, message, notify=False, now=now_fn(), snapshot_fn=snapshot_fn, status_fn=status_fn)
    if isinstance(r, str):
        out(f"❌ {r}")
        return 2
    did, start = r.id, clock()
    try:
        while True:
            try:
                d = advance(cfg, run, did, sleep=sleep, snapshot_fn=snapshot_fn, status_fn=status_fn, now=now_fn())
            except (dispatch.DispatchError, RegistryError) as e:
                out(f"❌ {e}")
                return 2
            if not wait:
                out(f"派工 {did}：{STATE_LABELS[d.state]}" + (f"（{d.reason}）" if d.reason else ""))
                return 0
            if d.state == "done":
                out(d.reply or "")
                return 0
            if d.state in ("failed", "cancelled"):
                out(f"❌ 派工 {did}{STATE_LABELS[d.state]}：{d.reason}")
                return 1
            if clock() - start >= timeout:
                out(f"還在做，派工 {did}（sampan dispatches 看狀態）")
                return 124
            sleep(POLL_S)
    except KeyboardInterrupt:
        out(f"\n不等了，派工 {did} 照樣留著（sampan dispatches 看狀態）")
        return 130


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
    out(f"{'編號':<6} {'從':<24} {'給':<24} {'狀態':<10} {'建立':<11} 說明")
    for d in shown:
        note = d.reason or (_preview(d.reply) if d.state == "done" and d.reply else "")
        out(f"{d.id:<6} {d.from_name:<24} {d.to_name:<24} {STATE_LABELS[d.state]:<10} "
            f"{fmt_local(parse_iso(d.created)):<11} {note}")
    return 0
