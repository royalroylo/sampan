"""The inbox a doorbelled Claude uses through MCP: inbox_take hands it every message queued for it at once (one `inbox`
delivery, named by its receipt), and reply records its answer to one of them. Neither presses a key.

Both run inside dispatch.transaction. When the turn after a take ends, and what happens to the messages then, is
judged by send._check (inbox deliveries), like every other delivery.
"""
from . import deliveries, dispatch, doorbell, send
from .claude_state import find_transcript
from .clock import fmt_local, parse_iso
from .deliver import body

REPLY_MAX = 100_000
OPEN = ("taken", "partial", "unanswered")  # taken and not answered for good (yet)
NOT_A_SESSION = "只有 sampan session 能取信"
NOT_YOURS = "這則不是寄給你的"
NOT_TAKEN = "這則還沒被取走"
WRONG_RECEIPT = "收據不符"
EMPTY = "回覆是空的"
TOO_LONG = f"回覆超過 {REPLY_MAX:,} 字"
LATE = "已記下；寄件者已取消（或已重送），不會通知對方"
HEADER = "收據 {receipt}；{n} 則，依時間順序；前後衝突時以後面的為準"
CLOSING = "每則做完用 mcp__sampan__reply 回一次（dispatch_id、text{receipt}），那是最終回覆，不要用來報進度。"


class InboxError(Exception):
    """A refusal, told to the agent as it is."""


def take(cfg, caller, *, rows, now):
    """Take every message queued for caller (a sampan session; rows: the status rows, for its conversation) up to
    the first direct send, in time order, as one inbox delivery: they become taken, the ring that rang for them taken.
    Never a second active delivery: while one is still active, nothing new is taken. The plain text for the agent:
    the receipt, each message, the ones taken earlier that still have no answer, and how to answer."""
    if caller.name is None or not caller.id:
        raise InboxError(NOT_A_SESSION)
    row = next((r for r in rows if r.id == caller.id), None)
    conversation = (row.sid or None) if row is not None else None
    cursor = _transcript_size(cfg, row)  # before the lock: the take's own result is written after it
    with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days) as items, dispatch._sqlite_errors(cfg.state_dir):
        conn = items.conn
        line = sorted((x for x in items if x.to_id == caller.id and x.state == "queued"), key=send._created_key)
        new = []
        for x in line:
            if x.raw:
                break
            new.append(x)
        active = deliveries.active_delivery(conn, caller.id)
        receipt = None
        if new and active is None:
            receipt = deliveries.open_delivery(conn, kind="inbox", to_id=caller.id, to_name=caller.name,
                                               messages=[x.id for x in new], text=None, conversation=conversation,
                                               cursor=cursor, now=now)
            for x in new:
                x.state, x.status, x.sent_at, x.reason = "sent", "taken", send._iso(now), send.REASON_TAKEN
            ring = doorbell.ringing(conn, caller.id)
            if ring is not None:
                conn.execute("UPDATE rings SET result=?, delivery=? WHERE id=?", (doorbell.TAKEN, receipt, ring["id"]))
        old = [x for x in items if x.to_id == caller.id and x.status in OPEN and not any(x is y for y in new)
               and _still_linked(conn, x)]
        text = _take_text(receipt, new, 0 if receipt else len(new), active, old)
        if receipt is not None:
            conn.execute("UPDATE deliveries SET text=? WHERE id=?", (text, receipt))
    if receipt is not None:
        send._log(cfg, "send", f"派工 {'、'.join(x.id for x in new)}：{caller.name} 用 inbox_take 取走"
                               f"（送達 {receipt}，{len(new)} 則）")
    return text


def _transcript_size(cfg, row):
    if row is None or not row.sid:
        return None
    path = find_transcript(cfg.profiles.values(), row.sid, prefer=row.profile)
    try:
        return path.stat().st_size if path else None
    except OSError:
        return None


def _still_linked(conn, x):
    """x still waits for the inbox delivery it was taken in (not cancelled or resent since)."""
    delivery = deliveries.delivery_of(conn, x.id)
    return delivery is not None and delivery["kind"] == "inbox" and not delivery["archived"]


def _take_text(receipt, new, held, active, old):
    if receipt is not None:
        n = len(new)
        lines = [HEADER.format(receipt=receipt, n=n)]
        for i, x in enumerate(new, 1):
            lines += ["", f"【{i}／{n}｜派工 {x.id}｜來自 {send._who(x)}｜{_when(x)}】\n{body(x.text)}"]
    elif held:
        lines = [f"沒有新訊息可以取：送達 {active['id']} 還沒結束，排隊中的 {held} 則等它結束後再送來。"]
    else:
        lines = ["沒有新訊息。"]
    if old:
        lines += ["", "已取走、還沒回：" + "、".join(f"{x.id}（來自 {send._who(x)}，{_when(x)}）" for x in old)]
    lines += ["", CLOSING.format(receipt=f"、receipt={receipt}" if receipt else "")]
    return "\n".join(lines)


def _when(x):
    return fmt_local(parse_iso(x.created))


def reply(cfg, caller, dispatch_id, text, receipt=None, *, now):
    """Record caller's answer to a message it took (a tool part of that delivery's reply). While the delivery is
    active the message is partial; after its turn ended it is replied again and its sender owed a notice. When the
    sender cancelled or resent it the part is kept as late history only. The text for the agent."""
    if not isinstance(text, str) or not text.strip():
        raise InboxError(EMPTY)
    if len(text) > REPLY_MAX:
        raise InboxError(TOO_LONG)
    if caller.name is None or not caller.id:
        raise InboxError(NOT_YOURS)
    with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days) as items, dispatch._sqlite_errors(cfg.state_dir):
        conn = items.conn
        d = next((x for x in items if x.id == dispatch_id), None)
        if d is None or d.to_id != caller.id:
            raise InboxError(NOT_YOURS)
        delivery = _taken_in(conn, d.id, receipt)
        if delivery is None:
            raise InboxError(WRONG_RECEIPT if receipt and _taken_in(conn, d.id, None) else NOT_TAKEN)
        late = bool(delivery["archived"] or delivery["link_archived"]) or d.state == "cancelled"
        deliveries.add_part(conn, delivery_id=delivery["id"], message=d.id, text=text, how="tool", now=now, late=late)
        if late:
            return LATE
        k = conn.execute("SELECT COUNT(*) FROM reply_parts WHERE message=? AND late=0 AND reply IN "
                         "(SELECT id FROM replies WHERE delivery=?)", (d.id, delivery["id"])).fetchone()[0]
        d.reply = text  # add_part wrote it; the record is written back whole
        if delivery["state"] == deliveries.ACTIVE:
            d.status, d.reason = "partial", send.REASON_PARTIAL
        else:  # its turn already ended (replied or unanswered): one more part, and one more notice
            if d.notify and not d.notified or not d.done_at:  # its sender was not told yet: one notice, of the reply
                d.done_at = send._iso(now)
            d.state, d.status, d.reason = "done", "replied", ""
            if d.from_kind == "session" and d.batch is None:
                d.notify, d.notified = True, False
    send._log(cfg, "send", f"派工 {d.id}：{caller.name} 回覆（第 {k} 段，送達 {delivery['id']}）")
    return f"已記下，派工 {d.id} 的回覆（第 {k} 段）"


def _taken_in(conn, message, receipt):
    """The latest inbox delivery message was taken in (receipt: that one only), archived links included; its row
    plus link_archived, or None."""
    sql = ("SELECT d.*, dm.archived AS link_archived FROM deliveries d JOIN delivery_messages dm ON dm.delivery=d.id "
           "WHERE dm.message=? AND d.kind='inbox'")
    args = (message,)
    if receipt:
        sql, args = sql + " AND d.id=?", (message, receipt)
    return conn.execute(sql + " ORDER BY d.rowid DESC LIMIT 1", args).fetchone()
