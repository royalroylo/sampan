"""Checking for a new version (git ls-remote tags, cached for 6 hours) and the U update flow. All subprocesses are fake: no network, the real uv install is untouched."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan import update
from sampan.update import Checker, do_update, latest_tag, newer, reinstall_cmd
from tests.fakes import FakeRunner, cp
from tests.helpers import status_row

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"


def tags(*names):
    return cp(out="".join(f"{SHA}\trefs/tags/{n}\n" for n in names))


class LatestTagTest(unittest.TestCase):
    def test_picks_highest_semver_ignoring_others(self):
        r = FakeRunner().on(["git", "ls-remote"], tags("v0.4.5", "v0.10.0", "v0.9.12", "latest", "v1.0.0-rc1",
                                                         "v0.4", "foo/v9.9.9", "v0.11.0.dev1"))
        self.assertEqual(latest_tag(r), "0.10.0")
        args = r.calls[0]["args"]
        self.assertEqual(args, ["git", "ls-remote", "--tags", "--refs", update.REPO])
        self.assertEqual(r.calls[0]["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_failures_return_none(self):
        self.assertIsNone(latest_tag(FakeRunner().on(["git"], cp(rc=128, err="auth failed"))))
        self.assertIsNone(latest_tag(FakeRunner().on(["git"], cp(rc=124))))
        self.assertIsNone(latest_tag(FakeRunner().on(["git"], tags("nightly"))))

        def boom(*a, **kw):
            raise OSError("no network")
        self.assertIsNone(latest_tag(boom))

    def test_default_runner_handles_timeout_and_missing_binary(self):
        import subprocess

        def timeout(*a, **kw):
            raise subprocess.TimeoutExpired(a[0], 10)
        orig = subprocess.run
        try:
            subprocess.run = timeout
            self.assertEqual(update._run(["git"]).returncode, 124)
        finally:
            subprocess.run = orig
        self.assertEqual(update._run(["/nonexistent/sampan-no-such-binary"]).returncode, 127)


class NewerTest(unittest.TestCase):
    def test_numeric_compare(self):
        self.assertTrue(newer("0.4.5", "0.4.6"))
        self.assertTrue(newer("0.9.0", "0.10.0"))
        self.assertFalse(newer("0.4.6", "0.4.6"))
        self.assertFalse(newer("0.5.0", "0.4.9"))

    def test_dev_versions_count_as_their_base(self):
        self.assertFalse(newer("0.4.6.dev3+gabc1234", "0.4.6"))
        self.assertTrue(newer("0.4.6.dev3+gabc1234", "0.4.7"))
        self.assertFalse(newer("0.4.6+dirty", "0.4.6"))

    def test_unparseable_or_missing_gives_no_hint(self):
        for cur in ("unknown", "0.0.0+unknown", "", None, "abc"):
            self.assertFalse(newer(cur, "9.9.9"), cur)
        self.assertFalse(newer("0.4.5", None))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.r = FakeRunner().on(["git", "ls-remote"], tags("v0.4.6"))

    def check(self, now):
        return update.check(self.dir, run=self.r, now=lambda: now)

    def test_checks_then_uses_cache_within_ttl(self):
        self.assertEqual(self.check(NOW)[0], "0.4.6")
        data = json.loads((self.dir / "update-check.json").read_text())
        self.assertEqual(data["latest"], "0.4.6")
        self.assertEqual(datetime.fromisoformat(data["checked_at"]), NOW)
        self.assertEqual(self.check(NOW + timedelta(hours=5, minutes=59))[0], "0.4.6")
        self.assertEqual(len(self.r.calls), 1)
        self.check(NOW + timedelta(hours=6))
        self.assertEqual(len(self.r.calls), 2)

    def test_failure_is_cached_as_null(self):
        r = FakeRunner().on(["git"], cp(rc=128))
        latest, at = update.check(self.dir, run=r, now=lambda: NOW)
        self.assertIsNone(latest)
        self.assertEqual(at, NOW)
        self.assertIsNone(json.loads((self.dir / "update-check.json").read_text())["latest"])

    def test_corrupt_or_missing_cache_checks(self):
        (self.dir / "update-check.json").write_text("{nope")
        self.assertEqual(self.check(NOW)[0], "0.4.6")
        (self.dir / "update-check.json").write_text(json.dumps({"checked_at": "yesterday", "latest": "0.1.0"}))
        self.assertEqual(self.check(NOW)[0], "0.4.6")
        self.assertEqual(len(self.r.calls), 2)

    def test_cache_from_the_future_is_stale(self):
        update.write_cache(self.dir, "0.1.0", NOW + timedelta(days=1))
        self.assertEqual(self.check(NOW)[0], "0.4.6")

    def test_check_force_ignores_fresh_cache(self):
        update.write_cache(self.dir, "0.6.0", NOW - timedelta(minutes=5))
        r = FakeRunner().on(["git", "ls-remote"], tags("v0.6.0", "v0.6.1"))
        self.assertEqual(update.check(self.dir, run=r, now=lambda: NOW)[0], "0.6.0")  # fresh cache, no network
        self.assertEqual(r.calls, [])
        latest, at = update.check(self.dir, run=r, now=lambda: NOW, force=True)
        self.assertEqual((latest, at), ("0.6.1", NOW))
        self.assertEqual(len(r.calls), 1)
        self.assertEqual(update.read_cache(self.dir), ("0.6.1", NOW))

    def test_read_cache_ignores_age(self):
        update.write_cache(self.dir, "0.4.6", NOW - timedelta(days=30))
        self.assertEqual(update.read_cache(self.dir), ("0.4.6", NOW - timedelta(days=30)))


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.r = FakeRunner().on(["git", "ls-remote"], tags("v0.4.6"))
        self.now = NOW
        self.spawned = []

    def checker(self, current="0.4.5"):
        return Checker(current, self.dir, run=self.r, now=lambda: self.now, spawn=self.spawned.append)

    def test_start_runs_in_background_and_sets_hint(self):
        c = self.checker()
        c.start()
        self.assertIsNone(c.hint())  # not finished yet
        c.start()  # not started again while running
        self.assertEqual(len(self.spawned), 1)
        self.spawned.pop()()
        self.assertEqual(c.hint(), "有新版 v0.4.6（目前 0.4.5），按 U 更新")

    def test_no_hint_when_up_to_date_or_failed(self):
        c = self.checker("0.4.6")
        c.refresh()
        self.assertIsNone(c.hint())
        self.r.on(["git"], cp(rc=128))
        c = Checker("0.4.5", Path(tempfile.mkdtemp()), run=self.r, now=lambda: NOW, spawn=lambda f: f())
        c.start()
        self.assertIsNone(c.hint())

    def test_tick_rechecks_only_after_ttl(self):
        c = self.checker()
        c.refresh()
        c.tick()
        self.assertEqual(self.spawned, [])
        self.now = NOW + timedelta(hours=6, seconds=1)
        c.tick()
        self.assertEqual(len(self.spawned), 1)

    def test_background_error_is_swallowed(self):
        def boom(*a, **kw):
            raise RuntimeError("x")
        c = Checker("0.4.5", self.dir, run=boom, now=lambda: NOW, spawn=lambda f: f())
        c.start()
        self.assertIsNone(c.hint())
        self.assertFalse(c._busy)

    def test_refresh_force_passes_through(self):
        update.write_cache(self.dir, "0.4.5", NOW - timedelta(minutes=1))
        c = self.checker()
        c.refresh()
        self.assertEqual((c.latest, self.r.calls), ("0.4.5", []))
        c.refresh(force=True)
        self.assertEqual(c.latest, "0.4.6")
        self.assertEqual(len(self.r.calls), 1)

    def test_load_cache_only(self):
        update.write_cache(self.dir, "0.4.6", NOW - timedelta(days=2))
        c = self.checker()
        c.load_cache()
        self.assertEqual(c.available(), "0.4.6")
        self.assertEqual(self.r.calls, [])


class DoUpdateTest(unittest.TestCase):
    def setUp(self):
        self.msgs, self.streamed = [], []

    def stream(self, results):
        results = iter(results)

        def fn(args, on_line):
            self.streamed.append(args)
            rc, lines = next(results)
            for line in lines:
                on_line(line)
            return rc
        return fn

    def versions(self, *vs):
        vs = iter(vs)
        return FakeRunner().on(["sampan", "--version"], lambda a: cp(out=f"sampan {next(vs)}\n"))

    def test_upgrade_success(self):
        ok = do_update(self.msgs.append, current="0.4.5", stream=self.stream([(0, ["Updated sampan"])]),
                       run=self.versions("0.4.6"), which=lambda n: "/x/uv")
        self.assertTrue(ok)
        self.assertEqual(self.streamed, [["uv", "tool", "upgrade", "sampan"]])
        self.assertIn("  Updated sampan", self.msgs)

    def test_unchanged_falls_back_to_reinstall_from_git(self):
        ok = do_update(self.msgs.append, current="0.4.5", stream=self.stream([(0, ["Nothing to upgrade"]), (0, [])]),
                       run=self.versions("0.4.5", "0.4.6").on(["git", "ls-remote"], cp(out="a\trefs/tags/v0.4.6\n")),
                       which=lambda n: "/x/uv")
        self.assertTrue(ok)
        self.assertEqual(self.streamed[1], ["uv", "tool", "install", "--reinstall", f"git+{update.REPO}@v0.4.6"])

    def test_reinstall_pins_the_tag_that_was_found_not_a_fixed_one(self):
        run = self.versions("0.7.2", "0.7.3")
        run.on(["git", "ls-remote"], cp(out="a\trefs/tags/v0.7.2\nb\trefs/tags/v0.7.3\n"))
        do_update(self.msgs.append, current="0.7.2", stream=self.stream([(0, []), (0, [])]), run=run,
                  which=lambda n: "/x/uv")
        self.assertTrue(self.streamed[1][-1].endswith("@v0.7.3"), self.streamed[1])

    def test_reinstall_is_unpinned_when_no_tag_can_be_found(self):
        run = self.versions("0.4.5", "0.4.6")
        run.on(["git", "ls-remote"], cp(rc=128))
        do_update(self.msgs.append, current="0.4.5", stream=self.stream([(0, []), (0, [])]), run=run,
                  which=lambda n: "/x/uv")
        self.assertEqual(self.streamed[1][-1], f"git+{update.REPO}")

    def test_both_fail(self):
        ok = do_update(self.msgs.append, current="0.4.5", stream=self.stream([(1, ["error: x"]), (2, ["error: y"])]),
                       run=self.versions("0.4.5", "0.4.5"), which=lambda n: "/x/uv")
        self.assertFalse(ok)
        self.assertIn("  error: y", self.msgs)
        self.assertTrue(self.msgs[-1].startswith("❌"))

    def test_do_update_force_reinstalls_same_version(self):
        ok = do_update(self.msgs.append, current="0.6.1", stream=self.stream([(0, ["Installed sampan"])]),
                       run=self.versions("0.6.1"), which=lambda n: "/x/uv", latest="0.6.1", force=True)
        self.assertTrue(ok)
        self.assertEqual(self.streamed, [["uv", "tool", "install", "--reinstall", f"git+{update.REPO}@v0.6.1"]])
        self.assertEqual(self.msgs[-1], "✔ 已重新安裝 0.6.1，重新開啟 sampan…")

    def test_force_looks_up_the_tag_when_not_given(self):
        run = self.versions("0.6.1").on(["git", "ls-remote"], tags("v0.6.1"))
        self.assertTrue(do_update(self.msgs.append, current="0.6.1", stream=self.stream([(0, [])]), run=run,
                                  which=lambda n: "/x/uv", force=True))
        self.assertEqual(self.streamed, [reinstall_cmd("0.6.1")])

    def test_force_failure(self):
        ok = do_update(self.msgs.append, current="0.6.1", stream=self.stream([(1, ["error: x"])]),
                       run=self.versions("0.6.1"), which=lambda n: "/x/uv", latest="0.6.1", force=True)
        self.assertFalse(ok)
        self.assertEqual(len(self.streamed), 1)
        self.assertTrue(self.msgs[-1].startswith("❌"))
        run = FakeRunner().on(["sampan", "--version"], cp(rc=1))
        self.assertFalse(do_update(self.msgs.append, current="0.6.1", stream=self.stream([(0, [])]), run=run,
                                   which=lambda n: "/x/uv", latest="0.6.1", force=True))

    def test_uv_missing(self):
        ok = do_update(self.msgs.append, current="0.4.5", stream=self.stream([]), run=FakeRunner(),
                       which=lambda n: None)
        self.assertFalse(ok)
        self.assertIn("uv tool upgrade sampan", self.msgs[-1])
        self.assertIn("--reinstall", self.msgs[-1])
        self.assertNotIn("@v", self.msgs[-1])  # no tag known: shown unpinned
        self.assertEqual(self.streamed, [])

    def test_uv_missing_with_known_latest_shows_pinned_command(self):
        do_update(self.msgs.append, current="0.4.5", stream=self.stream([]), run=FakeRunner(),
                  which=lambda n: None, latest="0.4.6")
        self.assertIn("@v0.4.6", self.msgs[-1])


class ReinstallCmdTest(unittest.TestCase):
    def test_pins_the_given_tag(self):
        cmd = update.reinstall_cmd("0.6.1")
        self.assertEqual(cmd[:4], ["uv", "tool", "install", "--reinstall"])
        self.assertTrue(cmd[-1].endswith("@v0.6.1"), cmd)

    def test_without_a_tag_it_is_unpinned(self):
        self.assertEqual(update.reinstall_cmd()[-1], f"git+{update.REPO}")

    def test_version_has_one_implementation(self):
        from sampan import cli
        self.assertIs(cli.version, update.current_version)


class FakeUpdater:
    def __init__(self, latest="0.4.6", current="0.4.5"):
        self.latest, self.current, self.ticks, self.refreshed = latest, current, 0, []

    def refresh(self, force=False):
        self.refreshed.append(force)

    def available(self):
        return self.latest if newer(self.current, self.latest) else None

    def hint(self):
        return f"有新版 v{self.latest}（目前 {self.current}），按 U 更新" if self.available() else None

    def tick(self):
        self.ticks += 1


def row(name):
    return status_row(name)


class UiUpdateTest(unittest.TestCase):
    def setUp(self):
        from tests.helpers import make_env
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.msgs, self.execs, self.updates, self.update_kws = [], [], [], []

    def ui(self, actions, answers=(), *, updater=None, result=True, picker=True, argv=("ui",)):
        from sampan.ui import cmd_ui
        actions, answers = iter(actions), iter(answers)

        def update_fn(out, *, current, **kw):
            self.updates.append(current)
            self.update_kws.append(kw)
            out("  uv 的輸出")
            return result
        return cmd_ui(self.cfg, FakeRunner(), self.msgs.append, lambda _p: next(answers),
                      rows_fn=lambda c, r: [row("a")], picker_fn=(lambda rows, cur: next(actions)) if picker else None,
                      exec_fn=lambda *a: self.execs.append(a), updater=updater, update_fn=update_fn, argv=list(argv),
                      loop=True)

    def test_u_confirms_updates_and_restarts_with_same_args(self):
        rc = self.ui([("update", [])], ["y"], updater=FakeUpdater())
        self.assertEqual(rc, 0)
        self.assertEqual(self.updates, ["0.4.5"])
        self.assertEqual(self.execs, [("sampan", ["sampan", "ui"])])

    def test_u_failure_stays_in_list(self):
        rc = self.ui([("update", []), ("quit", [])], ["y"], updater=FakeUpdater(), result=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.execs, [])
        self.assertIn("  uv 的輸出", self.msgs)

    def test_u_cancel_and_up_to_date(self):
        self.ui([("update", []), ("quit", [])], ["n"], updater=FakeUpdater())
        self.assertEqual(self.updates, [])
        self.assertIn("已取消", self.msgs)
        self.ui([("update", []), ("quit", [])], updater=None)
        self.assertEqual(self.msgs.count("目前已是最新版"), 1)
        self.assertEqual(self.updates, [])

    def test_U_rechecks_when_cache_says_up_to_date(self):
        state = Path(tempfile.mkdtemp())
        update.write_cache(state, "0.6.0", NOW - timedelta(minutes=5))
        r = FakeRunner().on(["git", "ls-remote"], tags("v0.6.0", "v0.6.1"))
        checker = Checker("0.6.0", state, run=r, now=lambda: NOW, spawn=lambda f: None)
        checker.load_cache()
        asked = []
        from sampan.ui.actions import _update

        def ask(prompt):
            asked.append(prompt)
            return "y"

        def update_fn(out, *, current, **kw):
            self.updates.append(current)
            return True
        self.assertTrue(_update(self.msgs.append, ask, checker, update_fn))
        self.assertIn("▶ 檢查最新版…", self.msgs)
        self.assertEqual(asked, ["更新到 v0.6.1？(y/N): "])
        self.assertEqual(self.updates, ["0.6.0"])
        self.assertEqual(len(r.calls), 1)

    def test_U_up_to_date_offers_reinstall(self):
        up = FakeUpdater(latest="0.4.5")
        rc = self.ui([("update", [])], ["y"], updater=up)
        self.assertEqual(rc, 0)
        self.assertEqual(up.refreshed, [True])
        self.assertIn("▶ 檢查最新版…", self.msgs)
        self.assertIn("目前已是最新版（v0.4.5）", self.msgs)
        self.assertEqual(self.updates, ["0.4.5"])
        self.assertEqual(self.update_kws, [{"latest": "0.4.5", "force": True}])
        self.assertEqual(self.execs, [("sampan", ["sampan", "ui"])])

    def test_U_up_to_date_reinstall_declined(self):
        asked = []
        from sampan.ui.actions import _update

        def ask(prompt):
            asked.append(prompt)
            return "n"
        self.assertFalse(_update(self.msgs.append, ask, FakeUpdater(latest="0.4.5"), lambda *a, **k: 1 / 0))
        self.assertEqual(asked, ["要重新安裝目前版本嗎？(y/N): "])
        self.assertEqual(self.msgs[-1], "已取消")

    def test_U_latest_unknown_says_so_then_offers_reinstall(self):
        from sampan.ui.actions import _update
        up = FakeUpdater(latest=None)
        self.assertTrue(_update(self.msgs.append, lambda _p: "y", up,
                                lambda out, *, current, **kw: self.update_kws.append(kw) or True))
        self.assertIn("查不到最新版（離線或連不到 GitHub）", self.msgs)
        self.assertEqual(self.update_kws, [{"latest": None, "force": True}])

    def test_plain_prints_hint_once_and_accepts_u(self):
        self.ui([], ["x", "U", "y"], updater=FakeUpdater(), picker=False, argv=["ui", "--plain"])
        self.assertEqual(self.msgs.count("有新版 v0.4.6（目前 0.4.5），按 U 更新"), 1)
        self.assertEqual(self.execs, [("sampan", ["sampan", "ui", "--plain"])])

    def test_plain_no_hint_when_up_to_date(self):
        self.ui([], ["q"], updater=FakeUpdater(latest="0.4.5"), picker=False)
        self.assertFalse(any("有新版" in m for m in self.msgs))

    def test_picker_u_key(self):
        from sampan.ui import Picker
        self.assertEqual(Picker([]).key("U"), ("update", []))


class ScreenHintTest(unittest.TestCase):
    def setUp(self):
        from tests.helpers import make_env
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def test_render_notice_line_above_help_and_fits(self):
        from sampan.ui import frame_text, render, text_width
        for w, h in ((120, 20), (80, 20), (40, 6), (10, 3), (1, 1)):
            lines = frame_text(render(title="sampan", left=[], cursor=0, card=[], help="q 離開", width=w, height=h,
                                      notice="有新版 v0.4.6（目前 0.4.5），按 U 更新"))
            self.assertLessEqual(len(lines), h)
            self.assertTrue(all(text_width(x) <= w for x in lines))
        lines = frame_text(render(title="sampan", left=[], cursor=0, card=[], help="q 離開", width=120, height=20,
                                  messages=["✔ ok"], notice="有新版 v0.4.6（目前 0.4.5），按 U 更新"))
        self.assertIn("有新版 v0.4.6", lines[-2])
        self.assertIn("✔ ok", lines[-3])

    def test_pick_shows_hint_and_ticks_updater(self):
        from sampan.ui import CursesScreen
        from tests.test_ui_screen import TICK, FakeScr
        up = FakeUpdater()
        scr = FakeScr([TICK, "q"])
        s = CursesScreen(scr, self.cfg, FakeRunner(), rows_fn=lambda c, r: [row("a")], updater=up)
        self.assertEqual(s.pick([row("a")], None), ("quit", []))
        self.assertIn("有新版 v0.4.6（目前 0.4.5），按 U 更新", "\n".join(scr.screens[-1]))
        self.assertEqual(up.ticks, 1)

    def test_pick_without_newer_has_no_hint(self):
        from sampan.ui import CursesScreen
        from tests.test_ui_screen import FakeScr
        scr = FakeScr(["q"])
        s = CursesScreen(scr, self.cfg, FakeRunner(), updater=FakeUpdater(latest="0.4.5"))
        s.pick([row("a")], None)
        self.assertNotIn("有新版", "\n".join(scr.screens[-1]))

    def test_curses_ui_update_leaves_curses_then_execs(self):
        from sampan.ui import curses_ui
        from tests.test_ui_screen import FakeScr
        events = []

        def wrapper(fn):
            rc = fn(FakeScr(["U", "y", "\n"]))
            events.append("curses closed")
            return rc
        rc = curses_ui(self.cfg, FakeRunner(), lambda _l: None, wrapper=wrapper, rows_fn=lambda c, r: [row("a")],
                       exec_fn=lambda f, a: events.append(("exec", f, a)), updater=FakeUpdater(), argv=[],
                       update_fn=lambda out, *, current: True)
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["curses closed", ("exec", "sampan", ["sampan"])])


class DoctorVersionTest(unittest.TestCase):
    def test_lines(self):
        from sampan.doctor import _version
        c = _version("0.4.5", lambda: "0.4.6")[0]
        self.assertEqual((c.level, c.title), ("warn", "sampan 版本：0.4.5（最新 v0.4.6，sampan 畫面按 U 更新）"))
        c = _version("0.4.6", lambda: "0.4.6")[0]
        self.assertEqual((c.level, c.title), ("ok", "sampan 版本：0.4.6（已是最新）"))
        c = _version("0.4.6.dev2+gabc", lambda: "0.4.6")[0]
        self.assertEqual(c.level, "ok")
        c = _version("0.4.5", lambda: None)[0]
        self.assertEqual((c.level, c.title), ("info", "sampan 版本：0.4.5（查不到最新版本）"))

        def boom():
            raise OSError
        self.assertEqual(_version("0.4.5", boom)[0].level, "info")

    def test_run_checks_includes_version_without_network(self):
        from sampan.doctor import run_checks
        from tests.helpers import make_env
        cfg = make_env(Path(tempfile.mkdtemp()))
        r = FakeRunner().on(["git", "ls-remote"], tags("v0.4.6"))
        checks = run_checks(cfg, r, now=NOW, uid=0, sampan_path="/x/sampan", which=lambda n: None, env={},
                            current="0.4.5")
        self.assertIn("sampan 版本：0.4.5（最新 v0.4.6，sampan 畫面按 U 更新）", [c.title for c in checks])
        self.assertTrue((cfg.state_dir / "update-check.json").exists())


if __name__ == "__main__":
    unittest.main()
