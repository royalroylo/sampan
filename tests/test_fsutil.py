import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sampan.fsutil import atomic_write


class ReverseLinesTests(unittest.TestCase):
    def test_lines_newest_first_across_small_chunks(self):
        from sampan.fsutil import reverse_lines
        p = Path(tempfile.mkdtemp()) / "t.jsonl"
        lines = [f"第 {i} 行 " + "x" * (i % 7) for i in range(200)]
        p.write_text("\n".join(lines) + "\n")
        self.assertEqual(list(reverse_lines(p, chunk=37)), lines[::-1])

    def test_no_trailing_newline_and_empty(self):
        from sampan.fsutil import reverse_lines
        d = Path(tempfile.mkdtemp())
        (d / "a").write_text("a\nb")
        (d / "e").write_text("")
        self.assertEqual(list(reverse_lines(d / "a", chunk=1)), ["b", "a"])
        self.assertEqual(list(reverse_lines(d / "e")), [])

    def test_multibyte_split_at_chunk_edge(self):
        from sampan.fsutil import reverse_lines
        p = Path(tempfile.mkdtemp()) / "m"
        p.write_text("中文一\n中文二\n")
        for chunk in range(1, 12):
            self.assertEqual(list(reverse_lines(p, chunk=chunk)), ["中文二", "中文一"])


class AtomicWriteTests(unittest.TestCase):
    def test_encoding_is_used_when_given(self):
        path = Path(tempfile.mkdtemp()) / "f"
        with mock.patch("os.fdopen", wraps=os.fdopen) as fd:
            atomic_write(path, "中\r\n", encoding="utf-8")
        self.assertEqual(fd.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(path.read_bytes(), "中\r\n".encode())
        with mock.patch("os.fdopen", wraps=os.fdopen) as fd:
            atomic_write(path, "x")
        self.assertIsNone(fd.call_args.kwargs.get("encoding"))  # other callers: unchanged (locale default)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_creates_parents_and_file(self):
        path = self.tmp / "a" / "b" / "f.txt"
        self.assertEqual(atomic_write(path, "hi\n"), path)
        self.assertEqual(path.read_text(), "hi\n")

    def test_keeps_mode_and_writes_through_symlink(self):
        real = self.tmp / "real.toml"
        real.write_text("old")
        os.chmod(real, 0o600)
        link = self.tmp / "link.toml"
        link.symlink_to(real)
        atomic_write(link, "new")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "new")
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)

    def test_dangling_symlink_creates_the_target(self):
        target = self.tmp / "dotfiles" / "settings.json"
        link = self.tmp / "settings.json"
        link.symlink_to(target)
        atomic_write(link, "{}")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "{}")

    def test_failed_replace_leaves_old_file_and_no_temp(self):
        path = self.tmp / "f.txt"
        path.write_text("old")
        with mock.patch("sampan.fsutil.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write(path, "new")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["f.txt"])


if __name__ == "__main__":
    unittest.main()
