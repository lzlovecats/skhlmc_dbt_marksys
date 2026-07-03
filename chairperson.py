import base64
import json
import os
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

from functions import check_admin, get_score_data, get_best_debater_results, load_matches_from_db, query_params, render_page_guidance
from schema import TABLE_SCORE_DRAFTS

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

st.header("主席主持易")
render_page_guidance(
    [
        "選擇場次後，可使用開場白和結語模板，或使用計時器為各辯論環節計時。",
        "計時器會在指定時間自動響鈴提示，自由辯論環節兩邊計時互斥。",
        "結語需要評分資料，請確保評判已完成提交。",
    ],
)

if not check_admin():
    st.stop()

all_matches = load_matches_from_db()

if not all_matches:
    st.info("目前未有比賽場次。請先在「賽務管理易」建立場次。")
    st.stop()


def _sort_key(match_id):
    m = all_matches[match_id]
    date_str = m.get("match_date", "") or ""
    time_str = m.get("match_time", "") or ""
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        diff = abs((dt - datetime.now()).total_seconds())
    except (ValueError, TypeError):
        diff = 999999999
    return diff


sorted_ids = sorted(all_matches.keys(), key=_sort_key)

selected_match = st.selectbox(
    "選擇場次",
    options=sorted_ids,
    format_func=lambda mid: (
        f"{mid} — {all_matches[mid].get('pro_team', '')} vs {all_matches[mid].get('con_team', '')} "
        f"({all_matches[mid].get('match_date', '')} {all_matches[mid].get('match_time', '')})"
    ),
)

match_data = all_matches[selected_match]


def _clean_value(value, fallback=""):
    text = str(value or "").strip()
    if not text or text.lower() in ("nan", "nat"):
        return fallback
    return text


def _unique_names(names):
    result = []
    for name in names:
        clean_name = _clean_value(name)
        if clean_name and clean_name not in result:
            result.append(clean_name)
    return result


@st.cache_data(show_spinner=False)
def _load_bell_b64():
    bell_path = os.path.join(ASSETS_DIR, "bell.mp3")
    try:
        with open(bell_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except FileNotFoundError:
        return ""


def _render_manual_bell_bar(bell_b64):
    bell_src = f"data:audio/mpeg;base64,{bell_b64}" if bell_b64 else ""
    bell_html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body {{
    margin: 0; padding: 0; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: transparent;
}}
.bell-bar {{
    display: flex; align-items: center; justify-content: center; gap: 10px;
    padding: 8px 10px;
    background: rgba(18, 18, 18, 0.94);
    border: 1px solid #333;
    border-radius: 8px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.22);
}}
.bell-select {{
    background: #2a2a2a; color: #e0e0e0; border: 1px solid #444; border-radius: 8px;
    padding: 10px 12px; font-size: 15px; cursor: pointer;
}}
.btn-bell {{
    background: #ff4b4b; color: white; border: none; border-radius: 8px;
    font-size: 18px; font-weight: 700; padding: 11px 32px;
    cursor: pointer; transition: background 0.2s;
}}
.btn-bell:hover {{ background: #e03e3e; }}
</style>
</head>
<body>
<div class="bell-bar">
    <select id="manual-bell-count" class="bell-select">
        <option value="1">1 叮</option>
        <option value="2">2 叮</option>
        <option value="3">3 叮</option>
        <option value="5">5 叮</option>
    </select>
    <button class="btn-bell" onclick="manualBell()">🔔 響叮</button>
</div>
<script>
try {{
    const frame = window.frameElement;
    if (frame) {{
        let el = frame;
        while (el && el !== document.documentElement) {{
            const ov = getComputedStyle(el).overflow;
            if (ov !== "visible") el.style.overflow = "visible";
            el = el.parentElement;
        }}
        const host = frame.closest('[data-testid="stElementContainer"]') || frame.parentElement;
        if (host) {{
            host.style.position = "sticky";
            host.style.top = "0";
            host.style.zIndex = "9999";
        }}
        frame.style.display = "block";
    }}
}} catch(e) {{}}

const BELL_SRC = "{bell_src}";
const BELL_VOLUME = 5.0;
let bellBuffer = null;
let bellLoading = null;
let audioCtx = null;

function getAudioCtx() {{
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
}}

async function loadBell() {{
    if (!BELL_SRC) return null;
    if (!bellLoading) {{
        bellLoading = fetch(BELL_SRC)
            .then(resp => resp.arrayBuffer())
            .then(arr => getAudioCtx().decodeAudioData(arr))
            .then(buffer => {{ bellBuffer = buffer; return buffer; }})
            .catch(() => null);
    }}
    return bellLoading;
}}

function playTone(count) {{
    const ctx = getAudioCtx();
    for (let i = 0; i < count; i++) {{
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = 850;
        gain.gain.setValueAtTime(0.45, ctx.currentTime + i * 0.35);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.35 + 0.22);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + i * 0.35);
        osc.stop(ctx.currentTime + i * 0.35 + 0.22);
    }}
}}

async function playBell(count) {{
    getAudioCtx();
    await loadBell();
    if (!bellBuffer) {{
        playTone(count);
        return;
    }}
    const ctx = getAudioCtx();
    for (let i = 0; i < count; i++) {{
        const src = ctx.createBufferSource();
        const gain = ctx.createGain();
        src.buffer = bellBuffer;
        gain.gain.value = BELL_VOLUME;
        src.connect(gain);
        gain.connect(ctx.destination);
        src.start(ctx.currentTime + i * 0.8);
    }}
}}

function manualBell() {{
    const count = parseInt(document.getElementById("manual-bell-count").value);
    playBell(count);
}}
</script>
</body>
</html>
"""
    components.html(bell_html, height=64, scrolling=False)


positions = ["主辯", "一副", "二副", "結辯"]
db_pro = [_clean_value(match_data.get(f"pro_{i}", "")) for i in range(1, 5)]
db_con = [_clean_value(match_data.get(f"con_{i}", "")) for i in range(1, 5)]

df_scores = get_score_data(match_id=selected_match)
match_results = None
if df_scores is not None and not df_scores.empty:
    match_results = df_scores[df_scores["match_id"].astype(str) == str(selected_match)]
    if match_results.empty:
        match_results = None

db_judge_names = []
if match_results is not None:
    db_judge_names.extend(match_results["judge_name"].tolist())

try:
    draft_judges = query_params(
        f"SELECT DISTINCT judge_name FROM {TABLE_SCORE_DRAFTS} WHERE match_id = :match_id ORDER BY judge_name",
        {"match_id": selected_match},
    )
    if not draft_judges.empty:
        db_judge_names.extend(draft_judges["judge_name"].tolist())
except Exception:
    pass

db_judge_names = _unique_names(db_judge_names)

with st.expander("📝 填寫資料"):
    fc1, fc2 = st.columns(2)
    with fc1:
        chairman_name = st.text_input("主席姓名", key="cp_chairman")
    with fc2:
        timekeeper_name = st.text_input("計時員姓名", key="cp_timekeeper")

    default_judges = "\n".join(f"- {n}" for n in db_judge_names) if db_judge_names else "- 稱謂：姓名\n- 稱謂：姓名\n- 稱謂：姓名"
    judges_key_suffix = abs(hash(default_judges))
    judges_text = st.text_area(
        "評判名單（每行一位，格式：- 稱謂：姓名）",
        value=default_judges,
        height=110,
        key=f"cp_judges_{selected_match}_{judges_key_suffix}",
    )

    st.markdown("**正方辯員**")
    pc1, pc2, pc3, pc4 = st.columns(4)
    pro_debaters = []
    for col, pos, default in zip([pc1, pc2, pc3, pc4], positions, db_pro):
        with col:
            pro_debaters.append(st.text_input(pos, value=default, key=f"cp_pro_{selected_match}_{pos}_{default}"))

    st.markdown("**反方辯員**")
    cc1, cc2, cc3, cc4 = st.columns(4)
    con_debaters = []
    for col, pos, default in zip([cc1, cc2, cc3, cc4], positions, db_con):
        with col:
            con_debaters.append(st.text_input(pos, value=default, key=f"cp_con_{selected_match}_{pos}_{default}"))

bell_b64 = _load_bell_b64()
_render_manual_bell_bar(bell_b64)

tab = st.segmented_control(
    "功能",
    options=["開場讀稿易", "結尾完結易", "叮叮易"],
    default="開場讀稿易",
    key="cp_selected_tab",
    label_visibility="collapsed",
    width="stretch",
)

topic_text = _clean_value(match_data.get("topic_text", ""), "（未設定辯題）")
pro_team = _clean_value(match_data.get("pro_team", ""), "（未設定）")
con_team = _clean_value(match_data.get("con_team", ""), "（未設定）")
match_date = _clean_value(match_data.get("match_date", ""), "（未設定）")
match_time = _clean_value(match_data.get("match_time", ""), "（未設定）")

judge_lines = [l.strip() for l in judges_text.strip().split("\n") if l.strip()]
judge_count = len(judge_lines)
judge_list_text = "\n".join(judge_lines) if judge_lines else "- 稱謂：姓名"
judge_comment_names = [l.lstrip("-").strip() for l in judge_lines]

pro_debater_lines = "\n".join(
    f"- {pos}：{name or '______'}" for pos, name in zip(positions, pro_debaters)
)
con_debater_lines = "\n".join(
    f"- {pos}：{name or '______'}" for pos, name in zip(positions, con_debaters)
)
first_speaker = f"正方主辯 {pro_debaters[0]}" if pro_debaters[0] else "正方主辯"

if tab == "開場讀稿易":
    template_path = os.path.join(ASSETS_DIR, "chairperson_welcome.md")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        st.error("找不到開場白模板檔案。")
        st.stop()

    rendered = template
    template_values = {
        "match_id": selected_match,
        "match_date": match_date,
        "match_time": match_time,
        "chairman_name": chairman_name or "______",
        "timekeeper_name": timekeeper_name or "______",
        "judge_count": judge_count,
        "judge_list": judge_list_text,
        "topic_text": topic_text,
        "pro_team": pro_team,
        "con_team": con_team,
        "pro_debaters": pro_debater_lines,
        "con_debaters": con_debater_lines,
        "first_speaker": first_speaker,
    }
    for key, value in template_values.items():
        rendered = rendered.replace("{" + key + "}", str(value))

    st.markdown(rendered)

elif tab == "結尾完結易":
    if st.button("🔄 重新整理"):
        st.rerun()

    template_path = os.path.join(ASSETS_DIR, "chairperson_closing.md")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        st.error("找不到結語模板檔案。")
        st.stop()

    if match_results is None:
        st.warning("尚未有評分紀錄，無法生成結語。請確認評判已完成提交。")
        st.stop()

    pro_votes = int((match_results["pro_total_score"] > match_results["con_total_score"]).sum())
    con_votes = int((match_results["con_total_score"] > match_results["pro_total_score"]).sum())
    draw_votes = int((match_results["pro_total_score"] == match_results["con_total_score"]).sum())

    if pro_votes > con_votes:
        winner_text = f"今日嘅勝方係正方 {pro_team}"
    elif con_votes > pro_votes:
        winner_text = f"今日嘅勝方係反方 {con_team}"
    else:
        winner_text = "今日雙方票數相同，須按賽規處理"

    df_final_best, best_one = get_best_debater_results(selected_match, match_results)
    if best_one is not None:
        best_debater = str(best_one["辯位"])
    else:
        best_debater = "（資料不足，暫時未能判定）"

    if judge_comment_names:
        judge_comment_lines = "\n\n".join(
            f"有請 {name} 對今場比賽作出評價。多謝 {name}。"
            for name in judge_comment_names
        )
    else:
        judge_comment_lines = "有請各位評判對今場比賽作出評價。"

    vote_summary = f"今日正方 {pro_team} 得票為 {pro_votes} 票，反方 {con_team} 得票為 {con_votes} 票"
    if draw_votes > 0:
        vote_summary += f"，另有 {draw_votes} 位評判給予同分"
    vote_summary += "。"

    rendered = template
    template_values = {
        "judge_comment_lines": judge_comment_lines,
        "best_debater": best_debater,
        "vote_summary": vote_summary,
        "winner_text": winner_text,
        "pro_team": pro_team,
        "con_team": con_team,
        "pro_votes": pro_votes,
        "con_votes": con_votes,
        "draw_votes": draw_votes,
    }
    for key, value in template_values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    st.markdown(rendered)

elif tab == "叮叮易":
    debate_format = st.selectbox("賽制", options=["校園隨想", "聯中", "星島"], key="cp_timer_format")
    free_debate_minutes = 5
    if debate_format == "聯中":
        free_debate_minutes = st.number_input(
            "自由辯論時間（每邊，分鐘）",
            min_value=2,
            max_value=10,
            value=5,
            step=1,
            key="cp_lz_free_minutes",
        )

    if debate_format == "星島":
        timer_stages = [
            ("main", "主辯一二副"),
            ("deputy", "結辯"),
            ("prep", "交互準備"),
            ("question", "交互問"),
            ("answer", "交互答"),
        ]
        bell_schedules = {
            "main": [
                {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
                {"t": 120, "rings": 1, "label": "2:00 — 1 叮"},
                {"t": 150, "rings": 2, "label": "2:30 — 2 叮"},
            ],
            "deputy": [
                {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
                {"t": 180, "rings": 1, "label": "3:00 — 1 叮"},
                {"t": 210, "rings": 2, "label": "3:30 — 2 叮"},
            ],
            "prep": [
                {"t": 0, "rings": 1, "label": "準備開始 — 1 叮"},
                {"t": 15, "rings": 2, "label": "0:15 — 2 叮"},
            ],
            "question": [
                {"t": 0, "rings": 1, "label": "問開始 — 1 叮"},
                {"t": 20, "rings": 2, "label": "0:20 — 2 叮"},
            ],
            "answer": [
                {"t": 0, "rings": 1, "label": "答開始 — 1 叮"},
                {"t": 40, "rings": 2, "label": "0:40 — 2 叮"},
            ],
        }
        warning_times = {"main": 120, "deputy": 180, "prep": None, "question": None, "answer": None}
        overtime_times = {"main": 150, "deputy": 210, "prep": 15, "question": 20, "answer": 40}
    elif debate_format == "聯中":
        timer_stages = [("main", "主結辯"), ("deputy", "一二副"), ("free", "自由辯論")]
        free_warning_time = int(free_debate_minutes) * 60 - 30
        free_overtime = int(free_debate_minutes) * 60
        bell_schedules = {
            "main": [
                {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
                {"t": 270, "rings": 1, "label": "4:30 — 1 叮"},
                {"t": 300, "rings": 2, "label": "5:00 — 2 叮"},
                {"t": 315, "rings": 3, "label": "5:15 — 3 叮"},
                {"t": 340, "rings": 5, "label": "5:40 — 5 叮"},
            ],
            "deputy": [
                {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
                {"t": 210, "rings": 1, "label": "3:30 — 1 叮"},
                {"t": 240, "rings": 2, "label": "4:00 — 2 叮"},
                {"t": 255, "rings": 3, "label": "4:15 — 3 叮"},
                {"t": 280, "rings": 5, "label": "4:40 — 5 叮"},
            ],
            "free": [
                {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
                {"t": free_warning_time, "rings": 1, "label": "完結前 30 秒 — 1 叮"},
                {"t": free_overtime, "rings": 2, "label": "時間到 — 2 叮"},
            ],
        }
        warning_times = {"main": 270, "deputy": 210, "free": free_warning_time}
        overtime_times = {"main": 300, "deputy": 240, "free": free_overtime}
    else:
        timer_stages = [("main", "主結辯"), ("deputy", "一二副"), ("free", "自由辯論")]
        bell_schedules = {
            "main": [
                {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
                {"t": 210, "rings": 1, "label": "3:30 — 1 叮"},
                {"t": 240, "rings": 2, "label": "4:00 — 2 叮"},
                {"t": 255, "rings": 3, "label": "4:15 — 3 叮"},
                {"t": 280, "rings": 5, "label": "4:40 — 5 叮"},
            ],
            "deputy": [
                {"t": 0, "rings": 1, "label": "開始 — 1 叮"},
                {"t": 150, "rings": 1, "label": "2:30 — 1 叮"},
                {"t": 180, "rings": 2, "label": "3:00 — 2 叮"},
                {"t": 195, "rings": 3, "label": "3:15 — 3 叮"},
                {"t": 220, "rings": 5, "label": "3:40 — 5 叮"},
            ],
            "free": [
                {"t": 120, "rings": 1, "label": "2:00 — 1 叮"},
                {"t": 150, "rings": 2, "label": "2:30 — 2 叮"},
            ],
        }
        warning_times = {"main": 210, "deputy": 150, "free": 120}
        overtime_times = {"main": 240, "deputy": 180, "free": 150}

    stage_buttons_html = "\n".join(
        f"""        <div class="stage-btn{' active' if idx == 0 else ''}" onclick="selectStage('{stage_id}')" id="stage-{stage_id}">{stage_label}</div>"""
        for idx, (stage_id, stage_label) in enumerate(timer_stages)
    )
    stage_labels = {stage_id: stage_label for stage_id, stage_label in timer_stages}
    main_stage_label = stage_labels["main"]
    stage_labels_json = json.dumps(stage_labels, ensure_ascii=False)
    bell_schedules_json = json.dumps(bell_schedules, ensure_ascii=False)
    warning_times_json = json.dumps(warning_times, ensure_ascii=False)
    overtime_times_json = json.dumps(overtime_times, ensure_ascii=False)
    bell_src = f"data:audio/mpeg;base64,{bell_b64}" if bell_b64 else ""
    timer_html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: transparent; color: #e0e0e0; }}

.timer-container {{ padding: 12px; }}

.stage-selector {{
    display: flex; gap: 0; margin: 0 0 20px;
    border: 1px solid #444; border-radius: 8px; overflow: hidden;
}}
.stage-btn {{
    flex: 1; padding: 12px 0; border: none;
    background: #2a2a2a; cursor: pointer; font-size: 15px; font-weight: 600;
    text-align: center; transition: all 0.2s; color: #aaa;
    border-right: 1px solid #444;
}}
.stage-btn:last-child {{ border-right: none; }}
.stage-btn:hover {{ background: #333; color: #ddd; }}
.stage-btn.active {{ background: #ff4b4b; color: white; }}

.stopwatch-area {{ margin-top: 12px; }}

.sw-display {{
    font-size: 64px; font-weight: 700; text-align: center; padding: 20px;
    border-radius: 12px; margin: 8px 0; font-variant-numeric: tabular-nums;
    transition: background 0.3s, color 0.3s;
}}
.sw-display.normal {{ background: #1a3a1a; color: #4caf50; }}
.sw-display.warning {{ background: #3a2a0a; color: #ff9800; }}
.sw-display.overtime {{ background: #3a1a1a; color: #f44336; }}

.sw-label {{ text-align: center; font-size: 14px; color: #888; margin-bottom: 4px; }}

.btn-row {{ display: flex; gap: 8px; justify-content: center; margin-top: 12px; }}
.btn {{
    padding: 10px 28px; border: none; border-radius: 8px; font-size: 15px;
    font-weight: 600; cursor: pointer; transition: all 0.2s;
}}
.btn-start {{ background: #4caf50; color: white; }}
.btn-start:hover {{ background: #43a047; }}
.btn-pause {{ background: #ff9800; color: white; }}
.btn-pause:hover {{ background: #f57c00; }}
.btn-reset {{ background: #555; color: #ddd; }}
.btn-reset:hover {{ background: #666; }}
.bell-schedule {{ margin-top: 16px; padding: 12px; background: #1e1e1e; border: 1px solid #333; border-radius: 8px; }}
.bell-schedule h4 {{ margin-bottom: 8px; font-size: 14px; color: #aaa; }}
.bell-item {{ font-size: 13px; padding: 3px 0; color: #777; }}
.bell-item.fired {{ color: #4caf50; font-weight: 600; }}

.free-debate-wrapper {{ display: flex; gap: 16px; }}
.free-debate-side {{ flex: 1; text-align: center; }}
.free-debate-side h3 {{ margin-bottom: 8px; font-size: 16px; color: #ccc; }}
</style>
</head>
<body>
<div class="timer-container">
    <div class="stage-selector">
{stage_buttons_html}
    </div>

    <div id="single-timer" class="stopwatch-area">
        <div class="sw-label" id="single-label">{main_stage_label}計時</div>
        <div class="sw-display normal" id="single-display">0:00:00</div>
        <div class="btn-row">
            <button class="btn btn-start" id="single-start" onclick="toggleSingle()">開始</button>
            <button class="btn btn-reset" onclick="resetSingle()">重置</button>
        </div>
        <div class="bell-schedule" id="single-bells"></div>
    </div>

    <div id="free-timer" class="stopwatch-area" style="display:none;">
        <div class="free-debate-wrapper">
            <div class="free-debate-side">
                <h3>正方</h3>
                <div class="sw-display normal" id="free-pro-display">0:00:00</div>
                <div class="btn-row">
                    <button class="btn btn-start" id="free-pro-btn" onclick="toggleFree('pro')">開始</button>
                    <button class="btn btn-reset" onclick="resetFree('pro')">重置</button>
                </div>
            </div>
            <div class="free-debate-side">
                <h3>反方</h3>
                <div class="sw-display normal" id="free-con-display">0:00:00</div>
                <div class="btn-row">
                    <button class="btn btn-start" id="free-con-btn" onclick="toggleFree('con')">開始</button>
                    <button class="btn btn-reset" onclick="resetFree('con')">重置</button>
                </div>
            </div>
        </div>
        <div class="bell-schedule" id="free-bells"></div>
    </div>
</div>

<script>
const BELL_SRC = "{bell_src}";
const BELL_VOLUME = 10.0;
let bellBuffer = null;
let audioCtx = null;

function getAudioCtx() {{
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
}}

function playTone(count) {{
    const ctx = getAudioCtx();
    for (let i = 0; i < count; i++) {{
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = 850;
        gain.gain.setValueAtTime(0.45, ctx.currentTime + i * 0.35);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.35 + 0.22);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + i * 0.35);
        osc.stop(ctx.currentTime + i * 0.35 + 0.22);
    }}
}}

(async function loadBell() {{
    if (!BELL_SRC) return;
    const ctx = getAudioCtx();
    try {{
        const resp = await fetch(BELL_SRC);
        const arr = await resp.arrayBuffer();
        bellBuffer = await ctx.decodeAudioData(arr);
    }} catch (e) {{
        bellBuffer = null;
    }}
}})();

function playBell(count) {{
    const ctx = getAudioCtx();
    if (!bellBuffer) {{
        playTone(count);
        return;
    }}
    for (let i = 0; i < count; i++) {{
        const src = ctx.createBufferSource();
        const gain = ctx.createGain();
        src.buffer = bellBuffer;
        gain.gain.value = BELL_VOLUME;
        src.connect(gain);
        gain.connect(ctx.destination);
        src.start(ctx.currentTime + i * 0.8);
    }}
}}

const BELL_SCHEDULES = {bell_schedules_json};
const STAGE_LABELS = {stage_labels_json};
const WARNING_TIMES = {warning_times_json};
const OVERTIME_TIMES = {overtime_times_json};

let currentStage = "main";
let sRunning = false, sElapsed = 0, sStartTs = 0, sFired = new Set(), sRaf = 0;
let fState = {{
    pro: {{ running: false, elapsed: 0, startTs: 0, fired: new Set(), raf: 0 }},
    con: {{ running: false, elapsed: 0, startTs: 0, fired: new Set(), raf: 0 }},
}};

function selectStage(stage) {{
    resetSingle();
    resetFree("pro"); resetFree("con");
    currentStage = stage;
    document.querySelectorAll(".stage-btn").forEach(b => b.classList.remove("active"));
    document.getElementById("stage-" + stage).classList.add("active");
    if (stage === "free") {{
        document.getElementById("single-timer").style.display = "none";
        document.getElementById("free-timer").style.display = "block";
        renderFreeBells();
    }} else {{
        document.getElementById("single-timer").style.display = "block";
        document.getElementById("free-timer").style.display = "none";
        document.getElementById("single-label").textContent = (STAGE_LABELS[stage] || "") + "計時";
        renderSingleBells();
    }}
}}

function fmtTime(sec) {{
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    const cs = Math.floor((sec * 100) % 100);
    return m + ":" + String(s).padStart(2, "0") + ":" + String(cs).padStart(2, "0");
}}

function getDisplayClass(sec, stage) {{
    const w = WARNING_TIMES[stage], o = OVERTIME_TIMES[stage];
    if (o !== null && o !== undefined && sec >= o) return "overtime";
    if (w !== null && w !== undefined && sec >= w) return "warning";
    return "normal";
}}

function renderSingleBells() {{
    const sched = BELL_SCHEDULES[currentStage];
    const el = document.getElementById("single-bells");
    el.innerHTML = "<h4>鈴聲時間表</h4>" + sched.map((b, i) =>
        '<div class="bell-item" id="sb-' + i + '">' + b.label + "</div>"
    ).join("");
}}

function updateSingleDisplay() {{
    const el = document.getElementById("single-display");
    el.textContent = fmtTime(sElapsed);
    el.className = "sw-display " + getDisplayClass(sElapsed, currentStage);
}}

function singleTick() {{
    if (!sRunning) return;
    sElapsed = (performance.now() - sStartTs) / 1000;
    updateSingleDisplay();
    const sched = BELL_SCHEDULES[currentStage];
    sched.forEach((b, i) => {{
        if (!sFired.has(i) && sElapsed >= b.t) {{
            sFired.add(i);
            playBell(b.rings);
            const bel = document.getElementById("sb-" + i);
            if (bel) bel.classList.add("fired");
        }}
    }});
    sRaf = requestAnimationFrame(singleTick);
}}

function toggleSingle() {{
    const btn = document.getElementById("single-start");
    if (sRunning) {{
        sRunning = false;
        cancelAnimationFrame(sRaf);
        sElapsed = (performance.now() - sStartTs) / 1000;
        btn.textContent = "繼續";
        btn.className = "btn btn-start";
    }} else {{
        getAudioCtx();
        sRunning = true;
        sStartTs = performance.now() - sElapsed * 1000;
        btn.textContent = "暫停";
        btn.className = "btn btn-pause";
        singleTick();
    }}
}}

function resetSingle() {{
    sRunning = false;
    cancelAnimationFrame(sRaf);
    sElapsed = 0;
    sFired.clear();
    updateSingleDisplay();
    document.getElementById("single-start").textContent = "開始";
    document.getElementById("single-start").className = "btn btn-start";
    renderSingleBells();
}}

function renderFreeBells() {{
    const sched = BELL_SCHEDULES.free || [];
    const el = document.getElementById("free-bells");
    el.innerHTML = "<h4>鈴聲時間表（每邊）</h4>" + sched.map((b, i) =>
        '<div class="bell-item" id="fb-pro-' + i + '">正方 ' + b.label + '</div>' +
        '<div class="bell-item" id="fb-con-' + i + '">反方 ' + b.label + '</div>'
    ).join("");
}}

function updateFreeDisplay(side) {{
    const s = fState[side];
    const el = document.getElementById("free-" + side + "-display");
    el.textContent = fmtTime(s.elapsed);
    el.className = "sw-display " + getDisplayClass(s.elapsed, "free");
}}

function freeTick(side) {{
    const s = fState[side];
    if (!s.running) return;
    s.elapsed = (performance.now() - s.startTs) / 1000;
    updateFreeDisplay(side);
    const sched = BELL_SCHEDULES.free;
    sched.forEach((b, i) => {{
        if (!s.fired.has(i) && s.elapsed >= b.t) {{
            s.fired.add(i);
            playBell(b.rings);
            const bel = document.getElementById("fb-" + side + "-" + i);
            if (bel) bel.classList.add("fired");
        }}
    }});
    s.raf = requestAnimationFrame(() => freeTick(side));
}}

function toggleFree(side) {{
    const s = fState[side];
    const other = side === "pro" ? "con" : "pro";
    const btn = document.getElementById("free-" + side + "-btn");

    if (s.running) {{
        s.running = false;
        cancelAnimationFrame(s.raf);
        s.elapsed = (performance.now() - s.startTs) / 1000;
        btn.textContent = "繼續";
        btn.className = "btn btn-start";
    }} else {{
        const os = fState[other];
        if (os.running) {{
            os.running = false;
            cancelAnimationFrame(os.raf);
            os.elapsed = (performance.now() - os.startTs) / 1000;
            updateFreeDisplay(other);
            const obtn = document.getElementById("free-" + other + "-btn");
            obtn.textContent = "繼續";
            obtn.className = "btn btn-start";
        }}
        getAudioCtx();
        s.running = true;
        s.startTs = performance.now() - s.elapsed * 1000;
        btn.textContent = "暫停";
        btn.className = "btn btn-pause";
        freeTick(side);
    }}
}}

function resetFree(side) {{
    if (!BELL_SCHEDULES.free) return;
    const s = fState[side];
    s.running = false;
    cancelAnimationFrame(s.raf);
    s.elapsed = 0;
    s.fired.clear();
    updateFreeDisplay(side);
    const btn = document.getElementById("free-" + side + "-btn");
    btn.textContent = "開始";
    btn.className = "btn btn-start";
    BELL_SCHEDULES.free.forEach((b, i) => {{
        const bel = document.getElementById("fb-" + side + "-" + i);
        if (bel) bel.classList.remove("fired");
    }});
}}

renderSingleBells();
</script>
</body>
</html>
"""
    components.html(timer_html, height=520, scrolling=False)
