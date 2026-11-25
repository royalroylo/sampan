"""The resource line at the right end of the main list's separator: memory light, compressor, swap, load, and running /
limit. `gauge` is a pure function; `ResourceWatcher` is the one thread that reads the machine (five seconds apart, fifteen
after five minutes without a key, paused while curses is out). The screen only reads its last result.

The light looks at memory only (limits.py's red light: the one thing that refuses a new session); the count over its limit
is written `23/16!` in the warning style and never turns the light red; load is only coloured."""
import os
import threading
import time
from dataclasses import dataclass

from .. import limits, resources
from ..textwidth import text_width

FRESH_S = 15  # a reading older than this is shown as "?"
FAST_S, SLOW_S, IDLE_AFTER_S = 5, 15, 300
COUNT_S = 15  # the count of agents (one `ps`, one tmux) is taken this often, never faster than the memory reading
COUNT_FRESH_S = 3 * COUNT_S  # a count older than this is not shown as the number (the list's own, marked ?)
SPAWN_TIMEOUT_S = 2
WIDE_COLS, MID_COLS, MIN_COLS = 100, 70, 50


@dataclass(frozen=True)
class Reading:
    at: float  # the watcher's clock when it was taken
    snap: object
    classified: object
    running: int | None = None  # limits.count's total (running, starting, outside tmux), None = not counted
    limit: int | None = None  # the count limit, None = no limit
    count_at: float | None = None  # the watcher's clock when that count was taken (every COUNT_S, slower than the memory)


_STYLE = {"yellow": "warn", "high": "warn", "red": "err"}


def _field(label, text, level, *, red=False):
    """One memory field: "!" in front and the warning / error style when it is not green; "?" (muted) when unknown.
    A compressor or swap that is high is red only while the memory light is red (limits: high alone is not enough)."""
    if text is None or level is None:
        return [(f"{label}?", "muted")]
    level = "red" if red and level == "high" else level
    return [(("!" if level in _STYLE else "") + label + text, _STYLE.get(level, ""))]


def gauge(reading, running, limit, width, *, now, approx=False):
    """[(text, style), …] for the right end of the separator line, [] when there is no room (width = terminal columns).
    reading: Reading or None (unknown: every memory field is "?"); running: agents counted; limit: int or None.
    >= 100 columns: everything; 70-99: abbreviated; 50-69: light and count; below: nothing."""
    if width < MIN_COLS:
        return []
    fresh = reading is not None and 0 <= now - reading.at <= FRESH_S
    c = reading.classified if fresh else None
    sn = reading.snap if fresh else None
    over = limit is not None and running > limit
    mark = "?" if approx else ""  # the list's own number, not limits.count's (that one is too old or not there yet)
    count = (f"{running}{mark}/{limit}!" if over else f"{running}{mark}/{limit}") if limit is not None else f"{running}{mark}"
    count_style = "warn" if over else ""
    light = {"green": "ok", "yellow": "warn", "red": "err"}.get(c.memory if c else "", "muted")
    if width < MID_COLS:
        return [("◆", light), (" ", ""), (count, count_style)]
    wide = width >= WIDE_COLS
    red = bool(c and c.red)
    comp = None if sn is None or sn.compressor_pct is None else f"{sn.compressor_pct * 100:.0f}%"
    swap = None if sn is None or sn.swap_used is None else f"{sn.swap_used / (1 << 30):.1f}G"
    load = None if sn is None or sn.load5 is None else f"{sn.load5:.1f}"
    fields = [
        _field("壓縮 " if wide else "壓", comp, c.compressor if c else None, red=red),
        _field("swap " if wide else "S", swap, c.swap if c else None, red=red),
        _field("負載 " if wide else "L", load, c.load if c else None),  # load is only coloured: red stays "red"
    ]
    joint = [(" │ ", "dim")] if wide else [(" ", "")]
    out = [("◆", light), (" ", "")]
    for k, f in enumerate(fields):
        out += (joint if k else []) + f
    return out + joint + [(("執行中 " if wide else "") + count, count_style)]


def gauge_text(segs):
    return "".join(t for t, _ in segs)


def plain_line(reading, running, limit, *, now):
    """The same line for the plain-text list (printed first and after each action)."""
    return gauge_text(gauge(reading, running, limit, WIDE_COLS, now=now))


def fit(segs, room):
    """segs when they fit in room columns, else []."""
    return segs if segs and text_width(gauge_text(segs)) <= room else []


class ResourceWatcher:
    """Reads the machine in the background. step() takes one reading (the loop calls it; tests call it directly).
    Stops by itself when the terminal's process is gone (parent pid 1) or stop() is called."""

    def __init__(self, cfg, run, *, clock=time.monotonic, getppid=os.getppid, snapshot_fn=resources.snapshot,
                 report_fn=limits.report):
        self.cfg, self.clock, self.getppid, self.snapshot_fn = cfg, clock, getppid, snapshot_fn
        self.report_fn = report_fn
        self.run = resources.with_timeout(run, SPAWN_TIMEOUT_S)  # a stuck spawn never stalls it
        self.backoff = resources.Backoff(base=FAST_S, clock=clock)
        self.latest = None
        self.last_key = clock()
        self.paused = False
        self._stop = threading.Event()
        self._thread = None

    def note_key(self):
        self.last_key = self.clock()

    def interval(self):
        return SLOW_S if self.clock() - self.last_key >= IDLE_AFTER_S else FAST_S

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        self.last_key = self.clock()

    def step(self):
        """One reading → latest. The memory sample (sysctl + vm_stat: two spawns) is taken every time; the count of agents
        (limits.report: one `ps`, one tmux) only every COUNT_S, and between two counts the last one stays. A failure
        leaves latest as it was (it ages out to "?") and the next waits longer (resources.Backoff): a reading that
        could not be had, or whose state file could not be written, both count."""
        if self.paused:
            return False
        try:
            lim = self.cfg.limits
            snap = self.snapshot_fn(self.run, self.cfg.state_dir, lim, probe=False)
            now = self.clock()
            prev = self.latest
            running, limit, count_at = (prev.running, prev.limit, prev.count_at) if prev is not None else (None, None, None)
            if count_at is None or now - count_at >= max(COUNT_S, self.interval()):
                try:
                    rep = self.report_fn(self.cfg, self.run, snap=snap)
                    running, limit, count_at = rep.count.total, rep.limit, now
                except Exception:  # noqa: BLE001  without the count the number on the line is the list's own
                    pass
            self.latest = Reading(now, snap, resources.classify(snap, lim), running, limit, count_at)
        except Exception:  # noqa: BLE001  a reading that fails shows as "?"
            self.backoff.failed()
            return False
        written = resources.state_age(self.cfg.state_dir)
        if resources.usable(snap) and written is not None and written <= resources.STALE_S + 2 * SLOW_S:
            self.backoff.ok()
        else:
            self.backoff.failed()
        return True

    def _loop(self):
        while not self._stop.is_set():
            if self.getppid() == 1:  # the terminal is gone
                return
            self.step()
            if self._stop.wait(max(self.interval(), self.backoff.delay())):
                return

    def start(self):
        if self._thread is None and getattr(self.cfg, "resource_bar", True):
            self._thread = threading.Thread(target=self._loop, name="sampan-resources", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()


def running_of(rows):
    """Agents the list shows as running (the same rows the list was built from: no second `ps`)."""
    return sum(1 for r in rows if r.live == "running" or getattr(r.state, "value", r.state) == "running")


def limit_of(cfg, reading):
    ram = reading.snap.ram if reading is not None else None
    return limits.effective_max(cfg.limits, ram)


def plain_line_now(cfg, run, rows, *, clock=time.monotonic):
    """The line for the plain-text list: one reading taken now ("" when the bar is off or nothing can be read)."""
    if not getattr(cfg, "resource_bar", True):
        return ""
    try:
        run = resources.with_timeout(run, SPAWN_TIMEOUT_S)
        snap = resources.snapshot(run, cfg.state_dir, cfg.limits, probe=False)
        rep = limits.report(cfg, run, snap=snap)
        reading = Reading(clock(), snap, resources.classify(snap, cfg.limits), rep.count.total, rep.limit, clock())
        return plain_line(reading, rep.count.total, rep.limit, now=clock())
    except Exception:  # noqa: BLE001  a reading that fails is no line
        return ""
