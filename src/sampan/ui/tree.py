"""The session list as a tree of folders (pure; no curses, no files): build_items turns status rows and a folders.Tree
into the (depth, item) list the Picker shows, item being a FolderItem or a StatusRow."""
from dataclasses import dataclass

from .. import folders as F
from ..restart import BUSY
from ..status import State

FILTERS = ("全部", "閒置", "忙碌", "已停止", "異常")  # Tab order of the main list's state filter
ODD = {State.PROFILE_MISMATCH, State.AGENT_MISMATCH, State.UNREGISTERED, State.NO_AGENT, State.MULTI, State.UNKNOWN,
       State.AWAIT_TRUST}


def matches(row, kind):
    """Whether a status row is listed under filter kind. 閒置 is what S restarts (running and idle); retired rows are
    only under 全部."""
    if kind == "閒置":
        return row.state is State.RUNNING and row.agent_status == "idle"
    if kind == "忙碌":
        return row.state is State.RUNNING and row.agent_status != "idle"
    if kind == "已停止":
        return row.state is State.STOPPED
    if kind == "異常":
        return row.state in ODD
    return True


@dataclass(frozen=True)
class FolderItem:
    path: str
    depth: int
    collapsed: bool
    total: int  # sessions below it, all levels
    busy: int  # of those, the ones in the middle of something

    @property
    def name(self):
        """Picker key only (never shown): cannot clash with a session name."""
        return folder_key(self.path)


def folder_key(path):
    """The Picker key of the folder row for path (FolderItem.name)."""
    return "\x00folder:" + path


def folder_of(tree, name):
    """The folder path session name sits in; "" at the top level or without a tree."""
    return tree.sessions.get(name, "") if tree is not None else ""


def _by_name(rows):
    return sorted(rows, key=lambda r: r.name)


def build_items(rows, tree, query, *, hide_empty=False):
    """(depth, item) in display order. No tree (broken folders.json) or no folders: every session at depth 0.
    While searching (query not None): only the matching sessions, depth 0, no folders. Otherwise each level lists its
    subfolders first, then its sessions, both by name; a collapsed folder shows none of what is in it. hide_empty
    (a state filter is on) leaves out folders with none of rows under them."""
    if query is not None:
        q = query.lower()
        return [(0, r) for r in _by_name(rows) if q in r.name.lower()]
    if tree is None:
        return [(0, r) for r in _by_name(rows)]
    t = F.resolve(tree, [r.name for r in rows])
    if not t.folders:
        return [(0, r) for r in _by_name(rows)]
    home = {r.name: folder_of(t, r.name) for r in rows}
    out = []

    def level(path, depth):
        for sub in sorted(p for p in t.folders if F.parent(p) == path):
            inside = F.sessions_under(t, sub)
            under = [r for r in rows if r.name in inside]
            if hide_empty and not under:
                continue
            item = FolderItem(sub, depth, t.folders[sub], len(under), sum(r.agent_status in BUSY for r in under))
            out.append((depth, item))
            if not item.collapsed:
                level(sub, depth + 1)
        out.extend((depth, r) for r in _by_name(r for r in rows if home[r.name] == path))

    level("", 0)
    return out
