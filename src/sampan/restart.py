"""Restart named sessions: stop the idle ones, then reattach exactly those in the background (never going in)."""
from dataclasses import dataclass

from . import actionlog
from .status import LABELS, State

BUSY = {"busy", "working", "waiting"}  # agent_status values that mean the agent is in the middle of something


@dataclass
class RestartPlan:
    restart: list  # running and idle: stop, then reattach
    resume: list  # stopped: reattach only
    skip: list  # (name, reason)


def _other_state(row):
    try:  # lazy: the ui package imports this module
        from .ui.state import list_state
        return list_state(row)
    except ImportError:
        return LABELS[row.state]


def plan_restart(rows, names, *, resume_stopped, own_pane=None):
    """Classify names (in the given order, each once). StatusRow.pane is the tmux pane id, the same kind of id
    cmd_stop compares with own_pane."""
    by_name = {r.name: r for r in rows}
    plan = RestartPlan([], [], [])
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        row = by_name.get(name)
        if row is None:
            plan.skip.append((name, "找不到"))
        elif row.state is State.RUNNING:
            if own_pane and row.pane == own_pane:
                plan.skip.append((name, "sampan 就在這個 pane"))
            elif row.agent_status in BUSY:
                plan.skip.append((name, "忙"))
            elif row.agent_status != "idle":  # cmd_stop only stops idle ones: unknown / "-" / menus are not
                plan.skip.append((name, f"沒有閒著（{row.agent_status}）"))
            else:
                plan.restart.append(name)
        elif row.state is State.STOPPED:
            if resume_stopped:
                plan.resume.append(name)
            else:
                plan.skip.append((name, "已停止"))
        elif row.state is State.RETIRED:
            plan.skip.append((name, "retired"))
        elif row.state is State.UNKNOWN:
            plan.skip.append((name, "狀態不明"))
        else:
            plan.skip.append((name, _other_state(row)))
    return plan


def skip_text(pairs):
    """The one-line "略過：名稱（原因）…" list; a pair without a reason shows the bare name."""
    return "略過：" + "、".join(f"{n}（{w}）" if w else n for n, w in pairs)


def _listed(items, limit):
    if len(items) <= limit:
        return "、".join(items)
    return "、".join(items[:limit]) + f"等 {len(items)} 個"


def confirm_line(plan, limit=8):
    """One-line question for the ui; "" when there is nothing to restart."""
    if not plan.restart and not plan.resume:
        return ""
    go = list(plan.restart) + [n + "（背景接回）" for n in plan.resume]
    line = "將重開：" + _listed(go, limit)
    if plan.skip:
        line += "　略過：" + _listed([f"{n}（{why}）" for n, why in plan.skip], limit)
    return line + "　確定？(y/N)"


def cmd_restart(cfg, run, out, ask, plan, *, own_pane=None, on_prompt=None, stop_fn=None, restore_fn=None):
    """Stop plan.restart, then reattach what stopped plus plan.resume with one restore (no going in). Returns the worst rc."""
    out = actionlog.tee(cfg, "重開", out)
    on_prompt = actionlog.logged_prompt(cfg, "重開", on_prompt)
    from . import restore, session_run  # lazy: session_run imports a lot
    stop_fn = stop_fn or session_run.cmd_stop
    restore_fn = restore_fn or restore.cmd_restore
    s, left, why = [], [], {}
    worst = 0
    if plan.restart:
        worst = stop_fn(cfg, run, out, names=tuple(plan.restart), stopped=s, left=left, why=why,
                        own_pane=own_pane, timeout=90)
    again = tuple(s) + tuple(n for n in plan.resume if n not in s)
    if again:
        worst = max(worst, restore_fn(cfg, run, out, ask, names=again, confirm=False, on_prompt=on_prompt))
    skipped = list(plan.skip) + [(n, why.get(n, "")) for n in left]
    if skipped:
        out(skip_text(skipped))
    return worst
