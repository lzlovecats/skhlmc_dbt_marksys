"""Live quota windows are Hong Kong-calendar based but stored in UTC.

A wrong conversion re-opens the historical eight-hour bypass window around
Hong Kong midnight, so the boundaries are pinned exactly here.
"""

import datetime
from zoneinfo import ZoneInfo

from deploy.proxy import _solo_quota_boundaries

HK = ZoneInfo("Asia/Hong_Kong")


def test_free_daily_window_starts_at_hk_midnight_in_utc():
    just_after_midnight = datetime.datetime(2026, 7, 14, 0, 30, tzinfo=HK)
    user_start, month_start = _solo_quota_boundaries(just_after_midnight, is_mock=False)
    assert user_start == datetime.datetime(2026, 7, 13, 16, 0)
    assert month_start == datetime.datetime(2026, 6, 30, 16, 0)
    assert user_start.tzinfo is None and month_start.tzinfo is None


def test_mock_weekly_window_starts_on_hk_monday_in_utc():
    tuesday = datetime.datetime(2026, 7, 14, 23, 59, tzinfo=HK)
    user_start, _ = _solo_quota_boundaries(tuesday, is_mock=True)
    # Monday 2026-07-13 00:00 HK == Sunday 2026-07-12 16:00 UTC.
    assert user_start == datetime.datetime(2026, 7, 12, 16, 0)
