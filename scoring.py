# Section A: Individual Speech (台上發言)
import math


SPEECH_CRITERIA = [
    {"key": "內容", "weight": 4, "max": 10},
    {"key": "辭鋒", "weight": 3, "max": 10},
    {"key": "組織", "weight": 2, "max": 10},
    {"key": "風度", "weight": 1, "max": 10},
]


def speech_col(c):
    return f"{c['key']} (x{c['weight']})"


SPEECH_MAX_PER_DEBATER = sum(c["weight"] * c["max"] for c in SPEECH_CRITERIA)  # 100
SPEECH_TOTAL_MAX = SPEECH_MAX_PER_DEBATER * 4  # 400

# Section B: Free Debate (自由辯論)
FREE_DEBATE_CRITERIA = [
    {"key": "內容", "max": 20},
    {"key": "辭鋒", "max": 15},
    {"key": "組織", "max": 10},
    {"key": "合作", "max": 5},
    {"key": "風度", "max": 5},
]


def free_debate_col(c):
    return f"{c['key']} ({c['max']})"


FREE_DEBATE_MAX = sum(c["max"] for c in FREE_DEBATE_CRITERIA)  # 55

# Section C: Coherence (內容連貫)
COHERENCE_MAX = 5

# Grand total
GRAND_TOTAL = SPEECH_TOTAL_MAX + FREE_DEBATE_MAX + COHERENCE_MAX  # 460


def derive_debater_ranks(pro_scores, con_scores):
    """Return one deterministic 1–8 ranking across both teams.

    The score-sheet ranking contract requires every rank to be used exactly
    once.  Equal speech totals therefore keep the stable score-sheet order
    (正方 1–4, then 反方 1–4) until the judge supplies an explicit ranking.
    """
    if len(pro_scores) != 4 or len(con_scores) != 4:
        raise ValueError("排名必須包含正反方各四位辯員。")

    try:
        rows = [
            {"side": side, "position": position, "score": float(score)}
            for side, scores in (("pro", pro_scores), ("con", con_scores))
            for position, score in enumerate(scores, 1)
        ]
    except (TypeError, ValueError) as exc:
        raise ValueError("辯員分數必須是有限數值。") from exc
    if any(not math.isfinite(row["score"]) for row in rows):
        raise ValueError("辯員分數必須是有限數值。")
    rows.sort(key=lambda row: row["score"], reverse=True)
    return {
        (row["side"], row["position"]): rank
        for rank, row in enumerate(rows, 1)
    }
