import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
RESUME_RE = re.compile(rf"--(?:resume|session-id)[ =]({UUID_RE})")
ENV_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=(\S*)")


def is_uuid(value):
    """A conversation id sampan may pass to claude/codex or use in a file glob: a lowercase UUID string."""
    return isinstance(value, str) and re.fullmatch(UUID_RE, value) is not None


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    stat: str
    comm: str
    args: str

    @property
    def zombie(self):
        return self.stat.startswith("Z")


def list_procs(run):
    r = run(["ps", "-A", "-o", "pid=,ppid=,stat=,comm=,args="])
    if r.returncode != 0:
        return {}
    procs = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        procs[pid] = Proc(pid, ppid, parts[2], os.path.basename(parts[3]), parts[4] if len(parts) > 4 else "")
    return procs


def claude_pids(procs):
    return {p.pid for p in procs.values() if p.comm == "claude" and not p.zombie}


def is_descendant(procs, pid, root_pid):
    """Whether pid runs inside the tree rooted at root_pid, root_pid itself included (a pane started as
    `tmux new-session … claude` has claude as its own process, with no shell in between)."""
    if pid == root_pid:
        return pid in procs
    seen = set()
    cur = procs.get(pid)
    while cur is not None and cur.pid not in seen:
        if cur.ppid == root_pid:
            return True
        seen.add(cur.pid)
        cur = procs.get(cur.ppid)
    return False


def descendant_claude(procs, root_pid):
    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p)
    root = procs.get(root_pid)
    queue = deque([root] if root is not None else children.get(root_pid, []))  # the pane's own process counts
    while queue:
        p = queue.popleft()
        if p.comm == "claude" and not p.zombie:
            return p
        queue.extend(children.get(p.pid, []))
    return None


def proc_env(run, pid, proc_root=Path("/proc")):
    """Environment of a process, or None when it cannot be read (never guess)."""
    try:
        data = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        data = None  # macOS has no /proc
    if data is not None:
        env = {}
        for item in data.split(b"\0"):
            key, sep, value = item.decode(errors="replace").partition("=")
            if sep:
                env[key] = value
        return env
    r = run(["ps", "eww", "-o", "command=", "-p", str(pid)])
    if r.returncode != 0:
        return None
    env = dict(ENV_RE.findall(r.stdout))
    if "PATH" not in env and "HOME" not in env:
        return None  # ps did not print the environment (permissions or a different format): treat as unreadable
    return env


def resume_id(args):
    m = RESUME_RE.search(args)
    return m.group(1) if m else None
