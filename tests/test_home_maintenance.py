import unittest

from core.home_logic import format_maintenance_deadline


class HomeMaintenanceTests(unittest.TestCase):
    def test_configured_deadline_is_rendered_as_hong_kong_time(self):
        self.assertEqual(
            format_maintenance_deadline("2026-07-13T22:05"),
            "2026年7月13日 22:05（香港時間）",
        )

    def test_missing_or_invalid_deadline_has_no_stale_fallback(self):
        self.assertEqual(format_maintenance_deadline(""), "")
        self.assertEqual(format_maintenance_deadline("2026年4月3日"), "")


if __name__ == "__main__":
    unittest.main()
