"""`send --restore`: how a queued dispatch brings its stopped target back before it is delivered.

Pure judging only (no tmux, no database, no clock of its own); send.py does the claims and the work. A dispatch sent with
restore carries the key RESTORE in its `extra`; while its target is stopped, every such queued dispatch of that target
shares one phase (PHASE, started at PHASE_AT), so whichever watcher looks first makes the one claim and the others wait:

    (none) → launching → starting → rc → ready
             claim, then the launch line goes out (outside the dispatches lock)
                        sampan looks at the target on every later look: up and idle → rc (or ready without Remote
                        Control), at a trust prompt, died or too slow → the dispatches fail
                                    Remote Control name reset, outside the lock; then the message is delivered as usual

Nothing here presses a key; a claim that was never followed by its result (the process died) fails the dispatches and is
never launched again, because the launch line may already have gone out.
"""
from dataclasses import dataclass

from .status import State

RESTORE = "restore"  # extra key: the sender asked for the stopped target to be reattached first
PROFILE = "restore_profile"  # extra key: reattach under this profile (a permanent switch of the registry row), "" = as registered
LAUNCHES = "restore_launches"  # extra key: how many times the launch line was claimed for this dispatch
PHASE = "restore_phase"  # extra key: "", launching, starting, rc or ready
PHASE_AT = "restore_at"  # extra key: when the phase began (UTC ISO)
LAUNCHING, STARTING, RC, READY_PHASE = "launching", "starting", "rc", "ready"
ACTIVE = (LAUNCHING, STARTING, RC)  # phases that hold one of the RESTORE_AT_ONCE places

RESTORE_AT_ONCE = 2  # targets being reattached at the same time on one machine (a Claude uses a few hundred MB)
LAUNCH_STALE_S = 60  # a launch claim with no result after this long: the process that made it is gone
START_LIMIT_S = 120  # from the claim until the target is up and idle
DIED_GRACE_S = 15  # right after the launch line tmux may not show the session yet: not "died" before this
MAX_LAUNCHES = 2  # a dispatch's target is launched at most this often (up, then down again before the message left)
HOLD_MAX_S = START_LIMIT_S + 120  # a place held by a phase older than this is an orphan (nobody advances it): free it
RC_STALE_S = 120  # a Remote Control reset claim with no result after this long: carry on to the delivery

NEEDS_RESTORE = "--profile 要跟 --restore 一起用"
PROFILE_RUNNING = "{name} 在執行中，不換 profile；要換用 sampan switch {name} {profile}"
PROFILE_AGENT = "{name} 是 {agent}，只有 Claude session 能換 profile"
PROFILE_UNKNOWN = "未知的 profile：{profile}（設定檔裡有：{known}）"
PROFILE_DISABLED = "profile {profile} 已停用（sampan profile enable {profile}）"
PROFILE_CONFLICT = "{name} 已有一則排隊中的接回派工要用「{other}」，這則要用「{mine}」；等那則做完或取消再送"
REASON_NOT_CLAIMED = "對方已停止，等接回"  # restore asked, but this look may not start anything (a tool result's own look)
REASON_RESTORING = "對方已停止，接回中"
REASON_STARTING = "對方接回中，等它啟動"
REASON_QUEUE = "排隊等接回（同時最多 {n} 個）"
REASON_RC = "對方已接回，校正 Remote Control 名稱"
FAIL_TRUST = "接回後停在信任提示，sampan 不替你按；到 tmux attach -t {name} 確認後，用 resend 重送"
FAIL_TIMEOUT = "接回逾時：{n} 秒內沒有閒下來（可能停在提示或還在載入）；到 tmux attach -t {name} 看，再用 resend 重送"
FAIL_INTERRUPTED = "接回中斷（送出接回指令時 sampan 結束了），不重複開；sampan status 看過 {name} 後用 resend 重送"
FAIL_DIED = "接回後 {name} 的 agent 又結束了；sampan status 看過後用 resend 重送"
FAIL_STATE = "接回後 {name} 的狀態是「{state}」，不送；sampan status 看過後用 resend 重送"
FAIL_RETIRED = "對方 {name} 已是 retired，不接回；要的話先 sampan activate {name}，再用 resend 重送"
FAIL_AGAIN = "接回 {n} 次，{name} 還是起不來又停了；到 tmux attach -t {name} 看過，再用 resend 重送"
FAIL_PROFILE_LOST = "{name} 已在執行（{profile}），沒換成 {want}；要換用 sampan switch {name} {want}，再用 resend 重送"
FAIL_START = "接回失敗：{why}"
# queued reasons that clear by themselves or end in a failure of their own: the 30-minute queue limit does not apply
WAIT_PREFIXES = ("對方已停止，等接回", "對方已停止，接回中", "對方接回中", "排隊等接回", "對方已接回")


def waiting(reason):
    return reason.startswith(WAIT_PREFIXES)


@dataclass(frozen=True)
class Step:
    """What a look at a restoring target decides. kind: wait (reason), launch / rc (claim the phase, then do it),
    ready (the delivery goes on), fail (reason)."""
    kind: str
    reason: str = ""


def decide(row, phase, age, *, others_active, rc):
    """The next step for the dispatches of one stopped target (StatusRow). phase: the shared one ("" = none yet);
    age: seconds since it began; others_active: how many other targets hold a place; rc: whether the Remote Control
    reset follows the launch."""
    name = row.name
    if phase == READY_PHASE:
        return Step("ready")
    if phase == "":
        if others_active >= RESTORE_AT_ONCE:
            return Step("wait", REASON_QUEUE.format(n=RESTORE_AT_ONCE))
        return Step("launch")
    if phase == LAUNCHING:
        if age > LAUNCH_STALE_S:
            return Step("fail", FAIL_INTERRUPTED.format(name=name))
        return Step("wait", REASON_RESTORING)
    if phase == STARTING:
        if row.state == State.AWAIT_TRUST:
            return Step("fail", FAIL_TRUST.format(name=name))
        too_slow = age > START_LIMIT_S
        if row.state == State.RUNNING:
            if row.agent_status in ("idle", "shell"):
                return Step("rc") if rc else Step("ready")
            return Step("fail", FAIL_TIMEOUT.format(n=START_LIMIT_S, name=name)) if too_slow else Step("wait", REASON_STARTING)
        if row.state == State.STOPPED:
            if too_slow:
                return Step("fail", FAIL_TIMEOUT.format(n=START_LIMIT_S, name=name))
            if row.tmux_alive or age <= DIED_GRACE_S:
                return Step("wait", REASON_STARTING)  # claude is still coming up: the registry row has no agent yet
            return Step("fail", FAIL_DIED.format(name=name))
        return Step("fail", FAIL_STATE.format(name=name, state=row.display))
    if phase == RC:
        if age > RC_STALE_S or row.state != State.RUNNING:
            return Step("ready")
        return Step("wait", REASON_RC)
    return Step("fail", FAIL_STATE.format(name=name, state=phase))
