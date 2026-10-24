import tomllib
import unittest

from sampan.config_edit import add_marketplace, add_plugin, plugin_lists, remove_marketplace, remove_plugin

MULTI = '''version = 1

[marketplaces]
m = "o/m"

[profiles.work]
config_dir = "~/.claude"
plugins = [
  "a@m",
  # keep this one
  "b@m",  # trailing note
  "c@m",
]

[profiles.other]
plugins = ["b@m"]
'''


class RemovePluginTest(unittest.TestCase):
    def test_remove_line_item_keeps_rest_byte_identical(self):
        new = remove_plugin(MULTI, "work", "b@m")
        self.assertEqual(new, MULTI.replace('  "b@m",  # trailing note\n', ""))
        self.assertEqual(plugin_lists(new)["work"], ["a@m", "c@m"])

    def test_remove_inline_first_middle_last_and_only(self):
        def t(arr):
            return f"[profiles.w]\nplugins = {arr}\n"
        cases = [('["a", "b", "c"]', "b", '["a", "c"]'), ('["a", "b", "c"]', "a", '["b", "c"]'),
                 ('["a", "b", "c"]', "c", '["a", "b"]'), ('["a"]', "a", "[]")]
        for arr, pid, want in cases:
            self.assertEqual(remove_plugin(t(arr), "w", pid), t(want))

    def test_remove_single_quoted(self):
        t = "[profiles.w]\nplugins = ['a@m', 'b@m']\n"
        self.assertEqual(remove_plugin(t, "w", "b@m"), "[profiles.w]\nplugins = ['a@m']\n")

    def test_remove_only_touches_named_profile(self):
        new = remove_plugin(MULTI, "other", "b@m")
        self.assertEqual(plugin_lists(new), {"work": ["a@m", "b@m", "c@m"], "other": []})

    def test_remove_missing_returns_none(self):
        t = MULTI + "\n[profiles.bare]\nlabel = \"x\"\n"
        self.assertIsNone(remove_plugin(t, "nope", "a@m"))
        self.assertIsNone(remove_plugin(t, "work", "z@m"))
        self.assertIsNone(remove_plugin(t, "bare", "a@m"))

    def test_remove_keeps_comments(self):
        def t(arr):
            return f"[profiles.w]\nplugins = {arr}\n"
        cases = [('["a", # note\n "b"]', "b", '["a" # note\n]'),
                 ('["a", "b", # n\n "c"]', "b", '["a", # n\n "c"]'),
                 ('["a", "b",]', "b", '["a",]'),
                 ('[ "a" ]', "a", "[]")]
        for arr, pid, want in cases:
            self.assertEqual(remove_plugin(t(arr), "w", pid), t(want))

    def test_add_then_remove_round_trips(self):
        inline = '[profiles.work]\nplugins = ["a@m", "b@m"]\n'
        empty = "[profiles.work]\nplugins = []\n"
        nocomma = '[profiles.work]\nplugins = [\n  "a@m",\n  "b@m"\n]\n'
        for t in (MULTI, inline, empty, nocomma):
            added = add_plugin(t, "work", "x@m")
            self.assertIn("x@m", plugin_lists(added)["work"])
            self.assertEqual(remove_plugin(added, "work", "x@m"), t)


class MarketplaceTest(unittest.TestCase):
    T = '[marketplaces]\na = "o/a"\n"b-c" = "o/b"\n\n[other]\na = "keep"\n'

    def test_remove_marketplace_line_only(self):
        self.assertEqual(remove_marketplace(self.T, "a"), self.T.replace('a = "o/a"\n', "", 1))
        self.assertEqual(remove_marketplace(self.T, "b-c"), self.T.replace('"b-c" = "o/b"\n', ""))
        self.assertIsNone(remove_marketplace(self.T, "zzz"))
        self.assertIn('a = "keep"', remove_marketplace(self.T, "a"))
        self.assertIsNone(remove_marketplace('[other]\na = "x"\n', "a"))

    def test_add_marketplace_and_plugin_lists(self):
        new = add_marketplace(MULTI, "new", "o/r")
        self.assertEqual(tomllib.loads(new)["marketplaces"]["new"], "o/r")
        self.assertIsNone(plugin_lists("not toml ["))


if __name__ == "__main__":
    unittest.main()
