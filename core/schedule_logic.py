"""Pure tournament-draw helpers shared by HTML APIs and legacy Streamlit."""

import random
from system_limits import SCHEDULE_MAX_TEAM_NAME_CHARS, SCHEDULE_MAX_TEAMS

MAX_DRAW_TEAMS = SCHEDULE_MAX_TEAMS
MAX_TEAM_NAME_CHARS = SCHEDULE_MAX_TEAM_NAME_CHARS


def normalize_teams(raw_text):
    return [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]


def validate_teams(teams):
    if len(teams) < 2:
        return "至少需要 2 隊隊伍。"
    if len(teams) > MAX_DRAW_TEAMS:
        return f"每次抽籤最多只可處理 {MAX_DRAW_TEAMS} 隊隊伍。"
    if any(len(team) > MAX_TEAM_NAME_CHARS for team in teams):
        return f"每個隊伍名稱最多 {MAX_TEAM_NAME_CHARS} 個字。"
    if len(teams) != len(set(teams)):
        return "隊伍名稱有重複，請檢查輸入。"
    return ""


def build_draw_result(teams):
    """Mirror draw_match_schedule.py's random main/loser/final bracket."""
    shuffled = teams[:]
    random.shuffle(shuffled)
    summary_rows = []

    if len(shuffled) == 2:
        final_match = {"left": shuffled[0], "right": shuffled[1]}
        summary_rows.append({
            "階段": "決賽", "輪次": "決賽", "場次": "場次 1",
            "正方／甲方": shuffled[0], "反方／乙方": shuffled[1], "備註": "2 隊隊伍直接進入決賽",
        })
        return {
            "teams": teams, "main_rounds": [], "main_byes": [], "loser_rounds": [],
            "final_match": final_match, "summary_rows": summary_rows, "direct_final": True,
        }

    main_rounds, main_byes = [], []
    current_labels = shuffled[:]
    round_num = 1
    while len(current_labels) > 1:
        title = "第一輪正賽" if round_num == 1 else f"正賽第{round_num}輪"
        pairs, next_labels = [], []
        bye_team = current_labels[-1] if len(current_labels) % 2 else None
        playing = current_labels[:-1] if bye_team else current_labels[:]
        for index in range(0, len(playing), 2):
            match_no = index // 2 + 1
            left_team, right_team = playing[index], playing[index + 1]
            pairs.append({"match_no": match_no, "left": left_team, "right": right_team})
            summary_rows.append({
                "階段": "正賽", "輪次": title, "場次": f"場次 {match_no}",
                "正方／甲方": left_team, "反方／乙方": right_team, "備註": "",
            })
            next_labels.append("正賽冠軍" if len(current_labels) == 2 and not bye_team else f"{title}場次{match_no}勝方")
        if bye_team:
            main_byes.append({"round": round_num, "title": title, "team": bye_team})
            summary_rows.append({
                "階段": "正賽", "輪次": title, "場次": "輪空",
                "正方／甲方": bye_team, "反方／乙方": "", "備註": "直接晉級下一輪",
            })
            next_labels.append(bye_team)
        main_rounds.append({"round": round_num, "title": title, "pairs": pairs})
        current_labels = next_labels
        round_num += 1

    loser_rounds = []
    first_round_match_count = len(main_rounds[0]["pairs"])
    if first_round_match_count == 1:
        loser_rounds.append({
            "round": 1, "title": "負方賽", "pairs": [], "bye": None,
            "note": "第一輪只有一場正賽，該場敗方直接成為負方賽冠軍。",
        })
        summary_rows.append({
            "階段": "負方賽", "輪次": "負方賽", "場次": "直接晉級",
            "正方／甲方": "第一輪場次1負方", "反方／乙方": "", "備註": "直接成為負方賽冠軍",
        })
    else:
        current_labels = [f"第一輪場次{index}負方" for index in range(1, first_round_match_count + 1)]
        round_num = 1
        while len(current_labels) > 1:
            title = "負方賽" if round_num == 1 else f"負方賽第{round_num}輪"
            pairs, next_labels = [], []
            bye_team = current_labels[-1] if len(current_labels) % 2 else None
            playing = current_labels[:-1] if bye_team else current_labels[:]
            for index in range(0, len(playing), 2):
                match_no = index // 2 + 1
                left_team, right_team = playing[index], playing[index + 1]
                pairs.append({"match_no": match_no, "left": left_team, "right": right_team})
                summary_rows.append({
                    "階段": "負方賽", "輪次": title, "場次": f"場次 {match_no}",
                    "正方／甲方": left_team, "反方／乙方": right_team, "備註": "",
                })
                next_labels.append("負方賽冠軍" if len(current_labels) == 2 and not bye_team else f"{title}場次{match_no}勝方")
            if bye_team:
                summary_rows.append({
                    "階段": "負方賽", "輪次": title, "場次": "輪空",
                    "正方／甲方": bye_team, "反方／乙方": "", "備註": "直接晉級下一輪",
                })
                next_labels.append(bye_team)
            loser_rounds.append({"round": round_num, "title": title, "pairs": pairs, "bye": bye_team, "note": ""})
            current_labels = next_labels
            round_num += 1

    final_match = {"left": "正賽冠軍", "right": "負方賽冠軍"}
    summary_rows.append({
        "階段": "決賽", "輪次": "決賽", "場次": "場次 1",
        "正方／甲方": final_match["left"], "反方／乙方": final_match["right"], "備註": "",
    })
    return {
        "teams": teams, "main_rounds": main_rounds, "main_byes": main_byes,
        "loser_rounds": loser_rounds, "final_match": final_match,
        "summary_rows": summary_rows, "direct_final": False,
    }


def draw_schedule(raw_text):
    teams = normalize_teams(raw_text)
    error = validate_teams(teams)
    if error:
        return {"ok": False, "message": error, "teams": teams}
    return {"ok": True, "result": build_draw_result(teams)}
