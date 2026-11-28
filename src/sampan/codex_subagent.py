"""Which Codex threads are subagents (the review guardian and the like), and which thread is their parent.

A subagent never is a conversation a person can resume or send to: it must not be registered for a pane. The decision
is made from `source` alone (plus a few `thread_source` names): a thread forked from another one, or attached to
one, is still a main conversation. Reads only: the threads table, the rollout's first line, the spawn edges."""
import json
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import quote

SUBAGENT_THREAD_SOURCES = ("guardian_review", "subagent")
MAX_DEPTH = 5  # parents followed up from a subagent of a subagent
FIRST_LINE_MAX = 1 << 20


def source_is_subagent(source):
    """source in any of the shapes it comes in: the threads column (JSON text: {"subagent": {"other": "guardian"}},
    {"subagent": "review"}, {"subagent": {"thread_spawn": {...}}}, or a plain word like vscode / cli), the rollout's
    session_meta (the same, parsed), the daemon's reduced one (the first key: "subagent")."""
    if isinstance(source, dict):
        return any(isinstance(k, str) and k.lower() == "subagent" for k in source)  # the app-server writes subAgent
    if isinstance(source, str):
        text = source.strip()
        if text.lower() == "subagent":
            return True
        if text.startswith("{"):
            try:
                return source_is_subagent(json.loads(text))
            except ValueError:
                return text.lower().startswith('{"subagent"')
    return False


def is_subagent(source, thread_source=None):
    return source_is_subagent(source) or (isinstance(thread_source, str) and thread_source in SUBAGENT_THREAD_SOURCES)


@dataclass(frozen=True)
class Meta:
    session_id: str | None
    parent_id: str | None
    subagent: bool


def rollout_meta(path):
    """The first line (session_meta) of a rollout; None when it cannot be read as one."""
    try:
        with open(path, encoding="utf-8") as f:
            line = f.readline(FIRST_LINE_MAX)
        data = json.loads(line)
    except (OSError, ValueError):
        return None
    meta = data.get("payload") if isinstance(data, dict) and isinstance(data.get("payload"), dict) else data
    if not isinstance(meta, dict) or ("source" not in meta and "id" not in meta):
        return None
    parent = meta.get("parent_thread_id")
    return Meta(meta.get("id") if isinstance(meta.get("id"), str) else None,
                parent if isinstance(parent, str) and parent else None,
                is_subagent(meta.get("source"), meta.get("thread_source")))


@dataclass(frozen=True)
class DbRow:
    id: str
    cwd: str
    archived: bool
    subagent: bool


def _columns(con):
    return {r[1] for r in con.execute("pragma table_info(threads)")}


def db_rows(codex_home, ids):
    """{id: DbRow} for the ids the threads table has; {} when there is no readable state db. A db older than the
    thread_source column is judged by source alone."""
    from . import codex_state
    ids = sorted(set(ids))
    db = codex_state._state_db(codex_home)
    if db is None or not ids:
        return {}
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
        try:
            cols = _columns(con)
            extra = ", thread_source" if "thread_source" in cols else ", null"
            marks = ",".join("?" * len(ids))
            rows = con.execute(f"select id, cwd, archived, source{extra} from threads where id in ({marks})", ids).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {i: DbRow(i, cwd or "", bool(arch), is_subagent(src, ts)) for i, cwd, arch, src, ts in rows}


def spawn_parent(codex_home, thread_id):
    """The parent the spawn edges table names for thread_id (a db without the table has none)."""
    from . import codex_state
    db = codex_state._state_db(codex_home)
    if db is None:
        return None
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
        try:
            row = con.execute("select parent_thread_id from thread_spawn_edges where child_thread_id = ?",
                              (thread_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def thread_is_subagent(codex_home, thread_id, *, rollout=None, row=None):
    """True/False when the threads table or the rollout says so, None when neither knows the thread. The table is asked
    first (one cheap read): a row that says subagent decides, a row that says not is believed; only a thread the table
    lacks is looked for among the rollout files (a glob of the whole sessions tree)."""
    from . import codex_state
    row = row if row is not None else db_rows(codex_home, [thread_id]).get(thread_id)
    if row is not None:
        return row.subagent
    path = rollout or codex_state.find_rollout(codex_home, thread_id)
    meta = rollout_meta(path) if path else None
    return None if meta is None else meta.subagent


def parent_of(codex_home, thread_id, daemon_parent=None):
    """The parent thread of a subagent: its rollout's parent_thread_id (found by glob: the db's rollout_path is not
    trusted), else the db's spawn edge, else what the daemon reported (daemon_parent: a function id → parent id or None,
    called only when the first two say nothing); None when none says."""
    from . import codex_state
    path = codex_state.find_rollout(codex_home, thread_id)
    meta = rollout_meta(path) if path else None
    if meta is not None and meta.parent_id:
        return meta.parent_id
    return spawn_parent(codex_home, thread_id) or (daemon_parent(thread_id) if daemon_parent else None)


def root_of(codex_home, thread_id, daemon_parent=None):
    """(root thread id, None) from a subagent up to the first thread that is not one (a subagent of a subagent goes up
    until it ends, at most MAX_DEPTH); (None, why) when the chain breaks."""
    seen, tid = {thread_id}, thread_id
    for _ in range(MAX_DEPTH):
        parent = parent_of(codex_home, tid, daemon_parent)
        if parent is None:
            return None, "找不到父對話"
        if parent in seen:
            return None, "父對話繞回自己"
        if thread_is_subagent(codex_home, parent) is not True:
            return parent, None
        seen.add(parent)
        tid = parent
    return None, "往上找太多層"


def parent_problem(codex_home, parent_id, cwd):
    """Why parent_id cannot take a pane's place (None = it can): it has to be in the threads table, not archived, not a
    subagent, and in the same directory."""
    row = db_rows(codex_home, [parent_id]).get(parent_id)
    if row is None:
        return "父對話不在 Codex 的資料庫裡"
    if row.archived:
        return "父對話已封存"
    if row.subagent:
        return "父對話也是小代理"
    if os.path.realpath(row.cwd) != os.path.realpath(cwd):
        return "父對話的目錄與這個 session 不同"
    return None


def daemon_parent_fn(cfg, run, snapshot_fn=None):
    """A function id → parentThreadId the daemon reports; the daemon is asked once, and only when it is called."""
    from . import codex_daemon
    cache = {}

    def parent(tid):
        if "snap" not in cache:
            try:
                snap = (snapshot_fn or codex_daemon.snapshot)(cfg.codex.home, run, now=datetime.now(timezone.utc))
            except Exception:  # an unreadable daemon is just no answer
                snap = None
            cache["snap"] = {t.id: t.parent_id for t in snap.threads} if snap is not None else {}
        return cache["snap"].get(tid)
    return parent


@dataclass(frozen=True)
class Decision:
    row: object
    parent: str | None  # what the row would become; None = left alone
    problem: str | None  # why it is left alone
    looked: bool = False  # the problem came from the (costly) lookup of the parent, not from what is running now


def warning_text(decision):
    r = decision.row
    return f"{r.tmux}：登記的 Codex 對話是審查小代理（{r.session_id[:8]}），{decision.problem}，沒有改"


def plan_rows(cfg, run, rows, observed, panes=None, procs=None, snapshot_fn=None, skip=None):
    """One Decision for every Codex row whose registered conversation is a review subagent (none when there is none: then
    nothing else is read). The one place that says whether and to what a row is changed; `sampan save` applies it and
    `sampan doctor` reports it. A row is changed only when this round did not observe it (what the screen shows now wins:
    /new, /clear), it is not retired, no Codex runs in its tmux session (an unreadable one is only reported), the parent
    is found and is a main conversation in the same directory, no other row holds it, no other row is changed to it too,
    and no other live Codex pane is in that directory. skip: {tmux: sid} of rows whose lookup failed recently: left alone,
    nothing looked up again."""
    from . import codex_state
    if not cfg.codex.enabled:
        return []
    home = cfg.codex.home
    observed = set(observed)
    cands = [r for r in rows if r.agent == "codex" and r.state != "retired" and r.tmux not in observed and r.session_id]
    known = db_rows(home, [r.session_id for r in cands])
    subs = [r for r in cands if thread_is_subagent(home, r.session_id, row=known.get(r.session_id)) is True]
    skip = skip or {}
    subs = [r for r in subs if skip.get(r.tmux) != r.session_id]
    if not subs:
        return []
    if panes is None or procs is None:
        from .procs import list_procs
        from .tmux import Tmux
        panes = Tmux(run).panes() or []
        procs = list_procs(run)
    live_panes = [p for p in panes if codex_state.codex_pid_in_pane(procs, p.pane_pid) is not None]
    daemon_parent = daemon_parent_fn(cfg, run, snapshot_fn)
    out = []
    for r in subs:
        parent = problem = None
        looked = False
        if any(p.session == r.tmux for p in live_panes):
            problem = "它的 tmux 裡有 codex 在跑，但這輪沒認出是哪一段對話"
        else:
            parent, problem = root_of(home, r.session_id, daemon_parent)
            if problem is None:
                problem = parent_problem(home, parent, r.cwd)
            looked = problem is not None
            if problem is None and any(o.tmux != r.tmux and o.session_id == parent for o in rows):
                problem = "父對話已經登記在別的 session"
            if problem is None and any(p.session != r.tmux and os.path.realpath(p.path) == os.path.realpath(r.cwd)
                                       for p in live_panes):
                problem = "同目錄還有別的 codex 在跑，不確定哪一段是這個 session 的"
        out.append(Decision(r, None if problem else parent, problem, looked))
    wanted = {}
    for d in out:
        if d.parent:
            wanted[d.parent] = wanted.get(d.parent, 0) + 1
    return [Decision(d.row, None, "另一個 session 也登記在同一個父對話的小代理上，分不出哪個才是它") if d.parent and wanted[d.parent] > 1 else d
            for d in out]


SKIP_FILE = "heal-skip.json"
SKIP_S = 3600  # a row whose parent cannot be found is looked at again after this long (each look reads the sessions tree)


def _skip_load(path, now):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("at"), (int, float))
            and now - v["at"] < SKIP_S} if isinstance(data, dict) else {}


def heal_rows(cfg, run, rows, observed, panes, procs, snapshot_fn=None, now=None, dry_run=False):
    """Apply plan_rows to the registry rows. Returns (rows, healed, warnings): healed is [(name, old id, new id)].
    A row whose parent could not be looked up warns once and then stays quiet for SKIP_S (heal-skip.json in the state dir, written
    under the save lock, not on --dry-run): every five minutes the same warning, the same lookups, would be noise."""
    import time
    from pathlib import Path

    from . import fsutil
    if not cfg.heal_subagent or not cfg.codex.enabled:
        return rows, [], []
    now = time.time() if now is None else now
    memo = Path(cfg.state_dir) / SKIP_FILE
    recent = _skip_load(memo, now)
    decisions = plan_rows(cfg, run, rows, observed, panes, procs, snapshot_fn,
                          skip={k: v.get("sid") for k, v in recent.items()})
    by_name = {d.row.tmux: d for d in decisions}
    out = [replace(r, session_id=by_name[r.tmux].parent) if r.tmux in by_name and by_name[r.tmux].parent else r for r in rows]
    healed = [(d.row.tmux, d.row.session_id, d.parent) for d in decisions if d.parent]
    warnings = [warning_text(d) for d in decisions if d.problem]
    if not dry_run:
        keep = {k: v for k, v in recent.items() if any(r.tmux == k and r.session_id == v.get("sid") for r in rows)}
        for d in decisions:
            if d.problem and d.looked:
                keep[d.row.tmux] = {"sid": d.row.session_id, "at": now}
        if keep != recent or (keep and not memo.exists()):
            try:
                fsutil.atomic_write(memo, json.dumps(keep), mode=fsutil.PRIVATE_FILE)
            except OSError:
                pass
    return out, healed, warnings
