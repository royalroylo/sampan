"""A model per session: registry field, `sampan model`, `--model` on launch lines, new, status and list."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sampan import cli, status
from sampan.launch import claude_argv, launch_line
from sampan.registry import Observed, RegistryError, Row, load_rows, merge, write_rows
from sampan.send import TERMINAL
from sampan.session_run import MODEL_MAX, cmd_model, cmd_new, model_problem
from sampan.steps import launch_cmd
from tests.fakes import FakeRunner
from tests.helpers import make_env, status_row
from tests.test_session_run import Base as RunBase

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
T = "01a1568b-81c2-71f1-99f7-b7a590f4ed96"


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def write(self, rows):
        write_rows(self.cfg.registry_path, rows, "t", self.cfg.history_dir)

    def raw(self):
        return json.loads(self.cfg.registry_path.read_text())

    def test_an_empty_model_is_no_key_so_files_stay_as_older_sampan_wrote_them(self):
        self.write([Row("a", "claude", "alice", A, "/c"), Row("b", "claude", "alice", B, "/c", model="sonnet")])
        sessions = {s["tmux"]: s for s in self.raw()["sessions"]}
        self.assertNotIn("model", sessions["a"])
        self.assertEqual(sessions["b"]["model"], "sonnet")
        self.assertEqual(self.raw()["version"], 3)  # no new version

    def test_round_trip_and_an_old_file_without_the_key(self):
        self.write([Row("a", "claude", "alice", A, "/c", model="opus[1m]"), Row("b", "claude", "alice", B, "/c")])
        self.assertEqual({r.tmux: r.model for r in load_rows(self.cfg.registry_path)}, {"a": "opus[1m]", "b": ""})
        data = self.raw()
        for s in data["sessions"]:
            s.pop("model", None)
        self.cfg.registry_path.write_text(json.dumps(data))
        self.assertEqual({r.model for r in load_rows(self.cfg.registry_path)}, {""})

    def test_setting_a_model_makes_a_history_snapshot_like_any_change_but_rewriting_the_same_does_not(self):
        row = dict(id="id-a")
        self.write([Row("a", "claude", "alice", A, "/c", **row)])
        self.write([Row("a", "claude", "alice", A, "/c", model="opus", **row)])
        snaps = len(list(self.cfg.history_dir.glob("registry-*.json")))
        self.assertEqual(snaps, 1)
        self.write([Row("a", "claude", "alice", A, "/c", model="opus", **row)])  # nothing changed
        self.assertEqual(len(list(self.cfg.history_dir.glob("registry-*.json"))), snaps)

    def test_a_registry_with_a_key_this_sampan_does_not_know_is_refused_not_overwritten(self):
        # what an older sampan does with a row that has `model`: the same fail-closed path as for any unknown key
        data = self.raw() if self.cfg.registry_path.exists() else None
        self.write([Row("a", "claude", "alice", A, "/c")])
        data = self.raw()
        data["sessions"][0]["from_the_future"] = "x"
        self.cfg.registry_path.write_text(json.dumps(data))
        before = self.cfg.registry_path.read_bytes()
        with self.assertRaises(RegistryError):
            load_rows(self.cfg.registry_path)
        self.assertEqual(self.cfg.registry_path.read_bytes(), before)

    def test_save_keeps_the_model(self):
        old = [Row("a", "claude", "alice", A, "/c", id="id-a", model="sonnet[1m]")]
        ob = [Observed("a", "claude", "alice", A, "/c", 100)]
        res = merge(old, ob, {"a"}, "now", "srv")
        self.assertEqual(res.rows[0].model, "sonnet[1m]")
        renamed = merge(old, [Observed("b", "claude", "alice", A, "/c", 100, sampan_id="id-a")], {"b"}, "now", "srv")
        self.assertEqual((renamed.rows[0].tmux, renamed.rows[0].model), ("b", "sonnet[1m]"))
        self.assertEqual(merge([], ob, {"a"}, "now", "srv").rows[0].model, "")


class ResumeHintTest(unittest.TestCase):
    def test_the_manual_resume_line_of_forget_has_the_model(self):
        from sampan.profile_ops import _resume_line
        cfg = make_env(Path(tempfile.mkdtemp()))
        row = Row("x", "claude", "work", A, "/c", model="sonnet")
        self.assertIn("--model sonnet", _resume_line(cfg, cfg.profiles["work"], row))
        self.assertNotIn("--model", _resume_line(cfg, cfg.profiles["work"], Row("x", "claude", "work", A, "/c")))


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_model_goes_after_the_other_arguments_and_only_when_set(self):
        p = self.cfg.profiles["alice"]
        self.assertEqual(claude_argv(p, resume=A, name="x", model="sonnet[1m]"),
                         ["claude", "--resume", A, "--name", "x", "--model", "sonnet[1m]"])
        self.assertEqual(claude_argv(p, resume=A, name="x"), ["claude", "--resume", A, "--name", "x"])
        self.assertEqual(claude_argv(p, resume=A, model=""), ["claude", "--resume", A])

    def test_launch_line_quotes_it(self):
        line = launch_line(self.cfg, "claude", "alice", resume=A, name="x", cwd="/c", model="sonnet[1m]")
        self.assertIn("--model 'sonnet[1m]'", line)
        self.assertNotIn("--model", launch_line(self.cfg, "claude", "alice", resume=A, name="x", cwd="/c"))
        self.assertNotIn("--model", launch_line(self.cfg, "codex", "codex", resume=T, cwd="/c", model="sonnet"))

    def test_every_launch_of_a_row_uses_its_model(self):
        row = Row("x", "claude", "work", A, "/c", model="opus")
        self.assertIn("--model opus", launch_cmd(self.cfg, row, cwd="/c"))
        self.assertIn("--model opus", launch_cmd(self.cfg, row, profile="alice", cwd="/c"))  # a switch keeps it
        self.assertNotIn("--model", launch_cmd(self.cfg, Row("x", "claude", "work", A, "/c"), cwd="/c"))


class ModelProblemTest(unittest.TestCase):
    def test_only_clear_typos_are_refused(self):
        for bad in ("", "sonnet 5", "a\nb", "x\x1b[0m", "a\tb", "m" * (MODEL_MAX + 1)):
            self.assertIsNotNone(model_problem(bad), repr(bad))
        for good in ("opus[1m]", "sonnet", "claude-sonnet-5-5", "some-future-model@2", "m" * MODEL_MAX):
            self.assertIsNone(model_problem(good), repr(good))


class CmdModelTest(RunBase):
    def setUp(self):
        super().setUp()
        self.registry([Row("u", "claude", "alice", A, str(self.cwd), "active", "t", "srv", None),
                       Row("other", "claude", "alice", B, str(self.cwd), "active", "t", "srv", None),
                       Row("cx", "codex", "codex", T, str(self.cwd), "active", "t", "srv", None)])

    def rows(self):
        return {r.tmux: r for r in load_rows(self.cfg.registry_path)}

    def test_set_clear_and_list(self):
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "u", "  sonnet[1m] "), 0)
        self.assertEqual((self.rows()["u"].model, self.rows()["other"].model), ("sonnet[1m]", ""))
        self.assertIn("✔ u 的模型設成 sonnet[1m]", self.msgs[-1])
        self.assertIn("正在跑的不變", self.msgs[-1])
        self.assertTrue(any("[model u#" in x for x in __import__("tests.test_session_run", fromlist=["x"]).action_lines(self.cfg)))
        self.msgs.clear()
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "u"), 0)
        self.assertEqual(self.msgs, ["u：sonnet[1m]"])
        self.msgs.clear()
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "other"), 0)
        self.assertIn("沒設", self.msgs[0])
        self.msgs.clear()
        self.assertEqual(cmd_model(self.cfg, self.msgs.append), 0)
        self.assertEqual(self.msgs, ["u  sonnet[1m]"])
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "u", clear=True), 0)
        self.assertEqual(self.rows()["u"].model, "")
        self.msgs.clear()
        cmd_model(self.cfg, self.msgs.append)
        self.assertIn("沒有 session 設了模型", self.msgs[0])

    def test_refusals_change_nothing(self):
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "u", "a b"), 2)
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "nope", "sonnet"), 2)
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "cx", "sonnet"), 2)
        self.assertIn("只有 Claude session 能設模型", self.msgs[-1])
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, None, "sonnet"), 2)
        self.assertEqual({r.model for r in self.rows().values()}, {""})

    def test_a_retired_session_can_be_set(self):
        self.registry([Row("old", "claude", "alice", A, str(self.cwd), "retired", "t", "srv", None)])
        self.assertEqual(cmd_model(self.cfg, self.msgs.append, "old", "opus"), 0)
        self.assertEqual(self.rows()["old"].model, "opus")


class NewTest(RunBase):
    def new(self, target="claude:work", **kw):
        r = self.runner()
        rc = cmd_new(self.cfg, r, self.msgs.append, "new-x", target, str(self.cwd), sleep=lambda _: None, timeout=1,
                     uuid4=lambda: B, **kw)
        return r, rc

    def test_new_with_a_model_registers_and_launches_with_it(self):
        r, _rc = self.new(model="sonnet[1m]")
        self.assertEqual([x.model for x in load_rows(self.cfg.registry_path)], ["sonnet[1m]"])
        self.assertIn("--model 'sonnet[1m]'", self.keys_sent(r)[0][1])

    def test_new_without_it_is_unchanged(self):
        r, _rc = self.new()
        self.assertEqual([x.model for x in load_rows(self.cfg.registry_path)], [""])
        self.assertNotIn("--model", self.keys_sent(r)[0][1])
        self.assertNotIn("model", {k for s in json.loads(self.cfg.registry_path.read_text())["sessions"] for k in s})

    def test_bad_model_and_codex_start_nothing(self):
        r, rc = self.new(model="a b")
        self.assertEqual(rc, 2)
        self.assertEqual((load_rows(self.cfg.registry_path), self.keys_sent(r)), ([], []))
        r, rc = self.new(target="codex", model="sonnet")
        self.assertEqual(rc, 2)
        self.assertIn("只有 Claude session 能設模型", self.msgs[-1])
        self.assertEqual(load_rows(self.cfg.registry_path), [])

    def test_dry_run_shows_the_model(self):
        r, rc = self.new(model="opus", dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("--model opus", "\n".join(self.msgs))
        self.assertEqual(load_rows(self.cfg.registry_path), [])


class StatusTest(unittest.TestCase):
    def table(self, *rows):
        lines = []
        listed = status.Rows(rows)
        with mock.patch.object(status, "status_rows", return_value=listed):
            status.cmd_status(None, None, lines.append)
        return lines

    def test_the_model_column_appears_only_when_somebody_has_a_model(self):
        plain = self.table(status_row("a", sid=A), status_row("b", sid=B))
        self.assertNotIn("模型", plain[0])
        with_model = self.table(status_row("a", sid=A, model="sonnet[1m]"), status_row("b", sid=B))
        self.assertIn("模型", with_model[0])
        self.assertLess(with_model[0].index("profile"), with_model[0].index("模型"))
        self.assertLess(with_model[0].index("模型"), with_model[0].index("id"))
        self.assertIn("sonnet[1m]", with_model[1])
        self.assertNotIn("sonnet", with_model[2])
        self.assertEqual(len(with_model[2].split()), len(with_model[0].split()))  # the unset row shows "-" in its place

    def test_status_rows_carry_the_registry_model(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(cfg.registry_path, [Row("a", "claude", "alice", A, "/c", model="opus")], "t", cfg.history_dir)
        run = FakeRunner(strict=True)
        from tests.fakes import cp
        run.on(["tmux", "-u", "list-panes"], cp(rc=1, err="no server running"))
        run.on(["tmux", "-u", "list-sessions"], cp(rc=1))
        run.on(["tmux", "-u", "has-session"], cp(rc=1))
        run.on(["ps", "-A"], cp(out=""))
        self.assertEqual([r.model for r in status.status_rows(cfg, run)], ["opus"])


class CardTest(unittest.TestCase):
    def test_the_card_shows_a_model_only_when_there_is_one(self):
        from sampan.ui.cards import card_lines
        cfg = make_env(Path(tempfile.mkdtemp()))
        from datetime import datetime, timezone
        now = datetime(2026, 11, 21, tzinfo=timezone.utc)

        def text(row):
            return "\n".join(str(x) for x in card_lines(cfg, row, {}, now))
        self.assertIn("sonnet", text(status_row("a", sid=A, model="sonnet")))
        self.assertNotIn("模型", text(status_row("a", sid=A)))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        write_rows(self.cfg.registry_path, [Row("u", "claude", "alice", A, "/c", id="id-u")], "t", self.cfg.history_dir)

    def run_cli(self, *argv, caller=TERMINAL):
        buf = io.StringIO()
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.runner.run", FakeRunner(strict=True)), \
                mock.patch("sampan.send.command_caller", return_value=caller), redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_set_list_clear(self):
        self.assertEqual(self.run_cli("model", "u", "sonnet")[0], 0)
        self.assertEqual(load_rows(self.cfg.registry_path)[0].model, "sonnet")
        rc, text = self.run_cli("model")
        self.assertEqual((rc, text.strip()), (0, "u  sonnet"))
        self.assertEqual(self.run_cli("model", "u", "--clear")[0], 0)
        self.assertEqual(load_rows(self.cfg.registry_path)[0].model, "")
        self.assertEqual(self.run_cli("model", "u", "x", "--clear")[0], 2)

    def test_an_agent_cannot_set_but_can_read(self):
        from sampan.send import Caller
        agent = Caller("a", "alice", "claude", "%1")
        rc, text = self.run_cli("model", "u", "sonnet", caller=agent)
        self.assertEqual(rc, 2)
        self.assertEqual(load_rows(self.cfg.registry_path)[0].model, "")
        self.assertEqual(self.run_cli("model", "u", caller=agent)[0], 0)  # reading is allowed

    def test_new_passes_the_model(self):
        with mock.patch("sampan.session_run.cmd_new", return_value=0) as cn, \
                mock.patch("sampan.cli.load_config", return_value=self.cfg), mock.patch("sampan.runner.run", FakeRunner(strict=True)):
            cli.main(["new", "x", "claude:work", "/tmp", "--model", "sonnet"])
            self.assertEqual(cn.call_args.kwargs["model"], "sonnet")
            cli.main(["new", "x", "claude:work", "/tmp"])
            self.assertNotIn("model", cn.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
