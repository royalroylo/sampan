import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan import codex_state
from sampan.codex_state import (
    AMBIGUOUS,
    LiveCodex,
    codex_pid_in_pane,
    find_rollout,
    live_codex,
    rollout_from_lsof,
    threads_fallback,
)
from sampan.procs import list_procs
from sampan.tmux import Pane
from tests.fakes import FakeRunner, cp, ps_line
from tests.helpers import make_env

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
TID2 = "01a1546a-1b3b-7753-beda-2fae08f6112b"
TID3 = "01a14b62-c91f-7d42-b701-89404b84ac61"
NATIVE = "/Users/u/.nvm/versions/node/v24.19.0/lib/node_modules/@openai/codex/vendor/aarch64-apple-darwin/bin/codex"


def fixture(name):
    return (FIXTURES / name).read_text()


def ps_runner(lines, run=None):
    run = run or FakeRunner()
    return run.on(["ps", "-A"], cp(out="\n".join(lines) + "\n"))


# Real-machine ps: the comm column is truncated to 16 chars ("/Users/u/.nvm/ve"), so the basename is not codex; look at args
CODEX_TREE = [
    ps_line(100, 1, "-zsh", "-zsh"),
    ps_line(200, 100, "node", "node /Users/u/.nvm/versions/node/v24.19.0/bin/codex"),
    ps_line(201, 200, "/Users/u/.nvm/ve", NATIVE),
    ps_line(202, 201, "npm", "npm exec @some/mcp@latest"),
]


def lsof_out(*paths):
    return "p201\nfcwd\nn/Users/u/proj\n" + "".join(f"f{i + 10}\nn{p}\n" for i, p in enumerate(paths))


def make_db(codex_home, rows, name="state_5.sqlite"):
    codex_home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(codex_home / name)
    con.execute("create table threads (id text primary key, cwd text, source text, archived integer, created_at integer)")
    con.executemany("insert into threads values (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


class ProcTest(unittest.TestCase):
    def test_codex_pid_in_pane_takes_deepest_native(self):
        procs = list_procs(ps_runner(CODEX_TREE))
        self.assertEqual(codex_pid_in_pane(procs, 100), 201)

    def test_codex_pid_in_pane_node_wrapper_only(self):
        procs = list_procs(ps_runner(CODEX_TREE[:2]))
        self.assertEqual(codex_pid_in_pane(procs, 100), 200)

    def test_codex_pid_in_pane_none(self):
        procs = list_procs(ps_runner([ps_line(100, 1, "-zsh", "-zsh"), ps_line(300, 100, "vim", "vim x")]))
        self.assertIsNone(codex_pid_in_pane(procs, 100))

    def test_codex_as_pane_root(self):
        # `tmux new-session -s x codex`: the native binary is the pane's own process
        procs = list_procs(ps_runner([ps_line(100, 1, "codex", "codex")]))
        self.assertEqual(codex_pid_in_pane(procs, 100), 100)
        # node wrapper as the pane root, native binary below it: the deepest still wins
        procs = list_procs(ps_runner([ps_line(100, 1, "node", "node /opt/bin/codex"), ps_line(101, 100, "codex", "codex")]))
        self.assertEqual(codex_pid_in_pane(procs, 100), 101)

    def test_codex_pid_ignores_zombie_and_lookalikes(self):
        procs = list_procs(ps_runner([
            ps_line(100, 1, "-zsh", "-zsh"),
            ps_line(200, 100, "codex", "codex", stat="Z"),
            ps_line(300, 100, "vim", "vim codex"),
            ps_line(400, 100, "node", "node /opt/x/codex-helper.js"),
        ]))
        self.assertIsNone(codex_pid_in_pane(procs, 100))


class RolloutTest(unittest.TestCase):
    def test_rollout_from_lsof_parses_thread_id(self):
        home = Path("/Users/u/.codex")
        rollout = home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
        run = FakeRunner().on(["lsof"], cp(out=lsof_out("/Users/u/.codex/state_5.sqlite", rollout)))
        self.assertEqual(rollout_from_lsof(run, 201, home), TID)
        self.assertEqual(run.called(["lsof"])[0]["args"], [codex_state.LSOF, "-p", "201", "-Fn"])

    def test_lsof_command_prefers_which(self):
        self.assertEqual(codex_state.lsof_command(which=lambda n: "/opt/x/lsof", exists=lambda p: True), "/opt/x/lsof")

    def test_lsof_command_falls_back_to_known_paths(self):
        # launchd's PATH may lack /usr/sbin, so which() can miss it
        none = lambda n: None  # noqa: E731
        self.assertEqual(codex_state.lsof_command(which=none, exists=lambda p: p == "/usr/sbin/lsof"), "/usr/sbin/lsof")
        self.assertEqual(codex_state.lsof_command(which=none, exists=lambda p: p == "/usr/bin/lsof"), "/usr/bin/lsof")

    def test_lsof_command_none_when_absent(self):
        self.assertIsNone(codex_state.lsof_command(which=lambda n: None, exists=lambda p: False))

    def test_no_lsof_means_unknown_not_an_error(self):
        run = FakeRunner()
        with mock.patch.object(codex_state, "LSOF", None):
            self.assertIsNone(rollout_from_lsof(run, 201, Path("/Users/u/.codex")))
        self.assertEqual(run.calls, [])

    def test_rollout_from_lsof_ignores_other_dirs_and_failure(self):
        home = Path("/Users/u/.codex")
        other = f"/tmp/sessions/rollout-2026-10-20T07-42-25-{TID}.jsonl"
        run = FakeRunner().on(["lsof"], cp(out=lsof_out(other)))
        self.assertIsNone(rollout_from_lsof(run, 201, home))
        self.assertIsNone(rollout_from_lsof(FakeRunner().on(["lsof"], cp(rc=1)), 201, home))

    def test_rollout_from_lsof_two_rollouts_is_unknown(self):
        home = Path("/Users/u/.codex")
        a = home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
        b = home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-50-00-{TID2}.jsonl"
        run = FakeRunner().on(["lsof"], cp(out=lsof_out(a, b, a)))
        self.assertIsNone(rollout_from_lsof(run, 201, home))

    def test_find_rollout(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            f = home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"
            f.parent.mkdir(parents=True)
            f.write_text("{}\n")
            self.assertEqual(find_rollout(home, TID), f)
            self.assertIsNone(find_rollout(home, TID2))
            self.assertIsNone(find_rollout(home / "missing", TID))
            for bad in ("*", "", None):  # an unvalidated id must not be globbed
                self.assertIsNone(find_rollout(home, bad))


class FallbackTest(unittest.TestCase):
    def test_fallback_matches_cwd_and_start_time(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            make_db(home, [
                (TID, "/Users/u/proj", "cli", 0, 1000),
                (TID2, "/Users/u/other", "cli", 0, 1001),       # different directory
                (TID3, "/Users/u/proj", "vscode", 0, 1000),     # not the interactive CLI
                ("01a10000-0000-7000-8000-000000000001", "/Users/u/proj", "cli", 1, 1000),  # archived
                ("01a10000-0000-7000-8000-000000000002", "/Users/u/proj", "cli", 0, 1500),  # too far
            ])
            self.assertEqual(threads_fallback(home, "/Users/u/proj", 1010.0, set()), TID)
            self.assertIsNone(threads_fallback(home, "/Users/u/proj", 1200.0, {TID}))  # all the rest are over 120 seconds off
            self.assertIsNone(threads_fallback(home, "/Users/u/nowhere", 1010.0, set()))

    def test_fallback_picks_newest_state_version(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            make_db(home, [(TID2, "/Users/u/proj", "cli", 0, 1000)], "state_5.sqlite")
            make_db(home, [(TID, "/Users/u/proj", "cli", 0, 1000)], "state_12.sqlite")
            self.assertEqual(threads_fallback(home, "/Users/u/proj", 1000.0, set()), TID)

    def test_fallback_missing_or_broken_db(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            self.assertIsNone(threads_fallback(home, "/Users/u/proj", 1000.0, set()))
            (home / "state_5.sqlite").write_text("not a database")
            self.assertIsNone(threads_fallback(home, "/Users/u/proj", 1000.0, set()))

    def test_fallback_excludes_bound_and_refuses_ambiguous(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            # two Codex in the same directory, started 1 second apart
            make_db(home, [(TID, "/Users/u/proj", "cli", 0, 1000), (TID2, "/Users/u/proj", "cli", 0, 1001)])
            self.assertIs(threads_fallback(home, "/Users/u/proj", 1000.5, set()), AMBIGUOUS)   # equally close -> don't guess
            self.assertIs(threads_fallback(home, "/Users/u/proj", 1000.0, set()), AMBIGUOUS)   # 0 s and 1 s apart, still within +/-2
            self.assertEqual(threads_fallback(home, "/Users/u/proj", 1000.0, {TID}), TID2)  # already-bound ones are excluded
            self.assertIsNone(threads_fallback(home, "/Users/u/proj", 1000.0, {TID, TID2}))

    def test_fallback_clear_winner_beyond_tolerance(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            make_db(home, [(TID, "/Users/u/proj", "cli", 0, 1000), (TID2, "/Users/u/proj", "cli", 0, 1060)])
            self.assertEqual(threads_fallback(home, "/Users/u/proj", 1005.0, set()), TID)

    def test_fallback_matches_resolved_cwd(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            real = root / "real"
            real.mkdir()
            (root / "link").symlink_to(real)
            make_db(root / "codex", [(TID, os.path.realpath(real), "cli", 0, 1000)])
            self.assertEqual(threads_fallback(root / "codex", str(root / "link"), 1000.0, set()), TID)


class LiveCodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_env(Path(self.tmp.name))
        self.home = self.cfg.codex.home

    def tearDown(self):
        self.tmp.cleanup()

    def procs(self):
        return list_procs(ps_runner([
            ps_line(100, 1, "-zsh", "-zsh"), ps_line(201, 100, "codex", NATIVE),
            ps_line(110, 1, "-zsh", "-zsh"), ps_line(211, 110, "codex", NATIVE),
            ps_line(120, 1, "-zsh", "-zsh"), ps_line(221, 120, "codex", NATIVE),
            ps_line(130, 1, "-zsh", "-zsh"),
        ]))

    def panes(self):
        return [Pane("a", "%1", 100, "codex", "/Users/u/proj"), Pane("b", "%2", 110, "codex", "/Users/u/proj"),
                Pane("c", "%3", 120, "codex", "/Users/u/proj"), Pane("d", "%4", 130, "zsh", "/Users/u/proj")]

    def runner(self, etimes):
        rollout = self.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{TID}.jsonl"

        def lsof(args):
            return cp(out=lsof_out(rollout)) if args[2] == "201" else cp(out=lsof_out())

        run = FakeRunner().on(["lsof"], lsof)
        run.on(["ps", "-o", "etime="], lambda a: cp(out=etimes[a[-1]] + "\n"))
        return run

    def test_live_codex_lsof_then_fallback(self):
        now = 10_000.0
        make_db(self.home, [(TID, "/Users/u/proj", "cli", 0, int(now - 3600)),
                            (TID2, "/Users/u/proj", "cli", 0, int(now - 300)),
                            (TID3, "/Users/u/proj", "cli", 0, int(now - 1000))])
        run = self.runner({"201": "01:00:00", "211": "05:00", "221": "16:40"})
        found, warnings = live_codex(self.cfg, run, self.panes(), self.procs(), now=now)
        self.assertEqual(found["%1"], LiveCodex(201, TID, "/Users/u/proj", "%1"))
        self.assertEqual(found["%2"].thread_id, TID2)
        self.assertEqual(found["%3"].thread_id, TID3)
        self.assertNotIn("%4", found)
        self.assertEqual(warnings, [])

    def test_live_codex_never_binds_two_panes_to_one_thread(self):
        now = 10_000.0
        # pane a's rollout is readable (TID); b and c started together in the same directory, leaving only TID2 as a candidate -> bind neither
        make_db(self.home, [(TID, "/Users/u/proj", "cli", 0, int(now - 300)),
                            (TID2, "/Users/u/proj", "cli", 0, int(now - 300))])
        run = self.runner({"201": "05:00", "211": "05:00", "221": "05:00"})
        found, warnings = live_codex(self.cfg, run, self.panes(), self.procs(), now=now)
        self.assertEqual(found["%1"].thread_id, TID)
        self.assertIsNone(found["%2"].thread_id)
        self.assertIsNone(found["%3"].thread_id)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(warnings[0].startswith("b："))

    def test_live_codex_fresh_without_thread_is_silent(self):
        # A Codex that was just opened and has no thread yet is normal: save --quiet should not make noise about it
        make_db(self.home, [])
        run = self.runner({"201": "00:05", "211": "00:05", "221": "00:05"})
        found, warnings = live_codex(self.cfg, run, self.panes()[1:2], self.procs(), now=10_000.0)
        self.assertIsNone(found["%2"].thread_id)
        self.assertEqual(warnings, [])

    def test_live_codex_ambiguous_fallback_warns(self):
        now = 10_000.0
        make_db(self.home, [(TID2, "/Users/u/proj", "cli", 0, int(now - 300)),
                            (TID3, "/Users/u/proj", "cli", 0, int(now - 301))])
        run = self.runner({"211": "05:00"})
        found, warnings = live_codex(self.cfg, run, self.panes()[1:2], self.procs(), now=now)
        self.assertIsNone(found["%2"].thread_id)
        self.assertEqual(len(warnings), 1)
        self.assertIn("無法判斷", warnings[0])

    def test_live_codex_disabled(self):
        cfg = self.cfg.__class__(**{**self.cfg.__dict__, "codex": self.cfg.codex.__class__(False, self.home)})
        run = self.runner({})
        self.assertEqual(live_codex(cfg, run, self.panes(), self.procs()), ({}, []))
        self.assertEqual(run.calls, [])


if __name__ == "__main__":
    unittest.main()
