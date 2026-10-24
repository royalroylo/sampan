import json
import tempfile
import unittest
from pathlib import Path

from sampan.observe import LiveAgent, live_by_pane, observe
from sampan.procs import list_procs
from sampan.registry import Observed
from sampan.tmux import Pane
from tests.fakes import FakeRunner, cp, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
TID = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NO_PROC = Path("/nonexistent-proc-root")


def write_hook(cfg, pid, sid, pane="%1", config_dir=""):
    d = cfg.state_dir / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "session_id": sid, "cwd": "/c", "pane": pane, "config_dir": config_dir,
         "source": "clear", "at": "2026-10-20T10:00:00+08:00"}))


class Env:
    def __init__(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.panes = []
        self.procs = [ps_line(1, 0, "launchd", "launchd")]
        self.lsof = {}

    def pane(self, session, pane_id, pane_pid, command="claude", path="/c"):
        self.panes.append(Pane(session, pane_id, pane_pid, command, path))
        self.procs.append(ps_line(pane_pid, 1, "-zsh", "-zsh"))

    def claude(self, pid, parent, args="claude"):
        self.procs.append(ps_line(pid, parent, "claude", args))

    def codex(self, pid, parent, tid=TID):
        self.procs.append(ps_line(pid, parent, "codex", "codex"))
        if tid:
            rollout = self.cfg.codex.home / "sessions/2026/10/20" / f"rollout-2026-10-20T07-42-25-{tid}.jsonl"
            self.lsof[str(pid)] = f"p{pid}\nfcwd\nn/c\nf10\nn{rollout}\n"

    def runner(self):
        r = FakeRunner()
        r.on(["ps", "-A"], cp(out="\n".join(self.procs) + "\n"))
        r.on(["lsof"], lambda a: cp(out=self.lsof.get(a[2], "")))
        r.on(["tmux", "-u", "capture-pane"], cp(out=(FIXTURES / "codex_idle.txt").read_text()))
        return r

    def live(self):
        run = self.runner()
        return live_by_pane(self.cfg, run, self.panes, list_procs(run), proc_root=NO_PROC)

    def observe(self):
        run = self.runner()
        return observe(self.cfg, run, self.panes, list_procs(run), proc_root=NO_PROC)


class LiveByPaneTest(unittest.TestCase):
    def test_hook_record_beats_session_file(self):
        # After /clear the session file's sessionId may still be the old one; the hook record is the latest
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        write_session(e.cfg, "work", 200, sid=A, cwd="/c", tmux="x:@1.%1", status="busy")
        write_hook(e.cfg, 200, B, config_dir=str(e.cfg.profiles["work"].config_dir))
        found, _w = e.live()
        self.assertEqual(found["%1"], LiveAgent("claude", 200, "work", B, "/c", "busy", "%1"))

    def test_dead_hook_record_ignored(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        write_hook(e.cfg, 999, B)  # pid 999 is gone
        found, _w = e.live()
        self.assertEqual(found["%1"].session_id, A)
        self.assertFalse((e.cfg.state_dir / "hooks" / "999.json").exists())

    def test_hook_record_of_pid_outside_the_pane_ignored(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        e.claude(300, 1)  # alive, but not under %1
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        write_hook(e.cfg, 300, B, pane="%1")
        found, _w = e.live()
        self.assertEqual(found["%1"].session_id, A)

    def test_hook_record_without_session_file(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        write_hook(e.cfg, 200, B)
        found, _w = e.live()
        self.assertEqual(found["%1"], LiveAgent("claude", 200, "alice", B, "/c", "unknown", "%1"))

    def test_codex_pane_observed(self):
        e = Env()
        e.pane("cx", "%1", 100, command="codex", path="/c")
        e.codex(200, 100)
        found, _w = e.live()
        self.assertEqual(found["%1"], LiveAgent("codex", 200, self.cfg.codex.profile, TID, "/c", "idle", "%1"))
        observed, warnings, skipped = e.observe()
        self.assertEqual(observed, [Observed("cx", "codex", "", TID, "/c", 200)])
        self.assertEqual(skipped, set())


class ObserveTest(unittest.TestCase):
    def test_two_agents_in_one_session_skipped_with_warning(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.pane("x", "%2", 101, command="codex")
        e.claude(200, 100)
        e.codex(201, 101)
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        observed, warnings, skipped = e.observe()
        self.assertEqual(observed, [])
        self.assertEqual(skipped, {"x"})
        self.assertTrue(any(w.startswith("x：同一個 tmux session 有 2 個 agent") and "拆成" in w for w in warnings))

    def test_two_claudes_in_one_session_skipped(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.pane("x", "%2", 101)
        e.claude(200, 100)
        e.claude(201, 101)
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        write_session(e.cfg, "alice", 201, sid=B, tmux="x:@1.%2")
        observed, _w, skipped = e.observe()
        self.assertEqual((observed, skipped), ([], {"x"}))

    def test_unidentified_claude_still_counts_as_an_agent(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.pane("x", "%2", 101)
        e.claude(200, 100)
        e.claude(201, 101)  # no session file, no uuid argument
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        observed, _w, skipped = e.observe()
        self.assertEqual((observed, skipped), ([], {"x"}))

    def test_claude_and_codex_in_different_sessions(self):
        e = Env()
        e.pane("cl", "%1", 100)
        e.pane("cx", "%2", 101, command="codex")
        e.claude(200, 100)
        e.codex(201, 101)
        write_session(e.cfg, "work", 200, sid=A, cwd="/c", tmux="cl:@1.%1")
        observed, _w, skipped = e.observe()
        self.assertEqual(sorted(observed, key=lambda o: o.tmux), [
            Observed("cl", "claude", "work", A, "/c", 200), Observed("cx", "codex", self.cfg.codex.profile, TID, "/c", 201)])
        self.assertEqual(skipped, set())

    def test_codex_without_thread_yet_is_an_agent_but_not_observed(self):
        e = Env()
        e.pane("cx", "%1", 100, command="codex")
        e.pane("cx", "%2", 101)
        e.codex(200, 100, tid=None)
        e.claude(201, 101)
        write_session(e.cfg, "alice", 201, sid=A, tmux="cx:@1.%2")
        observed, _w, skipped = e.observe()
        self.assertEqual((observed, skipped), ([], {"cx"}))
        e2 = Env()
        e2.pane("cx", "%1", 100, command="codex")
        e2.codex(200, 100, tid=None)
        observed, _w, skipped = e2.observe()
        self.assertEqual((observed, skipped), ([], set()))


class CodexDisabledTest(unittest.TestCase):
    def test_codex_counts_as_an_agent_even_when_disabled(self):
        import dataclasses
        e = Env()
        e.cfg = dataclasses.replace(e.cfg, codex=dataclasses.replace(e.cfg.codex, enabled=False))
        e.pane("x", "%1", 100)
        e.pane("x", "%2", 101, command="codex")
        e.claude(200, 100)
        e.codex(201, 101)
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        observed, warnings, skipped = e.observe()
        self.assertEqual((observed, skipped), ([], {"x"}))
