"""Second round: what the first review found (lock purity, the real send --restore paths, the MCP sampler, owners)."""
import dataclasses
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sampan import dispatch as dsp
from sampan import limits, plugin_apply, resources, restore, send, send_restore, session_run
from sampan.mcp_server import Server
from sampan.registry import Row, write_rows
from sampan.send import Caller
from sampan.status import State
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import TOML, status_row
from tests.test_limits_entries import RED, World

BLOCK4 = TOML + "\n[limits]\nmax_sessions = 4\ncount_mode = 'block'\n"
OWNERS = TOML + "\n[limits]\nmax_sessions = 4\n[limits.owners]\nann = ['alice']\nbob = ['work']\n"


def watching(r, state):
    """r, but every command run while the reservation lock is held is recorded in state['bad']."""
    def run(args, **kw):
        if state["held"]:
            state["bad"].append(list(args))
        return r(args, **kw)
    return run


class LockPurity(unittest.TestCase):
    def test_nothing_is_spawned_while_the_reservation_lock_is_held(self):
        w = World(self, running=3, toml=BLOCK4)
        state = {"held": False, "bad": []}
        enter, leave = limits._Lock.__enter__, limits._Lock.__exit__

        def e(lock):
            out = enter(lock)
            state["held"] = True
            return out

        def x(lock, *a):
            state["held"] = False
            return leave(lock, *a)
        with mock.patch.object(limits._Lock, "__enter__", e), mock.patch.object(limits._Lock, "__exit__", x):
            o = limits.check(w.cfg, watching(w.runner(), state), name="n1", profile="alice", platform="darwin")
        self.assertTrue(o.reserved and o.reserved[1])  # the start time was read, outside
        self.assertEqual(state["bad"], [])

    def test_the_hint_runs_no_tmux_and_counts_a_valid_reservation(self):
        w = World(self, running=3, toml=BLOCK4, stopped=("z1",))
        limits.reserve(w.cfg.state_dir, "other", __import__("time").time(), 100)  # somebody's place, no agent yet
        r = w.runner()
        row = status_row("z1", State.STOPPED)
        limits._HINT.clear()
        why = limits.room_hint(w.cfg, r, row, [])
        self.assertTrue(why)  # 3 running + the reserved one = 4 = the limit
        self.assertFalse([c for c in r.calls if c["args"][:2] == ["tmux", "-u"] and "list-panes" in c["args"]])


class CheckNewLimit(unittest.TestCase):
    def test_mcp_asks_first_and_cmd_new_asks_again(self):
        w = World(self, running=5, mem=RED)
        r = w.runner()
        why = session_run.check_new(w.cfg, r, "n1", "claude:alice", str(w.cwd), limit=True)
        self.assertIsInstance(why, str)
        self.assertIn("記憶體紅燈", why)
        self.assertEqual(limits.read_reservations(w.cfg.state_dir), [])  # a look only
        self.assertEqual(session_run.check_new(w.cfg, r, "n1", "claude:alice", str(w.cwd))[0], "claude")
        out = []
        rc = session_run.cmd_new(w.cfg, r, out.append, "n1", "claude:alice", str(w.cwd), sleep=lambda _: None)
        self.assertEqual((rc, w.launched), (2, []))  # the second look, with the place, at cmd_new

    def test_docstring_no_longer_claims_to_be_before_the_prompt(self):
        self.assertNotIn("before it asks", session_run.check_new.__doc__)


class McpSampler(unittest.TestCase):
    def sampler(self, w, r):
        return SimpleNamespace(cfg=w.cfg, run=r)

    def test_samples_only_when_the_file_is_stale(self):
        w = World(self, running=2, mem=RED)
        r = w.runner()
        s = self.sampler(w, r)
        Server.sample_resources(s)  # no file: samples and writes
        first = len(r.calls)
        self.assertGreater(first, 0)
        self.assertIsNotNone(resources.state_age(w.cfg.state_dir))
        Server.sample_resources(s)  # fresh: one stat, no spawn
        self.assertEqual(len(r.calls), first)
        with mock.patch("sampan.resources.state_age", return_value=resources.STALE_S + 1):
            Server.sample_resources(s)
        self.assertGreater(len(r.calls), first)

    def test_the_watch_loop_calls_it(self):
        calls = []
        srv = SimpleNamespace(_stop=SimpleNamespace(wait=lambda _i, it=iter([False, True]): next(it)), sample_resources=lambda: calls.append(1),
                              watch_once=lambda: calls.append(2))
        Server._watch(srv, 3)
        self.assertEqual(calls, [1, 2])

    def test_nothing_to_sample_when_everything_is_off(self):
        w = World(self, running=2, toml=TOML + "\n[limits]\nmax_sessions = 5\npressure_guard = false\n")
        r = w.runner()
        Server.sample_resources(self.sampler(w, r))
        self.assertEqual(r.calls, [])


class SendRestoreReal(unittest.TestCase):
    def dispatch_in_phase(self, w, phase, launches=1):
        now = datetime.now(timezone.utc)
        d = send.submit_new(w.cfg, Caller("a", "alice", "claude", "%1", id="x"), "z1", "claude", "alice", "hi",
                            notify=False, now=now)
        with dsp.transaction(w.cfg.state_dir, keep_days=90, ids=[d.id]) as items:
            for x in items:
                x.state = "queued"
                x.extra = {send_restore.RESTORE: True, send_restore.PHASE: phase, send_restore.LAUNCHES: launches}
        return d.id

    def job(self, w, did):
        reg = next(r for r in __import__("sampan.registry", fromlist=["load_rows"]).load_rows(w.cfg.registry_path) if r.tmux == "z1")
        return ("launch", "z1", reg.id, [did], "")

    def test_a_refused_launch_goes_back_to_waiting_through_restore_work(self):
        w = World(self, running=5, mem=RED, stopped=("z1",))
        did = self.dispatch_in_phase(w, "launching")
        with mock.patch.object(send.restore_mod, "start_one") as start:
            send._restore_work(w.cfg, w.runner(), self.job(w, did), sleep=lambda _s: None, snapshot_fn=None,
                               status_fn=None, now=datetime.now(timezone.utc))
        start.assert_not_called()
        [d] = dsp.load(w.cfg.state_dir)
        self.assertEqual((d.state, d.extra[send_restore.PHASE], d.extra[send_restore.LAUNCHES]), ("queued", "", 0))
        self.assertTrue(d.reason.startswith("排隊等額度"))
        self.assertIn(send_restore.LIMIT_WAIT_AT, d.extra)

    def test_the_place_is_given_back_when_start_one_raises(self):
        w = World(self, running=2, stopped=("z1",))
        did = self.dispatch_in_phase(w, "launching")
        with mock.patch.object(send.restore_mod, "start_one", side_effect=OSError("boom")):
            send._restore_work(w.cfg, w.runner(), self.job(w, did), sleep=lambda _s: None, snapshot_fn=None,
                               status_fn=None, now=datetime.now(timezone.utc))
        self.assertEqual(limits.read_reservations(w.cfg.state_dir), [])
        [d] = dsp.load(w.cfg.state_dir)
        self.assertEqual(d.state, "failed")

    def test_the_place_is_given_back_when_start_one_says_no(self):
        w = World(self, running=2, stopped=("z1",))
        did = self.dispatch_in_phase(w, "launching")
        with mock.patch.object(send.restore_mod, "start_one", return_value="tmux 失敗"):
            send._restore_work(w.cfg, w.runner(), self.job(w, did), sleep=lambda _s: None, snapshot_fn=None,
                               status_fn=None, now=datetime.now(timezone.utc))
        self.assertEqual(limits.read_reservations(w.cfg.state_dir), [])

    def test_a_target_in_launching_does_not_count_twice_with_one_place_left(self):
        w = World(self, running=3, toml=BLOCK4, stopped=("z1",))
        self.dispatch_in_phase(w, "launching")
        self.assertEqual(limits.phase_names(w.cfg), ["z1"])  # the phase counts for everybody else...
        gate = send._launch_gate(w.cfg, w.runner(), "z1", "", "")
        self.assertTrue(gate.passed)  # ...but not for itself: 3 + 1 = 4 fits
        other = limits.check(w.cfg, w.runner(), name="n2", profile="alice", write=False)
        self.assertEqual(other.verdict.kind, "refuse")  # 3 + z1 in launching + n2 = 5

    def test_resend_forgets_the_wait_for_room(self):
        w = World(self, running=2, stopped=("z1",))
        did = self.dispatch_in_phase(w, "")
        with dsp.transaction(w.cfg.state_dir, keep_days=90, ids=[did]) as items:
            for x in items:
                x.state = "failed"
                x.reason = "x"
                x.extra[send_restore.LIMIT_WAIT_AT] = "2026-01-01T00:00:00+00:00"
        send.resend(w.cfg, did, now=datetime.now(timezone.utc))
        [d] = dsp.load(w.cfg.state_dir)
        self.assertNotIn(send_restore.LIMIT_WAIT_AT, d.extra)
        self.assertIn(send.REQUEUED_AT, d.extra)

    def test_a_count_warning_is_said_beside_the_reason(self):
        w = World(self, running=17, stopped=("z1",))
        limits._HINT.clear()
        limits.room_hint(w.cfg, w.runner(), status_row("z1", State.STOPPED), [])
        self.assertIn("已超過上限", limits.hint_warning(w.cfg, status_row("z1", State.STOPPED)))


class Concurrency(unittest.TestCase):
    def test_two_callers_racing_for_the_last_place_only_one_gets_it(self):
        for _ in range(5):
            w = World(self, running=3, toml=BLOCK4)
            results = []
            barrier = threading.Barrier(2)

            def go(name):
                r = w.runner()
                barrier.wait()
                results.append(limits.check(w.cfg, r, name=name, profile="alice", platform="darwin").ok)
            ts = [threading.Thread(target=go, args=(n,)) for n in ("n1", "n2")]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(sorted(results), [False, True])


class RestartProfiles(unittest.TestCase):
    def test_holds_places_lets_go_and_says_so_under_a_red_light(self):
        w = World(self, running=3, mem=RED)
        seen = {}
        rows = [status_row(n, State.RUNNING, profile="alice", pane=f"%{i}") for i, n in enumerate(("s0", "s1", "s2"))]

        def stop_fn(cfg, run, out, **kw):
            seen["held"] = sorted(x["name"] for x in limits.read_reservations(cfg.state_dir))
            kw["stopped"].append("s0")
            kw["left"].append("s1")
            return 0

        def restore_fn(cfg, run, out, ask, **kw):
            seen["kw"] = kw
            return 0
        out = []
        with mock.patch("sampan.status.status_rows", return_value=rows):
            plugin_apply.restart_profiles(w.cfg, w.runner(), out.append, lambda _p: "", ["alice"], own_pane="%2",
                                          stop_fn=stop_fn, restore_fn=restore_fn)
        self.assertEqual(seen["held"], ["s0", "s1"])  # not the pane sampan runs in
        self.assertEqual(seen["kw"]["replacing"], frozenset({"s0"}))
        self.assertEqual([x["name"] for x in limits.read_reservations(w.cfg.state_dir)], ["s0"])  # s1 kept running
        self.assertIn("記憶體紅燈：重開會一個一個來；要晚一點再按 R 也可以", out)


class RestoreOrderAndWait(unittest.TestCase):
    def test_the_most_recently_active_come_back_first_when_room_is_short(self):
        w = World(self, running=3, toml=BLOCK4, stopped=("z1", "z2"))
        dated = {"z1": datetime(2026, 1, 1, tzinfo=timezone.utc), "z2": datetime(2026, 1, 9, tzinfo=timezone.utc)}
        real = restore.candidates

        def cands(*a, **k):
            return [dataclasses.replace(c, last=dated[c.row.tmux]) for c in real(*a, **k)]
        out = []
        with mock.patch.object(restore, "candidates", cands):
            restore.cmd_restore(w.cfg, w.runner(), out.append, lambda _p: "", names=("z1", "z2"), confirm=False,
                                sleep=lambda _s: None, interval=0, timeout=0)
        self.assertEqual(len(w.launched), 1)  # one place: z2, the newer, took it
        self.assertTrue(any("z1" in x and "超過上限，略過" in x for x in out), out)
        self.assertFalse(any("z2" in x and "超過上限" in x for x in out), out)

    def test_wait_up_recognises_codex(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("cx", "%7", 70, "codex", "/c") + "\n"))
        r.on(["ps", "-A"], cp(out=ps_line(70, 1, "zsh", "-zsh") + "\n" + ps_line(71, 70, "codex", "codex resume x") + "\n"))
        slept = []
        self.assertTrue(restore._wait_up(r, "%7", slept.append))
        self.assertEqual(slept, [])  # up at once: no 60 s of waiting


class Owners(unittest.TestCase):
    def test_without_owners_nobody_elses_session_is_offered(self):
        w = World(self, running=17)
        self.assertEqual(limits.candidates(w.cfg, w.runner(), ""), [])
        o = limits.check(w.cfg, w.runner(), name="n", profile="alice", write=False)
        self.assertIn("分不出哪些是你的", " ".join(o.lines("n")))

    def test_unknown_idle_time_goes_last_and_oldest_first(self):
        c = [limits.Candidate("a", "idle", None, 1), limits.Candidate("b", "idle", "2026-01-02", 1),
             limits.Candidate("c", "idle", "2026-01-01", 1), limits.Candidate("d", "retired")]
        c.sort(key=lambda x: (x.why != "retired", x.idle_since is None, str(x.idle_since or ""), -(x.tokens or 0)))
        self.assertEqual([x.name for x in c], ["d", "c", "b", "a"])

    def test_candidates_with_owners_are_only_your_own(self):
        w = World(self, running=3, toml=OWNERS)
        write_rows(w.cfg.registry_path, [Row("a1", "claude", "alice", "a" * 8 + "-0000-4000-8000-000000000000", "/c"),
                                         Row("b1", "claude", "work", "b" * 8 + "-0000-4000-8000-000000000000", "/c")],
                   "t", w.cfg.history_dir)
        rows = [status_row("a1", State.RUNNING, agent_status="idle", profile="alice"),
                status_row("b1", State.RUNNING, agent_status="idle", profile="work")]
        with mock.patch("sampan.status.status_rows", return_value=rows):
            got = limits.candidates(w.cfg, w.runner(), "ann", env={})
        self.assertEqual([c.name for c in got], ["a1"])

    def test_the_override_message_does_not_contradict_itself(self):
        w = World(self, running=5, mem=RED)
        out = []
        self.assertTrue(limits.gate(w.cfg, w.runner(), out.append, name="x", profile="alice", over_limit=True))
        self.assertEqual(len(out), 1)
        self.assertIn("已越過", out[0])
        self.assertNotIn("不開", out[0])


class ConfigTolerance(unittest.TestCase):
    def test_an_unknown_key_is_ignored_and_the_doctor_says_so(self):
        w = World(self, running=2, toml=TOML + "\n[limits]\nfuture_key = 1\nmax_sessions = 9\n")
        self.assertEqual((w.cfg.limits.max_sessions, w.cfg.limits.unknown), (9, ("future_key",)))
        self.assertEqual(limits.doctor_lines(w.cfg, w.runner())[0][:2], ("warn", "[limits] 有這個版本不認得的鍵：future_key（已忽略）"))

    def test_the_old_keys_still_point_at_the_new_ones(self):
        from sampan.config import ConfigError
        with self.assertRaises(ConfigError) as cm:
            World(self, running=0, toml=TOML + "\n[limits]\nenforce = 'off'\n")
        self.assertIn("count_mode", str(cm.exception))

    def test_limits_command_prints_no_traceback(self):
        w = World(self, running=2)
        out = []
        with mock.patch.object(limits, "report", side_effect=RuntimeError("x")):
            self.assertEqual(limits.cmd_limits(w.cfg, w.runner(), out.append), 2)
        self.assertIn("❌", out[0])


if __name__ == "__main__":
    unittest.main()


class AgentCannotAnswerY(unittest.TestCase):
    """`printf 'y\\n' | sampan ui --plain` from an agent must not get past the limit."""

    def cli_kwargs(self, *, agent, tty):
        import io
        import sys
        from contextlib import redirect_stdout

        from sampan import cli
        cfg = make_env_for_cli()
        seen = {}
        class Tty(io.StringIO):
            def isatty(self):
                return tty
        stdin = Tty("y\n")
        with redirect_stdout(Tty()), mock.patch("sampan.cli.load_config", return_value=cfg), \
                mock.patch("sampan.migrate.ensure", return_value=0), mock.patch("sampan.cli._upgrade_notice"), \
                mock.patch("sampan.cli._run_by_agent", return_value=agent), \
                mock.patch("sampan.cli._current_tmux_session", return_value=None), \
                mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.ui.cmd_ui", side_effect=lambda *a, **k: seen.update(k) or 0), \
                mock.patch.object(sys, "stdin", stdin):
            cli.main(["ui", "--plain"])
        return seen

    def test_an_agent_gets_no_question_even_on_a_terminal(self):
        self.assertFalse(self.cli_kwargs(agent=True, tty=True)["limit_override"])

    def test_no_terminal_no_question_even_for_a_person(self):
        self.assertFalse(self.cli_kwargs(agent=False, tty=False)["limit_override"])

    def test_a_person_at_a_terminal_may(self):
        self.assertTrue(self.cli_kwargs(agent=False, tty=True)["limit_override"])

    def test_end_to_end_the_refusal_stands_when_y_is_piped_in(self):
        from sampan.ui.actions import cmd_ui
        for allowed, launched in ((True, 1), (False, 0)):
            w = World(self, running=5, mem=RED, stopped=("z1",))
            rows = [status_row("z1", State.STOPPED)]
            answers = iter(["1"])
            asked = []

            def ask(prompt, answers=answers, asked=asked):
                if "仍要開" in prompt:
                    asked.append(prompt)
                    return "y"
                return next(answers)
            out = []
            cmd_ui(w.cfg, w.runner(), out.append, ask, rows_fn=lambda c, r, rows=rows: rows,
                   exec_fn=lambda *a: None, restore_fn=restore.cmd_restore, loop=False, limit_override=allowed)
            self.assertEqual(len(w.launched), launched, (allowed, out))
            self.assertEqual(len(asked), 1 if allowed else 0)
            if not allowed:
                self.assertTrue(any("--over-limit" in x for x in out), out)

    def test_batch_switch_text_does_not_promise_a_y_and_the_flag_reaches_it(self):
        w = World(self, running=5, mem=RED, stopped=("z1",))
        out = []
        self.assertFalse(limits.gate(w.cfg, w.runner(), out.append, name="z1", profile="alice"))
        text = "\n".join(out)
        self.assertIn("--over-limit", text)
        self.assertIn("單一 session", text)
        self.assertNotIn("或在畫面上按 y", text)
        from sampan import switch_many
        seen = []
        with mock.patch("sampan.switch_many.status_rows", return_value=[status_row("z1", State.STOPPED)]):
            switch_many.cmd_switch_many(w.cfg, w.runner(), out.append, None, ["z1"], "work", yes=True,
                                        over_limit=True, own_pane=None,
                                        switch_fn=lambda *a, **k: seen.append(k) or 0)
        self.assertTrue(seen and seen[0].get("over_limit"))


def make_env_for_cli():
    from tests.helpers import make_env
    return make_env(Path(tempfile.mkdtemp()))


class Backoffs(unittest.TestCase):
    def test_backoff_doubles_to_five_minutes_and_one_success_resets(self):
        t = [0.0]
        b = resources.Backoff(clock=lambda: t[0])
        self.assertTrue(b.ready())
        delays = []
        for _ in range(6):
            b.failed()
            delays.append(b.delay())
            self.assertFalse(b.ready())
            t[0] = b.next_at
            self.assertTrue(b.ready())
        self.assertEqual(delays, [30, 60, 120, 240, 300, 300])
        b.ok()
        self.assertEqual((b.delay(), b.ready()), (0, True))

    def test_a_machine_that_cannot_be_read_is_not_asked_every_round(self):
        w = World(self, running=2)
        r = FakeRunner(strict=True)  # sysctl and vm_stat both fail (no rule: unreadable)
        t = [0.0]
        srv = SimpleNamespace(cfg=w.cfg, run=r, res_backoff=resources.Backoff(clock=lambda: t[0]))
        for _ in range(5):  # five 3-second rounds
            Server.sample_resources(srv)
            t[0] += 3
        spawns = len([c for c in r.calls if c["args"][0] in ("sysctl", "vm_stat")])
        self.assertEqual(spawns, 2)  # one try (two spawns), then it waits
        t[0] += 40
        Server.sample_resources(srv)
        self.assertEqual(len([c for c in r.calls if c["args"][0] in ("sysctl", "vm_stat")]), 4)
        t[0] += 40  # only 40 s after the second failure: it now waits 60
        Server.sample_resources(srv)
        self.assertEqual(len([c for c in r.calls if c["args"][0] in ("sysctl", "vm_stat")]), 4)
        self.assertTrue(all(c["timeout"] == 2 for c in r.calls))

    def test_a_state_file_that_cannot_be_written_counts_as_a_failure_too(self):
        w = World(self, running=2)
        t = [0.0]
        srv = SimpleNamespace(cfg=w.cfg, run=w.runner(), res_backoff=resources.Backoff(clock=lambda: t[0]))
        with mock.patch.object(resources, "_write_state", lambda *a, **k: None):
            Server.sample_resources(srv)
        self.assertEqual(srv.res_backoff.failures, 1)
        Server.sample_resources(srv)  # not ready: nothing
        self.assertEqual(srv.res_backoff.failures, 1)
        t[0] += 31
        Server.sample_resources(srv)  # the file can be written now
        self.assertEqual(srv.res_backoff.failures, 0)

    def test_the_screen_watcher_backs_off_too(self):
        from sampan.ui import resource_bar as rb
        w = World(self, running=2)
        t = [0.0]
        watcher = rb.ResourceWatcher(w.cfg, FakeRunner(strict=True), clock=lambda: t[0])
        for _ in range(3):
            watcher.step()
        self.assertGreater(watcher.backoff.delay(), rb.FAST_S)
        self.assertEqual(watcher.backoff.failures, 3)

    def test_plain_line_spawns_have_the_two_second_timeout(self):
        from sampan.ui import resource_bar as rb
        w = World(self, running=2)
        r = w.runner()
        rb.plain_line_now(w.cfg, r, [])
        self.assertTrue(r.calls and all(c["timeout"] == 2 for c in r.calls))

    def test_the_lock_file_is_private(self):
        d = tempfile.mkdtemp()
        resources._write_state(d, {"t": 1.0, "swapouts": 1, "boot": 1.0}, None, 1.0, 1.0)
        self.assertEqual((Path(d) / "resources.lock").stat().st_mode & 0o777, 0o600)


class RestartLetsGo(unittest.TestCase):
    def test_an_early_return_or_an_exception_releases_every_place(self):
        from sampan import restart
        for how in ("early", "raises"):
            w = World(self, running=3)

            def stop_fn(cfg, run, out, **kw):
                if how == "raises":
                    raise OSError("tmux gone")
                return 2  # e.g. nothing found: stopped and left stay empty
            plan = restart.RestartPlan(["s0", "s1"], [], [])
            try:
                restart.cmd_restart(w.cfg, w.runner(), lambda _l: None, lambda _p: "", plan, stop_fn=stop_fn,
                                    restore_fn=lambda *a, **k: 0)
            except OSError:
                pass
            self.assertEqual(limits.read_reservations(w.cfg.state_dir), [], how)


class Docs(unittest.TestCase):
    def test_the_command_row_is_in_the_command_table_in_both_languages(self):
        root = Path(__file__).resolve().parent.parent / "docs"
        for path, start, end, row in ((root / "usage.md", "## 指令一覽", "## 資料夾", "`sampan limits [calibrate]`"),
                                      (root / "en" / "usage.md", "## Commands", "## Folders", "`sampan limits [calibrate]`")):
            text = path.read_text()
            section = text[text.index(start):text.index(end)]
            self.assertIn(row, section, path)
            self.assertEqual(text.count(row), 1, path)

    def test_the_help_page_tells_the_red_conditions_as_the_code_does(self):
        from sampan.ui.help import GAUGE_GROUP
        text = " ".join(w for _k, w in GAUGE_GROUP[1])
        for needle in ("同時偏高", "10 分鐘", "正在寫出", "<15%"):
            self.assertIn(needle, text)
