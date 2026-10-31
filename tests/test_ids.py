import unittest
from types import SimpleNamespace

from sampan import ids


def row(tmux, id):
    return SimpleNamespace(tmux=tmux, id=id)


A = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class IdsTests(unittest.TestCase):
    def test_new_id_and_short(self):
        i = ids.new_id()
        self.assertEqual(len(i), 36)
        self.assertNotEqual(i, ids.new_id())
        self.assertEqual(ids.short(i), i[:8])
        self.assertEqual(ids.PREFIX_MIN, 8)

    def test_resolve_name_first(self):
        named, other = row("1005abcd", A), row("other", "1005abcd-0000-4000-8000-000000000000")
        self.assertIs(ids.resolve([other, named], "1005abcd"), named)

    def test_resolve_full_id_and_prefix(self):
        a, b = row("a", A), row("b", B)
        self.assertIs(ids.resolve([a, b], B), b)
        self.assertIs(ids.resolve([a, b], A[:8]), a)
        self.assertIs(ids.resolve([a, b], A[:12]), a)

    def test_resolve_short_prefix_refused(self):
        self.assertEqual(ids.resolve([row("a", A)], A[:7]), "找不到 session「" + A[:7] + "」")

    def test_resolve_ambiguous_prefix_lists_candidates(self):
        a, b = row("a", "abcdef12-0000-4000-8000-000000000001"), row("b", "abcdef12-0000-4000-8000-000000000002")
        self.assertEqual(ids.resolve([a, b], "abcdef12"), "「abcdef12」對到多個 session：a（abcdef12）、b（abcdef12）")

    def test_resolve_missing(self):
        self.assertEqual(ids.resolve([row("a", A)], "nope"), "找不到 session「nope」")

    def test_empty_id_never_matches(self):
        self.assertEqual(ids.resolve([row("a", "")], ""), "找不到 session「」")
        self.assertEqual(ids.resolve([row("a", "")], "        "), "找不到 session「        」")
