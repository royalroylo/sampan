import os
import plistlib
import tempfile
import unittest
from pathlib import Path

from sampan import launchd
from tests.fakes import FakeRunner, cp

SAMPAN = "/Users/u/.local/bin/sampan"


class PlistTests(unittest.TestCase):
    def test_plist_has_no_restore_and_utf8_env(self):
        text = launchd.plist_text(SAMPAN, Path("/Users/u/.local/state/sampan"),
                                  "/Users/u/.local/bin:/opt/homebrew/bin:/usr/bin:/bin")
        self.assertNotIn("restore", text)
        self.assertIn("LC_CTYPE", text)
        data = plistlib.loads(text.encode())
        self.assertEqual(data["Label"], launchd.LABEL)
        self.assertEqual(data["ProgramArguments"], [SAMPAN, "save", "--quiet"])
        self.assertIs(data["RunAtLoad"], False)
        # Fires every 5 minutes on the clock: a run missed during sleep is made up on wake (StartInterval's timer pauses during sleep)
        self.assertEqual(data["StartCalendarInterval"], [{"Minute": m} for m in range(0, 60, 5)])
        self.assertNotIn("StartInterval", data)
        self.assertEqual(data["EnvironmentVariables"]["LANG"], "en_US.UTF-8")
        self.assertEqual(data["EnvironmentVariables"]["LC_CTYPE"], "en_US.UTF-8")
        self.assertIn("/opt/homebrew/bin", data["EnvironmentVariables"]["PATH"])
        self.assertEqual(data["StandardOutPath"], "/Users/u/.local/state/sampan/launchd.out.log")
        self.assertEqual(data["StandardErrorPath"], "/Users/u/.local/state/sampan/launchd.err.log")
        for verb in ("restore", "new", "move"):
            self.assertNotIn(verb, data["ProgramArguments"])

    def test_default_path_env_has_local_bin_and_homebrew(self):
        parts = launchd.default_path_env(Path("/Users/u")).split(":")
        self.assertIn("/Users/u/.local/bin", parts)
        self.assertIn("/opt/homebrew/bin", parts)
        self.assertIn("/usr/sbin", parts)  # lsof lives in /usr/sbin
        self.assertIn("/sbin", parts)

    def test_plist_carries_sampan_env_overrides(self):
        env = {"SAMPAN_CONFIG": "/Users/u/c.toml", "SAMPAN_STATE_DIR": "/Users/u/st"}
        data = plistlib.loads(launchd.plist_text(SAMPAN, Path("/Users/u/st"), "/usr/bin", env).encode())
        self.assertEqual({k: data["EnvironmentVariables"][k] for k in env}, env)
        data = plistlib.loads(launchd.plist_text(SAMPAN, Path("/Users/u/st"), "/usr/bin").encode())
        self.assertNotIn("SAMPAN_CONFIG", data["EnvironmentVariables"])


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.state = self.home / "state"

    def test_install_writes_plist_then_bootout_and_bootstrap(self):
        r = FakeRunner().on(["launchctl", "bootout"], cp(rc=3, err="not loaded"))
        self.assertTrue(launchd.install(r, SAMPAN, self.state, self.home))
        path = launchd.plist_path(self.home)
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["ProgramArguments"], launchd.expected_program(SAMPAN))
        self.assertIn(str(self.home / ".local" / "bin"), data["EnvironmentVariables"]["PATH"])
        self.assertTrue(self.state.is_dir())
        uid = os.getuid()
        self.assertEqual([c["args"] for c in r.calls], [
            ["launchctl", "bootout", f"gui/{uid}/{launchd.LABEL}"],
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
        ])
        self.assertEqual(launchd.check(self.home, SAMPAN), ("ok", launchd.expected_program(SAMPAN)))

    def test_labels(self):
        self.assertEqual(launchd.LABEL, "io.github.royalroylo.sampan-save")

    def test_install_reports_bootstrap_failure(self):
        r = FakeRunner().on(["launchctl", "bootstrap"], cp(rc=5, err="Input/output error"))
        self.assertFalse(launchd.install(r, SAMPAN, self.state, self.home))

    def test_check_states(self):
        self.assertEqual(launchd.check(self.home, SAMPAN), ("missing", None))
        path = launchd.plist_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("not a plist")
        self.assertEqual(launchd.check(self.home, SAMPAN), ("unreadable", None))
        path.write_bytes(plistlib.dumps({"ProgramArguments": ["/old/sampan", "save", "--quiet"]}))
        self.assertEqual(launchd.check(self.home, SAMPAN), ("wrong", ["/old/sampan", "save", "--quiet"]))

    def test_plist_runs_every_five_minutes(self):
        data = plistlib.loads(launchd.plist_text(SAMPAN, Path("/Users/u/st"), "/usr/bin").encode())
        self.assertEqual(data["StartCalendarInterval"], [{"Minute": m} for m in range(0, 60, 5)])
        self.assertNotIn("StartInterval", data)

    def test_hourly_plist_is_reported_as_old_schedule(self):
        launchd.write_plist(SAMPAN, self.state, self.home)
        path = launchd.plist_path(self.home)
        data = plistlib.loads(path.read_bytes())
        data["StartCalendarInterval"] = {"Minute": 0}
        path.write_bytes(plistlib.dumps(data))
        self.assertEqual(launchd.check(self.home, SAMPAN), ("schedule", [SAMPAN, "save", "--quiet"]))

    def test_check_reports_old_interval_schedule(self):
        launchd.write_plist(SAMPAN, self.state, self.home)
        path = launchd.plist_path(self.home)
        data = plistlib.loads(path.read_bytes())
        del data["StartCalendarInterval"]
        data["StartInterval"] = 3600
        path.write_bytes(plistlib.dumps(data))
        self.assertEqual(launchd.check(self.home, SAMPAN), ("schedule", [SAMPAN, "save", "--quiet"]))

    def test_check_reports_other_sampan_env(self):
        env = {"SAMPAN_CONFIG": "/Users/u/c.toml"}
        launchd.write_plist(SAMPAN, self.state, self.home, env)
        self.assertEqual(launchd.check(self.home, SAMPAN, env=env)[0], "ok")
        self.assertEqual(launchd.check(self.home, SAMPAN, env={})[0], "env")
        self.assertEqual(launchd.check(self.home, SAMPAN)[0], "ok")  # no comparison when env isn't given

    def test_check_not_loaded_queries_launchctl_print(self):
        launchd.write_plist(SAMPAN, self.state, self.home)
        self.assertEqual(launchd.check(self.home, SAMPAN)[0], "ok")  # when run isn't given, only the file is checked
        r = FakeRunner().on(["launchctl", "print"], cp(rc=113))
        self.assertEqual(launchd.check(self.home, SAMPAN, r, uid=501)[0], "not_loaded")
        self.assertEqual(r.calls[0]["args"], ["launchctl", "print", f"gui/501/{launchd.LABEL}"])
        self.assertEqual(launchd.check(self.home, SAMPAN, FakeRunner(), uid=501)[0], "ok")


if __name__ == "__main__":
    unittest.main()
