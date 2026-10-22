import unittest
from datetime import datetime, timedelta, timezone

from sampan import clock
from tests.helpers import local_tz


class ClockTest(unittest.TestCase):
    def setUp(self):
        cm = local_tz("Asia/Tokyo")  # UTC+9, no DST: deterministic local time
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def test_now_iso_is_utc_with_offset(self):
        s = clock.now_iso()
        self.assertTrue(s.endswith("+00:00"), s)
        self.assertLess(abs(datetime.now(timezone.utc) - datetime.fromisoformat(s)), timedelta(seconds=5))

    def test_naive_legacy_string_is_machine_local_time(self):
        d = clock.to_local("2026-10-21T08:44:01")
        self.assertEqual((d.hour, d.minute), (8, 44))
        self.assertEqual(d.strftime("%m/%d %H:%M"), "10/21 08:44")

    def test_aware_value_is_converted_to_local(self):
        self.assertEqual(clock.to_local("2026-10-21T00:44:01+00:00").strftime("%H:%M"), "09:44")
        self.assertEqual(clock.to_local("2026-10-21T08:44:01+08:00").strftime("%H:%M"), "09:44")

    def test_parse_iso_rejects_garbage(self):
        self.assertIsNone(clock.parse_iso("old"))
        self.assertIsNone(clock.parse_iso(""))
        self.assertIsNone(clock.parse_iso(None))
        self.assertIsNotNone(clock.parse_iso("2026-10-01"))

    def test_mixed_offsets_compare_by_instant(self):
        a = clock.parse_iso("2026-10-21T08:00:00+08:00")  # 00:00 UTC
        b = clock.parse_iso("2026-10-21T01:00:00+00:00")
        self.assertLess(a, b)


if __name__ == "__main__":
    unittest.main()
