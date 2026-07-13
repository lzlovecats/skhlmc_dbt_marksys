"""Versioned bootstrap data for the active TTS recording workflow."""

from sqlalchemy import text

from schema import TABLE_TTS_SCRIPTS


DEFAULT_TTS_SCRIPT_BANK = (
    ("free_001", "Free De", "你呢個講法最大問題係冇證明因果關係。"),
    ("free_002", "Free De", "我想追問你，政策成本由邊個承擔？"),
    ("free_003", "Free De", "如果你承認有例外，咁你個標準其實已經唔穩陣。"),
    ("mock_001", "Mock", "多謝主席，各位評判、各位同學，今日我方立場非常清晰。"),
    ("mock_002", "Mock", "總結我方三個重點，第一係可行性，第二係公平性，第三係長遠影響。"),
    ("mock_003", "Mock", "對方一直避開核心問題，就係制度本身會否製造更大不公。"),
    ("question_001", "追問", "你可唔可以畀一個具體例子，證明呢個方法真係有效？"),
    ("question_002", "追問", "如果資源有限，你會優先幫邊一類人，點解？"),
    ("rebuttal_001", "反駁", "對方將相關性講成因果性，呢個係明顯嘅邏輯跳步。"),
    ("rebuttal_002", "反駁", "你嘅例子只係個別情況，唔足以支持一個普遍政策。"),
    ("numbers_001", "數字讀法", "二零二六年，我哋預計有百分之三十五嘅學生受影響。"),
    ("numbers_002", "數字讀法", "如果滿分係一百分，呢個方案最多只可以攞六十五分。"),
    ("terms_002", "術語/英文", "自由辯論最重要唔係講得長，而係追問要準、反駁要快。"),
    ("feedback_001", "評語", "你頭先嘅主線清楚，但回應對方追問時可以再直接啲。"),
    ("feedback_002", "評語", "整體台風穩陣，不過個別位收得太急，畀評判嘅印象會扣分。"),
    ("feedback_003", "評語", "你嘅論點有數據支持，值得欣賞，下次記得同時交代數據嚟源。"),
    ("numbers_003", "數字讀法", "呢場比賽最後比數係四十八比五十二，我哋以四分之差落敗。"),
    ("numbers_004", "數字讀法", "報名人數由二百三十七人升到一千零五人，升幅超過三倍。"),
    ("numbers_005", "數字讀法", "第一、第二同第三名分別攞到九十五、八十八同八十一分。"),
    ("numbers_006", "數字讀法", "聯絡電話係二五二八，三六七九，有問題可以隨時致電查詢。"),
    ("date_001", "日期時間", "決賽定於二零二六年七月十九號，星期日下晝三點半喺禮堂舉行。"),
    ("date_002", "日期時間", "報名截止日期係下個月八號，逾期恕不受理，請各位隊伍準時提交。"),
    ("date_003", "日期時間", "每節限時四分三十秒，夠三分鐘會響第一次鈴，夠鐘就響兩下。"),
    ("terms_004", "術語/英文", "OK，我哋而家開始 free debate 環節，計時交由 timer 負責。"),
    ("terms_005", "術語/英文", "呢個 argument 嘅 logic 有斷層，你需要補返個 example 先撐得住。"),
    ("terms_006", "術語/英文", "AI 辯論易會用 GPT 同 Gemini 兩個模型，分別做評語同即時回應。"),
    ("poly_001", "多音字", "佢嘅行為好有問題，但銀行嗰行細字就冇人為意。"),
    ("poly_002", "多音字", "呢點好重要，所以我哋要重新檢視成個制度嘅設計。"),
    ("poly_003", "多音字", "校長話長遠嚟講，同學嘅成長比一時嘅長短更加關鍵。"),
    ("poly_004", "多音字", "呢部分嘅分數唔高，但佢反映嘅身分認同問題就唔可以忽視。"),
    ("poly_005", "多音字", "佢好奇點解一個好人會做出咁嘅選擇，我覺得值得深究。"),
    ("poly_006", "多音字", "快樂同音樂表面相似，實際上係兩種完全唔同嘅體驗。"),
    ("tone_001", "聲調覆蓋", "詩、史、試、時、市、事，呢六個字聲調各有不同，要讀得分明。"),
    ("tone_002", "聲調覆蓋", "三分鐘、九十九分、五十蚊、一百萬，數字讀音要清清楚楚。"),
    ("prosody_001", "長句韻律", "各位評判、各位老師、各位同學，多謝大家喺一個咁繁忙嘅星期日，抽時間出席今日呢場意義重大嘅辯論比賽。"),
    ("prosody_002", "長句韻律", "我方認為，無論係從公平、效率，定係從長遠嘅社會影響嚟睇，呢個政策都應該經過更充分嘅諮詢先至推行。"),
    ("prosody_003", "長句韻律", "如果我哋只係睇短期數字，好容易忽略咗背後真正需要幫助嘅人，而呢啲人往往就係最冇聲音嗰班。"),
)


def seed_default_tts_scripts(conn) -> None:
    """Idempotently seed the sentence bank during an explicit DB bootstrap."""
    conn.execute(
        text(
            f"""INSERT INTO {TABLE_TTS_SCRIPTS}
                (script_id,category,text,is_active,sort_order,created_by)
                VALUES(:id,:category,:text,TRUE,:sort,'system')
                ON CONFLICT(script_id) DO NOTHING"""
        ),
        [
            {"id": script_id, "category": category, "text": value, "sort": order}
            for order, (script_id, category, value) in enumerate(DEFAULT_TTS_SCRIPT_BANK)
        ],
    )
