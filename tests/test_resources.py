import json
import tempfile
import unittest
from pathlib import Path

from sampan import resources
from sampan.config import Limits
from tests.fakes import FakeRunner, cp

RAM = 17_179_869_184
PAGE = 16384

VM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               3000.
Pages occupied by compressor:           {comp}.
Swapouts:                               {outs}.
Swapins:                                  500.
"""


def sysctl_out(swap_used="4063.12M", level=42, boot=1_700_000_000):
    return (f"vm.swapusage: total = 7168.00M  used = {swap_used}  free = 3104.88M  (encrypted)\n{level}\n{RAM}\n10\n"
            f"{{ sec = {boot}, usec = 0 }} Thu Nov 14 22:13:20 2023\n")


def runner(comp=406_693, outs=0, **kw):
    r = FakeRunner(strict=True)
    # sysctl -n prints the bare values (no key), one per line
    out = sysctl_out(**kw).replace("vm.swapusage: ", "")
    r.on(["sysctl"], cp(out=out))
    r.on(["vm_stat"], cp(out=VM.format(comp=comp, outs=outs)))
    return r


LIM = Limits()


class Parse(unittest.TestCase):
    def test_vm_stat(self):
        self.assertEqual(resources.parse_vm_stat(VM.format(comp=406693, outs=7)), (16384, 406693, 7))
        self.assertEqual(resources.parse_vm_stat("garbage"), (None, None, None))

    def test_swapusage_mib(self):
        total, used = resources.parse_swapusage("total = 7168.00M  used = 4063.12M  free = 3104.88M  (encrypted)")
        self.assertEqual(total, 7168 << 20)
        self.assertAlmostEqual(used / RAM, 0.248, places=3)
        self.assertEqual(resources.parse_swapusage(""), (None, None))

    def test_boottime(self):
        self.assertEqual(resources.parse_boottime("{ sec = 1700000000, usec = 5 } Thu"), 1_700_000_000.0)
        self.assertIsNone(resources.parse_boottime("x"))

    def test_linux(self):
        root = Path(tempfile.mkdtemp())
        (root / "meminfo").write_text("MemTotal:   16000000 kB\nSwapTotal: 4000000 kB\nSwapFree: 3000000 kB\n")
        (root / "vmstat").write_text("pswpout 123\n")
        (root / "stat").write_text("btime 1700000000\n")
        f = resources.read_raw(FakeRunner(strict=True), platform="linux", proc_root=root, load=lambda: (0, 1.5, 0), ncpu=4)
        self.assertEqual((f["ram"], f["swap_used"], f["swapouts"], f["boot"], f["load5"], f["cores"]),
                         (16000000 << 10, 1000000 << 10, 123, 1_700_000_000.0, 1.5, 4))
        self.assertIsNone(f.get("compressed"))


class Units(unittest.TestCase):
    def snap(self, **kw):
        d = tempfile.mkdtemp()
        return resources.snapshot(runner(**kw), d, LIM, now=1000.0, platform="darwin", load=lambda: (0, 3.86, 0), ncpu=10)

    def test_0822_reading(self):
        s = self.snap()
        self.assertAlmostEqual(s.compressor_pct, 0.3879, places=4)
        self.assertAlmostEqual(s.swap_pct, 0.2480, places=3)
        self.assertEqual(resources.classify(s, LIM).memory, "yellow")

    def test_0853_reading(self):
        s = self.snap(comp=164_313)
        self.assertAlmostEqual(s.compressor_pct, 0.1567, places=4)


class Rate(unittest.TestCase):
    def sample(self, d, outs, now, comp=700_000, swap="3599M", boot=1_700_000_000, **kw):
        return resources.snapshot(runner(comp=comp, outs=outs, swap_used=swap, boot=boot), d, LIM, now=now,
                                  platform="darwin", load=lambda: (0, 1, 0), ncpu=10, **kw)

    def test_no_previous_sample_is_unknown(self):
        d = tempfile.mkdtemp()
        self.assertIsNone(self.sample(d, 100, 1000.0).rate)

    def test_rate_and_red(self):
        d = tempfile.mkdtemp()
        self.sample(d, 100, 1000.0)
        s = self.sample(d, 100 + 2000, 1060.0)  # 2000 pages in a minute, compressor 42.7% = high
        self.assertAlmostEqual(s.rate, 2000)
        c = resources.classify(s, LIM)
        self.assertTrue(c.red and c.swapping)
        self.assertEqual(c.memory, "red")

    def test_calm_swapins_only_is_yellow(self):
        d = tempfile.mkdtemp()
        self.sample(d, 100, 1000.0)
        s = self.sample(d, 100, 1060.0)
        c = resources.classify(s, LIM)
        self.assertEqual((c.swapping, c.red, c.memory), (False, False, "yellow"))

    def test_at_threshold_is_not_over(self):
        d = tempfile.mkdtemp()
        self.sample(d, 100, 1000.0)
        s = self.sample(d, 1100, 1060.0)  # exactly 1000 per minute
        self.assertFalse(resources.classify(s, LIM).swapping)

    def test_unknown_cases(self):
        for name, first, second in (("counter down", (500, 1000.0, 1_700_000_000), (100, 1060.0, 1_700_000_000)),
                                    ("clock back", (100, 1000.0, 1_700_000_000), (900, 900.0, 1_700_000_000)),
                                    ("too old", (100, 1000.0, 1_700_000_000), (900, 1700.0, 1_700_000_000)),
                                    ("reboot", (100, 1000.0, 1_700_000_000), (900, 1060.0, 1_700_100_000))):
            with self.subTest(name):
                d = tempfile.mkdtemp()
                self.sample(d, first[0], first[1], boot=first[2])
                self.assertIsNone(self.sample(d, second[0], second[1], boot=second[2]).rate)

    def test_too_young_uses_the_one_before(self):
        d = tempfile.mkdtemp()
        self.sample(d, 100, 1000.0)
        self.sample(d, 100, 1035.0)  # written (>= 30 s after the first)
        s = self.sample(d, 3100, 1040.0)  # 5 s after the newest: compare with the one 40 s back
        self.assertAlmostEqual(s.rate, 3000 * 60 / 40)

    def test_state_file_written_at_most_every_30s(self):
        d = tempfile.mkdtemp()
        self.sample(d, 100, 1000.0)
        t0 = (Path(d) / "resources.json").stat().st_mtime
        self.sample(d, 100, 1001.0)
        data = json.loads((Path(d) / "resources.json").read_text())
        self.assertEqual(len(data["samples"]), 1)
        self.assertEqual((Path(d) / "resources.json").stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual((Path(d) / "resources.json").stat().st_mtime, t0)

    def test_corrupt_state_file(self):
        d = tempfile.mkdtemp()
        (Path(d) / "resources.json").write_text("{nope")
        self.assertIsNone(self.sample(d, 100, 1000.0).rate)


class Hysteresis(unittest.TestCase):
    def snap(self, d, outs, now, comp, swap="3599M"):
        return resources.snapshot(runner(comp=comp, outs=outs, swap_used=swap), d, LIM, now=now, platform="darwin",
                                  load=lambda: (0, 1, 0), ncpu=10)

    def test_hold_ten_minutes(self):
        d = tempfile.mkdtemp()
        self.snap(d, 0, 1000.0, 700_000)
        self.assertTrue(resources.classify(self.snap(d, 5000, 1060.0, 700_000), LIM).red)  # written out
        for later, red in ((1060 + 599, True), (1060 + 601, False)):
            s = self.snap(d, 5000, float(later), 700_000)
            self.assertEqual(resources.classify(s, LIM).red, red, later)

    def test_hold_ends_when_not_high(self):
        d = tempfile.mkdtemp()
        self.snap(d, 0, 1000.0, 700_000)
        self.snap(d, 5000, 1060.0, 700_000)
        self.assertFalse(resources.classify(self.snap(d, 5000, 1100.0, 100_000), LIM).red)

    def test_plateau_both_high_is_red_without_swapping(self):
        d = tempfile.mkdtemp()
        s = self.snap(d, 0, 1000.0, 700_000, swap="8400M")  # 49% of RAM
        self.assertTrue(resources.classify(s, LIM).red)

    def test_only_one_high_no_history_is_yellow(self):
        d = tempfile.mkdtemp()
        s = self.snap(d, 0, 1000.0, 700_000)
        self.assertEqual(resources.classify(s, LIM).memory, "yellow")

    def test_boot_change_voids_history(self):
        d = tempfile.mkdtemp()
        self.snap(d, 0, 1000.0, 700_000)
        self.snap(d, 5000, 1060.0, 700_000)
        s = resources.snapshot(runner(comp=700_000, outs=0, boot=1_700_500_000), d, LIM, now=1100.0, platform="darwin",
                               load=lambda: (0, 1, 0), ncpu=10)
        self.assertIsNone(s.last_red_at)

    def test_level_below_15_is_red(self):
        s = resources.snapshot(runner(level=10), tempfile.mkdtemp(), LIM, now=1.0, platform="darwin",
                               load=lambda: (0, 1, 0), ncpu=10)
        self.assertTrue(resources.classify(s, LIM).red)

    def test_load_never_in_light(self):
        s = resources.snapshot(runner(comp=100_000), tempfile.mkdtemp(), LIM, now=1.0, platform="darwin",
                               load=lambda: (0, 50, 0), ncpu=10)
        c = resources.classify(s, LIM)
        self.assertEqual((c.load, c.memory), ("red", "green"))

    def test_unreadable_is_unknown_not_red(self):
        s = resources.snapshot(FakeRunner(), tempfile.mkdtemp(), LIM, now=1.0, platform="darwin", load=lambda: (0, 1, 0),
                               ncpu=10)
        c = resources.classify(s, LIM)
        self.assertEqual((c.red, c.memory), (False, "unknown"))


class Probe(unittest.TestCase):
    def test_probe_only_when_high_and_no_history(self):
        sleeps = []
        r = FakeRunner(strict=True)
        seq = [cp(out=sysctl_out().replace("vm.swapusage: ", "")), cp(out=VM.format(comp=700_000, outs=0))]
        seq2 = [cp(out=sysctl_out().replace("vm.swapusage: ", "")), cp(out=VM.format(comp=700_000, outs=500))]
        answers = iter(seq + seq2)
        r.on(["sysctl"], lambda a: next(answers))
        r.on(["vm_stat"], lambda a: next(answers))
        s = resources.snapshot(r, tempfile.mkdtemp(), LIM, now=1000.0, platform="darwin", load=lambda: (0, 1, 0),
                               ncpu=10, probe=True, sleep=sleeps.append)
        self.assertEqual(sleeps, [5])
        self.assertAlmostEqual(s.rate, 500 * 12)

    def test_no_probe_when_not_high(self):
        sleeps = []
        resources.snapshot(runner(comp=100_000), tempfile.mkdtemp(), LIM, now=1000.0, platform="darwin",
                           load=lambda: (0, 1, 0), ncpu=10, probe=True, sleep=sleeps.append)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
