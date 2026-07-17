"""Ledger logic shared by the lateness and AI fund APIs."""

import math
import json
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from account_access import NON_MEMBER_ACCOUNT_DB_KEYS, sql_account_id_literals
from ai_model_config import (
    NON_MANUAL_DEFAULT_AI_MODEL,
    build_tts_usage_metadata,
    get_tts_provider_config,
)
from core.config_store import get_config, get_configs, set_configs
from core.vote_logic import _resolve_db
from schema import (
    TABLE_ACCOUNTS, TABLE_AI_FUND_TRANSACTIONS,
    TABLE_AI_FUND_USAGE_LOGS, TABLE_LATENESS_FUND_EXPENSES,
    TABLE_LATENESS_FUND_PERIODS, TABLE_LATENESS_FUND_RECORDS,
    TABLE_MONTHLY_RESOURCE_LIMITS,
)
from system_limits import (
    ACCOUNT_LIST_LIMIT,
    AI_USAGE_RETENTION_DAYS,
    MAINTENANCE_PRUNE_INTERVAL_SECONDS,
)
from core.roles import is_ai_manager, is_senior_committee

HKD_PER_USD = 7.8
AI_FUND_TARGET_DEFAULT = 500.0
AI_FUND_LOW_BALANCE_DEFAULT = 100.0
AI_FUND_PAYMENT_DEFAULT = "請按賽會指示付款，並提交交易編號供 AI管理員確認。"
AI_TRANSACTION_TYPES = {"member_deposit", "provider_topup", "provider_refund", "member_refund", "adjustment"}
AI_PROVIDERS = {
    "openrouter", "gemini", "openai", "azure", "custom", "general", "other",
}
AI_PAYMENT_METHODS = {"FPS", "現金", "Alipay", "PayMe", "其他"}
AI_TRANSACTION_LABELS = {
    "member_deposit": "成員入數", "provider_topup": "AI provider 充值 / 帳單",
    "provider_refund": "Provider 退款予基金", "member_refund": "退款予委員",
    "adjustment": "手動調整",
}
AI_PROVIDER_LABELS = {
    "general": "整體AI基金", "gemini": "Gemini", "openrouter": "OpenRouter",
    "openai": "GPT", "azure": "Azure Speech", "custom": "自家模型",
    "other": "其他",
}
AI_FEATURE_LABELS = {
    "speech_review": "練習發言", "strategy": "主線策劃", "web_research": "搵料易",
    "fact_check": "Fact Check易", "free_debate_live": "打Free De",
    "full_mock_live": "打完整Mock", "vote_review": "辯題審查",
    "vote_analysis": "辯題庫 / 往績分析", "vote_discussion": "委員討論回應",
    "tts_review": "AI訓練·錄音檢查", "tts_script_analysis": "AI訓練·句庫分析",
    "llm_review": "AI訓練·文字審查", "tts": "粵語語音合成",
    "kiosk_match_review": "AI評判易（Kiosk）",
    "kiosk_match_review_tts": "AI評判易·粵語讀出",
}
AI_USAGE_FEATURES = (
    "speech_review", "strategy", "web_research", "fact_check",
    "free_debate_live", "full_mock_live", "vote_review",
    "vote_analysis", "vote_discussion", "tts_review",
    "tts_script_analysis", "llm_review",
    "kiosk_match_review", "tts", "kiosk_match_review_tts",
)
TTS_USAGE_FEATURES = frozenset(("tts", "kiosk_match_review_tts"))
_AI_PRUNE_LOCK = threading.Lock()
_AI_LAST_PRUNE = None
_NON_MEMBER_ACCOUNT_SQL = sql_account_id_literals((*NON_MEMBER_ACCOUNT_DB_KEYS, ""))
AI_BUDGET_PROVIDERS = ("google", "openrouter", "azure", "other")


def _now():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")


def _today_hk():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).date()


def _float(value, default=0.0):
    try:
        number = float(value if value is not None else default)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _nonnegative_int(value, default=0):
    try:
        return max(0, int(value if value is not None else default))
    except (TypeError, ValueError, OverflowError):
        return max(0, int(default))


def _rows(frame):
    return [{key: _json_value(value) for key, value in row.items()} for row in frame.to_dict("records")]


def _json_value(value):
    if hasattr(value, "item"):
        try:
            value = value.item()
        except ValueError:
            pass
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def fiscal_start(value):
    value = date.fromisoformat(str(value)[:10]) if not isinstance(value, date) else value
    return value.year if value.month >= 9 else value.year - 1


def fiscal_label(year):
    return f"{int(year)}-{(int(year) + 1) % 100:02d}"


def fiscal_range(year):
    year = int(year)
    if year < 1 or year > 9998:
        raise ValueError("財政年度無效。")
    return date(year, 9, 1), date(year + 1, 8, 31)


def _valid_date(value, label):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}無效。") from exc


def _lateness_totals(year, db):
    """Return all fiscal-period totals without four separate aggregate queries."""
    start, end = fiscal_range(year)
    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "year_label": fiscal_label(year),
    }
    rows = db.query(
        f"""
        WITH ranked AS (
            SELECT
                late_minutes,
                COALESCE(paid_amount, 0) AS paid_amount,
                ROW_NUMBER() OVER (
                    PARTITION BY member_user_id ORDER BY late_date, id
                ) AS late_no
            FROM {TABLE_LATENESS_FUND_RECORDS}
            WHERE late_date BETWEEN :start AND :end
        )
        SELECT
            COUNT(*) AS count,
            COALESCE(SUM(paid_amount), 0) AS received,
            COALESCE(SUM(late_no * late_minutes), 0) AS penalties,
            COALESCE((
                SELECT SUM(amount_hkd)
                FROM {TABLE_LATENESS_FUND_EXPENSES}
                WHERE expense_date BETWEEN :start AND :end
            ), 0) AS expenses,
            COALESCE((
                SELECT opening_balance
                FROM {TABLE_LATENESS_FUND_PERIODS}
                WHERE year_label = :year_label
            ), 0) AS opening
        FROM ranked
        """,
        params,
    )
    if rows.empty:
        return {"count": 0, "received": 0.0, "penalties": 0.0, "expenses": 0.0, "opening": 0.0}
    row = rows.iloc[0]
    return {
        "count": int(row.get("count", 0) or 0),
        "received": _float(row.get("received")),
        "penalties": _float(row.get("penalties")),
        "expenses": _float(row.get("expenses")),
        "opening": _float(row.get("opening")),
    }


def lateness_data(selected_year=None, user_id=None, db=None):
    db = _resolve_db(db)
    years = {fiscal_start(_today_hk())}
    year_rows = db.query(
        f"""
        SELECT DISTINCT (
            CASE WHEN EXTRACT(MONTH FROM late_date) >= 9
                 THEN EXTRACT(YEAR FROM late_date)
                 ELSE EXTRACT(YEAR FROM late_date) - 1 END
        )::int AS fiscal_year
        FROM {TABLE_LATENESS_FUND_RECORDS}
        UNION
        SELECT DISTINCT (
            CASE WHEN EXTRACT(MONTH FROM expense_date) >= 9
                 THEN EXTRACT(YEAR FROM expense_date)
                 ELSE EXTRACT(YEAR FROM expense_date) - 1 END
        )::int AS fiscal_year
        FROM {TABLE_LATENESS_FUND_EXPENSES}
        """
    )
    years.update(int(value) for value in year_rows.get("fiscal_year", []) if value is not None)
    year = int(selected_year) if selected_year is not None else max(years)
    start, end = fiscal_range(year)
    totals = _lateness_totals(year, db)
    accounts = db.query(
        f"SELECT user_id FROM {TABLE_ACCOUNTS} "
        f"WHERE LOWER(user_id) NOT IN ({_NON_MEMBER_ACCOUNT_SQL}) "
        "AND COALESCE(account_disabled,FALSE)=FALSE "
        "ORDER BY user_id LIMIT :account_limit",
        {"account_limit": ACCOUNT_LIST_LIMIT},
    )
    outstanding = lateness_outstanding(year, db=db)
    return {
        "year": year,
        "years": sorted(years, reverse=True),
        "label": fiscal_label(year),
        "range": [start.isoformat(), end.isoformat()],
        "metrics": {
            "opening": totals["opening"],
            "received": totals["received"],
            "expenses": totals["expenses"],
            "closing": totals["opening"] + totals["received"] - totals["expenses"],
            "penalties": totals["penalties"],
            "outstanding": totals["penalties"] - totals["received"],
            "count": totals["count"],
        },
        "summary": [],
        "records": [],
        "expenses": [],
        "members": [str(value).strip() for value in accounts.get("user_id", []) if str(value).strip()],
        "is_senior_committee": is_senior_committee(user_id, db=db) if user_id else False,
        "outstanding_members": outstanding,
    }


def lateness_outstanding(year, db=None):
    db = _resolve_db(db); start, end = fiscal_range(year)
    rows = db.query(f"""WITH ranked AS (
        SELECT member_user_id,late_minutes,COALESCE(paid_amount,0) paid_amount,
               ROW_NUMBER() OVER(PARTITION BY member_user_id ORDER BY late_date,id) late_no
        FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end
    ), grouped AS (
        SELECT member_user_id,SUM(late_no*late_minutes)-SUM(paid_amount) owed
        FROM ranked GROUP BY member_user_id
    ) SELECT member_user_id,owed FROM grouped WHERE owed>0 ORDER BY member_user_id""",
        {"start": start.isoformat(), "end": end.isoformat()})
    return _rows(rows)


def set_lateness_opening(year, amount, db=None):
    db = _resolve_db(db)
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_PERIODS}(year_label,opening_balance,updated_at) VALUES(:year,:amount,:now) ON CONFLICT(year_label) DO UPDATE SET opening_balance=EXCLUDED.opening_balance,updated_at=EXCLUDED.updated_at", {"year": fiscal_label(year), "amount": _float(amount), "now": _now()})


def carry_lateness_opening(year, db=None):
    db = _resolve_db(db)
    previous = _lateness_totals(int(year) - 1, db)
    amount = previous["opening"] + previous["received"] - previous["expenses"]
    set_lateness_opening(year, amount, db=db)
    return amount


def add_lateness_record(user_id, late_date, member_user_id, late_minutes, paid_amount, note, db=None):
    db = _resolve_db(db)
    member = str(member_user_id or "").strip()
    if not member or int(late_minutes or 0) < 1: raise ValueError("請選擇帳戶並輸入有效遲到分鐘。")
    paid = _float(paid_amount)
    if paid < 0: raise ValueError("已繳金額不能為負數。")
    account = db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:member AND COALESCE(account_disabled,FALSE)=FALSE", {"member": member})
    if account.empty: raise ValueError("所選帳戶不存在或已停用。")
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_RECORDS}(late_date,member_user_id,late_minutes,paid_amount,note,created_by,created_at) VALUES(:date,:member,:minutes,:paid,:note,:user,:now)", {"date": _valid_date(late_date, "遲到日期"), "member": member, "minutes": int(late_minutes), "paid": paid, "note": str(note or "").strip()[:2000], "user": user_id, "now": _now()})


def add_lateness_expense(user_id, expense_date, amount, note, db=None):
    if _float(amount) <= 0: raise ValueError("請輸入大於 0 的支出金額。")
    db = _resolve_db(db)
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_EXPENSES}(expense_date,amount_hkd,note,created_by,created_at) VALUES(:date,:amount,:note,:user,:now)", {"date":_valid_date(expense_date, "支出日期"),"amount":_float(amount),"note":str(note or "").strip()[:2000],"user":user_id,"now":_now()})


def update_lateness_paid(record_id, amount, db=None):
    amount = _float(amount)
    if amount < 0: raise ValueError("已繳金額不能為負數。")
    return _resolve_db(db).execute_count(f"UPDATE {TABLE_LATENESS_FUND_RECORDS} SET paid_amount=:amount,updated_at=:now WHERE id=:id", {"id":int(record_id),"amount":amount,"now":_now()})


def delete_lateness(kind, row_id, db=None):
    tables = {
        "record": TABLE_LATENESS_FUND_RECORDS,
        "expense": TABLE_LATENESS_FUND_EXPENSES,
    }
    if kind not in tables:
        raise ValueError("不支援的基金紀錄類型。")
    table = tables[kind]
    return _resolve_db(db).execute_count(f"DELETE FROM {table} WHERE id=:id", {"id":int(row_id)})


def prune_ai_usage(db):
    """Best-effort retention that never blocks the AI call being accounted."""
    global _AI_LAST_PRUNE
    monotonic_now = time.monotonic()
    if (
        _AI_LAST_PRUNE is not None
        and monotonic_now - _AI_LAST_PRUNE < MAINTENANCE_PRUNE_INTERVAL_SECONDS
    ):
        return
    with _AI_PRUNE_LOCK:
        if (
            _AI_LAST_PRUNE is not None
            and monotonic_now - _AI_LAST_PRUNE < MAINTENANCE_PRUNE_INTERVAL_SECONDS
        ):
            return
        try:
            db.execute(
                f"DELETE FROM {TABLE_AI_FUND_USAGE_LOGS} WHERE created_at<:cutoff",
                {
                    "cutoff": datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
                    - timedelta(days=AI_USAGE_RETENTION_DAYS),
                },
            )
        except Exception:
            # Throttle a permission/timeout failure instead of retrying the
            # same maintenance DELETE on every user request.
            _AI_LAST_PRUNE = monotonic_now
            return False
        _AI_LAST_PRUNE = monotonic_now
        return True


def log_ai_usage(user_id, feature, success, usage=None, error_message="", db=None):
    """Record one provider call using actual or explicitly estimated metadata.

    Failed calls normally remain zero-cost, preserving the legacy behaviour.
    When a provider may bill an unsuccessful attempt, the caller can pass its
    known usage metadata and it will be retained instead of silently discarded.
    ``operation_id`` and ``operation_stage`` correlate multiple provider calls
    without persisting prompts, transcripts or synthesized text.
    """
    if feature not in AI_USAGE_FEATURES:
        raise ValueError("不支援的 AI 功能類型。")
    db = _resolve_db(db)
    prune_ai_usage(db)
    usage = usage if isinstance(usage, dict) else {}
    model_label = str(usage.get("model_label") or NON_MANUAL_DEFAULT_AI_MODEL)
    provider = str(usage.get("provider") or "gemini").lower()
    if provider not in AI_PROVIDERS:
        provider = "other"
    params = {
        "user": user_id,
        "feature": feature,
        "model": model_label,
        "provider": provider,
        "usd": max(0.0, _float(usage.get("estimated_cost_usd"))),
        "hkd": max(0.0, _float(usage.get("estimated_cost_hkd"))),
        "input": _nonnegative_int(usage.get("input_tokens")),
        "output": _nonnegative_int(usage.get("output_tokens")),
        "audio": _nonnegative_int(usage.get("audio_tokens")),
        "characters": _nonnegative_int(usage.get("billable_characters")),
        "search": _nonnegative_int(usage.get("search_calls")),
        "operation_id": str(usage.get("operation_id") or "").strip()[:200] or None,
        "operation_stage": (
            str(usage.get("operation_stage") or "").strip()[:80] or None
        ),
        "source": str(
            usage.get("cost_source") or ("estimate" if success else "failed")
        )[:120],
        "status": "success" if success else "failed",
        "error": str(error_message or "")[:1000],
        "now": _now(),
    }
    db.execute(
        f"""INSERT INTO {TABLE_AI_FUND_USAGE_LOGS}(
            user_id,feature,model_label,provider,estimated_cost_usd,estimated_cost_hkd,
            input_tokens,output_tokens,audio_tokens,billable_characters,search_calls,
            operation_id,operation_stage,cost_source,status,error_message,created_at
        ) VALUES(
            :user,:feature,:model,:provider,:usd,:hkd,:input,:output,:audio,:characters,
            :search,:operation_id,:operation_stage,:source,:status,:error,:now
        )""",
        params,
    )


def log_tts_usage(
    user_id,
    feature,
    success,
    *,
    provider,
    text,
    operation_id,
    operation_stage="synthesis",
    model_label="",
    price_per_million_characters_usd=None,
    error_message="",
    db=None,
):
    """Log one actual TTS provider attempt without retaining synthesized text.

    Call this once around *each* Azure/custom HTTP attempt, including a failed
    custom attempt followed by a successful Azure fallback. ``provider`` must be
    the provider actually called, and ``text`` must be the post-lexicon text sent
    in that request.  Reuse one ``operation_id`` across fallbacks or across the
    transcript/judgement/read-out stages of one AI評判易 match.
    """
    if feature not in TTS_USAGE_FEATURES:
        raise ValueError("不支援的 TTS 用量功能類型。")
    if not str(operation_id or "").strip():
        raise ValueError("TTS 用量紀錄必須提供 operation_id。")
    selected, provider_config = get_tts_provider_config(provider)
    configured_price = price_per_million_characters_usd
    if configured_price in (None, ""):
        from core.runtime_secrets import get_secret

        price_secret = str(
            provider_config.get("price_per_million_characters_secret") or ""
        )
        configured_price = get_secret(price_secret, "") if price_secret else ""
    usage = build_tts_usage_metadata(
        selected,
        text,
        price_per_million_characters_usd=configured_price,
        model_label=model_label,
        operation_id=operation_id,
        operation_stage=operation_stage,
    )
    usage["estimated_cost_hkd"] = (
        float(usage.get("estimated_cost_usd") or 0) * HKD_PER_USD
    )
    return log_ai_usage(
        user_id,
        feature,
        success,
        usage=usage,
        error_message=error_message,
        db=db,
    )


def _config(db, key, default=""):
    return get_config(db, key, default)


def _configs(db, keys):
    return get_configs(db, keys)


def _month_shift(value: date, offset: int) -> date:
    index = value.year * 12 + value.month - 1 + int(offset)
    return date(index // 12, index % 12 + 1, 1)


def ai_budget_cycle(now=None):
    """Return the latest closed 25th-to-25th donation window in Hong Kong."""
    value = now or datetime.now(ZoneInfo("Asia/Hong_Kong"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
    else:
        value = value.astimezone(ZoneInfo("Asia/Hong_Kong"))
    month = value.date().replace(day=1)
    cutoff_month = month if value.day >= 25 else _month_shift(month, -1)
    window_end = datetime(
        cutoff_month.year, cutoff_month.month, 25,
        tzinfo=ZoneInfo("Asia/Hong_Kong"),
    )
    previous = _month_shift(cutoff_month, -1)
    window_start = datetime(
        previous.year, previous.month, 25,
        tzinfo=ZoneInfo("Asia/Hong_Kong"),
    )
    return {
        "window_start": window_start,
        "window_end": window_end,
        "budget_month": _month_shift(cutoff_month, 1),
        "can_settle": value >= window_end,
    }


def _budget_amount(db, cycle) -> float:
    frame = db.query(f"""SELECT COALESCE(SUM(amount_hkd),0) AS amount
        FROM {TABLE_AI_FUND_TRANSACTIONS}
        WHERE transaction_type='member_deposit' AND status='confirmed'
          AND confirmed_at>=:window_start AND confirmed_at<:window_end""", {
        # Existing fund timestamps are stored as naive Hong Kong wall time.
        "window_start": cycle["window_start"].replace(tzinfo=None),
        "window_end": cycle["window_end"].replace(tzinfo=None),
    })
    return round(_float(frame.iloc[0]["amount"] if not frame.empty else 0), 2)


def ai_budget_data(db, now=None) -> dict:
    cycle = ai_budget_cycle(now)
    amount = _budget_amount(db, cycle)
    month = cycle["budget_month"]
    try:
        rows = db.query(f"""SELECT * FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE period_month=:month
              AND (limit_key='ai_fund_available' OR limit_key LIKE 'provider:%')
            ORDER BY limit_key""", {"month": month})
        values = _rows(rows)
    except Exception:
        values = []
    fund = next((row for row in values if row["limit_key"] == "ai_fund_available"), {})
    providers = []
    for name in AI_BUDGET_PROVIDERS:
        row = next((item for item in values if item["limit_key"] == f"provider:{name}"), {})
        allocated = _float(row.get("allocated_hkd"))
        fx = _float(row.get("fx_hkd_per_usd"), 7.8) or 7.8
        factor = 0.9 if name == "google" else 1.0
        providers.append({
            "name": name,
            "allocated_hkd": allocated,
            "external_cap_usd": round(allocated / fx * factor, 4) if allocated else 0,
            "external_cap_confirmed": bool(row.get("external_cap_confirmed")),
            "external_cap_confirmed_by": row.get("external_cap_confirmed_by") or "",
            "external_cap_confirmed_at": row.get("external_cap_confirmed_at") or "",
        })
    allocated_total = round(sum(item["allocated_hkd"] for item in providers), 2)
    snapshot_amount = fund.get("allocated_hkd")
    available = amount if snapshot_amount is None else _float(snapshot_amount)
    return {
        "donation_window": {
            "start": cycle["window_start"].isoformat(),
            "end": cycle["window_end"].isoformat(),
            "confirmed_member_deposits_hkd": amount,
        },
        "budget_month": month.isoformat(),
        "available_hkd": available,
        "fx_hkd_per_usd": _float(fund.get("fx_hkd_per_usd"), 7.8) or 7.8,
        "providers": providers,
        "allocated_hkd": allocated_total,
        "unallocated_hkd": round(max(0, available - allocated_total), 2),
        "notified_at": fund.get("notified_at") or "",
        "notified_by": fund.get("notified_by") or "",
        "notification_audit": fund.get("notification_audit") or {},
        "configured": bool(fund),
        "can_settle": cycle["can_settle"],
        "needs_external_cap_update": any(
            item["allocated_hkd"] > 0 and not item["external_cap_confirmed"]
            for item in providers
        ),
        "needs_notification": not bool(fund.get("notified_at")),
    }


def save_ai_budget(user_id, payload, db=None, now=None):
    db = _resolve_db(db)
    if not is_ai_manager(user_id, db=db):
        raise PermissionError("只有 AI管理員可分配 provider 預算。")
    cycle = ai_budget_cycle(now)
    if not cycle["can_settle"]:
        raise ValueError("本期捐款窗口尚未結算。")
    requested_month = str(payload.get("budget_month") or "")
    if requested_month != cycle["budget_month"].isoformat():
        raise ValueError("預算月份已更新，請重新載入。")
    fx = _float(payload.get("fx_hkd_per_usd"), 7.8)
    if fx <= 0 or fx > 1000:
        raise ValueError("月度匯率無效。")
    raw_allocations = payload.get("allocations") or {}
    if not isinstance(raw_allocations, dict):
        raise ValueError("Provider 分配格式無效。")
    allocations = {}
    confirmations = {}
    for name in AI_BUDGET_PROVIDERS:
        item = raw_allocations.get(name) or {}
        if not isinstance(item, dict):
            raise ValueError("Provider 分配格式無效。")
        amount = round(_float(item.get("allocated_hkd")), 2)
        if amount < 0:
            raise ValueError("Provider 分配不能為負數。")
        confirmed = bool(item.get("external_cap_confirmed"))
        if amount > 0 and not confirmed:
            raise ValueError(f"請先確認已在 {name} 後台更新 spending cap。")
        allocations[name] = amount
        confirmations[name] = confirmed
    now_value = now or datetime.now(ZoneInfo("Asia/Hong_Kong"))
    with db.transaction() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('ai_fund_monthly_budget'))"))
        available = round(_float(session.execute(text(f"""SELECT COALESCE(SUM(amount_hkd),0)
            FROM {TABLE_AI_FUND_TRANSACTIONS}
            WHERE transaction_type='member_deposit' AND status='confirmed'
              AND confirmed_at>=:start AND confirmed_at<:end"""), {
            "start": cycle["window_start"].replace(tzinfo=None),
            "end": cycle["window_end"].replace(tzinfo=None),
        }).scalar()), 2)
        if round(sum(allocations.values()), 2) > available:
            raise ValueError("Provider 分配總額不能高於本期已確認捐款。")
        existing = session.execute(text(f"""SELECT notified_at,notification_audit
            FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE period_month=:month AND limit_key='ai_fund_available'"""), {
            "month": cycle["budget_month"],
        }).mappings().one_or_none()
        prior_audit = (existing or {}).get("notification_audit") or {}
        if isinstance(prior_audit, str):
            try:
                prior_audit = json.loads(prior_audit)
            except ValueError:
                prior_audit = {}
        if existing and (existing.get("notified_at") or prior_audit.get("announcement_at")):
            raise ValueError("本月預算已通知，不能再修改。")
        session.execute(text(f"""INSERT INTO {TABLE_MONTHLY_RESOURCE_LIMITS}
            (period_month,limit_key,unit,allocated_hkd,fx_hkd_per_usd,
             funding_window_start,funding_window_end,updated_by,updated_at)
            VALUES(:month,'ai_fund_available','hkd',:available,:fx,:start,:end,:user,:now)
            ON CONFLICT(period_month,limit_key) DO UPDATE SET
              allocated_hkd=EXCLUDED.allocated_hkd,fx_hkd_per_usd=EXCLUDED.fx_hkd_per_usd,
              funding_window_start=EXCLUDED.funding_window_start,
              funding_window_end=EXCLUDED.funding_window_end,
              updated_by=EXCLUDED.updated_by,updated_at=EXCLUDED.updated_at"""), {
            "month": cycle["budget_month"], "available": available, "fx": fx,
            "start": cycle["window_start"], "end": cycle["window_end"],
            "user": user_id, "now": now_value,
        })
        for name in AI_BUDGET_PROVIDERS:
            amount = allocations[name]
            cap = amount / fx * (0.9 if name == "google" else 1.0)
            session.execute(text(f"""INSERT INTO {TABLE_MONTHLY_RESOURCE_LIMITS}
                (period_month,limit_key,unit,hard_value,allocated_hkd,fx_hkd_per_usd,
                 external_cap_confirmed,external_cap_confirmed_by,
                 external_cap_confirmed_at,updated_by,updated_at)
                VALUES(:month,:key,'usd',:cap,:amount,:fx,:confirmed,
                       CASE WHEN :confirmed THEN :user ELSE NULL END,
                       CASE WHEN :confirmed THEN :now ELSE NULL END,:user,:now)
                ON CONFLICT(period_month,limit_key) DO UPDATE SET
                  hard_value=EXCLUDED.hard_value,allocated_hkd=EXCLUDED.allocated_hkd,
                  fx_hkd_per_usd=EXCLUDED.fx_hkd_per_usd,
                  external_cap_confirmed=EXCLUDED.external_cap_confirmed,
                  external_cap_confirmed_by=EXCLUDED.external_cap_confirmed_by,
                  external_cap_confirmed_at=EXCLUDED.external_cap_confirmed_at,
                  updated_by=EXCLUDED.updated_by,updated_at=EXCLUDED.updated_at"""), {
                "month": cycle["budget_month"], "key": f"provider:{name}",
                "cap": round(cap, 4), "amount": amount, "fx": fx,
                "confirmed": confirmations[name], "user": user_id, "now": now_value,
            })
    return ai_budget_data(db, now=now)


def notify_ai_budget(user_id, db, vapid, now=None):
    if not is_ai_manager(user_id, db=db):
        raise PermissionError("只有 AI管理員可發出預算通知。")
    cycle = ai_budget_cycle(now)
    if not cycle["can_settle"]:
        raise ValueError("本期捐款窗口尚未結算。")
    month = cycle["budget_month"]
    claim = secrets.token_urlsafe(18)
    now_value = now or datetime.now(ZoneInfo("Asia/Hong_Kong"))
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
    else:
        now_value = now_value.astimezone(ZoneInfo("Asia/Hong_Kong"))
    with db.transaction() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('ai_fund_monthly_budget'))"))
        fund = session.execute(text(f"""SELECT allocated_hkd,fx_hkd_per_usd,
                funding_window_start,funding_window_end,notified_at,notification_audit
            FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE period_month=:month AND limit_key='ai_fund_available'
            FOR UPDATE"""), {"month": month}).mappings().one_or_none()
        if fund is None:
            raise ValueError("請先保存 provider 預算分配。")
        if fund.get("notified_at"):
            raise ValueError("本預算月份已經通知。")
        prior_audit = fund.get("notification_audit") or {}
        if isinstance(prior_audit, str):
            try:
                prior_audit = json.loads(prior_audit)
            except ValueError:
                prior_audit = {}
        if prior_audit.get("state") == "sending":
            try:
                claimed_at = datetime.fromisoformat(str(prior_audit.get("claimed_at")))
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
            except (TypeError, ValueError):
                claimed_at = now_value
            if (now_value - claimed_at).total_seconds() < 15 * 60:
                raise ValueError("本預算通知正在發送中，請稍後再檢查。")
        provider_rows = session.execute(text(f"""SELECT limit_key,allocated_hkd,
                hard_value,external_cap_confirmed
            FROM {TABLE_MONTHLY_RESOURCE_LIMITS}
            WHERE period_month=:month AND limit_key LIKE 'provider:%'
            ORDER BY limit_key"""), {"month": month}).mappings().all()
        by_name = {str(row["limit_key"])[9:]: row for row in provider_rows}
        if any(name not in by_name for name in AI_BUDGET_PROVIDERS):
            raise ValueError("Provider 預算資料不完整，請重新保存。")
        available = round(_float(fund.get("allocated_hkd")), 2)
        allocated = round(sum(
            _float(by_name[name].get("allocated_hkd"))
            for name in AI_BUDGET_PROVIDERS
        ), 2)
        if allocated > available:
            raise ValueError("Provider 分配總額高於可用金額。")
        if any(
            _float(by_name[name].get("allocated_hkd")) > 0
            and not bool(by_name[name].get("external_cap_confirmed"))
            for name in AI_BUDGET_PROVIDERS
        ):
            raise ValueError("仍有 provider spending cap 未確認更新。")
        title = f"AI基金 {month.strftime('%Y-%m')} 月度預算"
        start = str(fund.get("funding_window_start") or cycle["window_start"])[:10]
        end = str(fund.get("funding_window_end") or cycle["window_end"])[:10]
        body = (
            f"{start} 至 {end} 收到已確認捐款 HKD {available:.2f}；"
            f"{month.strftime('%Y-%m')} 可用 HKD {available:.2f}。"
        )
        snapshot = {
            "state": "sending", "claim": claim,
            "claimed_at": now_value.isoformat(), "available_hkd": available,
            "allocated_hkd": allocated,
            # The login announcement is durable even when every Web Push
            # delivery fails. Its stable negative month ID makes retries safe.
            "announcement_at": now_value.isoformat(),
            "title": title, "body": body,
            "providers": {
                name: {
                    "allocated_hkd": _float(by_name[name].get("allocated_hkd")),
                    "external_cap_usd": _float(by_name[name].get("hard_value")),
                }
                for name in AI_BUDGET_PROVIDERS
            },
        }
        session.execute(text(f"""UPDATE {TABLE_MONTHLY_RESOURCE_LIMITS}
            SET notification_audit=CAST(:audit AS jsonb),updated_by=:user,updated_at=:now
            WHERE period_month=:month AND limit_key='ai_fund_available'
              AND notified_at IS NULL"""), {
            "audit": json.dumps(snapshot, ensure_ascii=False), "user": user_id,
            "now": now_value, "month": month,
        })

    from core.push import notify_committee
    try:
        sent = notify_committee(
            db, vapid, title, body,
            tag=f"ai-fund-budget-{month.strftime('%Y-%m')}",
            url="/ai-fund", committee_only=True,
        )
    except Exception:
        sent = 0
    if sent <= 0:
        db.execute_count(f"""UPDATE {TABLE_MONTHLY_RESOURCE_LIMITS}
            SET notification_audit=CAST(:audit AS jsonb),updated_by=:user,updated_at=:now
            WHERE period_month=:month AND limit_key='ai_fund_available'
              AND notified_at IS NULL AND notification_audit->>'claim'=:claim""", {
            "audit": json.dumps({
                **snapshot, "state": "retryable",
                "last_error": "zero_successful_delivery",
                "attempted_at": now_value.isoformat(),
            }, ensure_ascii=False),
            "user": user_id, "now": now_value, "month": month, "claim": claim,
        })
        raise RuntimeError("Web Push 未有成功送達；資料未標記為已通知，可安全重試。")
    audit = json.dumps({
        **snapshot, "state": "sent", "sent": sent, "title": title, "body": body,
        "sent_at": now_value.isoformat(),
    }, ensure_ascii=False)
    changed = db.execute_count(f"""UPDATE {TABLE_MONTHLY_RESOURCE_LIMITS}
        SET notified_by=:user,notified_at=:now,notification_audit=CAST(:audit AS jsonb),
            updated_by=:user,updated_at=:now
        WHERE period_month=:month AND limit_key='ai_fund_available'
          AND notified_at IS NULL AND notification_audit->>'claim'=:claim""", {
        "user": user_id, "now": now_value, "audit": audit, "month": month,
        "claim": claim,
    })
    if not changed:
        raise ValueError("本預算月份已經通知。")
    return {"sent": sent, "budget": ai_budget_data(db, now=now)}


def ai_data(user_id, db=None):
    db = _resolve_db(db)
    config = _configs(db, (
        "ai_managers",
        "ai_fund_target_hkd",
        "ai_fund_low_balance_hkd",
        "ai_fund_payment_instruction",
        "google_ai_studio_balance_usd",
        "google_ai_studio_balance_updated_at",
        "google_ai_studio_balance_updated_by",
    ))
    manager_values = config.get("ai_managers") or []
    managers = (
        [str(value).strip() for value in manager_values if str(value).strip()]
        if isinstance(manager_values, list) else []
    )
    manager = is_ai_manager(user_id, db=db)
    settings = {
        "ai_managers": managers,
        "target_hkd": _float(config.get("ai_fund_target_hkd"), AI_FUND_TARGET_DEFAULT),
        "low_balance_hkd": _float(config.get("ai_fund_low_balance_hkd"), AI_FUND_LOW_BALANCE_DEFAULT),
        "payment_instruction": config.get("ai_fund_payment_instruction") or AI_FUND_PAYMENT_DEFAULT,
    }
    since = (datetime.now(ZoneInfo("Asia/Hong_Kong"))-timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    totals = db.query(
        f"""
        SELECT
            COALESCE((
                SELECT SUM(CASE
                    WHEN transaction_type='member_deposit' THEN amount_hkd
                    WHEN transaction_type='provider_topup' THEN -amount_hkd
                    WHEN transaction_type IN ('refund','provider_refund') THEN amount_hkd
                    WHEN transaction_type='member_refund' THEN -amount_hkd
                    WHEN transaction_type='adjustment' THEN amount_hkd
                    ELSE 0 END)
                FROM {TABLE_AI_FUND_TRANSACTIONS}
                WHERE status='confirmed'
            ), 0) AS balance,
            COALESCE((
                SELECT SUM(amount_hkd)
                FROM {TABLE_AI_FUND_TRANSACTIONS}
                WHERE status='pending' AND transaction_type='member_deposit'
            ), 0) AS pending,
            COALESCE((
                SELECT SUM(estimated_cost_hkd)
                FROM {TABLE_AI_FUND_USAGE_LOGS}
                WHERE created_at>=:since
            ), 0) AS recent_usage
        """,
        {"since": since},
    )
    transactions = []
    usage = []
    accounts = db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE LOWER(user_id) NOT IN ({_NON_MEMBER_ACCOUNT_SQL}) AND COALESCE(account_disabled,FALSE)=FALSE ORDER BY user_id LIMIT :account_limit", {"account_limit": ACCOUNT_LIST_LIMIT})
    total_row = totals.iloc[0] if not totals.empty else {}
    amount = _float(total_row.get("balance")); account_count = len(accounts)
    google = config.get("google_ai_studio_balance_usd")
    result = {
        "user_id": user_id,
        "is_ai_manager": manager,
        "settings": settings,
        "summary": {
            "balance_hkd": amount,
            "pending_deposits_hkd": _float(total_row.get("pending")),
            "recent_usage_hkd": _float(total_row.get("recent_usage")),
            "target_hkd": settings["target_hkd"],
            "low_balance_hkd": settings["low_balance_hkd"],
            "member_count": account_count,
            "suggested_total_hkd": max(0, settings["target_hkd"] - amount),
            "suggested_per_member_hkd": (
                max(0, settings["target_hkd"] - amount) / account_count if account_count else 0
            ),
        },
        "transactions": transactions,
        "usage": usage,
        "accounts": [str(value) for value in accounts.get("user_id", [])],
        "google_balance": {
            "balance_usd": None if google is None else _float(google),
            "updated_at": config.get("google_ai_studio_balance_updated_at") or "",
            "updated_by": config.get("google_ai_studio_balance_updated_by") or "",
        },
    }
    result["monthly_budget"] = ai_budget_data(db)
    return result


def add_ai_transaction(user_id, transaction_type, amount, provider="general", payment_method="", reference_no="", note="", confirmed=False, db=None):
    if transaction_type not in AI_TRANSACTION_TYPES: raise ValueError("不支援的交易類型。")
    amount = _float(amount)
    if (transaction_type != "adjustment" and amount <= 0) or (transaction_type == "adjustment" and amount == 0): raise ValueError("金額不正確。")
    method = str(payment_method or "").strip()
    if transaction_type == "member_deposit" and method not in AI_PAYMENT_METHODS:
        raise ValueError("不支援的付款方式。")
    db = _resolve_db(db); provider = str(provider or "other").lower(); provider = provider if provider in AI_PROVIDERS else "other"; now = _now()
    db.execute(f"INSERT INTO {TABLE_AI_FUND_TRANSACTIONS}(transaction_type,status,provider,amount_hkd,payment_method,reference_no,note,created_by,created_at,confirmed_by,confirmed_at) VALUES(:type,:status,:provider,:amount,:method,:ref,:note,:user,:now,:confirmed_by,:confirmed_at)", {"type":str(transaction_type)[:40],"status":"confirmed" if confirmed else "pending","provider":str(provider or "")[:80],"amount":amount,"method":str(method or "")[:200],"ref":str(reference_no or "").strip()[:200],"note":str(note or "").strip()[:2000],"user":user_id,"now":now,"confirmed_by":user_id if confirmed else None,"confirmed_at":now if confirmed else None})


def set_ai_transaction_status(transaction_id, status, user_id, note="", db=None):
    if status not in {"confirmed", "rejected"}:
        raise ValueError("不支援的狀態。")
    db = _resolve_db(db)
    field = "confirmed" if status == "confirmed" else "rejected"
    now = _now()
    return db.execute_count(
        f"UPDATE {TABLE_AI_FUND_TRANSACTIONS} "
        f"SET status=:status,{field}_by=:user,{field}_at=:now,status_note=:note "
        "WHERE id=:id AND status='pending'",
        {
            "id": int(transaction_id),
            "status": status,
            "user": user_id,
            "now": now,
            "note": str(note or "").strip()[:2000],
        },
    )


def ai_usage_summary(user_id, manager=False, db=None, limit=None, offset=0):
    db = _resolve_db(db)
    where = "" if manager else "WHERE user_id=:user"
    params = {} if manager else {"user": user_id}
    paging = " LIMIT :limit OFFSET :offset" if limit is not None else ""
    if limit is not None:
        params.update(limit=max(1, int(limit)), offset=max(0, int(offset)))
    return db.query(f"""SELECT TO_CHAR(created_at,'YYYY-MM') AS "month",user_id,
        COALESCE(provider,'other') AS provider,feature,model_label,
        COUNT(*) FILTER (WHERE status='success') AS uses,
        COUNT(*) AS provider_calls,
        COUNT(DISTINCT CASE
            WHEN feature IN ('kiosk_match_review','kiosk_match_review_tts')
                 AND NULLIF(operation_id,'') IS NOT NULL
                THEN operation_id
            WHEN status='success' THEN 'call:' || id::text
            ELSE NULL
        END) AS tasks,
        COALESCE(SUM(billable_characters),0) AS billable_characters,
        ROUND(SUM(estimated_cost_hkd)::numeric,4) AS estimated_cost_hkd
        FROM {TABLE_AI_FUND_USAGE_LOGS} {where}
        GROUP BY TO_CHAR(created_at,'YYYY-MM'),user_id,COALESCE(provider,'other'),feature,model_label
        ORDER BY "month" DESC,estimated_cost_hkd DESC{paging}""", params)


def ai_usage_summary_count(user_id, manager=False, db=None):
    db = _resolve_db(db)
    where = "" if manager else "WHERE user_id=:user"
    params = {} if manager else {"user": user_id}
    result = db.query(f"""SELECT COUNT(*) AS n FROM (
        SELECT 1 FROM {TABLE_AI_FUND_USAGE_LOGS} {where}
        GROUP BY TO_CHAR(created_at,'YYYY-MM'),user_id,COALESCE(provider,'other'),feature,model_label
    ) grouped""", params)
    return int(result.iloc[0]["n"] or 0) if not result.empty else 0


def save_ai_admin(user_id, payload, db=None):
    db = _resolve_db(db)
    if not is_ai_manager(user_id, db=db):
        raise PermissionError("只有 AI管理員可更改設定。")
    kind = payload.get("kind")
    if kind == "settings":
        target = _float(payload.get("target_hkd")); low = _float(payload.get("low_balance_hkd"))
        if target < 0 or low < 0: raise ValueError("目標金額及低餘額警戒線不能為負數。")
        set_configs(db, {
            "ai_fund_target_hkd": target,
            "ai_fund_low_balance_hkd": low,
            "ai_fund_payment_instruction": str(
                payload.get("payment_instruction") or AI_FUND_PAYMENT_DEFAULT
            ).strip(),
        })
        return {"updated": True}
    elif kind == "google_balance":
        value = _float(payload.get("balance_usd"));
        if value < 0: raise ValueError("Google AI Studio 餘額不能為負數。")
        set_configs(db, {
            "google_ai_studio_balance_usd": value,
            "google_ai_studio_balance_updated_at": _now(),
            "google_ai_studio_balance_updated_by": user_id,
        })
        return {"updated": True}
    elif kind == "reset_usage":
        deleted = db.execute_count(f"DELETE FROM {TABLE_AI_FUND_USAGE_LOGS}")
        return {"deleted": int(deleted or 0)}
    else: raise ValueError("不支援的管理操作。")
