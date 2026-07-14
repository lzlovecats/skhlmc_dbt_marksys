from io import BytesIO
from pathlib import Path
import datetime as dt

import pandas as pd
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from scoring import (
    FREE_DEBATE_CRITERIA,
    SPEECH_CRITERIA,
    derive_debater_ranks,
    free_debate_col,
    speech_col,
)


TEMPLATE_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "pdf_templates" / "score_sheet_template.png"
PAGE_WIDTH = 612
PAGE_HEIGHT = 792
FONT_NAME = "ScoreSheetCJK"
FALLBACK_CID_FONT = "MSung-Light"
FONT_CANDIDATES = [
    (Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansTC-Regular.otf", 0),
    (Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansTC-Regular.ttf", 0),
    (Path("/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf"), 0),
    (Path("/usr/share/fonts/truetype/noto/NotoSansTC-Regular.ttf"), 0),
    (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), 3),
    (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), 4),
    (Path("/System/Library/Fonts/STHeiti Light.ttc"), 0),
    (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"), 0),
    (Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 0),
]

META_POS = {
    "date": (120, 637),
    "time": (388, 637),
    "side": (140, 611),
    "match_id": (390, 611),
    "topic": (95, 579),
}
SPEECH_Y = [487, 453, 419, 384]
SPEECH_X = {
    "name": 123,
    "content": 174,
    "delivery": 238,
    "structure": 302,
    "manner": 365,
    "total": 429,
    "rank": 512,
}
FREE_DEBATE_Y = 306
FREE_DEBATE_X = [82, 163, 244, 324, 405, 486]
DEDUCTION_Y = 230
DEDUCTION_X = [86, 171, 256, 341, 424]
SUMMARY_Y = 193
SUMMARY_X = {
    "subtotal": 134,
    "coherence": 267,
    "final_total": 407,
}
WINNER_POS = (100, 166)
JUDGE_NAME_POS = (135, 136)


def _register_font():
    global FONT_NAME
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        for font_path, subfont_index in FONT_CANDIDATES:
            if font_path.exists():
                try:
                    pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path), subfontIndex=subfont_index))
                    return
                except Exception:
                    continue
        FONT_NAME = FALLBACK_CID_FONT
        if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))


def _is_blank(value):
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except Exception:
        return False


def _get(source, key, default=""):
    try:
        value = source.get(key, default)
    except AttributeError:
        try:
            value = source[key]
        except Exception:
            value = default
    return default if _is_blank(value) else value


def _num(value, default=0):
    if _is_blank(value):
        return default
    try:
        return int(round(float(value)))
    except (OverflowError, TypeError, ValueError):
        return default


def _format_date(value):
    if _is_blank(value):
        return ""
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except Exception:
        parsed = None
    if parsed is None or pd.isna(parsed):
        return str(value)
    return f"{parsed.year} 年 {parsed.month} 月 {parsed.day} 日"


def _format_time(value):
    if _is_blank(value):
        return ""
    if isinstance(value, (dt.datetime, dt.time)):
        return value.strftime("%H:%M")
    text = str(value).strip()
    if len(text) >= 5 and text[2:3] == ":":
        return text[:5]
    try:
        parsed = pd.to_datetime(text, errors="coerce")
    except Exception:
        parsed = None
    if parsed is not None and not pd.isna(parsed):
        return parsed.strftime("%H:%M")
    return text


def _as_df(value):
    if isinstance(value, pd.DataFrame):
        return value
    if isinstance(value, dict):
        return pd.DataFrame([value])
    if isinstance(value, (list, tuple)):
        return pd.DataFrame(value)
    return pd.DataFrame()


def _row_value(df, row_index, column, default=""):
    if df.empty or column not in df.columns or row_index >= len(df.index):
        return default
    return _get(df.iloc[row_index], column, default)


def _text(value):
    return "" if _is_blank(value) else str(value)


def _draw_center(c, x, y, value, size=10):
    c.setFont(FONT_NAME, size)
    c.drawCentredString(x, y, _text(value))


def _draw_left(c, x, y, value, size=10):
    c.setFont(FONT_NAME, size)
    c.drawString(x, y, _text(value))


def _draw_fit(c, x, y, value, max_width, size=10, min_size=7):
    text = _text(value)
    c.setFont(FONT_NAME, size)
    while size > min_size and pdfmetrics.stringWidth(text, FONT_NAME, size) > max_width:
        size -= 0.5
        c.setFont(FONT_NAME, size)
    c.drawString(x, y, text)


def _draw_topic(c, x, y, value, max_width=455, size=9):
    text = _text(value)
    if not text:
        return

    lines = []
    current = ""
    for ch in text:
        test = current + ch
        if current and pdfmetrics.stringWidth(test, FONT_NAME, size) > max_width:
            lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)

    c.setFont(FONT_NAME, size)
    for i, line in enumerate(lines[:2]):
        c.drawString(x, y - i * 12, line)


def _weighted_speech_scores(df, row_index):
    scores = []
    for criterion in SPEECH_CRITERIA:
        raw_score = _num(_row_value(df, row_index, speech_col(criterion), 0))
        scores.append(raw_score * criterion["weight"])
    return scores


def _individual_scores(side_data):
    scores = side_data.get("ind_scores")
    if isinstance(scores, list) and len(scores) >= 4:
        return [_num(score) for score in scores[:4]]

    df = _as_df(side_data.get("raw_df_a"))
    return [sum(_weighted_speech_scores(df, i)) for i in range(4)]


def _build_ranks(pro_data, con_data, rankings=None):
    if isinstance(rankings, dict):
        try:
            pro_ranks = list(rankings["正方"])
            con_ranks = list(rankings["反方"])
            submitted = [int(value) for value in pro_ranks + con_ranks]
        except (KeyError, TypeError, ValueError):
            submitted = []
            pro_ranks, con_ranks = [], []
        if (
            len(pro_ranks) == 4
            and len(con_ranks) == 4
            and sorted(submitted) == list(range(1, 9))
        ):
            return {"正方": submitted[:4], "反方": submitted[4:]}

    all_scores = _individual_scores(pro_data) + _individual_scores(con_data)
    if len(all_scores) != 8:
        ranks = [""] * 8
    else:
        derived = derive_debater_ranks(all_scores[:4], all_scores[4:])
        ranks = [
            derived[(side, position)]
            for side in ("pro", "con")
            for position in range(1, 5)
        ]
    return {
        "正方": ranks[:4],
        "反方": ranks[4:],
    }


def _winner_label(judge_record):
    pro_total = _num(_get(judge_record, "pro_total_score"))
    con_total = _num(_get(judge_record, "con_total_score"))
    if pro_total > con_total:
        return "正方"
    if con_total > pro_total:
        return "反方"
    return "平局"


def _side_totals(side_data):
    total_a = _num(side_data.get("total_a"))
    total_b = _num(side_data.get("total_b"))
    deduction = _num(side_data.get("deduction"))
    coherence = _num(side_data.get("coherence"))
    final_total = _num(side_data.get("final_total"), total_a + total_b - deduction + coherence)
    return total_a, total_b, deduction, coherence, final_total


def _draw_meta(c, match_info, judge_record, side_label, team_name):
    _draw_center(c, *META_POS["date"], _format_date(_get(match_info, "match_date")), size=9)
    _draw_center(c, *META_POS["time"], _format_time(_get(match_info, "match_time")), size=9)
    _draw_center(c, *META_POS["side"], f"{side_label}：{team_name}", size=9)
    _draw_center(c, *META_POS["match_id"], _get(match_info, "match_id"), size=9)
    _draw_topic(c, *META_POS["topic"], _get(match_info, "topic_text"))


def _draw_speech_scores(c, side_data, ranks):
    df = _as_df(side_data.get("raw_df_a"))
    score_keys = ["content", "delivery", "structure", "manner"]
    for row_index, y in enumerate(SPEECH_Y):
        weighted_scores = _weighted_speech_scores(df, row_index)
        _draw_fit(c, SPEECH_X["name"] - 27, y, _row_value(df, row_index, "姓名", ""), 52, size=9)
        for key, score in zip(score_keys, weighted_scores):
            _draw_center(c, SPEECH_X[key], y, score, size=9)
        _draw_center(c, SPEECH_X["total"], y, sum(weighted_scores), size=9)
        _draw_center(c, SPEECH_X["rank"], y, ranks[row_index] if row_index < len(ranks) else "", size=9)


def _draw_free_debate_scores(c, side_data):
    df = _as_df(side_data.get("raw_df_b"))
    scores = [_num(_row_value(df, 0, free_debate_col(criterion), 0)) for criterion in FREE_DEBATE_CRITERIA]
    for x, score in zip(FREE_DEBATE_X, scores + [sum(scores)]):
        _draw_center(c, x, FREE_DEBATE_Y, score, size=9)


def _draw_deductions(c, side_data):
    deduction = _num(side_data.get("deduction"))
    for x in DEDUCTION_X[:-1]:
        _draw_center(c, x, DEDUCTION_Y, "", size=9)
    _draw_center(c, DEDUCTION_X[-1], DEDUCTION_Y, deduction, size=9)


def _draw_summary(c, side_data, judge_record):
    total_a, total_b, deduction, coherence, final_total = _side_totals(side_data)
    subtotal = total_a + total_b - deduction
    _draw_center(c, SUMMARY_X["subtotal"], SUMMARY_Y, subtotal, size=9)
    _draw_center(c, SUMMARY_X["coherence"], SUMMARY_Y, coherence, size=9)
    _draw_center(c, SUMMARY_X["final_total"], SUMMARY_Y, final_total, size=9)
    _draw_center(c, *WINNER_POS, _winner_label(judge_record), size=9)
    _draw_left(c, *JUDGE_NAME_POS, _get(judge_record, "judge_name"), size=9)


def _draw_score_page(c, match_info, judge_record, side_label, team_name, side_data, ranks):
    # The original Quartz PDF has malformed indirect references which can
    # cross-link when the same page is merged twice.  A checked 300 dpi render
    # is therefore used as the immutable background for deterministic output.
    c.drawImage(
        str(TEMPLATE_IMAGE_PATH),
        0,
        0,
        width=PAGE_WIDTH,
        height=PAGE_HEIGHT,
        preserveAspectRatio=True,
        mask="auto",
    )
    c.setFillColorRGB(0, 0, 0)

    _draw_meta(c, match_info, judge_record, side_label, team_name)
    _draw_speech_scores(c, side_data, ranks)
    _draw_free_debate_scores(c, side_data)
    _draw_deductions(c, side_data)
    _draw_summary(c, side_data, judge_record)


def build_score_sheet_pdf(match_info, judge_record, pro_data, con_data, rankings=None):
    if not TEMPLATE_IMAGE_PATH.exists():
        raise FileNotFoundError(f"PDF template image not found: {TEMPLATE_IMAGE_PATH}")

    ranks = _build_ranks(pro_data, con_data, rankings=rankings)

    pages = [
        ("正方", _get(judge_record, "pro_team"), pro_data, ranks["正方"]),
        ("反方", _get(judge_record, "con_team"), con_data, ranks["反方"]),
    ]

    buffer = BytesIO()
    c = canvas.Canvas(
        buffer,
        pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
        pageCompression=1,
    )
    c.setTitle("Debate score sheet")
    _register_font()

    for side_label, team_name, side_data, side_ranks in pages:
        _draw_score_page(
            c,
            match_info,
            judge_record,
            side_label,
            team_name,
            side_data,
            side_ranks,
        )
        c.showPage()

    c.save()
    return buffer.getvalue()
