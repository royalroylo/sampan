"""The doorbell: one short line that tells an idle Claude it has mail, which it collects itself (MCP inbox_take), instead
of the full text pasted into its prompt. Actions layer: ring presses keys.

Everything is decided inside dispatch.transaction (the caller holds dispatches.lock): the ring is written as `ringing`
and committed before any key is pressed, so two watchers never ring twice. A ring nobody answers (the turn after it
ended and inbox_take never ran) is `no_answer`, and its messages are pasted in full the next time.
"""
import json
import os
from typing import Protocol

from . import deliver, deliveries, dispatch, heartbeat, store
from .claude_state import TurnInfo, after_marker, find_transcript
from .clock import parse_iso
from .screens import claude_input_state, claude_prompt_box_text
from .tmux import Tmux

RINGING, TAKEN, NO_ANSWER, PASTE_FAILED, SUPERSEDED = "ringing", "taken", "no_answer", "paste_failed", "superseded"
REASON_RUNG = "已按門鈴，等對方取信（{code}，{hhmm}）"
REASON_NO_MCP = "對方沒有可用的 sampan MCP，改用貼全文"
REASON_NO_ANSWER = "門鈴沒人應，改用貼全文"
REASON_PERMISSION = "對方停在權限提示（可能是 inbox_take 沒允許）"
RING_TEXT = ("〔sampan 門鈴 {code}〕你有 {n} 則新訊息（來自 {senders}）。請呼叫 mcp__sampan__inbox_take 一次取完、全部看完再動手；"
             "每則做完用 mcp__sampan__reply 回一次（那是最終回覆，不要用來報進度）。工具沒載入時先用 ToolSearch 找 sampan。")
READY = ("idle", "shell")  # as send.READY: the prompt takes input
SEEN_GRACE_S = 15  # right after a ring the transcript may not have it yet
RING_LIMIT_S = 10 * 60  # a ring still unanswered after this long is no_answer, so it never holds the target's line
PERMISSION_MARKS = ("mcp__sampan__inbox_take", "sampan - inbox_take")  # how a permission prompt for it names the tool


def marker(code):
    """What a ring's code looks like in the transcript."""
    return f"〔sampan 門鈴 {code}〕"


def ring_pending(screen, line):
    """The input box holds exactly this ring's line as typed text, however Claude wrapped it over rows (compared
    without whitespace: a word wrap may drop the space it breaks at). Never a paste mark (a user's own paste looks the
    same) and never the line with more typed after it: Enter would send the user's words."""
    if claude_input_state(screen) != "draft":
        return False
    return "".join((claude_prompt_box_text(screen) or "").split()) == "".join(line.split())


def ring_text(code, counts, n):
    """The one line rung: counts is [(sender name, how many)] in the order they first came."""
    return RING_TEXT.format(code=code, n=n, senders="、".join(f"{name} {k}" for name, k in counts))


class Doorbell(Protocol):
    def ring(self, target_row, text) -> str | None:
        """None when rung; else why not, a plain str when nothing reached the prompt, deliver.Unsure when it may have."""


class PasteDoorbell:
    """Rings a Claude by pasting the line into its empty prompt (deliver.to_claude, outbox ring-<code>). When the prompt
    still holds this same ring (Enter was lost), only Enter is pressed again: a ring holds nothing, so pressing it twice
    is safe, but it is never pasted twice into one box."""

    def __init__(self, cfg, run, sleep):
        self.cfg, self.run, self.sleep = cfg, run, sleep

    def ring(self, target_row, text):
        tmux = Tmux(self.run)
        pane = target_row.pane
        if ring_pending(tmux.capture(pane, escapes=True), text.split("\n", 1)[0]):
            if tmux.pane_in_mode(pane) is not False:
                return deliver.Pasted(deliver.REASON_COPY_MODE)
            r = tmux.send_keys(pane, "Enter")
            return None if r.returncode == 0 else deliver.Pasted(f"tmux 結束碼 {r.returncode}")
        name = f"ring-{_code_of(text)}"
        outbox = dispatch.write_outbox(self.cfg.state_dir, name, text)
        return deliver.to_claude(tmux, pane, outbox, name, self.sleep)


def _code_of(text):
    return text.split("〕", 1)[0].rsplit(" ", 1)[-1]


class Plan(str):
    """What plan_for decided ("ring", "paste", "wait" or "raw"); reason: why a Claude gets the full text pasted."""
    reason = ""


def _plan(kind, reason=""):
    p = Plan(kind)
    p.reason = reason
    return p


def latest_ring(conn, to_id):
    return conn.execute("SELECT * FROM rings WHERE to_id=? ORDER BY rowid DESC LIMIT 1", (to_id,)).fetchone()


def ringing(conn, to_id):
    """The ring of to_id still waiting to be answered, or None."""
    return conn.execute("SELECT * FROM rings WHERE to_id=? AND result=? ORDER BY rowid DESC LIMIT 1",
                        (to_id, RINGING)).fetchone()


def last_ring_of(conn, message):
    """The newest ring message was in (a cancelled one was taken out of its ring), or None."""
    return conn.execute("SELECT * FROM rings WHERE messages LIKE ? ORDER BY rowid DESC LIMIT 1",
                        (f'%"{message}"%',)).fetchone()


def drop_from_ring(conn, to_id, message):
    """message (cancelled while rung) is no longer in to_id's ringing ring, so a take never hands it over; a ring
    left with nothing in it is superseded."""
    ring = ringing(conn, to_id)
    if ring is None or message not in _ids(ring):
        return
    left = [i for i in _ids(ring) if i != message]
    conn.execute("UPDATE rings SET messages=?, result=? WHERE id=?",
                 (json.dumps(left), RINGING if left else SUPERSEDED, ring["id"]))


def _ids(ring):
    try:
        return json.loads(ring["messages"] or "[]")
    except ValueError:
        return []


def plan_for(conn, target_row, now, line, *, start_of, alive=os.kill):
    """How the head of the target's line (line: its queued messages up to the first raw, or a raw alone) goes now.
    In order: Codex → paste; an active delivery → wait; a raw first → raw; its last ring went unanswered and some of
    these were in it → paste; no valid heartbeat → paste; a ring still ringing → wait; else ring."""
    target = target_row.id or target_row.name
    if target_row.agent == "codex":
        return _plan("paste")
    if deliveries.active_delivery(conn, target) is not None:
        return _plan("wait")
    if line and line[0].raw:
        return _plan("raw")
    last = latest_ring(conn, target)
    if last is not None and last["result"] == NO_ANSWER and {x.id for x in line} & set(_ids(last)):
        return _plan("paste", REASON_NO_ANSWER)
    if not target_row.id or heartbeat.valid(conn, session_id=target_row.id, conversation=target_row.sid, now=now,
                                            alive=alive, start_of=start_of) is None:
        return _plan("paste", REASON_NO_MCP)
    if ringing(conn, target) is not None:
        return _plan("wait")
    return _plan("ring")


def _back_in_line(members, reason=""):
    for x in members:
        x.status, x.reason = "", reason  # "" is derived from state: queued


def _set_result(conn, code, result):
    conn.execute("UPDATE rings SET result=? WHERE id=?", (result, code))


def ring_for(items, cfg, run, target_row, group, now, sleep, *, save, log, bell=None):
    """Ring the target for group (its queued messages before the first raw, any sender). The ring is written
    `ringing` and the messages `rung`, and committed (save) before the key is pressed. Nothing reached the prompt:
    the ring is `paste_failed` and the messages go back in line with why (returned); else None."""
    conn = items.conn
    code = store.new_ring_id(conn)
    conn.execute("INSERT INTO rings (id, to_id, conversation, messages, at, result) VALUES (?, ?, ?, ?, ?, ?)",
                 (code, target_row.id or target_row.name, target_row.sid or None, json.dumps([x.id for x in group]),
                  now.isoformat(timespec="seconds"), RINGING))
    reason = REASON_RUNG.format(code=code, hhmm=now.astimezone().strftime("%H:%M"))
    for x in group:
        x.status, x.reason = "rung", reason
    save()
    counts = {}
    for x in group:
        counts[x.from_name] = counts.get(x.from_name, 0) + 1
    why = (bell or PasteDoorbell(cfg, run, sleep)).ring(target_row, ring_text(code, list(counts.items()), len(group)))
    ids = "、".join(x.id for x in group)
    if isinstance(why, deliver.Unsure):  # it may be in the box: still ringing (settle presses Enter again)
        log(f"派工 {ids}：按門鈴 {code} 給 {target_row.name}，可能沒送出（{why}）")
        return None
    if why:
        _set_result(conn, code, PASTE_FAILED)
        _back_in_line(group, why)
        log(f"派工 {ids}：門鈴 {code} 貼不進去（{why}），維持排隊")
        return why
    log(f"派工 {ids}：按門鈴 {code} 給 {target_row.name}（{len(group)} 則）")
    return None


def settle_ring(conn, cfg, run, ring, target_row, rung, now, *, sleep, log, bell=None):
    """Judge the target's ringing ring (rung: the target's messages whose status is rung). A new conversation
    supersedes it; a permission prompt for inbox_take is noted (no key is pressed); once the target is idle and the
    turn after the ring ended without a take (or the ring never showed up after SEEN_GRACE_S and is not in the box
    either), it is `no_answer` and the messages go back in line to be pasted. A ring left in the box gets Enter
    again. Rung messages no ringing ring holds go back in line."""
    members = [x for x in rung if ring is not None and x.id in _ids(ring)]
    _back_in_line([x for x in rung if x not in members])
    if ring is None:
        return
    if not members:
        _set_result(conn, ring["id"], SUPERSEDED)  # every message of it was cancelled
        return
    code = ring["id"]
    if (target_row.sid or None) != ring["conversation"]:
        _set_result(conn, code, SUPERSEDED)
        _back_in_line(members)
        log(f"門鈴 {code}：對方換了對話，重新排隊")
        return
    tmux = Tmux(run)
    if target_row.agent_status == "waiting":
        screen = tmux.capture(target_row.pane) if target_row.pane else ""
        if any(m in screen for m in PERMISSION_MARKS):
            for x in members:
                x.reason = REASON_PERMISSION
        return
    if target_row.agent_status not in READY:
        return
    if _too_old(ring, now):
        give_up(conn, ring, members, log)
        return
    path = find_transcript(cfg.profiles.values(), target_row.sid, prefer=target_row.profile)
    info = after_marker(path, marker(code)) if path else TurnInfo(False)
    if info.found and not info.ended:
        return
    if not info.found:
        at = parse_iso(ring["at"])
        if at is not None and (now - at).total_seconds() < SEEN_GRACE_S:
            return
        text = _ring_outbox(cfg, code)
        if text and target_row.pane and ring_pending(tmux.capture(target_row.pane, escapes=True), text):
            why = (bell or PasteDoorbell(cfg, run, sleep)).ring(target_row, text)
            log(f"門鈴 {code}：還在輸入框，再按一次 Enter" + (f"（{why}）" if why else ""))
            return
    give_up(conn, ring, members, log)


def _too_old(ring, now):
    at = parse_iso(ring["at"])
    return at is not None and (now - at).total_seconds() > RING_LIMIT_S


def give_up(conn, ring, members, log):
    """The ring is `no_answer`: its messages go back in line, to be pasted."""
    _set_result(conn, ring["id"], NO_ANSWER)
    _back_in_line(members, REASON_NO_ANSWER)
    log(f"門鈴 {ring['id']}：{REASON_NO_ANSWER}（派工 {'、'.join(x.id for x in members)}）")


def give_up_if_old(conn, to_id, rung, now, log):
    """For a target that is not there to answer (stopped, not running, another agent): its ringing ring, once older
    than RING_LIMIT_S, is no_answer, so its messages wait in line like any other."""
    ring = ringing(conn, to_id)
    if ring is None or not _too_old(ring, now):
        return
    members = [x for x in rung if x.id in _ids(ring)]
    give_up(conn, ring, members, log)


def _ring_outbox(cfg, code):
    try:
        return dispatch.outbox_path(cfg.state_dir, f"ring-{code}").read_text(encoding="utf-8").split("\n", 1)[0]
    except OSError:
        return ""
