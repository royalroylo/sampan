"""Pure state machines of the ui: Picker (arrow-key menu), Checklist (the restore/adopt checklist), and the display conversion of state labels. Never touches curses."""
import time

from ..status import State, _fmt_tokens
from .text import BACKSPACE, ENTER, MOVES, pad


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


OTHER_REFUSED = "nRAwUposmPeXra"  # action keys that, on a read-only other row, only say where to go instead
JUMP_S = 1.0  # digit jump: a digit typed more than this many seconds after the previous one starts a new number


class Picker:
    """State of the arrow-key menu; key() returns (action, list of names) or None (keep choosing).
    s does not return immediately: it first sets prompt to a confirmation question, and on the next key y/Y returns ("stop!", name) (confirmed), any other key cancels.
    Digits jump to that row number (jump holds the digits typed so far); / starts a search (query is the text, None when not
    searching) and every key is text until Esc, Enter or Backspace on an empty query. visible is the list shown (filtered while
    query is non-empty) and cursor indexes it.
    others (the main list's「Codex 其他對話」, codex_others.OtherRow) follow the rows in visible: the cursor moves onto
    them and a search matches their thread name, but digits count only rows, a space does not mark them, and every
    action key there (all but moving, digits, / and quit) returns ("desktop", []) — they are read only."""

    def __init__(self, rows, current=None, *, clock=time.monotonic, others=()):
        self.rows = rows
        self.others = list(others)
        self.marked = set()
        self.clock = clock
        self._stop = None
        self.query = None
        self.jump = ""
        self._jump_at = None
        self._filter()
        names = [r.name for r in self.visible]
        self.cursor = names.index(current) if current in names else 0

    @property
    def prompt(self):
        if self._stop is not None:
            return f"將停掉：{'、'.join(self._stop)}　確定？(y/N)"
        if self.query is not None:
            return f"/{self.query}▏（{len(self.visible)} 筆，Enter 進入，Esc 取消）"
        self._expire()
        return f"跳到第 {self.jump} 列" if self.jump else None

    def _expire(self):
        """Drop a digit buffer older than JUMP_S."""
        if self.jump and (self._jump_at is None or self.clock() - self._jump_at > JUMP_S):
            self.jump = ""

    def _filter(self):
        q = (self.query or "").lower()
        self.visible = [r for r in self.rows if q in r.name.lower()]
        self.n_rows = len(self.visible)  # visible[n_rows:] are others
        self.visible += [o for o in self.others if q in o.thread.name.lower()]

    def on_other(self):
        """The cursor is on one of others."""
        return self.n_rows <= self.cursor < len(self.visible)

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
        self.marked &= {r.name for r in rows}
        self._filter()
        names = [r.name for r in self.visible]
        if here in names:
            self.cursor = names.index(here)
        elif self.query is not None:
            self.cursor = 0
        else:
            self.cursor = max(0, min(self.cursor, len(self.visible) - 1))

    def _end_search(self):
        here = self._here_or_none()
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
        self._jump_at = self.clock()
        for buf in (self.jump + k, k):
            n = int(buf)
            if 1 <= n <= len(self.rows):
                self.jump = "" if n * 10 > len(self.rows) else buf
                self.cursor = n - 1
                return
        self.jump = ""

    def key(self, k):
        if self._stop is not None:
            names, self._stop = self._stop, None
            return ("stop!", names) if k in ("y", "Y") else None
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
        if self.on_other() and k not in MOVES and k != "/":
            return ("desktop", []) if k in ENTER or (len(k) == 1 and k in OTHER_REFUSED) else None
        if k == "n":
            return ("new", [])
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
        if k == "/":
            self.query = ""
            return None
        if not self.visible:
            return None
        if k in MOVES:
            self.cursor = max(0, min(len(self.visible) - 1, self.cursor + MOVES[k]))
            return None
        if k == " ":
            self.marked ^= {self._here()}
            self.cursor = min(len(self.visible) - 1, self.cursor + 1)
            return None
        if k in ENTER:
            return ("attach", [self._here()])
        if k == "s":
            self._stop = [r.name for r in self.rows if r.name in self.marked] or [self._here()]
            return None
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
