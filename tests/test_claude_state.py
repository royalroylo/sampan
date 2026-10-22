import json
import tempfile
import unittest
from pathlib import Path

from sampan.claude_state import (
    find_transcript,
    last_user_text,
    read_session_files,
    stale_session_files,
    transcript_owners,
    transcript_stats,
)
from tests.helpers import make_env

SID = "e70d5dbc-0000-4000-8000-000000000001"


def write_session(cfg, profile, pid, sid=SID, cwd="/Users/u/proj", status="idle", tmux="001-proj:@1.%3", raw=None):
    d = cfg.profiles[profile].config_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    body = raw if raw is not None else json.dumps(
        {"pid": pid, "sessionId": sid, "cwd": cwd, "status": status, "tmux": tmux, "kind": "interactive"})
    (d / f"{pid}.json").write_text(body)
    (d / f"{pid}.abc.key").write_text("x")  # Claude Code also keeps .key files in the same directory; they must not be taken for session files


class SessionFilesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_reads_live_sessions_with_profile_and_pane(self):
        write_session(self.cfg, "work", 5359)
        write_session(self.cfg, "alice", 26772, sid="b19e16af-0000-4000-8000-000000000002", tmux="1001-x:@8.%8", status="busy")
        live, warnings = read_session_files(self.cfg.profiles.values(), {5359, 26772})
        by_pid = {l.pid: l for l in live}
        self.assertEqual(by_pid[5359].profile, "work")
        self.assertEqual(by_pid[5359].pane_id, "%3")
        self.assertEqual(by_pid[26772].status, "busy")
        self.assertEqual(warnings, [])

    def test_dead_pid_ignored(self):
        write_session(self.cfg, "work", 5359)
        live, _ = read_session_files(self.cfg.profiles.values(), set())
        self.assertEqual(live, [])

    def test_missing_tmux_field_gives_no_pane(self):
        write_session(self.cfg, "alice", 77, tmux="")
        live, _ = read_session_files(self.cfg.profiles.values(), {77})
        self.assertIsNone(live[0].pane_id)

    def test_unrecognised_format_is_skipped_with_warning(self):
        # Review Focus 1: a format change must not make the whole read fail
        write_session(self.cfg, "alice", 10, raw="not json")
        write_session(self.cfg, "alice", 11, raw=json.dumps({"pid": 11}))  # missing sessionId
        write_session(self.cfg, "alice", 12)
        live, warnings = read_session_files(self.cfg.profiles.values(), {10, 11, 12})
        self.assertEqual([l.pid for l in live], [12])
        self.assertEqual(len(warnings), 2)

    def test_null_or_bad_session_id_is_skipped_not_none(self):
        write_session(self.cfg, "alice", 13, sid=None)
        write_session(self.cfg, "alice", 14, sid="")
        write_session(self.cfg, "alice", 15, sid="../x")
        live, warnings = read_session_files(self.cfg.profiles.values(), {13, 14, 15})
        self.assertEqual(live, [])
        self.assertEqual(len(warnings), 3)

    def test_pid_must_match_filename(self):
        # Review R2: 200.json claiming pid 1 -> must not be trusted (otherwise --force would kill the wrong process)
        write_session(self.cfg, "alice", 200, raw=json.dumps({"pid": 1, "sessionId": SID, "cwd": "/c", "tmux": "x:@1.%1"}))
        live, warnings = read_session_files(self.cfg.profiles.values(), {1, 200})
        self.assertEqual(live, [])
        self.assertEqual(len(warnings), 1)

    def test_stale_files(self):
        write_session(self.cfg, "alice", 10)
        write_session(self.cfg, "alice", 12)
        stale = stale_session_files(self.cfg.profiles.values(), {12})
        self.assertEqual([p.name for p in stale], ["10.json"])


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_find_transcript(self):
        d = self.cfg.default_profile.projects_dir / "-Users-u-proj"
        d.mkdir(parents=True)
        (d / f"{SID}.jsonl").write_text("")
        profiles = self.cfg.profiles.values()
        self.assertEqual(find_transcript(profiles, SID), d / f"{SID}.jsonl")
        self.assertIsNone(find_transcript(profiles, "nope"))
        # an unvalidated id must not be globbed: "*" would match any transcript
        for bad in ("*", "", None, "../-Users-u-proj/" + SID):
            self.assertIsNone(find_transcript(profiles, bad))

    def test_transcript_owners_follow_symlinked_projects(self):
        alice, work = self.cfg.profiles["alice"], self.cfg.profiles["work"]
        d = alice.projects_dir / "-x"
        d.mkdir(parents=True)
        (d / f"{SID}.jsonl").write_text("")
        profiles = list(self.cfg.profiles.values())
        self.assertEqual(transcript_owners(profiles, d / f"{SID}.jsonl"), ["alice"])
        work.projects_dir.symlink_to(alice.projects_dir)  # shared projects/
        self.assertEqual(transcript_owners(profiles, d / f"{SID}.jsonl"), ["alice", "work"])

    def test_find_transcript_prefers_profile_then_others(self):
        alice, work = self.cfg.profiles["alice"], self.cfg.profiles["work"]
        profiles = list(self.cfg.profiles.values())
        wd = work.projects_dir / "-x"
        wd.mkdir(parents=True)
        (wd / f"{SID}.jsonl").write_text("")
        self.assertEqual(find_transcript(profiles, SID, prefer="alice"), wd / f"{SID}.jsonl")
        ad = alice.projects_dir / "-x"
        ad.mkdir(parents=True, exist_ok=True)
        (ad / f"{SID}.jsonl").write_text("")
        self.assertEqual(find_transcript(profiles, SID, prefer="alice"), ad / f"{SID}.jsonl")
        self.assertEqual(find_transcript(profiles, SID, prefer="work"), wd / f"{SID}.jsonl")

    def test_stats_last_activity_and_context(self):
        p = self.cfg.default_profile.projects_dir / "t.jsonl"
        lines = [
            {"type": "user", "timestamp": "2026-10-17T02:00:00.000Z", "message": {"role": "user"}},
            {"type": "assistant", "timestamp": "2026-10-17T02:01:00.000Z",
             "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 500000, "cache_creation_input_tokens": 42000}}},
            {"type": "summary", "timestamp": "2026-10-17T05:00:00.000Z"},
        ]
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\nbroken line\n")
        last, tokens = transcript_stats(p)
        self.assertEqual(last, "2026-10-17T02:01:00.000Z")
        self.assertEqual(tokens, 542010)

    def test_last_user_text_skips_injected_tags(self):
        p = self.cfg.default_profile.projects_dir / "u.jsonl"
        texts = ["幫我修 adopt", "<user-prompt-submit-hook>注入</user-prompt-submit-hook>",
                 "<ide_opened_file>a.py</ide_opened_file>", "  <command-name>/exit</command-name>"]
        p.write_text("\n".join(json.dumps({"type": "user", "message": {"role": "user", "content": t}}) for t in texts))
        self.assertEqual(last_user_text(p), "幫我修 adopt")

    def test_last_user_text_keeps_text_that_only_starts_with_angle(self):
        p = self.cfg.default_profile.projects_dir / "v.jsonl"
        p.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "<= 3 的情況要怎麼辦"}}))
        self.assertEqual(last_user_text(p), "<= 3 的情況要怎麼辦")

    def test_stats_and_last_text_are_cached_until_the_file_changes(self):
        import os

        from sampan import claude_state
        p = self.cfg.default_profile.projects_dir / "c.jsonl"
        rec = {"type": "user", "timestamp": "2026-10-17T02:00:00.000Z", "message": {"role": "user", "content": "一"}}
        p.write_text(json.dumps(rec) + "\n")
        self.assertEqual(last_user_text(p), "一")
        calls = []
        orig = claude_state.reverse_lines
        claude_state.reverse_lines = lambda *a, **k: calls.append(1) or orig(*a, **k)
        try:
            last_user_text(p)
            transcript_stats(p)
            transcript_stats(p)
            self.assertEqual(len(calls), 1)  # last_user_text is already cached; transcript_stats is only read the first time
            rec["message"]["content"] = "二"
            with open(p, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            os.utime(p, ns=(1, 2))
            self.assertEqual(last_user_text(p), "二")
        finally:
            claude_state.reverse_lines = orig

    def test_stats_empty(self):
        p = self.cfg.default_profile.projects_dir / "e.jsonl"
        p.write_text("")
        self.assertEqual(transcript_stats(p), (None, None))
