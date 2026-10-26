"""Pure state machines of the ui: Picker (arrow-key menu), Checklist (the restore/adopt checklist), and the display conversion of state labels. Never touches curses."""
import time

from .. import folders as F
from ..restart import confirm_line, plan_restart, skip_text, stop_line
from ..status import State, _fmt_tokens
from .text import BACKSPACE, ENTER, MOVES, pad
from .tree import FILTERS, FolderItem, build_items, folder_key, folder_of, matches


def show_state(row):
    """State label on the info card / text list: STOPPED is shown as "已停止" ("stopped"), others are LABELS + detail."""
    return "已停止" if row.state is State.STOPPED else row.display


def list_state(row):
    """Short state for the list column: the explanation in parentheses is left to the info card."""
    if row.state is State.PROFILE_MISMATCH:
        return "prof 不符"
    return show_state(row).split("（")[0]


def row_text(r):
    profile = "-" if r.agent == "codex" else (r.profile or "-")
    return (f"{pad(r.name, 38)} {pad(r.agent, 6)} {pad(profile, 12)} {pad(show_state(r), 22)} "
            f"{pad(r.agent_status, 7)} {_fmt_tokens(r.tokens, r.context_window)}")


JUMP_S = 1.0  # digit jump: a digit typed more than this many seconds after the previous one starts a new number


class Picker:
    """State of the arrow-key menu; key() returns (action, list of names) or None (keep choosing).
    s does not return immediately: it first sets prompt to a confirmation question, and on the next key y/Y returns ("stop!", name) (confirmed), any other key cancels.
    Digits jump to that row number (jump holds the digits typed so far); / starts a search (query is the text, None when not
    searching) and every key is text until Esc, Enter or Backspace on an empty query. visible is the list shown (filtered while
    query is non-empty) and cursor indexes it.
    others (the main list's「Codex 其他對話」, codex_others.OtherRow) follow the rows in visible: the cursor moves onto
    them and a search matches their thread name, but digits count only rows, a space does not mark them, and every
    row action key there (Enter s m P e X r a) returns ("desktop", []) — they are read only.

    tree (a folders.Tree; None = no folders or folders.json unreadable) turns the list into folders: items is
    tree.build_items' (depth, item) list and visible[:n_rows] its items, FolderItem rows included. The Picker keeps its
    own copy of the tree: collapsing (Enter, →, ←) changes it at once and returns ("collapse", [path, "1"|"0"]) for the
    caller to save. Folders are marked by path in marked_folders. s / S act on every session under a target folder
    (collapsed ones too); S asks with restart.confirm_line (a stopped session is resumed only when its own row is a
    target), keeps the plan in restart_plan and on y/Y returns
    ("restart!", names). g / f return the marked items (else the cursor item) as "folder:<path>" / "session:<name>".
    Without a tree the list is rows in the given order, as before folders existed.
    Tab / Shift+Tab (KEY_BTAB) step the state filter through tree.FILTERS (filter indexes it, 0 = 全部): only the
    matching rows are listed (a search narrows them further), folders with none are left out, others only show under
    全部, and marks (and what a folder row targets) never reach a session the filter hides. * marks every session row
    shown (not those in collapsed folders); when they all are already marked it clears every mark instead.
    width (None = unlimited) is how many cells the prompt row has: the s / S questions shorten their name lists to fit
    it, so 確定？(y/N) always shows; what y returns always carries every name."""

    def __init__(self, rows, current=None, *, clock=time.monotonic, others=(), tree=None, own_pane=None):
        self.rows = rows
        self.own_pane = own_pane  # sampan's own pane id: S lists that session as skipped
        self.others = list(others)
        self.tree = _copy_tree(tree)
        self.marked = set()
        self.marked_folders = set()
        self.clock = clock
        self._stop = None
        self._restart = False
        self.restart_plan = None
        self.width = None  # cells the prompt row has (set by the screen before each draw): confirm lines fit it
        self._notice = None
        self.query = None
        self.jump = ""
        self._jump_at = None
        self.filter = 0
        self._reveal(current)
        self._filter()
        names = [r.name for r in self.visible]
        self.cursor = names.index(current) if current in names else 0

    @property
    def prompt(self):
        if self._stop is not None:
            return stop_line(self._stop, width=self.width)
        if self._restart:
            return confirm_line(self.restart_plan, width=self.width)
        if self._notice:
            return self._notice
        if self.query is not None:
            return f"/{self.query}▏（{len(self.visible)} 筆，Enter 進入，Esc 取消）"
        self._expire()
        return f"跳到第 {self.jump} 列" if self.jump else None

    def _expire(self):
        """Drop a digit buffer older than JUMP_S."""
        if self.jump and (self._jump_at is None or self.clock() - self._jump_at > JUMP_S):
            self.jump = ""

    @property
    def filter_name(self):
        return FILTERS[self.filter]

    def shown(self):
        """The rows the state filter lets through, in rows order."""
        return [r for r in self.rows if matches(r, self.filter_name)]

    @property
    def filter_count(self):
        return len(self.shown())

    def base_items(self):
        """items as they are without a search (row numbers follow these)."""
        rows = self.shown()
        return [(0, r) for r in rows] if self.tree is None else build_items(rows, self.tree, None,
                                                                             hide_empty=self.filter != 0)

    def _filter(self):
        q = (self.query or "").lower()
        rows = self.shown()
        if self.tree is None:
            self.items = [(0, r) for r in rows if q in r.name.lower()]
        else:
            self.items = build_items(rows, self.tree, self.query, hide_empty=self.filter != 0)
        self.visible = [it for _, it in self.items]
        self.n_rows = len(self.visible)  # visible[n_rows:] are others
        if self.filter == 0:
            self.visible += [o for o in self.others if q in o.thread.name.lower()]

    def on_other(self):
        """The cursor is on one of others."""
        return self.n_rows <= self.cursor < len(self.visible)

    def on_folder(self):
        """The cursor is on a folder row."""
        return bool(self.visible) and not self.on_other() and isinstance(self.visible[self.cursor], FolderItem)

    def here_folder(self):
        """Path of the folder row under the cursor, or None."""
        return self.visible[self.cursor].path if self.on_folder() else None

    def here_parent(self):
        """Where n puts a new session: the folder under the cursor, or the folder of the session under it ("" = top)."""
        if not self.visible or self.on_other():
            return ""
        return self.here_folder() or folder_of(self.tree, self._here())

    def clear_marks(self):
        self.marked.clear()
        self.marked_folders.clear()

    def set_tree(self, tree):
        """A new folder tree (after a folder action): rebuild the list, the cursor stays on the same item."""
        here = self._here_or_none()
        self.tree = _copy_tree(tree)
        self._prune()
        self._filter()
        self._land(here)

    def _prune(self):
        shown = {r.name for r in self.shown()}
        self.marked &= shown
        if self.tree is None:
            self.marked_folders.clear()
        else:
            self.marked_folders &= set(F.resolve(self.tree, [r.name for r in self.rows]).folders)
            if self.filter:
                self.marked_folders = {p for p in self.marked_folders if F.sessions_under(self.tree, p) & shown}

    def _land(self, here):
        names = [r.name for r in self.visible]
        if here in names:
            self.cursor = names.index(here)
        elif self.query is not None:
            self.cursor = 0
        else:
            self.cursor = max(0, min(self.cursor, len(self.visible) - 1))

    def _goto(self, name):
        """Put the cursor on the row called name in visible, or on the first row when it is not there."""
        names = [r.name for r in self.visible]
        self.cursor = names.index(name) if name in names else 0

    def _here(self):
        return self.visible[self.cursor].name

    def _here_or_none(self):
        return self._here() if self.visible else None

    def reload(self, rows, others=None):
        """New rows from the periodic refresh (others None = keep them): keep query, jump and checkmarks, and the cursor on the same session if it is still visible.
        When that session is gone: while searching the cursor goes to the first match, otherwise it keeps its index (clamped)."""
        here = self._here_or_none()
        self.rows = rows
        if others is not None:
            self.others = list(others)
        self._prune()
        self._filter()
        self._land(here)

    def _reveal(self, name):
        """Open every collapsed folder above session name in the Picker's own tree (not saved)."""
        if self.tree is None or name is None:
            return
        parts = folder_of(self.tree, name).split("/")
        for i in range(1, len(parts) + 1):
            level = "/".join(parts[:i])
            if self.tree.folders.get(level):
                self.tree.folders[level] = False

    def _start_search(self):
        """Search lists sessions only: a cursor on a folder row moves to the first match."""
        here = self._here_or_none()
        self.query = ""
        self._filter()
        names = [r.name for r in self.visible]
        self.cursor = names.index(here) if here in names else 0

    def _end_search(self):
        here = self._here_or_none()
        if self.visible and not self.on_other():
            self._reveal(here)
        self.query = None
        self._filter()
        self._goto(here)

    def _search_key(self, k):
        if k == "\x1b":
            self._end_search()
            return None
        if k in ENTER:
            if not self.visible:
                return None
            if self.on_other():
                return ("desktop", [])
            name = self._here()
            self._end_search()
            return ("attach", [name])
        if k in BACKSPACE:
            if not self.query:
                self._end_search()
                return None
            self.query = self.query[:-1]
        elif k in ("KEY_UP", "KEY_DOWN"):
            self.cursor = max(0, min(len(self.visible) - 1, self.cursor + MOVES[k]))
            return None
        elif len(k) == 1 and k.isprintable():
            self.query += k
        else:
            return None
        self._filter()
        self.cursor = 0
        return None

    def _digit(self, k):
        """Jump to the n-th session row shown (folder rows are not counted)."""
        self._jump_at = self.clock()
        at = [i for i in range(self.n_rows) if not isinstance(self.visible[i], FolderItem)]
        for buf in (self.jump + k, k):
            n = int(buf)
            if 1 <= n <= len(at):
                self.jump = "" if n * 10 > len(at) else buf
                self.cursor = at[n - 1]
                return
        self.jump = ""

    def _targets(self):
        """(session names, folder paths) a row action applies to: the marks, else the cursor item."""
        if self.marked or self.marked_folders:
            return set(self.marked), set(self.marked_folders)
        if self.on_folder():
            return set(), {self.here_folder()}
        return {self._here()}, set()

    def _names(self, sessions, paths):
        """The sessions themselves plus every session under the folders (collapsed or not), once each, in rows order."""
        names = set(sessions)
        for p in paths:
            names |= F.sessions_under(self.tree, p)
        return [r.name for r in self.shown() if r.name in names]

    def _as_items(self):
        """g / f items: the marked ones in display order (every folder open), else the cursor item."""
        if not (self.marked or self.marked_folders):
            return [f"folder:{self.here_folder()}" if self.on_folder() else f"session:{self._here()}"]
        tree = None if self.tree is None else F.Tree({p: False for p in self.tree.folders}, dict(self.tree.sessions))
        out = []
        for _, it in build_items(self.rows, tree, None):
            if isinstance(it, FolderItem):
                if it.path in self.marked_folders:
                    out.append(f"folder:{it.path}")
            elif it.name in self.marked:
                out.append(f"session:{it.name}")
        return out

    def _collapse(self, path, collapsed):
        here = self._here()
        self.tree.folders[path] = collapsed
        self._filter()
        self._goto(here)
        return ("collapse", [path, "1" if collapsed else "0"])

    def _goto_folder(self, path):
        """Cursor to the folder row of path (when there is one shown)."""
        if path:
            names = [r.name for r in self.visible[:self.n_rows]]
            key = folder_key(path)
            if key in names:
                self.cursor = names.index(key)

    def _on_folder_key(self, k):
        """Keys on a folder row that differ from a session row's; returns (handled, result)."""
        it = self.visible[self.cursor]
        if k in ENTER:
            return True, self._collapse(it.path, not it.collapsed)
        if k == "KEY_RIGHT":
            return True, (self._collapse(it.path, False) if it.collapsed else None)
        if k == "KEY_LEFT":
            if it.collapsed:
                self._goto_folder(F.parent(it.path))
                return True, None
            return True, self._collapse(it.path, True)
        if k == " ":
            self.marked_folders ^= {it.path}
            self.cursor = min(len(self.visible) - 1, self.cursor + 1)
            return True, None
        if k == "e":
            return True, ("rename_folder", [it.path])
        if k == "X":
            return True, ("rm_folder", [it.path])
        if len(k) == 1 and k in "mPra":
            return True, None
        return False, None

    def _step_filter(self, d):
        here = self._here_or_none()
        self.filter = (self.filter + d) % len(FILTERS)
        self._prune()
        self._filter()
        self._land(here)

    def _select_all(self):
        rows = [it.name for it in self.visible[:self.n_rows] if not isinstance(it, FolderItem)]
        if rows and not set(rows) <= self.marked:
            self.marked |= set(rows)
        else:
            self.clear_marks()

    def _ask_restart(self):
        sessions, paths = self._targets()
        # a stopped session is reattached only when its own row was targeted, never when reached through a folder
        plan = plan_restart(self.rows, self._names(sessions, paths), resume_stopped=sessions, own_pane=self.own_pane)
        if confirm_line(plan):
            self.restart_plan, self._restart = plan, True
            return
        self.restart_plan = None
        why = skip_text(plan.skip).removeprefix("略過：") if plan.skip else ""
        self._notice = f"沒有可以重開的：{why}" if why else "沒有可以重開的 session"

    def key(self, k):
        if self._stop is not None:
            names, self._stop = self._stop, None
            return ("stop!", names) if k in ("y", "Y") else None
        if self._restart:
            self._restart = False
            if k in ("y", "Y"):
                return ("restart!", self.restart_plan.restart + self.restart_plan.resume)
            self.restart_plan = None
            return None
        self._notice = None
        self._expire()
        if self.query is not None:
            return self._search_key(k)
        if len(k) == 1 and k in "0123456789":
            if self.rows:
                self._digit(k)
            return None
        self.jump = ""
        if k in ("q", "\x1b"):
            return ("quit", [])
        if k == "n":
            return ("new", [self.here_parent()])
        if k == "R":
            return ("restore", [])
        if k == "A":
            return ("adopt", [])
        if k == "w":
            return ("save", [])
        if k == "U":
            return ("update", [])
        if k == "p":
            return ("plugins", [])
        if k == "o":
            return ("profiles", [])
        if k == "D":
            return ("dispatches", [])
        if k == "/":
            self._start_search()
            return None
        if k in ("\t", "KEY_BTAB"):
            self._step_filter(-1 if k == "KEY_BTAB" else 1)
            return None
        if k == "*":
            self._select_all()
            return None
        if not self.visible:
            return None
        if k in MOVES:
            self.cursor = max(0, min(len(self.visible) - 1, self.cursor + MOVES[k]))
            return None
        if self.on_other():  # row-targeted actions on a read-only other row only say where to go
            return ("desktop", []) if k in ENTER or (len(k) == 1 and k in "smPeXra") else None
        if k == "g":
            return ("group", self._as_items())
        if k == "f":
            return ("file", self._as_items())
        if k == "s":
            names = self._names(*self._targets())
            if names:
                self._stop = names
            else:
                self._notice = "資料夾裡沒有 session"
            return None
        if k == "S":
            self._ask_restart()
            return None
        if self.on_folder():
            handled, res = self._on_folder_key(k)
            if handled:
                return res
        elif k == "KEY_LEFT" and self.tree is not None:
            self._goto_folder(folder_of(self.tree, self._here()))
            return None
        if k == " ":
            self.marked ^= {self._here()}
            self.cursor = min(len(self.visible) - 1, self.cursor + 1)
            return None
        if k in ENTER:
            return ("attach", [self._here()])
        if k == "m":
            return ("move", [self._here()])
        if k == "P":
            return ("switch", [self._here()])
        if k == "e":
            return ("rename", [self._here()])
        if k == "X":
            return ("forget", [self._here()])
        if k in ("r", "a"):
            return ("retired" if k == "r" else "active", [self._here()])
        return None


def _copy_tree(tree):
    return None if tree is None else F.Tree(dict(tree.folders), dict(tree.sessions))


PROFILE_KEYS = {"r": "rename", "d": "toggle", "f": "forget", "i": "statusline"}


class ProfilePicker(Picker):
    """Profile screen list (rows are profile_ops.ProfileRow): ↑↓ move; r/d/f/i act on the row under the cursor,
    a adds, q / Esc go back."""

    def key(self, k):
        if k in ("q", "\x1b"):
            return ("back", [])
        if k == "a":
            return ("add", [])
        if not self.visible:
            return None
        if k in MOVES:
            self.cursor = max(0, min(len(self.visible) - 1, self.cursor + MOVES[k]))
            return None
        if k in PROFILE_KEYS:
            return (PROFILE_KEYS[k], [self._here()])
        return None


def _row_key(c):
    return c.row.tmux


class Checklist:
    """State of the restore/adopt checklist: default checkmarks equal candidates; a row with skip cannot be checked. key() returns (action, list of ids) or None.
    The id is ident(c): restore uses the tmux name, adopt uses Outside.key."""

    def __init__(self, cands, ident=_row_key):
        self.cands = cands
        self.ident = ident
        self.marked = {ident(c) for c in cands if c.selected and not c.skip}
        self.cursor = 0

    def key(self, k):
        if k in ("q", "\x1b"):
            return ("cancel", None)
        if k in ENTER:
            return ("done", [self.ident(c) for c in self.cands if self.ident(c) in self.marked])
        if not self.cands:
            return None
        if k in MOVES:
            self.cursor = max(0, min(len(self.cands) - 1, self.cursor + MOVES[k]))
        elif k == " ":
            c = self.cands[self.cursor]
            if not c.skip:
                self.marked ^= {self.ident(c)}
            self.cursor = min(len(self.cands) - 1, self.cursor + 1)
        return None
