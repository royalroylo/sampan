import unittest
from types import SimpleNamespace as NS

from sampan.plugin_view import ProfileChoice
from sampan.ui.plugin_state import CatalogPicker, PluginPicker, ProfileToggles


def rows(*names):
    return [NS(name=n) for n in names]


def feed(obj, keys):
    out = None
    for k in keys:
        out = obj.key(k)
    return out


class PluginPickerTest(unittest.TestCase):
    def mk(self):
        return PluginPicker(rows("context-tools@x", "security-guidance@y", "other@z"))

    def test_plugin_picker_keys_and_search_enter_opens(self):
        p = self.mk()
        self.assertEqual(p.key("q"), ("back", []))
        self.assertEqual(p.key("\x1b"), ("back", []))
        self.assertEqual(p.key("\n"), ("open", ["context-tools@x"]))
        self.assertEqual(p.key("u"), ("update", ["context-tools@x"]))
        self.assertEqual(p.key("U"), ("update_all", []))
        self.assertEqual(p.key("a"), ("add", []))
        self.assertEqual(p.key("r"), ("check", []))
        for k in ("n", "R", "w", "s", " ", "z"):
            self.assertIsNone(p.key(k))
        p.key("KEY_DOWN")
        self.assertEqual(p.key("KEY_ENTER"), ("open", ["security-guidance@y"]))
        p = self.mk()
        for k in "/sec":
            self.assertIsNone(p.key(k))
        self.assertIn("/sec", p.prompt)
        self.assertEqual(p.key("\n"), ("open", ["security-guidance@y"]))
        self.assertIsNone(p.query)
        # inside a search q is text, not back
        p = self.mk()
        p.key("/")
        self.assertIsNone(p.key("q"))
        self.assertEqual(p.query, "q")

    def test_plugin_picker_digits_jump(self):
        p = self.mk()
        p.key("3")
        self.assertEqual(p.cursor, 2)


class CatalogPickerTest(unittest.TestCase):
    def test_catalog_picker_pick_type_back(self):
        ents = [NS(pid="a@m", description="", source="s"), NS(pid="b@m", description="", source="s")]
        p = CatalogPicker(ents)
        self.assertEqual([r.name for r in p.rows], ["a@m", "b@m"])
        self.assertEqual(p.key("KEY_DOWN"), None)
        self.assertEqual(p.key("\n"), ("pick", ["b@m"]))
        self.assertEqual(p.key("i"), ("type", []))
        self.assertEqual(p.key("q"), ("back", []))
        self.assertEqual(p.key("\x1b"), ("back", []))
        self.assertIsNone(p.key("u"))
        self.assertIsNone(p.key(" "))
        p.key("/")
        for k in "a@":
            p.key(k)
        self.assertEqual(p.key("\n"), ("pick", ["a@m"]))
        self.assertEqual(p.visible[0].entry.pid, "a@m")


def C(profile, installed, listed, disabled=None):
    return ProfileChoice(profile, profile.upper(), installed, listed, disabled)


class ProfileTogglesTest(unittest.TestCase):
    def test_toggles_space_skips_disabled_notes_and_marks(self):
        a, b, c, d = C("a", "1", True), C("b", None, False), C("c", None, False, "目錄不存在"), C("d", "1", False)
        t = ProfileToggles([a, b, c, d])
        self.assertEqual(t.checked, {"a": True, "b": False, "c": False, "d": True})
        self.assertEqual(t.initial, t.checked)
        self.assertFalse(t.dirty())
        self.assertEqual(t.mark(a), "[x]")
        self.assertEqual(t.mark(b), "[ ]")
        self.assertEqual(t.mark(c), "[-]")
        self.assertEqual(t.mark(d), "[x]!")
        self.assertIsNone(t.note(a))
        self.assertEqual(t.note(c), "目錄不存在")
        self.assertEqual(t.note(d), "已裝但清單沒列")
        t.key(" ")  # a off, cursor -> b
        self.assertEqual((t.checked["a"], t.cursor), (False, 1))
        self.assertEqual(t.note(a), "← 儲存時會移除")
        t.key(" ")  # b on
        self.assertEqual(t.note(b), "← 儲存時會安裝")
        t.key(" ")  # c disabled: no toggle; still moves down
        self.assertFalse(t.checked["c"])
        self.assertTrue(t.dirty())
        self.assertEqual(t.key("KEY_UP"), None)
        self.assertEqual(t.cursor, 2)
        self.assertEqual(t.key("s"), ("save", None))

    def test_toggles_custom_notes_and_mismatch_fn(self):
        b, d = C("b", None, False), C("d", "1", False)
        t = ProfileToggles([b, d], notes=("加", "除"), mismatch_fn=lambda c: None)
        t.key(" ")
        t.key(" ")
        self.assertEqual((t.note(b), t.note(d)), ("加", "除"))
        self.assertEqual(ProfileToggles([d], mismatch_fn=lambda c: None).mark(d), "[x]")

    def test_toggles_discard_prompt(self):
        t = ProfileToggles([C("a", None, False)])
        self.assertEqual(t.key("\x1b"), ("back", None))
        t = ProfileToggles([C("a", None, False)])
        t.key(" ")
        self.assertIsNone(t.key("\x1b"))
        self.assertEqual(t.prompt, "放棄變更？(y/N)")
        self.assertIsNone(t.key("n"))
        self.assertIsNone(t.prompt)
        self.assertIsNone(t.key("q"))
        self.assertEqual(t.key("y"), ("back", None))

    def test_toggles_empty(self):
        t = ProfileToggles([])
        self.assertIsNone(t.key(" "))
        self.assertEqual(t.key("q"), ("back", None))


if __name__ == "__main__":
    unittest.main()
