"""Pure key-handling state of the plugin screens (no curses): the plugin list, the catalog picker and the per-profile toggles."""
from dataclasses import dataclass

from ..plugin_view import mismatch
from .state import Picker
from .text import ENTER, MOVES


class PluginPicker(Picker):
    """Plugin or marketplace list (rows only need .name): Picker navigation (digits, /, arrows) plus the list actions; Tab switches tabs."""

    def key(self, k):
        if self.query is not None:
            res = super().key(k)
            return ("open", res[1]) if res and res[0] == "attach" else res
        if len(k) == 1 and k.isdigit() or k == "/" or k in MOVES:
            return super().key(k)
        self._expire()
        self.jump = ""
        if k in ("q", "\x1b"):
            return ("back", [])
        if k == "\t":
            return ("tab", [])
        if k == "U":
            return ("update_all", [])
        if k == "a":
            return ("add", [])
        if k == "r":
            return ("check", [])
        if not self.visible:
            return None
        if k in ENTER:
            return ("open", [self._here()])
        if k == "u":
            return ("update", [self._here()])
        return None


@dataclass(frozen=True)
class _CatalogRow:
    entry: object

    @property
    def name(self):
        return self.entry.pid


class CatalogPicker(Picker):
    """Marketplace catalog: rows wrap CatalogEntry so that .name is the plugin id."""

    def __init__(self, entries, current=None, **kw):
        super().__init__([_CatalogRow(e) for e in entries], current, **kw)

    def key(self, k):
        if self.query is not None:
            res = super().key(k)
            return ("pick", res[1]) if res and res[0] == "attach" else res
        if len(k) == 1 and k.isdigit() or k == "/" or k in MOVES:
            return super().key(k)
        self._expire()
        self.jump = ""
        if k in ("q", "\x1b"):
            return ("back", [])
        if k == "i":
            return ("type", [])
        if k in ENTER and self.visible:
            return ("pick", [self._here()])
        return None


class ProfileToggles:
    """Per-profile install checkboxes for one plugin. key() returns ("save", None), ("back", None) or None."""

    def __init__(self, choices, checked=None, *, notes=("← 儲存時會安裝", "← 儲存時會移除"), mismatch_fn=mismatch):
        self.choices = choices
        self.notes = notes
        self.mismatch_fn = mismatch_fn
        self.checked = dict(checked) if checked is not None else {c.profile: c.installed is not None for c in choices}
        self.initial = dict(self.checked)
        self.cursor = 0
        self.prompt = None

    def dirty(self):
        return self.checked != self.initial

    def key(self, k):
        if self.prompt is not None:
            self.prompt = None
            return ("back", None) if k in ("y", "Y") else None
        if k in ("q", "\x1b"):
            if not self.dirty():
                return ("back", None)
            self.prompt = "放棄變更？(y/N)"
            return None
        if k == "s":
            return ("save", None)
        if not self.choices:
            return None
        if k in MOVES:
            self.cursor = max(0, min(len(self.choices) - 1, self.cursor + MOVES[k]))
        elif k == " ":
            c = self.choices[self.cursor]
            if not c.disabled:
                self.checked[c.profile] = not self.checked[c.profile]
            self.cursor = min(len(self.choices) - 1, self.cursor + 1)
        return None

    def mark(self, c):
        if c.disabled:
            return "[-]"
        return ("[x]" if self.checked[c.profile] else "[ ]") + ("!" if self.mismatch_fn(c) else "")

    def note(self, c):
        if c.disabled:
            return c.disabled
        want = self.checked[c.profile]
        if want != (c.installed is not None):
            return self.notes[0] if want else self.notes[1]
        return self.mismatch_fn(c)
