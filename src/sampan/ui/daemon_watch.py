"""Background reads of the Codex app-server daemon for the curses ui: every DAEMON_S seconds one codex_daemon.snapshot
(≤3 s, so never on the UI thread). A success saves the 額度 to usage/<profile>.json for the Codex profile whose
CODEX_HOME is the daemon's, and its thread states colour the tmux Codex rows; a failure just leaves latest None.
Only the curses ui makes one (--plain and the CLI commands never do), and only with an enabled Codex profile."""
import os
import threading
import time
from datetime import datetime, timezone

from .. import codex_daemon
from ..status import State
from ..usage import record_codex

DAEMON_S = 30  # seconds between two reads of the daemon
RETRY_WAIT_S = 2  # after a start that returned 0 but left the socket not ready: one more read this much later
STATE_LABEL = {"approval": "等你核准", "input": "等你輸入", "error": "錯誤", "working": "執行中", "idle": "閒置",
               "notloaded": "未開啟", "unknown": "?"}
SEVERITY = ("approval", "input", "error", "working", "idle", "notloaded", "unknown")  # most urgent first
_ROW_STATUS = {"approval": "waiting", "input": "waiting", "working": "working", "idle": "idle", "error": "error"}


def _now():
    return datetime.now(timezone.utc)


def thread_state(t):
    """A CodexThread's state: approval / input (active and waiting on you), error, working, idle, notloaded, unknown."""
    if t.status == "active":
        if "waitingOnApproval" in t.flags:
            return "approval"
        if "waitingOnUserInput" in t.flags:
            return "input"
        return "working"
    return {"systemError": "error", "idle": "idle", "notLoaded": "notloaded"}.get(t.status, "unknown")


def apply_codex_status(rows, snap):
    """Overwrite agent_status of the codex rows whose sid the daemon knows (a thread only in `loaded` counts as idle);
    notloaded / unknown / not found leave the row alone, and state is never touched. Stopped and retired rows are
    skipped: no agent of theirs runs in tmux, and the daemon still lists a thread it loaded before it exited.
    Idempotent; snap None = no-op."""
    if snap is None:
        return
    by_id = {t.id: t for t in snap.threads}
    for r in rows:
        if r.agent != "codex" or not r.sid or r.state in (State.STOPPED, State.RETIRED):
            continue
        t = by_id.get(r.sid)
        st = thread_state(t) if t is not None else ("idle" if r.sid in snap.loaded else None)
        if st in _ROW_STATUS:
            r.agent_status = _ROW_STATUS[st]


def _real(path):
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


class DaemonWatcher:
    """poll_once reads the daemon synchronously; start() does it every `interval` seconds on a daemon thread (any
    exception is swallowed: the ui must never notice a broken daemon). `latest` is the last result (None = failed).
    `cfg` may be replaced while running (the ui re-reads config.toml); each poll uses the current one."""

    def __init__(self, cfg, run, *, snapshot_fn=codex_daemon.snapshot, now_fn=_now, clock=time.monotonic,
                 interval=DAEMON_S, start_fn=codex_daemon.start_daemon, retry_wait=RETRY_WAIT_S):
        self.cfg, self.run, self.snapshot_fn, self.now_fn = cfg, run, snapshot_fn, now_fn
        self.clock, self.interval, self.start_fn, self.retry_wait = clock, interval, start_fn, retry_wait
        self.start_state = None  # None (not tried / succeeded / over) | "starting" | "failed"
        self.start_error = None  # one-line reason while start_state == "failed"
        self._tried = False  # at most one start per watcher (= per opening of the ui)
        self.latest = None
        self.polled = False  # a first read has finished (before it, latest None means "not known yet", not "down")
        self._stop = threading.Event()
        self._thread = None

    def poll_once(self):
        cfg = self.cfg
        if not cfg.codex.enabled:  # the re-read config.toml has no enabled Codex profile any more
            self.latest, self.polled = None, True
            return None
        try:
            snap = self.snapshot_fn(cfg.codex.home, self.run, now=self.now_fn())
        except Exception:  # noqa: BLE001  a broken daemon / client bug only means "no reading"
            snap = None
        if snap is not None and snap.usage is not None:
            home = _real(snap.home)
            for p in cfg.codex_profiles.values():
                if home is not None and _real(p.config_dir) == home:
                    try:
                        record_codex(cfg, p.name, snap.usage)
                    except OSError:
                        pass
        if snap is not None:
            self.start_state = None  # a daemon that answers ends any start note; an old "failed" must not return later
        self.latest = snap  # published only after the reading is saved, so the account lines never lag behind it
        self.polled = True
        return snap

    def first_read(self):
        """The first read; when the daemon is down, Codex is enabled and auto-start is on, start it once (this blocks
        up to START_TIMEOUT_S, so only the background thread calls it) and read again."""
        snap = self.poll_once()
        cfg = self.cfg
        if snap is not None or self._tried or not cfg.codex.enabled or not cfg.codex_auto_start:
            return
        self._tried = True
        self.start_state = "starting"
        try:
            reason = self.start_fn(cfg.codex.home, self.run)
        except Exception as e:  # noqa: BLE001  a broken start only means "failed", never a traceback
            reason = str(e) or type(e).__name__
        if reason is None:
            try:
                if self.poll_once() is None and not self._stop.wait(self.retry_wait):
                    self.poll_once()  # the socket may need a moment after a start that returned 0; one bounded retry
            finally:  # stay "starting" until the reads are in, so the ui never flickers to "down"
                self.start_state = None
        else:
            self.start_error = reason  # before the state, so the ui never sees "failed" without a reason
            self.start_state = "failed"
            self.poll_once()  # the daemon may be up anyway (e.g. it was starting itself)

    def _loop(self):
        step = self.first_read  # the first round may start the daemon; later rounds only read
        while not self._stop.is_set():
            try:
                step()
            except Exception:  # noqa: BLE001  never let the thread die with a traceback over the curses screen
                pass
            step = self.poll_once
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="sampan-codex-daemon", daemon=True)
        self._thread.start()

    def stop(self):
        """Ask the thread to end and wait briefly; a poll in flight is bounded by snapshot's own timeout, and the
        thread is a daemon thread, so sampan never waits for it on exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)


def make_watcher(cfg, run):
    """A DaemonWatcher for the curses ui, or None when no Codex profile is enabled."""
    return DaemonWatcher(cfg, run) if cfg.codex.enabled and cfg.codex_profiles else None
