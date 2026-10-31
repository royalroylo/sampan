"""sampan folder: list and edit the session folders (folders.json). Never reads a session's state and never writes the
registry; the only thing read from it is which sessions exist."""
from . import folders, ids, registry
from .folders import FoldersError

BROKEN_HINT = "資料夾設定壞了，修好或刪掉 folders.json 再試"
USAGE = {
    "list": "sampan folder list",
    "add": "sampan folder add <路徑>",
    "rename": "sampan folder rename <路徑> <新名稱> [--merge]",
    "rm": "sampan folder rm <路徑>",
    "move": "sampan folder move <名稱或路徑>... <目的|/> [--merge]",
}


def _path(text):
    return text.strip().strip("/")


def _session_rows(cfg, out):
    try:
        return registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except registry.RegistryError as e:
        out(f"❌ {e}")
        return None


def _session_names(cfg, out):
    rows = _session_rows(cfg, out)
    return None if rows is None else [r.tmux for r in rows]


def _render(tree, names):
    """Lines of the tree: folders first, then sessions, two spaces per level."""
    lines = []

    def walk(parent_path, depth):
        pad = "  " * depth
        for p in sorted(p for p in tree.folders if folders.parent(p) == parent_path):
            lines.append(f"{pad}{'▸' if tree.folders[p] else '▾'} {folders.basename(p)}/")
            walk(p, depth + 1)
        for n in sorted(n for n in names if tree.sessions.get(n, "") == parent_path):
            lines.append(f"{pad}{n}")

    walk("", 0)
    return lines


def _list(cfg, out):
    names = _session_names(cfg, out)
    if names is None:
        return 2
    if not names:
        out("目前沒有任何 session")
        return 0
    try:
        tree = folders.resolve(folders.load(cfg.state_dir), names)
    except FoldersError as e:
        out(f"⚠ {e}；先平坦列出全部 session。{BROKEN_HINT}")
        tree = folders.Tree()
    for line in _render(tree, names):
        out(line)
    return 0


def cmd_folder(cfg, out, action, args, *, merge=False):
    """action: list|add|rename|rm|move. 0 ok, 2 refused or bad usage."""
    if action not in USAGE:
        out(f"❌ 不認得 folder {action}（可用：{'、'.join(USAGE)}）")
        return 2
    want = {"list": (0, 0), "add": (1, 1), "rename": (2, 2), "rm": (1, 1), "move": (2, None)}[action]
    if len(args) < want[0] or (want[1] is not None and len(args) > want[1]):
        out(f"❌ 用法：{USAGE[action]}")
        return 2
    if action == "list":
        return _list(cfg, out)
    try:
        folders.load(cfg.state_dir)  # a file that cannot be trusted is never written over
    except FoldersError as e:
        out(f"❌ {e}")
        out(f"❌ {BROKEN_HINT}")
        return 2
    rows = None
    if action == "move":
        rows = _session_rows(cfg, out)
        if rows is None:
            return 2

    def apply(tree):
        if action == "add":
            folders.add(tree, _path(args[0]))
        elif action == "rename":
            folders.rename(tree, _path(args[0]), args[1].strip(), merge=merge)
        elif action == "rm":
            folders.remove(tree, _path(args[0]))
        else:
            dest = _path(args[-1])
            items = []
            for a in args[:-1]:
                p = _path(a)
                if p in tree.folders:
                    items.append(("folder", p))
                else:
                    r = ids.resolve(rows, a)  # a session by name, id or unique id prefix
                    if isinstance(r, str):
                        raise FoldersError(r if "對到多個" in r else f"找不到 {a}")
                    items.append(("session", r.tmux))
            folders.move(tree, items, dest, merge=merge)

    try:
        folders.edit(cfg.state_dir, apply)
    except FoldersError as e:
        out(f"❌ {e}")
        return 2
    except OSError as e:
        out(f"❌ 資料夾設定寫不進去：{e}")
        return 2
    out("✔ 完成")
    return 0


def sync_session(cfg, out, op, name, new=None):
    """Keep folders.json in step with a rename or forget of a session. Never raises: a failure is only a warning."""
    try:
        if not (cfg.state_dir / folders.FOLDERS_FILE).exists():
            return
        if op == "rename":
            folders.edit(cfg.state_dir, lambda t: folders.rename_session(t, name, new))
        else:
            folders.edit(cfg.state_dir, lambda t: folders.forget_session(t, name))
    except Exception as e:
        out(f"⚠ 資料夾設定沒同步：{e}")
