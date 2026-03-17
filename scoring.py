# Section A: Individual Speech (台上發言)
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
