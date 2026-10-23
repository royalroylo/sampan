import tempfile
import unittest
from pathlib import Path

from sampan.procs import claude_pids, descendant_claude, is_descendant, list_procs, proc_env, resume_id
from sampan.tmux import Tmux
from tests.fakes import FakeRunner, cp, pane_line, ps_line

UUID = "b19e16af-d25c-40db-87f7-33bc162b6739"
NO_PROC = Path("/nonexistent-proc-root")


class ProcsTest(unittest.TestCase):
    def runner(self):
        lines = [
            ps_line(1, 0, "/sbin/launchd", "/sbin/launchd"),
            ps_line(100, 1, "-zsh", "-zsh"),
            ps_line(200, 100, "claude", f"claude --resume {UUID}"),
            ps_line(300, 1, "/opt/homebrew/bin/tmux", "tmux new -s x"),
            ps_line(400, 1, "node", "node something"),
            ps_line(500, 100, "claude", "claude", stat="Z"),
        ]
        return FakeRunner().on(["ps", "-A"], cp(out="\n".join(lines) + "\n"))

    def test_list_procs_parses_and_basenames_comm(self):
        procs = list_procs(self.runner())
        self.assertEqual(procs[200].ppid, 100)
        self.assertEqual(procs[300].comm, "tmux")
        self.assertEqual(procs[200].args, f"claude --resume {UUID}")
        self.assertEqual(procs[500].stat, "Z")

    def test_claude_pids_excludes_zombies(self):
        self.assertEqual(claude_pids(list_procs(self.runner())), {200})

    def test_descendant_claude(self):
        procs = list_procs(self.runner())
        self.assertEqual(descendant_claude(procs, 100).pid, 200)
        self.assertIsNone(descendant_claude(procs, 300))

    def test_is_descendant(self):
        procs = list_procs(self.runner())
        self.assertTrue(is_descendant(procs, 200, 100))
        self.assertTrue(is_descendant(procs, 200, 1))
        self.assertFalse(is_descendant(procs, 200, 300))
        self.assertFalse(is_descendant(procs, 999, 100))

    def test_claude_as_pane_root(self):
        # `tmux new-session -s x claude` (no shell): the pane's own process is claude
        procs = list_procs(FakeRunner().on(["ps", "-A"], cp(out="\n".join([
            ps_line(1, 0, "/sbin/launchd", "/sbin/launchd"),
            ps_line(9052, 1, "tmux", "tmux new-session -d"),
            ps_line(88823, 9052, "claude", "claude"),
            ps_line(88900, 88823, "node", "node mcp-server.js"),
        ]) + "\n")))
        self.assertEqual(descendant_claude(procs, 88823).pid, 88823)
        self.assertTrue(is_descendant(procs, 88823, 88823))
        self.assertTrue(is_descendant(procs, 88900, 88823))
        self.assertFalse(is_descendant(procs, 9052, 88823))
        self.assertFalse(is_descendant(procs, 999, 999))  # an unknown pid is never inside a pane

    def test_zombie_claude_as_pane_root_is_not_an_agent(self):
        procs = list_procs(FakeRunner().on(["ps", "-A"], cp(out=ps_line(100, 1, "claude", "claude", stat="Z") + "\n")))
        self.assertIsNone(descendant_claude(procs, 100))

    def test_resume_id(self):
        self.assertEqual(resume_id(f"claude --resume {UUID}"), UUID)
        self.assertEqual(resume_id(f"claude --session-id={UUID} -c"), UUID)
        self.assertIsNone(resume_id("claude --resume Some Name"))

    def test_proc_env_from_ps(self):
        r = FakeRunner().on(["ps", "eww"], cp(out=f"claude --resume {UUID} HOME=/h PATH=/bin CLAUDE_CONFIG_DIR=/p/work\n"))
        env = proc_env(r, 200, proc_root=NO_PROC)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/p/work")

    def test_proc_env_unreadable_is_none(self):
        # Review R3:ps prints only the command, no environment variables -> unreadable, must not be treated as "CLAUDE_CONFIG_DIR not set"
        r = FakeRunner().on(["ps", "eww"], cp(out=f"claude --resume {UUID}\n"))
        self.assertIsNone(proc_env(r, 200, proc_root=NO_PROC))
        self.assertIsNone(proc_env(FakeRunner().on(["ps", "eww"], cp(rc=1)), 200, proc_root=NO_PROC))

    def test_proc_env_prefers_proc(self):
        root = Path(tempfile.mkdtemp())
        (root / "200").mkdir()
        (root / "200" / "environ").write_bytes(b"HOME=/h\0CLAUDE_CONFIG_DIR=/p/work\0")
        r = FakeRunner()
        self.assertEqual(proc_env(r, 200, proc_root=root)["CLAUDE_CONFIG_DIR"], "/p/work")
        self.assertEqual(r.calls, [])

    def test_list_procs_failure_returns_empty(self):
        self.assertEqual(list_procs(FakeRunner().on(["ps"], cp(rc=1))), {})


class TmuxTest(unittest.TestCase):
    def test_panes_parse_including_cjk_names(self):
        out = "\n".join([
            pane_line("003-帳務", "%4", 3505, "claude", "/Users/u/proj"),
            pane_line("008-app", "%9", 37498, "zsh", "/Users/u/proj"),
        ]) + "\n"
        r = FakeRunner().on(["tmux", "-u", "list-panes"], cp(out=out))
        panes = Tmux(r).panes()
        self.assertEqual(panes[0].session, "003-帳務")
        self.assertEqual(panes[1].pane_pid, 37498)
        self.assertEqual(panes[1].command, "zsh")

    def test_every_command_forces_utf8(self):
        # Review R4:without LANG under launchd, Chinese names turn into ____
        r = FakeRunner()
        t = Tmux(r)
        t.server_running()
        t.panes()
        t.has_session("x")
        t.new_session("x", "/tmp")
        t.send_keys("%1", "a")
        t.server_id()
        self.assertTrue(all(c["args"][:2] == ["tmux", "-u"] for c in r.calls))

    def test_panes_when_server_down(self):
        t = Tmux(FakeRunner().on(["tmux", "-u", "list-panes"], cp(rc=1, err="no server running")))
        self.assertEqual(t.panes(), [])  # no server = really no panes; only a read failure gives None (see test_tmux_unreadable)

    def test_has_session_uses_exact_match(self):
        r = FakeRunner()
        Tmux(r).has_session("008-app")
        self.assertEqual(r.calls[-1]["args"], ["tmux", "-u", "has-session", "-t", "=008-app"])

    def test_new_session_returns_pane_id(self):
        r = FakeRunner().on(["tmux", "-u", "new-session"], cp(out="%31\n"))
        self.assertEqual(Tmux(r).new_session("x", "/tmp"), "%31")
        self.assertEqual(r.calls[-1]["args"],
                         ["tmux", "-u", "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "x", "-c", "/tmp"])

    def test_new_session_failure(self):
        r = FakeRunner().on(["tmux", "-u", "new-session"], cp(rc=1, err="duplicate session"))
        self.assertIsNone(Tmux(r).new_session("x", "/tmp"))

    def test_server_running(self):
        self.assertFalse(Tmux(FakeRunner().on(["tmux", "-u", "list-sessions"], cp(rc=1))).server_running())
        self.assertTrue(Tmux(FakeRunner()).server_running())

    def test_server_id(self):
        r = FakeRunner().on(["tmux", "-u", "list-sessions"], cp(out="4242-1792197209\n4242-1792197209\n"))
        self.assertEqual(Tmux(r).server_id(), "4242-1792197209")
        self.assertIsNone(Tmux(FakeRunner().on(["tmux", "-u", "list-sessions"], cp(rc=1))).server_id())
