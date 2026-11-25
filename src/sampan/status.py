import time
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
    model: str = ""  # the registry row's model ("" = unset: the profile's own default)
    registry: str = ""  # the registry row's state: "active" | "retired" | "" (no row)
    live: str = "stopped"  # whether an agent runs in it now: "running" | "stopped" | "unknown" (tmux could not be read)
    anomaly: str = ""  # a retired row only: what is wrong besides being retired (mismatch, trust prompt), ready to show

    @property
    def display(self):
        """The state as the screens and `sampan status` show it."""
        return LABELS[self.state] + self.detail

    @property
    def run_display(self):
        """The state as `sampan status` shows it next to the registry column: only what is going on, never "retired"
        (a mismatch or a trust prompt on a retired row shows here; `state` itself stays retired)."""
        if self.state is State.RETIRED:
            if self.anomaly:
                return self.anomaly
            return {"running": LABELS[State.RUNNING], "unknown": LABELS[State.UNKNOWN]}.get(self.live, LABELS[State.STOPPED])
        return self.display


class Rows(list):
    """Result of status_rows (a list of StatusRow); warning not None = tmux could not be read (a one-line explanation for status/ui to show)."""
    warning = None


def _at_trust_prompt(tmux, panes, session, agent):
    """Claude Code does not remember trust for the home directory: a session opened from ~ stops at this screen every time and has no session file yet."""
    return any(at_trust_prompt(tmux.capture(p.pane_id), agent) for p in panes if p.session == session)


def status_rows(cfg, run, snapshot_fn=None, *, activity=True):
    """One StatusRow per registry row and live tmux session. activity False leaves last / tokens / context_window
    None and reads no transcript (status_rows_light: the dispatch paths, which never read them)."""
    rows = {r.tmux: r for r in load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)}
    tmux = Tmux(run)
    panes = tmux.panes()
    if panes is None:
        return _unreadable_rows(cfg, rows, unreadable_message(tmux.unreadable_reason), activity=activity)
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
        trust = row is not None and lc is None and name not in multi and _at_trust_prompt(tmux, panes, name, row.agent)
        anomaly = ""
        if name in multi:
            state = State.MULTI
        elif row is not None and row.state == "retired":
            # a retired row is always RETIRED (what MCP list_sessions and the dispatch code read); what else is wrong
            # with it is only shown: status and the screen use `anomaly`, doctor reads it
            state = State.RETIRED
            if lc is not None and lc.agent != row.agent:
                anomaly = LABELS[State.AGENT_MISMATCH] + f"（登記 {row.agent}）"
            elif lc is not None and lc.profile != row.profile:
                anomaly = LABELS[State.PROFILE_MISMATCH] + f"（登記 {row.profile}）"
            elif trust:
                anomaly = LABELS[State.AWAIT_TRUST]
        elif row is not None and lc is not None and lc.agent != row.agent:
            state, detail = State.AGENT_MISMATCH, f"（登記 {row.agent}）"
        elif row is not None and lc is not None and lc.profile != row.profile:
            state, detail = State.PROFILE_MISMATCH, f"（登記 {row.profile}）"
        elif trust:
            state = State.AWAIT_TRUST
        elif row is not None and lc is not None:
            state = State.RUNNING
        elif row is not None:
            state = State.STOPPED
        elif lc is not None:
            state = State.UNREGISTERED
        else:
            state = State.NO_AGENT
        src = lc or row
        # before Codex has its first prompt, lc has no thread id: keep the registry row's
        sid = (lc.session_id if lc is not None else None) or (row.session_id if row is not None else "")
        agent = src.agent if src is not None else "-"
        last, tokens, window = (_activity(cfg, agent, src.profile if src is not None else None, sid) if activity
                                else (None, None, None))
        result.append(StatusRow(name, agent, (src.profile or "-") if src else "-", sid, state, detail,
                                lc.status if lc else "-", last, tokens, window, lc.pane_id if lc else None,
                                name in alive, row.id if row is not None else "", row.alias if row is not None else "",
                                row.model if row is not None else "", row.state if row is not None else "",
                                "running" if (lc is not None or name in multi) else "stopped", anomaly))
    return result


def status_rows_light(cfg, run, snapshot_fn=None):
    """status_rows without the conversation activity (last / tokens / context_window are None): what sending,
    watching, settling and heartbeats use. Same signature as status_rows, so it goes wherever a status_fn does."""
    return status_rows(cfg, run, snapshot_fn=snapshot_fn, activity=False)


def _unreadable_rows(cfg, rows, warning, *, activity=True):
    """tmux unreadable: list only the registry rows, state always UNKNOWN (cannot tell) (retired stays as is); do not guess which are running."""
    result = Rows()
    result.warning = warning
    for name in sorted(rows):
        row = rows[name]
        last, tokens, window = (_activity(cfg, row.agent, row.profile, row.session_id) if activity
                                else (None, None, None))
        result.append(StatusRow(name, row.agent, row.profile or "-", row.session_id,
                                State.RETIRED if row.state == "retired" else State.UNKNOWN, "", "-", last, tokens, window, None,
                                False, row.id, row.alias, row.model, row.state, "unknown"))
    return result


def _parse_ts(ts):
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_DISPLAY_PATHS = {}  # display-path cache: key → (transcript path found, clock time it was found); never a miss
_DISPLAY_PATHS_MAX = 2048
DISPLAY_PATH_TTL_S = 60  # looked up again after this: a newer transcript of the same id elsewhere shows within a minute


def _cached_path(key, find, clock=time.monotonic):
    """The path find() returns, remembered under key for DISPLAY_PATH_TTL_S while it still exists. A miss is never
    remembered (a transcript that shows up later is found the next time), and a remembered path that is gone (moved,
    deleted) is looked up again. Display only: dispatch decisions call find_transcript themselves every time."""
    now = clock()
    hit = _DISPLAY_PATHS.get(key)
    if hit is not None and now - hit[1] < DISPLAY_PATH_TTL_S and hit[0].exists():
        return hit[0]
    _DISPLAY_PATHS.pop(key, None)
    path = find()
    if path is not None:
        if len(_DISPLAY_PATHS) >= _DISPLAY_PATHS_MAX:
            _DISPLAY_PATHS.clear()
        _DISPLAY_PATHS[key] = (path, now)
    return path


def display_transcript(profiles, sid, prefer=None):
    """find_transcript for what is only shown (the list's last activity and context, the info card's quote)."""
    profiles = list(profiles)
    key = ("claude", tuple(str(p.projects_dir) for p in profiles), sid, prefer)
    return _cached_path(key, lambda: find_transcript(profiles, sid, prefer=prefer))


def _activity(cfg, agent, profile, sid):
    """(last activity datetime | None, context tokens | None, context window | None) of a conversation; the window is Codex only."""
    if not sid:
        return None, None, None
    if agent == "codex":
        path = find_rollout(cfg.codex.home, sid)  # not cached: a resumed thread may write a newer rollout
        if path is None:
            return None, None, None
        return (rollout_last_time(path), *rollout_context(path))
    t = display_transcript(cfg.profiles.values(), sid, prefer=profile)
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
    with_model = any(r.model for r in rows)  # the column only shows once somebody uses it: old output stays as it was
    with_registry = any(r.registry == "retired" for r in rows)  # likewise: no retired row, no 登記 column
    widths = [38, 6, 12] + ([18] if with_model else []) + [8, 8] + ([10] if with_registry else []) + [16, 7, 11]

    def line(*cells):
        return " ".join(ljust(c, w) for c, w in zip(cells, widths)) + " " + cells[-1]

    out(line("名稱", "agent", "profile", *(["模型"] if with_model else []), "id", "對話",
             *(["登記"] if with_registry else []), "狀態", "活動", "最後活動", "context"))
    for r in rows:
        reg = ("retired ⚠" if r.live == "running" else "retired") if r.registry == "retired" else (r.registry or "-")
        out(line(r.name, r.agent, r.profile, *([r.model or "-"] if with_model else []), r.id[:8] or "-", r.sid[:8],
                 *([reg] if with_registry else []),
                 r.run_display if with_registry else r.display, r.agent_status, _fmt_time(r.last),
                 _fmt_tokens(r.tokens, r.context_window)))
    for r in rows:
        if r.registry == "retired" and r.live == "running":
            out(f"⚠ retired 但還在執行：{r.name}（sampan stop {r.name} 下線；sampan activate {r.name} 還原）")
    _footer(cfg, run, out)
    return 1 if warning else 0


def _footer(cfg, run, out):
    """The count, the limit and the memory light in one line (limits.report); not at all with no limit and no guard,
    and not when nothing can be read."""
    from . import limits
    try:
        rep = limits.report(cfg, run)
    except Exception:  # noqa: BLE001  a footer must never break status
        return
    if rep.show:
        out(rep.footer())
