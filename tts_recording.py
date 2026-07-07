import base64
import csv
import datetime
import io
import json
import re
import zipfile
from zoneinfo import ZoneInfo

import streamlit as st

from auth import require_committee
from functions import (
    ensure_tts_recording_tables,
    execute_query,
    get_system_config,
    query_params,
)
from schema import (
    TABLE_TTS_VOICE_CONSENTS,
    TABLE_TTS_VOICE_RECORDINGS,
    TABLE_TTS_SCRIPTS,
)
from speech_recorder_component import render_speech_recorder
from prompts import (
    TTS_AUDIO_REVIEW_SYSTEM_PROMPT,
    TTS_COVERAGE_SYSTEM_PROMPT,
    build_tts_audio_review_prompt,
    build_tts_coverage_prompt,
)


CONSENT_VERSION = "tts_voice_v1_2026_07"
CONSENT_TEXT = """我同意聖呂中辯收集本人在本頁提交的錄音，用作廣東話 TTS（文字轉語音）、讀音檢查及相關 AI 研究測試。錄音可能用於分析本人聲線及建立語音模型。我明白可向開發者要求撤回未來使用授權。"""

ALLOWED_USERS_CONFIG_KEY = "tts_recording_allowed_users"
REVIEWERS_CONFIG_KEY = "tts_recording_reviewers"
TTS_AUDIO_REVIEW_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


# ---------------------------------------------------------------------------
# 研發計劃書（委員及管理員均可查閱）
# ---------------------------------------------------------------------------
RD_PLAN_MD = """
## 聖呂中辯自家讀音模型 — 研發計劃書

> 目標：建立一套**屬於聖呂中辯自己**嘅廣東話讀音／語音系統，令 AI 辯論易（Free De、Mock、評語朗讀）可以用到**讀音準確、聲線自然**嘅廣東話。本計劃分兩層：**讀音層**（讀啱字）同**聲線層**（把聲似人、自然）。以下逐階段講清楚**做乜、需要乜、產出乜、幾時算完成**。

---

### 階段零｜定位與原則
- **唔會由零 pre-train 大模型**（成本以十萬美元計，唔實際）。我哋行嘅係「收乾淨授權資料 → fine-tune 現成開源模型」呢條務實路線。
- **私隱行先**：所有錄音都要委員親自書面同意，隨時可撤回；撤回即從資料集移除。
- **分層推進**：讀音層平、快、即刻見效；聲線層慢、要資料同 GPU。兩層可並行。

---

### 階段一｜資料收集（**而家進行中**）
- **做乜**：委員登入本頁，照住指定稿句錄音，經 AI 音質預檢後提交。
- **需要乜**：
  - 已簽同意書嘅**錄音委員**；
  - 覆蓋足夠嘅**句庫**（聲調、多音字、數字、日期、英文術語、長句韻律）；
  - 乾淨錄音環境（靜、唔好爆咪、關咗瀏覽器降噪）；
  - 目標：**每位主聲線委員先儲 30–60 分鐘乾淨錄音**做原型，長遠 1–3 小時做正式版。
- **產出**：`tts_voice_recordings` 資料庫入面一批 `pending` 錄音。
- **完成準則**：單一主聲線委員累積 ≥ 30 分鐘、且句庫各大類別都有覆蓋。

### 階段二｜資料審核與整理
- **做乜**：**錄音管理員**逐條試聽、對稿、判斷收唔收，最後 export 成訓練資料集。
- **需要乜**：清楚嘅**審核標準**（見管理員面板）、管理員人手、export 功能。
- **產出**：`accepted` 錄音 → `dataset.zip`（`audio/` + `metadata.csv`，屬 LJSpeech 格式，可直接餵訓練）。
- **完成準則**：有 ≥ 30 分鐘 `accepted` 且 metadata 齊全。

### 階段三｜讀音層（G2P 前端 + 詞典）
- **做乜**：文字 → 粵拼（jyutping）嘅前端，加一本**覆寫詞典**修正人名、術語、多音字、數字讀法。先落喺現用 Azure TTS（透過 Azure Custom Lexicon），令生產環境即刻讀啱字。
- **需要乜**：`ToJyutping` / `PyCantonese`、一份自維護詞典、一個讀音測試集（100–200 句人手標準答案）。
- **產出**：`text → jyutping` 函數 + `lexicon.xml`（Azure 用）。
- **完成準則**：測試集讀音正確率達標；域名詞（基本法盃、DSE、SBA 等）全部讀啱。

### 階段四｜聲線層（fine-tune 開源模型）
- **做乜**：用階段二嘅資料集，fine-tune 一個支援粵語嘅開源 TTS。
- **需要乜**：
  - **模型**：`GPT-SoVITS`（few-shot 最快）或 `CosyVoice2`（原生粵語）；
  - **GPU**：租雲端 GPU（Colab / vast.ai，約 US$0.3–0.7／小時）；
  - **資料**：階段二 export 嘅 zip；
  - **評估工具**：用 ASR（Whisper）計字錯率 CER；搵 3–5 人做 1–5 分盲聽（MOS）。
- **產出**：一個 voice checkpoint + 推理程式。
- **完成準則**：CER 低於目標、盲聽自然度可接受，且對比 Azure 有優勢。

### 階段五｜部署與整合
- **做乜**：將自家模型包成 API，喺 `deploy/proxy.py` 加 `/api/tts/custom`，同現有 Azure endpoint 並存、可切換；先做 A/B 對比先正式上。
- **需要乜**：一部可長期跑推理嘅機（有 GPU 更好）、proxy 改動、切換開關。
- **產出**：AI 辯論易可揀用「自家聲線」。
- **完成準則**：穩定、延遲可接受、盲聽不輸 Azure。

### 階段六｜維護與迭代
- 持續補句庫同詞典（撞到讀錯字就當 bug 記低）；
- 定期用新錄音重訓，逐步提升自然度；
- 檢視同意狀態，處理撤回。

---

### 時間表（solo、兼職，僅供參考）
| 週 | 讀音層 | 聲線層 |
|---|---|---|
| W1–2 | Azure Lexicon 上線、G2P 前端 | 修收音質素、擴句庫、開始收音 |
| W3–4 | 補測試集與詞典 | 收夠 30–60 分鐘、審核 |
| W5–7 | — | 選模型、跑通訓練、v0→v1 |
| W8–10 | 讀音層併入自家模型 | 部署、A/B、決定上唔上 |

### 私隱與倫理
- 只收**已書面同意**嘅委員錄音；同意可**隨時撤回**，撤回即標記 `withdrawn` 且不再 export。
- 資料只用於聖呂中辯內部讀音研究與系統，唔會外流或作其他用途。
""".strip()


# ---------------------------------------------------------------------------
# 錄音審核標準（管理員面板顯示）
# ---------------------------------------------------------------------------
REVIEW_STANDARDS_MD = """
#### 錄音審核標準（管理員必讀）

審核目標：**確保入到訓練集嘅每一段錄音都乾淨、讀啱、對得住稿**。AI 只做初步預檢，**最終由管理員把關**。逐條聽，符合以下全部條件先「接受」：

**一、內容正確**
- 讀嘅內容同指定稿句一致，冇漏字、加字、讀錯字。
- 多音字、數字、日期、英文術語讀法正確（例：`銀行` 讀 hong4、`重新` 讀 cung4、`DSE` 逐個字母讀）。

**二、聲音乾淨**
- 背景**安靜**：冇明顯人聲、冷氣、風扇、鍵盤、迴音。
- **冇爆咪／破音**（clipping）：音量過大會失真，聽到「拆拆」聲即拒絕。
- 音量**適中**：太細聲（要開好大聲先聽到）或太大聲都唔要。

**三、口條自然**
- 語速正常、咬字清楚，唔好過快含糊或過慢生硬。
- 停頓自然，冇無故長時間靜音、冇明顯口誤／重錄駁口。

**四、技術規格**
- 長度合理（約 1–60 秒，太短太長都唔理想）。
- 單一講者、由頭到尾一致（唔好中途換人／換環境）。

**判斷準則**
- ✅ **接受**：以上四項全部符合。
- ❌ **拒絕**：任何一項明顯不符，並喺**審核備註**寫低原因（例：「背景有冷氣聲」「『長遠』讀錯音」「尾段爆咪」），畀委員知道點改。
- 🤔 **有疑問**：情況模稜兩可時，寧可拒絕要求重錄，唔好放低標準 —— **資料質素直接決定模型質素**。
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
    # 多音字：同一個字唔同讀法，喺句中出現
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
    """畀 AI 分析現有句庫同錄音覆蓋，指出仲需要收咩，並建議新句子。"""
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
        st.warning("錄音太長，請控制喺 60 秒內。")
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
        if result.get("error_type") == "quota":
            st.warning(result.get("message") or "AI 使用量或速率已達上限。")
            if st.button("提交入待人工審核資料集", type="primary", use_container_width=True):
                review = {
                    "passed": False,
                    "reason": result.get("message") or "AI quota exhausted",
                    "error_type": "quota",
                }
                _insert_recording(user_id, script, audio_bytes, audio_data, review, ai_review_status="error")
                st.session_state.pop("tts_recording_ai_review", None)
                st.success("錄音已提交，等待人工審核。")
                st.rerun()
        else:
            st.error(result.get("message") or "AI 預檢失敗，請稍後再試。")


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
    status_filter = st.selectbox("狀態", ["pending", "accepted", "rejected", "withdrawn"])
    rows = query_params(
        f"""
        SELECT id, speaker_user_id, script_id, prompt_text, audio_data, mime_type,
               file_ext, size_bytes, duration_seconds, ai_review_json,
               ai_transcript, status, review_note, created_at
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        WHERE status = :status
        ORDER BY created_at DESC
        LIMIT 100
        """,
        {"status": status_filter},
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

    export_bytes = _build_export_zip()
    st.download_button(
        "下載 accepted dataset zip",
        data=export_bytes or b"",
        file_name="tts_voice_dataset.zip",
        mime="application/zip",
        use_container_width=True,
        disabled=export_bytes is None,
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


def _build_export_zip():
    rows = query_params(
        f"""
        SELECT id, speaker_user_id, script_id, prompt_text, audio_data, mime_type,
               file_ext, size_bytes, duration_seconds, ai_transcript, created_at
        FROM {TABLE_TTS_VOICE_RECORDINGS}
        WHERE status = 'accepted'
        ORDER BY speaker_user_id, id
        """
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


def _render_admin_scripts(user_id, all_scripts):
    st.divider()
    st.subheader("句庫管理（管理員）")
    st.caption("管理員可手動增／改／停用錄音句子；亦可用 AI 分析仲需要收咩錄音。")

    counts = _recording_counts_by_category(all_scripts)

    # --- AI 缺口分析 ---
    with st.expander("🤖 AI 句庫缺口分析", expanded=False):
        st.caption("AI 會睇現有句庫同已收錄音，指出仲欠邊類讀音，並建議新句子。")
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


st.header("TTS 錄音收集")
st.caption("用作訓練聖呂中辯讀音模型")

user_id = require_committee()

if not ensure_tts_recording_tables():
    st.error("未能建立或讀取 TTS 錄音資料表，請稍後再試或聯絡開發人員。")
    st.stop()

_seed_scripts_if_empty()

allowed_users = _parse_json_list(get_system_config(ALLOWED_USERS_CONFIG_KEY))
reviewers = _parse_json_list(get_system_config(REVIEWERS_CONFIG_KEY))
is_allowed = user_id in allowed_users
is_admin = user_id in reviewers

with st.expander("📖 聖呂中辯自家讀音模型研發計劃書", expanded=False):
    st.markdown(RD_PLAN_MD)

if not is_allowed and not is_admin:
    st.info("你暫時未獲加入 TTS 錄音收集名單。如需參與，請聯絡開發者。")
    st.stop()

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
        st.stop()

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
