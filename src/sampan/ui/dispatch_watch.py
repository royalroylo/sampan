"""Dispatches in the curses ui: a background thread advances every pending dispatch each INTERVAL_S seconds (send.advance
holds the dispatches lock and can run tmux, ps and `codex queue` for up to ~30 s, so never on the UI thread), plus the pure
pieces of the screens: the summary line, the list cells and the list's cursor state. Only the curses ui makes a watcher
(--plain and the CLI commands never do)."""
import subprocess
import threading
import time

from .. import dispatch, send
from ..clock import fmt_local, parse_iso
from .text import MOVES, text_width

INTERVAL_S = 3  # seconds between two rounds
LIST_HELP = "↑↓ 移動　x 取消（只有排隊中的）　q 返回"
LIST_TITLE = "sampan · 派工"
STATE_STYLE = {"queued": "warn", "sent": "info", "done": "ok", "failed": "err", "cancelled": "muted"}
NAME_MAX, REASON_MAX = 12, 40  # widest from / to and reason columns of the list


def dispatch_summary(items):
    """The line under the account lines, or None when nothing is queued or waiting for a reply."""
    queued = sum(1 for d in items if d.state == "queued")
    sent = sum(1 for d in items if d.state == "sent")
    return f"派工：{queued} 排隊、{sent} 等回覆（D 看清單）" if queued or sent else None


def _note(d):
    return d.reason or (" ".join(d.reply.split())[:40] if d.state == "done" and d.reply else "")


def dispatch_cells(d):
    """One list row: id, from, to, state, created, reason (or the start of the reply)."""
    return (d.id, d.from_name, d.to_name, (send.STATE_LABELS[d.state], STATE_STYLE.get(d.state, "")),
            fmt_local(parse_iso(d.created)), _note(d))


def dispatch_cols(items):
    """Widths of the columns after the id: from, to (at most NAME_MAX cells), state, created, reason (at most REASON_MAX)."""
    def w(vals, cap):
        return min(cap, max([text_width(v) for v in vals] + [4]))
    cells = [dispatch_cells(d) for d in items]
    return (w([c[1] for c in cells], NAME_MAX), w([c[2] for c in cells], NAME_MAX), 14, 11,
            w([c[5] for c in cells], REASON_MAX))


class DispatchPicker:
    """Cursor state of the dispatch list (items newest first): ↑↓ move, x asks to cancel the one under the cursor,
    q / Esc go back. key() returns ("cancel", [id]) / ("back", []) or None."""

    def __init__(self, items):
        self.items, self.cursor = list(items), 0

    @property
    def here(self):
        return self.items[self.cursor] if self.items else None

    def reload(self, items):
        """New items from the periodic refresh: the cursor stays on the same dispatch while it is still there."""
        here = self.here.id if self.items else None
        self.items = list(items)
        ids = [d.id for d in self.items]
        self.cursor = ids.index(here) if here in ids else max(0, min(self.cursor, len(ids) - 1))

    def key(self, k):
        if k in ("q", "\x1b"):
            return ("back", [])
        if not self.items:
            return None
        if k in MOVES:
            self.cursor = max(0, min(len(self.items) - 1, self.cursor + MOVES[k]))
        elif k == "x":
            return ("cancel", [self.here.id])
        return None


def _no_tty_stdin(run):
    """run, with stdin=DEVNULL unless the call passes its own: a child that inherits the terminal's stdin swallows the
    keystrokes meant for curses (the v0.10.0 lesson)."""
    def wrapped(args, **kw):
        if kw.get("stdin") is None:
            kw["stdin"] = subprocess.DEVNULL
        return run(args, **kw)
    return wrapped


class DispatchWatcher:
    """poll_once advances every pending dispatch once, synchronously; start() does it every `interval` seconds on a
    daemon thread (any exception is swallowed: the ui must never notice a broken dispatch file). `items` is the last read
    of dispatches.json (before and after a round), what the summary line counts. `cfg` may be replaced while running."""

    def __init__(self, cfg, run, *, advance_fn=send.advance, pending_fn=send.pending, load_fn=dispatch.load,
                 sleep=time.sleep, interval=INTERVAL_S):
        self.cfg, self.run = cfg, _no_tty_stdin(run)
        self.advance_fn, self.pending_fn, self.load_fn, self.sleep, self.interval = (
            advance_fn, pending_fn, load_fn, sleep, interval)
        self.items = []
        self._stop = threading.Event()
        self._thread = None

    def _read(self):
        try:
            self.items = self.load_fn(self.cfg.state_dir)
        except Exception:  # noqa: BLE001  an unreadable file just means nothing to show
            self.items = []

    def poll_once(self):
        cfg = self.cfg
        self._read()  # first, so a dispatch queued a moment ago shows before a slow advance ends
        try:
            ids = self.pending_fn(cfg)
        except Exception:  # noqa: BLE001
            ids = []
        for did in ids:
            if self._stop.is_set():
                break
            try:
                self.advance_fn(cfg, self.run, did, sleep=self.sleep)
            except Exception:  # noqa: BLE001  one bad dispatch must not hold the others back
                pass
        self._read()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001  never let the thread die with a traceback over the curses screen
                pass
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="sampan-dispatch-watch", daemon=True)
        self._thread.start()

    def stop(self):
        """Ask the thread to end and wait briefly; a round in flight is a daemon thread, so sampan never waits for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)


def make_dispatch_watcher(cfg, run):
    return DispatchWatcher(cfg, run)
