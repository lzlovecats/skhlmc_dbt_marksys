DEBATE_FORMATS = ["校園隨想", "聯中", "星島", "基本法盃"]


def _bell(t, rings, label):
    return {"t": t, "rings": rings, "label": label}


def _closing_prep_schedule(closing_prep_minutes):
    seconds = int(float(closing_prep_minutes or 2) * 60)
    return [
        _bell(0, 1, "準備開始 — 1 叮"),
        _bell(seconds, 2, f"{seconds // 60}:{seconds % 60:02d} — 2 叮"),
    ], seconds


def get_debate_timer_config(debate_format, free_debate_minutes=None, closing_prep_minutes=None):
    closing_prep_schedule, closing_prep_seconds = _closing_prep_schedule(closing_prep_minutes)

    if debate_format == "星島":
        timer_stages = [
            ("main", "主辯一二副"),
            ("deputy", "結辯"),
            ("prep", "交互準備"),
            ("question", "交互問"),
            ("answer", "交互答"),
            ("closing_prep", "結辯準備"),
        ]
        bell_schedules = {
            "main": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(120, 1, "2:00 — 1 叮"),
                _bell(150, 2, "2:30 — 2 叮"),
            ],
            "deputy": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(180, 1, "3:00 — 1 叮"),
                _bell(210, 2, "3:30 — 2 叮"),
            ],
            "prep": [
                _bell(0, 1, "準備開始 — 1 叮"),
                _bell(15, 2, "0:15 — 2 叮"),
            ],
            "question": [
                _bell(0, 1, "問開始 — 1 叮"),
                _bell(20, 2, "0:20 — 2 叮"),
            ],
            "answer": [
                _bell(0, 1, "答開始 — 1 叮"),
                _bell(40, 2, "0:40 — 2 叮"),
            ],
            "closing_prep": closing_prep_schedule,
        }
        warning_times = {
            "main": 120,
            "deputy": 180,
            "prep": None,
            "question": None,
            "answer": None,
            "closing_prep": None,
        }
        overtime_times = {
            "main": 150,
            "deputy": 210,
            "prep": 15,
            "question": 20,
            "answer": 40,
            "closing_prep": closing_prep_seconds,
        }
    elif debate_format == "聯中":
        free_overtime = round(float(free_debate_minutes or 5) * 60)
        free_warning_time = free_overtime - 30
        timer_stages = [
            ("main", "主結辯"),
            ("deputy", "一二副"),
            ("free", "自由辯論"),
            ("closing_prep", "結辯準備"),
            ("floor_question", "問台下"),
            ("floor_prep", "台下準備"),
            ("floor_answer", "答台下"),
        ]
        bell_schedules = {
            "main": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(270, 1, "4:30 — 1 叮"),
                _bell(300, 2, "5:00 — 2 叮"),
                _bell(315, 3, "5:15 — 3 叮"),
                _bell(340, 5, "5:40 — 5 叮"),
            ],
            "deputy": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(210, 1, "3:30 — 1 叮"),
                _bell(240, 2, "4:00 — 2 叮"),
                _bell(255, 3, "4:15 — 3 叮"),
                _bell(280, 5, "4:40 — 5 叮"),
            ],
            "free": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(free_warning_time, 1, "完結前 30 秒 — 1 叮"),
                _bell(free_overtime, 2, "時間到 — 2 叮"),
            ],
            "closing_prep": closing_prep_schedule,
            "floor_question": [
                _bell(0, 1, "問開始 — 1 叮"),
                _bell(30, 1, "0:30 — 1 叮"),
                _bell(60, 2, "1:00 — 2 叮"),
            ],
            "floor_prep": [
                _bell(0, 1, "準備開始 — 1 叮"),
                _bell(60, 2, "1:00 — 2 叮"),
            ],
            "floor_answer": [
                _bell(0, 1, "答開始 — 1 叮"),
                _bell(30, 1, "0:30 — 1 叮"),
                _bell(60, 2, "1:00 — 2 叮"),
            ],
        }
        warning_times = {
            "main": 270,
            "deputy": 210,
            "free": free_warning_time,
            "closing_prep": None,
            "floor_question": 30,
            "floor_prep": None,
            "floor_answer": 30,
        }
        overtime_times = {
            "main": 300,
            "deputy": 240,
            "free": free_overtime,
            "closing_prep": closing_prep_seconds,
            "floor_question": 60,
            "floor_prep": 60,
            "floor_answer": 60,
        }
    elif debate_format == "基本法盃":
        timer_stages = [
            ("main", "主結辯"),
            ("deputy", "一二副"),
            ("closing_prep", "結辯準備"),
        ]
        bell_schedules = {
            "main": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(210, 1, "3:30 — 1 叮"),
                _bell(240, 2, "4:00 — 2 叮"),
                _bell(255, 3, "4:15 — 3 叮"),
            ],
            "deputy": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(150, 1, "2:30 — 1 叮"),
                _bell(180, 2, "3:00 — 2 叮"),
                _bell(195, 3, "3:15 — 3 叮"),
            ],
            "closing_prep": closing_prep_schedule,
        }
        warning_times = {"main": 210, "deputy": 150, "closing_prep": None}
        overtime_times = {"main": 240, "deputy": 180, "closing_prep": closing_prep_seconds}
    else:
        timer_stages = [
            ("main", "主結辯"),
            ("deputy", "一二副"),
            ("free", "自由辯論"),
            ("closing_prep", "結辯準備"),
        ]
        bell_schedules = {
            "main": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(210, 1, "3:30 — 1 叮"),
                _bell(240, 2, "4:00 — 2 叮"),
                _bell(255, 3, "4:15 — 3 叮"),
                _bell(280, 5, "4:40 — 5 叮"),
            ],
            "deputy": [
                _bell(0, 1, "開始 — 1 叮"),
                _bell(150, 1, "2:30 — 1 叮"),
                _bell(180, 2, "3:00 — 2 叮"),
                _bell(195, 3, "3:15 — 3 叮"),
                _bell(220, 5, "3:40 — 5 叮"),
            ],
            "free": [
                _bell(120, 1, "2:00 — 1 叮"),
                _bell(150, 2, "2:30 — 2 叮"),
            ],
            "closing_prep": closing_prep_schedule,
        }
        warning_times = {"main": 210, "deputy": 150, "free": 120, "closing_prep": None}
        overtime_times = {"main": 240, "deputy": 180, "free": 150, "closing_prep": closing_prep_seconds}

    return {
        "timer_stages": timer_stages,
        "bell_schedules": bell_schedules,
        "warning_times": warning_times,
        "overtime_times": overtime_times,
    }


# 完整 Mock：每個賽制嘅有序流程。結辯準備時間按賽制固定（校園隨想2/聯中3/星島1/基本法盃2 分）。
_MOCK_CLOSING_PREP_MINUTES = {"校園隨想": 2.0, "聯中": 3.0, "星島": 1.0, "基本法盃": 2.0}


def _mock_segment(seg_id, label, side, bells):
    seconds = bells[-1]["t"] if bells else 0
    return {"id": seg_id, "label": label, "side": side, "seconds": seconds, "bells": bells}


def get_full_mock_sequence(debate_format, free_debate_minutes=None):
    """回傳完整 Mock 嘅有序段落 list，每段 {id,label,side,seconds,bells}。

    台上發言／台下問答／交互答問嘅鈴聲全部重用 get_debate_timer_config 嘅 bell schedule，
    唔另外硬寫秒數。side 取值：正方 / 反方 / 雙方 / 準備。
    """
    debate_format = str(debate_format or "校園隨想").strip() or "校園隨想"
    config = get_debate_timer_config(
        debate_format,
        free_debate_minutes=free_debate_minutes,
        closing_prep_minutes=_MOCK_CLOSING_PREP_MINUTES.get(debate_format, 2.0),
    )
    bells = config["bell_schedules"]
    segments = []

    def add(seg_id, label, side, stage):
        segments.append(_mock_segment(seg_id, label, side, bells[stage]))

    if debate_format == "星島":
        # 主辯 + 一二副 全部用 "main"（2:30）；結辯用 "deputy"（3:30）。
        add("main_pro", "正方主辯", "正方", "main")
        add("main_con", "反方主辯", "反方", "main")
        for n, cn in ((1, "一"), (2, "二")):
            add(f"dep{n}_pro", f"正方{cn}副", "正方", "main")
            add(f"dep{n}_con", f"反方{cn}副", "反方", "main")
        # 交互答問 x6：每次 準備發問→問→準備回答→答。
        # 第 1/3/5 次 正方問·反方答；第 2/4/6 次 反方問·正方答。
        for r in range(1, 7):
            asker = "正方" if r % 2 == 1 else "反方"
            answerer = "反方" if r % 2 == 1 else "正方"
            add(f"cx{r}_prep_q", f"交互答問 第{r}次 · 準備發問", "準備", "prep")
            add(f"cx{r}_q", f"交互答問 第{r}次 · {asker}問", asker, "question")
            add(f"cx{r}_prep_a", f"交互答問 第{r}次 · 準備回答", "準備", "prep")
            add(f"cx{r}_a", f"交互答問 第{r}次 · {answerer}答", answerer, "answer")
        add("closing_prep", "結辯準備", "準備", "closing_prep")
        add("closing_con", "反方結辯", "反方", "deputy")
        add("closing_pro", "正方結辯", "正方", "deputy")
    elif debate_format == "聯中":
        add("main_pro", "正方主辯", "正方", "main")
        add("main_con", "反方主辯", "反方", "main")
        for n, cn in ((1, "一"), (2, "二"), (3, "三")):
            add(f"dep{n}_pro", f"正方{cn}副", "正方", "deputy")
            add(f"dep{n}_con", f"反方{cn}副", "反方", "deputy")
        # 台下問答 x3：每次 正方問→反方答→反方問→正方答。
        for r in range(1, 4):
            add(f"floor{r}_pro_q", f"台下問答 第{r}次 · 正方問", "正方", "floor_question")
            add(f"floor{r}_con_a", f"台下問答 第{r}次 · 反方答", "反方", "floor_answer")
            add(f"floor{r}_con_q", f"台下問答 第{r}次 · 反方問", "反方", "floor_question")
            add(f"floor{r}_pro_a", f"台下問答 第{r}次 · 正方答", "正方", "floor_answer")
        add("free", "自由辯論（每邊）", "雙方", "free")
        add("closing_prep", "結辯準備", "準備", "closing_prep")
        add("closing_con", "反方結辯", "反方", "main")
        add("closing_pro", "正方結辯", "正方", "main")
    elif debate_format == "基本法盃":
        add("main_pro", "正方主辯", "正方", "main")
        add("main_con", "反方主辯", "反方", "main")
        for n, cn in ((1, "一"), (2, "二")):
            add(f"dep{n}_pro", f"正方{cn}副", "正方", "deputy")
            add(f"dep{n}_con", f"反方{cn}副", "反方", "deputy")
        add("closing_prep", "結辯準備", "準備", "closing_prep")
        add("closing_con", "反方結辯", "反方", "main")
        add("closing_pro", "正方結辯", "正方", "main")
    else:  # 校園隨想
        add("main_pro", "正方主辯", "正方", "main")
        add("main_con", "反方主辯", "反方", "main")
        for n, cn in ((1, "一"), (2, "二")):
            add(f"dep{n}_pro", f"正方{cn}副", "正方", "deputy")
            add(f"dep{n}_con", f"反方{cn}副", "反方", "deputy")
        add("free", "自由辯論（每邊）", "雙方", "free")
        add("closing_prep", "結辯準備", "準備", "closing_prep")
        add("closing_con", "反方結辯", "反方", "main")
        add("closing_pro", "正方結辯", "正方", "main")

    return segments


def full_mock_total_seconds(segments):
    """估算 Mock 全長（秒）。雙方段落（自由辯論）兩邊都計。"""
    total = 0
    for seg in segments:
        secs = seg.get("seconds", 0)
        total += secs * 2 if seg.get("side") == "雙方" else secs
    return total


# 對應 Gemini Live 純語音單次連線 15 分鐘上限，留安全邊際。
MOCK_SESSION_BUDGET_SECONDS = 780


def _mock_chapter(seg_id):
    if seg_id in ("main_pro", "main_con"):
        return "主辯"
    if seg_id.startswith("dep"):
        return "一二三副"
    if seg_id.startswith(("floor", "cx", "free")):
        return "台下或交互"
    return "結辯"


def split_mock_into_sessions(segments, budget_seconds=MOCK_SESSION_BUDGET_SECONDS):
    """將完整 Mock 段落拆成多個 Live session，每節 ≤ budget、唔跨 chapter。

    回傳 [{chapter, label, segments:[...], planned_seconds}]。同一 chapter 若因超 budget
    拆成多節，label 會標「(1/2)」等。每節 planned_seconds 用嚟逐節估成本。
    """
    raw = []
    cur = []
    cur_sec = 0
    cur_ch = None
    for seg in segments:
        ch = _mock_chapter(seg["id"])
        if cur and (ch != cur_ch or cur_sec + seg["seconds"] > budget_seconds):
            raw.append((cur_ch, cur, cur_sec))
            cur = []
            cur_sec = 0
        cur.append(seg)
        cur_sec += seg["seconds"]
        cur_ch = ch
    if cur:
        raw.append((cur_ch, cur, cur_sec))

    ch_totals = {}
    for ch, _, _ in raw:
        ch_totals[ch] = ch_totals.get(ch, 0) + 1
    ch_seen = {}
    sessions = []
    for ch, segs, sec in raw:
        if ch_totals[ch] > 1:
            ch_seen[ch] = ch_seen.get(ch, 0) + 1
            label = f"{ch}（{ch_seen[ch]}/{ch_totals[ch]}）"
        else:
            label = ch
        sessions.append({
            "chapter": ch,
            "label": label,
            "segments": segs,
            "planned_seconds": sec,
        })
    return sessions
