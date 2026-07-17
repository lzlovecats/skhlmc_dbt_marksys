"""Chairperson console regressions for login, results and official timing."""

from pathlib import Path
import shutil
import subprocess

import core.chairperson_logic as chairperson_logic
from debate_timing import get_debate_timer_config


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "chairperson" / "index.html").read_text(
    encoding="utf-8"
)
INLINE_SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]


def _between(source, start, end):
    return source.split(start, 1)[1].split(end, 1)[0]


def test_unauthenticated_data_response_reveals_the_login_form():
    api_helper = _between(INLINE_SCRIPT, "async function api", "function values")
    load_handler = _between(INLINE_SCRIPT, "async function load", "async function bell")

    assert "error.status = r.status" in api_helper
    assert "e.status === 401" in load_handler
    assert '$("app").classList.add("hidden")' in load_handler
    assert '$("login").classList.remove("hidden")' in load_handler


def test_pause_and_resume_excludes_the_paused_interval():
    current_source = "function current" + _between(
        INLINE_SCRIPT, "function current", "function styleClock"
    )
    stop_source = "function stop" + _between(
        INLINE_SCRIPT, "function stop", "function resume"
    )
    resume_source = "function resume" + _between(
        INLINE_SCRIPT, "function resume", "function freeButton"
    )
    script = f"""
      {current_source}
      {stop_source}
      {resume_source}
      const timer = {{ elapsed: 0, running: false, start: 0, fired: new Set() }};
      resume(timer, 0);
      stop(timer, 10_000);
      if (Math.abs(timer.elapsed - 10) > 0.0001) process.exit(1);
      resume(timer, 12_000);
      if (Math.abs(current(timer, 15_000) - 13) > 0.0001) process.exit(2);
    """
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_switching_script_panes_keeps_the_official_timer_running():
    pane_handler = _between(
        INLINE_SCRIPT,
        'document.querySelectorAll("[data-pane]")',
        '$("ring").onclick',
    )

    assert "resetTimers()" not in pane_handler


def test_free_debate_switch_stops_the_other_side_before_resuming():
    free_handler = _between(
        INLINE_SCRIPT,
        "const side = e.target.dataset.free;",
        "const reset = e.target.dataset.reset;",
    )

    assert "stop(free[other]);" in free_handler
    assert "resume(free[side]);" in free_handler


def test_closing_panel_displays_the_official_submission_count():
    assert 'id="closingStatus"' in HTML
    assert "現時已有 " in INLINE_SCRIPT
    assert "submitted_judge_count" in INLINE_SCRIPT


def test_chairperson_read_model_reports_submitted_judges(monkeypatch):
    match = {
        "match_id": "M1",
        "match_date": "2026-07-17",
        "match_time": "16:00",
        "topic_text": "測試辯題",
        "pro_team": "正方",
        "con_team": "反方",
    }
    monkeypatch.setattr(chairperson_logic, "_match_records", lambda _db: [match])
    monkeypatch.setattr(chairperson_logic, "_judge_names", lambda *_args: [])
    monkeypatch.setattr(chairperson_logic, "_template", lambda name: name)
    monkeypatch.setattr(
        chairperson_logic,
        "results_data",
        lambda *_args, **_kwargs: {
            "has_scores": True,
            "pro_votes": 2,
            "con_votes": 1,
            "draws": 0,
            "judge_count": 3,
            "best_debater": {"role": "正方主辯", "tied_roles": []},
        },
    )

    data = chairperson_logic.chairperson_data("M1", db=object())

    assert data["closing"]["submitted_judge_count"] == 3


def test_school_format_has_the_complete_official_bell_schedule():
    config = get_debate_timer_config("校園隨想", closing_prep_minutes=2)
    schedules = config["bell_schedules"]

    assert [(bell["t"], bell["rings"]) for bell in schedules["main"]] == [
        (0, 1),
        (210, 1),
        (240, 2),
        (255, 3),
        (280, 5),
    ]
    assert [(bell["t"], bell["rings"]) for bell in schedules["deputy"]] == [
        (0, 1),
        (150, 1),
        (180, 2),
        (195, 3),
        (220, 5),
    ]
    assert [(bell["t"], bell["rings"]) for bell in schedules["free"]] == [
        (0, 1),
        (120, 1),
        (150, 2),
    ]
    assert [(bell["t"], bell["rings"]) for bell in schedules["closing_prep"]] == [
        (0, 1),
        (120, 2),
    ]
    assert config["warning_times"] == {
        "main": 210,
        "deputy": 150,
        "free": 120,
        "closing_prep": None,
    }
    assert config["overtime_times"] == {
        "main": 240,
        "deputy": 180,
        "free": 150,
        "closing_prep": 120,
    }
