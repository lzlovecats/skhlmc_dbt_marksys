"""Score arithmetic — a wrong number here is a wrong competition result."""

import pytest

from scoring import (
    COHERENCE_MAX,
    FREE_DEBATE_CRITERIA,
    FREE_DEBATE_MAX,
    GRAND_TOTAL,
    SPEECH_CRITERIA,
    SPEECH_MAX_PER_DEBATER,
    SPEECH_TOTAL_MAX,
    free_debate_col,
    speech_col,
)
from core.judging_logic import auto_derive_ranking_order, normalise_side_data


def _speech_rows(value=10):
    return [
        {
            "辯位": f"位{index}",
            "姓名": f"名{index}",
            **{speech_col(criterion): value for criterion in SPEECH_CRITERIA},
        }
        for index in range(4)
    ]


def _free_rows(full=True):
    return [{
        free_debate_col(criterion): criterion["max"] if full else 0
        for criterion in FREE_DEBATE_CRITERIA
    }]


def _side_data(**overrides):
    data = {
        "team_name": "測試隊",
        "raw_df_a": _speech_rows(),
        "raw_df_b": _free_rows(),
        "deduction": 0,
        "coherence": COHERENCE_MAX,
    }
    data.update(overrides)
    return data


def test_scoresheet_constants_match_official_rules():
    assert SPEECH_MAX_PER_DEBATER == 100
    assert SPEECH_TOTAL_MAX == 400
    assert FREE_DEBATE_MAX == 55
    assert COHERENCE_MAX == 5
    assert GRAND_TOTAL == 460


def test_full_marks_reach_grand_total():
    result = normalise_side_data("正方", _side_data())
    assert result["ind_scores"] == [SPEECH_MAX_PER_DEBATER] * 4
    assert result["total_a"] == SPEECH_TOTAL_MAX
    assert result["total_b"] == FREE_DEBATE_MAX
    assert result["final_total"] == GRAND_TOTAL


def test_speech_criteria_weights_are_applied():
    rows = _speech_rows(0)
    # 內容 carries weight 4: 10 marks there alone must contribute 40.
    rows[0][speech_col(SPEECH_CRITERIA[0])] = 10
    result = normalise_side_data("正方", _side_data(
        raw_df_a=rows, raw_df_b=_free_rows(full=False), coherence=0,
    ))
    assert result["ind_scores"][0] == 40
    assert result["final_total"] == 40


def test_deduction_subtracts_from_final_total():
    result = normalise_side_data("正方", _side_data(deduction=7))
    assert result["final_total"] == GRAND_TOTAL - 7


def test_out_of_range_and_malformed_input_is_rejected():
    over = _speech_rows()
    over[0][speech_col(SPEECH_CRITERIA[0])] = SPEECH_CRITERIA[0]["max"] + 1
    with pytest.raises(ValueError):
        normalise_side_data("正方", _side_data(raw_df_a=over))
    with pytest.raises(ValueError):
        normalise_side_data("正方", _side_data(raw_df_a=_speech_rows()[:3]))
    with pytest.raises(ValueError):
        normalise_side_data("旁證", _side_data())
    with pytest.raises(ValueError):
        normalise_side_data("正方", _side_data(coherence=COHERENCE_MAX + 1))


def test_best_debater_ranking_orders_all_eight_speakers():
    ranks = auto_derive_ranking_order([100, 90, 80, 70], [95, 85, 75, 65])
    assert ranks[("pro", 1)] == 1
    assert ranks[("con", 1)] == 2
    assert ranks[("con", 4)] == 8
    assert sorted(ranks.values()) == list(range(1, 9))
