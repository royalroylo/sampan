import tempfile
import threading
import time
import unittest
from dataclasses import replace
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
        return DaemonWatcher(self.cfg, FakeRunner(strict=True), snapshot_fn=fn, now_fn=lambda: NOW,
                             start_fn=lambda home, run: "測試不啟動")

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
        self.assertIn(self.cfg.codex.home, self.calls)  # a failed auto-start re-reads once more
        self.assertEqual(set(self.calls), {self.cfg.codex.home})
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


class AutoStartTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()), TWO_CODEX)
        self.starts, self.reads = [], 0

    def make(self, snaps, start_result=None, start_fn=None, cfg=None):
        seq = list(snaps)

        def snapshot_fn(home, run, *, now, **_kw):
            self.reads += 1
            return seq.pop(0) if len(seq) > 1 else seq[0]

        def fake_start(home, run):
            self.starts.append((home, run))
            if isinstance(start_result, BaseException):
                raise start_result
            return start_result
        self.run = FakeRunner(strict=True)
        return DaemonWatcher(cfg or self.cfg, self.run, snapshot_fn=snapshot_fn, now_fn=lambda: NOW,
                             start_fn=start_fn or fake_start, retry_wait=0.01)

    def test_first_read_starts_once_when_down_then_rereads(self):
        s = snap(self.cfg.codex.home)
        w = self.make([None, s])
        w.first_read()
        self.assertEqual(self.starts, [(self.cfg.codex.home, self.run)])
        self.assertIs(w.latest, s)
        self.assertIsNone(w.start_state)
        self.assertEqual(self.reads, 2)

    def test_first_read_failed_start_keeps_reason(self):
        w = self.make([None], "找不到 codex 指令")
        w.first_read()
        self.assertEqual((w.start_state, w.start_error), ("failed", "找不到 codex 指令"))
        self.assertIsNone(w.latest)

    def test_failed_start_still_rereads(self):
        s = snap(self.cfg.codex.home)
        w = self.make([None, s], "boom")
        w.first_read()
        self.assertEqual(self.reads, 2)
        self.assertIs(w.latest, s)
        self.assertIsNone(w.start_state)  # the daemon is up anyway: no failed note

    def test_failed_start_and_still_down_keeps_failed(self):
        w = self.make([None], "boom")
        w.first_read()
        self.assertEqual((w.start_state, w.start_error, self.reads), ("failed", "boom", 2))

    def test_old_failed_does_not_return_after_recovery(self):
        s = snap(self.cfg.codex.home)
        w = self.make([None, None, s, None], "boom")
        w.first_read()
        self.assertEqual(w.start_state, "failed")
        w.poll_once()  # recovered
        self.assertIsNone(w.start_state)
        w.poll_once()  # dies again
        self.assertIsNone(w.latest)
        self.assertIsNone(w.start_state)

    def test_ok_start_with_socket_not_ready_gets_one_extra_read(self):
        s = snap(self.cfg.codex.home)
        w = self.make([None, None, s])
        seen = []
        orig = w.snapshot_fn
        w.snapshot_fn = lambda *a, **k: (seen.append(w.start_state), orig(*a, **k))[1]
        w.first_read()
        self.assertEqual(self.reads, 3)
        self.assertIs(w.latest, s)
        self.assertEqual(seen, [None, "starting", "starting"])
        self.assertIsNone(w.start_state)

    def test_extra_read_happens_only_once(self):
        w = self.make([None])
        w.first_read()
        self.assertEqual(self.reads, 3)

    def test_stop_during_the_extra_wait_returns_fast_and_skips_the_read(self):
        w = self.make([None])
        w.retry_wait = 10
        w.start()
        deadline = time.monotonic() + 2
        while self.reads < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.reads, 2)
        t0 = time.monotonic()
        w.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        time.sleep(0.1)
        self.assertEqual(self.reads, 2)
        self.assertIsNone(w.start_state)

    def test_first_read_start_ok_but_still_down_does_not_retry(self):
        w = self.make([None])
        w.first_read()
        self.assertEqual(len(self.starts), 1)
        self.assertIsNone(w.start_state)
        w.first_read()
        w.poll_once()
        self.assertEqual(len(self.starts), 1)

    def test_first_read_no_start_when_up(self):
        self.make([snap(self.cfg.codex.home)]).first_read()
        self.assertEqual(self.starts, [])

    def test_first_read_no_start_when_auto_start_off(self):
        cfg = replace(self.cfg, codex_auto_start=False)
        w = self.make([None], cfg=cfg)
        w.first_read()
        self.assertEqual(self.starts, [])
        self.assertIsNone(w.start_state)

    def test_first_read_no_start_when_codex_disabled(self):
        w = self.make([None])
        w.cfg = make_env(Path(tempfile.mkdtemp()), TWO_CODEX.replace('agent = "codex"', 'agent = "codex"\ndisabled = true'))
        self.assertFalse(w.cfg.codex.enabled)
        w.first_read()
        self.assertEqual(self.starts, [])

    def test_start_state_starting_while_start_runs(self):
        seen = []

        def start_fn(home, run):
            seen.append(w.start_state)
        w = self.make([None], start_fn=start_fn)
        w.first_read()
        self.assertEqual(seen, ["starting"])

    def test_start_error_is_set_before_failed_state(self):
        order = []

        class W(DaemonWatcher):
            def __setattr__(self, k, v):
                if k in ("start_error", "start_state"):
                    order.append((k, v))
                super().__setattr__(k, v)
        w = self.make([None], "boom")
        w.__class__ = W
        order.clear()
        w.first_read()
        self.assertEqual(order[-2:], [("start_error", "boom"), ("start_state", "failed")])

    def test_state_stays_starting_until_reread_finished(self):
        seen = []
        s = snap(self.cfg.codex.home)
        w = self.make([None, s])
        orig = w.snapshot_fn

        def snapshot_fn(*a, **k):
            seen.append(w.start_state)
            return orig(*a, **k)
        w.snapshot_fn = snapshot_fn
        w.first_read()
        self.assertEqual(seen, [None, "starting"])
        self.assertIsNone(w.start_state)

    def test_stop_returns_fast_while_start_blocks(self):
        gate = threading.Event()
        entered = threading.Event()

        def start_fn(home, run):
            entered.set()
            gate.wait(10)
        w = self.make([None], start_fn=start_fn)
        w.start()
        self.assertTrue(entered.wait(2))
        t0 = time.monotonic()
        w.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        gate.set()

    def test_first_read_swallows_start_exception(self):
        w = self.make([None], RuntimeError("boom"))
        w.first_read()
        self.assertEqual((w.start_state, w.start_error), ("failed", "boom"))


if __name__ == "__main__":
    unittest.main()
