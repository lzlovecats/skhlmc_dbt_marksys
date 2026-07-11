import re
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from auth import require_committee
from functions import execute_query, query_params
from schema import CREATE_BUG_REPORTS, TABLE_BUG_REPORTS


st.header("Bug回報")
st.caption("請提供足夠資料讓 developer 可以重現問題。太籠統的描述不會提交。")


STATUS_LABELS = {
    "open": "待處理",
    "investigating": "跟進中",
    "fixed": "已修正",
    "not_reproducible": "未能重現",
    "duplicate": "重複回報",
    "closed": "已關閉",
}

PAGE_OPTIONS = [
    "主頁",
    "辯題徵集、投票及罷免",
    "AI 辯論易",
    "聖呂中辯AI訓練",
    "比賽片段重溫",
    "比賽圖片回顧",
    "遲到罰款基金",
    "其他",
]

# Filler phrases that carry no reproduction detail on their own. Stripped out
# before measuring how much concrete content a description actually has.
VAGUE_PATTERNS = [
    r"有\s*bug",
    r"用\s*唔\s*到",
    r"壞\s*咗",
    r"出\s*錯",
    r"唔\s*得",
    r"有\s*問題",
    r"唔\s*work",
    r"唔\s*正常",
]

# Minimum concrete (non-whitespace) characters a reproduction description needs.
MIN_STEPS_LEN = 15


def ensure_bug_reports_table():
    execute_query(CREATE_BUG_REPORTS)


def _plain_len(text):
    return len(re.sub(r"\s+", "", text or ""))


def _is_too_vague(text):
    cleaned = re.sub(r"\s+", "", text or "")
    if len(cleaned) < MIN_STEPS_LEN:
        return True
    # Remove filler phrases; if almost nothing concrete is left, the report
    # is just "有bug / 用唔到" with no actual steps.
    concrete = cleaned
    for pattern in VAGUE_PATTERNS:
        concrete = re.sub(pattern, "", concrete)
    return len(concrete) < 8


def _validate_report(affected_page, steps, expected, actual):
    errors = []
    if not affected_page.strip():
        errors.append("請選擇或填寫受影響頁面。")
    if _is_too_vague(steps):
        errors.append("請用具體步驟寫明點樣重現，例如：先去邊頁、撳邊個掣、輸入咩內容、之後發生咩事。")
    if _plain_len(actual) < 15:
        errors.append("請具體描述實際出現的錯誤或異常畫面。")
    if _plain_len(expected) < 8:
        errors.append("請寫明正常情況下你預期系統應該點樣運作。")
    return errors


def _format_time(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "")[:16]


user_id = require_committee()
ensure_bug_reports_table()

with st.form("bug_report_form"):
    page_choice = st.selectbox("受影響頁面", PAGE_OPTIONS)
    other_page = ""
    if page_choice == "其他":
        other_page = st.text_input("請填寫頁面名稱")
    device_info = st.text_input(
        "裝置及瀏覽器",
        placeholder="例如：iPhone 15 / iOS 18 / 主畫面 App；或 Chrome Android",
    )
    reproduction_steps = st.text_area(
        "重現步驟",
        placeholder="請逐步寫低：1. 進入哪個頁面；2. 撳哪個按鈕/選項；3. 輸入甚麼；4. 出現甚麼問題。",
        height=170,
    )
    expected_result = st.text_area(
        "預期結果",
        placeholder="正常應該出現或完成甚麼？",
        height=90,
    )
    actual_result = st.text_area(
        "實際結果",
        placeholder="實際出現了甚麼錯誤、卡在哪一步、畫面有甚麼異常？",
        height=120,
    )
    extra_notes = st.text_area(
        "補充資料（可留空）",
        placeholder="例如：錯誤訊息、截圖已發到群組、發生時間、是否每次都重現。",
        height=90,
    )
    submit_report = st.form_submit_button("提交Bug回報", type="primary", width="stretch")

if submit_report:
    affected_page = other_page.strip() if page_choice == "其他" else page_choice
    validation_errors = _validate_report(affected_page, reproduction_steps, expected_result, actual_result)
    if validation_errors:
        for error in validation_errors:
            st.warning(error)
    else:
        now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
        execute_query(
            f"""
            INSERT INTO {TABLE_BUG_REPORTS}
                (reporter_user_id, affected_page, device_info, reproduction_steps,
                 expected_result, actual_result, extra_notes, status, created_at, updated_at)
            VALUES
                (:uid, :page, :device, :steps, :expected, :actual, :notes, 'open', :now, :now)
            """,
            {
                "uid": user_id,
                "page": affected_page,
                "device": device_info.strip(),
                "steps": reproduction_steps.strip(),
                "expected": expected_result.strip(),
                "actual": actual_result.strip(),
                "notes": extra_notes.strip(),
                "now": now,
            },
        )
        st.success("Bug回報已提交。Developer 更新版本後會在此回覆修正狀態。")
        st.rerun()

st.divider()
st.subheader("我的回報")

reports = query_params(
    f"""
    SELECT id, affected_page, device_info, reproduction_steps, expected_result,
           actual_result, extra_notes, status, developer_reply, fixed_version,
           created_at, updated_at, resolved_at
    FROM {TABLE_BUG_REPORTS}
    WHERE reporter_user_id = :uid
    ORDER BY created_at DESC
    LIMIT 30
    """,
    {"uid": user_id},
)

if reports.empty:
    st.info("你暫時未提交任何 Bug 回報。")
else:
    for _, row in reports.iterrows():
        status = str(row.get("status") or "open")
        title = f"#{int(row['id'])} {row['affected_page']}｜{STATUS_LABELS.get(status, status)}"
        with st.expander(title, expanded=status not in ("fixed", "closed", "duplicate")):
            st.caption(f"提交時間：{_format_time(row['created_at'])}｜更新時間：{_format_time(row['updated_at'])}")
            if row.get("device_info"):
                st.write(f"**裝置及瀏覽器：** {row['device_info']}")
            st.write("**重現步驟**")
            st.write(row["reproduction_steps"])
            st.write("**預期結果**")
            st.write(row["expected_result"] or "未提供")
            st.write("**實際結果**")
            st.write(row["actual_result"])
            if row.get("extra_notes"):
                st.write("**補充資料**")
                st.write(row["extra_notes"])
            if row.get("fixed_version"):
                st.success(f"已於版本 {row['fixed_version']} 修正。")
            if row.get("developer_reply"):
                st.write("**Developer 回覆**")
                st.info(row["developer_reply"])
