"""Checking for a new version (git ls-remote tags, cached for 6 hours) and the U update flow. All subprocesses are fake: no network, the real uv install is untouched."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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

    def test_reopen_false_success_lines_do_not_promise_a_restart(self):
        do_update(self.msgs.append, current="0.6.0", stream=self.stream([(0, [])]), run=self.versions("0.6.1"),
                  which=lambda n: "/x/uv", reopen=False)
        self.assertEqual(self.msgs[-1], "✔ 已更新到 0.6.1")
        do_update(self.msgs.append, current="0.6.1", stream=self.stream([(0, [])]), run=self.versions("0.6.1"),
                  which=lambda n: "/x/uv", latest="0.6.1", force=True, reopen=False)
        self.assertEqual(self.msgs[-1], "✔ 已重新安裝 0.6.1")

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

    def ui(self, actions, answers=(), *, updater=None, result=True, picker=True, argv=("ui",), **kw):
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
                      loop=True, **kw)

    def test_u_confirms_updates_and_restarts_with_same_args(self):
        rc = self.ui([("update", [])], ["y"], updater=FakeUpdater())
        self.assertEqual(rc, 0)
        self.assertEqual(self.updates, ["0.4.5"])
        self.assertEqual(self.execs, [("sampan", ["sampan", "ui"])])

    def test_u_prints_the_update_notices_after_a_successful_update(self):
        calls = []

        def notices():
            calls.append(1)
            return ["這些 session 要重開才會用新版：alpha-pm（在 sampan 畫面按 S）。", "第二行"]
        self.ui([("update", [])], ["y"], updater=FakeUpdater(), notices_fn=notices)
        self.assertEqual(calls, [1])
        self.assertEqual(self.msgs[-2:], ["這些 session 要重開才會用新版：alpha-pm（在 sampan 畫面按 S）。", "第二行"])
        self.assertEqual(self.execs, [("sampan", ["sampan", "ui"])])  # still restarts after them

    def test_u_failure_or_cancel_prints_no_notices(self):
        calls = []

        def notices():
            calls.append(1)
            return ["不該出現"]
        self.ui([("update", []), ("quit", [])], ["y"], updater=FakeUpdater(), result=False, notices_fn=notices)
        self.ui([("update", []), ("quit", [])], ["n"], updater=FakeUpdater(), notices_fn=notices)
        self.assertEqual(calls, [])
        self.assertNotIn("不該出現", self.msgs)

    def test_u_a_failing_notice_still_restarts(self):
        self.ui([("update", [])], ["y"], updater=FakeUpdater(), notices_fn=lambda: 1 / 0)
        self.assertEqual(self.execs, [("sampan", ["sampan", "ui"])])

    def test_u_default_notices_are_after_update_notices(self):
        with mock.patch("sampan.update.after_update_notices", return_value=["提示"]) as an:
            self.ui([("update", [])], ["y"], updater=FakeUpdater())
        self.assertEqual(an.call_args.args[1], self.cfg)
        self.assertIn("提示", self.msgs)

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

    HINT = "；要重新安裝請用 sampan update --force"

    def only_informs(self, up):
        from sampan.ui.actions import _update
        self.msgs.clear()
        self.assertFalse(_update(self.msgs.append, lambda p: 1 / 0, up, lambda *a, **k: 1 / 0))
        self.assertEqual(up.refreshed, [True])
        return self.msgs

    def test_update_when_latest_only_informs(self):
        msgs = self.only_informs(FakeUpdater(latest="0.4.5"))
        self.assertEqual(msgs, ["▶ 檢查最新版…", "目前已是最新版（v0.4.5）" + self.HINT])

    def test_update_unknown_latest_only_informs(self):
        msgs = self.only_informs(FakeUpdater(latest=None, current="0.6.1"))
        self.assertEqual(msgs, ["▶ 檢查最新版…", "查不到最新版（離線或連不到 GitHub）" + self.HINT])

    def test_update_dev_build_ahead_only_informs(self):
        msgs = self.only_informs(FakeUpdater(latest="0.6.1", current="0.6.2.dev3+gabc"))
        self.assertEqual(msgs, ["▶ 檢查最新版…", "目前版本（0.6.2.dev3+gabc）比最新版 v0.6.1 新，不更新" + self.HINT])

    def test_update_newer_still_asks(self):
        from sampan.ui.actions import _update
        asked = []
        self.assertTrue(_update(self.msgs.append, lambda p: asked.append(p) or "y", FakeUpdater(),
                                lambda out, *, current, **kw: self.update_kws.append(kw) or True))
        self.assertEqual(asked, ["更新到 v0.4.6？(y/N): "])
        self.assertEqual(self.update_kws, [{"latest": "0.4.6"}])

    def test_U_latest_unknown_and_dev_build_offers_nothing(self):
        from sampan.ui.actions import _update
        for cur in ("0.6.2.dev3+gabc", "unknown", "0.6.1+dirty"):
            self.msgs.clear()
            up = FakeUpdater(latest=None, current=cur)
            self.assertFalse(_update(self.msgs.append, lambda p: 1 / 0, up, lambda *a, **k: 1 / 0), cur)
            self.assertEqual(self.msgs, ["▶ 檢查最新版…", "查不到最新版（離線或連不到 GitHub）；要重新安裝請用 sampan update --force"])

    def test_U_newer_passes_latest(self):
        self.ui([("update", [])], ["y"], updater=FakeUpdater())
        self.assertEqual(self.update_kws, [{"latest": "0.4.6"}])

    def test_clean_release(self):
        self.assertEqual(update.clean_release("0.6.1"), "0.6.1")
        self.assertEqual(update.clean_release("v0.6.1"), "0.6.1")
        for v in ("0.6.1.dev2+gabc", "0.6.1+dirty", "unknown", "0.0.0+unknown", "", None, "0.6"):
            self.assertIsNone(update.clean_release(v), v)

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
                       update_fn=lambda out, *, current, **kw: True)
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


class RestartNoticeTest(unittest.TestCase):
    """After `sampan update`: the sessions whose `sampan mcp serve` started before this install, and Codex's tables."""

    INSTALLED_AT = datetime(2026, 11, 13, 9, 0, 0).timestamp()

    def runner(self, starts, panes=None):
        from tests.fakes import ps_line
        procs = "\n".join([
            ps_line(100, 1, "tmux", "tmux"),
            ps_line(200, 100, "zsh", "-zsh"), ps_line(201, 200, "claude", "claude --resume x"),
            ps_line(202, 201, "python3", "/u/.local/share/uv/tools/sampan/bin/python /u/.local/bin/sampan mcp serve"),
            ps_line(300, 100, "zsh", "-zsh"), ps_line(301, 300, "claude", "claude"),
            ps_line(302, 301, "sampan", "/u/.local/bin/sampan mcp serve"),
            ps_line(400, 100, "zsh", "-zsh"), ps_line(401, 400, "codex", "codex"),
            ps_line(402, 401, "sampan", "/u/.local/bin/sampan mcp serve"),
            ps_line(500, 1, "claude", "claude"), ps_line(501, 500, "sampan", "sampan mcp serve"),  # not in tmux
            ps_line(600, 100, "zsh", "-zsh"), ps_line(601, 600, "sampan", "sampan mcp install"),
            ps_line(602, 600, "less", "less sampan mcp serve.txt"),
        ])
        r = FakeRunner(strict=True).on(["ps", "-A"], cp(out=procs + "\n"))
        r.on(["ps", "-o", "lstart="], lambda a: cp(out=starts.get(int(a[-1]), "")))
        from tests.fakes import pane_line
        lines = panes if panes is not None else [pane_line("alpha-pm", "%1", 200, "claude", "/w"),
                                                 pane_line("beta-dev", "%2", 300, "claude", "/w"),
                                                 pane_line("gamma-cx", "%3", 400, "codex", "/w")]
        r.on(["tmux", "-u", "list-panes"], cp(out="".join(line + "\n" for line in lines)))
        return r

    def test_update_lists_sessions_still_on_old_servers(self):
        starts = {202: "Thu Nov 13 08:10:00 2026", 302: "Thu Nov 13 09:01:00 2026",  # beta-dev restarted after
                  402: "Wed Nov 12 23:00:00 2026", 501: "Thu Nov 13 08:00:00 2026"}
        r = self.runner(starts)
        self.assertEqual(update.running_0_16_servers(r, installed_at=self.INSTALLED_AT), ["alpha-pm", "gamma-cx"])
        asked = sorted(int(c["args"][-1]) for c in r.called(["ps", "-o", "lstart="]))
        self.assertEqual(asked, [202, 302, 402])  # only servers in a tmux pane are looked at

    def test_a_server_whose_start_cannot_be_read_is_not_listed(self):
        r = self.runner({302: "Thu Nov 13 08:00:00 2026"})
        self.assertEqual(update.running_0_16_servers(r, installed_at=self.INSTALLED_AT), ["beta-dev"])

    def test_no_install_time_or_no_tmux_lists_nothing(self):
        starts = {202: "Thu Nov 13 08:10:00 2026"}
        self.assertEqual(update.running_0_16_servers(self.runner(starts), installed_at=None), [])
        r = self.runner(starts)
        r.on(["tmux", "-u", "list-panes"], cp(rc=1, err="error connecting to /tmp/x (Permission denied)"))
        self.assertEqual(update.running_0_16_servers(r, installed_at=self.INSTALLED_AT), [])

    def test_installed_at_defaults_to_the_installed_package(self):
        import sampan
        self.assertEqual(update.installed_at(), Path(sampan.__file__).stat().st_mtime)

    def test_notice_text(self):
        self.assertEqual(update.restart_notice(["alpha-pm", "beta-dev"]),
                         ["這些 session 要重開才會用新版：alpha-pm、beta-dev（在 sampan 畫面按 S）。",
                          "重開之前，它們用 sampan 送訊息會失敗；收件端會先用貼全文。"])
        self.assertEqual(update.restart_notice([]), [])

    def test_after_update_notices_name_sessions_and_stale_codex_tables(self):
        from tests.helpers import make_env
        cfg = make_env(Path(tempfile.mkdtemp()))
        cfg.codex.home.mkdir(parents=True, exist_ok=True)
        old = '[mcp_servers.sampan]\ncommand = "sampan"\n\n[mcp_servers.sampan.tools.send]\napproval_mode = "prompt"\n'
        (cfg.codex.home / "config.toml").write_text(old)
        r = self.runner({202: "Thu Nov 13 08:10:00 2026"})
        lines = update.after_update_notices(r, cfg, installed_at=self.INSTALLED_AT)
        self.assertEqual(lines, ["這些 session 要重開才會用新版：alpha-pm（在 sampan 畫面按 S）。",
                                 "重開之前，它們用 sampan 送訊息會失敗；收件端會先用貼全文。",
                                 "Codex 的 sampan 工具表要更新：跑 sampan mcp install 補上"])
        from sampan import codex_approval
        (cfg.codex.home / "config.toml").write_text(codex_approval.with_prompt(old))
        self.assertEqual(update.after_update_notices(self.runner({}), cfg, installed_at=self.INSTALLED_AT), [])
        self.assertEqual(update.after_update_notices(self.runner({}), None, installed_at=self.INSTALLED_AT), [])


class PendingNoticeTest(unittest.TestCase):
    """The first 0.17 command after an upgrade from 0.16 (whose `sampan update` ran the old code) shows the notices
    once: the migration leaves NOTICE_MARKER, show_pending_notices prints and removes it."""

    INSTALLED_AT = RestartNoticeTest.INSTALLED_AT
    runner = RestartNoticeTest.runner

    def setUp(self):
        from tests.helpers import make_env
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.marker = self.cfg.state_dir / update.NOTICE_MARKER
        self.msgs = []

    def show(self, run):
        update.show_pending_notices(self.cfg, run, self.msgs.append, installed_at=self.INSTALLED_AT)

    def test_mark_then_show_once(self):
        update.mark_upgrade_notice(self.cfg.state_dir)
        self.assertTrue(self.marker.exists())
        self.show(self.runner({202: "Thu Nov 13 08:10:00 2026"}))
        self.assertEqual(self.msgs, ["這些 session 要重開才會用新版：alpha-pm（在 sampan 畫面按 S）。",
                                     "重開之前，它們用 sampan 送訊息會失敗；收件端會先用貼全文。"])
        self.assertFalse(self.marker.exists())
        self.show(FakeRunner(strict=True))  # no marker: nothing is looked at
        self.assertEqual(len(self.msgs), 2)

    def test_codex_tables_out_of_date_are_said_too(self):
        self.cfg.codex.home.mkdir(parents=True, exist_ok=True)
        (self.cfg.codex.home / "config.toml").write_text(
            '[mcp_servers.sampan]\ncommand = "sampan"\n\n[mcp_servers.sampan.tools.send]\napproval_mode = "prompt"\n')
        update.mark_upgrade_notice(self.cfg.state_dir)
        self.show(self.runner({}))
        self.assertEqual(self.msgs, ["Codex 的 sampan 工具表要更新：跑 sampan mcp install 補上"])

    def test_nothing_to_say_is_quiet_and_clears_the_marker(self):
        update.mark_upgrade_notice(self.cfg.state_dir)
        self.show(self.runner({}))
        self.assertEqual(self.msgs, [])
        self.assertFalse(self.marker.exists())

    def test_a_failure_is_quiet_and_keeps_the_marker(self):
        update.mark_upgrade_notice(self.cfg.state_dir)
        r = FakeRunner(strict=True)  # ps has no rule: the lookup raises
        self.show(r)
        self.assertEqual(self.msgs, [])
        self.assertTrue(self.marker.exists())  # tried again next time

    def test_which_commands_show_it(self):
        from sampan import cli
        parse = cli.build_parser().parse_args
        for argv, shown in ((["status"], True), (["save"], True), (["save", "--quiet"], False),
                            (["mcp", "serve"], False), (["mcp", "install"], True), (["dispatches"], True)):
            with self.subTest(argv=argv):
                self.assertEqual(cli._shows_upgrade_notice(parse(argv)), shown)

    def test_cli_shows_it_after_migrate(self):
        from sampan import cli
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.migrate.ensure", return_value=0), \
                mock.patch("sampan.update.show_pending_notices") as show, \
                mock.patch("sampan.status.cmd_status", return_value=0):
            cli.main(["status"])
        self.assertEqual(show.call_args.args[0], self.cfg)
        with mock.patch("sampan.cli.load_config", return_value=self.cfg), \
                mock.patch("sampan.migrate.ensure", return_value=2), \
                mock.patch("sampan.update.show_pending_notices") as show:
            cli.main(["status"])
        show.assert_not_called()  # the migration failed: the command stops before


if __name__ == "__main__":
    unittest.main()
