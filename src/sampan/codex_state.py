"""Which Codex thread each tmux pane is running, and what its screen says."""
import json
import os
import re
import shutil
import sqlite3
import time
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import codex_daemon
from .claude_state import Reply
from .fsutil import cache_by_stat, reverse_lines
from .procs import UUID_RE, is_uuid

ROLLOUT_RE = re.compile(rf"^rollout-.+-({UUID_RE})\.jsonl$")
STATE_RE = re.compile(r"^state_(\d+)\.sqlite$")
ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")
FALLBACK_WINDOW = 120  # seconds: the most threads.created_at may differ from the process start time
TIE_TOLERANCE = 2  # seconds: if the two closest candidates are within this range → cannot tell

AMBIGUOUS = "ambiguous"  # threads_fallback: two candidates are equally close


@dataclass(frozen=True)
class LiveCodex:
    pid: int
    thread_id: object  # str | None
    cwd: str
    pane_id: str


def _is_codex(p):
    if p.zombie:
        return False
    argv = p.args.split()
    names = [os.path.basename(a) for a in argv[:2]]
    if p.comm == "codex" or names[:1] == ["codex"]:
        return True
    # a codex installed through npm is wrapped by node: comm is node and the second argument is …/bin/codex
    return names[:1] in (["node"], ["bun"]) and names[1:2] == ["codex"]


def codex_pid_in_pane(procs, pane_pid):
    """Deepest codex process in the pane, the pane's own process included (the native binary holds the rollout open, not the node wrapper)."""
    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p)
    best, best_depth = None, -1
    root = procs.get(pane_pid)
    # the pane's own process counts too (a pane started as `tmux new-session … codex` has no shell)
    queue = deque([(root, 0)] if root is not None else ((p, 1) for p in children.get(pane_pid, [])))
    seen = set()
    while queue:
        p, depth = queue.popleft()
        if p.pid in seen:
            continue
        seen.add(p.pid)
        if _is_codex(p) and depth > best_depth:
            best, best_depth = p, depth
        queue.extend((c, depth + 1) for c in children.get(p.pid, []))
    return best.pid if best is not None else None


def _sessions_dirs(codex_home):
    home = Path(codex_home)
    return {home / "sessions", Path(os.path.realpath(home)) / "sessions"}


LSOF_FALLBACKS = ("/usr/sbin/lsof", "/usr/bin/lsof")  # macOS, Linux; launchd's PATH may not include /usr/sbin


def lsof_command(which=shutil.which, exists=os.path.exists):
    """Path of lsof: PATH first, then the usual locations; None when this machine has none."""
    return which("lsof") or next((p for p in LSOF_FALLBACKS if exists(p)), None)


LSOF = lsof_command()


def rollout_from_lsof(run, pid, codex_home):
    """Thread id of the rollout file the process has open; None when none or more than one."""
    if LSOF is None:  # no lsof on this machine: the thread is unknown, not an error
        return None
    r = run([LSOF, "-p", str(pid), "-Fn"])
    if r.returncode != 0:
        return None
    roots = _sessions_dirs(codex_home)
    ids = set()
    for line in r.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path = Path(line[1:])
        m = ROLLOUT_RE.match(path.name)
        if m and any(root in path.parents for root in roots):
            ids.add(m.group(1))
    return ids.pop() if len(ids) == 1 else None


def _state_db(codex_home):
    best = None
    for f in Path(codex_home).glob("state_*.sqlite"):
        m = STATE_RE.match(f.name)
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), f)
    return best[1] if best else None


# the interactive TUI is source 'cli' when it runs its own app-server; attached to the shared daemon (Codex 0.164+) its
# threads are source 'vscode' with originator 'codex-tui' (the Desktop app is 'vscode' + 'Codex Desktop': not a match)
_THREADS_SQL = "select id, created_at from threads where ({match}) and archived = 0 and cwd in ({marks})"


def threads_fallback(codex_home, cwd, started_at, exclude):
    """Interactive thread in cwd created closest to started_at (≤ 120 s); None when absent, AMBIGUOUS when two are as close."""
    db = _state_db(codex_home)
    if db is None:
        return None
    cwds = sorted({cwd, os.path.realpath(cwd)})
    marks = ",".join("?" * len(cwds))
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
        try:
            try:
                rows = con.execute(_THREADS_SQL.format(match="source = 'cli' or originator = 'codex-tui'", marks=marks),
                                   cwds).fetchall()
            except sqlite3.OperationalError:  # an older state db without the originator column
                rows = con.execute(_THREADS_SQL.format(match="source = 'cli'", marks=marks), cwds).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    diffs = sorted((abs(created - started_at), tid) for tid, created in rows
                   if tid not in exclude and isinstance(created, (int, float)))
    diffs = [d for d in diffs if d[0] <= FALLBACK_WINDOW]
    if not diffs:
        return None
    if len(diffs) > 1 and diffs[1][0] - diffs[0][0] <= TIE_TOLERANCE:
        return AMBIGUOUS  # two equally close: do not guess
    return diffs[0][1]


def daemon_fallback(snap, cwd, exclude, started_at, panes_in_cwd=1):
    """Live codex-tui threads the daemon reports in cwd that no other pane has and that were touched since this process
    started (a stale thread of an earlier TUI is not this pane's): a thread id, AMBIGUOUS when there are several or more
    than the tmux panes that could own them (one runs outside tmux), None when none. For a pane whose db row is not
    near its start time (a `/new` long after launch)."""
    cwds = {cwd, os.path.realpath(cwd)}
    ids = sorted({t.id for t in snap.threads
                  if t.originator == "codex-tui" and t.id not in exclude and (t.id in snap.loaded or t.status in ("active", "idle"))
                  and (t.cwd in cwds or os.path.realpath(t.cwd) in cwds)
                  and t.updated_at.timestamp() >= started_at - TIE_TOLERANCE})
    if len(ids) > max(1, panes_in_cwd):
        return AMBIGUOUS
    return ids[0] if len(ids) == 1 else AMBIGUOUS if ids else None


def _started_at(run, pid, now):
    r = run(["ps", "-o", "etime=", "-p", str(pid)])
    m = ETIME_RE.match(r.stdout.strip()) if r.returncode == 0 else None
    if not m:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return now - (((days * 24 + hours) * 60 + minutes) * 60 + seconds)


def live_codex(cfg, run, panes, procs, now=None, snapshot_fn=None):
    """Map pane id → LiveCodex. Rollout via lsof first (a TUI attached to the daemon does not hold it), then the threads
    table, then the daemon's loaded threads for what is still unknown; never two panes on one thread."""
    if not cfg.codex.enabled:
        return {}, []
    home = cfg.codex.home
    now = time.time() if now is None else now
    found, pending = {}, []
    for pane in panes:
        pid = codex_pid_in_pane(procs, pane.pane_pid)
        if pid is None:
            continue
        tid = rollout_from_lsof(run, pid, home)
        found[pane.pane_id] = LiveCodex(pid, tid, pane.path, pane.pane_id)
        if tid is None:
            pending.append(pane)
    bound = {lc.thread_id for lc in found.values() if lc.thread_id}
    guesses, started_at = {}, {}
    for pane in pending:
        started = started_at[pane.pane_id] = _started_at(run, found[pane.pane_id].pid, now)
        guesses[pane.pane_id] = threads_fallback(home, pane.path, started, bound) if started is not None else None
    unresolved = [p for p in pending if guesses[p.pane_id] is None and started_at[p.pane_id] is not None]
    if unresolved:  # the only daemon call: nothing is read for a machine with no Codex pane, or when lsof / db found all
        snap = (snapshot_fn or codex_daemon.snapshot)(home, run, now=datetime.fromtimestamp(now, timezone.utc))
        if snap is not None and os.path.realpath(snap.home) == os.path.realpath(home):  # another CODEX_HOME's threads are not ours
            taken = bound | {t for t in guesses.values() if t and t != AMBIGUOUS}
            for pane in unresolved:
                same = sum(1 for p in unresolved if os.path.realpath(p.path) == os.path.realpath(pane.path))
                guesses[pane.pane_id] = daemon_fallback(snap, pane.path, taken, started_at[pane.pane_id], same)
    counts = Counter(t for t in guesses.values() if t and t != AMBIGUOUS)
    warnings = []
    for pane in pending:
        lc, tid = found[pane.pane_id], guesses[pane.pane_id]
        if tid is None:
            continue  # no thread yet (just opened, first prompt not sent): normal, stay quiet
        if tid != AMBIGUOUS and counts[tid] == 1:
            found[pane.pane_id] = replace(lc, thread_id=tid)
            continue
        why = "同目錄有多個 Codex 對到同一段對話" if tid != AMBIGUOUS else "同目錄有兩段對話一樣接近"
        warnings.append(f"{pane.session}：有 codex（pid {lc.pid}）但讀不到 rollout，{why}，無法判斷 thread id")
    return found, warnings


def find_rollout(codex_home, thread_id):
    if not is_uuid(thread_id):  # used in a glob: an unvalidated id must not be used
        return None
    sessions = Path(codex_home) / "sessions"
    if not sessions.is_dir():
        return None
    hits = sorted(sessions.glob(f"**/rollout-*-{thread_id}.jsonl"))
    return hits[-1] if hits else None


def rollout_last_time(path, tail_bytes=65536):
    """Timestamp of the rollout's last record that has one; the file's mtime when none can be read."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - tail_bytes))
            lines = fh.read().decode(errors="replace").splitlines()
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    for line in reversed(lines):
        try:
            ts = json.loads(line).get("timestamp")
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(mtime, timezone.utc)


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def rollout_context(path, tail_bytes=262144):
    """(input tokens of the last turn, model context window) from the rollout's newest token_count; (None, None) when unreadable.

    last_token_usage.input_tokens already includes the cached input. A token_count whose info is null is skipped."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - tail_bytes))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        try:
            payload = json.loads(line).get("payload")
            if payload.get("type") != "token_count" or not isinstance(payload.get("info"), dict):
                continue
            info = payload["info"]
            return _int(info["last_token_usage"]["input_tokens"]), _int(info.get("model_context_window"))
        except (ValueError, AttributeError, TypeError, KeyError):
            continue
    return None, None


def last_user_text(path):
    """The last message the user sent in a rollout (event_msg user_message); "" when none can be read."""
    try:
        return _last_user_text(path)
    except OSError:
        return ""


@cache_by_stat
def _last_user_text(path):
    for line in reverse_lines(path):
        if '"user_message"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload")
        except (ValueError, AttributeError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "user_message":
            msg = payload.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg
    return ""


def _item_text(item):
    parts = item.get("content")
    if not isinstance(parts, list):
        return ""
    return "".join(c["text"] for c in parts if isinstance(c, dict) and isinstance(c.get("text"), str))


def _event(line):
    """(payload type, payload) of an event_msg line; (None, None) for anything else."""
    try:
        rec = json.loads(line)
    except ValueError:
        return None, None
    payload = rec.get("payload") if isinstance(rec, dict) and rec.get("type") == "event_msg" else None
    if not isinstance(payload, dict):
        return None, None
    return payload.get("type"), payload


def _user_event(kind, payload):
    """(text, turn_id) of a user message event: Codex 0.164 item_completed/UserMessage, or the older user_message."""
    if kind == "item_completed" and isinstance(payload.get("item"), dict) and payload["item"].get("type") == "UserMessage":
        return _item_text(payload["item"]), payload.get("turn_id")
    if kind == "user_message" and isinstance(payload.get("message"), str):
        return payload["message"], None
    return None, None


def reply_to(path, sent):
    """The reply to the last user message of a rollout whose text equals sent: the last AgentMessage of that turn
    (final_answer preferred), else task_complete.last_agent_message; done once that turn's task_complete is seen.
    Also reads the older user_message / agent_message events. Reply(False, False, "") when sent is not in the rollout."""
    try:
        return _reply_to(path, sent)
    except OSError:
        return Reply(False, False, "")


def _reply_to(path, sent):
    after = []  # (kind, payload) after the candidate, newest first
    for line in reverse_lines(path):
        kind, payload = _event(line)
        if kind is None:
            continue
        text, turn = _user_event(kind, payload)
        if text is not None and text == sent:
            return _reply_from(turn, list(reversed(after)))
        after.append((kind, payload))
    return Reply(False, False, "")


def _reply_from(turn, events):
    finals, others, complete, done, closed = [], [], None, False, False
    for kind, payload in events:
        text, uturn = _user_event(kind, payload)
        if text is not None and (turn is None or uturn != turn):  # the next message starts another turn (legacy: no turn ids)
            closed = True
            break
        if turn is not None and payload.get("turn_id") not in (None, turn):
            continue
        if kind == "item_completed" and isinstance(payload.get("item"), dict) and payload["item"].get("type") == "AgentMessage":
            msg = _item_text(payload["item"])
            if msg.strip():
                (finals if payload["item"].get("phase") == "final_answer" else others).append(msg)
        elif kind == "agent_message" and isinstance(payload.get("message"), str) and payload["message"].strip():
            others.append(payload["message"])
        elif kind == "task_complete":
            done = True
            last = payload.get("last_agent_message")
            if isinstance(last, str) and last.strip():
                complete = last
    text = (finals or others)[-1] if (finals or others) else (complete or "")
    return Reply(True, done, text, closed)
