"""actions.log: what plugin / restart / forget / switch actions printed, kept so an earlier action's messages can be read back."""
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

from sampan import actionlog

STAMP = "2026-10-24T12:00:00+08:00"


class ActionLogTest(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)
        self.cfg = NS(state_dir=Path(tempfile.mkdtemp()) / "state")
        self.shown = []

    def tee(self, label, out=None):
        return actionlog.tee(self.cfg, label, out or self.shown.append, now=lambda: STAMP)

    def lines(self):
        return actionlog.log_path(self.cfg).read_text().splitlines()

    def test_tee_shows_and_logs_each_line_with_time_and_label(self):
        out = self.tee("plugin 存檔 x@m")
        out("✔ alice：已安裝 x")
        out("a\nb")
        out()
        self.assertEqual(self.shown, ["✔ alice：已安裝 x", "a\nb", ""])
        self.assertEqual(self.lines(), [f"{STAMP} [plugin 存檔 x@m] ✔ alice：已安裝 x", f"{STAMP} [plugin 存檔 x@m] a",
                                        f"{STAMP} [plugin 存檔 x@m] b"])

    def test_log_is_private(self):
        self.tee("重開")("x")
        self.assertEqual(stat.S_IMODE(os.stat(actionlog.log_path(self.cfg)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg.state_dir).st_mode), 0o700)
        os.chmod(actionlog.log_path(self.cfg), 0o644)
        self.tee("重開")("y")
        self.assertEqual(stat.S_IMODE(os.stat(actionlog.log_path(self.cfg)).st_mode), 0o600)

    def test_tokens_are_redacted_in_the_log_not_on_screen(self):
        self.tee("來源 m")("❌ alice：fatal: https://alice:s3cret@git.example.com/a.git not found")
        self.assertIn("s3cret", self.shown[0])
        self.assertNotIn("s3cret", "\n".join(self.lines()))
        self.assertIn("https://***@git.example.com/a.git", self.lines()[0])

    def test_other_secret_forms_and_the_label_are_masked(self):
        self.tee("來源 https://alice:s3cret@h/x")("GH_TOKEN=abc123 ghp_" + "a" * 36 + " Authorization: token zzz9")
        line = self.lines()[0]
        for secret in ("s3cret", "abc123", "a" * 36, "zzz9"):
            self.assertNotIn(secret, line)
        self.assertIn("[來源 https://***@h/x] GH_TOKEN=*** *** Authorization: token ***", line)

    def test_nested_tee_logs_once_under_the_outer_label(self):
        outer = self.tee("重開")
        inner = self.tee("forget a", outer)
        self.assertIs(inner, outer)
        inner("✔ 已從登記表移除 a")
        self.assertEqual(self.lines(), [f"{STAMP} [重開] ✔ 已從登記表移除 a"])
        self.assertEqual(self.shown, ["✔ 已從登記表移除 a"])

    def test_unwritable_log_never_breaks_the_action(self):
        self.cfg.state_dir.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_dir.write_text("not a dir")
        self.tee("重開")("still shown")
        self.assertEqual(self.shown, ["still shown"])

    def test_labels_of_the_hooked_actions(self):
        from sampan import plugin_apply as pa
        cfg = NS(state_dir=self.cfg.state_dir, profiles={}, path=None)
        snap = NS(states={}, missing=set())
        with unittest.mock.patch.object(pa, "snapshot", return_value=snap), \
                unittest.mock.patch.object(pa, "plan_update", return_value={}):
            pa.update_plugins(cfg, None, self.shown.append)
        pa.update_markets(cfg, None, self.shown.append)  # no profiles: prints nothing
        pa.check_latest(cfg, None, self.shown.append, snap)
        pa.restart_profiles(cfg, None, self.shown.append, None, ["p"], stop_fn=lambda *a, **k: k["left"].append("b") or 0,
                            restore_fn=None)
        labels = [line.split("] ")[0].split(" [", 1)[1] for line in self.lines()]
        self.assertEqual(labels, ["plugin 更新", "重開"])

    def test_prompt_answers_are_logged(self):
        from sampan.prompts import ATTACH, SKIP
        p = NS(question="Do you trust the files in this folder?", options=("Yes, I trust this folder", "No, exit"))
        answers = iter([0, ATTACH, SKIP, None])
        ask = actionlog.logged_prompt(self.cfg, "重開", lambda name, agent, prompt: next(answers), now=lambda: STAMP)
        self.assertEqual([ask("a", "claude", p) for _ in range(4)], [0, ATTACH, SKIP, None])
        self.assertEqual(self.lines(), [
            f"{STAMP} [重開] a（claude）提示「Do you trust the files in this folder?」→ 1. Yes, I trust this folder",
            f"{STAMP} [重開] a（claude）提示「Do you trust the files in this folder?」→ 進去自己操作",
            f"{STAMP} [重開] a（claude）提示「Do you trust the files in this folder?」→ 先不管，之後再處理",
            f"{STAMP} [重開] a（claude）提示「Do you trust the files in this folder?」→ None"])
        self.assertIsNone(actionlog.logged_prompt(self.cfg, "重開", None))

    def test_restart_logs_prompt_answers(self):
        from sampan import plugin_apply as pa
        seen = {}

        def restore_fn(cfg, run, out, ask, **kw):
            seen["answer"] = kw["on_prompt"]("s1", "claude", NS(question="Q?", options=("A",)))
            return 0
        pa.restart_profiles(self.cfg, None, self.shown.append, None, ["p"], on_prompt=lambda *a: 0,
                            stop_fn=lambda *a, **k: k["stopped"].append("s1") or 0, restore_fn=restore_fn)
        self.assertEqual(seen["answer"], 0)
        self.assertTrue(self.lines()[-1].endswith("[重開] s1（claude）提示「Q?」→ 1. A"))

    def test_doctor_names_the_log(self):
        from sampan.doctor import _action_log
        c = _action_log(self.cfg)[0]
        self.assertEqual((c.level, c.title), ("info", f"操作紀錄：{actionlog.log_path(self.cfg)}（還沒有紀錄）"))
        self.tee("重開")("x")
        c = _action_log(self.cfg)[0]
        from sampan.clock import fmt_local, parse_iso
        self.assertEqual(c.title, f"操作紀錄：{actionlog.log_path(self.cfg)}（最後一筆 {fmt_local(parse_iso(STAMP))}）")

    def test_rotates_when_large(self):
        out = self.tee("重開")
        out("first")
        path = actionlog.log_path(self.cfg)
        with open(path, "a") as f:
            f.write("x" * actionlog.MAX_BYTES)
        out("second")
        self.assertEqual(self.lines(), [f"{STAMP} [重開] second"])
        old = path.with_name(path.name + ".1")
        self.assertTrue(old.read_text().startswith(f"{STAMP} [重開] first"))
        self.assertEqual(stat.S_IMODE(os.stat(old).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
