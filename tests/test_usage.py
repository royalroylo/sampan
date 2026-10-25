import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sampan import usage
from tests.helpers import local_tz, make_env

NOW = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)  # 12:00 in Asia/Taipei
SOON = int((NOW + timedelta(hours=3)).timestamp())
LATER = int((NOW + timedelta(days=3)).timestamp())
PAST = int((NOW - timedelta(minutes=1)).timestamp())


def claude_data(five=42, week=18, five_reset=SOON, week_reset=LATER):
    return {"model": {"display_name": "Opus"}, "workspace": {"current_dir": "/w/proj"}, "cwd": "/w/proj",
            "context_window": {"used_percentage": 37.4},
            "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": five_reset},
                            "seven_day": {"used_percentage": week, "resets_at": week_reset}}}


def codex_line(ts, limit_id, primary, secondary):
    return json.dumps({"timestamp": ts, "type": "event_msg",
                       "payload": {"type": "token_count", "info": None,
                                   "rate_limits": {"limit_id": limit_id, "primary": primary, "secondary": secondary}}})


class Fake:
    """subprocess.run stand-in: records calls, answers per first word."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        rc, so, se = self.answers.get(argv[0], (0, b"", b""))
        return subprocess.CompletedProcess(argv, rc, so, se)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp)
        self.msgs = []


class ClaudeUsageTest(Base):
    def test_record_and_read_claude(self):
        usage.record_claude(self.cfg, "alice", claude_data(five_reset=LATER), NOW)
        path = usage.usage_path(self.cfg, "alice")
        self.assertEqual(path, self.cfg.state_dir / "usage" / "alice.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["five_hour"], {"used": 42, "resets_at": LATER})
        self.assertEqual(saved["week"], {"used": 18, "resets_at": LATER})
        u = usage.claude_usage(self.cfg, "alice")
        with local_tz("Asia/Taipei"):
            self.assertEqual(usage.usage_text(u, NOW), "5h 42%・週 18%")
            # not today any more: month/day too
            self.assertEqual(usage.usage_text(u, NOW + timedelta(days=1)), "5h 42%・週 18%（10/24 12:00）")

    def test_no_rate_limits_writes_nothing(self):
        data = claude_data()
        del data["rate_limits"]
        usage.record_claude(self.cfg, "alice", data, NOW)
        self.assertFalse(usage.usage_path(self.cfg, "alice").exists())
        self.assertIsNone(usage.claude_usage(self.cfg, "alice"))
        self.assertEqual(usage.usage_text(None, NOW), "")

    def test_one_window_absent(self):
        data = claude_data()
        del data["rate_limits"]["seven_day"]
        usage.record_claude(self.cfg, "alice", data, NOW)
        u = usage.claude_usage(self.cfg, "alice")
        self.assertIsNone(u.week)
        with local_tz("Asia/Taipei"):
            self.assertEqual(usage.usage_text(u, NOW), "5h 42%")

    def test_expired_window_dropped(self):
        usage.record_claude(self.cfg, "alice", claude_data(five=91.6, five_reset=PAST), NOW)
        u = usage.claude_usage(self.cfg, "alice")
        with local_tz("Asia/Taipei"):
            self.assertEqual(usage.usage_text(u, NOW), "週 18%")
        usage.record_claude(self.cfg, "alice", claude_data(five_reset=PAST, week_reset=PAST), NOW)
        self.assertEqual(usage.usage_text(usage.claude_usage(self.cfg, "alice"), NOW), "")

    def test_bad_file_reads_none(self):
        path = usage.usage_path(self.cfg, "alice")
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertIsNone(usage.claude_usage(self.cfg, "alice"))


class BadResetTest(unittest.TestCase):
    def test_unrepresentable_resets_at_is_no_window(self):
        for bad in (1e20, float("nan"), SOON * 1000, float("inf")):
            self.assertIsNone(usage._window(42, bad), bad)
        self.assertIsNotNone(usage._window(42, SOON))


class CodexUsageTest(Base):
    def rollout(self, name, lines, mtime=None):
        d = self.cfg.home / ".codex" / "sessions" / "2026" / "10" / "24"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text("\n".join(lines) + "\n")
        if mtime is not None:
            os.utime(f, (mtime, mtime))
        return f

    def profile(self):
        return self.cfg.all_profiles["codex"]

    def test_codex_skips_premium_record(self):
        five = {"used_percent": 27.0, "window_minutes": 300, "resets_at": SOON}
        week = {"used_percent": 36.0, "window_minutes": 10080, "resets_at": LATER}
        self.rollout("rollout-2026-10-24T11-00-00-x.jsonl", [
            json.dumps({"timestamp": "2026-10-24T03:00:00.000Z", "type": "session_meta", "payload": {}}),
            # windows told apart by window_minutes, not by position
            codex_line("2026-10-24T03:48:49.137Z", "codex", week, five),
            codex_line("2026-10-24T03:49:00.000Z", "premium", None, None),
            '{"truncated',
        ])
        u = usage.codex_usage(self.profile())
        self.assertEqual((u.five_hour.used, u.week.used), (27.0, 36.0))
        self.assertEqual(u.at, datetime(2026, 10, 24, 3, 48, 49, 137000, tzinfo=timezone.utc))
        with local_tz("Asia/Taipei"):
            self.assertEqual(usage.usage_text(u, NOW), "5h 27%・週 36%（11:48）")

    def test_codex_newest_rollout_by_mtime(self):
        old = {"used_percent": 5.0, "window_minutes": 300, "resets_at": SOON}
        new = {"used_percent": 60.0, "window_minutes": 300, "resets_at": SOON}
        self.rollout("rollout-b.jsonl", [codex_line("2026-10-24T01:00:00Z", "codex", old, None)], mtime=1000)
        self.rollout("rollout-a.jsonl", [codex_line("2026-10-24T02:00:00Z", "codex", new, None)], mtime=2000)
        u = usage.codex_usage(self.profile())
        self.assertEqual(u.five_hour.used, 60.0)
        self.assertIsNone(u.week)

    def test_codex_bad_resets_at_never_raises(self):
        good = {"used_percent": 27.0, "window_minutes": 300, "resets_at": SOON}
        bad = {"used_percent": 36.0, "window_minutes": 10080, "resets_at": LATER * 1000}  # milliseconds
        self.rollout("rollout-a.jsonl", [codex_line("2026-10-24T02:00:00Z", "codex", good, bad)])
        u = usage.codex_usage(self.profile())
        self.assertEqual((u.five_hour.used, u.week), (27.0, None))
        self.rollout("rollout-a.jsonl", [codex_line("2026-10-24T02:00:00Z", "codex", dict(good, resets_at=1e20),
                                                    dict(bad, resets_at=float("nan")))])
        self.assertIsNone(usage.codex_usage(self.profile()))

    def test_claude_bad_resets_at_never_raises(self):
        data = claude_data(five_reset=1e20, week_reset=SOON * 1000)
        usage.record_claude(self.cfg, "alice", data, NOW)
        self.assertFalse(usage.usage_path(self.cfg, "alice").exists())
        path = usage.usage_path(self.cfg, "alice")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"at": NOW.isoformat(), "five_hour": {"used": 1, "resets_at": 1e20}, "week": None}))
        self.assertIsNone(usage.claude_usage(self.cfg, "alice"))

    def test_codex_no_rollout(self):
        self.assertIsNone(usage.codex_usage(self.profile()))
        self.rollout("rollout-a.jsonl", [codex_line("2026-10-24T02:00:00Z", "premium", None, None)])
        self.assertIsNone(usage.codex_usage(self.profile()))


def win(used, hours):
    return usage.Window(used, NOW + timedelta(hours=hours))


class AlertTextTest(Base):
    def u(self, five=None, week=None, at=NOW, reached=False):
        return usage.Usage(five, week, at, reached)

    def test_usage_text_exhausted(self):
        u = self.u(win(100, 0.5), win(73, 72))
        with local_tz("Asia/Taipei"):
            self.assertEqual(usage.usage_text(u, NOW), "⛔ 5h 已用完（12:30 重置）・週 73%")
        self.assertEqual(usage.usage_alert(u, NOW), "err")

    def test_usage_text_warn_orders_worst_first(self):
        u = self.u(win(40, 2), usage.Window(85, datetime(2026, 10, 27, 1, 10, tzinfo=timezone.utc)))
        with local_tz("Asia/Taipei"):
            self.assertEqual(usage.usage_text(u, NOW), "⚠ 週 85%（10/27 09:10 重置）・5h 40%")
        self.assertEqual(usage.usage_alert(u, NOW), "warn")

    def test_below_threshold_has_no_alert(self):
        u = self.u(win(79, 2), win(18, 72))
        self.assertEqual((usage.usage_text(u, NOW), usage.usage_alert(u, NOW)), ("5h 79%・週 18%", ""))

    def test_reached_without_full_window(self):
        u = self.u(win(98, 0.5), win(60, 72), reached=True)
        with local_tz("Asia/Taipei"):
            self.assertEqual(usage.usage_text(u, NOW), "⛔ 5h 已用完（12:30 重置）・週 60%")
        self.assertEqual(usage.usage_alert(u, NOW), "err")

    def test_reached_without_any_window(self):
        u = self.u(None, None, reached=True)
        self.assertEqual((usage.usage_text(u, NOW), usage.usage_alert(u, NOW)), ("⛔ 已用完", "err"))

    def test_reached_but_all_windows_past_reset_is_not_reached(self):
        u = self.u(win(100, -1), win(50, -2), reached=True)
        self.assertEqual((usage.usage_text(u, NOW), usage.usage_alert(u, NOW)), ("", ""))

    def test_stale_reading_time_appended(self):
        with local_tz("Asia/Taipei"):
            old = self.u(win(40, 2), None, at=NOW - timedelta(minutes=10))
            self.assertEqual(usage.usage_text(old, NOW), "5h 40%（11:50）")
            fresh = self.u(win(40, 2), None, at=NOW - timedelta(minutes=1))
            self.assertEqual(usage.usage_text(fresh, NOW), "5h 40%")

    def test_alert_clears_after_reset(self):
        u = self.u(win(100, -1), win(30, 72))
        self.assertEqual((usage.usage_alert(u, NOW), usage.usage_text(u, NOW)), ("", "週 30%"))
        self.assertEqual((usage.usage_alert(None, NOW), usage.usage_text(None, NOW)), ("", ""))


class CodexSavedTest(Base):
    def profile(self):
        return self.cfg.all_profiles["codex"]

    def rollout(self, ts, used):
        d = self.profile().config_dir / "sessions" / "2026" / "10" / "24"
        d.mkdir(parents=True)
        five = {"used_percent": used, "window_minutes": 300, "resets_at": SOON}
        (d / "rollout-a.jsonl").write_text(codex_line(ts, "codex", five, None) + "\n")

    def saved(self, at, used):
        usage.record_codex(self.cfg, "codex", usage.Usage(win(used, 3), None, at))

    def test_codex_usage_prefers_newer_saved_reading(self):
        self.rollout("2026-10-24T04:07:00Z", 77)
        self.saved(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc), 100)
        self.assertEqual(usage.codex_profile_usage(self.cfg, self.profile()).five_hour.used, 100)
        self.saved(datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc), 50)
        self.assertEqual(usage.codex_profile_usage(self.cfg, self.profile()).five_hour.used, 77)

    def test_codex_usage_either_missing(self):
        self.assertIsNone(usage.codex_profile_usage(self.cfg, self.profile()))
        self.saved(NOW, 10)
        self.assertEqual(usage.codex_profile_usage(self.cfg, self.profile()).five_hour.used, 10)

    def test_record_codex_is_private_and_atomic(self):
        usage.record_codex(self.cfg, "codex", usage.Usage(win(100, 3), win(20, 72), NOW, reached=True))
        path = usage.usage_path(self.cfg, "codex")
        self.assertEqual((path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777), (0o600, 0o700))
        self.assertEqual([f.name for f in path.parent.iterdir()], ["codex.json"])
        u = usage.saved_usage(self.cfg, "codex")
        self.assertEqual((u.five_hour.used, u.week.used, u.reached, u.at), (100, 20, True, NOW))
        self.assertIs(usage.claude_usage, usage.saved_usage)

    def test_saved_reached_only_reading(self):
        usage.record_codex(self.cfg, "codex", usage.Usage(None, None, NOW, reached=True))
        self.assertTrue(usage.saved_usage(self.cfg, "codex").reached)


class StatuslineTest(Base):
    def setUp(self):
        super().setUp()
        self.so, self.se = io.BytesIO(), io.BytesIO()

    def sl(self, *a, **kw):
        return usage.cmd_statusline(*a, stdout=self.so, stderr=self.se, **kw)

    def env(self):
        return {}  # no CLAUDE_CONFIG_DIR: the default profile

    def test_wrapped_command_output_and_exit_code_pass_through(self):
        run = Fake({"npx": (3, b"X\n", b"")})
        stdin = json.dumps(claude_data()).encode()
        rc = self.sl(self.cfg, self.msgs.append, ["npx", "-y", "ccstatusline@latest"], stdin=stdin,
                                  env=self.env(), run_wrapped=run, now=NOW)
        self.assertEqual(rc, 3)
        self.assertEqual((self.msgs, self.so.getvalue()), ([], b"X\n"))
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["npx", "-y", "ccstatusline@latest"])
        self.assertEqual(kw["input"], stdin)
        self.assertTrue(usage.usage_path(self.cfg, "alice").exists())

    def test_wrapped_output_is_byte_exact_and_stderr_forwarded(self):
        raw = b"\x1b[31m\xff\xfe bad utf-8, no newline"
        run = Fake({"npx": (0, raw, b"warn\xff\n")})
        self.sl(self.cfg, self.msgs.append, ["npx"], stdin=b"{}", env=self.env(), run_wrapped=run, now=NOW)
        self.assertEqual(self.so.getvalue(), raw)
        self.assertEqual(self.se.getvalue(), b"warn\xff\n")
        self.assertEqual(self.msgs, [])

    def test_bad_stdin_still_runs_wrapped_command(self):
        run = Fake({"npx": (0, b"Y\n", b"")})
        rc = self.sl(self.cfg, self.msgs.append, ["npx"], stdin=b"not json", env=self.env(),
                                  run_wrapped=run, now=NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(self.so.getvalue(), b"Y\n")
        self.assertEqual(run.calls[0][1]["input"], b"not json")
        self.assertFalse((self.cfg.state_dir / "usage").exists())

    def test_profile_from_config_dir_and_unknown_dir(self):
        stdin = json.dumps(claude_data()).encode()
        env = {"CLAUDE_CONFIG_DIR": str(self.cfg.home / ".claude-profiles" / "work")}
        self.sl(self.cfg, self.msgs.append, ["npx"], stdin=stdin, env=env, run_wrapped=Fake({}), now=NOW)
        self.assertTrue(usage.usage_path(self.cfg, "work").exists())
        self.assertFalse(usage.usage_path(self.cfg, "alice").exists())
        self.sl(self.cfg, self.msgs.append, ["npx"], stdin=stdin, env={"CLAUDE_CONFIG_DIR": "/elsewhere"},
                             run_wrapped=Fake({}), now=NOW)
        self.assertFalse(usage.usage_path(self.cfg, "alice").exists())

    def test_no_config_still_runs_wrapped(self):
        run = Fake({"npx": (0, b"Z", b"")})
        rc = self.sl(None, self.msgs.append, ["npx"], stdin=b"{}", env={}, run_wrapped=run, now=NOW)
        self.assertEqual((rc, self.so.getvalue()), (0, b"Z"))

    def test_missing_wrapped_command(self):
        def run(argv, **kw):
            raise FileNotFoundError(argv[0])
        with mock.patch.object(usage, "_err") as err:
            rc = self.sl(self.cfg, self.msgs.append, ["nope"], stdin=b"{}", env={}, run_wrapped=run,
                                      now=NOW)
        self.assertEqual(rc, 127)
        self.assertEqual(self.msgs, [])  # the error goes to stderr, never into the status line
        self.assertIn("nope", err.call_args[0][0])

    def test_no_wrapped_prints_own_line(self):
        run = Fake({"git": (0, b"main\n", b"")})
        stdin = json.dumps(claude_data()).encode()
        with local_tz("Asia/Taipei"):
            rc = self.sl(self.cfg, self.msgs.append, [], stdin=stdin, env=self.env(), run_wrapped=run,
                                      now=NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(self.msgs, ["Opus · /w/proj（main）· context 37% · 5h 42%・週 18%"])
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["git", "-C", "/w/proj", "branch", "--show-current"])
        self.assertEqual(kw["timeout"], 1)

    def test_own_line_without_branch_context_or_usage(self):
        run = Fake({"git": (128, b"", b"fatal")})
        data = {"model": {"display_name": "Opus"}, "cwd": str(self.cfg.home / "x"),
                "context_window": {"used_percentage": None}}
        self.sl(self.cfg, self.msgs.append, [], stdin=json.dumps(data).encode(), env=self.env(),
                             run_wrapped=run, now=NOW)
        self.assertEqual(self.msgs, ["Opus · ~/x"])

    def test_own_line_never_raises_on_garbage(self):
        rc = self.sl(self.cfg, self.msgs.append, [], stdin=b"\xff\xfe", env=self.env(),
                                  run_wrapped=Fake({}), now=NOW)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
