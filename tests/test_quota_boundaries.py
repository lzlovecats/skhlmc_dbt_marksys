"""Hong Kong month and AI-fund settlement boundary regressions."""

import datetime as dt
from zoneinfo import ZoneInfo

from core import funds_logic, resource_limits


HK = ZoneInfo("Asia/Hong_Kong")


def test_resource_month_changes_at_hong_kong_midnight():
    before = dt.datetime(2026, 7, 31, 23, 59, 59, tzinfo=HK)
    after = dt.datetime(2026, 8, 1, 0, 0, 0, tzinfo=HK)
    assert resource_limits.current_period_month(before) == dt.date(2026, 7, 1)
    assert resource_limits.current_period_month(after) == dt.date(2026, 8, 1)


def test_ai_budget_cycle_advances_exactly_at_the_25th_hk_cutoff():
    before = funds_logic.ai_budget_cycle(
        dt.datetime(2026, 7, 24, 23, 59, 59, tzinfo=HK),
    )
    exact = funds_logic.ai_budget_cycle(
        dt.datetime(2026, 7, 25, 0, 0, 0, tzinfo=HK),
    )

    assert before["window_start"] == dt.datetime(2026, 5, 25, tzinfo=HK)
    assert before["window_end"] == dt.datetime(2026, 6, 25, tzinfo=HK)
    assert before["budget_month"] == dt.date(2026, 7, 1)
    assert exact["window_start"] == dt.datetime(2026, 6, 25, tzinfo=HK)
    assert exact["window_end"] == dt.datetime(2026, 7, 25, tzinfo=HK)
    assert exact["budget_month"] == dt.date(2026, 8, 1)


def test_budget_sum_uses_confirmed_at_half_open_window_and_member_deposits_only():
    captured = {}

    class Frame:
        empty = False

        @property
        def iloc(self):
            return [{"amount": 125.5}]

    class Db:
        def query(self, sql, params):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = params
            return Frame()

    cycle = funds_logic.ai_budget_cycle(
        dt.datetime(2026, 7, 25, 0, 0, tzinfo=HK),
    )
    assert funds_logic._budget_amount(Db(), cycle) == 125.5
    assert "transaction_type='member_deposit'" in captured["sql"]
    assert "status='confirmed'" in captured["sql"]
    assert "confirmed_at>=:window_start" in captured["sql"]
    assert "confirmed_at<:window_end" in captured["sql"]
    assert captured["params"] == {
        "window_start": dt.datetime(2026, 6, 25),
        "window_end": dt.datetime(2026, 7, 25),
    }


def test_safe_defaults_keep_only_system_wide_render_and_r2_thresholds():
    render = resource_limits.DEFAULT_LIMITS["render_bandwidth"]
    r2 = resource_limits.DEFAULT_LIMITS["r2_storage"]
    assert (render["warning_value"], render["stop_value"], render["hard_value"]) == (
        3_000_000_000, 3_500_000_000, 4_000_000_000,
    )
    assert (r2["warning_value"], r2["stop_value"], r2["hard_value"]) == (
        7_000_000_000, 8_000_000_000, 8_000_000_000,
    )
