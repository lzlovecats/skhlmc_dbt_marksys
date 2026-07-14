"""AI helpers for topic review and vote analysis."""

from account_access import NON_MEMBER_ACCOUNT_DB_KEYS, is_non_member_account
from ai_model_config import (
    AI_MODEL_OPTIONS, NON_MANUAL_DEFAULT_AI_MODEL,
    NON_MANUAL_MODEL_OPTIONS, get_feature_model,
)
from prompts import (
    VOTE_BANK_ANALYSIS_SYSTEM_PROMPT,
    VOTE_DISCUSSION_SYSTEM_PROMPT,
    VOTE_HISTORY_ANALYSIS_SYSTEM_PROMPT,
    VOTE_TOPIC_REVIEW_SYSTEM_PROMPT,
    build_vote_bank_analysis_prompt,
    build_vote_discussion_prompt,
    build_vote_history_analysis_prompt,
    build_vote_topic_review_prompt,
)
from schema import TABLE_TOPIC_REMOVAL_VOTES, TABLE_TOPIC_VOTES, TABLE_TOPICS
from core.vote_logic import parse_reason_list
from system_limits import (
    ACCOUNT_LIST_LIMIT, VOTE_AI_CATEGORY_EXAMPLE_LIMIT,
    VOTE_AI_DISCUSSION_COMMENT_LIMIT, VOTE_AI_MAX_OUTPUT_TOKENS,
    VOTE_AI_PROMPT_MAX_CHARS, VOTE_AI_TOPIC_SAMPLE_LIMIT,
)

CATEGORIES = [
    "國際與時事", "科技與未來", "文化與生活",
    "香港社會政策", "青少年與教育", "哲理／價值觀",
]
DIFFICULTY_OPTIONS = {1: "Lv1 — 概念日常", 2: "Lv2 — 一般議題", 3: "Lv3 — 進階專業"}
DIFFICULTY_CRITERIA = {
    1: "Lv1：概念日常、背景知識少，適合完全無經驗的新手",
    2: "Lv2：需要一定議題認識或邏輯鋪陳，但不需要專業知識",
    3: "Lv3：涉及專業政策、複雜概念界定、或需要大量資料支撐",
}


def is_successful_ai_result(result: str | None) -> bool:
    if not result:
        return False
    return not str(result).lstrip().startswith(("⚠️", "❌"))


def _model_config(model_label=None, feature="vote_discussion"):
    if model_label:
        label = model_label
    else:
        label, feature_config = get_feature_model(feature)
        return {**feature_config, "label": label}
    config = (
        NON_MANUAL_MODEL_OPTIONS.get(label)
        or AI_MODEL_OPTIONS.get(label)
        or NON_MANUAL_MODEL_OPTIONS.get(NON_MANUAL_DEFAULT_AI_MODEL)
    )
    if not config:
        return None
    return {**config, "label": label if label in AI_MODEL_OPTIONS or label in NON_MANUAL_MODEL_OPTIONS else NON_MANUAL_DEFAULT_AI_MODEL}


def _read_attr(value, *names):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _usage_record(model, input_tokens=0, output_tokens=0):
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    usd = (
        input_tokens * float(model.get("input_price_per_million") or 0)
        + output_tokens * float(model.get("output_price_per_million") or 0)
    ) / 1_000_000
    return {
        "model_label": model.get("label") or NON_MANUAL_DEFAULT_AI_MODEL,
        "provider": model.get("provider") or "other",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "audio_tokens": 0,
        "search_calls": 0,
        "estimated_cost_usd": round(usd, 6),
        "estimated_cost_hkd": round(usd * 7.8, 4),
        "cost_source": "actual_tokens",
    }


def _format_ai_error(provider, error):
    return f"❌ {provider} 回覆失敗：{error}"


def generate_general_ai_reply(
    system_prompt, user_text, secrets, model_label=None, *, feature="vote_discussion",
):
    user_text = str(user_text or "")
    if len(user_text) > VOTE_AI_PROMPT_MAX_CHARS:
        user_text = user_text[:VOTE_AI_PROMPT_MAX_CHARS] + "\n[輸入已按伺服器資源上限截斷]"
    model = _model_config(model_label, feature)
    if not model:
        return "❌ 未能載入 AI 模型設定。", None
    if model.get("provider") == "gemini":
        return _generate_gemini(model, system_prompt, user_text, secrets)
    return _generate_openrouter(model, system_prompt, user_text, secrets)


def _generate_gemini(model, system_prompt, user_text, secrets):
    api_key = secrets.get("GEMINI_API_KEY")
    if not api_key:
        return "❌ 未設定 Gemini API Key，請聯絡開發人員。", None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "❌ Gemini SDK 尚未安裝，請先更新 requirements.txt 並重新部署。", None
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model["model"],
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_text)])],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
                max_output_tokens=VOTE_AI_MAX_OUTPUT_TOKENS,
            ),
        )
        meta = _read_attr(response, "usage_metadata", "usageMetadata")
        usage = _usage_record(
            model,
            _read_attr(meta, "prompt_token_count", "promptTokenCount"),
            _read_attr(meta, "candidates_token_count", "candidatesTokenCount"),
        )
        return response.text or "AI 未能生成回覆，請再試一次。", usage
    except Exception as e:
        return _format_ai_error("Gemini", e), None


def _generate_openrouter(model, system_prompt, user_text, secrets):
    api_key = secrets.get("OPENROUTER_API_KEY")
    if not api_key:
        return "❌ 未設定 OpenRouter API Key，請聯絡開發人員。", None
    try:
        from openai import OpenAI
    except ImportError:
        return "❌ OpenAI SDK 尚未安裝，請先更新 requirements.txt 並重新部署。", None
    try:
        client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
        response = client.chat.completions.create(
            model=model["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            temperature=0.7,
            max_tokens=VOTE_AI_MAX_OUTPUT_TOKENS,
        )
        usage_meta = _read_attr(response, "usage")
        usage = _usage_record(
            model,
            _read_attr(usage_meta, "prompt_tokens", "promptTokens"),
            _read_attr(usage_meta, "completion_tokens", "completionTokens"),
        )
        return response.choices[0].message.content or "AI 未能生成回覆，請再試一次。", usage
    except Exception as e:
        return _format_ai_error("OpenRouter", e), None


def gather_topic_review_context(category, difficulty, db):
    lines = []
    totals = db.query(f"""SELECT COUNT(*) AS total,
        COUNT(*) FILTER (WHERE category=:category) AS category_count FROM {TABLE_TOPICS}""",
        {"category": category})
    total = int(totals.iloc[0]["total"] or 0) if not totals.empty else 0
    cat_count = int(totals.iloc[0]["category_count"] or 0) if not totals.empty else 0
    if total:
        ratio = cat_count / total * 100 if total else 0.0
        lines.append(f"現有辯題庫共 {total} 條；類別「{category}」佔 {cat_count} 條（{ratio:.0f}%，上限 20%）。")
        sample_df = db.query(f"SELECT topic_text FROM {TABLE_TOPICS} WHERE category=:category ORDER BY topic_text LIMIT :limit",
                             {"category": category, "limit": VOTE_AI_CATEGORY_EXAMPLE_LIMIT})
        sample = sample_df["topic_text"].tolist() if not sample_df.empty else []
        if sample:
            lines.append("同類別現有辯題（用嚟檢查重複／重疊）：")
            lines.extend(f"- {t}" for t in sample)
    else:
        lines.append("現有辯題庫暫時無資料。")

    votes_df = db.query(f"""SELECT status,category,difficulty,COUNT(*) AS n
        FROM {TABLE_TOPIC_VOTES} WHERE status IN ('passed','rejected')
        GROUP BY status,category,difficulty""")
    if votes_df.empty:
        lines.append("歷史提案投票數據不足，通過機率只能作定性判斷。")
        return "\n".join(lines)

    def _rate(df):
        n = int(df["n"].sum())
        passed = int(df.loc[df["status"] == "passed", "n"].sum())
        return passed, n, (passed / n * 100 if n else 0.0)

    passed, n, rate = _rate(votes_df)
    lines.append(f"歷史提案通過率：整體 {passed}/{n}（{rate:.0f}%）。")
    cat_df = votes_df[votes_df["category"] == category]
    if not cat_df.empty:
        passed, n, rate = _rate(cat_df)
        lines.append(f"　同類別「{category}」：{passed}/{n}（{rate:.0f}%）。")
    try:
        diff_df = votes_df[votes_df["difficulty"] == int(difficulty)]
    except (TypeError, ValueError):
        diff_df = votes_df.iloc[0:0]
    if not diff_df.empty:
        passed, n, rate = _rate(diff_df)
        diff_label = DIFFICULTY_OPTIONS.get(int(difficulty), str(difficulty))
        lines.append(f"　同難度「{diff_label}」：{passed}/{n}（{rate:.0f}%）。")
    return "\n".join(lines)


def review_topic(topic, category, difficulty, db, secrets):
    difficulty_label = DIFFICULTY_OPTIONS.get(int(difficulty), str(difficulty))
    user_text = build_vote_topic_review_prompt(
        topic,
        category,
        difficulty_label,
        category_options=CATEGORIES,
        difficulty_definitions=DIFFICULTY_CRITERIA,
        analytics_context=gather_topic_review_context(category, difficulty, db),
    )
    return generate_general_ai_reply(
        VOTE_TOPIC_REVIEW_SYSTEM_PROMPT, user_text, secrets,
        feature="vote_review",
    )


def gather_bank_analysis_context(db):
    distribution = db.query(f"""SELECT category,difficulty,COUNT(*) AS n FROM {TABLE_TOPICS}
        GROUP BY category,difficulty""")
    if distribution.empty:
        return "辯題庫暫時無題目。", []
    total = int(distribution["n"].sum())
    summary_lines = [f"總題目數：{total}", "類別分佈："]
    cat_counts = distribution.groupby("category", dropna=False)["n"].sum()
    for cat in CATEGORIES:
        c = int(cat_counts.get(cat, 0))
        pct = c / total * 100 if total else 0
        flag = "（已超過 20% 上限）" if pct > 20 else ""
        summary_lines.append(f"- {cat}：{c}（{pct:.0f}%）{flag}")
    for cat, c in cat_counts.items():
        if cat not in CATEGORIES:
            summary_lines.append(f"- {cat or '（未分類）'}：{int(c)}（{int(c) / total * 100:.0f}%）")
    summary_lines.append("難度分級標準：")
    summary_lines.extend(f"- {DIFFICULTY_CRITERIA[lvl]}" for lvl in (1, 2, 3))
    summary_lines.append("難度分佈：")
    for lvl in (1, 2, 3):
        c = int(distribution.loc[distribution["difficulty"] == lvl, "n"].sum())
        pct = c / total * 100 if total else 0
        summary_lines.append(f"- {DIFFICULTY_OPTIONS.get(lvl, lvl)}：{c}（{pct:.0f}%）")
    votes_df = db.query(f"""SELECT status,COUNT(*) AS n FROM {TABLE_TOPIC_VOTES}
        WHERE status IN ('passed','rejected') GROUP BY status""")
    if not votes_df.empty:
        n = int(votes_df["n"].sum())
        passed = int(votes_df.loc[votes_df["status"] == "passed", "n"].sum())
        summary_lines.append(f"歷史提案通過率：{passed}/{n}（{passed / n * 100:.0f}%）")
    bank_df = db.query(f"""SELECT topic_text,category,difficulty FROM {TABLE_TOPICS}
        ORDER BY topic_text LIMIT :sample_limit""", {"sample_limit": VOTE_AI_TOPIC_SAMPLE_LIMIT})
    if total > len(bank_df):
        summary_lines.append(f"題目逐項檢查只抽取最多 {VOTE_AI_TOPIC_SAMPLE_LIMIT} 條；分佈統計仍使用完整資料。")
    topic_lines = []
    for _, r in bank_df.iterrows():
        try:
            diff_label = DIFFICULTY_OPTIONS.get(int(r["difficulty"]), str(r["difficulty"]))
        except (TypeError, ValueError):
            diff_label = "—"
        topic_lines.append(f"- {r['topic_text']}（{r.get('category') or '—'}｜{diff_label}）")
    return "\n".join(summary_lines), topic_lines


def analyze_topic_bank(db, secrets):
    bank_summary, topic_lines = gather_bank_analysis_context(db)
    user_text = build_vote_bank_analysis_prompt(bank_summary, topic_lines)
    return generate_general_ai_reply(
        VOTE_BANK_ANALYSIS_SYSTEM_PROMPT, user_text, secrets,
        feature="vote_analysis",
    )


def _status_label(status):
    return {"pending": "進行中", "passed": "通過", "rejected": "否決"}.get(str(status), str(status) or "—")


def gather_vote_history_analysis_context(vote_df, db):
    if vote_df.empty:
        return "暫時未有歷史投票數據。", [], [], []
    df = vote_df.fillna("")
    motion_cols = ["motion_type", "topic_text", "status", "proposer_user_id", "category", "difficulty", "created_at"]
    motions = df[motion_cols].drop_duplicates()
    ballots = df[df["user_id"].astype(str).str.strip() != ""].copy()
    total_motions = len(motions)
    total_ballots = len(ballots)
    agree_count = int((ballots["vote_choice"] == "agree").sum()) if total_ballots else 0
    against_count = total_ballots - agree_count
    avg_ballots = total_ballots / total_motions if total_motions else 0
    summary_lines = [
        f"歷史議案總數：{total_motions}",
        f"總投票數：{total_ballots}",
        f"平均每項議案投票數：{avg_ballots:.1f}",
        f"整體同意票：{agree_count}（{agree_count / total_ballots * 100:.0f}%）" if total_ballots else "整體同意票：0",
        f"整體反對票：{against_count}（{against_count / total_ballots * 100:.0f}%）" if total_ballots else "整體反對票：0",
    ]
    for motion_type, type_df in motions.groupby("motion_type"):
        status_parts = [f"{_status_label(status)} {int(count)}" for status, count in type_df["status"].value_counts().items()]
        summary_lines.append(f"{motion_type}：{len(type_df)} 項（{'、'.join(status_parts) if status_parts else '未有狀態'}）")

    account_df = db.query(
        "SELECT user_id FROM accounts "
        "WHERE LOWER(user_id) <> ALL(:excluded_account_keys) "
        "ORDER BY user_id LIMIT :limit",
        {
            "excluded_account_keys": list(NON_MEMBER_ACCOUNT_DB_KEYS),
            "limit": ACCOUNT_LIST_LIMIT,
        },
    )
    all_user_ids = account_df["user_id"].tolist() if not account_df.empty else []
    if not ballots.empty:
        for uid in ballots["user_id"].dropna().unique().tolist():
            if not is_non_member_account(uid) and uid not in all_user_ids:
                all_user_ids.append(uid)
    ballot_groups = {uid: member_df for uid, member_df in ballots.groupby("user_id")} if not ballots.empty else {}
    member_lines = []
    for uid in all_user_ids:
        member_df = ballot_groups.get(uid)
        if member_df is None or member_df.empty:
            member_lines.append(f"- {uid}：未有投票紀錄；暫時未能判斷偏好。")
            continue
        n = len(member_df)
        agree = int((member_df["vote_choice"] == "agree").sum())
        against = n - agree
        topic_n = int((member_df["motion_type"] == "辯題投票").sum())
        removal_n = int((member_df["motion_type"] == "罷免投票").sum())
        agree_cats = (
            member_df[(member_df["motion_type"] == "辯題投票") & (member_df["vote_choice"] == "agree")]["category"]
            .replace("", "未分類").value_counts().head(2)
        )
        against_cats = (
            member_df[(member_df["motion_type"] == "辯題投票") & (member_df["vote_choice"] != "agree")]["category"]
            .replace("", "未分類").value_counts().head(2)
        )
        support_txt = "、".join(f"{cat}({int(count)})" for cat, count in agree_cats.items()) or "—"
        oppose_txt = "、".join(f"{cat}({int(count)})" for cat, count in against_cats.items()) or "—"
        member_lines.append(
            f"- {uid}：投票 {n} 次；同意率 {agree / n * 100:.0f}%（同意 {agree}／反對 {against}）；"
            f"辯題投票 {topic_n}、罷免投票 {removal_n}；較常支持：{support_txt}；較常反對：{oppose_txt}"
        )

    category_lines = []
    topic_motions = motions[motions["motion_type"] == "辯題投票"]
    if not topic_motions.empty:
        for cat, cat_df in topic_motions.groupby(topic_motions["category"].replace("", "未分類")):
            n = len(cat_df)
            passed = int((cat_df["status"] == "passed").sum())
            rejected = int((cat_df["status"] == "rejected").sum())
            category_lines.append(f"- 類別 {cat}：議案 {n}；通過 {passed}；否決 {rejected}；通過率 {passed / n * 100:.0f}%")
        for diff, diff_df in topic_motions.groupby(topic_motions["difficulty"]):
            try:
                diff_label = DIFFICULTY_OPTIONS.get(int(diff), str(diff))
            except (TypeError, ValueError):
                diff_label = str(diff) or "—"
            n = len(diff_df)
            passed = int((diff_df["status"] == "passed").sum())
            category_lines.append(f"- 難度 {diff_label}：議案 {n}；通過 {passed}；通過率 {passed / n * 100:.0f}%")
    reason_counts = {}
    if "against_reasons" in ballots.columns:
        for raw in ballots["against_reasons"].tolist():
            for reason in parse_reason_list(raw):
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
    reason_lines = [f"- {reason}：{count} 次" for reason, count in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)]
    return "\n".join(summary_lines), member_lines, category_lines, reason_lines


def analyze_vote_history(vote_df, db, secrets):
    overall_summary, member_lines, category_lines, reason_lines = gather_vote_history_analysis_context(vote_df, db)
    user_text = build_vote_history_analysis_prompt(overall_summary, member_lines, category_lines, reason_lines)
    return generate_general_ai_reply(
        VOTE_HISTORY_ANALYSIS_SYSTEM_PROMPT, user_text, secrets,
        feature="vote_analysis",
    )


def extract_gemini_question(comment):
    idx = str(comment or "").lower().find("@gemini")
    if idx == -1:
        return None
    question = str(comment)[idx + len("@gemini"):].strip().lstrip("：:，, ").strip()
    return question or str(comment).strip()


def build_motion_background(motion_type, motion_key, db):
    src = TABLE_TOPIC_VOTES if motion_type == "topic_vote" else TABLE_TOPICS
    meta = db.query(f"SELECT category, difficulty FROM {src} WHERE topic_text = :t LIMIT 1", {"t": motion_key})
    lines = []
    if not meta.empty:
        row = meta.iloc[0]
        if row.get("category"):
            lines.append(f"辯題類別：{row['category']}")
        try:
            diff_label = DIFFICULTY_OPTIONS.get(int(row["difficulty"]))
        except (TypeError, ValueError):
            diff_label = None
        if diff_label:
            lines.append(f"目前難度：{diff_label}")
    lines.append("難度分級標準：")
    lines.extend(f"- {DIFFICULTY_CRITERIA[lvl]}" for lvl in (1, 2, 3))
    return "\n".join(lines)


def discussion_reply(motion_type, motion_key, comments, db, secrets, question=None):
    recent_comments = comments[-VOTE_AI_DISCUSSION_COMMENT_LIMIT:]
    discussion_lines = [f"{c['user_id']}：{str(c['comment_text'])[:2000]}" for c in recent_comments]
    removal_reasons = None
    if motion_type == "topic_removal":
        reason_rows = db.query(
            f"SELECT removal_reasons FROM {TABLE_TOPIC_REMOVAL_VOTES} "
            "WHERE topic_text = :topic AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            {"topic": motion_key},
        )
        if not reason_rows.empty:
            removal_reasons = parse_reason_list(reason_rows.iloc[0]["removal_reasons"])
    user_text = build_vote_discussion_prompt(
        motion_type, motion_key, discussion_lines,
        removal_reasons=removal_reasons,
        question=question,
        background=build_motion_background(motion_type, motion_key, db),
    )
    return generate_general_ai_reply(
        VOTE_DISCUSSION_SYSTEM_PROMPT, user_text, secrets,
        feature="vote_discussion",
    )
