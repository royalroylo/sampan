import stat
import tempfile
import unittest
from pathlib import Path

from sampan import machine


def host(name):
    return lambda: name


class MachineTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cfg = self.d / "config.toml"

    def test_path_next_to_config(self):
        self.assertEqual(machine.path(self.cfg), self.d / "machine.toml")

    def test_machine_created_once(self):
        m1 = machine.load_or_create(self.cfg, hostname=host("box.local"))
        m2 = machine.load_or_create(self.cfg, hostname=host("other"))
        self.assertTrue(m1.id)
        self.assertEqual(m1, m2)
        self.assertEqual(stat.S_IMODE(machine.path(self.cfg).stat().st_mode), 0o600)

    def test_machine_name_defaults_to_hostname_without_local(self):
        self.assertEqual(machine.load_or_create(self.cfg, hostname=host("box.local")).name, "box")

    def test_machine_broken_file_is_unknown_not_an_error(self):
        machine.path(self.cfg).write_text("this is = = not toml")
        self.assertEqual(machine.load_or_create(self.cfg, hostname=host("box.local")), machine.Machine("", "box"))

    def test_machine_unwritable_dir_is_unknown(self):
        blocker = self.d / "file"
        blocker.write_text("x")
        self.assertEqual(machine.load_or_create(blocker / "sub" / "config.toml", hostname=host("box")),
                         machine.Machine("", "box"))

    def test_machine_rename_keeps_id(self):
        m = machine.load_or_create(self.cfg, hostname=host("box"))
        r = machine.rename(self.cfg, 'mac "mini"')
        self.assertEqual(r, machine.Machine(m.id, 'mac "mini"'))
        self.assertEqual(machine.load_or_create(self.cfg, hostname=host("x")), r)

    def test_machine_rename_rejects_empty(self):
        self.assertIsInstance(machine.rename(self.cfg, ""), str)
        self.assertIsInstance(machine.rename(self.cfg, "a\nb"), str)

    def test_ref(self):
        self.assertEqual(machine.ref("m", "s"), "m:s")
