import tomllib
import unittest

from sampan.config_edit import (
    add_marketplace,
    add_plugin,
    append_section,
    plugin_lists,
    remove_marketplace,
    remove_plugin,
    remove_profile,
    remove_table,
    rename_profile,
    set_profile_flag,
)

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


PROF = '''version = 1

# the a profile
# second line
[profiles.a]  # hdr note
config_dir = "~/a"
disabled = true  # off for now

[profiles."b"]
config_dir = "~/b"

# about c
[profiles.c]
config_dir = "~/c"

[codex]
enabled = true
'''


class ProfileSectionTest(unittest.TestCase):
    def test_rename_bare_and_quoted(self):
        new = rename_profile(PROF, "a", "alpha")
        self.assertIn("[profiles.alpha]  # hdr note\n", new)
        self.assertEqual(set(tomllib.loads(new)["profiles"]), {"alpha", "b", "c"})
        self.assertEqual(new.split("[profiles.alpha]")[1], PROF.split("[profiles.a]")[1])
        q = rename_profile(PROF, "b", "bee")
        self.assertIn("[profiles.bee]\n", q)
        self.assertEqual(q.replace("[profiles.bee]", '[profiles."b"]'), PROF)
        self.assertIn('[profiles."x y"]', rename_profile(PROF, "c", "x y"))
        self.assertIsNone(rename_profile(PROF, "zzz", "q"))

    def test_set_flag_replace_keeps_comment(self):
        new = set_profile_flag(PROF, "a", "disabled", False)
        self.assertIn("disabled = false  # off for now\n", new)
        self.assertEqual(tomllib.loads(new)["profiles"]["a"]["disabled"], False)
        self.assertEqual(new.split("[profiles.\"b\"]")[1], PROF.split("[profiles.\"b\"]")[1])

    def test_set_flag_insert_and_delete(self):
        new = set_profile_flag(PROF, "c", "disabled", True)
        self.assertIn("[profiles.c]\ndisabled = true\nconfig_dir", new)
        self.assertTrue(tomllib.loads(new)["profiles"]["c"]["disabled"])
        self.assertEqual(set_profile_flag(new, "c", "disabled", None), PROF)
        gone = set_profile_flag(PROF, "a", "disabled", None)
        self.assertNotIn("disabled", tomllib.loads(gone)["profiles"]["a"])
        self.assertEqual(set_profile_flag(PROF, "b", "disabled", None), PROF)
        self.assertIsNone(set_profile_flag(PROF, "zzz", "disabled", True))

    def test_set_flag_insert_header_without_newline(self):
        self.assertEqual(set_profile_flag("[profiles.a]", "a", "disabled", True), "[profiles.a]\ndisabled = true\n")

    def test_remove_profile_with_comments(self):
        new = remove_profile(PROF, "a")
        self.assertEqual(set(tomllib.loads(new)["profiles"]), {"b", "c"})
        self.assertNotIn("the a profile", new)
        self.assertNotIn("second line", new)
        self.assertTrue(new.startswith('version = 1\n\n[profiles."b"]'))
        c = remove_profile(PROF, "c")
        self.assertNotIn("about c", c)
        self.assertIn('config_dir = "~/b"\n\n[codex]', c)
        self.assertEqual(c.split("[codex]")[1], PROF.split("[codex]")[1])
        self.assertIsNone(remove_profile(PROF, "zzz"))

    def test_remove_profile_last_section(self):
        t = "version = 1\n\n[profiles.a]\nx = 1\n\n[profiles.b]\ny = 2\n"
        self.assertEqual(remove_profile(t, "b"), "version = 1\n\n[profiles.a]\nx = 1\n")
        self.assertEqual(remove_profile("[profiles.a]\nx = 1\n", "a"), "")

    def test_append_section(self):
        self.assertEqual(append_section("a = 1\n", "[profiles.n]\nx = 1\n"), "a = 1\n\n[profiles.n]\nx = 1\n")
        self.assertEqual(append_section("a = 1", "[profiles.n]\nx = 1"), "a = 1\n\n[profiles.n]\nx = 1\n")
        self.assertEqual(append_section("a = 1\n\n\n", "[p]\n"), "a = 1\n\n[p]\n")
        self.assertEqual(append_section("", "[p]\n"), "[p]\n")

    def test_remove_table(self):
        new = remove_table(PROF, "codex")
        self.assertNotIn("codex", tomllib.loads(new))
        self.assertTrue(new.endswith('config_dir = "~/c"\n'))
        self.assertIsNone(remove_table(PROF, "nope"))
        self.assertIsNone(remove_table("[[codex]]\nx = 1\n", "codex"))
