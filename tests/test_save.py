import tempfile
import unittest
from pathlib import Path

from sampan.registry import Row, load_rows, write_rows
from sampan.save import cmd_save
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
SRV = "4242-1792197209"
NO_PROC = Path("/nonexistent-proc-root")


class Env:
    def __init__(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.panes = []
        self.procs = [ps_line(1, 0, "launchd", "launchd")]
        self.env_out = {}
        self.server = True
        self.server_id = SRV
        self.messages = []
        self.stamps = {}  # tmux session → the @sampan_id it carries
        self.stamped = []  # set-option calls: (target, value)

    def pane(self, session, pane_id, pane_pid, command="claude", path="/c"):
        self.panes.append(pane_line(session, pane_id, pane_pid, command, path))
        self.procs.append(ps_line(pane_pid, 1, "-zsh", "-zsh"))

    def claude(self, pid, parent, args="claude"):
        self.procs.append(ps_line(pid, parent, "claude", args))

    def runner(self):
        r = FakeRunner()

        def list_sessions(args):
            if not self.server:
                return cp(rc=1)
            if "#{session_name}\t#{@sampan_id}" in args:
                return cp(out="".join(f"{n}\t{i}\n" for n, i in self.stamps.items()))
            return cp(out=self.server_id + "\n") if "-F" in args else cp()
        r.on(["tmux", "-u", "list-sessions"], list_sessions)

        def set_option(args):
            self.stamped.append((args[4], args[6]))
            return cp()
        r.on(["tmux", "-u", "set-option"], set_option)
        r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(self.panes) + "\n") if self.server else cp(rc=1))
        r.on(["ps", "-A"], cp(out="\n".join(self.procs) + "\n"))
        r.on(["ps", "eww"], lambda args: cp(out=self.env_out.get(int(args[-1]), "claude")))
        return r

    def save(self, **kw):
        return cmd_save(self.cfg, self.runner(), self.messages.append, proc_root=NO_PROC, **kw)

    def rows(self):
        return load_rows(self.cfg.registry_path)

    def seed(self, rows):
        write_rows(self.cfg.registry_path, rows, "t", self.cfg.history_dir)


class SaveTest(unittest.TestCase):
    def test_records_profile_uuid_server_and_current_tmux_name(self):
        e = Env()
        e.pane("001-proj", "%3", 100)
        e.claude(200, 100)
        write_session(e.cfg, "work", 200, sid=A, cwd="/c", tmux="old-name:@1.%3")
        self.assertEqual(e.save(), 0)
        self.assertEqual([(r.tmux, r.profile, r.session_id, r.server) for r in e.rows()],
                         [("001-proj", "work", A, SRV)])
        self.assertIn(" ok ", e.cfg.save_log.read_text())
        self.assertTrue(e.messages[0].startswith("sampan save："), e.messages)

    def test_manual_tmux_rename_keeps_id_and_folder(self):
        e = Env()
        e.seed([Row("old", "claude", "work", A, "/c", id="x-id")])
        e.pane("new-name", "%3", 100)
        e.claude(200, 100)
        write_session(e.cfg, "work", 200, sid=B, cwd="/c")  # the conversation id changed too
        e.stamps = {"new-name": "x-id"}
        self.assertEqual(e.save(), 0)
        self.assertEqual([(r.tmux, r.id, r.session_id) for r in e.rows()], [("new-name", "x-id", B)])
        self.assertEqual(e.stamped, [])  # already stamped: nothing to write
        self.assertTrue(any("old → new-name" in m for m in e.messages), e.messages)

    def test_stamped_id_beats_the_name_of_a_stopped_row(self):
        e = Env()
        e.seed([Row("A", "claude", "work", A, "/c", id="a-id"), Row("B", "claude", "work", C, "/d", "active", id="b-id")])
        e.pane("B", "%3", 100)
        e.claude(200, 100)
        write_session(e.cfg, "work", 200, sid=B, cwd="/c")
        e.stamps = {"B": "a-id"}
        e.save()
        rows = e.rows()
        self.assertEqual([(r.tmux, r.id, r.session_id) for r in rows], [("B", "a-id", B)])
        self.assertTrue(any("b-id" not in m and "B：這個名稱" in m for m in e.messages), e.messages)

    def test_save_writes_missing_option(self):
        e = Env()
        e.seed([Row("x", "claude", "work", A, "/c", id="x-id"), Row("y", "claude", "work", B, "/d", id="y-id")])
        e.pane("x", "%3", 100)
        e.claude(200, 100)
        write_session(e.cfg, "work", 200, sid=A, cwd="/c")
        e.pane("y", "%4", 101)
        e.claude(201, 101)
        write_session(e.cfg, "work", 201, sid=B, cwd="/d")
        e.stamps = {"x": "", "y": "other-id"}  # y carries someone else's id: left alone
        e.save()
        self.assertEqual(e.stamped, [("=x", "x-id")])

    def test_dry_run_writes_no_option(self):
        e = Env()
        e.seed([Row("x", "claude", "work", A, "/c", id="x-id")])
        e.pane("x", "%3", 100)
        e.claude(200, 100)
        write_session(e.cfg, "work", 200, sid=A, cwd="/c")
        e.save(dry_run=True)
        self.assertEqual(e.stamped, [])

    def test_tmux_server_down_leaves_registry_untouched(self):
        # Review Focus 2
        e = Env()
        e.seed([Row("x", "claude", "alice", A, "/c")])
        before = e.cfg.registry_path.read_text()
        e.server = False
        self.assertEqual(e.save(), 0)
        self.assertEqual(e.cfg.registry_path.read_text(), before)
        self.assertIn("skip", e.cfg.save_log.read_text())

    def test_shell_only_sessions_after_reboot_keep_rows(self):
        # Review Focus 2: a shell restored by continuum
        e = Env()
        e.seed([Row("x", "claude", "alice", A, "/c", server="old"), Row("y", "claude", "work", B, "/d", server="old")])
        e.pane("x", "%1", 100, command="zsh")
        e.pane("y", "%2", 101, command="zsh")
        e.save()
        self.assertEqual([(r.tmux, r.session_id) for r in e.rows()], [("x", A), ("y", B)])

    def test_rows_not_yet_restarted_survive_autosave(self):
        # Review R1: only one session was opened after a reboot; keep the other rows
        e = Env()
        e.seed([Row("x", "claude", "alice", A, "/c", server="old"), Row("y", "claude", "work", B, "/d", server="old")])
        e.pane("tmp", "%1", 100, command="zsh")
        e.save(quiet=True)
        self.assertEqual([r.tmux for r in e.rows()], ["x", "y"])

    def test_corrupt_registry_aborts(self):
        # Review Focus 4
        e = Env()
        e.cfg.registry_path.parent.mkdir(parents=True)
        e.cfg.registry_path.write_text("{")
        e.pane("x", "%1", 100)
        self.assertEqual(e.save(), 2)
        self.assertEqual(e.cfg.registry_path.read_text(), "{")

    def test_session_file_pointing_at_foreign_pane_is_not_trusted(self):
        # Review R2: a stale session file points the uuid at another pane
        e = Env()
        e.pane("x", "%1", 100)
        e.pane("y", "%2", 101, command="zsh")
        e.claude(200, 100)
        e.claude(300, 1)  # pid 300 is alive but not under %2
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        write_session(e.cfg, "alice", 300, sid=B, tmux="y:@2.%2")
        e.save()
        self.assertEqual([(r.tmux, r.session_id) for r in e.rows()], [("x", A)])
        self.assertTrue(any("不在那個 pane" in m for m in e.messages))

    def test_fallback_uses_process_args_and_env(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100, f"claude --resume {C}")
        e.env_out[200] = f"claude --resume {C} HOME=/h PATH=/bin CLAUDE_CONFIG_DIR={e.cfg.profiles['work'].config_dir}"
        e.save()
        self.assertEqual([(r.tmux, r.profile, r.session_id) for r in e.rows()], [("x", "work", C)])
        self.assertTrue(any("process 參數" in m for m in e.messages))

    def test_fallback_env_without_config_dir_means_default_profile(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100, f"claude --resume {C}")
        e.env_out[200] = f"claude --resume {C} HOME=/h PATH=/bin"
        e.save()
        self.assertEqual(e.rows()[0].profile, "alice")

    def test_fallback_unreadable_env_does_not_guess_alice(self):
        # Review R3
        e = Env()
        e.seed([Row("x", "claude", "work", A, "/c", server=SRV)])
        e.pane("x", "%1", 100)
        e.claude(200, 100, f"claude --resume {C}")
        e.save()
        self.assertEqual([(r.profile, r.session_id) for r in e.rows()], [("work", A)])
        self.assertTrue(any("判斷不出" in m for m in e.messages))

    def test_fallback_without_uuid_warns_and_skips(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100, "claude --resume Some Name")
        e.env_out[200] = "claude HOME=/h PATH=/bin"
        e.save()
        self.assertEqual(e.rows(), [])
        self.assertTrue(any("判斷不出" in m for m in e.messages))

    def test_two_agents_in_one_session_keeps_previous_row(self):
        # One agent per session: extras are not recorded at all, treated as if the agent were absent -> keep the old row, and don't add a separate "no claude" warning
        e = Env()
        e.seed([Row("x", "claude", "alice", C, "/c", server=SRV)])
        e.pane("x", "%1", 100)
        e.pane("x", "%2", 101)
        e.claude(200, 100)
        e.claude(201, 101)
        write_session(e.cfg, "alice", 201, sid=B, tmux="x:@1.%2")
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        e.save()
        self.assertEqual([(r.tmux, r.session_id) for r in e.rows()], [("x", C)])
        self.assertTrue(any("2 個 agent" in m and "拆成" in m for m in e.messages))
        self.assertFalse(any("沿用" in m for m in e.messages))

    def test_records_pid(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        e.save()
        self.assertEqual(e.rows()[0].pid, 200)

    def test_claude_outside_tmux_ignored(self):
        e = Env()
        e.claude(200, 1)
        write_session(e.cfg, "alice", 200, sid=A, tmux="")
        e.pane("x", "%1", 100, command="zsh")
        e.save()
        self.assertEqual(e.rows(), [])

    def test_quiet_prints_nothing_without_warnings(self):
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        e.save(quiet=True)
        self.assertEqual(e.messages, [])

    def test_dry_run_writes_nothing(self):
        # Review R8
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        write_session(e.cfg, "alice", 200, sid=A, tmux="x:@1.%1")
        self.assertEqual(e.save(dry_run=True), 0)
        self.assertFalse(e.cfg.registry_path.exists())
        self.assertTrue(any("新增 1" in m for m in e.messages))


class ReviewFixTest(unittest.TestCase):
    def test_list_panes_failure_does_not_empty_registry(self):
        # Final review I1
        e = Env()
        e.seed([Row("x", "claude", "alice", A, "/c", server=SRV), Row("y", "claude", "work", B, "/d", server=SRV)])
        before = e.cfg.registry_path.read_text()
        r = e.runner()
        r.on(["tmux", "-u", "list-panes"], cp(rc=1, err="server exited"))
        rc = cmd_save(e.cfg, r, e.messages.append, quiet=True, proc_root=NO_PROC)
        self.assertNotEqual(rc, 0)
        self.assertEqual(e.cfg.registry_path.read_text(), before)
        self.assertIn("error", e.cfg.save_log.read_text())

    def test_nested_claude_in_same_pane_does_not_win(self):
        # Final review I3:the claude (work) in the pane opened another claude (alice)
        e = Env()
        e.pane("x", "%1", 100)
        e.claude(200, 100)
        e.claude(150, 200)  # inner one: alice's file is read first
        write_session(e.cfg, "work", 200, sid=A, tmux="x:@1.%1")
        write_session(e.cfg, "alice", 150, sid=B, tmux="x:@1.%1")
        e.save()
        self.assertEqual([(r.profile, r.session_id) for r in e.rows()], [("work", A)])
