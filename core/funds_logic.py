"""Ledger logic shared by the lateness and AI fund APIs."""

import math
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.config_store import get_config, get_configs, set_configs
from core.vote_logic import _resolve_db
from schema import (
    CREATE_AI_FUND_TRANSACTIONS, CREATE_AI_FUND_USAGE_LOGS,
    CREATE_LATENESS_FUND_EXPENSES, CREATE_LATENESS_FUND_PERIODS,
    CREATE_LATENESS_FUND_RECORDS, TABLE_ACCOUNTS, TABLE_AI_FUND_TRANSACTIONS,
    TABLE_AI_FUND_USAGE_LOGS, TABLE_LATENESS_FUND_EXPENSES,
    TABLE_LATENESS_FUND_PERIODS, TABLE_LATENESS_FUND_RECORDS,
)
from system_limits import (
    ACCOUNT_LIST_LIMIT,
    AI_USAGE_RETENTION_DAYS,
    MAINTENANCE_PRUNE_INTERVAL_SECONDS,
)

HKD_PER_USD = 7.8
AI_FUND_TARGET_DEFAULT = 500.0
AI_FUND_LOW_BALANCE_DEFAULT = 100.0
AI_FUND_PAYMENT_DEFAULT = "請按賽會指示付款，並提交交易編號供 AI基金管理員確認。"
AI_TRANSACTION_TYPES = {"member_deposit", "provider_topup", "provider_refund", "member_refund", "adjustment"}
AI_PROVIDERS = {"openrouter", "gemini", "openai", "general", "other"}
AI_PAYMENT_METHODS = {"FPS", "現金", "Alipay", "PayMe", "其他"}
AI_TRANSACTION_LABELS = {
    "member_deposit": "成員入數", "provider_topup": "AI provider 充值 / 帳單",
    "provider_refund": "Provider 退款予基金", "member_refund": "退款予委員",
    "adjustment": "手動調整",
}
AI_PROVIDER_LABELS = {
    "general": "整體AI基金", "gemini": "Gemini", "openrouter": "OpenRouter",
    "openai": "GPT", "other": "其他",
}
AI_FEATURE_LABELS = {
    "speech_review": "練習發言", "strategy": "主線策劃", "web_research": "搵料易",
    "fact_check": "Fact Check易", "free_debate_live": "打Free De",
    "full_mock_live": "打完整Mock", "vote_review": "辯題審查",
    "vote_analysis": "辯題庫 / 往績分析", "vote_discussion": "委員討論回應",
    "tts_review": "AI訓練·錄音檢查", "tts_script_analysis": "AI訓練·句庫分析",
    "llm_review": "AI訓練·文字審查",
}
AI_USAGE_FEATURES = (
    "speech_review", "strategy", "web_research", "fact_check",
    "free_debate_live", "full_mock_live", "vote_review",
    "vote_analysis", "vote_discussion", "tts_review",
    "tts_script_analysis", "llm_review",
)
LATENESS_FUND_MANAGERS_DEFAULT = ("leungph",)
_LATENESS_SCHEMA_LOCK = threading.Lock()
_LATENESS_SCHEMA_READY = False
_AI_SCHEMA_LOCK = threading.Lock()
_AI_SCHEMA_READY = False
_AI_PRUNE_LOCK = threading.Lock()
_AI_LAST_PRUNE = 0.0


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


def _ensure_lateness(db):
    """Create the lazily-supported ledger schema once per worker."""
    global _LATENESS_SCHEMA_READY
    if _LATENESS_SCHEMA_READY:
        return
    with _LATENESS_SCHEMA_LOCK:
        if _LATENESS_SCHEMA_READY:
            return
        db.execute(CREATE_LATENESS_FUND_RECORDS)
        db.execute(CREATE_LATENESS_FUND_EXPENSES)
        db.execute(CREATE_LATENESS_FUND_PERIODS)
        _LATENESS_SCHEMA_READY = True


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


def lateness_managers(db=None):
    db = _resolve_db(db)
    values = _config(db, "lateness_fund_managers", [])
    if not isinstance(values, list) or not values:
        return list(LATENESS_FUND_MANAGERS_DEFAULT)
    managers = [str(value).strip() for value in values if str(value).strip()]
    return managers or list(LATENESS_FUND_MANAGERS_DEFAULT)


def is_lateness_manager(user_id, db=None):
    return str(user_id or "").strip() in lateness_managers(db)


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
    _ensure_lateness(db)
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
        "WHERE user_id NOT IN ('admin','developer','') "
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
        "is_manager": is_lateness_manager(user_id, db=db) if user_id else False,
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
    db = _resolve_db(db); _ensure_lateness(db)
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_PERIODS}(year_label,opening_balance,updated_at) VALUES(:year,:amount,:now) ON CONFLICT(year_label) DO UPDATE SET opening_balance=EXCLUDED.opening_balance,updated_at=EXCLUDED.updated_at", {"year": fiscal_label(year), "amount": _float(amount), "now": _now()})


def carry_lateness_opening(year, db=None):
    db = _resolve_db(db)
    _ensure_lateness(db)
    previous = _lateness_totals(int(year) - 1, db)
    amount = previous["opening"] + previous["received"] - previous["expenses"]
    set_lateness_opening(year, amount, db=db)
    return amount


def add_lateness_record(user_id, late_date, member_user_id, late_minutes, paid_amount, note, db=None):
    db = _resolve_db(db); _ensure_lateness(db)
    member = str(member_user_id or "").strip()
    if not member or int(late_minutes or 0) < 1: raise ValueError("請選擇帳戶並輸入有效遲到分鐘。")
    paid = _float(paid_amount)
    if paid < 0: raise ValueError("已繳金額不能為負數。")
    account = db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:member AND COALESCE(account_disabled,FALSE)=FALSE", {"member": member})
    if account.empty: raise ValueError("所選帳戶不存在或已停用。")
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_RECORDS}(late_date,member_user_id,late_minutes,paid_amount,note,created_by,created_at) VALUES(:date,:member,:minutes,:paid,:note,:user,:now)", {"date": _valid_date(late_date, "遲到日期"), "member": member, "minutes": int(late_minutes), "paid": paid, "note": str(note or "").strip()[:2000], "user": user_id, "now": _now()})


def add_lateness_expense(user_id, expense_date, amount, note, db=None):
    if _float(amount) <= 0: raise ValueError("請輸入大於 0 的支出金額。")
    db = _resolve_db(db); _ensure_lateness(db)
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


def _ensure_ai(db):
    global _AI_SCHEMA_READY
    if not _AI_SCHEMA_READY:
        with _AI_SCHEMA_LOCK:
            if not _AI_SCHEMA_READY:
                db.execute(CREATE_AI_FUND_TRANSACTIONS)
                db.execute(CREATE_AI_FUND_USAGE_LOGS)
                db.execute(f"ALTER TABLE {TABLE_AI_FUND_TRANSACTIONS} ADD COLUMN IF NOT EXISTS provider TEXT")
                db.execute(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} ADD COLUMN IF NOT EXISTS cost_source TEXT DEFAULT 'estimate'")
                db.execute(f"""DO $$
        DECLARE item RECORD;
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtext('ai_fund_transaction_type_v2'));
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
            WHERE t.relname='{TABLE_AI_FUND_TRANSACTIONS}' AND c.contype='c'
              AND pg_get_constraintdef(c.oid) ILIKE '%provider_refund%'
              AND pg_get_constraintdef(c.oid) ILIKE '%member_refund%'
          ) THEN
            FOR item IN SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
              WHERE t.relname='{TABLE_AI_FUND_TRANSACTIONS}' AND c.contype='c'
                AND pg_get_constraintdef(c.oid) ILIKE '%transaction_type%'
            LOOP
              EXECUTE format('ALTER TABLE {TABLE_AI_FUND_TRANSACTIONS} DROP CONSTRAINT %I',item.conname);
            END LOOP;
            UPDATE {TABLE_AI_FUND_TRANSACTIONS} SET transaction_type='provider_refund' WHERE transaction_type='refund';
            ALTER TABLE {TABLE_AI_FUND_TRANSACTIONS} ADD CONSTRAINT chk_ai_fund_transaction_type
              CHECK (transaction_type IN ('member_deposit','provider_topup','provider_refund','member_refund','adjustment'));
          END IF;
        END $$""")
                _AI_SCHEMA_READY = True
    _prune_ai_usage(db)


def _prune_ai_usage(db):
    """Retain detailed token telemetry without relying on worker restarts."""
    global _AI_LAST_PRUNE
    monotonic_now = time.monotonic()
    if monotonic_now - _AI_LAST_PRUNE < MAINTENANCE_PRUNE_INTERVAL_SECONDS:
        return
    with _AI_PRUNE_LOCK:
        if monotonic_now - _AI_LAST_PRUNE < MAINTENANCE_PRUNE_INTERVAL_SECONDS:
            return
        db.execute(
            f"DELETE FROM {TABLE_AI_FUND_USAGE_LOGS} WHERE created_at<:cutoff",
            {
                "cutoff": datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
                - timedelta(days=AI_USAGE_RETENTION_DAYS),
            },
        )
        _AI_LAST_PRUNE = monotonic_now


def log_ai_usage(user_id, feature, success, usage=None, error_message="", db=None):
    """Record one AI call using actual provider token metadata."""
    if feature not in AI_USAGE_FEATURES:
        raise ValueError("不支援的 AI 功能類型。")
    db = _resolve_db(db)
    _ensure_ai(db)
    usage = usage or {}
    model_label = str(usage.get("model_label") or "Gemini 3.5 Flash")
    provider = str(usage.get("provider") or "gemini").lower()
    if provider not in AI_PROVIDERS:
        provider = "other"
    params = {
        "user": user_id,
        "feature": feature,
        "model": model_label,
        "provider": provider,
        "usd": _float(usage.get("estimated_cost_usd")) if success else 0,
        "hkd": _float(usage.get("estimated_cost_hkd")) if success else 0,
        "input": int(usage.get("input_tokens") or 0) if success else 0,
        "output": int(usage.get("output_tokens") or 0) if success else 0,
        "audio": int(usage.get("audio_tokens") or 0) if success else 0,
        "search": int(usage.get("search_calls") or 0) if success else 0,
        "source": str(usage.get("cost_source") or "estimate") if success else "failed",
        "status": "success" if success else "failed",
        "error": str(error_message or "")[:1000],
        "now": _now(),
    }
    db.execute(
        f"""INSERT INTO {TABLE_AI_FUND_USAGE_LOGS}(
            user_id,feature,model_label,provider,estimated_cost_usd,estimated_cost_hkd,
            input_tokens,output_tokens,audio_tokens,search_calls,cost_source,status,error_message,created_at
        ) VALUES(
            :user,:feature,:model,:provider,:usd,:hkd,:input,:output,:audio,:search,:source,:status,:error,:now
        )""",
        params,
    )


def _config(db, key, default=""):
    return get_config(db, key, default)


def _configs(db, keys):
    return get_configs(db, keys)


def is_ai_treasurer(user_id, db=None):
    """Check the role without loading balances, usage aggregates, and accounts."""
    db = _resolve_db(db)
    values = _config(db, "ai_fund_treasurers", [])
    if not isinstance(values, list):
        return False
    user_id = str(user_id or "").strip()
    return user_id in {str(value).strip() for value in values if str(value).strip()}


def ai_data(user_id, db=None):
    db = _resolve_db(db)
    _ensure_ai(db)
    config = _configs(db, (
        "ai_fund_treasurers",
        "ai_fund_target_hkd",
        "ai_fund_low_balance_hkd",
        "ai_fund_payment_instruction",
        "google_ai_studio_balance_usd",
        "google_ai_studio_balance_updated_at",
        "google_ai_studio_balance_updated_by",
    ))
    treasurer_values = config.get("ai_fund_treasurers") or []
    treasurers = (
        [str(value).strip() for value in treasurer_values if str(value).strip()]
        if isinstance(treasurer_values, list) else []
    )
    treasurer = str(user_id or "").strip() in treasurers
    settings = {
        "treasurers": treasurers,
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
                WHERE status='success' AND created_at>=:since
            ), 0) AS recent_usage
        """,
        {"since": since},
    )
    transactions = []
    usage = []
    accounts = db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE user_id NOT IN ('admin','developer','') AND COALESCE(account_disabled,FALSE)=FALSE ORDER BY user_id LIMIT :account_limit", {"account_limit": ACCOUNT_LIST_LIMIT})
    total_row = totals.iloc[0] if not totals.empty else {}
    amount = _float(total_row.get("balance")); account_count = len(accounts)
    google = config.get("google_ai_studio_balance_usd")
    return {
        "user_id": user_id,
        "is_treasurer": treasurer,
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


def add_ai_transaction(user_id, transaction_type, amount, provider="general", payment_method="", reference_no="", note="", confirmed=False, db=None):
    if transaction_type not in AI_TRANSACTION_TYPES: raise ValueError("不支援的交易類型。")
    amount = _float(amount)
    if (transaction_type != "adjustment" and amount <= 0) or (transaction_type == "adjustment" and amount == 0): raise ValueError("金額不正確。")
    method = str(payment_method or "").strip()
    if transaction_type == "member_deposit" and method not in AI_PAYMENT_METHODS:
        raise ValueError("不支援的付款方式。")
    db = _resolve_db(db); _ensure_ai(db); provider = str(provider or "other").lower(); provider = provider if provider in AI_PROVIDERS else "other"; now = _now()
    db.execute(f"INSERT INTO {TABLE_AI_FUND_TRANSACTIONS}(transaction_type,status,provider,amount_hkd,payment_method,reference_no,note,created_by,created_at,confirmed_by,confirmed_at) VALUES(:type,:status,:provider,:amount,:method,:ref,:note,:user,:now,:confirmed_by,:confirmed_at)", {"type":str(transaction_type)[:40],"status":"confirmed" if confirmed else "pending","provider":str(provider or "")[:80],"amount":amount,"method":str(method or "")[:200],"ref":str(reference_no or "").strip()[:200],"note":str(note or "").strip()[:2000],"user":user_id,"now":now,"confirmed_by":user_id if confirmed else None,"confirmed_at":now if confirmed else None})


def set_ai_transaction_status(transaction_id, status, user_id, note="", db=None):
    if status not in {"confirmed", "rejected"}:
        raise ValueError("不支援的狀態。")
    db = _resolve_db(db)
    _ensure_ai(db)
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


def ai_usage_summary(user_id, treasurer=False, db=None, limit=None, offset=0):
    db = _resolve_db(db); _ensure_ai(db)
    where = "WHERE status='success'" if treasurer else "WHERE status='success' AND user_id=:user"
    params = {} if treasurer else {"user": user_id}
    paging = " LIMIT :limit OFFSET :offset" if limit is not None else ""
    if limit is not None:
        params.update(limit=max(1, int(limit)), offset=max(0, int(offset)))
    return db.query(f"""SELECT TO_CHAR(created_at,'YYYY-MM') AS "month",user_id,
        COALESCE(provider,'other') AS provider,feature,model_label,COUNT(*) AS uses,
        ROUND(SUM(estimated_cost_hkd)::numeric,4) AS estimated_cost_hkd
        FROM {TABLE_AI_FUND_USAGE_LOGS} {where}
        GROUP BY TO_CHAR(created_at,'YYYY-MM'),user_id,COALESCE(provider,'other'),feature,model_label
        ORDER BY "month" DESC,estimated_cost_hkd DESC{paging}""", params)


def ai_usage_summary_count(user_id, treasurer=False, db=None):
    db = _resolve_db(db); _ensure_ai(db)
    where = "WHERE status='success'" if treasurer else "WHERE status='success' AND user_id=:user"
    params = {} if treasurer else {"user": user_id}
    result = db.query(f"""SELECT COUNT(*) AS n FROM (
        SELECT 1 FROM {TABLE_AI_FUND_USAGE_LOGS} {where}
        GROUP BY TO_CHAR(created_at,'YYYY-MM'),user_id,COALESCE(provider,'other'),feature,model_label
    ) grouped""", params)
    return int(result.iloc[0]["n"] or 0) if not result.empty else 0


def save_ai_admin(user_id, payload, db=None):
    db = _resolve_db(db)
    if not is_ai_treasurer(user_id, db=db):
        raise PermissionError("只有 AI基金管理員可更改設定。")
    _ensure_ai(db)
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
