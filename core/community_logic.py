"""Validation and display rules for committee community features."""

from datetime import date, time
import re


RECENT_SIDES = frozenset(("pro", "con", "unconfirmed"))
RECENT_RESULTS = frozenset(("win", "loss", "draw", "unconfirmed"))
MEMBERSHIP_EXIT_TYPES = frozenset(("current", "left", "graduated"))
RESULT_LABELS = {
    "win": "勝",
    "loss": "負",
    "draw": "和",
    "unconfirmed": "未能確認",
}
SIDE_LABELS = {
    "pro": "正方",
    "con": "反方",
    "unconfirmed": "未能確認",
}


def _text(value, label, maximum, *, required=False):
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"請填寫{label}。")
    if len(result) > maximum:
        raise ValueError(f"{label}不可多於 {maximum} 個字元。")
    return result


def _date(value, label):
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}無效。") from exc


def _time(value, label):
    text = str(value or "").strip()[:8]
    try:
        parsed = time.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}無效。") from exc
    return parsed.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def validate_recent_match(values):
    side = str(values.get("our_side") or "").strip()
    result = str(values.get("result") or "unconfirmed").strip()
    if side not in RECENT_SIDES:
        raise ValueError("我方站方無效。")
    if result not in RECENT_RESULTS:
        raise ValueError("賽果無效。")
    score = _text(values.get("score_text"), "比分", 40)
    if score and not re.fullmatch(r"\d{1,3}\s*:\s*\d{1,3}", score):
        raise ValueError("比分格式應為「正方票數:反方票數」，例如 3:0。")
    return {
        "competition_name": _text(
            values.get("competition_name"), "比賽", 300, required=True,
        ),
        "opponent": _text(values.get("opponent"), "對手", 300, required=True),
        "match_date": _date(values.get("match_date"), "日期"),
        "match_time": _time(values.get("match_time"), "時間"),
        "topic_text": _text(values.get("topic_text"), "辯題", 1000, required=True),
        "our_side": side,
        "result": result,
        "score_text": score.replace(" ", ""),
        "best_debater": _text(values.get("best_debater"), "最佳辯論員", 300),
        "notes": _text(values.get("notes"), "備註", 3000),
    }


def academic_year_label(start_year):
    year = int(start_year)
    return f"{year}/{str(year + 1)[-2:]}"


def validate_membership(values):
    try:
        joined = int(values.get("joined_academic_year"))
        ended_raw = values.get("ended_academic_year")
        ended = None if ended_raw in (None, "") else int(ended_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("請提供有效學年。") from exc
    if not 1900 <= joined <= 2200 or (ended is not None and not 1900 <= ended <= 2200):
        raise ValueError("學年必須介乎 1900 至 2200。")
    exit_type = str(values.get("exit_type") or "current").strip()
    if exit_type not in MEMBERSHIP_EXIT_TYPES:
        raise ValueError("任期狀態無效。")
    if exit_type == "current" and ended is not None:
        raise ValueError("現役委員不可設定離隊學年。")
    if exit_type != "current" and (ended is None or ended < joined):
        raise ValueError("離隊／畢業學年不可早於入隊學年。")
    return {
        "member_user_id": _text(values.get("member_user_id"), "帳戶", 200) or None,
        "display_name": _text(values.get("display_name"), "姓名", 300, required=True),
        "joined_academic_year": joined,
        "ended_academic_year": ended,
        "exit_type": exit_type,
    }


def validate_history_event(values):
    try:
        academic_year = int(values.get("academic_year_start"))
    except (TypeError, ValueError) as exc:
        raise ValueError("請提供有效學年。") from exc
    if not 1900 <= academic_year <= 2200:
        raise ValueError("學年必須介乎 1900 至 2200。")
    raw_date = str(values.get("event_date") or "").strip()
    event_date = _date(raw_date, "事件日期") if raw_date else None
    if event_date:
        parsed = date.fromisoformat(event_date)
        start = date(academic_year, 9, 1)
        end = date(academic_year + 1, 8, 31)
        if not start <= parsed <= end:
            raise ValueError("事件日期必須位於所選學年（9 月至翌年 8 月）內。")
    return {
        "academic_year_start": academic_year,
        "event_date": event_date,
        "title": _text(values.get("title"), "事件標題", 500, required=True),
        "description": _text(values.get("description"), "事件內容", 5000),
        "match_ids": _identifier_list(values.get("match_ids"), 20, 200, "比賽"),
        "photo_ids": _integer_list(values.get("photo_ids"), 30, "圖片"),
    }


def validate_thread(values):
    return {
        "title": _text(values.get("title"), "主題", 300, required=True),
        "body": _text(values.get("body"), "內容", 8000, required=True),
        "match_ids": _identifier_list(values.get("match_ids"), 20, 200, "比賽"),
        "photo_ids": _integer_list(values.get("photo_ids"), 30, "圖片"),
    }


def validate_post_body(value):
    return _text(value, "內容", 8000, required=True)


def _identifier_list(values, maximum_items, maximum_length, label):
    if not isinstance(values, list) or len(values) > maximum_items:
        raise ValueError(f"每次最多連結 {maximum_items} 個{label}。")
    cleaned = []
    for value in values:
        item = _text(value, label, maximum_length, required=True)
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def _integer_list(values, maximum_items, label):
    if not isinstance(values, list) or len(values) > maximum_items:
        raise ValueError(f"每次最多連結 {maximum_items} 張{label}。")
    try:
        cleaned = list(dict.fromkeys(int(value) for value in values))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}連結無效。") from exc
    if any(value < 1 for value in cleaned):
        raise ValueError(f"{label}連結無效。")
    return cleaned


def recent_notification_copy(match, event_kind):
    name = str(match.get("competition_name") or "近期比賽")
    opponent = str(match.get("opponent") or "未定")
    if event_kind == "result":
        result = RESULT_LABELS.get(str(match.get("result")), "未能確認")
        score = str(match.get("score_text") or "").strip()
        suffix = f"（{score}）" if score else ""
        return f"賽果：{name}－{result}", f"對手：{opponent}{suffix}"
    day = str(match.get("match_date") or "")[:10]
    clock = str(match.get("match_time") or "")[:5]
    return f"新比賽：{name}", f"對手：{opponent}｜{day} {clock}".strip()


def forum_notification_copy(author_user_id, thread_title, event_kind):
    author = _text(author_user_id, "作者", 200, required=True)
    title = _text(thread_title, "主題", 300, required=True)
    if event_kind == "thread":
        return "老鬼專區有新主題", f"{author} 發表「{title}」"
    if event_kind == "reply":
        return "老鬼專區有新回覆", f"{author} 回覆「{title}」"
    raise ValueError("老鬼專區通知類型無效。")
