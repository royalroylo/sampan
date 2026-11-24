"""sampan rc: correct the Remote Control name of running Claude sessions, in place (no restart, no new conversation).

Only sessions that are running, idle, Claude, on a profile with Remote Control on get the rename (steps.rc_rename); every
other session is skipped with its reason. One session at a time; the state of each is looked at again just before its
turn, because the plan may be minutes old."""
import time
from dataclasses import dataclass

from . import actionlog, steps
from .registry import display_name
from .restart import BUSY, NOT_FOUND, _other_state
from .status import State, status_rows
from .tmux import Tmux

PAUSE = steps.POLL  # between two sessions, so one redraw settles before the next starts
CLUTTER = "Remote Control 已連上的 session 只改名（不斷開，手機列表不多一筆）；連不上的才斷開重連，那時列表會多一筆新的（舊的離線）。"


@dataclass
class RcPlan:
    todo: list  # StatusRow of the sessions to correct, in order
    skip: list  # (name, reason)


def skip_reason(cfg, row, own_pane=None):
    """Why this row is not corrected (None = it is)."""
    if row.state is State.RUNNING:
        if own_pane and row.pane == own_pane:
            return "sampan 就在這個 pane"
        if row.agent != "claude":
            return "Codex 沒有 Remote Control" if row.agent == "codex" else "不是 Claude"
        if not steps.rc_wanted(cfg, row):
            return f"profile {row.profile} 沒開 Remote Control（remoteControlAtStartup）"
        if row.agent_status in BUSY:
            return "忙"
        if row.agent_status != "idle":
            return f"沒有閒著（{row.agent_status}）"
        return None
    if row.state is State.STOPPED:
        return "已停止；下次接回會自動套用"
    if row.state is State.RETIRED:
        return "retired（仍在執行）" if row.live == "running" else "retired"
    if row.state is State.UNKNOWN:
        return "狀態不明"
    return _other_state(row)


def plan_rc(cfg, rows, names, *, everyone=False, profile=None, own_pane=None):
    """Classify the sessions to correct: `names`, plus every session (everyone) or every one registered on `profile`
    (both: the ones on that profile). A name that matches nothing is skipped as NOT_FOUND."""
    by_name = {r.name: r for r in rows}
    wanted = list(names)
    if everyone or profile:
        wanted += [r.name for r in rows if profile is None or r.profile == profile]
    plan, seen = RcPlan([], []), set()
    for name in wanted:
        if name in seen:
            continue
        seen.add(name)
        row = by_name.get(name)
        if row is None:
            plan.skip.append((name, NOT_FOUND))
            continue
        why = skip_reason(cfg, row, own_pane)
        if why is None:
            plan.todo.append(row)
        else:
            plan.skip.append((name, why))
    return plan


def skip_lines(pairs):
    """The skipped sessions, one line per reason: 略過（忙）：a、b."""
    by_reason = {}
    for name, why in pairs:
        by_reason.setdefault(why, []).append(name)
    return [f"  略過（{why}）：" + "、".join(names) for why, names in by_reason.items()]


def plan_lines(plan, *, limit=8):
    """What would be done: the count and the names (limit None = all of them), then the skipped ones."""
    names = [display_name(r) if display_name(r) == r.name else f"{r.name}（{display_name(r)}）" for r in plan.todo]
    shown = names if limit is None or len(names) <= limit else names[:limit] + [f"等 {len(names)} 個"]
    lines = [f"將校正 {len(plan.todo)} 個：" + "、".join(shown)]
    return lines + skip_lines(plan.skip)


def cmd_rc(cfg, run, out, plan, *, own_pane=None, sleep=time.sleep, rows_fn=None, tmux=None):
    """Correct plan.todo one by one. 0 when nothing failed (skips are not failures), 1 when any did."""
    out = actionlog.tee(cfg, "RC 校正", out)
    tmux = tmux or Tmux(run)
    rows_fn = rows_fn or (lambda: status_rows(cfg, run))
    done, failed, later = [], [], []
    skipped = list(plan.skip)
    interrupted = False
    try:
        for i, planned in enumerate(plan.todo):
            if i:
                sleep(PAUSE)
            now = {r.name: r for r in rows_fn()}.get(planned.name)
            if now is None or skip_reason(cfg, now, own_pane) is not None:  # busy now, stopped meanwhile, …
                skipped.append((planned.name, (skip_reason(cfg, now, own_pane) if now else NOT_FOUND) or "忙"))
                continue
            alias = display_name(now)
            out(f"  … {now.name}：RC 校正中（最多約 {steps.RC_WAIT} 秒）")

            def report(why, screen, name=now.name):
                if screen is not None:  # a local file only: the screen may hold part of the conversation
                    actionlog.append(cfg, "RC 校正", [f"{name}：{why}；最後的畫面："]
                                     + ["  | " + ln for ln in steps.snapshot(screen)])

            try:
                reason = steps.rc_rename(tmux, now.pane, alias, sleep=sleep, report=report,
                                         peek=steps.peek_for(cfg, tmux, now.pane))
            except Exception as e:  # a tmux failure mid-way is a reason like any other
                reason = str(e) or type(e).__name__
            if reason is None:
                done.append(now.name)
                out(f"  · {now.name} Remote Control 名稱已校正為「{alias}」")
            else:
                failed.append(now.name)
                out(f"  ⚠ {now.name}：RC 校正沒做：{reason}；之後可再跑 sampan rc {now.name}")
    except KeyboardInterrupt:
        interrupted = True
        later = [r.name for r in plan.todo if r.name not in done and r.name not in failed
                 and r.name not in {n for n, _ in skipped}]
    for line in skip_lines(skipped):
        out(line)
    if interrupted:
        out("中斷：" + ("還沒處理 " + "、".join(later) if later else "已全部處理"))
    out(f"校正 {len(done)}、略過 {len(skipped)}、失敗 {len(failed)}")
    return 1 if failed or interrupted else 0
