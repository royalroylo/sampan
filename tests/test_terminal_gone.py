"""The sampan screen leaves when its terminal is gone, instead of spinning on a read that fails at once forever (a
closed terminal whose SIGHUP never came: nohup, or started by something that ignores SIGHUP)."""
import os
import pty
import signal
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from sampan.ui import screen as screen_mod
from sampan.ui.screen import CursesScreen, TerminalGone, curses_ui, terminal_gone
from tests.fakes import FakeRunner
from tests.helpers import make_env
from tests.test_ui_screen import TICK, FakeScr

ROOT = Path(__file__).resolve().parent.parent


def _set_size(fd, rows=40, cols=120):
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class TerminalGoneProbeTest(unittest.TestCase):
    def test_live_terminal_is_not_gone_and_a_waiting_key_is_left_alone(self):
        import tty
        master, slave = pty.openpty()
        tty.setraw(slave)  # as curses has it: a key is there at once, no line to wait for
        try:
            self.assertFalse(terminal_gone(slave))
            os.write(master, b"q")
            time.sleep(0.05)
            self.assertFalse(terminal_gone(slave))
            self.assertEqual(os.read(slave, 1), b"q")  # the probe read nothing
        finally:
            os.close(master)
            os.close(slave)

    def test_closed_terminal_is_gone(self):
        master, slave = pty.openpty()
        try:
            os.close(master)
            self.assertTrue(terminal_gone(slave))
        finally:
            os.close(slave)

    def test_not_a_terminal_is_never_gone(self):
        r, w = os.pipe()
        os.close(w)  # a pipe at end of file polls readable with nothing in it: still not "a terminal that is gone"
        try:
            self.assertFalse(terminal_gone(r))
        finally:
            os.close(r)


class ReadTest(unittest.TestCase):
    def test_a_failed_read_on_a_gone_terminal_raises(self):
        s = CursesScreen(FakeScr([TICK]), tty_gone=lambda: True)
        with self.assertRaises(TerminalGone):
            s.key()

    def test_a_timeout_on_a_live_terminal_is_no_key(self):
        s = CursesScreen(FakeScr([TICK]), tty_gone=lambda: False)
        self.assertIsNone(s.key())

    def test_gone_is_not_caught_as_an_ordinary_error(self):
        self.assertFalse(issubclass(TerminalGone, Exception))


class CursesUiGoneTest(unittest.TestCase):
    def test_leaves_quietly_with_rc_0(self):
        cfg = make_env(Path(tempfile.mkdtemp()))
        printed, execs = [], []

        class Watcher:
            items = []

            def start(self):
                pass

            def stop(self, notice=None):
                self.notice = notice
        dw = Watcher()
        with mock.patch.object(screen_mod, "terminal_gone", lambda: True):
            rc = curses_ui(cfg, FakeRunner(), printed.append, wrapper=lambda fn: fn(FakeScr([TICK])),
                           rows_fn=lambda c, run: [], dispatch_watcher=dw, exec_fn=lambda *a: execs.append(a))
        self.assertEqual((rc, printed, execs), (0, [], []))
        self.assertIsNone(dw.notice)  # nothing to print to

    def test_gone_found_anywhere_in_the_chain(self):
        try:
            try:
                try:
                    raise TerminalGone()
                except TerminalGone:
                    raise OSError("endwin")
            except OSError as e:
                raise ValueError("cleanup") from e
        except ValueError as e:
            self.assertTrue(screen_mod._caused_by_gone(e))
        self.assertFalse(screen_mod._caused_by_gone(ValueError("plain")))

    def test_closing_curses_failing_after_it_still_leaves_quietly(self):
        import curses
        cfg = make_env(Path(tempfile.mkdtemp()))

        def wrapper(fn):
            try:
                return fn(FakeScr([TICK]))
            finally:
                raise curses.error("endwin")
        with mock.patch.object(screen_mod, "terminal_gone", lambda: True):
            rc = curses_ui(cfg, FakeRunner(), lambda _l: None, wrapper=wrapper, rows_fn=lambda c, run: [])
        self.assertEqual(rc, 0)


CHILD = textwrap.dedent("""
    import signal, sys, tempfile
    from pathlib import Path
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # the case that used to spin: no SIGHUP ends the process
    from sampan.ui.screen import curses_ui
    from tests.fakes import FakeRunner
    from tests.helpers import make_env
    cfg = make_env(Path(tempfile.mkdtemp()))
    rc = curses_ui(cfg, FakeRunner(), print, rows_fn=lambda c, r: [])
    open(sys.argv[1], "w").write(str(rc))
""")


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "needs a pty")
class RealTerminalGoneTest(unittest.TestCase):
    def test_screen_ends_within_2s_after_its_terminal_closes_with_sighup_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        done = tmp / "rc"
        master, slave = pty.openpty()
        _set_size(slave)
        env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
                   HOME=str(tmp))
        p = subprocess.Popen([sys.executable, "-c", CHILD, str(done)], stdin=slave, stdout=slave, stderr=slave,
                             env=env, cwd=str(ROOT), start_new_session=True)
        os.close(slave)
        try:
            out, start = b"", time.monotonic()
            while time.monotonic() - start < 10 and b"\x1b[" not in out:  # the screen is up (curses wrote to it)
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break
            self.assertIsNone(p.poll(), out[-500:])
            time.sleep(0.5)
            os.close(master)
            master = None
            closed = time.monotonic()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("the screen kept running after its terminal closed")
            self.assertLess(time.monotonic() - closed, 2.0)
            self.assertEqual(done.read_text(), "0")
        finally:
            if master is not None:
                os.close(master)
            if p.poll() is None:
                p.send_signal(signal.SIGKILL)
                p.wait()


if __name__ == "__main__":
    unittest.main()
