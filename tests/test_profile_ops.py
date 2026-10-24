import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan import profile_ops
from sampan.config import load_config
from sampan.config_edit import set_profile_flag
from sampan.registry import Row, load_rows, write_rows
from sampan.status import State
from sampan.tmux import TmuxUnreadable
from tests.fakes import FakeRunner, cp, pane_line, ps_line
from tests.helpers import TOML, make_env, status_row
from tests.test_claude_state import write_session

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
NO_PROC = Path("/nonexistent-proc-root")


class Base(unittest.TestCase):
    toml = TOML

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_env(self.tmp, self.toml)
        self.msgs = []

    @property
    def out(self):
        return "\n".join(self.msgs)

    def reload(self):
        return load_config(self.cfg.path, home=self.cfg.home, state_dir=self.cfg.state_dir)

    def backups(self):
        return list(self.cfg.path.parent.glob("config.toml.bak-*"))

    def write_registry(self, rows):
        write_rows(self.cfg.registry_path, rows, "2026-10-24T00:00:00+00:00", self.cfg.history_dir)

    def rename(self, old, new, running=()):
        with mock.patch.object(profile_ops, "running_sessions", return_value=list(running)):
            return profile_ops.cmd_profile_rename(self.cfg, FakeRunner(strict=True), self.msgs.append, old, new)


class RenameTest(Base):
    def test_rename_updates_config_and_registry(self):
        self.write_registry([Row("a", "claude", "alice", A, "/c", "active"), Row("b", "claude", "alice", B, "/c", "active"),
                             Row("w", "claude", "work", B, "/c", "active")])
        rc = self.rename("alice", "amy")
        self.assertEqual(rc, 0)
        cfg = self.reload()
        self.assertIn("amy", cfg.profiles)
        self.assertTrue(cfg.profiles["amy"].default)
        self.assertNotIn("alice", cfg.all_profiles)
        self.assertEqual({r.profile for r in load_rows(self.cfg.registry_path)}, {"amy", "work"})
        self.assertIn("✔ 已把 profile alice 改名為 amy（登記表 2 列一起改）", self.out)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_text(), TOML)
        # every profile action is logged
        self.assertIn("[profile rename alice] ✔ 已把 profile alice 改名為 amy", (self.cfg.state_dir / "actions.log").read_text())

    def test_rename_refused_while_running(self):
        before = self.cfg.path.read_bytes()
        rc = self.rename("alice", "amy", running=["a"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertEqual(self.msgs, ["❌ profile alice 還有 session 在跑：a；先停掉再改名"])
        self.assertEqual(self.backups(), [])

    def test_rename_undone_when_registry_write_fails(self):
        self.write_registry([Row("a", "claude", "alice", A, "/c", "active")])
        before = self.cfg.path.read_bytes()
        reg_before = self.cfg.registry_path.read_bytes()
        self.cfg.path.chmod(0o640)
        with mock.patch.object(profile_ops.registry, "write_rows", side_effect=OSError("disk full")):
            rc = self.rename("alice", "amy")
        self.assertEqual(rc, 1)
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertEqual(self.cfg.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.cfg.registry_path.read_bytes(), reg_before)
        self.assertIn("❌ 登記表寫入失敗（disk full），設定檔已還原，什麼都沒改", self.msgs)
        self.assertNotIn("✔", self.out)

    def test_rename_refused_when_tmux_unreadable(self):
        before = self.cfg.path.read_bytes()
        with mock.patch.object(profile_ops, "running_sessions", side_effect=TmuxUnreadable("讀不到 tmux")):
            rc = profile_ops.cmd_profile_rename(self.cfg, FakeRunner(strict=True), self.msgs.append, "alice", "amy")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertIn("讀不到 tmux", self.out)

    def test_rename_rejects_bad_names(self):
        before = self.cfg.path.read_bytes()
        self.assertEqual(self.rename("alice", "a b"), 1)
        self.assertEqual(self.rename("alice", "work"), 1)
        self.assertEqual(self.rename("nobody", "amy"), 1)
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertIn("名稱只能用英數字、- 和 _", self.out)
        self.assertIn("已經有 profile work", self.out)
        self.assertIn("沒有 profile nobody", self.out)

    def test_rename_disabled_profile(self):
        self.cfg.path.write_text(set_profile_flag(TOML, "work", "disabled", True))
        self.cfg = self.reload()
        self.assertEqual(self.rename("work", "job"), 0)
        self.assertTrue(self.reload().all_profiles["job"].disabled)

    def test_rename_codex_profile_updates_old_rows(self):
        # Review Focus 2: an old Codex row has profile "" and the Codex profile is implicit
        self.cfg.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.registry_path.write_text(json.dumps({"version": 2, "saved_at": "x", "sessions": [
            {"tmux": "cx1", "agent": "codex", "profile": "", "session_id": A, "cwd": "/c", "state": "active"}]}))
        rc = self.rename("codex", "cx")
        self.assertEqual(rc, 0)
        text = self.cfg.path.read_text()
        self.assertIn('[profiles.cx]\nagent = "codex"', text)
        self.assertNotIn("[codex]", text)
        cfg = self.reload()
        self.assertFalse(cfg.implicit_codex)
        self.assertEqual(cfg.codex.profile, "cx")
        raw = json.loads(self.cfg.registry_path.read_text())["sessions"][0]
        self.assertEqual(raw["profile"], "cx")
        self.assertIn("✔ 已把 profile codex 改名為 cx（登記表 1 列一起改）", self.out)


class ConvertTest(Base):
    toml = TOML + '\n[codex]\nhome = "~/cxh"\n'

    def test_first_write_converts_codex_table(self):
        rc = self.rename("alice", "amy")
        self.assertEqual(rc, 0)
        text = self.cfg.path.read_text()
        self.assertIn('[profiles.codex]\nagent = "codex"\nconfig_dir = "~/cxh"\n', text)
        self.assertNotIn("[codex]", text)
        self.assertIn("已把 [codex] 轉成 [profiles.codex]（目錄 ~/cxh）", self.out)
        self.assertEqual(self.reload().codex.home, self.cfg.home / "cxh")
        self.assertEqual(len(self.backups()), 1)

    def test_conversion_uses_the_implicit_profile_name(self):
        # a Claude profile already called codex: the implicit Codex profile is codex-1
        self.cfg.path.write_text(self.toml + '\n[profiles.codex]\nconfig_dir = "~/.claude-profiles/work"\n')
        self.cfg = self.reload()
        self.assertEqual(self.cfg.codex.profile, "codex-1")
        self.assertEqual(self.rename("alice", "amy"), 0)
        text = self.cfg.path.read_text()
        self.assertIn('[profiles.codex-1]\nagent = "codex"\nconfig_dir = "~/cxh"\n', text)
        self.assertIn("已把 [codex] 轉成 [profiles.codex-1]（目錄 ~/cxh）", self.out)

    def test_codex_off_is_left_alone(self):
        # Ruling T1-a: enabled = false means no Codex profile, nothing to convert
        toml = TOML + "\n[codex]\nenabled = false\n"
        self.cfg.path.write_text(toml)
        self.cfg = self.reload()
        self.assertEqual(self.rename("alice", "amy"), 0)
        text = self.cfg.path.read_text()
        self.assertIn("[codex]\nenabled = false\n", text)
        self.assertNotIn('agent = "codex"', text)
        self.assertNotIn("轉成", self.out)


class EditConfigTest(Base):
    def test_failed_edit_writes_nothing(self):
        before = self.cfg.path.read_bytes()
        self.assertIsNone(profile_ops.edit_config(self.cfg, self.msgs.append, lambda t: None))
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertIn("設定檔沒改", self.out)
        self.assertEqual(self.backups(), [])

    def test_invalid_result_writes_nothing(self):
        before = self.cfg.path.read_bytes()
        bad = lambda t: t.replace("default = true", "default = false")  # noqa: E731 - no default profile left
        self.assertIsNone(profile_ops.edit_config(self.cfg, self.msgs.append, bad))
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertIn("❌", self.out)
        self.assertIn("設定檔沒改", self.out)
        self.assertEqual(self.backups(), [])
        self.assertEqual([p.name for p in self.cfg.path.parent.iterdir() if p.name.startswith(".")], [])

    def test_rereads_disk_and_returns_new_config(self):
        self.cfg.path.write_text(TOML.replace('label = "work@example.com"', 'label = "hand edit"'))
        new = profile_ops.edit_config(self.cfg, self.msgs.append, lambda t: t + "\n# x\n")
        self.assertEqual(new.all_profiles["work"].label, "hand edit")
        self.assertIn("hand edit", self.cfg.path.read_text())

    def test_backup_keeps_mode(self):
        self.cfg.path.chmod(0o640)
        profile_ops.edit_config(self.cfg, self.msgs.append, lambda t: t + "\n# x\n")
        self.assertEqual(self.backups()[0].stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.cfg.path.stat().st_mode & 0o777, 0o640)


class RunningSessionsTest(Base):
    def runner(self, panes, procs):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-sessions"], lambda a: cp(out="srv\n") if "-F" in a else cp())
        r.on(["tmux", "-u", "list-panes"], cp(out="\n".join(panes) + "\n"))
        r.on(["ps", "-A"], cp(out="\n".join(procs) + "\n"))
        r.on(["ps", "eww"], cp())
        return r

    def test_finds_sessions_of_a_disabled_profile(self):
        write_session(self.cfg, "alice", 200, sid=A, tmux="a:@1.%1")
        write_session(self.cfg, "work", 201, sid=B, tmux="b:@2.%2")
        self.cfg.path.write_text(set_profile_flag(TOML, "work", "disabled", True))
        self.cfg = self.reload()
        r = self.runner([pane_line("a", "%1", 100, "claude", "/c"), pane_line("b", "%2", 101, "claude", "/c")],
                        [ps_line(100, 1, "-zsh", "-zsh"), ps_line(101, 1, "-zsh", "-zsh"),
                         ps_line(200, 100, "claude", "claude"), ps_line(201, 101, "claude", "claude")])
        self.assertEqual(profile_ops.running_sessions(self.cfg, r, "work", proc_root=NO_PROC), ["b"])
        self.assertEqual(profile_ops.running_sessions(self.cfg, r, "alice", proc_root=NO_PROC), ["a"])

    def test_unreadable_tmux_raises(self):
        r = FakeRunner(strict=True)
        r.on(["tmux", "-u", "list-sessions"], cp(rc=1, err="permission denied"))
        r.on(["tmux", "-u", "list-panes"], cp(rc=1, err="permission denied"))
        with self.assertRaises(TmuxUnreadable):
            profile_ops.running_sessions(self.cfg, r, "alice", proc_root=NO_PROC)


class ListTest(Base):
    def runner(self):
        r = FakeRunner(strict=True)
        r.on(["claude", "auth", "status"], lambda a: cp(out=json.dumps({"email": "a@example.com"})))
        r.on(["codex", "login", "status"], cp(out="\nLogged in using ChatGPT\nmore\n"))
        return r

    def test_list_lines(self):
        rows = [status_row("a", profile="alice"), status_row("b", profile="alice"),
                status_row("c", State.STOPPED, profile="work"), status_row("x", agent="codex", profile="codex")]
        r = self.runner()
        with mock.patch.object(profile_ops, "status_rows", return_value=rows):
            rc = profile_ops.cmd_profile_list(self.cfg, r, self.msgs.append)
        self.assertEqual(rc, 0)
        self.assertEqual(self.msgs, [
            "alice  claude  ~/.claude  a@example.com  使用中（預設）  2 個執行中  plugin 2",
            "work  claude  ~/.claude-profiles/work  a@example.com  使用中  0 個執行中  plugin 1",
            "codex  codex  ~/.codex  Logged in using ChatGPT  使用中  1 個執行中",
        ])
        envs = {c["env"].get("CLAUDE_CONFIG_DIR") for c in r.called(["claude", "auth", "status"])}
        self.assertEqual(envs, {None, str(self.cfg.home / ".claude-profiles" / "work")})
        self.assertEqual(r.called(["codex", "login", "status"])[0]["env"]["CODEX_HOME"], str(self.cfg.home / ".codex"))

    def test_list_shows_usage(self):
        from datetime import datetime, timedelta, timezone

        from sampan import usage
        from tests.helpers import local_tz
        now = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)
        later = int((now + timedelta(days=2)).timestamp())
        usage.record_claude(self.cfg, "alice", {"rate_limits": {
            "five_hour": {"used_percentage": 42, "resets_at": later},
            "seven_day": {"used_percentage": 18, "resets_at": later}}}, now - timedelta(days=1))
        d = self.cfg.home / ".codex" / "sessions" / "2026" / "10" / "24"
        d.mkdir(parents=True)
        (d / "rollout-x.jsonl").write_text(json.dumps({"timestamp": "2026-10-24T03:48:49Z", "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {"limit_id": "codex",
                "primary": {"used_percent": 27.0, "window_minutes": 300, "resets_at": later},
                "secondary": None}}}) + "\n")
        with mock.patch.object(profile_ops, "status_rows", return_value=[]), local_tz("Asia/Taipei"):
            profile_ops.cmd_profile_list(self.cfg, self.runner(), self.msgs.append, now=now)
        self.assertEqual(self.msgs, [
            "alice  claude  ~/.claude  a@example.com  使用中（預設）  0 個執行中  plugin 2  5h 42%・週 18%（10/23 12:00）",
            "work  claude  ~/.claude-profiles/work  a@example.com  使用中  0 個執行中  plugin 1",
            "codex  codex  ~/.codex  Logged in using ChatGPT  使用中  0 個執行中  5h 27%（11:48）",
        ])

    def test_unreadable_account_and_disabled(self):
        self.cfg.path.write_text(set_profile_flag(TOML, "work", "disabled", True))
        self.cfg = self.reload()
        r = FakeRunner(strict=True)
        r.on(["claude", "auth", "status"], cp(rc=1))
        r.on(["codex", "login", "status"], cp(rc=127, err=""))
        prows = profile_ops.profile_rows(self.cfg, r, [])
        by = {p.name: p for p in prows}
        self.assertEqual(by["alice"].account, "（讀不到）")
        self.assertEqual(by["codex"].account, "（讀不到）")
        self.assertTrue(by["work"].disabled)
        self.assertIsNone(by["codex"].plugins)
        with mock.patch.object(profile_ops, "status_rows", return_value=[]):
            profile_ops.cmd_profile_list(self.cfg, r, self.msgs.append)
        self.assertIn("work  claude  ~/.claude-profiles/work  （讀不到）  停用  0 個執行中  plugin 1", self.msgs)

    def test_codex_account_from_stderr(self):
        r = FakeRunner(strict=True)
        r.on(["claude", "auth", "status"], cp(rc=1))
        r.on(["codex", "login", "status"], cp(err="Logged in using an API key\n"))
        by = {p.name: p for p in profile_ops.profile_rows(self.cfg, r, [])}
        self.assertEqual(by["codex"].account, "Logged in using an API key")


CODEX_SECTION = '\n[profiles.cx]\nagent = "codex"\nconfig_dir = "~/.codex"\n'


class DisableTest(Base):
    def call(self, fn, name, running=()):
        with mock.patch.object(profile_ops, "running_sessions", return_value=list(running)):
            return fn(self.cfg, FakeRunner(strict=True), self.msgs.append, name)

    def test_disable_default_refused(self):
        before = self.cfg.path.read_bytes()
        rc = self.call(profile_ops.cmd_profile_disable, "alice")
        self.assertEqual(rc, 1)
        self.assertEqual(self.msgs, ["❌ alice 是預設 profile，不能停用；先把別的 profile 設成 default"])
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_disable_then_enable_round_trip(self):
        toml = TOML + CODEX_SECTION  # no implicit Codex profile, so nothing is converted on the first write
        self.cfg.path.write_text(toml)
        self.cfg = self.reload()
        self.assertEqual(self.call(profile_ops.cmd_profile_disable, "work"), 0)
        self.assertIn("✔ 已停用 work；目錄 ~/.claude-profiles/work 保留（要刪的話先確認裡面沒有連到別處的捷徑）", self.msgs)
        self.cfg = self.reload()
        self.assertTrue(self.cfg.all_profiles["work"].disabled)
        self.assertNotIn("work", self.cfg.profiles)
        self.assertEqual(self.call(profile_ops.cmd_profile_enable, "work"), 0)
        self.assertIn("✔ 已恢復 work", self.msgs)
        self.assertEqual(self.cfg.path.read_text(), toml)
        self.assertEqual(len(self.backups()), 2)
        log = (self.cfg.state_dir / "actions.log").read_text()
        self.assertIn("[profile disable work] ✔ 已停用 work", log)
        self.assertIn("[profile enable work] ✔ 已恢復 work", log)

    def test_disable_codex_profile_turns_codex_off(self):
        self.assertEqual(self.call(profile_ops.cmd_profile_disable, "codex"), 0)  # the implicit one: converted first
        cfg = self.reload()
        self.assertTrue(cfg.all_profiles["codex"].disabled)
        self.assertFalse(cfg.codex.enabled)
        self.assertIn("已把 [codex] 轉成 [profiles.codex]（目錄 ~/.codex）", self.msgs)

    def test_disable_refused_while_running(self):
        before = self.cfg.path.read_bytes()
        rc = self.call(profile_ops.cmd_profile_disable, "work", running=["b", "c"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.msgs, ["❌ profile work 還有 session 在跑：b、c；先停掉再停用"])
        self.assertEqual(self.cfg.path.read_bytes(), before)
        with mock.patch.object(profile_ops, "running_sessions", side_effect=TmuxUnreadable("讀不到 tmux")):
            rc = profile_ops.cmd_profile_disable(self.cfg, FakeRunner(strict=True), self.msgs.append, "work")
        self.assertEqual(rc, 2)
        self.assertEqual(self.cfg.path.read_bytes(), before)

    def test_enable_and_disable_unknown_or_unchanged(self):
        before = self.cfg.path.read_bytes()
        self.assertEqual(self.call(profile_ops.cmd_profile_disable, "nobody"), 1)
        self.assertEqual(self.call(profile_ops.cmd_profile_enable, "work"), 0)
        self.assertIn("❌ 沒有 profile nobody", self.msgs)
        self.assertIn("profile work 沒有停用", self.msgs)
        self.assertEqual(self.cfg.path.read_bytes(), before)


class ForgetTest(Base):
    toml = set_profile_flag(TOML, "work", "disabled", True) + CODEX_SECTION

    def forget(self, name, answer=None, running=()):
        asked = []

        def ask(q):
            asked.append(q)
            return name if answer is None else answer

        with mock.patch.object(profile_ops, "running_sessions", return_value=list(running)):
            rc = profile_ops.cmd_profile_forget(self.cfg, FakeRunner(strict=True), self.msgs.append, ask, name)
        return rc, asked

    def test_forget_requires_disabled_and_confirmation(self):
        before = self.cfg.path.read_bytes()
        rc, asked = self.forget("alice")
        self.assertEqual(rc, 1)
        self.assertEqual(asked, [])
        self.assertEqual(self.msgs, ["❌ 先 sampan profile disable alice"])
        self.msgs.clear()
        rc, asked = self.forget("work", answer="wor")
        self.assertEqual(asked, ["輸入 work 確認："])
        self.assertEqual(self.msgs, ["已取消"])
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertEqual(self.backups(), [])
        rc, _ = self.forget("nobody")
        self.assertEqual(rc, 1)
        self.assertIn("❌ 沒有 profile nobody", self.msgs)

    def test_forget_removes_rows_and_prints_resume_lines(self):
        self.write_registry([Row("a", "claude", "alice", A, "/c", "active"),
                             Row("w1", "claude", "work", A, "/c d", "active"),
                             Row("w2", "claude", "work", "", "/c", "retired"),
                             Row("cx", "codex", "cx", B, "/c", "active")])
        rc, _ = self.forget("work")
        self.assertEqual(rc, 0)
        cfg = self.reload()
        self.assertNotIn("work", cfg.all_profiles)
        self.assertNotIn("[profiles.work]", self.cfg.path.read_text())
        self.assertTrue((self.cfg.home / ".claude-profiles" / "work").is_dir())  # the directory stays
        self.assertEqual([r.tmux for r in load_rows(self.cfg.registry_path)], ["a", "cx"])
        work = self.cfg.home / ".claude-profiles" / "work"
        self.assertEqual(self.msgs, [
            "✔ 已從設定檔拿掉 profile work（目錄 ~/.claude-profiles/work 保留；登記表 2 列一起移除）",
            f"  對話還在：cd '/c d' && env CLAUDE_CONFIG_DIR={work} claude --resume {A}",
            "  對話 id ",
        ])
        self.assertIn("[profile forget work] ✔", (self.cfg.state_dir / "actions.log").read_text())

    def test_forget_refused_while_running(self):
        before = self.cfg.path.read_bytes()
        rc, _ = self.forget("work", running=["w1"])
        self.assertEqual(rc, 1)
        self.assertIn("❌ profile work 還有 session 在跑：w1；先停掉再 forget", self.msgs)
        self.assertEqual(self.cfg.path.read_bytes(), before)

    def test_forget_undone_when_registry_write_fails(self):
        self.write_registry([Row("w1", "claude", "work", A, "/c", "active")])
        before = self.cfg.path.read_bytes()
        with mock.patch.object(profile_ops.registry, "write_rows", side_effect=OSError("disk full")):
            rc, _ = self.forget("work")
        self.assertEqual(rc, 1)
        self.assertEqual(self.cfg.path.read_bytes(), before)
        self.assertIn("❌ 登記表寫入失敗（disk full），設定檔已還原，什麼都沒改", self.msgs)

    def test_forget_last_codex_writes_codex_off(self):
        self.cfg.path.write_text(set_profile_flag(self.toml, "cx", "disabled", True))
        self.cfg = self.reload()
        self.cfg.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.registry_path.write_text(json.dumps({"version": 2, "saved_at": "x", "sessions": [
            {"tmux": "old", "agent": "codex", "profile": "", "session_id": A, "cwd": "/c", "state": "active"},
            {"tmux": "new", "agent": "codex", "profile": "cx", "session_id": B, "cwd": "/c", "state": "active"},
            {"tmux": "a", "agent": "claude", "profile": "alice", "session_id": A, "cwd": "/c", "state": "active"}]}))
        rc, _ = self.forget("cx")
        self.assertEqual(rc, 0)
        self.assertTrue(self.cfg.path.read_text().endswith("[codex]\nenabled = false\n"))
        cfg = self.reload()
        self.assertFalse(cfg.codex.enabled)
        self.assertFalse(any(p.agent == "codex" for p in cfg.all_profiles.values()))
        self.assertEqual([r.tmux for r in load_rows(self.cfg.registry_path)], ["a"])
        self.assertIn(f"  對話還在：cd /c && codex resume {A} -C /c", self.msgs)
        self.assertIn(f"  對話還在：cd /c && codex resume {B} -C /c", self.msgs)


if __name__ == "__main__":
    unittest.main()
