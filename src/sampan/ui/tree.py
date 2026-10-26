"""The session list as a tree of folders (pure; no curses, no files): build_items turns status rows and a folders.Tree
into the (depth, item) list the Picker shows, item being a FolderItem or a StatusRow."""
from dataclasses import dataclass

from .. import folders as F
from ..restart import BUSY


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
        return "\x00folder:" + self.path


def folder_of(tree, name):
    """The folder path session name sits in; "" at the top level or without a tree."""
    return tree.sessions.get(name, "") if tree is not None else ""


def _by_name(rows):
    return sorted(rows, key=lambda r: r.name)


def build_items(rows, tree, query):
    """(depth, item) in display order. No tree (broken folders.json) or no folders: every session at depth 0.
    While searching (query not None): only the matching sessions, depth 0, no folders. Otherwise each level lists its
    subfolders first, then its sessions, both by name; a collapsed folder shows none of what is in it."""
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
            item = FolderItem(sub, depth, t.folders[sub], len(under), sum(r.agent_status in BUSY for r in under))
            out.append((depth, item))
            if not item.collapsed:
                level(sub, depth + 1)
        out.extend((depth, r) for r in _by_name(r for r in rows if home[r.name] == path))

    level("", 0)
    return out
