import base64
import csv
import datetime
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from auth import require_committee
from functions import (
    clear_field_draft,
    ensure_tts_recording_tables,
    execute_query,
    get_system_config,
    query_params,
)
from schema import (
    TABLE_LLM_TRAINING_SUBMISSIONS,
    TABLE_TTS_VOICE_CONSENTS,
    TABLE_TTS_VOICE_RECORDINGS,
    TABLE_TTS_SCRIPTS,
)
from speech_recorder_component import render_speech_recorder
from prompts import (
    LLM_TEXT_REVIEW_SYSTEM_PROMPT,
    TTS_AUDIO_REVIEW_SYSTEM_PROMPT,
    TTS_COVERAGE_SYSTEM_PROMPT,
    build_llm_text_review_prompt,
    build_tts_audio_review_prompt,
    build_tts_coverage_prompt,
)


CONSENT_VERSION = "tts_voice_v1_2026_07"
CONSENT_TEXT = """我同意聖呂中辯收集本人在本頁提交的錄音，用作廣東話 TTS（文字轉語音）、讀音檢查及相關 AI 研究測試。錄音可能用於分析本人聲線及建立語音模型。我明白可向開發者要求撤回未來使用授權。"""

ALLOWED_USERS_CONFIG_KEY = "tts_recording_allowed_users"
REVIEWERS_CONFIG_KEY = "tts_recording_reviewers"
TTS_AUDIO_REVIEW_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
LLM_TEXT_REVIEW_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
RD_PLAN_PATH = Path(__file__).parent / "assets" / "tts_rd_plan.md"
REVIEW_PAGE_SIZE = 5
LLM_DATA_TYPES = [
    "發言稿",
    "自由辯論逐字稿",
    "完整 Mock 逐字稿",
    "評語樣本",
    "攻防問答",
    "主線/策略",
    "辯題資料",
    "其他",
]
LLM_SIDE_OPTIONS = ["不適用", "正方", "反方", "中立/評判"]


# ---------------------------------------------------------------------------
# 研發計劃書（委員及管理員均可查閱）
# ---------------------------------------------------------------------------
def _load_rd_plan():
    try:
        return RD_PLAN_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return "研發計劃書暫時未能讀取，請檢查 `assets/tts_rd_plan.md`。"


# ---------------------------------------------------------------------------
# 錄音審核標準（管理員面板顯示）
# ---------------------------------------------------------------------------
REVIEW_STANDARDS_MD = """
#### 錄音審核標準（管理員必讀）

審核目標：**確保納入訓練集的每一段錄音都清晰、讀音正確、內容與稿件一致**。AI 只作初步預檢，**最終由管理員把關**。請逐條試聽，符合以下全部條件才「接受」：

**一、內容正確**
- 朗讀內容與指定稿句一致，沒有漏字、加字或讀錯字。
- 多音字、數字、日期、英文術語讀法正確（例：`銀行` 讀 hong4、`重新` 讀 cung4、`DSE` 逐個字母讀）。

**二、聲音乾淨**
- 背景**安靜**：沒有明顯人聲、冷氣、風扇、鍵盤聲或迴音。
- **沒有爆咪／破音**（clipping）：音量過大會失真，聽到明顯破音即拒絕。
- 音量**適中**：音量過小或過大均不適合納入訓練集。

**三、口條自然**
- 語速正常、咬字清楚，不應過快含糊或過慢生硬。
- 停頓自然，沒有無故長時間靜音、明顯口誤或重錄接駁痕跡。

**四、技術規格**
- 長度合理（約 1–60 秒，過短或過長都不理想）。
- 單一講者、由頭到尾一致（不應中途換人或換錄音環境）。

**判斷準則**
- ✅ **接受**：以上四項全部符合。
- ❌ **拒絕**：任何一項明顯不符，請在**審核備註**寫明原因（例：「背景有冷氣聲」「『長遠』讀錯音」「尾段爆咪」），讓委員知道如何改善。
- 🤔 **有疑問**：情況模稜兩可時，寧可拒絕並要求重錄，不應降低標準 —— **資料質素直接決定模型質素**。
""".strip()


# ---------------------------------------------------------------------------
# LLM 文字資料審核標準（管理員面板顯示）
# ---------------------------------------------------------------------------
LLM_REVIEW_STANDARDS_MD = """
#### LLM 文字資料審核標準（管理員必讀）

審核目標：**只接受能改善辯論 LLM / RAG 的高質、已授權、已匿名化文字資料**。AI 只作初步預檢，最終仍由管理員判斷。

**一、資料用途清晰**
- 內容應屬於發言稿、自由辯論逐字稿、完整 Mock 逐字稿、評語樣本、攻防問答、主線策略或辯題資料。
- 應盡量附有辯題、立場、環節、來源或使用情境，方便日後分類和檢索。

**二、內容質素足夠**
- 發言稿和策略資料應邏輯清楚、論點完整、有例子或推論。
- 評語樣本應具體引用表現，並提出可操作的改善建議。
- 逐字稿應盡量整理停頓、追問、回應關係；過度零碎或未整理內容不宜接受。
- 文字內容應主要使用**粵語口語**撰寫，方便日後訓練出符合校隊辯論語氣的 LLM；少量辯論術語、英文詞或必要書面詞可接受。

**三、私隱與授權**
- 不可包含真名、電話、班別、私人對話、未授權學生資料或其他可識別個人資料。
- 提交者必須確認有權提交作聖呂中辯內部 AI 訓練 / RAG 測試用途。
- 如內容涉及其他隊員或外部比賽資料，管理員應按實際情況審慎處理。

**四、判斷準則**
- ✅ **接受**：內容有明確辯論訓練價值、已匿名化、來源合理、質素足夠，並主要以粵語口語撰寫。
- ❌ **拒絕**：內容含敏感個人資料、授權不清、資料太碎、與辯論訓練無關、質素不足，或主要使用書面中文／普通話式中文而非粵語口語。
- 🤔 **有疑問**：可先拒絕並在審核備註要求提交者補充來源、匿名化或重新整理。
""".strip()


DEFAULT_SCRIPT_BANK = [
    {"id": "free_001", "category": "Free De", "text": "你呢個講法最大問題係冇證明因果關係。"},
    {"id": "free_002", "category": "Free De", "text": "我想追問你，政策成本由邊個承擔？"},
    {"id": "free_003", "category": "Free De", "text": "如果你承認有例外，咁你個標準其實已經唔穩陣。"},
    {"id": "mock_001", "category": "Mock", "text": "多謝主席，各位評判、各位同學，今日我方立場非常清晰。"},
    {"id": "mock_002", "category": "Mock", "text": "總結我方三個重點，第一係可行性，第二係公平性，第三係長遠影響。"},
    {"id": "mock_003", "category": "Mock", "text": "對方一直避開核心問題，就係制度本身會否製造更大不公。"},
    {"id": "question_001", "category": "追問", "text": "你可唔可以畀一個具體例子，證明呢個方法真係有效？"},
    {"id": "question_002", "category": "追問", "text": "如果資源有限，你會優先幫邊一類人，點解？"},
    {"id": "rebuttal_001", "category": "反駁", "text": "對方將相關性講成因果性，呢個係明顯嘅邏輯跳步。"},
    {"id": "rebuttal_002", "category": "反駁", "text": "你嘅例子只係個別情況，唔足以支持一個普遍政策。"},
    {"id": "numbers_001", "category": "數字讀法", "text": "二零二六年，我哋預計有百分之三十五嘅學生受影響。"},
    {"id": "numbers_002", "category": "數字讀法", "text": "如果滿分係一百分，呢個方案最多只可以攞六十五分。"},
    {"id": "terms_002", "category": "術語/英文", "text": "自由辯論最重要唔係講得長，而係追問要準、反駁要快。"},
    {"id": "feedback_001", "category": "評語", "text": "你頭先嘅主線清楚，但回應對方追問時可以再直接啲。"},
    {"id": "feedback_002", "category": "評語", "text": "整體台風穩陣，不過個別位收得太急，畀評判嘅印象會扣分。"},
    {"id": "feedback_003", "category": "評語", "text": "你嘅論點有數據支持，值得欣賞，下次記得同時交代數據嚟源。"},
    # 數字讀法：百分比、分數、年份、序數、電話式數字
    {"id": "numbers_003", "category": "數字讀法", "text": "呢場比賽最後比數係四十八比五十二，我哋以四分之差落敗。"},
    {"id": "numbers_004", "category": "數字讀法", "text": "報名人數由二百三十七人升到一千零五人，升幅超過三倍。"},
    {"id": "numbers_005", "category": "數字讀法", "text": "第一、第二同第三名分別攞到九十五、八十八同八十一分。"},
    {"id": "numbers_006", "category": "數字讀法", "text": "聯絡電話係二五二八，三六七九，有問題可以隨時致電查詢。"},
    # 日期時間：年月日、星期、時分
    {"id": "date_001", "category": "日期時間", "text": "決賽定於二零二六年七月十九號，星期日下晝三點半喺禮堂舉行。"},
    {"id": "date_002", "category": "日期時間", "text": "報名截止日期係下個月八號，逾期恕不受理，請各位隊伍準時提交。"},
    {"id": "date_003", "category": "日期時間", "text": "每節限時四分三十秒，夠三分鐘會響第一次鈴，夠鐘就響兩下。"},
    # 術語／英文：縮寫逐字母、中英夾雜
    {"id": "terms_004", "category": "術語/英文", "text": "OK，我哋而家開始 free debate 環節，計時交由 timer 負責。"},
    {"id": "terms_005", "category": "術語/英文", "text": "呢個 argument 嘅 logic 有斷層，你需要補返個 example 先撐得住。"},
    {"id": "terms_006", "category": "術語/英文", "text": "AI 辯論易會用 GPT 同 Gemini 兩個模型，分別做評語同即時回應。"},
    # 多音字：同一個字不同讀法，在句中出現
    {"id": "poly_001", "category": "多音字", "text": "佢嘅行為好有問題，但銀行嗰行細字就冇人為意。"},
    {"id": "poly_002", "category": "多音字", "text": "呢點好重要，所以我哋要重新檢視成個制度嘅設計。"},
    {"id": "poly_003", "category": "多音字", "text": "校長話長遠嚟講，同學嘅成長比一時嘅長短更加關鍵。"},
    {"id": "poly_004", "category": "多音字", "text": "呢部分嘅分數唔高，但佢反映嘅身分認同問題就唔可以忽視。"},
    {"id": "poly_005", "category": "多音字", "text": "佢好奇點解一個好人會做出咁嘅選擇，我覺得值得深究。"},
    {"id": "poly_006", "category": "多音字", "text": "快樂同音樂表面相似，實際上係兩種完全唔同嘅體驗。"},
    # 聲調覆蓋：短句集中唔同聲調，練清晰度
    {"id": "tone_001", "category": "聲調覆蓋", "text": "詩、史、試、時、市、事，呢六個字聲調各有不同，要讀得分明。"},
    {"id": "tone_002", "category": "聲調覆蓋", "text": "三分鐘、九十九分、五十蚊、一百萬，數字讀音要清清楚楚。"},
    # 長句韻律：一口氣較長，練停頓同氣息
    {"id": "prosody_001", "category": "長句韻律", "text": "各位評判、各位老師、各位同學，多謝大家喺一個咁繁忙嘅星期日，抽時間出席今日呢場意義重大嘅辯論比賽。"},
    {"id": "prosody_002", "category": "長句韻律", "text": "我方認為，無論係從公平、效率，定係從長遠嘅社會影響嚟睇，呢個政策都應該經過更充分嘅諮詢先至推行。"},
    {"id": "prosody_003", "category": "長句韻律", "text": "如果我哋只係睇短期數字，好容易忽略咗背後真正需要幫助嘅人，而呢啲人往往就係最冇聲音嗰班。"},
]


def _now_hk():
    return datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)


def _parse_json_list(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _to_bytes(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return bytes(value)


def _script_by_id(scripts, script_id):
    for script in scripts:
        if script["id"] == script_id:
            return script
    return scripts[0] if scripts else {"id": script_id, "category": "", "text": ""}


# ---------------------------------------------------------------------------
# 句庫（tts_scripts）讀寫：管理員可增／改／停用
# ---------------------------------------------------------------------------
def _seed_scripts_if_empty():
    rows = query_params(f"SELECT COUNT(*) AS n FROM {TABLE_TTS_SCRIPTS}")
    try:
        already = int(rows.iloc[0]["n"]) > 0
    except Exception:
        already = False
    if already:
        return
    for order, item in enumerate(DEFAULT_SCRIPT_BANK):
        execute_query(
            f"""
            INSERT INTO {TABLE_TTS_SCRIPTS} (script_id, category, text, is_active, sort_order, created_by)
            VALUES (:script_id, :category, :text, TRUE, :sort_order, 'system')
            ON CONFLICT (script_id) DO NOTHING
            """,
            {
                "script_id": item["id"],
                "category": item["category"],
                "text": item["text"],
                "sort_order": order,
            },
        )


def _load_scripts(active_only=True):
    where = "WHERE is_active = TRUE" if active_only else ""
    rows = query_params(
        f"""
        SELECT script_id, category, text, is_active, sort_order
        FROM {TABLE_TTS_SCRIPTS}
        {where}
        ORDER BY category, sort_order, script_id
        """
    )
    scripts = []
    for _, row in rows.iterrows():
        scripts.append({
            "id": row["script_id"],
            "category": row["category"],
            "text": row["text"],
            "is_active": bool(row["is_active"]),
            "sort_order": int(row["sort_order"] or 0),
        })
    return scripts


def _next_script_id(category):
    prefix = re.sub(r"[^a-z0-9]+", "_", str(category or "script").lower()).strip("_") or "script"
    rows = query_params(
        f"SELECT script_id FROM {TABLE_TTS_SCRIPTS} WHERE script_id LIKE :like",
        {"like": f"{prefix}\\_%"},
    )
    max_n = 0
    for _, row in rows.iterrows():
        match = re.search(rf"^{re.escape(prefix)}_(\d+)$", str(row["script_id"]))
        if match:
            max_n = max(max_n, int(match.group(1)))
    return f"{prefix}_{max_n + 1:03d}"


def _upsert_script(script_id, category, text_value, created_by, sort_order=0):
    execute_query(
        f"""
        INSERT INTO {TABLE_TTS_SCRIPTS} (script_id, category, text, is_active, sort_order, created_by, updated_at)
        VALUES (:script_id, :category, :text, TRUE, :sort_order, :created_by, :now)
        ON CONFLICT (script_id) DO UPDATE SET
            category = EXCLUDED.category,
            text = EXCLUDED.text,
            updated_at = EXCLUDED.updated_at
        """,
        {
            "script_id": script_id,
            "category": category.strip(),
            "text": text_value.strip(),
            "sort_order": int(sort_order or 0),
            "created_by": created_by,
            "now": _now_hk(),
        },
    )


def _set_script_active(script_id, is_active):
    execute_query(
        f"UPDATE {TABLE_TTS_SCRIPTS} SET is_active = :active, updated_at = :now WHERE script_id = :script_id",
        {"active": bool(is_active), "script_id": script_id, "now": _now_hk()},
    )


def _recording_counts_by_category(scripts):
    """每個類別已有幾多段 accepted / pending 錄音（供 AI 缺口分析）。"""
    script_to_cat = {s["id"]: s["category"] for s in scripts}
    rows = query_params(
        f"""
        SELECT script_id, status, COUNT(*) AS n
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        WHERE status IN ('accepted', 'pending')
        GROUP BY script_id, status
        """
    )
    counts = {}
    for _, row in rows.iterrows():
        cat = script_to_cat.get(row["script_id"], "（其他）")
        bucket = counts.setdefault(cat, {"accepted": 0, "pending": 0})
        bucket[row["status"]] = bucket.get(row["status"], 0) + int(row["n"])
    return counts


def _audio_ext(mime_type):
    if "wav" in str(mime_type or "").lower():
        return "wav"
    if "webm" in str(mime_type or "").lower():
        return "webm"
    return "audio"


def _active_consent(user_id):
    rows = query_params(
        f"""
        SELECT consented_at
        FROM {TABLE_TTS_VOICE_CONSENTS}
        WHERE user_id = :user_id
          AND consent_version = :version
          AND withdrawn_at IS NULL
        """,
        {"user_id": user_id, "version": CONSENT_VERSION},
    )
    return not rows.empty


def _record_consent(user_id):
    execute_query(
        f"""
        INSERT INTO {TABLE_TTS_VOICE_CONSENTS} (
            user_id, consent_version, consent_text, consented_at, withdrawn_at
        )
        VALUES (:user_id, :version, :text, :now, NULL)
        ON CONFLICT (user_id, consent_version) DO UPDATE SET
            consent_text = EXCLUDED.consent_text,
            consented_at = EXCLUDED.consented_at,
            withdrawn_at = NULL
        """,
        {
            "user_id": user_id,
            "version": CONSENT_VERSION,
            "text": CONSENT_TEXT,
            "now": _now_hk(),
        },
    )


def _withdraw_consent(user_id):
    execute_query(
        f"""
        UPDATE {TABLE_TTS_VOICE_CONSENTS}
        SET withdrawn_at = :now
        WHERE user_id = :user_id
          AND consent_version = :version
          AND withdrawn_at IS NULL
        """,
        {"user_id": user_id, "version": CONSENT_VERSION, "now": _now_hk()},
    )
    execute_query(
        f"""
        UPDATE {TABLE_TTS_VOICE_RECORDINGS}
        SET status = 'withdrawn'
        WHERE speaker_user_id = :user_id
          AND status != 'withdrawn'
        """,
        {"user_id": user_id},
    )


def _extract_json(text_value):
    text_value = str(text_value or "").strip()
    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?", "", text_value).strip()
        text_value = re.sub(r"```$", "", text_value).strip()
    try:
        return json.loads(text_value)
    except Exception:
        match = re.search(r"\{.*\}", text_value, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def _is_ai_quota_error(error):
    error_text = str(error or "")
    return "429" in error_text or "RESOURCE_EXHAUSTED" in error_text or "quota" in error_text.lower()


def review_tts_recording_audio(audio_bytes, prompt_text):
    if "GEMINI_API_KEY" not in st.secrets:
        return {
            "ok": False,
            "status": "error",
            "message": "未設定 GEMINI_API_KEY，未能進行 AI 音質預檢。",
            "review": None,
        }
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return {
            "ok": False,
            "status": "error",
            "message": "Gemini SDK 尚未安裝，未能進行 AI 音質預檢。",
            "review": None,
        }

    client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    last_quota_error = None
    for model_name in TTS_AUDIO_REVIEW_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(text=build_tts_audio_review_prompt(prompt_text)),
                            types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                        ],
                    )
                ],
                config=types.GenerateContentConfig(
                    system_instruction=TTS_AUDIO_REVIEW_SYSTEM_PROMPT,
                    temperature=0,
                ),
            )
            review = _extract_json(response.text or "{}")
            passed = bool(review.get("passed"))
            required_ok = (
                passed
                and review.get("speech_clarity") == "clear"
                and review.get("volume") == "ok"
                and review.get("noise_level") in ("low", "medium")
                and not bool(review.get("clipping"))
                and bool(review.get("matches_prompt"))
            )
            review["passed"] = bool(required_ok)
            review["model"] = model_name
            return {
                "ok": True,
                "status": "passed" if required_ok else "failed",
                "message": review.get("reason") or ("通過" if required_ok else "未通過"),
                "review": review,
            }
        except Exception as e:
            if _is_ai_quota_error(e):
                last_quota_error = e
                continue
            return {
                "ok": False,
                "status": "error",
                "message": f"AI 音質預檢失敗：{e}",
                "review": None,
            }

    if last_quota_error is not None:
        return {
            "ok": False,
            "status": "error",
            "error_type": "quota",
            "message": "AI 使用量或速率已達上限，今次可先提交錄音，稍後由管理員人手審核。",
            "review": None,
        }

    return {
        "ok": False,
        "status": "error",
        "message": "AI 音質預檢失敗，請稍後再試。",
        "review": None,
    }


def analyze_script_coverage(scripts, counts):
    """讓 AI 分析現有句庫和錄音覆蓋，指出仍需收集的內容，並建議新句子。"""
    if "GEMINI_API_KEY" not in st.secrets:
        return {"ok": False, "message": "未設定 GEMINI_API_KEY，未能進行 AI 缺口分析。", "analysis": None}
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return {"ok": False, "message": "Gemini SDK 尚未安裝，未能進行 AI 缺口分析。", "analysis": None}

    lines = []
    for s in scripts:
        c = counts.get(s["category"], {})
        lines.append(f"[{s['category']}] {s['id']}｜accepted={c.get('accepted', 0)}｜pending={c.get('pending', 0)}｜{s['text']}")
    bank_summary = "\n".join(lines) if lines else "（句庫為空）"

    try:
        client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=build_tts_coverage_prompt(bank_summary))])],
            config=types.GenerateContentConfig(system_instruction=TTS_COVERAGE_SYSTEM_PROMPT, temperature=0.4),
        )
        analysis = _extract_json(response.text or "{}")
        return {"ok": True, "message": "分析完成。", "analysis": analysis}
    except Exception as e:
        return {"ok": False, "message": f"AI 缺口分析失敗：{e}", "analysis": None}


def review_llm_training_text(data_type, side, title, topic_text, source_note, content_text):
    if "GEMINI_API_KEY" not in st.secrets:
        return {
            "ok": False,
            "status": "error",
            "message": "未設定 GEMINI_API_KEY，未能進行 AI 文字資料預檢。",
            "review": None,
        }
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return {
            "ok": False,
            "status": "error",
            "message": "Gemini SDK 尚未安裝，未能進行 AI 文字資料預檢。",
            "review": None,
        }

    client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    last_quota_error = None
    prompt = build_llm_text_review_prompt(data_type, side, title, topic_text, source_note, content_text)
    for model_name in LLM_TEXT_REVIEW_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(
                    system_instruction=LLM_TEXT_REVIEW_SYSTEM_PROMPT,
                    temperature=0,
                ),
            )
            review = _extract_json(response.text or "{}")
            passed = bool(review.get("passed"))
            required_ok = (
                passed
                and review.get("relevance") in ("high", "medium")
                and review.get("quality") in ("good", "usable")
                and review.get("anonymization") == "ok"
                and review.get("permission_risk") in ("low", "medium")
            )
            review["passed"] = bool(required_ok)
            review["model"] = model_name
            return {
                "ok": True,
                "status": "passed" if required_ok else "failed",
                "message": review.get("reason") or ("通過" if required_ok else "未通過"),
                "review": review,
            }
        except Exception as e:
            if _is_ai_quota_error(e):
                last_quota_error = e
                continue
            return {
                "ok": False,
                "status": "error",
                "message": f"AI 文字資料預檢失敗：{e}",
                "review": None,
            }

    if last_quota_error is not None:
        return {
            "ok": False,
            "status": "error",
            "error_type": "quota",
            "message": "AI 使用量或速率已達上限，今次可先提交資料，稍後由管理員人手審核。",
            "review": None,
        }

    return {
        "ok": False,
        "status": "error",
        "message": "AI 文字資料預檢失敗，請稍後再試。",
        "review": None,
    }


def _insert_recording(user_id, script, audio_bytes, audio_data, review, ai_review_status="passed"):
    mime_type = audio_data.get("mime_type") or "audio/wav"
    execute_query(
        f"""
        INSERT INTO {TABLE_TTS_VOICE_RECORDINGS} (
            speaker_user_id, script_id, prompt_text, audio_data, mime_type,
            file_ext, size_bytes, duration_seconds, ai_review_status,
            ai_review_json, ai_transcript, status, created_at
        )
        VALUES (
            :speaker_user_id, :script_id, :prompt_text, :audio_data, :mime_type,
            :file_ext, :size_bytes, :duration_seconds, :ai_review_status,
            :ai_review_json, :ai_transcript, 'pending', :created_at
        )
        """,
        {
            "speaker_user_id": user_id,
            "script_id": script["id"],
            "prompt_text": script["text"],
            "audio_data": audio_bytes,
            "mime_type": mime_type,
            "file_ext": _audio_ext(mime_type),
            "size_bytes": int(audio_data.get("size") or len(audio_bytes)),
            "duration_seconds": int(audio_data.get("duration_seconds") or 0),
            "ai_review_status": ai_review_status,
            "ai_review_json": json.dumps(review, ensure_ascii=False),
            "ai_transcript": review.get("transcript") or "",
            "created_at": _now_hk(),
        },
    )


def _record_key(script_id, audio_data):
    return f"{script_id}:{audio_data.get('recorded_at')}:{audio_data.get('size')}"


def _render_recorder(user_id, all_scripts):
    if not all_scripts:
        st.info("句庫暫時未有可錄音句子，請聯絡管理員。")
        return
    categories = sorted({s["category"] for s in all_scripts})
    selected_category = st.selectbox("句子類別", options=categories)
    scripts = [s for s in all_scripts if s["category"] == selected_category]
    script_id = st.selectbox(
        "錄音句子",
        options=[s["id"] for s in scripts],
        format_func=lambda sid: _script_by_id(scripts, sid)["text"],
    )
    script = _script_by_id(scripts, script_id)

    st.markdown(f"**請照讀：** {script['text']}")
    audio_data = render_speech_recorder(
        key=f"tts_recording_{script_id}",
        output_format="wav",
    )
    if not audio_data or not audio_data.get("audio_base64"):
        st.info("請先錄音。")
        return

    try:
        audio_bytes = base64.b64decode(audio_data["audio_base64"])
    except Exception:
        st.error("錄音資料無法讀取，請重新錄音。")
        return

    duration = int(audio_data.get("duration_seconds") or 0)
    size = int(audio_data.get("size") or len(audio_bytes))
    st.audio(audio_bytes, format=audio_data.get("mime_type") or "audio/wav")
    st.caption(f"錄音長度：約 {duration} 秒｜大小：約 {round(size / 1024)} KB")

    if duration < 1:
        st.warning("錄音太短，請重新錄音。")
        return
    if duration > 60:
        st.warning("錄音太長，請控制在 60 秒內。")
        return
    if size > 10 * 1024 * 1024:
        st.warning("錄音太大，請重新錄短一點。")
        return

    key = _record_key(script_id, audio_data)
    if st.button("AI 檢查音質", type="primary", use_container_width=True):
        with st.spinner("AI 正在檢查錄音音質..."):
            result = review_tts_recording_audio(audio_bytes, script["text"])
        st.session_state["tts_recording_ai_review"] = {
            "key": key,
            "result": result,
        }

    cached = st.session_state.get("tts_recording_ai_review") or {}
    if cached.get("key") != key:
        return

    result = cached.get("result") or {}
    review = result.get("review") or {}
    if result.get("status") == "passed":
        st.success("AI 預檢通過，可以提交入待審核資料集。")
        if review.get("transcript"):
            st.caption(f"AI 聽到：{review['transcript']}")
        if st.button("提交入待審核資料集", type="primary", use_container_width=True):
            _insert_recording(user_id, script, audio_bytes, audio_data, review)
            st.session_state.pop("tts_recording_ai_review", None)
            st.success("錄音已提交，等待人工審核。")
            st.rerun()
    elif result.get("status") == "failed":
        st.error(f"AI 預檢未通過：{result.get('message') or '請重新錄音。'}")
        if review:
            st.json(review)
    elif result.get("status") == "error":
        message = result.get("message") or "AI 預檢暫時未能完成。"
        st.warning(f"{message}\n\n你可以自行確認錄音合適後，略過 AI 檢查直接提交，交由管理員人手審核。")
        manual_confirm = st.checkbox(
            "我確認呢段錄音清晰、讀音正確、內容與稿件一致",
            key=f"tts_manual_confirm_{key}",
        )
        if st.button(
            "略過 AI 檢查並提交待人工審核",
            type="primary",
            disabled=not manual_confirm,
            use_container_width=True,
        ):
            fallback_review = {
                "passed": False,
                "reason": message,
                "error_type": result.get("error_type") or "unavailable",
                "manual_confirmed": True,
            }
            _insert_recording(user_id, script, audio_bytes, audio_data, fallback_review, ai_review_status="error")
            st.session_state.pop("tts_recording_ai_review", None)
            st.success("錄音已提交，等待人工審核。")
            st.rerun()


def _render_my_records(user_id):
    rows = query_params(
        f"""
        SELECT id, script_id, prompt_text, status, ai_transcript, review_note, created_at
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        WHERE speaker_user_id = :user_id
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"user_id": user_id},
    )
    st.subheader("我的提交紀錄")
    if rows.empty:
        st.info("你暫時未提交任何錄音。")
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_review_panel(user_id):
    st.divider()
    st.subheader("錄音審核 / Export（管理員）")
    with st.expander("📋 審核標準（審核前必讀）", expanded=False):
        st.markdown(REVIEW_STANDARDS_MD)

    _render_recording_status_summary()
    _render_all_recording_status_table()

    status_filter = st.selectbox("狀態", ["pending", "accepted", "rejected", "withdrawn"])
    total_rows = _count_recordings(status_filter)
    total_pages = max(1, (total_rows + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page_key = f"tts_review_page_{status_filter}"
    page = st.number_input(
        "頁數",
        min_value=1,
        max_value=total_pages,
        value=min(int(st.session_state.get(page_key, 1)), total_pages),
        step=1,
        key=page_key,
    )
    offset = (int(page) - 1) * REVIEW_PAGE_SIZE
    st.caption(f"共 {total_rows} 段 {status_filter} 錄音｜每頁 {REVIEW_PAGE_SIZE} 段｜第 {int(page)} / {total_pages} 頁")

    rows = query_params(
        f"""
        SELECT id, speaker_user_id, script_id, prompt_text, audio_data, mime_type,
               file_ext, size_bytes, duration_seconds, ai_review_json,
               ai_transcript, status, review_note, created_at
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        WHERE status = :status
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
        """,
        {"status": status_filter, "limit": REVIEW_PAGE_SIZE, "offset": offset},
    )
    if rows.empty:
        st.info("沒有相關錄音。")
    else:
        for _, row in rows.iterrows():
            with st.container(border=True):
                audio_bytes = _to_bytes(row["audio_data"])
                st.write(f"#{row['id']}｜{row['speaker_user_id']}｜{row['script_id']}")
                st.write(row["prompt_text"])
                st.audio(audio_bytes, format=row["mime_type"] or "audio/wav")
                if row["ai_transcript"]:
                    st.caption(f"AI 聽到：{row['ai_transcript']}")
                with st.expander("AI 預檢 JSON"):
                    try:
                        st.json(json.loads(row["ai_review_json"] or "{}"))
                    except Exception:
                        st.write(row["ai_review_json"] or "")
                note = st.text_area("審核備註", value=row["review_note"] or "", key=f"tts_note_{row['id']}")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("接受", key=f"tts_accept_{row['id']}", use_container_width=True):
                        _update_recording_status(row["id"], "accepted", user_id, note)
                        st.rerun()
                with col2:
                    if st.button("拒絕", key=f"tts_reject_{row['id']}", use_container_width=True):
                        _update_recording_status(row["id"], "rejected", user_id, note)
                        st.rerun()

    speaker_options = _accepted_speaker_options()
    selected_speaker = st.selectbox(
        "Export speaker",
        options=["全部"] + speaker_options,
        help="選「全部」會下載所有 accepted 錄音；選指定委員只會下載該委員的 accepted 錄音。",
    )
    speaker_filter = None if selected_speaker == "全部" else selected_speaker

    _render_accepted_dataset_preview(speaker_filter)

    export_bytes = _build_export_zip(speaker_filter)
    export_name = "tts_voice_dataset.zip" if not speaker_filter else f"tts_voice_dataset_{speaker_filter}.zip"
    st.download_button(
        "下載 accepted dataset zip",
        data=export_bytes or b"",
        file_name=export_name,
        mime="application/zip",
        use_container_width=True,
        disabled=export_bytes is None,
    )


def _count_recordings(status):
    rows = query_params(
        f"""
        SELECT COUNT(*) AS n
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        WHERE status = :status
        """,
        {"status": status},
    )
    if rows.empty:
        return 0
    return int(rows.iloc[0]["n"] or 0)


def _render_recording_status_summary():
    rows = query_params(
        f"""
        SELECT status, COUNT(*) AS n, COALESCE(SUM(duration_seconds), 0) AS total_seconds
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        GROUP BY status
        """
    )
    summary = {status: {"n": 0, "seconds": 0} for status in ["pending", "accepted", "rejected", "withdrawn"]}
    for _, row in rows.iterrows():
        status = row["status"]
        summary.setdefault(status, {"n": 0, "seconds": 0})
        summary[status]["n"] = int(row["n"] or 0)
        summary[status]["seconds"] = int(row["total_seconds"] or 0)

    cols = st.columns(4)
    for col, status in zip(cols, ["pending", "accepted", "rejected", "withdrawn"]):
        data = summary.get(status, {"n": 0, "seconds": 0})
        minutes = data["seconds"] / 60
        col.metric(status, f"{data['n']} 段 / {minutes:.1f} 分鐘")


def _render_all_recording_status_table():
    with st.expander("全部提交狀態（metadata）", expanded=False):
        rows = query_params(
            f"""
            SELECT id, speaker_user_id, script_id, status, ai_review_status,
                   duration_seconds, size_bytes, review_note, reviewed_by,
                   created_at, reviewed_at
            FROM {TABLE_TTS_VOICE_RECORDINGS}
            ORDER BY created_at DESC
            LIMIT 300
            """
        )
        if rows.empty:
            st.info("暫時未有提交紀錄。")
            return
        st.caption("只顯示最近 300 段 metadata；詳細試聽請用下面審核列表。")
        st.dataframe(rows, use_container_width=True, hide_index=True)


def _accepted_speaker_options():
    rows = query_params(
        f"""
        SELECT DISTINCT speaker_user_id
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        WHERE status = 'accepted'
          AND speaker_user_id IS NOT NULL
        ORDER BY speaker_user_id
        """
    )
    if rows.empty:
        return []
    return [str(row["speaker_user_id"]) for _, row in rows.iterrows() if str(row["speaker_user_id"]).strip()]


def _render_accepted_dataset_preview(speaker_user_id=None):
    with st.expander("accepted dataset 內容（metadata preview）", expanded=False):
        where = "WHERE status = 'accepted'"
        params = {}
        if speaker_user_id:
            where += " AND speaker_user_id = :speaker_user_id"
            params["speaker_user_id"] = speaker_user_id
        rows = query_params(
            f"""
            SELECT id, speaker_user_id, script_id, prompt_text, mime_type,
                   file_ext, size_bytes, duration_seconds, ai_transcript, created_at
            FROM {TABLE_TTS_VOICE_RECORDINGS}
            {where}
            ORDER BY speaker_user_id, id
            """,
            params,
        )
        if rows.empty:
            st.info("accepted dataset 暫時未有錄音。")
            return

        rows = rows.copy()
        rows["audio_file"] = rows.apply(
            lambda row: f"audio/{row['speaker_user_id']}_{int(row['id']):04d}.{row['file_ext'] or 'wav'}",
            axis=1,
        )
        st.caption(f"目前 export 會包含 {len(rows)} 段 accepted 錄音。")
        st.dataframe(
            rows[[
                "id", "speaker_user_id", "script_id", "prompt_text", "audio_file",
                "mime_type", "size_bytes", "duration_seconds", "ai_transcript", "created_at",
            ]],
            use_container_width=True,
            hide_index=True,
        )


def _update_recording_status(recording_id, status, reviewer, note):
    execute_query(
        f"""
        UPDATE {TABLE_TTS_VOICE_RECORDINGS}
        SET status = :status,
            review_note = :note,
            reviewed_by = :reviewer,
            reviewed_at = :now
        WHERE id = :id
        """,
        {
            "id": int(recording_id),
            "status": status,
            "note": note.strip() if note else None,
            "reviewer": reviewer,
            "now": _now_hk(),
        },
    )


def _build_export_zip(speaker_user_id=None):
    where = "WHERE status = 'accepted'"
    params = {}
    if speaker_user_id:
        where += " AND speaker_user_id = :speaker_user_id"
        params["speaker_user_id"] = speaker_user_id
    rows = query_params(
        f"""
        SELECT id, speaker_user_id, script_id, prompt_text, audio_data, mime_type,
               file_ext, size_bytes, duration_seconds, ai_transcript, created_at
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        {where}
        ORDER BY speaker_user_id, id
        """,
        params,
    )
    if rows.empty:
        return None

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        metadata_io = io.StringIO()
        writer = csv.writer(metadata_io)
        writer.writerow([
            "id", "speaker_user_id", "script_id", "prompt_text", "audio_file",
            "mime_type", "size_bytes", "duration_seconds", "ai_transcript", "created_at",
        ])
        for _, row in rows.iterrows():
            ext = row["file_ext"] or "wav"
            audio_name = f"audio/{row['speaker_user_id']}_{int(row['id']):04d}.{ext}"
            zf.writestr(audio_name, _to_bytes(row["audio_data"]))
            writer.writerow([
                row["id"], row["speaker_user_id"], row["script_id"], row["prompt_text"],
                audio_name, row["mime_type"], row["size_bytes"], row["duration_seconds"],
                row["ai_transcript"], row["created_at"],
            ])
        zf.writestr("metadata.csv", metadata_io.getvalue().encode("utf-8-sig"))
    return buffer.getvalue()


def _insert_llm_submission(
    user_id,
    data_type,
    side,
    title,
    topic_text,
    content_text,
    source_note,
    anonymized,
    permission_confirmed,
    ai_review_status,
    ai_review,
):
    execute_query(
        f"""
        INSERT INTO {TABLE_LLM_TRAINING_SUBMISSIONS} (
            submitted_by, data_type, title, topic_text, side, content_text,
            source_note, anonymized, permission_confirmed, ai_review_status,
            ai_review_json, status, created_at
        )
        VALUES (
            :submitted_by, :data_type, :title, :topic_text, :side, :content_text,
            :source_note, :anonymized, :permission_confirmed, :ai_review_status,
            :ai_review_json, 'pending', :created_at
        )
        """,
        {
            "submitted_by": user_id,
            "data_type": data_type,
            "title": title.strip() if title else None,
            "topic_text": topic_text.strip() if topic_text else None,
            "side": side,
            "content_text": content_text.strip(),
            "source_note": source_note.strip() if source_note else None,
            "anonymized": bool(anonymized),
            "permission_confirmed": bool(permission_confirmed),
            "ai_review_status": ai_review_status,
            "ai_review_json": json.dumps(ai_review or {}, ensure_ascii=False),
            "created_at": _now_hk(),
        },
    )


def _llm_submission_fingerprint(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mark_llm_submitted(fingerprint):
    fingerprints = st.session_state.setdefault("llm_submitted_fingerprints", set())
    fingerprints.add(fingerprint)


def _render_llm_manual_fallback(user_id):
    pending = st.session_state.get("llm_pending_submission")
    if not pending:
        return
    payload = pending["payload"]
    with st.container(border=True):
        st.warning(
            f"{pending['message']}\n\n你可以自行確認資料合適後，略過 AI 檢查直接提交，交由管理員人手審核。"
        )
        st.caption(f"待提交：{payload['data_type']}｜{payload.get('title') or '（無標題）'}")
        manual_confirm = st.checkbox(
            "我確認以上文字資料屬辯論訓練用途、已匿名化，並適合提交",
            key="llm_manual_confirm",
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button(
                "略過 AI 檢查並提交待人工審核",
                type="primary",
                disabled=not manual_confirm,
                use_container_width=True,
            ):
                fallback_review = {
                    "passed": False,
                    "reason": pending["message"],
                    "error_type": pending["error_type"],
                    "manual_confirmed": True,
                }
                _insert_llm_submission(user_id, ai_review_status="error", ai_review=fallback_review, **payload)
                _mark_llm_submitted(pending["fingerprint"])
                st.session_state.pop("llm_pending_submission", None)
                st.success("資料已提交，等待人工審核。")
                st.rerun()
        with col2:
            if st.button("取消", use_container_width=True):
                st.session_state.pop("llm_pending_submission", None)
                st.rerun()


def _render_llm_submission(user_id):
    st.subheader("LLM 文字資料提交")
    st.caption("用作建立辯論 LLM 知識庫。可由不同委員提交，但請只提交有權使用、已匿名化、質素足夠，並以粵語口語撰寫的文字資料。")

    with st.expander("可以提交哪些資料？", expanded=False):
        st.markdown(
            """
- **優秀發言稿**：立論、反駁、結辯稿，最好附辯題及立場。
- **逐字稿**：Free De / Mock / 答問片段，請先移除真名及敏感資料。
- **評語樣本**：有具體引用、有改善建議、符合校隊評分標準的評語。
- **攻防問答**：一問一答、追問鏈、常見漏洞及示範回應。
- **主線/策略**：辯題定義、標準、論點、例子、反駁部署。
- **辯題資料**：背景資料、常見正反論據、關鍵概念解釋。

請盡量使用粵語口語撰寫（例如「我哋」「咁」「點解」「對方呢個講法」），避免整段使用書面中文或普通話式中文。

不建議提交：私人聊天、未經同意的同學資料、含真名/電話/班別等個人資料、質素太低或未整理的原始內容、主要以書面中文撰寫的稿件。
            """.strip()
        )

    with st.form("llm_training_submission_form"):
        col1, col2 = st.columns(2)
        with col1:
            data_type = st.selectbox("資料類型", LLM_DATA_TYPES)
        with col2:
            side = st.selectbox("立場 / 角色", LLM_SIDE_OPTIONS)
        title = st.text_input("標題", placeholder="例如：基本法盃初賽反方二副反駁稿")
        topic_text = st.text_area("辯題 / 情境（如適用）", height=80)
        content_text = st.text_area(
            "文字內容（請用粵語口語撰寫）",
            height=260,
            placeholder="例如：我方認為對方呢個講法最大問題係冇證明因果關係，所以呢個論點唔可以成立。",
        )
        source_note = st.text_area("來源 / 備註", height=80, placeholder="例如：本人撰寫；已獲隊員同意整理；2026-07 mock 後匿名化逐字稿")
        anonymized = st.checkbox("我已移除真名、班別、電話、私人對話等敏感或可識別個人資料")
        permission_confirmed = st.checkbox("我確認有權提交此內容作聖呂中辯內部 AI 訓練 / RAG 測試用途")
        submitted = st.form_submit_button("提交 LLM 訓練資料", type="primary")

    if submitted:
        if not content_text.strip():
            st.warning("請填寫文字內容。")
        elif not anonymized or not permission_confirmed:
            st.warning("提交前必須確認已匿名化，以及有權提交作內部 AI 訓練用途。")
        else:
            payload = {
                "data_type": data_type,
                "side": side,
                "title": title,
                "topic_text": topic_text,
                "content_text": content_text,
                "source_note": source_note,
                "anonymized": anonymized,
                "permission_confirmed": permission_confirmed,
            }
            fingerprint = _llm_submission_fingerprint(payload)
            if fingerprint in st.session_state.get("llm_submitted_fingerprints", set()):
                st.info("呢份資料已經提交咗，請勿重複提交。")
            else:
                with st.spinner("AI 正在預檢文字資料..."):
                    result = review_llm_training_text(data_type, side, title, topic_text, source_note, content_text)
                review = result.get("review") or {}
                status = result.get("status")
                if status == "failed":
                    st.error(f"AI 預檢未通過：{result.get('message') or '請修改後再提交。'}")
                    if review:
                        st.json(review)
                    st.session_state.pop("llm_pending_submission", None)
                elif status == "error":
                    # rate limit / 暫時無法使用：改由委員自行確認後略過 AI 檢查
                    st.session_state["llm_pending_submission"] = {
                        "payload": payload,
                        "fingerprint": fingerprint,
                        "message": result.get("message") or "AI 預檢暫時未能完成。",
                        "error_type": result.get("error_type") or "unavailable",
                    }
                else:
                    _insert_llm_submission(user_id, ai_review_status="passed", ai_review=review, **payload)
                    _mark_llm_submitted(fingerprint)
                    st.session_state.pop("llm_pending_submission", None)
                    st.success("AI 預檢通過，資料已提交待管理員審核。")

    _render_llm_manual_fallback(user_id)


def _render_my_llm_submissions(user_id):
    rows = query_params(
        f"""
        SELECT id, data_type, title, topic_text, side, ai_review_status, status, review_note, created_at
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
        WHERE submitted_by = :user_id
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"user_id": user_id},
    )
    st.subheader("我的 LLM 資料提交紀錄")
    if rows.empty:
        st.info("你暫時未提交任何 LLM 文字資料。")
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)

    withdrawable = rows[rows["status"].isin(["pending", "accepted"])] if "status" in rows else rows
    if withdrawable.empty:
        return
    with st.expander("撤回 LLM 資料提交", expanded=False):
        selected_id = st.selectbox("選擇要撤回的提交", options=withdrawable["id"].tolist(), format_func=lambda rid: f"#{rid}")
        if st.button("撤回所選 LLM 資料"):
            _withdraw_llm_submission(selected_id, user_id)
            st.success("已標記為 withdrawn，之後不會 export。")
            st.rerun()


def _withdraw_llm_submission(submission_id, user_id):
    execute_query(
        f"""
        UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS}
        SET status = 'withdrawn'
        WHERE id = :id
          AND submitted_by = :user_id
          AND status != 'withdrawn'
        """,
        {"id": int(submission_id), "user_id": user_id},
    )


def _render_llm_admin_panel(user_id):
    st.divider()
    st.subheader("LLM 資料審核 / Export（管理員）")
    with st.expander("📋 LLM 審核標準（審核前必讀）", expanded=False):
        st.markdown(LLM_REVIEW_STANDARDS_MD)

    rows = query_params(
        f"""
        SELECT status, COUNT(*) AS n
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
        GROUP BY status
        """
    )
    summary = {status: 0 for status in ["pending", "accepted", "rejected", "withdrawn"]}
    for _, row in rows.iterrows():
        summary[row["status"]] = int(row["n"] or 0)
    cols = st.columns(4)
    for col, status in zip(cols, ["pending", "accepted", "rejected", "withdrawn"]):
        col.metric(status, f"{summary.get(status, 0)} 份")

    status_filter = st.selectbox("LLM 資料狀態", ["pending", "accepted", "rejected", "withdrawn"])
    submissions = query_params(
        f"""
        SELECT id, submitted_by, data_type, title, topic_text, side, content_text,
               source_note, anonymized, permission_confirmed, ai_review_status,
               ai_review_json, status, review_note, created_at
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
        WHERE status = :status
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"status": status_filter},
    )
    if submissions.empty:
        st.info("沒有相關 LLM 資料。")
    else:
        for _, row in submissions.iterrows():
            with st.container(border=True):
                st.write(f"#{row['id']}｜{row['submitted_by']}｜{row['data_type']}｜{row['side']}")
                if row["title"]:
                    st.markdown(f"**{row['title']}**")
                if row["topic_text"]:
                    st.caption(f"辯題 / 情境：{row['topic_text']}")
                st.text_area("內容", value=row["content_text"], height=180, key=f"llm_content_{row['id']}", disabled=True)
                if row["source_note"]:
                    st.caption(f"來源 / 備註：{row['source_note']}")
                st.caption(
                    f"ai_review={row['ai_review_status'] or '未有'}｜"
                    f"anonymized={bool(row['anonymized'])}｜"
                    f"permission_confirmed={bool(row['permission_confirmed'])}｜"
                    f"created_at={row['created_at']}"
                )
                with st.expander("AI 預檢 JSON"):
                    try:
                        st.json(json.loads(row["ai_review_json"] or "{}"))
                    except Exception:
                        st.write(row["ai_review_json"] or "")
                note = st.text_area("審核備註", value=row["review_note"] or "", key=f"llm_note_{row['id']}")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("接受", key=f"llm_accept_{row['id']}", use_container_width=True):
                        _update_llm_submission_status(row["id"], "accepted", user_id, note)
                        st.rerun()
                with col2:
                    if st.button("拒絕", key=f"llm_reject_{row['id']}", use_container_width=True):
                        _update_llm_submission_status(row["id"], "rejected", user_id, note)
                        st.rerun()

    export_bytes = _build_llm_export_jsonl()
    st.download_button(
        "下載 accepted LLM dataset JSONL",
        data=export_bytes or b"",
        file_name="llm_training_dataset.jsonl",
        mime="application/jsonl",
        use_container_width=True,
        disabled=export_bytes is None,
    )


def _update_llm_submission_status(submission_id, status, reviewer, note):
    execute_query(
        f"""
        UPDATE {TABLE_LLM_TRAINING_SUBMISSIONS}
        SET status = :status,
            review_note = :note,
            reviewed_by = :reviewer,
            reviewed_at = :now
        WHERE id = :id
        """,
        {
            "id": int(submission_id),
            "status": status,
            "note": note.strip() if note else None,
            "reviewer": reviewer,
            "now": _now_hk(),
        },
    )


def _build_llm_export_jsonl():
    rows = query_params(
        f"""
        SELECT id, submitted_by, data_type, title, topic_text, side, content_text,
               source_note, created_at
        FROM {TABLE_LLM_TRAINING_SUBMISSIONS}
        WHERE status = 'accepted'
          AND anonymized = TRUE
          AND permission_confirmed = TRUE
        ORDER BY data_type, id
        """
    )
    if rows.empty:
        return None

    lines = []
    for _, row in rows.iterrows():
        item = {
            "id": int(row["id"]),
            "submitted_by": row["submitted_by"],
            "data_type": row["data_type"],
            "title": row["title"],
            "topic_text": row["topic_text"],
            "side": row["side"],
            "content_text": row["content_text"],
            "source_note": row["source_note"],
            "created_at": str(row["created_at"]),
        }
        lines.append(json.dumps(item, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_admin_scripts(user_id, all_scripts):
    st.divider()
    st.subheader("句庫管理（管理員）")
    st.caption("管理員可手動新增、修改或停用錄音句子；亦可用 AI 分析仍需要收集哪些錄音。")

    counts = _recording_counts_by_category(all_scripts)

    # --- AI 缺口分析 ---
    with st.expander("🤖 AI 句庫缺口分析", expanded=False):
        st.caption("AI 會分析現有句庫和已收錄音，指出仍欠缺哪些讀音類型，並建議新句子。")
        if st.button("執行 AI 缺口分析", use_container_width=True):
            with st.spinner("AI 正在分析句庫覆蓋..."):
                st.session_state["tts_coverage_analysis"] = analyze_script_coverage(all_scripts, counts)
        coverage = st.session_state.get("tts_coverage_analysis")
        if coverage:
            if not coverage.get("ok"):
                st.error(coverage.get("message") or "分析失敗。")
            else:
                analysis = coverage.get("analysis") or {}
                if analysis.get("overall"):
                    st.info(analysis["overall"])
                if analysis.get("well_covered"):
                    st.markdown("**已足夠覆蓋：** " + "、".join(analysis["well_covered"]))
                for gap in analysis.get("gaps") or []:
                    st.markdown(f"- ⚠️ **{gap.get('area', '')}**：{gap.get('why', '')}")
                suggestions = analysis.get("suggested_scripts") or []
                if suggestions:
                    st.markdown("**建議新增句子**（剔選後一次過加入句庫）：")
                    chosen = []
                    for i, sug in enumerate(suggestions):
                        cat = str(sug.get("category") or "AI建議").strip()
                        txt = str(sug.get("text") or "").strip()
                        if not txt:
                            continue
                        if st.checkbox(f"[{cat}] {txt}", key=f"tts_sug_{i}"):
                            chosen.append((cat, txt))
                    if st.button("加入所選句子", type="primary", disabled=not chosen):
                        for cat, txt in chosen:
                            _upsert_script(_next_script_id(cat), cat, txt, user_id)
                        st.session_state.pop("tts_coverage_analysis", None)
                        st.success(f"已加入 {len(chosen)} 句。")
                        st.rerun()

    # --- 新增／編輯 ---
    with st.expander("➕ 新增句子", expanded=False):
        existing_categories = sorted({s["category"] for s in all_scripts})
        new_category = st.text_input(
            "類別（可用現有或自訂新類別）",
            key="tts_new_script_category",
            placeholder="例如：多音字、數字讀法、追問…",
        )
        if existing_categories:
            st.caption("現有類別：" + "、".join(existing_categories))
        new_text = st.text_area("句子內容（書面粵語）", key="tts_new_script_text")
        if st.button("新增句子", type="primary"):
            cat = (new_category or "").strip()
            txt = (new_text or "").strip()
            if not cat or not txt:
                st.warning("類別同句子內容都要填。")
            else:
                _upsert_script(_next_script_id(cat), cat, txt, user_id)
                clear_field_draft("tts_new_script_category", "tts_new_script_text")
                st.success("已新增句子。")
                st.rerun()

    # --- 現有句子列表：編輯 / 停用 ---
    with st.expander("✏️ 編輯 / 停用現有句子", expanded=False):
        full_scripts = _load_scripts(active_only=False)
        if not full_scripts:
            st.info("句庫為空。")
        else:
            for s in full_scripts:
                with st.container(border=True):
                    c = counts.get(s["category"], {})
                    status_tag = "🟢 啟用中" if s["is_active"] else "⚪ 已停用"
                    st.caption(f"{s['id']}｜{s['category']}｜{status_tag}｜accepted={c.get('accepted', 0)}")
                    edit_text = st.text_area("內容", value=s["text"], key=f"tts_edit_text_{s['id']}")
                    edit_cat = st.text_input("類別", value=s["category"], key=f"tts_edit_cat_{s['id']}")
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("儲存修改", key=f"tts_save_{s['id']}", use_container_width=True):
                            if edit_text.strip() and edit_cat.strip():
                                _upsert_script(s["id"], edit_cat, edit_text, user_id, s["sort_order"])
                                st.success("已儲存。")
                                st.rerun()
                            else:
                                st.warning("類別同內容都要填。")
                    with col2:
                        toggle_label = "停用" if s["is_active"] else "重新啟用"
                        if st.button(toggle_label, key=f"tts_toggle_{s['id']}", use_container_width=True):
                            _set_script_active(s["id"], not s["is_active"])
                            st.rerun()


st.header("聖呂中辯AI訓練")
st.caption("收集自家 TTS 聲線資料及辯論 LLM 文字資料")

user_id = require_committee()

if not ensure_tts_recording_tables():
    st.error("未能建立或讀取 AI 訓練資料表，請稍後再試或聯絡開發人員。")
    st.stop()

_seed_scripts_if_empty()

allowed_users = _parse_json_list(get_system_config(ALLOWED_USERS_CONFIG_KEY))
reviewers = _parse_json_list(get_system_config(REVIEWERS_CONFIG_KEY))
is_allowed = user_id in allowed_users
is_admin = user_id in reviewers

with st.expander("📖 聖呂中辯自家讀音模型研發計劃書", expanded=False):
    st.markdown(_load_rd_plan())

_tab_options = ["tts", "llm"]


def format_training_tab_label(tab_name):
    if tab_name == "tts":
        return "🎙️ TTS 錄音提交"
    return "📝 LLM 文字資料提交"


if hasattr(st, "segmented_control"):
    selected_tab = st.segmented_control(
        "頁面",
        options=_tab_options,
        default="tts",
        format_func=format_training_tab_label,
        key="ai_training_selected_tab",
        label_visibility="collapsed",
        width="stretch",
    )
else:
    selected_tab = st.radio(
        "頁面",
        options=_tab_options,
        format_func=format_training_tab_label,
        key="ai_training_selected_tab",
        horizontal=True,
        label_visibility="collapsed",
    )

if selected_tab is None:
    selected_tab = "tts"

if selected_tab == "tts":
    if not is_allowed and not is_admin:
        st.info("你暫時未獲加入 TTS 錄音收集名單。你仍可到 LLM 文字資料提交分頁提交辯論文字資料。")
    else:
        active_scripts = _load_scripts(active_only=True)

        if is_allowed:
            st.subheader("錄音提交")
            if not _active_consent(user_id):
                with st.container(border=True):
                    st.write(CONSENT_TEXT)
                    agree = st.checkbox("我已閱讀並同意以上錄音用途及授權安排")
                    if st.button("確認同意", type="primary", disabled=not agree):
                        _record_consent(user_id)
                        st.success("已記錄同意。")
                        st.rerun()
            else:
                with st.expander("撤回同意", expanded=False):
                    st.warning("撤回後，你已提交的錄音會標記為 withdrawn，並不再列入 export。")
                    if st.button("撤回 TTS 錄音使用同意"):
                        _withdraw_consent(user_id)
                        st.success("已撤回同意並標記既有錄音。")
                        st.rerun()

                _render_recorder(user_id, active_scripts)
                _render_my_records(user_id)

        if is_admin:
            _render_review_panel(user_id)
            _render_admin_scripts(user_id, active_scripts)
else:
    _render_llm_submission(user_id)
    _render_my_llm_submissions(user_id)
    if is_admin:
        _render_llm_admin_panel(user_id)
