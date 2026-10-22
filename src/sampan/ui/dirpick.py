"""Directory chooser (the ui's n opens a new session, m moves the directory): a pure state machine, never touches curses.

DirPicker.key(key) returns None (continue), ("done", path) or ("cancel", None). The filesystem operations (listdir/isdir/mkdir) can all be
injected, so tests use a temporary directory. Typed questions (+ new folder, / type a path directly, the confirmation when the home directory is picked) are also in the state machine: prompt is the line to show,
message is the result of the previous key (error, cancel).
"""
import os

from ..clock import parse_iso
from .text import BACKSPACE, ENTER

UP = ("KEY_UP", "k")
DOWN = ("KEY_DOWN", "j")
HOME_CONFIRM = "~ 是家目錄，建議用專案目錄。還是要用 ~？(y/N)"
MKDIR_PROMPT = "新資料夾名稱（Enter 建立、Esc 取消）："
PATH_PROMPT = "路徑（Enter 確定、Esc 取消）："


def tilde(path, home):
    if path == home:
        return "~"
    return "~" + path[len(home):] if path.startswith(home.rstrip("/") + "/") else path


def recent_dirs(rows, isdir=os.path.isdir):
    """cwds used in the registry whose directory still exists (no duplicates): those with more sessions first, ties broken by the most recent seen_at."""
    stats = {}
    for r in rows:
        if not r.cwd:
            continue
        cwd = os.path.normpath(r.cwd)
        n, last = stats.get(cwd, (0, 0.0))
        stats[cwd] = (n + 1, max(last, _epoch(r.seen_at)))
    ordered = sorted(stats.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
    return [cwd for cwd, _ in ordered if isdir(cwd)]


def _epoch(s):
    """seen_at as a timestamp, comparable across offsets; unparseable = oldest."""
    dt = parse_iso(s)
    return dt.timestamp() if dt else 0.0


def sessions_by_dir(rows):
    out = {}
    for r in rows:
        if r.cwd:
            out.setdefault(os.path.normpath(r.cwd), []).append(r.tmux)
    return out


class DirPicker:
    def __init__(self, start, *, home, recent=(), sessions=None, allow_missing=False, listdir=os.listdir,
                 isdir=os.path.isdir, mkdir=os.mkdir):
        self.home = os.path.normpath(home)
        self.recent = list(recent)
        self.sessions = sessions or {}
        self.allow_missing = allow_missing
        self.listdir, self.isdir, self.mkdir = listdir, isdir, mkdir
        self.show_hidden = False
        self.focus = "list"
        self.rcursor = 0
        self.message = None
        self.prompt = None
        self._mode, self._buf = None, ""
        self.cwd, self.entries, self.cursor = None, [], 0
        for d in (start, self.home, "/"):
            if d and self.isdir(d) and self._go(os.path.normpath(d)):
                break

    # ── directories ──

    def _list(self, path):
        names = [n for n in self.listdir(path)
                 if (self.show_hidden or not n.startswith(".")) and self.isdir(os.path.join(path, n))]
        names.sort(key=lambda n: (n.lower(), n))
        return ([] if path == "/" else [".."]) + names

    def _go(self, path, focus=None):
        """Enter path; if unreadable, stay where we are and explain in the message. Returns whether it succeeded."""
        try:
            entries = self._list(path)
        except OSError as e:
            self.message = f"⚠ 讀不到 {tilde(path, self.home)}（{e.strerror or e}）"
            return False
        self.cwd, self.entries = path, entries
        self.cursor = entries.index(focus) if focus in entries else 0
        return True

    def _up(self):
        if self.cwd != "/":
            self._go(os.path.dirname(self.cwd), focus=os.path.basename(self.cwd))

    def _here(self):
        """The directory under the cursor (.. does not count)."""
        if not self.entries or self.entries[self.cursor] == "..":
            return None
        return os.path.join(self.cwd, self.entries[self.cursor])

    def note(self):
        """Registered sessions in the directory under the cursor (the current directory if there is none)."""
        if self.focus == "recent" and self.recent:
            cands = [self.recent[self.rcursor]]
        else:
            cands = [d for d in (self._here(), self.cwd) if d]
        for d in cands:
            names = self.sessions.get(d)
            if names:
                name = "~" if d == self.home else (os.path.basename(d) or d)
                return f"{name} 裡已有 session：{', '.join(names)}"
        return None

    # ── selection ──

    def _choose(self, path):
        if path == self.home:
            self._mode, self.prompt, self._picked = "home", HOME_CONFIRM, path
            return None
        return ("done", path)

    def _ask(self, mode, prompt):
        self._mode, self._buf = mode, ""
        self._prompt_text = prompt
        self.prompt = prompt + "_"

    def _end_input(self):
        self._mode, self._buf, self.prompt = None, "", None

    def _typed(self, k):
        """While typing (+ or /): returns what key() should return."""
        if k == "\x1b":
            self._end_input()
            self.message = "已取消"
            return None
        if k in BACKSPACE:
            self._buf = self._buf[:-1]
        elif k in ENTER:
            mode, buf = self._mode, self._buf.strip()
            self._end_input()
            return self._mkdir(buf) if mode == "mkdir" else self._path(buf)
        elif len(k) == 1 and k.isprintable():
            self._buf += k
        self.prompt = self._prompt_text + self._buf + "_"
        return None

    def _mkdir(self, name):
        if not name:
            self.message = "已取消"
        elif "/" in name:
            self.message = "⚠ 資料夾名稱不能含 /"
        elif name in (".", ".."):
            self.message = f"⚠ 不能用 {name} 當資料夾名稱"
        elif os.path.lexists(os.path.join(self.cwd, name)):
            self.message = f"⚠ 已經有 {name}"
        else:
            path = os.path.join(self.cwd, name)
            try:
                self.mkdir(path)
            except OSError as e:
                self.message = f"❌ 建立失敗：{e.strerror or e}"
                return None
            self.message = f"✔ 已建立 {tilde(path, self.home)}"
            self._go(path)
        return None

    def _path(self, raw):
        if not raw:
            self.message = "已取消"
            return None
        if raw == "~" or raw.startswith("~/"):
            raw = self.home + raw[1:]
        path = os.path.normpath(os.path.join(self.cwd, os.path.expanduser(raw)))
        if self.isdir(path):
            return self._choose(path)
        if self.allow_missing:
            return ("done", path)
        self.message = f"⚠ 目錄不存在：{tilde(path, self.home)}"
        return None

    def key(self, k):
        if self._mode == "home":
            picked = self._picked
            self._end_input()
            if k in ("y", "Y"):
                return ("done", picked)
            self.message = "已取消"
            return None
        if self._mode is not None:
            return self._typed(k)
        self.message = None
        if k == "\x1b":
            return ("cancel", None)
        if k == "\t":
            if self.recent:
                self.focus = "recent" if self.focus == "list" else "list"
            return None
        if k == "+":
            self._ask("mkdir", MKDIR_PROMPT)
            return None
        if k == "/":
            self._ask("path", PATH_PROMPT)
            return None
        if k == ".":
            self.show_hidden = not self.show_hidden
            keep = self.entries[self.cursor] if self.entries else None
            self._go(self.cwd, focus=keep)
            return None
        if self.focus == "recent":
            return self._recent_key(k)
        if k in UP or k in DOWN:
            step = -1 if k in UP else 1
            self.cursor = max(0, min(len(self.entries) - 1, self.cursor + step))
            return None
        if k in ("KEY_RIGHT", "l") or k in ENTER:
            here = self._here()
            if here is None:
                if self.entries and self.entries[self.cursor] == "..":
                    self._up()
                return None
            self._go(here)
            return None
        if k in ("KEY_LEFT", "h") or k in BACKSPACE:
            self._up()
            return None
        if k == " ":
            return self._choose(self.cwd)
        return None

    def _recent_key(self, k):
        if k in UP or k in DOWN:
            step = -1 if k in UP else 1
            self.rcursor = max(0, min(len(self.recent) - 1, self.rcursor + step))
            return None
        if k in ENTER or k == " " or k == "KEY_RIGHT":
            return self._choose(self.recent[self.rcursor])
        if k == "KEY_LEFT":
            self.focus = "list"
        return None
