"""Background reads of the Codex app-server daemon for the curses ui: every DAEMON_S seconds one codex_daemon.snapshot
(≤3 s, so never on the UI thread). A success saves the 額度 to usage/<profile>.json for the Codex profile whose
CODEX_HOME is the daemon's, and its thread states colour the tmux Codex rows; a failure just leaves latest None.
Only the curses ui makes one (--plain and the CLI commands never do), and only with an enabled Codex profile."""
import os
import threading
import time
from datetime import datetime, timezone

from .. import codex_daemon
from ..usage import record_codex

DAEMON_S = 30  # seconds between two reads of the daemon
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
    notloaded / unknown / not found leave the row alone, and state is never touched. Idempotent; snap None = no-op."""
    if snap is None:
        return
    by_id = {t.id: t for t in snap.threads}
    for r in rows:
        if r.agent != "codex" or not r.sid:
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
                 interval=DAEMON_S):
        self.cfg, self.run, self.snapshot_fn, self.now_fn = cfg, run, snapshot_fn, now_fn
        self.clock, self.interval = clock, interval
        self.latest = None
        self._stop = threading.Event()
        self._thread = None

    def poll_once(self):
        cfg = self.cfg
        if not cfg.codex.enabled:  # the re-read config.toml has no enabled Codex profile any more
            self.latest = None
            return None
        try:
            snap = self.snapshot_fn(cfg.codex.home, self.run, now=self.now_fn())
        except Exception:  # noqa: BLE001  a broken daemon / client bug only means "no reading"
            snap = None
        self.latest = snap
        if snap is not None and snap.usage is not None:
            home = _real(snap.home)
            for p in cfg.codex_profiles.values():
                if home is not None and _real(p.config_dir) == home:
                    try:
                        record_codex(cfg, p.name, snap.usage)
                    except OSError:
                        pass
        return snap

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
