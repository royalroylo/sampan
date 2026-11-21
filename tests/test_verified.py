import unittest

from sampan import verified
from sampan.verified import count_versions, judge, newest, parse


class VerifiedTest(unittest.TestCase):
    def test_parse_takes_plain_versions_only(self):
        self.assertEqual(parse("2.1.311"), (2, 1, 311))
        self.assertEqual(parse(" 2.1.9 "), (2, 1, 9))
        for bad in ("2.1.311-beta", "2.1", "", None, "unknown", "v2.1.311", "2.1.311.4"):
            self.assertIsNone(parse(bad), bad)

    def test_judge(self):
        self.assertEqual(judge("claude", "2.1.311"), "verified")
        self.assertEqual(judge("claude", "2.1.287"), "verified")
        self.assertEqual(judge("claude", "2.1.330"), "verified")
        self.assertEqual(judge("claude", "2.1.340"), "newer")
        self.assertEqual(judge("claude", "2.2.0"), "newer")
        self.assertEqual(judge("claude", "3.0.0"), "newer")
        self.assertEqual(judge("claude", "2.1.300"), "older")
        self.assertEqual(judge("claude", "2.0.99"), "older")
        self.assertEqual(judge("claude", "2.1.311-beta"), "unknown")
        self.assertEqual(judge("claude", "2.1.1000"), "newer")  # compared as numbers, not as text

    def test_newest_is_the_highest_number_not_the_last_listed(self):
        self.assertEqual(newest("claude"), max(verified.VERIFIED["claude"], key=parse))

    def test_every_listed_version_is_a_plain_version(self):
        for v in verified.VERIFIED["claude"]:
            self.assertIsNotNone(parse(v), v)

    def test_count_versions_orders_by_version_and_skips_empty(self):
        self.assertEqual(list(count_versions(["2.1.330", "", "2.1.311", "2.1.311", None, "x"]).items()),
                         [("2.1.311", 2), ("2.1.330", 1), ("x", 1)])


if __name__ == "__main__":
    unittest.main()
