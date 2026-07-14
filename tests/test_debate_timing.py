"""Mock sequence and bell contracts — wrong here means a broken formal Mock."""

from debate_timing import (
    DEBATE_FORMATS,
    MOCK_SESSION_BUDGET_SECONDS,
    full_mock_total_seconds,
    get_debate_timer_config,
    get_full_mock_sequence,
    split_mock_into_sessions,
)


def test_every_format_has_a_complete_mock_sequence():
    for debate_format in DEBATE_FORMATS:
        segments = get_full_mock_sequence(debate_format)
        ids = [segment["id"] for segment in segments]
        for required in ("main_pro", "main_con", "closing_pro", "closing_con"):
            assert required in ids, f"{debate_format} 缺少 {required}"
        assert all(segment["bells"] for segment in segments), debate_format
        assert all(segment["seconds"] >= 0 for segment in segments), debate_format


def test_session_split_preserves_total_seconds_and_budget():
    for debate_format in DEBATE_FORMATS:
        minutes = 5 if debate_format == "聯中" else None
        segments = get_full_mock_sequence(debate_format, free_debate_minutes=minutes)
        sessions = split_mock_into_sessions(segments)
        assert sum(s["planned_seconds"] for s in sessions) == full_mock_total_seconds(segments)
        assert all(
            s["planned_seconds"] <= MOCK_SESSION_BUDGET_SECONDS for s in sessions
        ), debate_format


def test_linked_free_debate_session_counts_both_time_banks():
    segments = get_full_mock_sequence("聯中", free_debate_minutes=5)
    sessions = split_mock_into_sessions(segments)
    free_session = next(
        session for session in sessions
        if any(segment["id"] == "free" for segment in session["segments"])
    )
    assert free_session["planned_seconds"] == 600


def test_free_debate_bell_schedule_exists_for_practice_formats():
    for debate_format in ("校園隨想", "聯中"):
        config = get_debate_timer_config(debate_format, free_debate_minutes=2.5)
        assert config["bell_schedules"].get("free"), debate_format
