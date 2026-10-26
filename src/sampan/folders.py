"""Session folders: which folder each session sits in, kept apart from the registry (folders.json).

Only the display layer reads this. `load` raises FoldersError for a file it cannot trust and nothing here ever writes
over such a file; `resolve` makes a tree agree with the sessions that really exist. Operations are pure edits of a
Tree, applied atomically (a failed operation leaves the tree as it was); `edit` wraps load, operation and save in the
registry lock.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import registry
from .fsutil import PRIVATE_FILE, atomic_write

FOLDERS_FILE = "folders.json"
VERSION = 1
Item = tuple[str, str]  # ("session", name) or ("folder", path)


class FoldersError(Exception):
    pass


class NameTaken(FoldersError):
    """The target level already has a folder called name (retry with merge=True to merge into it)."""

    def __init__(self, name):
        super().__init__(f"同一層已經有「{name}」")
        self.name = name


@dataclass
class Tree:
    folders: dict[str, bool] = field(default_factory=dict)  # full path -> collapsed
    sessions: dict[str, str] = field(default_factory=dict)  # session name -> folder path


def name_problem(name):
    """Why name cannot be a single folder name, or None."""
    if not name.strip():
        return "名稱不能是空的"
    if "/" in name:
        return "名稱不能含 /"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "名稱不能含控制字元"
    return None


def parent(path):
    return path.rpartition("/")[0]


def basename(path):
    return path.rpartition("/")[2]


def join(parent_path, name):
    return f"{parent_path}/{name}" if parent_path else name


def _inside(path, ancestor):
    """path is ancestor or below it. The top level (ancestor "") contains everything."""
    return not ancestor or path == ancestor or path.startswith(ancestor + "/")


def _levels(path):
    """'a/b/c' -> ['a', 'a/b', 'a/b/c']"""
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _copy(tree):
    return Tree(dict(tree.folders), dict(tree.sessions))


def _assign(tree, other):
    tree.folders, tree.sessions = other.folders, other.sessions


def _unreadable(why):
    return FoldersError(f"資料夾設定讀不到：{why}")


def _check_path(file, p):
    for segment in p.split("/"):
        problem = name_problem(segment)
        if problem:
            raise _unreadable(f"{file} 裡的路徑「{p}」不對：{problem}")


def load(state_dir):
    path = Path(state_dir) / FOLDERS_FILE
    if not path.exists():
        return Tree()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:  # ValueError covers bad JSON and bad UTF-8
        raise _unreadable(f"{path}（{e}）") from e
    if not isinstance(data, dict):
        raise _unreadable(f"{path} 的內容不是物件")
    if data.get("version") != VERSION:
        raise _unreadable(f"{path} 是未知的版本 {data.get('version')!r}（只認得 {VERSION}）。不會覆蓋它")
    raw_folders, raw_sessions = data.get("folders"), data.get("sessions")
    if not isinstance(raw_folders, dict) or not isinstance(raw_sessions, dict):
        raise _unreadable(f"{path} 的 folders 或 sessions 格式不對")
    tree = Tree()
    for p, meta in raw_folders.items():
        if not isinstance(meta, dict) or not isinstance(meta.get("collapsed", False), bool):
            raise _unreadable(f"{path} 的資料夾「{p}」格式不對")
        _check_path(path, p)
        tree.folders[p] = meta.get("collapsed", False)
    for name, p in raw_sessions.items():
        if not isinstance(p, str):
            raise _unreadable(f"{path} 的 session「{name}」格式不對")
        _check_path(path, p)
        tree.sessions[name] = p
    return tree


def save(state_dir, tree):
    data = {"version": VERSION,
            "folders": {p: {"collapsed": c} for p, c in tree.folders.items()},
            "sessions": tree.sessions}
    atomic_write(Path(state_dir) / FOLDERS_FILE, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                 mode=PRIVATE_FILE)


def resolve(tree, names):
    """A new tree that matches the sessions that exist: unknown sessions dropped, missing folders added."""
    names = set(names)
    out = Tree(dict(tree.folders), {n: p for n, p in tree.sessions.items() if n in names and p})
    for p in list(out.folders) + list(out.sessions.values()):
        for level in _levels(p):
            out.folders.setdefault(level, False)
    return out


def edit(state_dir, fn):
    """load, fn(tree) (edits in place), save, under the registry lock. Nothing is written if load or fn raises."""
    with registry.lock(state_dir):
        tree = load(state_dir)
        result = fn(tree)
        save(state_dir, tree)
        return result


def _ensure(tree, path):
    for level in _levels(path):
        tree.folders.setdefault(level, False)


def add(tree, path):
    for segment in path.split("/"):
        problem = name_problem(segment)
        if problem:
            raise FoldersError(f"資料夾名稱不能用「{path}」：{problem}")
    _ensure(tree, path)


def _relocate(tree, src, dst, merge):
    """Move the folder src and everything under it to dst, in place. An existing dst takes the contents and keeps its own
    collapsed state (merge must be allowed). dst must not be inside src."""
    if src == dst:
        return
    if dst in tree.folders and not merge:
        raise NameTaken(basename(dst))

    def new(p):
        return dst + p[len(src):] if _inside(p, src) else p

    folders = {p: c for p, c in tree.folders.items() if not _inside(p, src)}
    for p, c in tree.folders.items():
        if _inside(p, src):
            folders.setdefault(new(p), c)
    tree.folders = folders
    tree.sessions = {n: new(p) for n, p in tree.sessions.items()}
    _ensure(tree, dst)


def _require(tree, path):
    if path not in tree.folders:
        raise FoldersError(f"沒有「{path}」這個資料夾")


def rename(tree, path, new_name, *, merge=False):
    problem = name_problem(new_name)
    if problem:
        raise FoldersError(f"資料夾名稱不能用「{new_name}」：{problem}")
    _require(tree, path)
    dst = join(parent(path), new_name)
    work = _copy(tree)
    _relocate(work, path, dst, merge)
    _assign(tree, work)
    return dst


def remove(tree, path):
    """Delete the folder; what is in it moves up one level (a same-named child merges into the existing sibling)."""
    _require(tree, path)
    up = parent(path)
    work = _copy(tree)
    children = [p for p in work.folders if parent(p) == path]
    for n, p in list(work.sessions.items()):
        if p == path:
            place_session(work, n, up)
    del work.folders[path]  # before relocating: a child named like path may merge into path's own place
    for child in children:
        _relocate(work, child, join(up, basename(child)), True)
    _assign(tree, work)


def top_items(tree, items):
    """Drop duplicates and every item that lies inside a folder that is also in items: only the top ones move."""
    folder_paths = {p for kind, p in items if kind == "folder"}
    out = []
    for item in items:
        kind, key = item
        where = key if kind == "folder" else tree.sessions.get(key, "")
        if any(_inside(where, f) and (kind == "session" or where != f) for f in folder_paths):
            continue
        if item not in out:
            out.append(item)
    return out


def _move(tree, items, dest, merge):
    """move() on a tree that may be left half-done on error."""
    if dest:
        add(tree, dest)
    for kind, key in items:
        if kind == "folder":
            _require(tree, key)
            if _inside(dest, key):
                raise FoldersError("不能把資料夾搬進它自己裡面")
            _relocate(tree, key, join(dest, basename(key)), merge)
        else:
            place_session(tree, key, dest)


def move(tree, items, dest, *, merge=False):
    items = top_items(tree, items)
    work = _copy(tree)
    _move(work, items, dest, merge)
    _assign(tree, work)


def group(tree, items, name, *, merge=False):
    """Put items into a new folder called name, next to them (at their deepest common parent). Returns its path."""
    problem = name_problem(name)
    if problem:
        raise FoldersError(f"資料夾名稱不能用「{name}」：{problem}")
    items = top_items(tree, items)
    if not items:
        raise FoldersError("沒有要放進資料夾的項目")
    homes = [parent(key) if kind == "folder" else tree.sessions.get(key, "") for kind, key in items]
    common = ""
    for level in reversed(_levels(homes[0]) if homes[0] else []):
        if all(_inside(h, level) for h in homes):
            common = level
            break
    dst = join(common, name)
    if dst in tree.folders and not merge:
        raise NameTaken(name)
    work = _copy(tree)
    _move(work, items, dst, merge)
    _assign(tree, work)
    return dst


def set_collapsed(tree, path, collapsed):
    _require(tree, path)
    tree.folders[path] = collapsed


def place_session(tree, name, path):
    if path:
        add(tree, path)
        tree.sessions[name] = path
    else:
        tree.sessions.pop(name, None)


def rename_session(tree, old, new):
    if old in tree.sessions:
        tree.sessions[new] = tree.sessions.pop(old)


def forget_session(tree, name):
    tree.sessions.pop(name, None)


def sessions_under(tree, path):
    return {n for n, p in tree.sessions.items() if _inside(p, path)}
