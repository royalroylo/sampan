"""Codex review subagents (the guardian and the like) are never a pane's conversation: not picked, not registered, not
sent to; a registry row that already holds one is put back on its parent by `sampan save`. Fake data only."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sampan import codex_subagent, deliver, doctor, save
from sampan.codex_daemon import CodexThread, DaemonSnapshot
from sampan.codex_state import daemon_fallback, live_codex, rollout_from_lsof, threads_fallback
from sampan.procs import list_procs
from sampan.registry import Row, load_rows, write_rows
from sampan.tmux import Pane
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_codex_state import NATIVE, lsof_out, ps_runner

MAIN = "00000000-0000-4000-8000-0000000000a1"
GUARD = "00000000-0000-4000-8000-0000000000b1"
GUARD2 = "00000000-0000-4000-8000-0000000000b2"
REVIEW = "00000000-0000-4000-8000-0000000000b3"
SPAWNED = "00000000-0000-4000-8000-0000000000b4"
DEEP = "00000000-0000-4000-8000-0000000000b5"
FORK = "00000000-0000-4000-8000-0000000000c1"
OTHER = "00000000-0000-4000-8000-0000000000d1"
CWD = "/w/proj-a"
NOW = 10_000.0

GUARDIAN_SOURCE = json.dumps({"subagent": {"other": "guardian"}})
STRING_SOURCE = json.dumps({"subagent": "review"})
SPAWN_SOURCE = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": MAIN, "depth": 1}}})


class Home:
    """A fake CODEX_HOME: a state db with every column the real one has that matters, and rollouts."""

    def __init__(self, path, *, with_thread_source=True, with_originator=True):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        cols = ("id text primary key, cwd text, source text, archived integer, created_at integer"
                + (", originator text" if with_originator else "") + (", thread_source text" if with_thread_source else "")
                + ", attached_thread_parent_id text")
        self.thread_source, self.originator = with_thread_source, with_originator
        con = sqlite3.connect(self.path / "state_5.sqlite")
        con.execute(f"create table threads ({cols})")
        con.execute("create table thread_spawn_edges (parent_thread_id text not null, child_thread_id text primary key, status text)")
        con.commit()
        con.close()

    def thread(self, tid, source="vscode", *, cwd=CWD, created=NOW - 60, archived=0, thread_source=None, originator="sampan"):
        con = sqlite3.connect(self.path / "state_5.sqlite")
        values = [tid, cwd, source, archived, created]
        if self.originator:
            values.append(originator)
        if self.thread_source:
            values.append(thread_source)
        values.append(None)
        con.execute(f"insert into threads values ({','.join('?' * len(values))})", values)
        con.commit()
        con.close()

    def edge(self, parent, child):
        con = sqlite3.connect(self.path / "state_5.sqlite")
        con.execute("insert into thread_spawn_edges values (?, ?, 'open')", (parent, child))
        con.commit()
        con.close()

    def rollout(self, tid, *, source="vscode", parent=None, thread_source=None):
        d = self.path / "sessions/2026/11/28"
        d.mkdir(parents=True, exist_ok=True)
        meta = {"id": tid, "session_id": parent or tid, "cwd": CWD, "originator": "sampan", "source": json.loads(source) if source.startswith("{") else source}
        if parent:
            meta["parent_thread_id"] = parent
        if thread_source:
            meta["thread_source"] = thread_source
        path = d / f"rollout-2026-11-28T06-42-02-{tid}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
        return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_env(Path(self.tmp.name))
        self.home = Home(self.cfg.codex.home)


class Shapes(unittest.TestCase):
    def test_every_shape_of_source_is_recognised(self):
        for source in (GUARDIAN_SOURCE, STRING_SOURCE, SPAWN_SOURCE, '{"subagent":', {"subagent": {"x": 1}}, "subagent"):
            self.assertTrue(codex_subagent.source_is_subagent(source), source)
        for source in ("vscode", "cli", "exec", "", None, json.dumps({"fork": {"id": MAIN}}), {"attached": MAIN}):
            self.assertFalse(codex_subagent.source_is_subagent(source), source)

    def test_thread_source_names(self):
        self.assertTrue(codex_subagent.is_subagent("vscode", "guardian_review"))
        self.assertTrue(codex_subagent.is_subagent("vscode", "subagent"))
        self.assertFalse(codex_subagent.is_subagent("vscode", "user"))
        self.assertFalse(codex_subagent.is_subagent("vscode", None))


class Picking(Base):
    def populate(self):
        self.home.thread(MAIN, "vscode", created=NOW - 3600)
        self.home.thread(GUARD, GUARDIAN_SOURCE, created=NOW - 3590, thread_source="guardian_review")
        self.home.thread(REVIEW, STRING_SOURCE, created=NOW - 3595)
        self.home.thread(SPAWNED, SPAWN_SOURCE, created=NOW - 3598)

    def test_threads_fallback_never_picks_a_subagent(self):
        self.populate()
        self.assertEqual(threads_fallback(self.home.path, CWD, NOW - 3600, set()), MAIN)
        self.assertIsNone(threads_fallback(self.home.path, CWD, NOW - 3591, {MAIN}))  # only subagents left: nothing

    def test_a_fork_stays_a_main_conversation(self):
        self.home.thread(FORK, "vscode", created=NOW - 100)  # forked from another thread: source is still vscode
        self.assertEqual(threads_fallback(self.home.path, CWD, NOW - 100, set()), FORK)

    def test_old_databases_without_the_newer_columns(self):
        home = Home(Path(self.tmp.name) / "old", with_thread_source=False, with_originator=False)
        home.thread(MAIN, "cli", created=NOW - 100)
        home.thread(GUARD, GUARDIAN_SOURCE, created=NOW - 99)
        self.assertEqual(threads_fallback(home.path, CWD, NOW - 100, set()), MAIN)

    def test_thread_source_alone_is_enough(self):
        self.home.thread(MAIN, "cli", created=NOW - 3600)
        self.home.thread(GUARD, "cli", created=NOW - 3599, thread_source="guardian_review")
        self.assertEqual(threads_fallback(self.home.path, CWD, NOW - 3599, {MAIN}) or "none", "none")

    def snap(self, *threads):
        return DaemonSnapshot(self.home.path, None, tuple(threads), frozenset(t.id for t in threads),
                              datetime(2026, 11, 28, tzinfo=timezone.utc))

    @staticmethod
    def thread(tid, source="vscode", parent=None):
        return CodexThread(tid, "", CWD, "sampan", source, parent, "idle", (), datetime.fromtimestamp(4_000_000_000, timezone.utc), None)

    def test_daemon_fallback_skips_subagents_but_not_forks(self):
        snap = self.snap(self.thread(MAIN), self.thread(GUARD, "subagent", MAIN), self.thread(SPAWNED, "subagent", MAIN))
        self.assertEqual(daemon_fallback(snap, CWD, set(), NOW - 100), MAIN)
        self.assertEqual(daemon_fallback(self.snap(self.thread(FORK, "vscode", parent=MAIN)), CWD, set(), NOW - 100), FORK)

    def test_daemon_fallback_with_only_subagents_finds_nothing(self):
        self.assertIsNone(daemon_fallback(self.snap(self.thread(GUARD, "subagent", MAIN)), CWD, set(), NOW - 100))


class Lsof(Base):
    def test_the_rollout_first_line_decides_and_the_db_is_the_second_opinion(self):
        guard = self.home.rollout(GUARD, source=GUARDIAN_SOURCE, parent=MAIN)
        main = self.home.rollout(MAIN)
        for path, want in ((guard, None), (main, MAIN)):
            run = FakeRunner().on(["lsof"], cp(out=lsof_out(path)))
            tid = rollout_from_lsof(run, 201, self.home.path)
            self.assertEqual(tid, want)

    def test_the_db_row_alone_excludes_when_the_rollout_cannot_be_read(self):
        self.home.thread(GUARD, GUARDIAN_SOURCE)
        path = self.home.path / "sessions/2026/11/28" / f"rollout-2026-11-28T06-42-02-{GUARD}.jsonl"
        run = FakeRunner().on(["lsof"], cp(out=lsof_out(path)))  # the file is gone: only the db knows
        self.assertIsNone(rollout_from_lsof(run, 201, self.home.path))

    def test_the_rollout_alone_excludes_when_the_db_has_no_such_row(self):
        path = self.home.rollout(GUARD, source=STRING_SOURCE, parent=MAIN)
        run = FakeRunner().on(["lsof"], cp(out=lsof_out(path)))
        self.assertIsNone(rollout_from_lsof(run, 201, self.home.path))

    def test_an_ordinary_conversation_with_a_session_id_is_kept(self):
        path = self.home.rollout(MAIN)
        self.assertEqual(codex_subagent.rollout_meta(path).session_id, MAIN)
        self.assertFalse(codex_subagent.rollout_meta(path).subagent)
        run = FakeRunner().on(["lsof"], cp(out=lsof_out(path)))
        self.assertEqual(rollout_from_lsof(run, 201, self.home.path), MAIN)


class LiveAndAdopt(Base):
    def procs(self):
        return list_procs(ps_runner([ps_line(100, 1, "-zsh", "-zsh"), ps_line(201, 100, "codex", NATIVE)]))

    def run_for(self):
        run = FakeRunner().on(["lsof"], cp(out=lsof_out()))
        return run.on(["ps", "-o", "etime="], cp(out="01:00:00\n"))

    def test_live_codex_binds_the_main_conversation_not_the_guardian(self):
        self.home.thread(MAIN, "vscode", created=NOW - 3600)
        for g in (GUARD, GUARD2):
            self.home.thread(g, GUARDIAN_SOURCE, created=NOW - 3590, thread_source="guardian_review")
        found, warnings = live_codex(self.cfg, self.run_for(), [Pane("a", "%1", 100, "codex", CWD)], self.procs(), now=NOW,
                                     snapshot_fn=lambda *a, **k: None)
        self.assertEqual(found["%1"].thread_id, MAIN)
        self.assertEqual(warnings, [])

    def test_a_pane_with_only_a_guardian_in_its_directory_has_no_thread_yet(self):
        self.home.thread(GUARD, GUARDIAN_SOURCE, created=NOW - 3600)
        found, warnings = live_codex(self.cfg, self.run_for(), [Pane("a", "%1", 100, "codex", CWD)], self.procs(), now=NOW,
                                     snapshot_fn=lambda *a, **k: None)
        self.assertIsNone(found["%1"].thread_id)
        self.assertEqual(warnings, [])

    def test_adopt_does_not_take_a_subagent_in(self):
        from sampan import adopt
        self.home.thread(GUARD, GUARDIAN_SOURCE, created=NOW - 3600)
        snap = DaemonSnapshot(self.home.path, None, (), frozenset(), datetime(2026, 11, 28, tzinfo=timezone.utc))
        ids, unknown = adopt._in_tmux_threads(self.cfg, self.run_for(), [Pane("a", "%1", 100, "codex", CWD)], self.procs(),
                                              datetime.fromtimestamp(NOW, timezone.utc), snap)
        self.assertEqual(ids, set())
        self.assertEqual(len(unknown), 1)


class Parents(Base):
    def test_rollout_first(self):
        self.home.thread(GUARD, GUARDIAN_SOURCE)
        self.home.rollout(GUARD, source=GUARDIAN_SOURCE, parent=MAIN)
        self.assertEqual(codex_subagent.root_of(self.home.path, GUARD), (MAIN, None))

    def test_rollout_deleted_the_spawn_edge_answers(self):
        self.home.thread(SPAWNED, SPAWN_SOURCE)
        self.home.edge(MAIN, SPAWNED)
        self.assertEqual(codex_subagent.root_of(self.home.path, SPAWNED), (MAIN, None))

    def test_the_daemon_is_the_last_resort_and_asked_only_then(self):
        self.home.thread(GUARD, GUARDIAN_SOURCE)
        asked = []
        self.assertEqual(codex_subagent.root_of(self.home.path, GUARD, lambda t: asked.append(t) or MAIN), (MAIN, None))
        self.assertEqual(asked, [GUARD])
        self.home.edge(MAIN, GUARD)
        asked.clear()
        codex_subagent.root_of(self.home.path, GUARD, lambda t: asked.append(t) or OTHER)
        self.assertEqual(asked, [])

    def test_a_subagent_of_a_subagent_goes_up_to_the_main_conversation(self):
        self.home.thread(SPAWNED, SPAWN_SOURCE)
        self.home.thread(DEEP, SPAWN_SOURCE)
        self.home.edge(SPAWNED, DEEP)
        self.home.edge(MAIN, SPAWNED)
        self.assertEqual(codex_subagent.root_of(self.home.path, DEEP), (MAIN, None))

    def test_no_parent_anywhere_is_a_reason_not_a_guess(self):
        self.home.thread(GUARD, GUARDIAN_SOURCE)
        self.assertEqual(codex_subagent.root_of(self.home.path, GUARD), (None, "找不到父對話"))

    def test_a_loop_ends(self):
        self.home.thread(SPAWNED, SPAWN_SOURCE)
        self.home.thread(DEEP, SPAWN_SOURCE)
        self.home.edge(SPAWNED, DEEP)
        self.home.edge(DEEP, SPAWNED)
        self.assertIsNone(codex_subagent.root_of(self.home.path, DEEP)[0])

    def test_the_parent_has_to_be_a_live_main_conversation_in_the_same_directory(self):
        self.home.thread(MAIN)
        self.home.thread(OTHER, cwd="/w/elsewhere")
        self.home.thread(FORK, archived=1)
        self.home.thread(GUARD2, GUARDIAN_SOURCE)
        problems = [codex_subagent.parent_problem(self.home.path, t, CWD) for t in (MAIN, OTHER, FORK, GUARD2, REVIEW)]
        self.assertEqual(problems[0], None)
        self.assertTrue(all(problems[1:]), problems)


class Heal(Base):
    def setUp(self):
        super().setUp()
        self.home.thread(MAIN, "vscode", created=NOW - 3600)
        self.home.thread(GUARD, GUARDIAN_SOURCE, created=NOW - 3590, thread_source="guardian_review")
        self.home.rollout(GUARD, source=GUARDIAN_SOURCE, parent=MAIN)
        self.panes = [Pane("a", "%1", 100, "zsh", CWD)]
        self.procs = list_procs(ps_runner([ps_line(100, 1, "-zsh", "-zsh")]))

    def row(self, name="a", sid=GUARD, state="active", cwd=CWD):
        return Row(name, "codex", "codex", sid, cwd, state)

    def heal(self, rows, observed=(), panes=None, procs=None):
        return codex_subagent.heal_rows(self.cfg, FakeRunner(), rows, observed, self.panes if panes is None else panes,
                                        self.procs if procs is None else procs, snapshot_fn=lambda *a, **k: None)

    def test_a_stopped_row_goes_back_to_its_parent(self):
        rows, healed, warnings = self.heal([self.row()])
        self.assertEqual([r.session_id for r in rows], [MAIN])
        self.assertEqual(healed, [("a", GUARD, MAIN)])
        self.assertEqual(warnings, [])

    def test_what_this_round_observed_wins(self):  # /new, /clear: the screen's own answer is not rolled back
        rows, healed, _ = self.heal([self.row()], observed={"a"})
        self.assertEqual((rows[0].session_id, healed), (GUARD, []))

    def test_retired_and_ordinary_rows_stay(self):
        rows, healed, _ = self.heal([self.row(state="retired"), self.row("b", sid=MAIN), Row("c", "claude", "alice", GUARD, CWD)])
        self.assertEqual([r.session_id for r in rows], [GUARD, MAIN, GUARD])
        self.assertEqual(healed, [])

    def test_a_codex_running_in_the_session_without_being_recognised_is_only_reported(self):
        procs = list_procs(ps_runner([ps_line(100, 1, "-zsh", "-zsh"), ps_line(201, 100, "codex", NATIVE)]))
        rows, healed, warnings = self.heal([self.row()], procs=procs)
        self.assertEqual((rows[0].session_id, healed), (GUARD, []))
        self.assertIn("沒認出", warnings[0])

    def test_another_live_codex_in_the_directory_stops_it(self):
        procs = list_procs(ps_runner([ps_line(100, 1, "-zsh", "-zsh"), ps_line(110, 1, "-zsh", "-zsh"), ps_line(211, 110, "codex", NATIVE)]))
        panes = [Pane("a", "%1", 100, "zsh", CWD), Pane("b", "%2", 110, "codex", CWD)]
        rows, healed, warnings = self.heal([self.row()], panes=panes, procs=procs)
        self.assertEqual(healed, [])
        self.assertIn("同目錄", warnings[0])

    def test_the_parent_already_on_another_row_is_never_duplicated(self):
        for state in ("active", "pending"):
            rows, healed, warnings = self.heal([self.row(), self.row("b", sid=MAIN, state=state)])
            self.assertEqual([r.session_id for r in rows], [GUARD, MAIN], state)
            self.assertEqual(healed, [])
            self.assertIn("別的 session", warnings[0])

    def test_no_parent_found_changes_nothing(self):
        self.home.thread(GUARD2, GUARDIAN_SOURCE)
        rows, healed, warnings = self.heal([self.row(sid=GUARD2)])
        self.assertEqual((rows[0].session_id, healed), (GUARD2, []))
        self.assertIn("找不到父對話", warnings[0])

    def test_only_the_spawn_edge_and_the_rollout_gone(self):
        self.home.thread(SPAWNED, SPAWN_SOURCE)
        self.home.edge(MAIN, SPAWNED)
        rows, healed, _ = self.heal([self.row(sid=SPAWNED)])
        self.assertEqual(healed, [("a", SPAWNED, MAIN)])

    def test_a_parent_that_is_itself_a_subagent_goes_up(self):
        self.home.thread(SPAWNED, SPAWN_SOURCE)
        self.home.thread(DEEP, SPAWN_SOURCE)
        self.home.edge(SPAWNED, DEEP)
        self.home.edge(MAIN, SPAWNED)
        _, healed, _ = self.heal([self.row(sid=DEEP)])
        self.assertEqual(healed, [("a", DEEP, MAIN)])

    def test_the_switch_turns_it_off(self):
        import dataclasses
        self.cfg = dataclasses.replace(self.cfg, heal_subagent=False)
        rows, healed, warnings = self.heal([self.row()])
        self.assertEqual((rows[0].session_id, healed, warnings), (GUARD, [], []))


class SaveEndToEnd(Base):
    """cmd_save with the registry on disk: the write, the summary, the log, --dry-run."""

    def setUp(self):
        super().setUp()
        self.home.thread(MAIN, "vscode", created=NOW - 3600)
        self.home.thread(GUARD, GUARDIAN_SOURCE, created=NOW - 3590, thread_source="guardian_review")
        self.home.rollout(GUARD, source=GUARDIAN_SOURCE, parent=MAIN)
        write_rows(self.cfg.registry_path, [Row("a", "codex", "codex", GUARD, CWD, "active", "2026-11-28T00:00:00Z")], "t",
                   self.cfg.history_dir)

    def run_save(self, **kw):
        run = FakeRunner()
        run.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="1-2\n") if "-F" in a and "@sampan_id" not in " ".join(a) else cp(out=""))
        run.on(["tmux", "-u", "set-option"], cp())
        run.on(["tmux", "-u", "list-panes"], cp(out=pane_line("a", "%1", 100, "zsh", CWD) + "\n"))
        run.on(["ps", "-A"], cp(out=ps_line(100, 1, "-zsh", "-zsh") + "\n"))
        out = []
        rc = save.cmd_save(self.cfg, run, out.append, proc_root=Path(self.tmp.name) / "no-proc", **kw)
        return rc, "\n".join(out)

    def sid(self):
        return load_rows(self.cfg.registry_path, codex_profile="codex")[0].session_id

    def test_dry_run_previews_without_writing_or_printing_a_path(self):
        with mock.patch.object(save, "append_log"):
            rc, text = self.run_save(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("改回父對話", text)
        self.assertIn(GUARD[:8], text)
        self.assertNotIn("/", text.replace("dry-run", ""))
        self.assertEqual(self.sid(), GUARD)

    def test_save_writes_it_and_the_summary_and_log_say_so(self):
        logged = []
        with mock.patch.object(save, "append_log", lambda cfg, msg: logged.append(msg)):
            rc, text = self.run_save()
        self.assertEqual(rc, 0)
        self.assertEqual(self.sid(), MAIN)
        self.assertIn("改回父對話 1", text)
        self.assertIn("改回父對話 1", logged[-1])


class Refusals(Base):
    def test_no_message_is_queued_on_a_subagent(self):
        self.home.thread(GUARD, GUARDIAN_SOURCE)
        run = FakeRunner(strict=True)  # `codex queue` must not even be started
        self.assertEqual(deliver.to_codex(self.cfg, run, GUARD, "hi"), deliver.REASON_SUBAGENT)
        self.assertEqual(run.calls, [])

    def test_a_main_conversation_is_queued_as_before(self):
        self.home.thread(MAIN)
        run = FakeRunner(strict=True).on(["codex", "queue"], cp())
        self.assertIsNone(deliver.to_codex(self.cfg, run, MAIN, "hi"))

    def test_doctor_warns_and_says_what_save_will_do(self):
        self.home.thread(MAIN)
        self.home.thread(GUARD, GUARDIAN_SOURCE)
        self.home.rollout(GUARD, source=GUARDIAN_SOURCE, parent=MAIN)
        write_rows(self.cfg.registry_path, [Row("a", "codex", "codex", GUARD, CWD)], "t", self.cfg.history_dir)
        checks = doctor._codex_registered(self.cfg)
        self.assertEqual([c.level for c in checks], ["warn"])
        self.assertIn("改回父對話", checks[0].fix if hasattr(checks[0], "fix") else str(checks[0]))
        write_rows(self.cfg.registry_path, [Row("a", "codex", "codex", MAIN, CWD)], "t", self.cfg.history_dir)
        self.assertEqual([c.level for c in doctor._codex_registered(self.cfg)], ["ok"])

    def test_doctor_says_why_when_it_cannot(self):
        self.home.thread(GUARD, GUARDIAN_SOURCE)
        write_rows(self.cfg.registry_path, [Row("a", "codex", "codex", GUARD, CWD)], "t", self.cfg.history_dir)
        (check,) = doctor._codex_registered(self.cfg)
        self.assertEqual(check.level, "warn")
        self.assertIn("找不到父對話", str(check))


class Config(Base):
    def test_the_switch_works_with_and_without_a_codex_profile_in_the_file(self):
        from sampan.config import HEAL_SUBAGENT_DEFAULT, load_config
        from tests.helpers import TOML
        self.assertEqual(self.cfg.heal_subagent, HEAL_SUBAGENT_DEFAULT)
        codex_profile = "\n[profiles.codex-a]\nagent = 'codex'\nconfig_dir = '~/.codex'\n"  # [codex] is ignored from here on
        for extra in ("", codex_profile):
            for value in ("false", "true"):
                self.cfg.path.write_text(TOML + extra + f"\n[save]\nheal_subagent = {value}\n")
                cfg = load_config(self.cfg.path, home=self.cfg.home, state_dir=self.cfg.state_dir)
                self.assertEqual(cfg.heal_subagent, value == "true", (bool(extra), value))
                self.assertEqual(cfg.codex.enabled, True)

    def test_a_non_boolean_is_refused(self):
        from sampan.config import ConfigError, load_config
        from tests.helpers import TOML
        self.cfg.path.write_text(TOML + "\n[save]\nheal_subagent = 'yes'\n")
        with self.assertRaises(ConfigError):
            load_config(self.cfg.path, home=self.cfg.home, state_dir=self.cfg.state_dir)


if __name__ == "__main__":
    unittest.main()
