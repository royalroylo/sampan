import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import ids
from .clock import now_stamp
from .fsutil import PRIVATE_FILE, atomic_write, private_dir, private_file, try_chmod
from .procs import is_uuid

STATES = ("active", "retired")
AGENTS = ("claude", "codex")
VERSION = 3
READABLE = (1, 2, 3)


class RegistryError(Exception):
    pass


@dataclass
class Row:
    tmux: str
    agent: str  # "claude" | "codex"
    profile: str  # codex 列是 Codex profile 名稱（舊資料可能是 ""）
    session_id: str
    cwd: str
    state: str = "active"
    seen_at: str = ""
    server: str = ""  # tmux server when it was last seen (pid-start_time)
    pid: int | None = None  # agent process last seen
    id: str = ""  # stable sampan id (uuid4), kept across saves and renames; "" until write_rows fills it


@dataclass(frozen=True)
class Observed:
    tmux: str
    agent: str
    profile: str
    session_id: str
    cwd: str
    pid: int
    sampan_id: str = ""  # the id sampan stamped on the tmux session, "" when not stamped


@dataclass
class MergeResult:
    rows: list
    added: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def merge(old, observed, tmux_sessions, now, server, keep_silently=frozenset()):
    """Merge what is running now into the registry.

    Rows are never dropped: a row whose tmux session is gone (stopped, closed by hand, or a reboot) is kept as
    pending for `sampan restore`; only the user retires it. A row whose tmux session is still
    there but has no observed agent is kept as is, with a warning unless its name is in
    keep_silently (sessions the caller already explained, or a Codex with no thread yet).
    """
    res = MergeResult(rows=[])
    by_name = {r.tmux: r for r in old}
    used = set()
    new_rows = {}
    for ob in observed:
        prev = by_name.get(ob.tmux)
        if prev is None:
            if ob.sampan_id:  # the stamped id names the row first; session_id is the fallback
                prev = next((o for o in old if o.id == ob.sampan_id and o.tmux not in used), None)
            if prev is None:
                prev = next((o for o in old if o.session_id == ob.session_id
                             and o.tmux not in tmux_sessions and o.tmux not in used), None)
            if prev is None:
                res.added.append(ob.tmux)
            else:
                res.renamed.append((prev.tmux, ob.tmux))
        elif (prev.agent, prev.profile, prev.session_id, prev.cwd) != (ob.agent, ob.profile, ob.session_id, ob.cwd):
            res.updated.append(ob.tmux)
        if prev is not None:
            used.add(prev.tmux)
        state = prev.state if prev is not None else "active"
        new_rows[ob.tmux] = Row(ob.tmux, ob.agent, ob.profile, ob.session_id, ob.cwd, state, now, server, ob.pid,
                                prev.id if prev is not None else ids.new_id())
    for r in old:
        if r.tmux in used or r.tmux in new_rows:
            continue
        if r.tmux in tmux_sessions:
            new_rows[r.tmux] = r
            if r.state == "active" and r.tmux not in keep_silently:
                res.warnings.append(f"{r.tmux}：沒有 {r.agent}，沿用上次的對話 {r.session_id[:8]}")
        elif r.state == "retired":
            new_rows[r.tmux] = r
        else:
            new_rows[r.tmux] = r
            res.pending.append(r.tmux)
    res.pending.sort()
    res.rows = sorted(new_rows.values(), key=lambda r: r.tmux)
    return res


def id_problem(session_id):
    """Why a registered conversation id cannot be resumed (None = it is a UUID). Checked before any lookup: an empty or
    malformed id would make claude/codex start a new conversation."""
    if not session_id:
        return "沒有對話 id，無法接回（不改開新對話）"
    if not is_uuid(session_id):
        return f"對話 id {session_id!r} 不是 UUID，無法接回（不改開新對話）"
    return None


@contextmanager
def lock(state_dir):
    state_dir = private_dir(state_dir)
    with os.fdopen(os.open(state_dir / "registry.lock", os.O_WRONLY | os.O_CREAT, PRIVATE_FILE), "w") as fh:
        try_chmod(fh.fileno(), PRIVATE_FILE)
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_rows(path, *, codex_profile=""):
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        version = data["version"]
        if version not in READABLE:
            raise RegistryError(f"{path} 是未知的版本 {version!r}（只認得 1、2、3）。不會覆蓋它；請升級 sampan 或從 state/history/ 挑一份還原。")
        rows = [Row(**({"agent": "claude", "pid": None} | s)) for s in data["sessions"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RegistryError(f"{path} 無法解析（{e}）。不會覆蓋它；請從 state/history/ 挑一份還原。") from e
    for r in rows:
        if r.agent == "codex" and r.profile == "":  # old rows: fill in the Codex profile name
            r.profile = codex_profile
        if r.state not in STATES:
            raise RegistryError(f"{path}：{r.tmux} 的 state 是 {r.state!r}，只能是 {STATES}")
        if r.agent not in AGENTS:
            raise RegistryError(f"{path}：{r.tmux} 的 agent 是 {r.agent!r}，只能是 {AGENTS}")
        if not isinstance(r.session_id, str):
            raise RegistryError(f"{path}：{r.tmux} 的 session_id 是 {r.session_id!r}，必須是字串")
        if r.pid is not None and (type(r.pid) is not int):
            raise RegistryError(f"{path}：{r.tmux} 的 pid 是 {r.pid!r}，必須是整數或 null")
    return rows


# Row fields that change on every save without the session itself changing; ignored when deciding on a history snapshot.
VOLATILE_FIELDS = ("seen_at", "pid", "server")


def _stable_sessions(sessions):
    return [{k: v for k, v in s.items() if k not in VOLATILE_FIELDS} for s in sessions]


def _only_volatile_changes(path, new_sessions):
    """True when the registry at path holds the same sessions as new_sessions apart from VOLATILE_FIELDS.
    An unreadable old file is never "same": it should be kept as a snapshot."""
    try:
        old = json.loads(Path(path).read_text())["sessions"]
        return _stable_sessions(old) == _stable_sessions(new_sessions)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def load_machine(path):
    """The machine id recorded in the registry header ("" for a version 1/2 file, a missing or unreadable one)."""
    try:
        m = json.loads(Path(path).read_text()).get("machine", "")
        return m if isinstance(m, str) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def write_rows(path, rows, now, history_dir, keep=20, *, machine=""):
    path = Path(path)
    for r in rows:
        if not r.id:
            r.id = ids.new_id()
    history_dir = Path(history_dir)
    private_dir(path.parent)
    sessions = [asdict(r) for r in rows]
    if path.exists() and not _only_volatile_changes(path, sessions):
        private_dir(history_dir)
        snap = history_dir / f"registry-{now_stamp()}.json"
        # Created 0600 and copied by hand, not shutil.copy2: copy2 also copies the mode with chmod, which fails where chmod is refused.
        with open(path, "rb") as src, os.fdopen(os.open(snap, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE), "wb") as dst:
            shutil.copyfileobj(src, dst)
        private_file(snap)
        st = path.stat()
        try:
            os.utime(snap, ns=(st.st_atime_ns, st.st_mtime_ns))  # keep the registry's mtime: snapshots are pruned by mtime
        except OSError:
            pass
        # By mtime, not by name: older snapshots are named in local time (Taipei), newer ones in UTC, so names
        # from before and after an upgrade do not sort in time order.
        snaps = sorted(history_dir.glob("registry-*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
        for old in snaps[:-keep]:
            old.unlink()
        for kept in snaps[-keep:]:
            private_file(kept)  # snapshots an older version left readable by others
    body = {"version": VERSION, "machine": machine, "saved_at": now, "sessions": sessions}
    atomic_write(path, json.dumps(body, ensure_ascii=False, indent=2) + "\n", mode=PRIVATE_FILE)


def set_state(rows, name, state):
    if state not in STATES:
        raise ValueError(f"state 只能是 {STATES}，不是 {state!r}")
    for r in rows:
        if r.tmux == name:
            r.state = state
            return True
    return False
