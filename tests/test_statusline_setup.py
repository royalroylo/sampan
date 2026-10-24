import json
import shlex
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sampan import statusline_setup as ss
from sampan.doctor import run_checks
from tests.fakes import FakeRunner, cp
from tests.helpers import make_env
from tests.test_plugins import GOOD_KNOWN, write_plugins

SAMPAN = "/Users/u/.local/bin/sampan"
NOW = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.alice = self.cfg.profiles["alice"]
        self.work = self.cfg.profiles["work"]
        self.lines = []

    def settings(self, p):
        return Path(p.config_dir) / "settings.json"

    def put(self, p, data):
        self.settings(p).write_text(data if isinstance(data, str) else json.dumps(data))

    def get(self, p):
        return json.loads(self.settings(p).read_text())

    def install(self, profiles=None, answer="y", **kw):
        return ss.install(self.cfg, self.lines.append, lambda _p: answer, profiles or [self.alice], SAMPAN, **kw)

    def test_install_wraps_existing_command(self):
        orig = "jq -r '.model' | tr a-z A-Z; echo \"$(date)\""
        self.put(self.alice, {"theme": "dark", "statusLine": {"type": "command", "command": orig}})
        self.assertEqual(ss.planned(self.cfg, self.alice, SAMPAN)[0], orig)
        self.assertEqual(self.install(), 0)
        cmd = self.get(self.alice)["statusLine"]["command"]
        self.assertEqual(cmd, f"{SAMPAN} statusline -- sh -c {shlex.quote(orig)}")
        self.assertEqual(shlex.split(cmd)[-1], orig)
        self.assertEqual(self.get(self.alice)["theme"], "dark")
        self.assertEqual(len(list(self.settings(self.alice).parent.glob("settings.json.bak-*"))), 1)
        self.assertTrue(any("→" in ln for ln in self.lines))

    def test_install_without_statusline_and_missing_file(self):
        self.assertEqual(ss.planned(self.cfg, self.work, SAMPAN), (None, f"{SAMPAN} statusline"))
        self.assertEqual(self.install([self.work]), 0)
        self.assertEqual(self.get(self.work), {"statusLine": {"type": "command", "command": f"{SAMPAN} statusline"}})
        self.assertEqual(list(self.settings(self.work).parent.glob("settings.json.bak-*")), [])
        self.assertEqual(ss.uninstall(self.cfg, self.lines.append, lambda _p: "y", [self.work]), 0)
        self.assertNotIn("statusLine", self.get(self.work))

    def test_install_is_idempotent(self):
        self.put(self.alice, {"statusLine": {"type": "command", "command": "echo hi"}})
        self.install()
        before = self.settings(self.alice).read_text()
        cur, new = ss.planned(self.cfg, self.alice, SAMPAN)
        self.assertEqual(cur, new)
        self.lines.clear()
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.settings(self.alice).read_text(), before)
        self.assertIn("  alice：已經接上，不用改", self.lines)
        self.assertIn("沒有要變更的", self.lines)
        self.assertEqual(len(list(self.settings(self.alice).parent.glob("settings.json.bak-*"))), 1)

    def test_install_rewrites_only_an_older_sampan_path(self):
        old = "'/old path/sampan' statusline -- sh -c 'echo hi'"
        self.put(self.alice, {"statusLine": {"type": "command", "command": old}})
        self.install()
        self.assertEqual(self.get(self.alice)["statusLine"]["command"], f"{SAMPAN} statusline -- sh -c 'echo hi'")

    def test_uninstall_restores_original(self):
        orig = "jq -r '.a' | cat"
        self.put(self.alice, {"statusLine": {"type": "command", "command": orig, "padding": 2}})
        self.install()
        self.assertEqual(self.get(self.alice)["statusLine"]["padding"], 2)
        self.assertEqual(ss.uninstall(self.cfg, self.lines.append, lambda _p: "y", [self.alice]), 0)
        self.assertEqual(self.get(self.alice)["statusLine"],
                         {"type": "command", "command": orig, "padding": 2})

    def test_uninstall_hand_written_and_bare(self):
        self.put(self.alice, {"statusLine": {"type": "command", "command": f"{SAMPAN} statusline -- my line 'a b'"}})
        ss.uninstall(self.cfg, self.lines.append, lambda _p: "y", [self.alice])
        self.assertEqual(self.get(self.alice)["statusLine"]["command"], "my line 'a b'")
        self.put(self.alice, {"statusLine": {"type": "command", "command": "echo hi"}})
        ss.uninstall(self.cfg, self.lines.append, lambda _p: "y", [self.alice])
        self.assertEqual(self.get(self.alice)["statusLine"]["command"], "echo hi")

    def test_install_keeps_other_keys(self):
        self.put(self.alice, {"env": {"A": "中文"}, "hooks": {"Stop": []}, "statusLine": {"type": "command",
                                                                                         "command": "x", "padding": 1}})
        self.install()
        d = self.get(self.alice)
        self.assertEqual(d["env"], {"A": "中文"})
        self.assertEqual(d["hooks"], {"Stop": []})
        self.assertEqual(d["statusLine"]["padding"], 1)
        self.assertIn("中文", self.settings(self.alice).read_text())

    def test_install_refuses_invalid_json(self):
        self.put(self.alice, "{not json")
        self.put(self.work, "[1, 2]")
        rc = self.install([self.alice, self.work])
        self.assertEqual(rc, 1)
        self.assertEqual(self.settings(self.alice).read_text(), "{not json")
        self.assertEqual(self.settings(self.work).read_text(), "[1, 2]")
        self.assertIn(f"❌ alice：{self.settings(self.alice)} 不是合法的 JSON，沒改", self.lines)
        self.assertEqual(list(self.settings(self.alice).parent.glob("settings.json.bak-*")), [])

    def test_invalid_one_does_not_stop_the_others(self):
        self.put(self.alice, "oops")
        self.assertEqual(self.install([self.alice, self.work]), 1)
        self.assertIn("statusLine", self.get(self.work))

    def test_no_means_no_write(self):
        self.put(self.alice, {"a": 1})
        self.install(answer="n")
        self.assertEqual(self.get(self.alice), {"a": 1})
        self.assertIn("已取消", self.lines)


class DoctorLinesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        for p in ("alice", "work"):
            write_plugins(self.cfg, p, known=GOOD_KNOWN)
        self.r = FakeRunner().on(["claude", "auth", "status"], cp(out=json.dumps({"loggedIn": True, "email": "a@b.c"})))

    def lines(self):
        cs = run_checks(self.cfg, self.r, now=NOW, uid=0, sampan_path=SAMPAN, which=lambda n: f"/bin/{n}",
                        env={"PATH": "/usr/bin"}, platform="linux", current="0.8.0", latest_fn=lambda: None)
        return [c for c in cs if "狀態列" in c.title]

    def test_doctor_lines(self):
        got = {c.title.split("：")[0]: c for c in self.lines()}
        self.assertEqual(got["alice"].level, "info")
        self.assertIn("沒有安裝", got["alice"].title)
        self.assertIn("sampan statusline install", got["alice"].detail)
        real = Path(self.cfg.home) / "sampan"
        real.write_text("")
        alice = self.cfg.profiles["alice"]
        (Path(alice.config_dir) / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": f"{real} statusline"}}))
        work = self.cfg.profiles["work"]
        (Path(work.config_dir) / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "/gone/sampan statusline -- x"}}))
        got = {c.title.split("：")[0]: c for c in self.lines()}
        self.assertEqual(got["alice"].level, "info")
        self.assertEqual(got["alice"].title, "alice：狀態列已接上（額度 還沒有資料）")
        self.assertEqual(got["work"].level, "warn")
        self.assertIn("sampan statusline install", got["work"].detail)


class InitOfferTest(SetupTest):
    def test_offer_installs_for_all_after_one_y(self):
        from sampan.init import _offer_statusline
        asked = []

        def ask(p):
            asked.append(p)
            return "y"
        _offer_statusline(self.cfg, ask, self.lines.append, SAMPAN)
        self.assertEqual(len(asked), 1)
        for p in (self.alice, self.work):
            self.assertEqual(self.get(p)["statusLine"]["command"], f"{SAMPAN} statusline")

    def test_offer_default_is_no_and_skips_when_all_done(self):
        from sampan.init import _offer_statusline
        _offer_statusline(self.cfg, lambda _p: "", self.lines.append, SAMPAN)
        self.assertFalse(self.settings(self.alice).exists())
        self.install([self.alice, self.work])
        asked = []
        _offer_statusline(self.cfg, lambda p: asked.append(p) or "y", self.lines.append, SAMPAN)
        self.assertEqual(asked, [])
