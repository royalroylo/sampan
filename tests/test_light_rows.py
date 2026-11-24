"""Rows without the conversation activity (status_rows_light) for sending, watching and heartbeats; the transcript
path cache that only the display uses."""
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sampan import claude_state, dispatch, mcp_server, remote, send, status
from sampan.registry import Row, load_rows, write_rows
from sampan.send import Caller
from sampan.status import State, status_rows, status_rows_light
from sampan.ui import cards
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import make_env
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-0000000000a1"
B = "bbbbbbbb-0000-4000-8000-0000000000b2"
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
ACTIVITY = ("last", "tokens", "context_window")


def transcript(cfg, profile, sid, folder="-c", ts="2026-11-24T03:00:00.000Z", text="hello"):
    d = cfg.profiles[profile].projects_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    path.write_text(json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}) + "\n"
                    + json.dumps({"type": "assistant", "timestamp": ts,
                                  "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 1000}}}) + "\n")
    return path


class Env(unittest.TestCase):
    """Two registered Claude sessions in tmux: a (idle) and b (busy), each with a transcript."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.cfg = make_env(Path(tmp))
        env = mock.patch.dict("os.environ", {"CODEX_HOME": str(self.cfg.codex.home)})
        env.start()
        self.addCleanup(env.stop)
        write_rows(self.cfg.registry_path, [Row("a", "claude", "alice", A, "/c"), Row("b", "claude", "alice", B, "/c")],
                   "t", self.cfg.history_dir)
        self.ids = {r.tmux: r.id for r in load_rows(self.cfg.registry_path)}
        write_session(self.cfg, "alice", 200, sid=A, tmux="a:@1.%1", status="idle")
        write_session(self.cfg, "alice", 201, sid=B, tmux="b:@2.%2", status="busy")
        self.paths = {A: transcript(self.cfg, "alice", A), B: transcript(self.cfg, "alice", B)}
        panes = [pane_line("a", "%1", 100, "claude", "/c"), pane_line("b", "%2", 101, "claude", "/c")]
        procs = [ps_line(100, 1, "-zsh", "-zsh"), ps_line(101, 1, "-zsh", "-zsh"),
                 ps_line(200, 100, "claude", "claude"), ps_line(201, 101, "claude", "claude")]
        self.run = FakeRunner()
        self.run.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        self.run.on(["ps", "-A"], cp(out="\n".join(procs) + "\n"))
        status._DISPLAY_PATHS.clear()
        cards._QUOTES.clear()

    def server(self, **kw):
        caller = Caller("a", "alice", "claude", "%1", id=self.ids["a"])
        return mcp_server.Server(self.cfg, self.run, io.StringIO(), io.StringIO(), caller=caller,
                                 sleep=lambda _s: None, clock=lambda: 0.0, now_fn=lambda: NOW,
                                 start_of=lambda pid: "Mon Nov 24 10:00:00 2026", **kw)

    def count_activity(self):
        spy = mock.patch.object(status, "_activity", wraps=status._activity)
        m = spy.start()
        self.addCleanup(spy.stop)
        return m


class LightRowsTest(Env):
    def test_light_rows_equal_full_rows_but_for_the_activity(self):
        full, light = status_rows(self.cfg, self.run), status_rows_light(self.cfg, self.run)
        self.assertEqual(len(full), len(light))
        self.assertIsNotNone(full[0].last)
        for f, g in zip(full, light):
            for field in f.__dataclass_fields__:
                if field in ACTIVITY:
                    self.assertIsNone(getattr(g, field), field)
                else:
                    self.assertEqual(getattr(f, field), getattr(g, field), field)

    def test_light_rows_read_no_transcript(self):
        seen = self.count_activity()
        with mock.patch.object(status, "find_transcript", wraps=status.find_transcript) as find:
            status_rows_light(self.cfg, self.run)
        self.assertEqual((seen.call_count, find.call_count), (0, 0))

    def test_unreadable_tmux_light_rows_have_no_activity_either(self):
        self.run.on(["tmux", "-u", "list-panes"], cp(1, "", "permission denied"))
        full, light = status_rows(self.cfg, self.run), status_rows_light(self.cfg, self.run)
        self.assertTrue(light.warning)
        self.assertEqual(full.warning, light.warning)
        self.assertIsNotNone(full[0].last)
        for f, g in zip(full, light):
            self.assertEqual((f.name, f.state, f.sid, f.id), (g.name, g.state, g.sid, g.id))
            self.assertEqual((g.last, g.tokens, g.context_window), (None, None, None))
            self.assertEqual(g.state, State.UNKNOWN)

    def test_send_rows_default_to_light(self):
        seen = self.count_activity()
        rows = send._rows(self.cfg, self.run, None, None)
        self.assertEqual(seen.call_count, 0)
        self.assertIsNone(rows["a"].last)
        injected = []
        send._rows(self.cfg, self.run, None, lambda cfg, run, snapshot_fn=None: injected.append(1) or [])
        self.assertEqual(injected, [1])  # an injected status_fn is used as it is


class ServerRowsTest(Env):
    def test_watch_once_and_beat_read_no_activity(self):
        d = send.submit(self.cfg, self.run, Caller("a", "alice", "claude", "%1", id=self.ids["a"]), "b", "hi",
                        notify=True, now=NOW)
        self.assertNotIsInstance(d, str)
        server = self.server()
        seen = self.count_activity()
        server.beat()
        server.watch_once()
        self.assertEqual(seen.call_count, 0)
        [after] = dispatch.load(self.cfg.state_dir)
        self.assertEqual(after.state, "queued")  # b is busy: it waits
        with dispatch.transaction(self.cfg.state_dir) as items:  # the heartbeat was written (with the conversation)
            hb = items.conn.execute("SELECT * FROM inboxes").fetchall()
        self.assertEqual([r["conversation"] for r in hb], [A])

    def test_send_batch_and_inbox_tools_read_no_activity(self):
        server = self.server()
        seen = self.count_activity()
        out = server.tool_send({"session": "b", "message": "hi"})
        self.assertEqual(out["state"], "queued")  # b is busy
        with self.assertRaises(mcp_server.ToolError):  # refused (b is busy), after the rows were read
            server.tool_send_batch({"sessions": ["b"], "message": "again"})
        server.tool_inbox_take({})
        self.assertEqual(seen.call_count, 0)
        self.assertEqual(len(dispatch.load(self.cfg.state_dir)), 1)

    def test_list_sessions_still_shows_last_activity(self):
        server = self.server()
        out = server.tool_list_sessions({})
        text = json.dumps(out, ensure_ascii=False)
        self.assertIn("2026-11-24T03:00:00+00:00", text)

    def test_an_injected_status_fn_serves_both(self):
        def fake(cfg, run, snapshot_fn=None):
            return []
        server = self.server(status_fn=fake)
        self.assertIs(server.status_fn, fake)
        self.assertIs(server.watch_status_fn, fake)
        plain = self.server()
        self.assertIs(plain.status_fn, status_rows)
        self.assertIsNone(plain.watch_status_fn)  # send's own default (status_rows_light)


class RemoteDefaultsTest(Env):
    def test_remote_hooks_use_light_rows_by_default(self):
        class Store:
            def published(self):
                return [{"session": "a", "alias": ""}]
        seen = self.count_activity()
        self.assertEqual(remote.published_status(self.cfg, self.run, Store()),
                         [{"session": "a", "alias": "", "status": "idle"}])
        hooks = remote.make_hooks(self.cfg, self.run, Store())
        self.assertEqual(hooks.published_status()[0]["status"], "idle")
        self.assertEqual(seen.call_count, 0)
        for fn in (remote.make_deliver_local, remote.published_status, remote.make_hooks):
            self.assertIsNone(fn.__defaults__[-1], fn.__name__)


class DisplayCacheTest(Env):
    def find_count(self):
        spy = mock.patch.object(status, "find_transcript", wraps=claude_state.find_transcript)
        m = spy.start()
        self.addCleanup(spy.stop)
        return m

    def test_a_found_path_is_remembered(self):
        find = self.find_count()
        profiles = self.cfg.profiles.values()
        self.assertEqual(status.display_transcript(profiles, A, "alice"), self.paths[A])
        self.assertEqual(status.display_transcript(profiles, A, "alice"), self.paths[A])
        self.assertEqual(find.call_count, 1)

    def test_a_moved_transcript_is_found_again(self):
        profiles = self.cfg.profiles.values()
        status.display_transcript(profiles, A, "alice")
        new = self.paths[A].parent.parent / "-elsewhere" / self.paths[A].name
        new.parent.mkdir()
        os.replace(self.paths[A], new)
        self.assertEqual(status.display_transcript(profiles, A, "alice"), new)

    def test_a_deleted_transcript_is_not_found(self):
        profiles = self.cfg.profiles.values()
        status.display_transcript(profiles, A, "alice")
        self.paths[A].unlink()
        self.assertIsNone(status.display_transcript(profiles, A, "alice"))

    def test_a_miss_is_not_remembered(self):
        profiles = self.cfg.profiles.values()
        sid = "cccccccc-0000-4000-8000-0000000000c3"
        self.assertIsNone(status.display_transcript(profiles, sid, "alice"))
        path = transcript(self.cfg, "alice", sid)
        self.assertEqual(status.display_transcript(profiles, sid, "alice"), path)

    def test_another_config_is_another_key(self):
        other = make_env(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, other.home.parent, True)
        status.display_transcript(self.cfg.profiles.values(), A, "alice")
        self.assertIsNone(status.display_transcript(other.profiles.values(), A, "alice"))

    def test_dispatch_decisions_look_every_time(self):
        status_rows(self.cfg, self.run)  # the display has the paths cached
        self.paths[B].unlink()
        self.assertIsNone(claude_state.find_transcript(self.cfg.profiles.values(), B, prefer="alice"))
        find = self.find_count()
        status_rows(self.cfg, self.run)
        status_rows(self.cfg, self.run)
        self.assertEqual(find.call_count, 2)  # b's miss is looked up each time; a comes from the cache

    def test_dispatch_decisions_never_use_the_display_cache(self):
        caller = Caller("a", "alice", "claude", "%1", id=self.ids["a"])
        d = send.submit(self.cfg, self.run, caller, "b", "hi", notify=True, now=NOW)
        with dispatch.transaction(self.cfg.state_dir) as items:  # as if it went out: the sent branch reads b's transcript
            x = next(i for i in items if i.id == d.id)
            x.state, x.sent_at, x.thread_id = "sent", NOW.isoformat(), B
        status_rows(self.cfg, self.run)  # the display has b's path cached
        spy = mock.patch.object(status, "display_transcript", wraps=status.display_transcript)
        cached = spy.start()
        self.addCleanup(spy.stop)
        with mock.patch.object(send, "find_transcript", wraps=send.find_transcript) as find:
            send.advance(self.cfg, self.run, d.id, sleep=lambda _s: None, now=NOW)
            self.server().watch_once()
        self.assertEqual(cached.call_count, 0)
        self.assertGreater(find.call_count, 0)  # the judgement looked for the transcript itself

    def test_a_remembered_path_is_looked_up_again_after_the_ttl(self):
        t = [100.0]
        calls = []

        def find():
            calls.append(1)
            return self.paths[A]
        key = ("k",)
        status._cached_path(key, find, clock=lambda: t[0])
        t[0] += status.DISPLAY_PATH_TTL_S - 1
        status._cached_path(key, find, clock=lambda: t[0])
        self.assertEqual(len(calls), 1)
        t[0] += 2
        status._cached_path(key, find, clock=lambda: t[0])
        self.assertEqual(len(calls), 2)

    def test_card_quote_is_read_again_only_when_the_file_changes(self):
        with mock.patch.object(claude_state, "last_user_text", wraps=claude_state.last_user_text) as read:
            self.assertEqual(cards._quote_of(self.cfg, "claude", "alice", A), "hello")
            self.assertEqual(cards._quote_of(self.cfg, "claude", "alice", A), "hello")
            self.assertEqual(read.call_count, 1)
            with self.paths[A].open("a") as fh:
                fh.write(json.dumps({"type": "user", "timestamp": "2026-11-24T04:00:00.000Z",
                                     "message": {"role": "user", "content": "second"}}) + "\n")
            self.assertEqual(cards._quote_of(self.cfg, "claude", "alice", A), "second")
            self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
