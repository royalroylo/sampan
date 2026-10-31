from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from .claude_state import find_transcript, transcript_stats
from .clock import fmt_local
from .codex_state import find_rollout, rollout_context, rollout_last_time
from .observe import live_by_pane, multi_agent_sessions
from .procs import list_procs
from .registry import RegistryError, load_rows
from .screens import at_trust_prompt
from .textwidth import ljust
from .tmux import Tmux, unreadable_message

CONTEXT_LIMIT = 250_000
CONTEXT_RATIO = 0.8  # Codex: ⚠ at this share of the model's own context window


class State(StrEnum):
    """What a session is doing right now (the DISPLAY status computed here; not the registry row's active/retired).
    The values are stable identifiers; the zh-TW text lives only in LABELS."""
    RUNNING = "running"
    STOPPED = "stopped"  # registered and should be running, but is not
    RETIRED = "retired"
    AWAIT_TRUST = "await_trust"  # stopped at a folder-trust prompt
    UNREGISTERED = "unregistered"  # an agent runs, but the registry has no row
    NO_AGENT = "no_agent"  # a tmux session with neither an agent nor a row
    AGENT_MISMATCH = "agent_mismatch"
    PROFILE_MISMATCH = "profile_mismatch"
    MULTI = "multi"  # several agents in one tmux session: not managed
    UNKNOWN = "unknown"  # tmux could not be read: do not claim "stopped"


LABELS = {
    State.RUNNING: "執行中",
    State.STOPPED: "已停止",
    State.RETIRED: "retired",
    State.AWAIT_TRUST: "等待信任確認",
    State.UNREGISTERED: "未登記",
    State.NO_AGENT: "沒有 agent（未登記）",
    State.AGENT_MISMATCH: "agent 不符",
    State.PROFILE_MISMATCH: "profile 不符",
    State.MULTI: "多個 agent（不管理）",
    State.UNKNOWN: "無法判斷",
}


@dataclass
class StatusRow:
    name: str
    agent: str  # "claude" | "codex" | "-"
    profile: str
    sid: str
    state: State
    detail: str = ""  # shown right after the label, e.g. "（登記 codex）" for a mismatch
    agent_status: str = "-"  # what the agent itself reports: idle / busy / working / waiting / "-"
    last: datetime | None = None
    tokens: int | None = None
    context_window: int | None = None  # Codex only: the model's window (Claude: None → CONTEXT_LIMIT)
    pane: str | None = None
    tmux_alive: bool = False  # the tmux session exists (a STOPPED row may still have its tmux, e.g. while starting)
    id: str = ""  # the registry row's sampan id ("" for a live session that is not registered)
    alias: str = ""  # the registry row's alias ("" = unset)

    @property
    def display(self):
        """The state as the screens and `sampan status` show it."""
        return LABELS[self.state] + self.detail


class Rows(list):
    """Result of status_rows (a list of StatusRow); warning not None = tmux could not be read (a one-line explanation for status/ui to show)."""
    warning = None


def _at_trust_prompt(tmux, panes, session, agent):
    """Claude Code does not remember trust for the home directory: a session opened from ~ stops at this screen every time and has no session file yet."""
    return any(at_trust_prompt(tmux.capture(p.pane_id), agent) for p in panes if p.session == session)


def status_rows(cfg, run, snapshot_fn=None):
    rows = {r.tmux: r for r in load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)}
    tmux = Tmux(run)
    panes = tmux.panes()
    if panes is None:
        return _unreadable_rows(cfg, rows, unreadable_message(tmux.unreadable_reason))
    procs = list_procs(run)
    found, _w = live_by_pane(cfg, run, panes, procs, snapshot_fn=snapshot_fn)
    multi = multi_agent_sessions(procs, panes, found)
    live_by_session = {}
    for p in panes:
        if p.pane_id in found:
            live_by_session.setdefault(p.session, found[p.pane_id])
    result = Rows()
    alive = {p.session for p in panes}
    for name in sorted(set(rows) | alive):
        row, lc = rows.get(name), live_by_session.get(name)
        detail = ""
        if name in multi:
            state = State.MULTI
        elif row is not None and row.state == "retired":
            state = State.RETIRED
        elif row is not None and lc is not None and lc.agent != row.agent:
            state, detail = State.AGENT_MISMATCH, f"（登記 {row.agent}）"
        elif row is not None and lc is not None and lc.profile != row.profile:
            state, detail = State.PROFILE_MISMATCH, f"（登記 {row.profile}）"
        elif row is not None and lc is not None:
            state = State.RUNNING
        elif row is not None:
            state = State.AWAIT_TRUST if _at_trust_prompt(tmux, panes, name, row.agent) else State.STOPPED
        elif lc is not None:
            state = State.UNREGISTERED
        else:
            state = State.NO_AGENT
        src = lc or row
        # before Codex has its first prompt, lc has no thread id: keep the registry row's
        sid = (lc.session_id if lc is not None else None) or (row.session_id if row is not None else "")
        agent = src.agent if src is not None else "-"
        last, tokens, window = _activity(cfg, agent, src.profile if src is not None else None, sid)
        result.append(StatusRow(name, agent, (src.profile or "-") if src else "-", sid, state, detail,
                                lc.status if lc else "-", last, tokens, window, lc.pane_id if lc else None,
                                name in alive, row.id if row is not None else "", row.alias if row is not None else ""))
    return result


def _unreadable_rows(cfg, rows, warning):
    """tmux unreadable: list only the registry rows, state always UNKNOWN (cannot tell) (retired stays as is); do not guess which are running."""
    result = Rows()
    result.warning = warning
    for name in sorted(rows):
        row = rows[name]
        last, tokens, window = _activity(cfg, row.agent, row.profile, row.session_id)
        result.append(StatusRow(name, row.agent, row.profile or "-", row.session_id,
                                State.RETIRED if row.state == "retired" else State.UNKNOWN, "", "-", last, tokens, window, None,
                                False, row.id, row.alias))
    return result


def _parse_ts(ts):
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _activity(cfg, agent, profile, sid):
    """(last activity datetime | None, context tokens | None, context window | None) of a conversation; the window is Codex only."""
    if not sid:
        return None, None, None
    if agent == "codex":
        path = find_rollout(cfg.codex.home, sid)
        if path is None:
            return None, None, None
        return (rollout_last_time(path), *rollout_context(path))
    t = find_transcript(cfg.profiles.values(), sid, prefer=profile)
    if t is None:
        return None, None, None
    last, tokens = transcript_stats(t)
    return _parse_ts(last), tokens, None


def _fmt_time(dt):
    return fmt_local(dt)


def _fmt_tokens(n, window=None):
    if n is None:
        return "-"
    limit = CONTEXT_RATIO * window if window else CONTEXT_LIMIT
    return f"{round(n / 1000)}k" + (" ⚠" if n >= limit else "")


def cmd_status(cfg, run, out):
    try:
        rows = status_rows(cfg, run)
    except RegistryError as e:
        out(f"❌ {e}")
        return 2
    warning = rows.warning
    if warning:
        out(f"⚠ {warning}")
    widths = (38, 6, 12, 8, 8, 16, 7, 11)

    def line(*cells):
        return " ".join(ljust(c, w) for c, w in zip(cells, widths)) + " " + cells[-1]

    out(line("名稱", "agent", "profile", "id", "對話", "狀態", "活動", "最後活動", "context"))
    for r in rows:
        out(line(r.name, r.agent, r.profile, r.id[:8] or "-", r.sid[:8], r.display, r.agent_status,
                 _fmt_time(r.last), _fmt_tokens(r.tokens, r.context_window)))
    return 1 if warning else 0
