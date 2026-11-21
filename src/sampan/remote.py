"""Where the hub meets the rest of sampan: sending to another machine's session, taking what comes from one, and what the
hub reports back. connect.py knows nothing of dispatches; dispatch/send know nothing of the hub; this module is the join.

A remote dispatch is an ordinary row of dispatches.db (nothing about its schema changes). What makes it remote is
`extra["remote"]`:

    role "out": this machine sent it. {id (the envelope id), phase (outbox → at-hub → taken → sent → replied, or failed), machine,
        machine_name, session, expires, env (the signed envelope, so a lost outbox row can be rebuilt)}. send.advance leaves it
        alone; the hub's receipts and the reply move it, through the hooks below.
    role "in": this machine received it from another machine's session. {id, sender, session, sent_receipt, reply_sent}. It is
        a plain message to a local session from then on: the doorbell, inbox_take, reply and the transcript do the rest, and
        pump_inbound turns its progress and its reply into envelopes going back.

A caller of a remote dispatch is told like for any other (notice when it is finished) because that part is the caller's own
machine's business.
"""
from datetime import datetime, timezone
from pathlib import Path

from . import dispatch, machine
from . import hubproto as hp
from .connect import KEY_FILE, KEY_KID, Hooks, Rejected, load_keys
from .deliver import compose
from .remote_store import RemoteStore
from .status import State, status_rows

REMOTE = "remote"
OUT, IN = "out", "in"
PHASES = {  # what the sender is shown for each phase (dispatch.reason)
    "outbox": "排隊中，等 hub（還沒送到 hub）",
    "at-hub": "已送到 hub，對方機器還沒取走",
    "taken": "對方機器已收下，等它貼進 session",
    "sent": "已貼進對方的 session，等回覆",
}
EXPIRED_TEXT = "對方一直沒上線，已過期"
RESEND_REFUSAL = "遠端派工不能重送：請再送一則新的"


def _utcnow():
    return datetime.now(timezone.utc)


def hub_dir(cfg):
    return Path(cfg.path).parent / "hub"


def key_path(cfg):
    return hub_dir(cfg) / KEY_FILE


def credentials_path(cfg):
    from .hubhttp import CREDENTIALS_FILE
    return hub_dir(cfg) / CREDENTIALS_FILE


def open_store(cfg, clock=_utcnow):
    return RemoteStore.at(cfg.state_dir, clock=clock)


def info(d):
    """The remote part of a dispatch (a dict), or None for a local one."""
    r = d.extra.get(REMOTE) if isinstance(getattr(d, "extra", None), dict) else None
    return r if isinstance(r, dict) else None


def is_outgoing(d):
    r = info(d)
    return r is not None and r.get("role") == OUT


# ---------- sending ----------

def find_peer(store, text):
    """(peer dict, session name) for `machine/session` out of the hub's list of this owner's machines, or a problem string."""
    parsed = hp.parse_target(text)
    if parsed is None or parsed[0] != "device":
        return "遠端要寫成「機器/session」"
    _, machine_name, session = parsed
    peers = store.peers_get()
    if not peers:
        return "還沒有遠端機器的清單：sampan connect 沒在跑，或這台機器還沒被 hub 核准（sampan hub status）"
    peer = next((p for p in peers if p["name"] == machine_name), None)
    if peer is None:
        return f"沒有叫「{machine_name}」的機器（有：{'、'.join(p['name'] for p in peers)}）"
    for s in peer["sessions"]:
        if session in (s.get("session"), s.get("alias")):
            return peer, s["session"]
    shown = "、".join(s["session"] for s in peer["sessions"]) or "沒有"
    return f"{machine_name} 沒有發布「{session}」（發布的：{shown}；在那台機器 sampan hub publish）"


def submit_remote(cfg, caller, to, message, *, notify, now, store=None, raw=False, restore=False, profile=None):
    """Create a queued dispatch for a session on another machine and put its signed envelope in the outbox. The dispatch,
    or why not (a str). The caller has already been checked against the approval gate (the MCP tool asks the user)."""
    from . import send
    if raw:
        return "直接送（raw）不能送到別台機器"
    if restore or profile:
        return "遠端不支援 restore／profile：接回與換帳號是對方那台機器的決定"
    if not message.strip():
        return "訊息是空的"
    text = message.strip()
    if len(text) > hp.MAX_BODY:
        return f"訊息太長（{len(text)} 字，上限 {hp.MAX_BODY}）：請分成幾則送"
    own = machine.load_or_create(cfg.path)
    store = store or open_store(cfg)
    parsed = hp.parse_target(to)
    if parsed is not None and parsed[0] == "device" and parsed[1] == own.name:
        return f"「{own.name}」就是這台機器：本機的 session 直接寫名稱"
    found = find_peer(store, to)
    if isinstance(found, str):
        return found
    peer, session = found
    if own.id and peer["machine_id"] == own.id:
        return f"「{peer['name']}」就是這台機器：本機的 session 直接寫名稱"
    keys = load_keys(key_path(cfg))
    if KEY_KID not in keys:
        return f"沒有 owner key（{key_path(cfg)}）：sampan connect keygen，再把那一行手動放到你其他的機器"
    label = send.caller_label(caller)
    env = hp.sign(hp.new_envelope("message", peer["machine_id"], session, text, now=now, from_session=label),
                  keys, KEY_KID, own.id)
    remote = {"role": OUT, "id": env["id"], "phase": "outbox", "machine": peer["machine_id"], "machine_name": peer["name"],
              "session": session, "expires": env["expires"], "env": env}
    try:
        with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days) as items:
            did = dispatch.new_id(items.ids)
            body = compose(label, caller.profile, did, text)
            dispatch.write_outbox(cfg.state_dir, did, body)
            d = dispatch.Dispatch(
                did, label, caller.profile, caller.agent, f"{peer['name']}/{session}", "", "", body, "queued", _iso(now),
                notify=bool(notify and caller.name is not None), from_kind=send.caller_kind(caller),
                from_id=caller.id if caller.name is not None else "", machine=own.id, status="queued",
                reason=PHASES["outbox"], extra={REMOTE: remote})
            items.append(d)
        store.outbox_add(env, d.id)
    except (dispatch.DispatchError, OSError) as e:
        return str(e)
    send._log(cfg, "send", f"派工 {d.id}：{send._who(d)} → {d.to_name}（遠端）：{send._preview(text)}")
    return d


def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def advance_out(cfg, d, now, store=None):
    """What send.advance does for a dispatch this machine sent to another machine: only the things that need no one's
    word: the envelope that never got into the outbox (a crash between the two files) is put there again, and a dispatch
    nobody took by its time fails as expired. Everything else is moved by the hub's receipts."""
    r = info(d)
    if d.state in dispatch.FINISHED or r is None:
        return
    if r.get("phase") in ("outbox", "at-hub"):
        expires = hp.parse_time(r.get("expires"))
        if expires is not None and now >= expires:
            r["phase"], d.state, d.status, d.reason = "expired", "failed", "failed", EXPIRED_TEXT
            d.done_at = _iso(now)
            return
    if r.get("phase") == "outbox" and isinstance(r.get("env"), dict):
        try:
            (store or open_store(cfg)).outbox_add(r["env"], d.id)  # idempotent by envelope id
        except OSError:
            pass


# ---------- what the hub reports back (the hooks, outbound side) ----------

def _update(cfg, env_id, role, change):
    """Run change(d, remote_info) on the dispatch whose envelope id is env_id (under the lock); True when found."""
    found = False
    with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days, ids=()) as items:
        for d in items:
            r = info(d)
            if r is not None and r.get("role") == role and r.get("id") == env_id:
                change(d, r)
                found = True
                break
    return found


def _update_by_id(cfg, did, change):
    """Run change(d, remote_info) on dispatch `did`, finished or not (it is loaded because it is named)."""
    with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days, ids=(did,)) as items:
        d = next((x for x in items if x.id == did), None)
        r = info(d) if d is not None else None
        if r is not None:
            change(d, r)
            return True
    return False


def on_accepted(cfg):
    def hook(dispatch_id, env_id, resp):
        def change(d, r):
            if r.get("phase") == "outbox":
                r["phase"], d.reason = "at-hub", PHASES["at-hub"]
        _update(cfg, env_id, OUT, change)
    return hook


def on_failed(cfg, now_fn=_utcnow):
    def hook(dispatch_id, env_id, code, message):
        def change(d, r):
            if d.state not in dispatch.FINISHED:
                r["phase"], d.state, d.status = "failed", "failed", "failed"
                d.reason, d.done_at = (EXPIRED_TEXT if code == "expired" else f"hub 拒收：{message}"), _iso(now_fn())
        _update(cfg, env_id, OUT, change)
    return hook


def on_receipt(cfg, now_fn=_utcnow):
    def hook(env):
        phase, ref = env.get("phase"), env.get("ref")

        def change(d, r):
            if d.state in dispatch.FINISHED:
                return
            if phase == "taken" and r.get("phase") in ("outbox", "at-hub"):
                r["phase"], d.status, d.reason = "taken", "taken", PHASES["taken"]
            elif phase == "sent":
                r["phase"], d.state, d.status, d.reason = "sent", "sent", "pasted", PHASES["sent"]
                d.sent_at = d.sent_at or _iso(now_fn())
            elif phase == "rejected":
                r["phase"], d.state, d.status = "failed", "failed", "failed"
                d.reason, d.done_at = f"對方機器拒收：{env.get('reason') or '沒說原因'}", _iso(now_fn())
            elif phase == "expired":
                r["phase"], d.state, d.status = "expired", "failed", "failed"
                d.reason, d.done_at = EXPIRED_TEXT, _iso(now_fn())
        _update(cfg, ref, OUT, change)
    return hook


def on_reply(cfg, now_fn=_utcnow):
    def hook(env):
        def change(d, r):
            if d.state in ("done", "failed", "cancelled"):
                return
            if env.get("status") == "done":
                r["phase"], d.state, d.status, d.reply, d.reason = "replied", "done", "replied", env.get("body") or "", ""
            else:
                r["phase"], d.state, d.status = "failed", "failed", "failed"
                d.reason, d.reply = f"對方回報失敗：{env.get('reason') or '沒說原因'}", env.get("body") or None
            d.done_at = _iso(now_fn())
        _update(cfg, env.get("in_reply_to"), OUT, change)
    return hook


def cancel(cfg, d, store=None):
    """The part of cancelling a remote dispatch that is not in dispatches.db: take its envelope back out of the outbox when
    it was never sent, or (at the hub, not yet handed out) have `sampan connect` ask the hub to cancel at its next pass.
    Once the other machine has it there is nothing to take back: the caller only stops waiting."""
    r = info(d)
    if r is None or r.get("phase") not in ("outbox", "at-hub"):
        return
    store = store or open_store(cfg)
    if not (r.get("phase") == "outbox" and store.outbox_withdraw(r["id"])):
        store.put(CANCELS, store.get(CANCELS, []) + [r["id"]])


CANCELS = "cancel_wanted"  # remote.db state: envelope ids `sampan connect` is to ask the hub to take back


def process_cancels(store, transport):
    """Ask the hub to take back the envelopes of cancelled dispatches. Done or hopeless ones are dropped from the list."""
    from .connect import TransportError
    left = []
    for env_id in store.get(CANCELS, []):
        try:
            transport.cancel(env_id)
        except TransportError:
            left.append(env_id)  # the hub is not there: try again next pass
        except hp.ProtoError:
            pass  # not found, or no longer ours to cancel
    store.put(CANCELS, left)


# ---------- taking what comes (the hooks, inbound side) ----------

def _published_names(store):
    names = set()
    for p in store.published():
        names.add(p["session"])
        if p["alias"]:
            names.add(p["alias"])
    return names


OPEN = "inbound_open"  # remote.db state: the dispatches received from other machines that still owe a receipt or a reply


def _track(store, did):
    open_ = store.get(OPEN, [])
    if did not in open_:
        store.put(OPEN, open_ + [did])


def _untrack(store, did):
    store.put(OPEN, [x for x in store.get(OPEN, []) if x != did])


def make_deliver_local(cfg, run, store, now_fn=_utcnow, status_fn=status_rows):
    """The hook that turns an envelope from another machine into a local message for the session it names. Idempotent by
    envelope id: an envelope that comes again finds its message and does not make another."""
    def deliver_local(env):
        from . import send
        row = None
        session = env["to"]["session"]
        if session not in _published_names(store):
            raise Rejected("這個 session 沒有發布")
        rows = status_fn(cfg, run)
        row = next((r for r in rows if r.name == session), None) or next((r for r in rows if r.alias == session), None)
        if row is None or row.state != State.RUNNING:
            raise Rejected(f"{session} 沒在執行")
        stamp = env.get("stamp") or {}
        sender = f"{stamp.get('sender_machine_name') or '?'}/{env.get('from_session') or '?'}"
        now = now_fn()
        with dispatch.transaction(cfg.state_dir, keep_days=cfg.keep_days) as items:
            for d in items:
                r = info(d)
                if r is not None and r.get("role") == IN and r.get("id") == env["id"]:
                    return d.id
            did = dispatch.new_id(items.ids)
            body = compose(sender, "", did, env.get("body") or "")
            dispatch.write_outbox(cfg.state_dir, did, body)
            d = dispatch.Dispatch(
                did, sender, "", "", row.name, row.agent, row.profile, body, "queued", _iso(now), notify=False,
                from_kind="unregistered", to_id=row.id, machine=stamp.get("sender_machine") or "", status="queued",
                thread_id=(row.sid or None) if row.agent == "codex" else None, in_reply_to=env.get("in_reply_to"),
                extra={REMOTE: {"role": IN, "id": env["id"], "sender": sender, "session": row.name,
                                "sender_machine": stamp.get("sender_machine"), "from_session": env.get("from_session") or "",
                                "from_label": sender}})
            items.append(d)
        _track(store, d.id)
        send._log(cfg, "hub", f"收到遠端訊息 {did}：{sender} → {row.name}：{send._preview(env.get('body') or '')}")
        return d.id
    return deliver_local


def published_status(cfg, run, store, status_fn=status_rows):
    """[{session, alias, status}] of what this machine publishes: idle, busy, or offline (not running)."""
    out = []
    published = store.published()
    if not published:
        return out
    rows = status_fn(cfg, run)
    for p in published:
        row = next((r for r in rows if r.name == p["session"]), None)
        if row is None or row.state != State.RUNNING:
            status = "offline"
        else:
            status = "busy" if row.agent_status in ("busy", "working", "waiting") else "idle"
        out.append({"session": p["session"], "alias": p["alias"], "status": status})
    return out


def pump_inbound(cfg, run, store, conn, *, sleep, advance=None, now_fn=_utcnow, status_fn=None):
    """Move the messages received from other machines along, and send back what has happened to them: advance each one (the
    ordinary delivery to the local session), then queue a `sent` receipt once it is in the session and the reply once it
    has one (signed, in the outbox). Returns how many envelopes it queued."""
    from . import send
    advance = advance or send.advance
    queued = 0
    open_ = store.get(OPEN, [])
    if not open_:
        return 0
    loaded = {d.id: d for d in dispatch.load(cfg.state_dir, ids=tuple(open_))}
    for did in open_:
        d = loaded.get(did)
        r = info(d) if d is not None else None
        if r is None or r.get("role") != IN:
            _untrack(store, did)  # purged, or not ours any more
            continue
        if d.state not in dispatch.FINISHED:
            try:
                d = advance(cfg, run, d.id, sleep=sleep, **({"status_fn": status_fn} if status_fn else {}))
            except (dispatch.DispatchError, OSError):
                continue
            r = info(d) or r
        want_sent = d.state in ("sent", "done", "failed") and not r.get("sent_receipt")
        want_reply = d.state in dispatch.FINISHED and not r.get("reply_sent")
        done = []
        to_machine, from_session = r.get("sender_machine"), r.get("from_session") or ""
        if not to_machine:
            _untrack(store, did)
            continue
        now = now_fn()
        try:
            if want_sent:
                rec = conn.sign(hp.new_envelope("receipt", to_machine, from_session or "-", "", now=now, phase="sent",
                                                ref=r["id"], from_session=r.get("session") or ""))
                store.outbox_add(rec, "")
                done.append("sent_receipt")
            if want_reply:
                status = "done" if d.state == "done" else "failed"
                reply = conn.sign(hp.new_envelope(
                    "reply", to_machine, from_session or "-", (d.reply or "")[:hp.MAX_BODY], now=now, in_reply_to=r["id"],
                    status=status, reason="" if status == "done" else (d.reason or "")[:hp.MAX_FIELD],
                    from_session=r.get("session") or ""))
                store.outbox_add(reply, "")
                done.append("reply_sent")
        except Rejected:
            continue  # no owner key: nothing can be signed
        if done:
            def change(item, info_, done=done):
                for key in done:
                    info_[key] = True
            _update_by_id(cfg, did, change)
            queued += len(done)
        if d.state in dispatch.FINISHED and (r.get("reply_sent") or "reply_sent" in done):
            _untrack(store, did)
    return queued


def make_hooks(cfg, run, store, log=lambda line: None, now_fn=_utcnow, status_fn=status_rows):
    return Hooks(
        deliver_local=make_deliver_local(cfg, run, store, now_fn, status_fn), apply_receipt=on_receipt(cfg, now_fn),
        apply_reply=on_reply(cfg, now_fn), accepted=on_accepted(cfg), failed=on_failed(cfg, now_fn),
        published_status=lambda: published_status(cfg, run, store, status_fn), log=log)


def summary(d):
    """The remote part of a dispatch for dispatch_result: {role, machine, session, phase, expires} or None."""
    r = info(d)
    if r is None:
        return None
    if r.get("role") == OUT:
        return {"role": OUT, "machine": r.get("machine_name"), "session": r.get("session"), "phase": r.get("phase"),
                "expires": r.get("expires")}
    return {"role": IN, "from": r.get("from_label"), "session": r.get("session")}

