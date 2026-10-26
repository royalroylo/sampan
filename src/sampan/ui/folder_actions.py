"""Folder actions of the ui (g, f, e / X on a folder row, collapse): ask, then edit folders.json under the registry
lock. They never stop, forget or change a session and never write the registry. ask(prompt) returns the stripped
answer, or None when cancelled (actions._ask). Every message is also written to actions.log (「資料夾」)."""
from .. import actionlog
from .. import folders as F
from ..folder_cmd import BROKEN_HINT
from .tree import FolderItem, build_items, folder_of

TOP = "（最外層）"
NEW = "新資料夾…"
LABEL = "資料夾"


def items_of(names):
    """The Picker's "folder:<path>" / "session:<name>" strings as folders items."""
    return [tuple(n.split(":", 1)) for n in names]


def _readable(cfg, out):
    """False (after saying so) when folders.json cannot be trusted: nothing is asked and nothing is written."""
    try:
        F.load(cfg.state_dir)
    except F.FoldersError:
        out(f"❌ {BROKEN_HINT}")
        return False
    return True


def _complete(tree):
    """Make every folder a session points at exist (load leaves implied folders out; the screen shows them)."""
    for p in set(tree.sessions.values()):
        F.add(tree, p)


def _edit(cfg, out, ask, op):
    """F.edit with op(tree, merge). A taken name asks once whether to merge. Returns (rc, op's result or None)."""
    merge = False
    while True:
        try:
            return 0, F.edit(cfg.state_dir, lambda t: (_complete(t), op(t, merge))[1])
        except F.NameTaken as e:
            if merge or (ask(f"同一層已經有「{e.name}」，合併進去？(y/N): ") or "").lower() != "y":
                out("已取消")
                return 0, None
            merge = True
        except F.FoldersError as e:
            out(f"❌ {e}")
            return 1, None
        except OSError as e:
            out(f"❌ 資料夾設定寫不進去：{e}")
            return 1, None


def _name(out, ask, prompt):
    """A folder name typed in, or None (cancelled, or refused after saying why)."""
    name = ask(prompt)
    if not name:
        out("已取消")
        return None
    problem = F.name_problem(name)
    if problem:
        out(f"❌ {problem}")
        return None
    return name


def save_collapsed(cfg, path, collapsed):
    """Remember a folder's collapsed state. Silent on any failure: only the memory of it is lost."""
    try:
        F.edit(cfg.state_dir, lambda t: (F.add(t, path), F.set_collapsed(t, path, collapsed)))
    except Exception:  # noqa: BLE001  a broken or locked file must never disturb the list
        pass


def group_flow(cfg, out, ask, names):
    """g: put the items into a new folder next to them."""
    out = actionlog.tee(cfg, LABEL, out)
    if not _readable(cfg, out):
        return 1
    name = _name(out, ask, "新資料夾名稱：")
    if name is None:
        return 0
    items = items_of(names)
    rc, res = _edit(cfg, out, ask, lambda t, merge: (len(F.top_items(t, items)),
                                                     F.group(t, items, name, merge=merge)))
    if res is not None:
        out(f"✔ 已建立資料夾 {res[1]}（{res[0]} 項）")
    return rc


def destinations(cfg, rows, names):
    """f's choices: the top level, a new folder, then every folder by name except the moved ones and what is in them."""
    tree = F.resolve(F.load(cfg.state_dir), [r.name for r in rows])
    moved = [key for kind, key in items_of(names) if kind == "folder"]
    keep = [p for p in sorted(tree.folders) if not any(p == m or p.startswith(m + "/") for m in moved)]
    return [TOP, NEW] + keep


def _choose_plain(out, ask, labels):
    for i, label in enumerate(labels, 1):
        out(f"  [{i}] {label}")
    raw = ask("搬到哪裡？(編號，Enter 取消): ")
    if not raw:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= len(labels):
        out(f"無效編號：{raw}")
        return None
    return int(raw) - 1


def file_flow(cfg, out, ask, names, rows, choose_fn=None):
    """f: move the items into a folder picked from destinations() (choose_fn(labels) -> index or None; None =
    numbered in text)."""
    out = actionlog.tee(cfg, LABEL, out)
    if not _readable(cfg, out):
        return 1
    labels = destinations(cfg, rows, names)
    i = choose_fn(labels) if choose_fn is not None else _choose_plain(out, ask, labels)
    if i is None:
        out("已取消")
        return 0
    if i == 0:
        dest = ""
    elif i == 1:
        dest = _name(out, ask, "新資料夾名稱：")
        if dest is None:
            return 0
    else:
        dest = labels[i]
    items = items_of(names)
    rc, res = _edit(cfg, out, ask, lambda t, merge: F.move(t, items, dest, merge=merge) or True)
    if res:
        out(f"✔ 已搬到 {dest or '最外層'}")
    return rc


def rename_flow(cfg, out, ask, path):
    """e on a folder row."""
    out = actionlog.tee(cfg, LABEL, out)
    if not _readable(cfg, out):
        return 1
    name = _name(out, ask, "新名稱：")
    if name is None:
        return 0
    rc, dst = _edit(cfg, out, ask, lambda t, merge: F.rename(t, path, name, merge=merge))
    if dst is not None:
        out(f"✔ 已改名：{path} → {dst}")
    return rc


def remove_flow(cfg, out, ask, path):
    """X on a folder row: what is in it moves up one level; no session is touched."""
    out = actionlog.tee(cfg, LABEL, out)
    if not _readable(cfg, out):
        return 1
    if (ask(f"刪除資料夾 {path}？裡面的 session 會往上移一層，不會停掉 (y/N): ") or "").lower() != "y":
        out("已取消")
        return 0
    rc, res = _edit(cfg, out, ask, lambda t, merge: F.remove(t, path) or True)
    if res:
        out(f"✔ 已刪除資料夾 {path}")
    return rc


def plain_order(rows, tree):
    """--plain: (rows in the tree's order with every folder open, {name: "folder path · "}). No tree: as given."""
    if tree is None:
        return list(rows), {}
    t = F.resolve(tree, [r.name for r in rows])
    if not t.folders:
        return list(rows), {}
    opened = F.Tree({p: False for p in t.folders}, dict(t.sessions))
    ordered = [it for _, it in build_items(rows, opened, None) if not isinstance(it, FolderItem)]
    return ordered, {r.name: f"{folder_of(t, r.name)} · " for r in ordered if folder_of(t, r.name)}
