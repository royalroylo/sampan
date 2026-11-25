import tempfile
import time
import unittest
from pathlib import Path

from sampan import limits
from sampan.config import ConfigError, Limits
from sampan.procs import Proc
from sampan.registry import Row, write_rows
from sampan.tmux import Pane
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import TOML, make_env
from tests.test_resources import VM, sysctl_out

OWNERS = TOML + """
[limits.owners]
roy = ["alice"]
mel = ["work"]
"""


def P(pid, ppid, comm="claude", args="claude", stat="S"):
    return Proc(pid, ppid, stat, comm, args)


def count_for(cfg, procs, panes=(), rows=(), res=(), phases=(), exclude=()):
    return limits.count(cfg, {p.pid: p for p in procs}, list(panes), list(rows), list(res), list(phases), exclude=exclude)


class Config(unittest.TestCase):
    def load(self, extra):
        return make_env(Path(tempfile.mkdtemp()), TOML + extra).limits

    def test_default_without_section(self):
        lim = make_env(Path(tempfile.mkdtemp())).limits
        self.assertEqual((lim.max_sessions, lim.count_mode, lim.pressure_guard, lim.explicit), ("auto", "warn", True, False))

    def test_owners(self):
        lim = self.load("\n[limits]\nmax_sessions = 10\n[limits.owners]\nroy = [\"alice\"]\n")
        self.assertEqual((lim.max_sessions, lim.owner_of("alice"), lim.owner_of("zzz")), (10, "roy", ""))

    def test_errors(self):
        for text, word in (("[limits]\nenforce = 'off'\n", "count_mode"), ("[limits]\nmin_sessions = 3\n", "min_auto"),
                           ("[limits]\ncount_mode = 'x'\n", "count_mode"), ("[limits]\nmax_sessions = -1\n", "max_sessions"),
                           ("[limits]\nfoo = 1\n", "foo"), ("[limits.owners]\nreserve_per_owner = 3\n", "reserve_per_owner"),
                           ("[limits.owners]\na = ['alice']\nb = ['alice']\n", "alice"),
                           ("[limits]\npressure_guard = 1\n", "pressure_guard")):
            with self.subTest(text), self.assertRaises(ConfigError) as cm:
                self.load("\n" + text)
            self.assertIn(word, str(cm.exception))


class Formula(unittest.TestCase):
    def test_auto(self):
        lim = Limits()
        for gib, want in ((8, 4), (16, 16), (32, 48), (4, 4), (1024, 64)):
            self.assertEqual(limits.effective_max(lim, gib << 30), want, gib)
        self.assertEqual(limits.effective_max(lim, None), 4)

    def test_zero_and_number(self):
        self.assertIsNone(limits.effective_max(Limits(max_sessions=0), 16 << 30))
        self.assertEqual(limits.effective_max(Limits(max_sessions=7), 16 << 30), 7)


class Count(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()), OWNERS)

    def test_top_level_only_and_short_commands(self):
        procs = [P(10, 1), P(11, 10, "claude", "claude"), P(12, 10, "node", "node x"), P(20, 1, "claude", "claude plugin list"),
                 P(21, 1, "claude", "claude -p hi"), P(22, 1, "claude", "claude mcp serve"), P(23, 1, "claude", "claude", "Z"),
                 P(24, 1, "claude", "claude --resume x")]
        c = count_for(self.cfg, procs)
        self.assertEqual((c.total, c.outside), (2, 2))

    def test_panes_owners_and_outside(self):
        procs = [P(10, 100), P(11, 101), P(12, 1)]
        panes = [Pane("a", "%1", 100, "claude", "/"), Pane("b", "%2", 101, "claude", "/")]
        rows = [Row("a", "claude", "alice", "i1", "/"), Row("b", "claude", "work", "i2", "/")]
        c = count_for(self.cfg, procs, panes, rows)
        self.assertEqual((c.total, c.by_owner, c.outside), (3, {"roy": 1, "mel": 1, "": 1}, 1))

    def test_codex_interactive_counts_daemon_does_not(self):
        procs = [P(30, 300, "codex", "codex resume x"), P(31, 1, "codex", "codex app-server --listen x")]
        panes = [Pane("cx", "%5", 300, "codex", "/")]
        self.assertEqual(count_for(self.cfg, procs, panes).total, 1)

    def test_reserved_and_phase_once_and_only_without_agent(self):
        procs = [P(10, 100)]
        panes = [Pane("a", "%1", 100, "claude", "/")]
        c = count_for(self.cfg, procs, panes, res=["a", "n1", "n2"], phases=["n1", "n3"])
        self.assertEqual(c.total, 4)  # a (running; reservation ignored), n1 once, n2, n3
        self.assertEqual(count_for(self.cfg, procs, panes, res=["n1"], exclude=("n1",)).total, 1)

    def test_unreadable_tmux_counts_outside(self):
        c = limits.count(self.cfg, {10: P(10, 100)}, None, [], [], [])
        self.assertEqual((c.total, c.outside), (1, 1))


def cnt(by_owner):
    return limits.Count((), sum(by_owner.values()), dict(by_owner), 0, frozenset())


class Decide(unittest.TestCase):
    lim = Limits(owners=(("roy", ("alice",)), ("mel", ("work",))))

    def test_warn_over_limit(self):
        v = limits.decide(cnt({"roy": 22}), Limits(), 16, "roy", 1, False)
        self.assertEqual((v.kind, v.why, v.over), ("warn", "count", True))
        self.assertEqual(limits.decide(cnt({"roy": 10}), Limits(), 16, "roy", 1, False).kind, "ok")

    def test_block_mode(self):
        lim = Limits(count_mode="block")
        self.assertEqual(limits.decide(cnt({"": 16}), lim, 16, "", 1, False).kind, "refuse")
        self.assertEqual(limits.decide(cnt({"": 15}), lim, 16, "", 1, False).kind, "ok")

    def test_block_leaves_other_owners_reserve(self):
        lim = Limits(count_mode="block", owners=self.lim.owners)
        # 15 running all roy; mel has 0 and 3 reserved: roy's 16th would eat mel's reserve
        self.assertEqual(limits.decide(cnt({"roy": 12}), lim, 16, "roy", 1, False).kind, "ok")
        self.assertEqual(limits.decide(cnt({"roy": 13}), lim, 16, "roy", 1, False).kind, "refuse")
        self.assertEqual(limits.decide(cnt({"roy": 14}), lim, 16, "mel", 1, False).kind, "ok")

    def test_red_with_owners(self):
        d = lambda by, owner, adds=1, **kw: limits.decide(cnt(by), self.lim, 16, owner, adds, True, **kw).kind  # noqa: E731
        self.assertEqual(d({"roy": 4}, "mel"), "ok")
        self.assertEqual(d({"roy": 4, "mel": 2}, "mel"), "ok")
        self.assertEqual(d({"roy": 4, "mel": 3}, "mel"), "refuse")
        self.assertEqual(d({"roy": 3}, "roy"), "refuse")
        self.assertEqual(d({"roy": 2}, "roy"), "ok")
        self.assertEqual(d({}, ""), "refuse")  # others have no reserve under red

    def test_red_without_owners_uses_floor(self):
        v = lambda total, **kw: limits.decide(cnt({"": total}), Limits(**kw), 16, "", 1, True).kind  # noqa: E731
        self.assertEqual((v(3), v(4), v(3, red_floor=3)), ("ok", "refuse", "refuse"))

    def test_replacement_adds_nothing(self):
        self.assertEqual(limits.decide(cnt({"roy": 30}), Limits(count_mode="block"), 16, "roy", 0, True).kind, "ok")

    def test_pressure_guard_off(self):
        lim = Limits(pressure_guard=False)
        self.assertEqual(limits.decide(cnt({"": 30}), lim, 16, "", 1, True).kind, "warn")

    def test_no_count_limit_still_red(self):
        self.assertEqual(limits.decide(cnt({"": 30}), Limits(max_sessions=0), None, "", 1, False).kind, "ok")
        self.assertEqual(limits.decide(cnt({"": 30}), Limits(max_sessions=0), None, "", 1, True).kind, "refuse")


class Reservations(unittest.TestCase):
    def test_valid_only_unexpired_without_agent(self):
        res = [{"name": "a", "expires": 200}, {"name": "b", "expires": 50}, {"name": "c", "expires": 200}]
        self.assertEqual(limits.valid_reservations(res, 100, {"c"}), ["a"])

    def test_dead_writer_is_still_valid(self):
        res = [{"name": "a", "pid": 99999999, "start": "x", "expires": 200}]
        self.assertEqual(limits.valid_reservations(res, 100, set()), ["a"])

    def test_release_checks_pid_and_start(self):
        d = tempfile.mkdtemp()
        limits.reserve(d, "a", time.time(), 100, pid=5, start="s1")
        limits.release(d, "a", pid=5, start="other")  # a reused pid: not ours
        self.assertEqual(len(limits.read_reservations(d)), 1)
        limits.release(d, "a", pid=5, start="s1")
        self.assertEqual(limits.read_reservations(d), [])

    def test_lock_timeout_does_not_block(self):
        d = tempfile.mkdtemp()
        with limits._Lock(d) as one:
            self.assertTrue(one.held)
            with limits._Lock(d, wait=0.1) as two:
                self.assertFalse(two.held)


class Check(unittest.TestCase):
    """limits.check end to end with a fake ps / tmux / sysctl."""

    def build(self, running=3, comp=100_000, outs=0, toml=TOML, swap="2000M"):
        cfg = make_env(Path(tempfile.mkdtemp()), toml)
        write_rows(cfg.registry_path, [Row(f"s{i}", "claude", "alice", f"0000000{i}-0000-4000-8000-000000000000", "/c")
                                       for i in range(running)], "t", cfg.history_dir)
        ps = [ps_line(100 + i, 1, "zsh", "-zsh") + "" for i in range(running)]
        ps = [ps_line(200 + i, 100 + i, "claude", "claude") for i in range(running)] + ps
        panes = [pane_line(f"s{i}", f"%{i}", 100 + i, "claude", "/c") for i in range(running)]
        r = FakeRunner()
        r.on(["ps", "-A"], cp(out="\n".join(ps) + "\n"))
        r.on(["tmux"], cp(out="\n".join(panes) + "\n"))
        r.on(["sysctl"], cp(out=sysctl_out(swap_used=swap).replace("vm.swapusage: ", "")))
        r.on(["vm_stat"], cp(out=VM.format(comp=comp, outs=outs)))
        return cfg, r

    def run_check(self, cfg, r, **kw):
        return limits.check(cfg, r, name="new1", profile="alice", platform="darwin", sleep=lambda s: None,
                            env={}, **kw)

    def test_under_limit_ok_and_reserved(self):
        cfg, r = self.build(3)
        out = self.run_check(cfg, r)
        self.assertEqual((out.verdict.kind, out.reserved), ("ok", True))
        self.assertEqual([x["name"] for x in limits.read_reservations(cfg.state_dir)], ["new1"])

    def test_second_check_counts_the_reservation(self):
        cfg, r = self.build(3, toml=TOML + "\n[limits]\nmax_sessions = 4\ncount_mode = 'block'\n")
        self.assertEqual(self.run_check(cfg, r).verdict.kind, "ok")
        out = limits.check(cfg, r, name="new2", profile="alice", platform="darwin", env={})
        self.assertEqual(out.verdict.kind, "refuse")
        self.assertFalse(out.ok)

    def test_own_reservation_not_counted_twice(self):
        cfg, r = self.build(3, toml=TOML + "\n[limits]\nmax_sessions = 4\ncount_mode = 'block'\n")
        self.assertEqual(self.run_check(cfg, r).verdict.kind, "ok")
        self.assertEqual(self.run_check(cfg, r).verdict.kind, "ok")  # same name again: its own place, not a second

    def test_over_limit_flag_overrides_and_logs(self):
        cfg, r = self.build(4, toml=TOML + "\n[limits]\nmax_sessions = 4\ncount_mode = 'block'\n")
        out = self.run_check(cfg, r, over_limit=True)
        self.assertTrue(out.ok and out.override)
        self.assertIn("已越過", (cfg.state_dir / "actions.log").read_text())

    def test_default_warns_over_16(self):
        cfg, r = self.build(17)
        out = self.run_check(cfg, r)
        self.assertEqual((out.verdict.kind, out.ok), ("warn", True))

    def test_memory_red_refuses_without_owner_entry(self):
        cfg, r = self.build(5, comp=700_000, swap="8400M")  # compressor 66%, swap 49%
        out = self.run_check(cfg, r)
        self.assertEqual((out.verdict.kind, out.verdict.why), ("refuse", "memory"))
        self.assertEqual(limits.read_reservations(cfg.state_dir), [])

    def test_replacing_passes_under_red(self):
        cfg, r = self.build(5, comp=700_000, swap="8400M")
        self.assertTrue(self.run_check(cfg, r, replacing=True).ok)

    def test_nothing_readable_never_refuses(self):
        cfg, _ = self.build(2)
        out = limits.check(cfg, FakeRunner(), name="x", profile="alice", platform="darwin", env={})
        self.assertTrue(out.ok)

    def test_pressure_guard_off_just_shows(self):
        cfg, r = self.build(5, comp=700_000, swap="8400M", toml=TOML + "\n[limits]\npressure_guard = false\n")
        self.assertTrue(self.run_check(cfg, r).ok)

    def test_candidates_listed_on_refusal(self):
        cfg, r = self.build(5, comp=700_000, swap="8400M")
        out = self.run_check(cfg, r)
        self.assertEqual(out.cands, [])  # no session file: status unknown, nothing is offered as idle
        self.assertIn("不開 new1", out.lines("new1")[0])


if __name__ == "__main__":
    unittest.main()
