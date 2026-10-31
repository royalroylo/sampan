"""Session folders: which folder each session sits in, kept apart from the registry (folders.json).

On disk (version 2) a folder has an id and a parent folder id, and a session belongs to a folder by its sampan session id
(Row.id), so renaming a session or a folder never loses the membership. In memory the Tree still speaks paths and session
names; `load` and `save` translate with the registry rows. Version 1 files (path -> name) are still read.

Only the display layer reads this. `load` raises FoldersError for a file it cannot trust and nothing here ever writes
over such a file, and adds every folder a path implies (「客戶/甲」 makes 「客戶」 real); `resolve` makes a tree agree with the sessions that really exist. Operations are pure edits of a
Tree, applied atomically (a failed operation leaves the tree as it was); `edit` wraps load, operation and save in the
registry lock.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import ids as idmod
from . import registry
from .fsutil import PRIVATE_FILE, atomic_write

FOLDERS_FILE = "folders.json"
VERSION = 2
READABLE = (1, 2)
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
    ids: dict[str, str] = field(default_factory=dict, compare=False)  # folder path -> folder id (kept across renames)


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
    return Tree(dict(tree.folders), dict(tree.sessions), dict(tree.ids))


def _assign(tree, other):
    tree.folders, tree.sessions, tree.ids = other.folders, other.sessions, other.ids


def _fill(tree):
    """Every folder has an id; ids of folders that are gone are dropped."""
    for p in tree.folders:
        tree.ids.setdefault(p, idmod.new_id())
    for p in [p for p in tree.ids if p not in tree.folders]:
        del tree.ids[p]


def _unreadable(why):
    return FoldersError(f"資料夾設定讀不到：{why}")


def _check_path(file, p):
    for segment in p.split("/"):
        problem = name_problem(segment)
        if problem:
            raise _unreadable(f"{file} 裡的路徑「{p}」不對：{problem}")


def _registry_rows(state_dir):
    try:
        return registry.load_rows(Path(state_dir) / "registry.json")
    except registry.RegistryError as e:
        raise _unreadable(f"登記表讀不到（{e}）") from e


def _load_v1(path, raw_folders, raw_sessions):
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


def _load_v2(path, raw_folders, raw_sessions, rows):
    for fid, meta in raw_folders.items():
        if (not isinstance(meta, dict) or not isinstance(meta.get("name"), str)
                or not isinstance(meta.get("collapsed", False), bool)
                or not (meta.get("parent") is None or meta["parent"] in raw_folders)):
            raise _unreadable(f"{path} 的資料夾 {fid} 格式不對")
        problem = name_problem(meta["name"])
        if problem:
            raise _unreadable(f"{path} 的資料夾 {fid}「{meta['name']}」不對：{problem}")
    paths = {}

    def path_of(fid):
        if fid in paths:
            return paths[fid]
        chain, cur = [], fid
        while cur is not None:
            if cur in chain:
                raise _unreadable(f"{path} 的資料夾 {fid} 繞成一圈")
            chain.append(cur)
            cur = raw_folders[cur].get("parent")
        text = "/".join(raw_folders[c]["name"] for c in reversed(chain))
        paths[fid] = text
        return text

    tree = Tree()
    for fid, meta in raw_folders.items():
        p = path_of(fid)
        if p in tree.ids:
            raise _unreadable(f"{path} 的資料夾「{p}」重複了")
        tree.folders[p] = meta.get("collapsed", False)
        tree.ids[p] = fid
    if raw_sessions:
        if rows is None:
            rows = _registry_rows(Path(path).parent)
        names = {r.id: r.tmux for r in rows if r.id}
        for sid, fid in raw_sessions.items():
            if not isinstance(fid, str) or fid not in raw_folders:
                raise _unreadable(f"{path} 的 session {sid} 格式不對")
            if sid in names:  # a session that no longer exists is dropped
                tree.sessions[names[sid]] = path_of(fid)
    return tree


def load(state_dir, rows=None):
    """The tree in state_dir/folders.json. rows (registry rows) turn session ids back into names; without them the
    registry is read (read only, no lock) when the file needs it."""
    path = Path(state_dir) / FOLDERS_FILE
    if not path.exists():
        return Tree()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:  # ValueError covers bad JSON and bad UTF-8
        raise _unreadable(f"{path}（{e}）") from e
    if not isinstance(data, dict):
        raise _unreadable(f"{path} 的內容不是物件")
    if data.get("version") not in READABLE:
        raise _unreadable(f"{path} 是未知的版本 {data.get('version')!r}（只認得 1、2）。不會覆蓋它")
    raw_folders, raw_sessions = data.get("folders"), data.get("sessions")
    if not isinstance(raw_folders, dict) or not isinstance(raw_sessions, dict):
        raise _unreadable(f"{path} 的 folders 或 sessions 格式不對")
    if data["version"] == 1:
        tree = _load_v1(path, raw_folders, raw_sessions)
    else:
        tree = _load_v2(path, raw_folders, raw_sessions, rows)
    for p in list(tree.folders) + list(tree.sessions.values()):  # 「客戶/甲」 implies 「客戶」: make it real
        _ensure(tree, p)
    _fill(tree)
    return tree


def save(state_dir, tree, rows=None):
    """Write version 2, sessions keyed by Row.id (a session not in rows is not written). While a member session has no
    id yet (a registry from before 0.15) the version 1 format is written unchanged, so no membership is lost."""
    path = Path(state_dir) / FOLDERS_FILE
    if rows is None:
        rows = _registry_rows(state_dir)
    by_name = {r.tmux: r for r in rows}
    if any(n in by_name and not by_name[n].id for n in tree.sessions):
        data = {"version": 1,
                "folders": {p: {"collapsed": c} for p, c in tree.folders.items()},
                "sessions": tree.sessions}
    else:
        _fill(tree)
        data = {"version": VERSION,
                "folders": {tree.ids[p]: {"name": basename(p), "parent": tree.ids.get(parent(p)) if parent(p) else None,
                                          "collapsed": c} for p, c in tree.folders.items()},
                "sessions": {by_name[n].id: tree.ids[p] for n, p in tree.sessions.items()
                             if n in by_name and p and p in tree.ids}}
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", mode=PRIVATE_FILE)


def resolve(tree, names):
    """A new tree that matches the sessions that exist: unknown sessions dropped, missing folders added."""
    names = set(names)
    out = Tree(dict(tree.folders), {n: p for n, p in tree.sessions.items() if n in names and p}, dict(tree.ids))
    for p in list(out.folders) + list(out.sessions.values()):
        for level in _levels(p):
            out.folders.setdefault(level, False)
    _fill(out)
    return out


def edit(state_dir, fn):
    """load, fn(tree) (edits in place), save, under the registry lock. Nothing is written if load or fn raises."""
    with registry.lock(state_dir):
        rows = _registry_rows(state_dir)  # read directly: the lock is already held
        tree = load(state_dir, rows)
        result = fn(tree)
        save(state_dir, tree, rows)
        return result


def _ensure(tree, path):
    for level in _levels(path):
        if level not in tree.folders:
            tree.folders[level] = False
            tree.ids.setdefault(level, idmod.new_id())


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
    _fill(tree)

    def new(p):
        return dst + p[len(src):] if _inside(p, src) else p

    folders = {p: c for p, c in tree.folders.items() if not _inside(p, src)}
    for p, c in tree.folders.items():
        if _inside(p, src):
            folders.setdefault(new(p), c)
    moved = {p: i for p, i in tree.ids.items() if not _inside(p, src)}
    for p, i in tree.ids.items():
        if _inside(p, src):
            moved.setdefault(new(p), i)  # an existing dst keeps its own id
    tree.folders = folders
    tree.ids = moved
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
    work.ids.pop(path, None)
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
