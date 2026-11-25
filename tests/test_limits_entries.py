"""The limit check at every way of starting an agent, and what status / list_sessions / doctor show."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sampan import cli, limits, restart, restore, send, send_restore
from sampan.dispatch import Dispatch
from sampan.registry import Row, load_rows, write_rows
from sampan.session_run import cmd_new
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import TOML, make_env
from tests.test_resources import VM, sysctl_out

RED = {"comp": 700_000, "swap": "8400M"}  # compressor 66% and swap 49% of RAM: both high = red without a rate
CALM = {"comp": 100_000, "swap": "2000M"}
OWNERS = "\n[limits.owners]\nann = ['alice']\nbob = ['work']\n"
A = "aaaaaaaa-0000-4000-8000-000000000001"


class World:
    """A fake machine: n running sessions s0..s(n-1) (profile alice), memory per `mem`."""

    def __init__(self, test, running=2, mem=CALM, toml=TOML, stopped=()):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp, toml)
        self.cwd = self.tmp / "repo"
        self.cwd.mkdir()
        self.mem = mem
        self.running = running
        rows = [Row(f"s{i}", "claude", "alice", f"0000000{i}-0000-4000-8000-000000000000", "/c") for i in range(running)]
        rows += [Row(n, "claude", "alice", f"1111111{i}-0000-4000-8000-000000000000", str(self.cwd))
                 for i, n in enumerate(stopped)]
        write_rows(self.cfg.registry_path, rows, "t", self.cfg.history_dir)
        self.launched = []
        proj = self.cfg.default_profile.projects_dir / "-x"
        proj.mkdir(parents=True, exist_ok=True)
        for i, _ in enumerate(stopped):
            (proj / f"1111111{i}-0000-4000-8000-000000000000.jsonl").write_text("")

    def runner(self):
        r = FakeRunner(strict=True)
        ps = [ps_line(100 + i, 1, "zsh", "-zsh") for i in range(self.running)]
        ps += [ps_line(200 + i, 100 + i, "claude", "claude") for i in range(self.running)]
        panes = [pane_line(f"s{i}", f"%{i}", 100 + i, "claude", "/c") for i in range(self.running)]
        r.on(["ps", "-A"], cp(out="\n".join(ps) + "\n"))
        r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        r.on(["tmux", "-u", "new-session"], lambda a: (self.launched.append(a), cp(out="%50\n"))[1])
        r.on(["tmux", "-u", "has-session"], cp(rc=1))
        for sub in ("set-option", "send-keys", "capture-pane", "list-sessions", "load-buffer", "paste-buffer",
                    "delete-buffer", "display-message", "show-options"):
            r.on(["tmux", "-u", sub], cp())
        r.on(["ps", "-o", "lstart="], cp(out="Wed Nov 25 18:00:00 2026\n"))
        r.on(["ps", "eww"], cp())
        r.on(["codex"], cp(rc=1))
        r.on(["lsof"], cp())
        r.on(["sysctl"], cp(out=sysctl_out(swap_used=self.mem["swap"]).replace("vm.swapusage: ", "")))
        r.on(["vm_stat"], cp(out=VM.format(comp=self.mem["comp"], outs=0)))
        return r


class NewTest(unittest.TestCase):
    def new(self, w, r, **kw):
        out = []
        rc = cmd_new(w.cfg, r, out.append, "n1", "claude:alice", str(w.cwd), sleep=lambda _: None, timeout=1,
                     uuid4=lambda: A, **kw)
        return rc, out

    def test_over_sixteen_only_warns(self):
        w = World(self, running=17)
        rc, out = self.new(w, w.runner())
        self.assertTrue(any("已超過上限（18／16）" in x for x in out), out)
        self.assertNotEqual(rc, 2)
        self.assertEqual(len(w.launched), 1)

    def test_memory_red_refuses_and_creates_nothing(self):
        w = World(self, running=5, mem=RED)
        rc, out = self.new(w, w.runner())
        self.assertEqual(rc, 2)
        self.assertEqual(w.launched, [])
        self.assertTrue(any(x.startswith("❌ 不開 n1：記憶體紅燈") for x in out), out)
        self.assertNotIn("n1", [r.tmux for r in load_rows(w.cfg.registry_path)])

    def test_over_limit_flag_gets_through_and_is_logged(self):
        w = World(self, running=5, mem=RED)
        rc, out = self.new(w, w.runner(), over_limit=True)
        self.assertNotEqual(rc, 2)
        self.assertEqual(len(w.launched), 1)
        self.assertIn("已越過", (w.cfg.state_dir / "actions.log").read_text())

    def test_person_at_a_screen_can_say_yes(self):
        w = World(self, running=5, mem=RED)
        asked = []
        rc, out = self.new(w, w.runner(), ask_limit=lambda q: asked.append(q) or True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(w.launched), 1)

    def test_dry_run_reserves_nothing(self):
        w = World(self, running=2)
        self.new(w, w.runner(), dry_run=True)
        self.assertEqual(limits.read_reservations(w.cfg.state_dir), [])

    def test_guard_and_count_off_is_the_old_behaviour(self):
        w = World(self, running=30, mem=RED, toml=TOML + "\n[limits]\nmax_sessions = 0\npressure_guard = false\n")
        rc, out = self.new(w, w.runner())
        self.assertEqual(len(w.launched), 1)
        self.assertFalse(any("上限" in x for x in out))

    def test_block_mode_refuses_over_the_count(self):
        w = World(self, running=16, toml=TOML + "\n[limits]\ncount_mode = 'block'\n")
        rc, out = self.new(w, w.runner())
        self.assertEqual((rc, w.launched), (2, []))

    def test_second_new_sees_the_first_ones_place(self):
        w = World(self, running=3, toml=TOML + "\n[limits]\nmax_sessions = 4\ncount_mode = 'block'\n")
        r = w.runner()
        rc1, _ = self.new(w, r)
        out = []
        rc2 = cmd_new(w.cfg, r, out.append, "n2", "claude:alice", str(w.cwd), sleep=lambda _: None, timeout=1,
                      uuid4=lambda: A)
        self.assertEqual(rc2, 2)

    def test_failed_start_gives_the_place_back(self):
        w = World(self, running=2)
        r = w.runner()
        r.on(["tmux", "-u", "new-session"], cp(rc=1))
        rc, _ = self.new(w, r)
        self.assertEqual(rc, 1)
        self.assertEqual(limits.read_reservations(w.cfg.state_dir), [])


class RestoreTest(unittest.TestCase):
    def go(self, w, names, **kw):
        out = []
        rc = restore.cmd_restore(w.cfg, w.runner(), out.append, lambda _p: "", names=names, confirm=False,
                                 sleep=lambda _s: None, interval=0, timeout=0, **kw)
        return rc, out

    def test_red_skips_with_reason(self):
        w = World(self, running=5, mem=RED, stopped=("z1",))
        rc, out = self.go(w, ("z1",))
        self.assertEqual(w.launched, [])
        self.assertTrue(any("z1" in x and "超過上限，略過" in x for x in out), out)

    def test_replacing_goes_through_a_red_light(self):
        w = World(self, running=5, mem=RED, stopped=("z1",))
        rc, out = self.go(w, ("z1",), replacing=frozenset({"z1"}))
        self.assertEqual(len(w.launched), 1)
        self.assertIn("記憶體紅燈：重開會一個一個來", out)

    def test_calm_goes_through(self):
        w = World(self, running=2, stopped=("z1",))
        rc, out = self.go(w, ("z1",))
        self.assertEqual(len(w.launched), 1)


class RestartTest(unittest.TestCase):
    def test_restart_holds_places_and_passes_replacing(self):
        w = World(self, running=2)
        seen = {}

        def stop_fn(cfg, run, out, names, stopped, left, why, own_pane, timeout):
            seen["held"] = [r["name"] for r in limits.read_reservations(cfg.state_dir)]
            stopped.extend(names)
            return 0

        def restore_fn(cfg, run, out, ask, **kw):
            seen["kw"] = kw
            return 0

        plan = restart.RestartPlan(["s0", "s1"], ["z9"], [])
        restart.cmd_restart(w.cfg, w.runner(), lambda _l: None, lambda _p: "", plan, stop_fn=stop_fn, restore_fn=restore_fn)
        self.assertEqual(sorted(seen["held"]), ["s0", "s1"])
        self.assertEqual(seen["kw"]["replacing"], frozenset({"s0", "s1"}))

    def test_places_of_what_could_not_stop_are_let_go(self):
        w = World(self, running=2)

        def stop_fn(cfg, run, out, names, stopped, left, why, own_pane, timeout):
            left.append("s1")
            stopped.append("s0")
            return 1

        plan = restart.RestartPlan(["s0", "s1"], [], [])
        restart.cmd_restart(w.cfg, w.runner(), lambda _l: None, lambda _p: "", plan, stop_fn=stop_fn,
                            restore_fn=lambda *a, **k: 0)
        self.assertEqual([r["name"] for r in limits.read_reservations(w.cfg.state_dir)], ["s0"])


class SendRestoreTest(unittest.TestCase):
    row = None

    def setUp(self):
        from sampan.status import State, StatusRow
        self.row = StatusRow("z1", "claude", "alice", "sid", State.STOPPED)

    def test_decide_waits_for_room_only_at_the_front_of_the_line(self):
        called = []
        step = send_restore.decide(self.row, "", 0, others_active=0, rc=False,
                                   over_limit=lambda: called.append(1) or "執行中 4／上限 4")
        self.assertEqual((step.kind, step.reason), ("wait", "排隊等額度（執行中 4／上限 4）"))
        self.assertFalse(send_restore.waiting(step.reason))  # the 30 minute limit applies
        send_restore.decide(self.row, "", 0, others_active=2, rc=False, over_limit=lambda: called.append(2) or "x")
        self.assertEqual(called, [1])  # not asked when it waits for a place anyway
        self.assertEqual(send_restore.decide(self.row, "", 0, others_active=0, rc=False, over_limit=lambda: "").kind, "launch")
        self.assertEqual(send_restore.decide(self.row, "starting", 5, others_active=0, rc=False,
                                             over_limit=lambda: self.fail("asked")).kind, "wait")

    def dispatch(self, **extra):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return Dispatch("d1", "a", "alice", "claude", "z1", "claude", "alice", "t", "queued",
                        (now).isoformat(), reason=send_restore.REASON_LIMIT.format(why="x"), extra=extra)

    def test_queue_limit_counts_from_the_first_wait_for_room(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        d = self.dispatch()
        d.created = created.isoformat()
        now = created + timedelta(minutes=29)
        d.extra[send_restore.LIMIT_WAIT_AT] = (now - timedelta(seconds=5)).isoformat()
        send._expire(cfg, d, now + timedelta(minutes=2))  # 31 minutes after creation, 2 minutes after the first wait
        self.assertEqual(d.state, "queued")
        send._expire(cfg, d, now + timedelta(minutes=31))
        self.assertEqual(d.state, "failed")

    def test_final_check_refused_goes_back_to_waiting_not_failed(self):
        w = World(self, running=5, mem=RED, stopped=("z1",))
        from sampan import dispatch as dsp
        now = datetime.now(timezone.utc)
        from sampan.send import Caller
        d = send.submit_new(w.cfg, Caller("a", "alice", "claude", "%1", id="x"), "z1", "claude", "alice", "hi",
                            notify=False, now=now)
        with dsp.transaction(w.cfg.state_dir, keep_days=90, ids=[d.id]) as items:
            for x in items:
                x.state = "queued"
                x.extra = {send_restore.RESTORE: True, send_restore.PHASE: "launching", send_restore.LAUNCHES: 1}
        gate = send._launch_gate(w.cfg, w.runner(), "z1", "", "")
        self.assertFalse(gate.passed)
        send._back_to_waiting(w.cfg, [d.id], gate, now)
        [back] = dsp.load(w.cfg.state_dir)
        self.assertEqual((back.state, back.extra[send_restore.PHASE], back.extra[send_restore.LAUNCHES]),
                         ("queued", "", 0))
        self.assertTrue(back.reason.startswith("排隊等額度"))
        self.assertIn(send_restore.LIMIT_WAIT_AT, back.extra)

    def test_room_hint_is_cached_and_never_raises(self):
        w = World(self, running=5, mem=RED, stopped=("z1",))
        r = w.runner()
        t = [100.0]
        row = self.row
        a = limits.room_hint(w.cfg, r, row, [], clock=lambda: t[0])
        calls = len(r.calls)
        b = limits.room_hint(w.cfg, r, row, [], clock=lambda: t[0] + 1)
        self.assertEqual((a, len(r.calls)), (b, calls))
        self.assertTrue(a)


class ShowTest(unittest.TestCase):
    def test_footer_default_applies_without_a_limits_section(self):
        w = World(self, running=17)
        rep = limits.report(w.cfg, w.runner())
        self.assertEqual((rep.running, rep.limit, rep.over), (17, 16, 1))
        self.assertIn("執行中 17／上限 16（超過 1，只警告）", rep.footer())
        self.assertIn("記憶體：", rep.footer())

    def test_five_combinations_of_the_switches(self):
        base = TOML + "\n[limits]\n"
        for extra, shown, has_limit, red_blocks in (
                ("", True, True, True), ("count_mode = 'block'\n", True, True, True),
                ("pressure_guard = false\n", True, True, False),
                ("max_sessions = 0\n", True, False, True), ("max_sessions = 0\npressure_guard = false\n", False, False, False)):
            with self.subTest(extra):
                w = World(self, running=5, mem=RED, toml=base + extra)
                rep = limits.report(w.cfg, w.runner())
                self.assertEqual((rep.show, rep.limit is not None), (shown, has_limit))
                o = limits.check(w.cfg, w.runner(), name="x", profile="alice", write=False)
                self.assertEqual(o.verdict.kind == "refuse", red_blocks)

    def test_status_prints_the_footer_and_not_when_everything_is_off(self):
        from sampan.status import cmd_status
        for toml, want in ((TOML, True), (TOML + "\n[limits]\nmax_sessions = 0\npressure_guard = false\n", False)):
            w = World(self, running=2, toml=toml)
            out = []
            cmd_status(w.cfg, w.runner(), out.append)
            self.assertEqual(any("執行中 " in x and "tmux 外" in x for x in out), want, out)

    def test_report_json_shape(self):
        w = World(self, running=3, mem=RED)
        data = limits.report(w.cfg, w.runner()).as_json()
        self.assertEqual(data["memory"], "red")
        self.assertEqual(set(data["resources"]), {"ram_bytes", "swap_used_bytes", "swap_total_bytes", "compressor_pct",
                                                  "swapout_rate", "level", "load5", "cores", "at"})
        self.assertEqual(data["items"], {"compressor": "high", "swap": "high", "level": "green"})

    def test_doctor_lines(self):
        w = World(self, running=17)
        self.assertEqual([x[0] for x in limits.doctor_lines(w.cfg, w.runner())][:1], ["warn"])
        calm = World(self, running=2)
        self.assertEqual(limits.doctor_lines(calm.cfg, calm.runner()), [])

    def test_limits_command(self):
        w = World(self, running=3)
        out = []
        self.assertEqual(limits.cmd_limits(w.cfg, w.runner(), out.append, calibrate=True), 0)
        text = "\n".join(out)
        self.assertIn("預設值（設定檔沒有 [limits]）", text)
        self.assertIn("數量上限：16", text)
        self.assertIn("校正用", text)


class OverLimitCliTest(unittest.TestCase):
    def test_agent_or_no_terminal_may_not_override(self):
        import io
        import sys

        class Tty(io.StringIO):
            on = True

            def isatty(self):
                return self.on
        w = World(self, running=1)
        args = type("A", (), {"over_limit": True})()
        orig = cli._run_by_agent
        try:
            for agent, tty, ok in ((True, True, False), (False, False, False), (True, False, False), (False, True, True)):
                out = []
                cli._run_by_agent = lambda cfg, run, agent=agent: agent
                fake = Tty()
                fake.on = tty
                with mock.patch.object(sys, "stdin", fake), mock.patch.object(sys, "stdout", fake):
                    got = cli._over_limit(w.cfg, w.runner(), args, out.append)
                self.assertEqual(got, True if ok else None, (agent, tty))
                if not ok:
                    self.assertIn("--over-limit", out[0])
                    self.assertIn("畫面上按 y", out[0])
                    self.assertIn("背景", out[0])
        finally:
            cli._run_by_agent = orig
        self.assertFalse(cli._over_limit(w.cfg, w.runner(), type("A", (), {})(), []))

    def test_independent_of_mcp_confirm(self):
        # the agent test looks at who runs the command, not at [mcp] confirm
        import inspect
        self.assertNotIn("mcp_confirm", inspect.getsource(cli._over_limit))


class OwnersTest(unittest.TestCase):
    def test_red_keeps_each_owner_three(self):
        toml = TOML + "\n[limits]\n" + OWNERS.replace("\n[limits.owners]", "[limits.owners]")
        w = World(self, running=5, mem=RED, toml=toml)  # 5 running, all alice = ann
        o = limits.check(w.cfg, w.runner(), name="x", profile="work", write=False)
        self.assertTrue(o.ok)  # bob has none: his reserve of 3 is free
        o = limits.check(w.cfg, w.runner(), name="x", profile="alice", write=False)
        self.assertFalse(o.ok)


if __name__ == "__main__":
    unittest.main()


class SampanClaudeTest(unittest.TestCase):
    def test_only_warns_and_never_raises(self):
        w = World(self, running=17)
        out = []
        cli._warn_limits(w.cfg, w.runner(), out.append)
        self.assertEqual(len(out), 1)
        self.assertIn("已超過上限（17／16）", out[0])
        calm = World(self, running=2)
        out = []
        cli._warn_limits(calm.cfg, calm.runner(), out.append)
        self.assertEqual(out, [])
        cli._warn_limits(calm.cfg, None, out.append)  # a broken runner: silent
        self.assertEqual(out, [])


class _Terminal:
    pass


class OverLimitEveryCommandTest(unittest.TestCase):
    """--over-limit reaches new, restore, restart, move and switch, through the real caller_kind: a person at a
    terminal may use it, an agent (with [mcp] confirm on or off) is refused and nothing runs."""
    COMMANDS = (
        (["new", "x", "alice", "/tmp", "--over-limit"], "sampan.session_run.cmd_new"),
        (["restore", "x", "--over-limit"], "sampan.restore.cmd_restore"),
        (["restart", "x", "--over-limit"], "sampan.restart.cmd_restart"),
        (["move", "x", "/tmp", "--over-limit"], "sampan.session_run.cmd_move"),
        (["switch", "x", "work", "--over-limit"], "sampan.session_run.cmd_switch"),
    )

    def run_cli(self, argv, target, caller, confirm, tty=True):
        import dataclasses
        import io
        import sys
        from contextlib import redirect_stdout
        from unittest import mock

        from sampan.restart import RestartPlan
        cfg = dataclasses.replace(make_env(Path(tempfile.mkdtemp())), mcp_confirm=confirm)
        class Tty(io.StringIO):
            def isatty(self):
                return tty
        buf = Tty()
        with redirect_stdout(buf), mock.patch.object(sys, "stdin", Tty()), \
                mock.patch("sampan.cli.load_config", return_value=cfg), \
                mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.command_caller", return_value=caller), \
                mock.patch("sampan.cli._resolve_one", return_value="x"), \
                mock.patch("sampan.cli._resolve_refs", return_value=["x"]), \
                mock.patch("sampan.status.status_rows", return_value=[]), \
                mock.patch("sampan.restart.plan_restart", return_value=RestartPlan(["x"], [], [])), \
                mock.patch(target, return_value=0) as fn:
            rc = cli.main(argv)
        return rc, buf.getvalue(), fn

    def test_terminal_may_agent_may_not(self):
        from sampan.send import TERMINAL, Caller
        agent = Caller("a", "alice", "claude", "%1")
        for argv, target in self.COMMANDS:
            for confirm in (True, False):
                with self.subTest(argv[0], confirm=confirm):
                    rc, text, fn = self.run_cli(argv, target, TERMINAL, confirm)
                    self.assertEqual(fn.call_count, 1, text)
                    self.assertTrue(fn.call_args.kwargs.get("over_limit"), fn.call_args)
                    rc, text, fn = self.run_cli(argv, target, agent, confirm)
                    self.assertEqual((rc, fn.call_count), (2, 0), text)
                    self.assertIn("--over-limit", text)
                    self.assertIn("畫面上按 y", text)
                    # a person's command that lost its terminal (nohup, in the background) is refused as well
                    rc, text, fn = self.run_cli(argv, target, TERMINAL, confirm, tty=False)
                    self.assertEqual((rc, fn.call_count), (2, 0), text)
