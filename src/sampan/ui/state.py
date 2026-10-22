"""Pure state machines of the ui: Picker (arrow-key menu), Checklist (the restore/adopt checklist), and the display conversion of state labels. Never touches curses."""
from ..status import State, _fmt_tokens
from .text import ENTER, MOVES, pad


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
            f"{pad(r.agent_status, 7)} {_fmt_tokens(r.tokens)}")


class Picker:
    """State of the arrow-key menu; key() returns (action, list of names) or None (keep choosing).
    s does not return immediately: it first sets prompt to a confirmation question, and on the next key y/Y returns ("stop!", name) (confirmed), any other key cancels."""

    MOVES = MOVES

    def __init__(self, rows, current=None):
        self.rows = rows
        self.marked = set()
        self.prompt = None
        self._stop = None
        names = [r.name for r in rows]
        self.cursor = names.index(current) if current in names else 0

    def _here(self):
        return self.rows[self.cursor].name

    def key(self, k):
        if self._stop is not None:
            names, self._stop, self.prompt = self._stop, None, None
            return ("stop!", names) if k in ("y", "Y") else None
        if k in ("q", "\x1b"):
            return ("quit", [])
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
        if not self.rows:
            return None
        if k in self.MOVES:
            self.cursor = max(0, min(len(self.rows) - 1, self.cursor + self.MOVES[k]))
            return None
        if k == " ":
            self.marked ^= {self._here()}
            self.cursor = min(len(self.rows) - 1, self.cursor + 1)
            return None
        if k in ENTER:
            return ("attach", [self._here()])
        if k == "s":
            self._stop = [r.name for r in self.rows if r.name in self.marked] or [self._here()]
            self.prompt = f"將停掉：{'、'.join(self._stop)}　確定？(y/N)"
            return None
        if k == "m":
            return ("move", [self._here()])
        if k == "e":
            return ("rename", [self._here()])
        if k in ("r", "a"):
            return ("retired" if k == "r" else "active", [self._here()])
        return None


def _row_key(c):
    return c.row.tmux


class Checklist:
    """State of the restore/adopt checklist: default checkmarks equal candidates; a row with skip cannot be checked. key() returns (action, list of ids) or None.
    The id is ident(c): restore uses the tmux name, adopt uses Outside.key."""

    MOVES = Picker.MOVES

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
        if k in self.MOVES:
            self.cursor = max(0, min(len(self.cands) - 1, self.cursor + self.MOVES[k]))
        elif k == " ":
            c = self.cands[self.cursor]
            if not c.skip:
                self.marked ^= {self.ident(c)}
            self.cursor = min(len(self.cands) - 1, self.cursor + 1)
        return None
