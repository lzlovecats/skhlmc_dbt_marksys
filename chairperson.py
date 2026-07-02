import os
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

from functions import check_admin, get_score_data, get_best_debater_results, load_matches_from_db, render_page_guidance

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

tab = st.segmented_control(
    "功能",
    options=["開場白", "結語", "計時器"],
    default="開場白",
    label_visibility="collapsed",
)

if tab == "開場白":
    template_path = os.path.join(ASSETS_DIR, "chairperson_welcome.md")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        st.error("找不到開場白模板檔案。")
        st.stop()

    rendered = (
        template
        .replace("{match_id}", str(selected_match))
        .replace("{topic_text}", str(match_data.get("topic_text", "") or "（未設定辯題）"))
        .replace("{pro_team}", str(match_data.get("pro_team", "") or "（未設定）"))
        .replace("{con_team}", str(match_data.get("con_team", "") or "（未設定）"))
        .replace("{match_date}", str(match_data.get("match_date", "") or "（未設定）"))
        .replace("{match_time}", str(match_data.get("match_time", "") or "（未設定）"))
    )
    st.markdown(rendered)

elif tab == "結語":
    template_path = os.path.join(ASSETS_DIR, "chairperson_closing.md")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        st.error("找不到結語模板檔案。")
        st.stop()

    df_scores = get_score_data(match_id=selected_match)
    if df_scores is None or df_scores.empty:
        st.warning("尚未有評分紀錄，無法生成結語。請確認評判已完成提交。")
        st.stop()

    match_results = df_scores[df_scores["match_id"].astype(str) == str(selected_match)]
    if match_results.empty:
        st.warning("尚未有此場次的評分紀錄。")
        st.stop()

    pro_votes = int((match_results["pro_total_score"] > match_results["con_total_score"]).sum())
    con_votes = int((match_results["con_total_score"] > match_results["pro_total_score"]).sum())

    pro_team = str(match_data.get("pro_team", "") or "正方")
    con_team = str(match_data.get("con_team", "") or "反方")

    if pro_votes > con_votes:
        winner_text = f"勝方：正方 {pro_team}"
    elif con_votes > pro_votes:
        winner_text = f"勝方：反方 {con_team}"
    else:
        winner_text = "票數相同，須按賽規處理"

    df_final_best, best_one = get_best_debater_results(selected_match, match_results)
    if best_one is not None:
        best_debater = str(best_one["辯位"])
    else:
        best_debater = "（資料不足，暫時未能判定）"

    rendered = (
        template
        .replace("{pro_team}", pro_team)
        .replace("{con_team}", con_team)
        .replace("{pro_votes}", str(pro_votes))
        .replace("{con_votes}", str(con_votes))
        .replace("{winner_text}", winner_text)
        .replace("{best_debater}", best_debater)
    )
    st.markdown(rendered)

elif tab == "計時器":
    timer_html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: transparent; color: #333; }

.timer-container { padding: 12px; }

.stage-selector { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.stage-btn {
    flex: 1; min-width: 100px; padding: 10px 16px; border: 2px solid #ddd; border-radius: 8px;
    background: #f8f8f8; cursor: pointer; font-size: 15px; font-weight: 600;
    text-align: center; transition: all 0.2s;
}
.stage-btn:hover { border-color: #4a90d9; }
.stage-btn.active { background: #4a90d9; color: white; border-color: #4a90d9; }

.stopwatch-area { margin-top: 12px; }

.sw-display {
    font-size: 64px; font-weight: 700; text-align: center; padding: 20px;
    border-radius: 12px; margin: 8px 0; font-variant-numeric: tabular-nums;
    transition: background 0.3s, color 0.3s;
}
.sw-display.normal { background: #e8f5e9; color: #2e7d32; }
.sw-display.warning { background: #fff3e0; color: #e65100; }
.sw-display.overtime { background: #ffebee; color: #c62828; }

.sw-label { text-align: center; font-size: 14px; color: #666; margin-bottom: 4px; }

.btn-row { display: flex; gap: 8px; justify-content: center; margin-top: 12px; }
.btn {
    padding: 10px 28px; border: none; border-radius: 8px; font-size: 15px;
    font-weight: 600; cursor: pointer; transition: all 0.2s;
}
.btn-start { background: #4caf50; color: white; }
.btn-start:hover { background: #43a047; }
.btn-pause { background: #ff9800; color: white; }
.btn-pause:hover { background: #f57c00; }
.btn-reset { background: #9e9e9e; color: white; }
.btn-reset:hover { background: #757575; }
.btn-test { background: #2196f3; color: white; }
.btn-test:hover { background: #1976d2; }

.bell-schedule { margin-top: 16px; padding: 12px; background: #f5f5f5; border-radius: 8px; }
.bell-schedule h4 { margin-bottom: 8px; font-size: 14px; }
.bell-item { font-size: 13px; padding: 3px 0; color: #555; }
.bell-item.fired { color: #4caf50; font-weight: 600; }

.free-debate-wrapper { display: flex; gap: 16px; }
.free-debate-side { flex: 1; text-align: center; }
.free-debate-side h3 { margin-bottom: 8px; font-size: 16px; }
</style>
</head>
<body>
<div class="timer-container">
    <div class="stage-selector">
        <div class="stage-btn active" onclick="selectStage('main')" id="stage-main">主結辯</div>
        <div class="stage-btn" onclick="selectStage('deputy')" id="stage-deputy">一二副</div>
        <div class="stage-btn" onclick="selectStage('free')" id="stage-free">自由辯論</div>
    </div>

    <div style="text-align:center; margin-bottom:8px;">
        <button class="btn btn-test" onclick="testBell()">🔔 測試鈴聲</button>
    </div>

    <div id="single-timer" class="stopwatch-area">
        <div class="sw-label" id="single-label">主結辯計時</div>
        <div class="sw-display normal" id="single-display">0:00.0</div>
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
                <div class="sw-display normal" id="free-pro-display">0:00.0</div>
                <div class="btn-row">
                    <button class="btn btn-start" id="free-pro-btn" onclick="toggleFree('pro')">開始</button>
                    <button class="btn btn-reset" onclick="resetFree('pro')">重置</button>
                </div>
            </div>
            <div class="free-debate-side">
                <h3>反方</h3>
                <div class="sw-display normal" id="free-con-display">0:00.0</div>
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
const BELL_SCHEDULES = {
    main: [
        { t: 0, rings: 1, label: "開始 — 1 叮" },
        { t: 210, rings: 1, label: "3:30 — 1 叮" },
        { t: 240, rings: 2, label: "4:00 — 2 叮" },
        { t: 255, rings: 3, label: "4:15 — 3 叮" },
        { t: 280, rings: 5, label: "4:40 — 5 叮" },
    ],
    deputy: [
        { t: 0, rings: 1, label: "開始 — 1 叮" },
        { t: 150, rings: 1, label: "2:30 — 1 叮" },
        { t: 180, rings: 2, label: "3:00 — 2 叮" },
        { t: 195, rings: 3, label: "3:15 — 3 叮" },
        { t: 220, rings: 5, label: "3:40 — 5 叮" },
    ],
    free: [
        { t: 120, rings: 1, label: "2:00 — 1 叮" },
        { t: 150, rings: 2, label: "2:30 — 2 叮" },
    ],
};

const WARNING_TIMES = { main: 210, deputy: 150, free: 120 };
const OVERTIME_TIMES = { main: 240, deputy: 180, free: 150 };

let audioCtx = null;
function getAudioCtx() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
}

function playBell(count) {
    const ctx = getAudioCtx();
    for (let i = 0; i < count; i++) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = 800;
        gain.gain.setValueAtTime(0.5, ctx.currentTime + i * 0.3);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.3 + 0.2);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + i * 0.3);
        osc.stop(ctx.currentTime + i * 0.3 + 0.2);
    }
}

function testBell() { playBell(2); }

let currentStage = "main";

// Single timer state
let sRunning = false, sElapsed = 0, sStartTs = 0, sFired = new Set(), sRaf = 0;

// Free debate state
let fState = {
    pro: { running: false, elapsed: 0, startTs: 0, fired: new Set(), raf: 0 },
    con: { running: false, elapsed: 0, startTs: 0, fired: new Set(), raf: 0 },
};

function selectStage(stage) {
    resetSingle();
    resetFree("pro"); resetFree("con");
    currentStage = stage;
    document.querySelectorAll(".stage-btn").forEach(b => b.classList.remove("active"));
    document.getElementById("stage-" + stage).classList.add("active");
    if (stage === "free") {
        document.getElementById("single-timer").style.display = "none";
        document.getElementById("free-timer").style.display = "block";
        renderFreeBells();
    } else {
        document.getElementById("single-timer").style.display = "block";
        document.getElementById("free-timer").style.display = "none";
        document.getElementById("single-label").textContent = stage === "main" ? "主結辯計時" : "一二副計時";
        renderSingleBells();
    }
}

function fmtTime(sec) {
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    const d = Math.floor((sec * 10) % 10);
    return m + ":" + String(s).padStart(2, "0") + "." + d;
}

function getDisplayClass(sec, stage) {
    const w = WARNING_TIMES[stage], o = OVERTIME_TIMES[stage];
    if (sec >= o) return "overtime";
    if (sec >= w) return "warning";
    return "normal";
}

// ---- Single timer ----
function renderSingleBells() {
    const sched = BELL_SCHEDULES[currentStage];
    const el = document.getElementById("single-bells");
    el.innerHTML = "<h4>鈴聲時間表</h4>" + sched.map((b, i) =>
        '<div class="bell-item" id="sb-' + i + '">' + b.label + "</div>"
    ).join("");
}

function updateSingleDisplay() {
    const el = document.getElementById("single-display");
    el.textContent = fmtTime(sElapsed);
    el.className = "sw-display " + getDisplayClass(sElapsed, currentStage);
}

function singleTick() {
    if (!sRunning) return;
    sElapsed = (performance.now() - sStartTs) / 1000;
    updateSingleDisplay();
    const sched = BELL_SCHEDULES[currentStage];
    sched.forEach((b, i) => {
        if (!sFired.has(i) && sElapsed >= b.t) {
            sFired.add(i);
            playBell(b.rings);
            const bel = document.getElementById("sb-" + i);
            if (bel) bel.classList.add("fired");
        }
    });
    sRaf = requestAnimationFrame(singleTick);
}

function toggleSingle() {
    const btn = document.getElementById("single-start");
    if (sRunning) {
        sRunning = false;
        cancelAnimationFrame(sRaf);
        sElapsed = (performance.now() - sStartTs) / 1000;
        btn.textContent = "繼續";
        btn.className = "btn btn-start";
    } else {
        getAudioCtx();
        sRunning = true;
        sStartTs = performance.now() - sElapsed * 1000;
        btn.textContent = "暫停";
        btn.className = "btn btn-pause";
        singleTick();
    }
}

function resetSingle() {
    sRunning = false;
    cancelAnimationFrame(sRaf);
    sElapsed = 0;
    sFired.clear();
    updateSingleDisplay();
    document.getElementById("single-start").textContent = "開始";
    document.getElementById("single-start").className = "btn btn-start";
    renderSingleBells();
}

// ---- Free debate timers ----
function renderFreeBells() {
    const sched = BELL_SCHEDULES.free;
    const el = document.getElementById("free-bells");
    el.innerHTML = "<h4>鈴聲時間表（每邊）</h4>" + sched.map((b, i) =>
        '<div class="bell-item" id="fb-pro-' + i + '">正方 ' + b.label + '</div>' +
        '<div class="bell-item" id="fb-con-' + i + '">反方 ' + b.label + '</div>'
    ).join("");
}

function updateFreeDisplay(side) {
    const s = fState[side];
    const el = document.getElementById("free-" + side + "-display");
    el.textContent = fmtTime(s.elapsed);
    el.className = "sw-display " + getDisplayClass(s.elapsed, "free");
}

function freeTick(side) {
    const s = fState[side];
    if (!s.running) return;
    s.elapsed = (performance.now() - s.startTs) / 1000;
    updateFreeDisplay(side);
    const sched = BELL_SCHEDULES.free;
    sched.forEach((b, i) => {
        if (!s.fired.has(i) && s.elapsed >= b.t) {
            s.fired.add(i);
            playBell(b.rings);
            const bel = document.getElementById("fb-" + side + "-" + i);
            if (bel) bel.classList.add("fired");
        }
    });
    s.raf = requestAnimationFrame(() => freeTick(side));
}

function toggleFree(side) {
    const s = fState[side];
    const other = side === "pro" ? "con" : "pro";
    const btn = document.getElementById("free-" + side + "-btn");

    if (s.running) {
        s.running = false;
        cancelAnimationFrame(s.raf);
        s.elapsed = (performance.now() - s.startTs) / 1000;
        btn.textContent = "繼續";
        btn.className = "btn btn-start";
    } else {
        // Pause the other side (mutual exclusion)
        const os = fState[other];
        if (os.running) {
            os.running = false;
            cancelAnimationFrame(os.raf);
            os.elapsed = (performance.now() - os.startTs) / 1000;
            updateFreeDisplay(other);
            const obtn = document.getElementById("free-" + other + "-btn");
            obtn.textContent = "繼續";
            obtn.className = "btn btn-start";
        }
        getAudioCtx();
        s.running = true;
        s.startTs = performance.now() - s.elapsed * 1000;
        btn.textContent = "暫停";
        btn.className = "btn btn-pause";
        freeTick(side);
    }
}

function resetFree(side) {
    const s = fState[side];
    s.running = false;
    cancelAnimationFrame(s.raf);
    s.elapsed = 0;
    s.fired.clear();
    updateFreeDisplay(side);
    const btn = document.getElementById("free-" + side + "-btn");
    btn.textContent = "開始";
    btn.className = "btn btn-start";
    // Reset bell indicators for this side
    BELL_SCHEDULES.free.forEach((b, i) => {
        const bel = document.getElementById("fb-" + side + "-" + i);
        if (bel) bel.classList.remove("fired");
    });
}

// Init
renderSingleBells();
</script>
</body>
</html>
"""
    components.html(timer_html, height=520, scrolling=False)
