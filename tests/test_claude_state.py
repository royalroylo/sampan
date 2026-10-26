import json
import tempfile
import unittest
from pathlib import Path

from sampan.claude_state import (
    Reply,
    find_transcript,
    last_user_text,
    read_session_files,
    reply_to,
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

    def test_last_user_text_skips_compaction_summary(self):
        p = self.cfg.default_profile.projects_dir / "w.jsonl"
        summary = ("<artifact-content-authored-by-others/>\nThe summarized conversation so far: alice asked for a fix…")
        recs = [{"type": "user", "message": {"role": "user", "content": "改好 adopt"}},
                {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
                 "message": {"role": "user", "content": summary}}]
        p.write_text("\n".join(json.dumps(x) for x in recs))
        self.assertEqual(last_user_text(p), "改好 adopt")
        # the flags alone are enough, and so is a self-closing leading tag alone
        recs[1]["message"]["content"] = "plain summary text"
        p.write_text("\n".join(json.dumps(x) for x in recs))
        self.assertEqual(last_user_text(p), "改好 adopt")
        recs[1] = {"type": "user", "message": {"role": "user", "content": summary}}
        p.write_text("\n".join(json.dumps(x) for x in recs))
        self.assertEqual(last_user_text(p), "改好 adopt")

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


FIX = Path(__file__).resolve().parent / "fixtures"


def u(content, **extra):
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


def a(*parts):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(parts)}}


def text(t):
    return {"type": "text", "text": t}


def write_jsonl(recs):
    f = Path(tempfile.mkdtemp()) / "t.jsonl"
    f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    return f


SKILL = u([text("Base directory for this skill: /tmp/skills/x\n\n# X\nsteps")], isMeta=True, sourceToolUseID="toolu_1")


class ReplyToTest(unittest.TestCase):
    def test_real_transcript_multiline_paste(self):
        sent = json.loads((FIX / "claude_transcript_paste.jsonl").read_text().splitlines()[0])["message"]["content"]
        self.assertEqual(reply_to(FIX / "claude_transcript_paste.jsonl", sent), Reply(True, True, "OK"))

    def test_real_transcript_long_paste_is_wrapped(self):
        f = FIX / "claude_transcript_pasted_content.jsonl"
        long = "L1 只回 OK\nL2\nL3\nL4\nL5\nL6\nL7\nL8\nL9\nL10"
        self.assertEqual(reply_to(f, long).found, True)
        self.assertEqual(reply_to(f, long + "\n").found, True)  # trailing newlines never count
        self.assertTrue(reply_to(f, long).text.startswith("你貼了一段 10 行"))
        short = reply_to(f, "A1 只回 OK\nA2\nA3")
        self.assertTrue(short.done and short.text.startswith("A1–A3"))
        self.assertEqual(reply_to(f, "L1 只回 OK\nL2"), Reply(False, False, ""))  # a prefix is not the message

    def test_wrapped_record_needs_matching_ids_and_one_block(self):
        w = lambda i, j, body: u("\n\n<pasted_content id=\"%s\">\n%s\n</pasted_content id=\"%s\">\n" % (i, body, j))
        f = write_jsonl([w("ab12", "cd34", "x\ny\nz\nw"), a(text("r"))])
        self.assertEqual(reply_to(f, "x\ny\nz\nw").found, False)
        f = write_jsonl([w("ab12", "ab12", "x\ny\nz\nw"), a(text("r"))])
        self.assertEqual(reply_to(f, "x\ny\nz\nw"), Reply(True, True, "r"))
        f = write_jsonl([w("ab12", "ab12", "x\ny\nz\nw"), a(text("r"))])
        self.assertEqual(reply_to(f, "Base directory for this skill: /tmp/skills/x"), Reply(False, False, ""))

    def test_question_and_answer(self):
        f = write_jsonl([u("old"), a(text("old reply")), u("hi"), a(text("hello"))])
        self.assertEqual(reply_to(f, "hi"), Reply(True, True, "hello"))

    def test_skill_injection_is_not_the_reply(self):
        f = write_jsonl([u("hi"), a({"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {}}),
                         u([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]), SKILL,
                         a(text("done it"))])
        self.assertEqual(reply_to(f, "hi"), Reply(True, True, "done it"))

    def test_skill_injection_only_is_no_reply_yet(self):
        f = write_jsonl([u("hi"), SKILL])
        self.assertEqual(reply_to(f, "hi"), Reply(True, False, ""))

    def test_injected_tag_and_non_text_user_records_skipped(self):
        f = write_jsonl([u("hi"), u("<system-reminder>x</system-reminder>"), u("<ide_opened_file>y</ide_opened_file>"),
                         u([text("a"), {"type": "image"}]), a(text("fine"))])
        self.assertEqual(reply_to(f, "hi"), Reply(True, True, "fine"))

    def test_skill_text_never_bound_as_sent(self):
        f = write_jsonl([SKILL, a(text("x"))])
        self.assertEqual(reply_to(f, "Base directory for this skill: /tmp/skills/x\n\n# X\nsteps"),
                         Reply(False, False, ""))

    def test_compaction_summary_skipped(self):
        f = write_jsonl([u("hi"), u("This session is being continued...", isCompactSummary=True),
                         u("summary", isVisibleInTranscriptOnly=True), a(text("after compact"))])
        self.assertEqual(reply_to(f, "hi"), Reply(True, True, "after compact"))

    def test_compaction_summary_equal_to_sent_not_bound(self):
        f = write_jsonl([u("hi", isCompactSummary=True), a(text("x"))])
        self.assertEqual(reply_to(f, "hi"), Reply(False, False, ""))

    def test_no_assistant_yet(self):
        f = write_jsonl([u("hi")])
        self.assertEqual(reply_to(f, "hi"), Reply(True, False, ""))

    def test_thinking_only_is_not_text(self):
        f = write_jsonl([u("hi"), a({"type": "thinking", "thinking": "", "signature": "x"})])
        self.assertEqual(reply_to(f, "hi"), Reply(True, False, ""))

    def test_last_assistant_text_wins_and_tool_use_between(self):
        f = write_jsonl([u("hi"), a(text("working")), a({"type": "tool_use", "id": "t", "name": "Bash", "input": {}}),
                         u([{"type": "tool_result", "tool_use_id": "t", "content": "o"}]), a(text("final"))])
        self.assertEqual(reply_to(f, "hi"), Reply(True, True, "final"))

    def test_cleared_conversation_has_no_match(self):
        f = write_jsonl([u("something else"), a(text("not for you"))])
        self.assertEqual(reply_to(f, "hi"), Reply(False, False, ""))

    def test_duplicate_sent_takes_last(self):
        f = write_jsonl([u("hi"), a(text("first")), u("hi")])
        self.assertEqual(reply_to(f, "hi"), Reply(True, False, ""))
        f = write_jsonl([u("hi"), a(text("first")), u("hi"), a(text("second"))])
        self.assertEqual(reply_to(f, "hi").text, "second")

    def test_later_user_message_ends_the_window(self):
        f = write_jsonl([u("hi"), a(text("mine")), u("next question"), a(text("answer to next"))])
        self.assertEqual(reply_to(f, "hi"), Reply(True, True, "mine", closed=True))

    def test_closed_without_text_and_open_window(self):  # final review 4: the window closes at a later typed message
        f = write_jsonl([u("hi"), u("typed on"), a(text("answer to typed on"))])
        self.assertEqual(reply_to(f, "hi"), Reply(True, False, "", closed=True))
        f = write_jsonl([u("hi")])
        self.assertEqual(reply_to(f, "hi"), Reply(True, False, "", closed=False))

    def test_text_list_content_matches(self):
        f = write_jsonl([u([text("multi\nline")]), a(text("ok"))])
        self.assertEqual(reply_to(f, "multi\nline"), Reply(True, True, "ok"))

    def test_unreadable_or_missing_file(self):
        self.assertEqual(reply_to(Path(tempfile.mkdtemp()) / "none.jsonl", "hi"), Reply(False, False, ""))

    def test_garbage_lines_skipped(self):
        f = Path(tempfile.mkdtemp()) / "t.jsonl"
        f.write_text("not json\n" + json.dumps(u("hi")) + "\n[1]\n" + json.dumps(a(text("ok"))) + "\n")
        self.assertEqual(reply_to(f, "hi"), Reply(True, True, "ok"))
