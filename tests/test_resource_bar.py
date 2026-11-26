import dataclasses
import importlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sampan import resources
from sampan.config import Limits
from sampan.textwidth import text_width
from sampan.ui import resource_bar as rb
from tests.fakes import FakeRunner, cp
from tests.helpers import as_macos, make_env
from tests.test_resources import RAM, VM, sysctl_out

ui_render = importlib.import_module("sampan.ui.render")  # the package re-exports a function named render
LIM = Limits()


def snap(comp=0.39, swap_gib=3.6, load=3.6, level=42, rate=0.0, **kw):
    return resources.Snapshot(at=1.0, ram=RAM, swap_used=int(swap_gib * 2 ** 30), swap_total=7 << 30,
                              compressed=int(comp * RAM), level=level, load5=load, cores=10, rate=rate, **kw)


def reading(**kw):
    s = snap(**kw)
    return rb.Reading(1.0, s, resources.classify(s, LIM))


def text(segs):
    return rb.gauge_text(segs)


def setUpModule():
    as_macos()


class Gauge(unittest.TestCase):
    def test_widths_tiers_and_cjk(self):
        r = reading()
        want = {120: "◆ !壓縮 39% │ swap 3.6G │ 負載 3.6 │ 執行中 21/16!", 100: None, 99: "◆ !壓39% S3.6G L3.6 21/16!",
                70: None, 69: "◆ 21/16!", 50: None, 49: ""}
        for w, expect in want.items():
            g = text(rb.gauge(r, 21, 16, w, now=2.0))
            if expect is not None:
                self.assertEqual(g, expect, w)
            self.assertLessEqual(text_width(g), w)
        self.assertEqual(text(rb.gauge(r, 21, 16, 100, now=2.0)), want[120])
        self.assertEqual(text(rb.gauge(r, 21, 16, 70, now=2.0)), want[99])
        self.assertEqual(text(rb.gauge(r, 21, 16, 50, now=2.0)), want[69])

    def styles(self, r, running=3, limit=16, width=120):
        return {t.strip(" │"): s for t, s in rb.gauge(r, running, limit, width, now=2.0) if t.strip(" │")}

    def test_green_yellow_red(self):
        green = self.styles(reading(comp=0.2, swap_gib=1.0, load=1.0, level=60))
        self.assertEqual((green["壓縮 20%"], green["swap 1.0G"], green["負載 1.0"]), ("", "", ""))
        self.assertEqual(green["◆"], "ok")
        yellow = self.styles(reading(comp=0.35))
        self.assertEqual((yellow["◆"], yellow["!壓縮 35%"]), ("warn", "warn"))
        red = self.styles(reading(comp=0.7, swap_gib=9.0))  # both high: the red light
        self.assertEqual((red["◆"], red["!壓縮 70%"], red["!swap 9.0G"]), ("err", "err", "err"))

    def test_high_alone_is_not_red(self):
        s = self.styles(reading(comp=0.42, swap_gib=1.0))  # 08:22-like: compressor high, not written out
        self.assertEqual((s["◆"], s["!壓縮 42%"]), ("warn", "warn"))

    def test_written_out_makes_it_red(self):
        s = self.styles(reading(comp=0.44, swap_gib=1.0, rate=13000.0))
        self.assertEqual((s["◆"], s["!壓縮 44%"]), ("err", "err"))

    def test_load_is_coloured_but_never_moves_the_light(self):
        s = self.styles(reading(comp=0.2, swap_gib=1.0, load=50.0, level=60))
        self.assertEqual((s["◆"], s["!負載 50.0"]), ("ok", "err"))

    def test_count_over_the_limit_is_warn_and_never_the_light(self):
        s = self.styles(reading(comp=0.2, swap_gib=1.0, level=60), running=23)
        self.assertEqual((s["◆"], s["執行中 23/16!"]), ("ok", "warn"))
        self.assertEqual(self.styles(reading(), running=3)["執行中 3/16"], "")

    def test_no_limit_shows_only_the_number(self):
        self.assertEqual(text(rb.gauge(reading(), 5, None, 120, now=2.0)).endswith("執行中 5"), True)

    def test_old_or_missing_reading_is_a_question_mark(self):
        for r, now in ((reading(), 1.0 + rb.FRESH_S + 1), (None, 2.0)):
            g = rb.gauge(r, 3, 16, 120, now=now)
            self.assertEqual(g[0], ("◆", "muted"))
            self.assertIn("壓縮 ?", text(g))
            self.assertIn("swap ?", text(g))
            self.assertIn("執行中 3/16", text(g))

    def test_every_field_alone_can_be_unknown(self):
        s = dataclasses.replace(snap(), swap_used=None, load5=None)
        r = rb.Reading(1.0, s, resources.classify(s, LIM))
        self.assertIn("swap ?", text(rb.gauge(r, 1, 16, 120, now=2.0)))
        self.assertIn("負載 ?", text(rb.gauge(r, 1, 16, 120, now=2.0)))


class Render(unittest.TestCase):
    def frame(self, gauge, width=120):
        return ui_render.render(title="t", left=[("a",), ("b",)], cursor=0, card=["x"], help="h", width=width, height=20,
                                gauge=gauge)

    def test_only_the_separator_line_changes_and_the_gauge_sits_at_its_right_end(self):
        r = reading()
        plain = ui_render.frame_text(self.frame(None))
        with_g = ui_render.frame_text(self.frame(lambda w: rb.gauge(r, 21, 16, w, now=2.0)))
        diff = [i for i, (a, b) in enumerate(zip(plain, with_g)) if a != b]
        self.assertEqual(len(diff), 1)
        line = with_g[diff[0]]
        self.assertTrue(plain[diff[0]].startswith("─"))
        self.assertTrue(line.rstrip().endswith("執行中 21/16!"), line)
        self.assertLessEqual(text_width(line), 120)
        self.assertEqual(text_width(line), text_width(plain[diff[0]]))
        self.assertIn("┴", line)  # the card's corner stays

    def test_no_room_no_gauge_and_never_wider_than_the_screen(self):
        r = reading()
        for width in (120, 100, 99, 80, 70, 60, 50, 49, 30):
            lines = ui_render.frame_text(self.frame(lambda w: rb.gauge(r, 21, 16, w, now=2.0), width))
            self.assertTrue(all(text_width(x) <= width for x in lines), width)

    def test_the_gauge_never_covers_the_corner(self):
        r = reading()
        lines = ui_render.frame_text(self.frame(lambda w: rb.gauge(r, 21, 16, w, now=2.0), 100))
        sep = next(x for x in lines if "┴" in x)
        self.assertGreater(sep.find("◆"), sep.find("┴"))


def memory_runner():
    r = FakeRunner(strict=True)
    r.on(["sysctl"], cp(out=sysctl_out().replace("vm.swapusage: ", "")))
    r.on(["vm_stat"], cp(out=VM.format(comp=406_693, outs=0)))
    return r


def counting_runner(claudes):
    """memory_runner, plus one agent in a tmux pane and `claudes` more outside tmux."""
    from tests.fakes import pane_line, ps_line
    r = memory_runner()
    ps = [ps_line(100, 1, "zsh", "-zsh"), ps_line(200, 100, "claude", "claude")]
    ps += [ps_line(300 + i, 1, "claude", "claude --resume x") for i in range(claudes)]
    r.on(["ps", "-A"], cp(out="\n".join(ps) + "\n"))
    r.on(["tmux", "-u", "list-panes"], cp(out=pane_line("a", "%1", 100, "claude", "/c") + "\n"))
    return r


class Watcher(unittest.TestCase):
    def runner(self):
        return memory_runner()

    def watcher(self, **kw):
        cfg = make_env(Path(tempfile.mkdtemp()))
        clock = {"t": 100.0}
        r = self.runner()
        kw.setdefault("report_fn", lambda *a, **k: (_ for _ in ()).throw(OSError("no count in this test")))
        w = rb.ResourceWatcher(cfg, r, clock=lambda: clock["t"], snapshot_fn=kw.pop("snapshot_fn", None) or
                               (lambda run, sd, lim, **k: resources.snapshot(run, sd, lim, platform="darwin",
                                                                              load=lambda: (0, 1, 0), ncpu=10, **k)),
                               **kw)
        return w, r, clock

    def test_a_step_is_two_spawns_and_gives_a_reading(self):
        w, r, _ = self.watcher()
        self.assertTrue(w.step())
        self.assertEqual(len(r.calls), 2)  # the memory reading: two spawns
        self.assertEqual(sorted(c["args"][0] for c in r.calls), ["sysctl", "vm_stat"])
        self.assertTrue(all(c["timeout"] == rb.SPAWN_TIMEOUT_S for c in r.calls))
        self.assertAlmostEqual(w.latest.snap.compressor_pct, 0.3879, places=4)

    def test_the_number_is_limits_count_one_ps_one_tmux_and_includes_agents_outside_tmux(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        r = counting_runner(2)
        w = rb.ResourceWatcher(cfg, r, clock=lambda: 100.0, snapshot_fn=lambda run, sd, lim, **k: resources.snapshot(
            run, sd, lim, platform="darwin", load=lambda: (0, 1, 0), ncpu=10, **k))
        self.assertTrue(w.step())
        self.assertEqual((w.latest.running, w.latest.limit), (3, 16))  # 1 in tmux + 2 outside: what `new` counts
        spawns = sorted(c["args"][0] for c in r.calls)
        self.assertEqual(spawns, ["ps", "sysctl", "tmux", "vm_stat"])  # the memory sample, one ps, one tmux
        self.assertTrue(all(c["timeout"] == rb.SPAWN_TIMEOUT_S for c in r.calls))

    def test_interval_slows_after_five_minutes_without_a_key_and_a_key_brings_it_back(self):
        w, _, clock = self.watcher()
        self.assertEqual(w.interval(), 5)
        clock["t"] += 299
        self.assertEqual(w.interval(), 5)
        clock["t"] += 2
        self.assertEqual(w.interval(), 15)
        w.note_key()
        self.assertEqual(w.interval(), 5)

    def test_paused_reads_nothing_and_resume_counts_as_a_key(self):
        w, r, clock = self.watcher()
        w.pause()
        self.assertFalse(w.step())
        self.assertEqual(r.calls, [])
        clock["t"] += 1000
        w.resume()
        self.assertEqual(w.interval(), 5)
        self.assertTrue(w.step())

    def test_a_failed_reading_keeps_the_last_one(self):
        w, _, _ = self.watcher()
        w.step()
        first = w.latest
        w.snapshot_fn = mock.Mock(side_effect=OSError("x"))
        self.assertFalse(w.step())
        self.assertIs(w.latest, first)

    def test_it_stops_when_the_terminal_is_gone_and_when_told(self):
        w, r, _ = self.watcher(getppid=lambda: 1)
        w.start()
        w._thread.join(2)
        self.assertFalse(w._thread.is_alive())
        self.assertEqual(r.calls, [])  # not even one reading
        w2, _, _ = self.watcher(getppid=lambda: 4242)
        w2.start()
        w2.stop()
        w2._thread.join(2)
        self.assertFalse(w2._thread.is_alive())

    def test_off_means_no_thread(self):
        cfg = dataclasses.replace(make_env(Path(tempfile.mkdtemp())), resource_bar=False)
        r = self.runner()
        w = rb.ResourceWatcher(cfg, r)
        w.start()
        self.assertIsNone(w._thread)
        self.assertEqual(r.calls, [])
        self.assertEqual(rb.plain_line_now(cfg, r, []), "")
        self.assertEqual(r.calls, [])

    def test_threads_are_daemons(self):
        w, _, _ = self.watcher(getppid=lambda: 4242)
        w.start()
        self.assertTrue(w._thread.daemon)
        w.stop()
        self.assertEqual(threading.active_count() >= 1, True)


class Shared(unittest.TestCase):
    def test_plain_line_uses_the_same_function(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        r = counting_runner(1)
        line = rb.plain_line_now(cfg, r, [])
        self.assertIn("壓縮 39%", line)
        self.assertIn("執行中 2/16", line)  # the rows are not what is counted: limits.count is

    def test_running_and_limit(self):
        from sampan.status import State
        from tests.helpers import status_row
        rows = [status_row("a"), status_row("b", State.STOPPED), status_row("c")]
        self.assertEqual(rb.running_of(rows), 2)
        cfg = make_env(Path(tempfile.mkdtemp()))
        self.assertEqual(rb.limit_of(cfg, reading()), 16)


if __name__ == "__main__":
    unittest.main()
