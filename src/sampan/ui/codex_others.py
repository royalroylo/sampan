"""The main list's「Codex 其他對話」block: Codex threads the daemon knows that are not tmux sessions (mostly opened in
Codex Desktop), updated in the last 24 hours. Read only — a row of it never becomes an action (GO_DESKTOP instead).
Pure functions over a DaemonSnapshot; the only file read is the selected row's rollout tail, for its card's context."""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..adopt import _ago
from ..codex_state import rollout_context
from ..status import _fmt_time, _fmt_tokens
from .cards import Keys
from .daemon_watch import SEVERITY, STATE_LABEL, thread_state
from .text import pad

OTHERS_MAX = 10
OTHERS_WINDOW = timedelta(hours=24)
SECTION_TITLE = "── Codex 其他對話（24 小時內）──"
DAEMON_DOWN = "Codex 常駐服務沒在執行（codex remote-control start）"
STARTING = "正在啟動 Codex 常駐服務…"
START_FAILED = "Codex 常駐服務啟動失敗：{reason}（codex remote-control start）"
GO_DESKTOP = "到 Codex Desktop 操作"
OTHER_KEY = "codex:"  # list key prefix of a block row; tmux turns ":" in a session name into "_", so never a session
OTHER_COLS = (7, 10, 8, 4)  # source, folder, state, updated
OTHER_NAME_W = 32  # name column cap (lead included)
_STYLE = {"approval": "attn", "input": "attn", "error": "err", "working": "warn", "idle": "ok"}  # others: muted


@dataclass(frozen=True)
class OtherRow:
    """One block row: thread is the top-level thread (a parent, or a child whose parent is not on the list page);
    state is the most urgent thread_state of it and its children, via the child that state came from (None = itself),
    updated the newest updated_at among them (None = the thread's own)."""
    thread: object  # codex_daemon.CodexThread
    state: str
    via: str | None
    children: int
    updated: datetime | None = None

    @property
    def name(self):
        """The list key (like a tmux row's name, so Picker handles both alike); the display name is thread.name."""
        return OTHER_KEY + self.thread.id

    @property
    def when(self):
        return self.updated or self.thread.updated_at


def _under(path, roots):
    """path lies in (or is) one of roots, by whole path components: /tmpfoo is not under /tmp."""
    if not path:
        return False
    p = os.path.normpath(path)
    for r in roots:
        r = os.path.normpath(r) if r else ""
        if r and (p == r or p.startswith(r.rstrip("/") + "/")):
            return True
    return False


def _rank(state):
    return SEVERITY.index(state) if state in SEVERITY else len(SEVERITY)


def other_rows(snap, rows, now, *, tmp_roots):
    """(at most OTHERS_MAX OtherRows, how many more were cut). Listed: threads of snap.threads that are not a tmux row's
    sid, not opened by Claude Code, not in a temp directory (tmp_roots), with it or one of its children updated within
    OTHERS_WINDOW. Children fold into their parent (state rolled up); a child whose parent is not on the page stands
    alone, one whose parent is a tmux session is that session's. Threads only in snap.loaded are never listed.
    Order: most urgent state first, then newest."""
    if snap is None:
        return [], 0
    tmux = {r.sid for r in rows if getattr(r, "sid", None)}
    known = {t.id for t in snap.threads} | tmux
    kids = {}
    for t in snap.threads:
        if t.parent_id is not None and t.parent_id in known:
            kids.setdefault(t.parent_id, []).append(t)
    found = []
    for t in snap.threads:
        if t.parent_id is not None and t.parent_id in known:
            continue  # folded into its parent (or belongs to a tmux session)
        if t.id in tmux or t.originator == "Claude Code" or _under(t.cwd, tmp_roots):
            continue
        family = kids.get(t.id, [])
        updated = max([t.updated_at] + [c.updated_at for c in family])
        if now - updated > OTHERS_WINDOW:
            continue
        state, via = thread_state(t), None
        for c in sorted(family, key=lambda c: c.updated_at, reverse=True):
            cs = thread_state(c)
            if _rank(cs) < _rank(state):
                state, via = cs, c.name or c.id[:8]
        found.append(OtherRow(t, state, via, len(family), updated))
    found.sort(key=lambda o: (_rank(o.state), -o.when.timestamp()))
    return found[:OTHERS_MAX], max(0, len(found) - OTHERS_MAX)


def rel_time(dt, now):
    """Short age for the list column: <1m, 5m, 3h, 2d."""
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "<1m"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def _source(t):
    return "Desktop" if t.originator == "Codex Desktop" else "CLI"


def _state_text(o):
    return o.thread.status if o.state == "unknown" else STATE_LABEL.get(o.state, o.state)


def _title(t):
    return t.name or t.id[:8]


def other_cells(o, now, lead=""):
    """One block row's cells: name, source, folder (last part), coloured state, age. lead indents the name so it lines
    up with the tmux names (their number and mark)."""
    folder = os.path.basename(o.thread.cwd.rstrip("/")) or o.thread.cwd or "-"
    return (lead + " ".join(_title(o.thread).split()), _source(o.thread), folder,
            (_state_text(o), _STYLE.get(o.state, "muted")), rel_time(o.when, now))


def _line(name, value, style=""):
    return ((pad(name, 9), "label"), (value, style))


def _tilde(path, home):
    home = str(home) if home else ""
    if home and (path == home or path.startswith(home.rstrip("/") + "/")):
        return "~" + path[len(home.rstrip("/")):]
    return path


def other_card(o, now, *, home=None):
    """Info card of a block row (built only when the row is selected: the context is read from its rollout here)."""
    t = o.thread
    state = _state_text(o) + (f"（子對話：{o.via}）" if o.via else "")
    tokens, window = rollout_context(t.path) if t.path else (None, None)
    return [_title(t), "─" * 200,
            _line("資料夾", _tilde(t.cwd, home) or "-"),
            _line("來源", _source(t)),
            _line("狀態", state, _STYLE.get(o.state, "muted")),
            _line("context", _fmt_tokens(tokens, window)),
            _line("更新時間", f"{_fmt_time(o.when)}（{_ago(o.when, now)}）"),
            _line("子對話", str(o.children)),
            "", Keys(GO_DESKTOP)]


class ListNote(str):
    """A display-only line of the list (the block title, the overflow and daemon-down notes): never selectable,
    never numbered, drawn in style."""

    def __new__(cls, text, style=""):
        line = super().__new__(cls, text)
        line.style = style
        return line


class OwnCols(tuple):
    """A list row with its own column widths and name cap (a block row: its columns differ from the tmux rows')."""

    def __new__(cls, cells, cols=OTHER_COLS, cap=OTHER_NAME_W):
        row = super().__new__(cls, cells)
        row.cols, row.cap = cols, cap
        return row


def block_left(cells, extra, *, down, starting=False, failed=None):
    """The block's list lines in order: [title, row cells…, overflow note] — or [STARTING] while the daemon is being
    started, [START_FAILED] when that failed (failed = the reason), [DAEMON_DOWN] when down — or [] when there is no
    row. cells are the OwnCols of the OtherRows shown (already filtered by a search)."""
    if starting:
        return [ListNote(STARTING, "muted")]
    if failed:
        return [ListNote(START_FAILED.format(reason=failed), "warn")]
    if down:
        return [ListNote(DAEMON_DOWN, "muted")]
    if not cells:
        return []
    return [ListNote(SECTION_TITLE, "dim"), *cells] + ([ListNote(f"還有 {extra} 段", "muted")] if extra else [])
