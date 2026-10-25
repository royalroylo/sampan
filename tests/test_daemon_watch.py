import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan.codex_daemon import CodexThread, DaemonSnapshot
from sampan.status import State
from sampan.ui.daemon_watch import (
    DAEMON_S,
    SEVERITY,
    STATE_LABEL,
    DaemonWatcher,
    apply_codex_status,
    make_watcher,
    thread_state,
)
from sampan.usage import Usage, Window, saved_usage
from tests.fakes import FakeRunner
from tests.helpers import make_env, status_row

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)

TWO_CODEX = """
version = 1

[marketplaces]

[profiles.alice]
config_dir = "~/.claude"
default = true

[profiles.codex-a]
agent = "codex"
config_dir = "~/.codex"

[profiles.codex-b]
agent = "codex"
config_dir = "~/.codex-b"
"""


def thread(tid, status="idle", flags=()):
    return CodexThread(id=tid, name="", cwd="/w", originator="codex-tui", source="cli", parent_id=None, status=status,
                       flags=tuple(flags), updated_at=NOW, path=None)


def snap(home, threads=(), loaded=(), usage=None):
    return DaemonSnapshot(Path(home), usage, tuple(threads), frozenset(loaded), NOW)


USAGE = Usage(Window(42.0, NOW + timedelta(hours=2)), Window(10.0, NOW + timedelta(days=3)), NOW)


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()), TWO_CODEX)
        for d in (".codex", ".codex-b"):
            (self.cfg.home / d).mkdir(parents=True, exist_ok=True)
        self.calls = []

    def watcher(self, result):
        def fn(home, run, *, now, **_kw):
            self.calls.append(home)
            if isinstance(result, BaseException):
                raise result
            return result
        return DaemonWatcher(self.cfg, FakeRunner(strict=True), snapshot_fn=fn, now_fn=lambda: NOW)

    def test_constants(self):
        self.assertEqual(DAEMON_S, 30)
        self.assertEqual(SEVERITY, ("approval", "input", "error", "working", "idle", "notloaded", "unknown"))
        self.assertEqual(STATE_LABEL, {"approval": "等你核准", "input": "等你輸入", "error": "錯誤", "working": "執行中",
                                       "idle": "閒置", "notloaded": "未開啟", "unknown": "?"})

    def test_poll_once_records_usage_for_matching_profile(self):
        s = snap(self.cfg.codex.home, usage=USAGE)
        w = self.watcher(s)
        self.assertIs(w.poll_once(), s)
        self.assertIs(w.latest, s)
        self.assertEqual(self.calls, [self.cfg.codex.home])  # snap.home comes from cfg.codex.home
        self.assertEqual(self.cfg.codex.profile, "codex-a")
        self.assertEqual(saved_usage(self.cfg, "codex-a").five_hour.used, 42.0)
        self.assertIsNone(saved_usage(self.cfg, "codex-b"))
        self.assertFalse((self.cfg.state_dir / "usage" / "codex-b.json").exists())

    def test_poll_once_matches_by_realpath(self):
        link = self.cfg.home / "codex-link"
        link.symlink_to(self.cfg.home / ".codex-b")
        self.watcher(snap(link, usage=USAGE)).poll_once()
        self.assertIsNotNone(saved_usage(self.cfg, "codex-b"))
        self.assertIsNone(saved_usage(self.cfg, "codex-a"))

    def test_poll_once_no_match_writes_nothing(self):
        w = self.watcher(snap(self.cfg.home / "elsewhere", usage=USAGE))
        self.assertIsNotNone(w.poll_once())
        self.assertFalse((self.cfg.state_dir / "usage").exists())

    def test_poll_once_without_usage_writes_nothing(self):
        self.watcher(snap(self.cfg.codex.home)).poll_once()
        self.assertFalse((self.cfg.state_dir / "usage").exists())

    def test_watcher_failure_keeps_ui_responsive(self):
        for result in (None, RuntimeError("boom"), OSError("gone")):
            with self.subTest(result=result):
                w = self.watcher(result)
                w.latest = snap(self.cfg.codex.home)  # an earlier success is dropped on failure
                self.assertFalse(w.polled)
                self.assertIsNone(w.poll_once())
                self.assertIsNone(w.latest)
                self.assertTrue(w.polled)  # None now means "down", not "not read yet"
        w = self.watcher(None)
        w.interval = 3600
        w.start()
        try:
            t0 = time.monotonic()
        finally:
            w.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(w._thread.is_alive())

    def test_start_polls_in_background_and_swallows_errors(self):
        w = self.watcher(RuntimeError("boom"))
        w.interval = 3600
        w.start()
        try:
            for _ in range(100):
                if self.calls:
                    break
                time.sleep(0.01)
        finally:
            w.stop()
        self.assertEqual(self.calls, [self.cfg.codex.home])
        self.assertIsNone(w.latest)
        self.assertFalse(w._thread.is_alive())
        w.stop()  # twice is fine

    def test_poll_once_skips_when_codex_was_disabled_meanwhile(self):
        from dataclasses import replace
        w = self.watcher(snap(self.cfg.codex.home, usage=USAGE))
        w.cfg = replace(self.cfg, codex=replace(self.cfg.codex, enabled=False))
        self.assertIsNone(w.poll_once())
        self.assertEqual(self.calls, [])

    def test_make_watcher_only_with_enabled_codex_profile(self):
        self.assertIsInstance(make_watcher(self.cfg, FakeRunner()), DaemonWatcher)
        none = make_env(Path(tempfile.mkdtemp()), "version = 1\n[marketplaces]\n[profiles.alice]\n"
                        "config_dir = \"~/.claude\"\ndefault = true\n[codex]\nenabled = false\n")
        self.assertIsNone(make_watcher(none, FakeRunner()))


class ThreadStateTest(unittest.TestCase):
    def test_thread_state(self):
        cases = [(("active", ("waitingOnApproval",)), "approval"), (("active", ("waitingOnUserInput",)), "input"),
                 (("active", ("waitingOnApproval", "waitingOnUserInput")), "approval"),
                 (("systemError", ()), "error"), (("active", ()), "working"), (("idle", ()), "idle"),
                 (("notLoaded", ()), "notloaded"), (("unknown", ()), "unknown"), (("weird", ()), "unknown")]
        for (status, flags), want in cases:
            with self.subTest(status=status, flags=flags):
                self.assertEqual(thread_state(thread("t", status, flags)), want)


class ApplyStatusTest(unittest.TestCase):
    def test_apply_codex_status(self):
        rows = [status_row("ap", agent="codex", sid="t-ap", agent_status="working"),
                status_row("wk", agent="codex", sid="t-wk", agent_status="idle"),
                status_row("ld", agent="codex", sid="t-ld", agent_status="unknown"),
                status_row("nl", agent="codex", sid="t-nl", agent_status="working"),
                status_row("in", agent="codex", sid="t-in", agent_status="idle"),
                status_row("er", agent="codex", sid="t-er", agent_status="idle"),
                status_row("gone", agent="codex", sid="t-gone", agent_status="idle"),
                status_row("cl", agent="claude", sid="t-ap", agent_status="busy", state=State.STOPPED)]
        s = snap("/h", threads=[thread("t-ap", "active", ["waitingOnApproval"]), thread("t-wk", "active"),
                                thread("t-nl", "notLoaded"), thread("t-in", "active", ["waitingOnUserInput"]),
                                thread("t-er", "systemError")], loaded={"t-ld"})
        states = [r.state for r in rows]
        apply_codex_status(rows, s)
        self.assertEqual([r.agent_status for r in rows],
                         ["waiting", "working", "idle", "working", "waiting", "error", "idle", "busy"])
        self.assertEqual([r.state for r in rows], states)
        apply_codex_status(rows, s)  # idempotent
        self.assertEqual(rows[0].agent_status, "waiting")

    def test_apply_none_or_empty_sid_is_a_no_op(self):
        rows = [status_row("a", agent="codex", sid="", agent_status="idle")]
        apply_codex_status(rows, None)
        apply_codex_status(rows, snap("/h", loaded={""}))
        self.assertEqual(rows[0].agent_status, "idle")


if __name__ == "__main__":
    unittest.main()


class PublishAfterSaveTest(WatcherTest):
    def test_latest_is_published_only_after_the_usage_is_saved(self):
        seen = []
        w = self.watcher(snap(self.cfg.codex.home, usage=USAGE))
        import sampan.ui.daemon_watch as dw
        real = dw.record_codex

        def spy(*a, **k):
            seen.append(w.latest)
            return real(*a, **k)

        dw.record_codex = spy
        try:
            w.poll_once()
        finally:
            dw.record_codex = real
        self.assertEqual(seen, [None])
        self.assertIsNotNone(w.latest)
