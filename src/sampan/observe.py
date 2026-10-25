"""Which agent each tmux pane is running, and which conversation it is in."""
from dataclasses import dataclass
from pathlib import Path

from .claude_state import read_session_files
from .codex_state import codex_pid_in_pane, live_codex
from .hook import read_hook_records
from .procs import claude_pids, descendant_claude, is_descendant, proc_env, resume_id
from .registry import Observed
from .screens import codex_activity
from .tmux import Tmux

PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class LiveAgent:
    agent: str  # "claude" | "codex"
    pid: int
    profile: str  # "" for codex
    session_id: object  # str | None (no thread before Codex has its first prompt)
    cwd: str
    status: str  # claude: idle/busy/waiting/unknown (from the session file); codex: working/idle/unknown
    pane_id: str


def profile_for_dir(cfg, config_dir):
    if not config_dir:
        return cfg.default_profile.name
    target = Path(config_dir).expanduser()
    for p in cfg.profiles.values():
        if Path(p.config_dir) == target:
            return p.name
    return None


def _claude_by_pane(cfg, run, panes, procs, proc_root):
    """Claude in each pane. The pane's outermost claude is the agent; its conversation id comes from
    the SessionStart hook record, else its session file, else its --resume/--session-id argument."""
    alive = claude_pids(procs)
    files, warnings = read_session_files(cfg.profiles.values(), alive)
    hooks = read_hook_records(cfg.state_dir, alive)
    file_by_pid = {lc.pid: lc for lc in files}
    pane_by_id = {p.pane_id: p for p in panes}
    file_candidates = {}
    for lc in files:
        pane = pane_by_id.get(lc.pane_id)
        if pane is None:
            continue
        if not is_descendant(procs, lc.pid, pane.pane_pid):
            warnings.append(f"session 檔指向 {lc.pane_id}（{pane.session}），但 pid {lc.pid} 不在那個 pane 裡，略過")
            continue
        file_candidates.setdefault(lc.pane_id, []).append(lc)
    found = {}
    for pane in panes:
        top = descendant_claude(procs, pane.pane_pid)
        if top is None:
            continue
        rec = hooks.get(top.pid)
        if rec is not None:
            prof = profile_for_dir(cfg, rec.config_dir)
            if prof is not None:
                lc = file_by_pid.get(top.pid)
                found[pane.pane_id] = LiveAgent("claude", top.pid, prof, rec.session_id,
                                                lc.cwd if lc else (rec.cwd or pane.path),
                                                lc.status if lc else "unknown", pane.pane_id)
                continue
            warnings.append(f"{pane.session}：hook 記錄的 CLAUDE_CONFIG_DIR {rec.config_dir} 不是已知 profile，改看 session 檔")
        items = file_candidates.get(pane.pane_id)
        if items:
            # a claude in the pane started another claude (e.g. running sampan claude inside the Bash tool): take the outermost one
            lc = next((x for x in items if x.pid == top.pid), items[0])
            found[pane.pane_id] = LiveAgent("claude", lc.pid, lc.profile, lc.session_id, lc.cwd, lc.status, pane.pane_id)
            continue
        sid = resume_id(top.args)
        env = proc_env(run, top.pid, proc_root)
        prof = profile_for_dir(cfg, env.get("CLAUDE_CONFIG_DIR")) if env is not None else None
        if not sid or not prof:
            why = "讀不到 process 環境變數" if env is None else ("沒有 uuid 參數" if not sid else "CLAUDE_CONFIG_DIR 不是已知 profile")
            warnings.append(f"{pane.session}：有 claude（pid {top.pid}）但沒有 session 檔，判斷不出 uuid／profile（{why}），這列不更新")
            continue
        warnings.append(f"{pane.session}：沒有 session 檔，改用 process 參數判斷（{prof} {sid[:8]}），uuid 可能是 resume 前的舊值")
        found[pane.pane_id] = LiveAgent("claude", top.pid, prof, sid, pane.path, "unknown", pane.pane_id)
    return found, warnings


def live_by_pane(cfg, run, panes, procs, proc_root=PROC_ROOT, snapshot_fn=None):
    """Map pane id → LiveAgent (claude or codex)."""
    found, warnings = _claude_by_pane(cfg, run, panes, procs, proc_root)
    codex, codex_warnings = live_codex(cfg, run, [p for p in panes if p.pane_id not in found], procs,
                                       snapshot_fn=snapshot_fn)
    warnings += codex_warnings
    tmux = Tmux(run)
    for pane_id, lc in codex.items():
        found[pane_id] = LiveAgent("codex", lc.pid, cfg.codex.profile, lc.thread_id, lc.cwd, codex_activity(tmux.capture(pane_id)), pane_id)
    return found, warnings


def agent_sessions(procs, panes):
    """tmux sessions that have a claude or codex process in some pane (identified or not)."""
    return {p.session for p in panes
            if descendant_claude(procs, p.pane_pid) is not None or codex_pid_in_pane(procs, p.pane_pid) is not None}


def agent_panes(procs, panes, found):
    """tmux session → its panes that run an agent: identified ones in `found`, plus any claude or codex process
    (Codex counts even when disabled in the config, where it is just not identified)."""
    per_session = {}
    for pane in panes:
        if (pane.pane_id in found or descendant_claude(procs, pane.pane_pid) is not None
                or codex_pid_in_pane(procs, pane.pane_pid) is not None):
            per_session.setdefault(pane.session, []).append(pane)
    return per_session


def multi_agent_sessions(procs, panes, found):
    """Sessions with more than one agent: sampan does not record, resume, stop or move them."""
    return {s for s, items in agent_panes(procs, panes, found).items() if len(items) > 1}


SPLIT_HINT = "sampan 只管一個 session 一個 agent，請拆成各自的 tmux session"


def observe(cfg, run, panes, procs, proc_root=PROC_ROOT):
    """(observed, warnings, skipped): one Observed per tmux session with exactly one identified agent;
    skipped = sessions with more than one agent, which sampan does not record."""
    found, warnings = live_by_pane(cfg, run, panes, procs, proc_root)
    observed, skipped = [], set()
    for session, items in agent_panes(procs, panes, found).items():
        if len(items) > 1:
            skipped.add(session)
            warnings.append(f"{session}：同一個 tmux session 有 {len(items)} 個 agent，這個 session 不記；{SPLIT_HINT}")
            continue
        la = found.get(items[0].pane_id)
        if la is None or la.session_id is None:
            continue
        observed.append(Observed(session, la.agent, la.profile, la.session_id, la.cwd, la.pid))
    return observed, warnings, skipped
