"""Streamlit-free ledger logic shared by the lateness and AI fund pages."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.vote_logic import _resolve_db
from schema import (
    CREATE_AI_FUND_TRANSACTIONS, CREATE_AI_FUND_USAGE_LOGS,
    CREATE_LATENESS_FUND_EXPENSES, CREATE_LATENESS_FUND_PERIODS,
    CREATE_LATENESS_FUND_RECORDS, TABLE_ACCOUNTS, TABLE_AI_FUND_TRANSACTIONS,
    TABLE_AI_FUND_USAGE_LOGS, TABLE_LATENESS_FUND_EXPENSES,
    TABLE_LATENESS_FUND_PERIODS, TABLE_LATENESS_FUND_RECORDS,
)

HKD_PER_USD = 7.8
AI_FUND_TARGET_DEFAULT = 500.0
AI_FUND_LOW_BALANCE_DEFAULT = 100.0
AI_FUND_PAYMENT_DEFAULT = "請按賽會指示付款，並提交交易編號供 AI基金管理員確認。"
AI_TRANSACTION_TYPES = {"member_deposit", "provider_topup", "refund", "adjustment"}
AI_PROVIDERS = {"openrouter", "gemini", "openai", "general", "other"}
AI_USAGE_FEATURES = (
    "speech_review", "strategy", "web_research", "fact_check",
    "free_debate_live", "full_mock_live", "vote_review",
    "vote_analysis", "vote_discussion", "tts_review",
    "tts_script_analysis", "llm_review",
)
LATENESS_FUND_MANAGERS_DEFAULT = ("leungph",)


def _now():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")


def _today_hk():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).date()


def _float(value, default=0.0):
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


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
    db.execute(CREATE_LATENESS_FUND_RECORDS)
    db.execute(CREATE_LATENESS_FUND_EXPENSES)
    db.execute(CREATE_LATENESS_FUND_PERIODS)
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_lateness_fund_records_member_user_date ON {TABLE_LATENESS_FUND_RECORDS}(member_user_id, late_date)")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_lateness_fund_expenses_date ON {TABLE_LATENESS_FUND_EXPENSES}(expense_date)")


def fiscal_start(value):
    value = date.fromisoformat(str(value)[:10]) if not isinstance(value, date) else value
    return value.year if value.month >= 9 else value.year - 1


def fiscal_label(year):
    return f"{int(year)}-{(int(year) + 1) % 100:02d}"


def fiscal_range(year):
    return date(int(year), 9, 1), date(int(year) + 1, 8, 31)


def _valid_date(value, label):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}無效。") from exc


def lateness_managers(db=None):
    db = _resolve_db(db)
    raw = _config(db, "lateness_fund_managers", "")
    if not raw:
        return list(LATENESS_FUND_MANAGERS_DEFAULT)
    try:
        values = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        values = []
    managers = [str(value).strip() for value in values if str(value).strip()]
    return managers or list(LATENESS_FUND_MANAGERS_DEFAULT)


def is_lateness_manager(user_id, db=None):
    return str(user_id or "").strip() in lateness_managers(db)


def lateness_data(selected_year=None, user_id=None, db=None):
    db = _resolve_db(db); _ensure_lateness(db)
    years = {fiscal_start(_today_hk())}
    year_rows=db.query(f"SELECT DISTINCT (CASE WHEN EXTRACT(MONTH FROM late_date)>=9 THEN EXTRACT(YEAR FROM late_date) ELSE EXTRACT(YEAR FROM late_date)-1 END)::int fiscal_year FROM {TABLE_LATENESS_FUND_RECORDS} UNION SELECT DISTINCT (CASE WHEN EXTRACT(MONTH FROM expense_date)>=9 THEN EXTRACT(YEAR FROM expense_date) ELSE EXTRACT(YEAR FROM expense_date)-1 END)::int FROM {TABLE_LATENESS_FUND_EXPENSES}")
    years.update(int(value) for value in year_rows.get("fiscal_year",[]) if value is not None)
    year = int(selected_year) if selected_year is not None else max(years)
    start, end = fiscal_range(year)
    params={"start":start.isoformat(),"end":end.isoformat()}
    totals=db.query(f"""WITH ranked AS (SELECT late_minutes,COALESCE(paid_amount,0) paid_amount,ROW_NUMBER() OVER(PARTITION BY member_user_id ORDER BY late_date,id) late_no FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end) SELECT COUNT(*) count,COALESCE(SUM(paid_amount),0) received,COALESCE(SUM(late_no*late_minutes),0) penalties FROM ranked""",params).iloc[0]
    expense_row=db.query(f"SELECT COALESCE(SUM(amount_hkd),0) expenses FROM {TABLE_LATENESS_FUND_EXPENSES} WHERE expense_date BETWEEN :start AND :end",params).iloc[0]
    opening_df = db.query(f"SELECT opening_balance FROM {TABLE_LATENESS_FUND_PERIODS} WHERE year_label=:year", {"year": fiscal_label(year)})
    opening = _float(opening_df.iloc[0]["opening_balance"]) if not opening_df.empty else 0.0
    penalties=_float(totals["penalties"]);received=_float(totals["received"]);expense_total=_float(expense_row["expenses"])
    accounts = db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE user_id NOT IN ('admin','developer','') AND COALESCE(account_disabled,FALSE)=FALSE ORDER BY user_id")
    outstanding = lateness_outstanding(year, db=db)
    return {"year": year, "years": sorted(years, reverse=True), "label": fiscal_label(year), "range": [start.isoformat(), end.isoformat()],
            "metrics": {"opening": opening, "received": received, "expenses": expense_total, "closing": opening + received - expense_total, "penalties": penalties, "outstanding": penalties - received, "count": int(totals["count"] or 0)},
            "summary": [], "records": [], "expenses": [],
            "members": [str(value).strip() for value in accounts.get("user_id", []) if str(value).strip()],
            "is_manager": is_lateness_manager(user_id, db=db) if user_id else False,
            "outstanding_members": outstanding}


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
    previous = lateness_data(int(year) - 1, db=db)
    amount = previous["metrics"]["closing"]
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
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_RECORDS}(late_date,member_user_id,late_minutes,paid_amount,note,created_by,created_at) VALUES(:date,:member,:minutes,:paid,:note,:user,:now)", {"date": _valid_date(late_date, "遲到日期"), "member": member, "minutes": int(late_minutes), "paid": paid, "note": str(note or "").strip(), "user": user_id, "now": _now()})


def add_lateness_expense(user_id, expense_date, amount, note, db=None):
    if _float(amount) <= 0: raise ValueError("請輸入大於 0 的支出金額。")
    db = _resolve_db(db); _ensure_lateness(db)
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_EXPENSES}(expense_date,amount_hkd,note,created_by,created_at) VALUES(:date,:amount,:note,:user,:now)", {"date":_valid_date(expense_date, "支出日期"),"amount":_float(amount),"note":str(note or "").strip(),"user":user_id,"now":_now()})


def update_lateness_paid(record_id, amount, db=None):
    amount = _float(amount)
    if amount < 0: raise ValueError("已繳金額不能為負數。")
    return _resolve_db(db).execute_count(f"UPDATE {TABLE_LATENESS_FUND_RECORDS} SET paid_amount=:amount,updated_at=:now WHERE id=:id", {"id":int(record_id),"amount":amount,"now":_now()})


def delete_lateness(kind, row_id, db=None):
    table = TABLE_LATENESS_FUND_RECORDS if kind == "record" else TABLE_LATENESS_FUND_EXPENSES
    return _resolve_db(db).execute_count(f"DELETE FROM {table} WHERE id=:id", {"id":int(row_id)})


def _ensure_ai(db):
    db.execute(CREATE_AI_FUND_TRANSACTIONS); db.execute(CREATE_AI_FUND_USAGE_LOGS)
    db.execute(f"ALTER TABLE {TABLE_AI_FUND_TRANSACTIONS} ADD COLUMN IF NOT EXISTS provider TEXT")
    db.execute(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} ADD COLUMN IF NOT EXISTS cost_source TEXT DEFAULT 'estimate'")
    db.execute(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} DROP CONSTRAINT IF EXISTS {TABLE_AI_FUND_USAGE_LOGS}_feature_check")
    db.execute(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} DROP CONSTRAINT IF EXISTS chk_ai_fund_usage_feature")
    allowed = ", ".join(f"'{feature}'" for feature in AI_USAGE_FEATURES)
    db.execute(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} ADD CONSTRAINT chk_ai_fund_usage_feature CHECK (feature IN ({allowed}))")


def log_ai_usage(user_id, feature, success, usage=None, error_message="", db=None):
    """Record one non-Streamlit AI call using actual provider token metadata."""
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
    rows = db.query("SELECT value FROM system_config WHERE key=:key", {"key":key})
    return str(rows.iloc[0]["value"]) if not rows.empty and rows.iloc[0]["value"] is not None else default


def _save_config(db, key, value):
    db.execute("INSERT INTO system_config(key,value,updated_at) VALUES(:key,:value,:now) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at", {"key":key,"value":str(value),"now":_now()})


def ai_data(user_id, db=None):
    db = _resolve_db(db); _ensure_ai(db)
    try: treasurers = [str(x).strip() for x in json.loads(_config(db, "ai_fund_treasurers", "[]")) if str(x).strip()]
    except (TypeError, json.JSONDecodeError): treasurers = []
    treasurer = user_id in treasurers
    settings = {"treasurers":treasurers,"target_hkd":_float(_config(db,"ai_fund_target_hkd",AI_FUND_TARGET_DEFAULT)),"low_balance_hkd":_float(_config(db,"ai_fund_low_balance_hkd",AI_FUND_LOW_BALANCE_DEFAULT)),"payment_instruction":_config(db,"ai_fund_payment_instruction",AI_FUND_PAYMENT_DEFAULT)}
    balance = db.query(f"SELECT COALESCE(SUM(CASE WHEN transaction_type='member_deposit' THEN amount_hkd WHEN transaction_type='provider_topup' THEN -amount_hkd WHEN transaction_type IN ('refund','adjustment') THEN amount_hkd ELSE 0 END),0) amount FROM {TABLE_AI_FUND_TRANSACTIONS} WHERE status='confirmed'")
    pending = db.query(f"SELECT COALESCE(SUM(amount_hkd),0) amount FROM {TABLE_AI_FUND_TRANSACTIONS} WHERE status='pending' AND transaction_type='member_deposit'")
    since = (datetime.now(ZoneInfo("Asia/Hong_Kong"))-timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    recent = db.query(f"SELECT COALESCE(SUM(estimated_cost_hkd),0) amount FROM {TABLE_AI_FUND_USAGE_LOGS} WHERE status='success' AND created_at>=:since", {"since":since})
    where = "" if treasurer else "WHERE created_by=:user"; params = {"user":user_id} if not treasurer else {}
    transactions = []
    usage_where = "" if treasurer else "WHERE user_id=:user"; usage_params = {"user":user_id} if not treasurer else {}
    usage = []
    accounts = db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE user_id NOT IN ('admin','developer','') ORDER BY user_id")
    amount = _float(balance.iloc[0]["amount"]); account_count = len(accounts)
    google = _config(db,"google_ai_studio_balance_usd","")
    return {"user_id":user_id,"is_treasurer":treasurer,"settings":settings,"summary":{"balance_hkd":amount,"pending_deposits_hkd":_float(pending.iloc[0]["amount"]),"recent_usage_hkd":_float(recent.iloc[0]["amount"]),"member_count":account_count,"suggested_total_hkd":max(0,settings["target_hkd"]-amount),"suggested_per_member_hkd":max(0,settings["target_hkd"]-amount)/account_count if account_count else 0},"transactions":transactions,"usage":usage,"accounts":[str(v) for v in accounts.get("user_id",[])],"google_balance":{"balance_usd":None if google=="" else _float(google),"updated_at":_config(db,"google_ai_studio_balance_updated_at"),"updated_by":_config(db,"google_ai_studio_balance_updated_by")}}


def add_ai_transaction(user_id, transaction_type, amount, provider="general", payment_method="", reference_no="", note="", confirmed=False, db=None):
    if transaction_type not in AI_TRANSACTION_TYPES: raise ValueError("不支援的交易類型。")
    amount = _float(amount)
    if (transaction_type != "adjustment" and amount <= 0) or (transaction_type == "adjustment" and amount == 0): raise ValueError("金額不正確。")
    db = _resolve_db(db); _ensure_ai(db); provider = str(provider or "other").lower(); provider = provider if provider in AI_PROVIDERS else "other"; now = _now()
    db.execute(f"INSERT INTO {TABLE_AI_FUND_TRANSACTIONS}(transaction_type,status,provider,amount_hkd,payment_method,reference_no,note,created_by,created_at,confirmed_by,confirmed_at) VALUES(:type,:status,:provider,:amount,:method,:ref,:note,:user,:now,:confirmed_by,:confirmed_at)", {"type":transaction_type,"status":"confirmed" if confirmed else "pending","provider":provider,"amount":amount,"method":str(payment_method or "").strip(),"ref":str(reference_no or "").strip(),"note":str(note or "").strip(),"user":user_id,"now":now,"confirmed_by":user_id if confirmed else None,"confirmed_at":now if confirmed else None})


def set_ai_transaction_status(transaction_id, status, user_id, note="", db=None):
    if status not in {"confirmed","rejected"}: raise ValueError("不支援的狀態。")
    field = "confirmed" if status == "confirmed" else "rejected"; now = _now()
    return _resolve_db(db).execute_count(f"UPDATE {TABLE_AI_FUND_TRANSACTIONS} SET status=:status,{field}_by=:user,{field}_at=:now,status_note=:note WHERE id=:id AND status='pending'", {"id":int(transaction_id),"status":status,"user":user_id,"now":now,"note":str(note or "").strip()})


def save_ai_admin(user_id, payload, db=None):
    db = _resolve_db(db)
    data = ai_data(user_id, db=db)
    if not data["is_treasurer"]: raise PermissionError("只有 AI基金管理員可更改設定。")
    kind = payload.get("kind")
    if kind == "settings":
        _save_config(db,"ai_fund_target_hkd",f"{_float(payload.get('target_hkd')):.2f}"); _save_config(db,"ai_fund_low_balance_hkd",f"{_float(payload.get('low_balance_hkd')):.2f}"); _save_config(db,"ai_fund_payment_instruction",str(payload.get("payment_instruction") or AI_FUND_PAYMENT_DEFAULT).strip())
    elif kind == "google_balance":
        value = _float(payload.get("balance_usd"));
        if value < 0: raise ValueError("Google AI Studio 餘額不能為負數。")
        _save_config(db,"google_ai_studio_balance_usd",f"{value:.4f}"); _save_config(db,"google_ai_studio_balance_updated_at",_now()); _save_config(db,"google_ai_studio_balance_updated_by",user_id)
    elif kind == "reset_usage": db.execute(f"DELETE FROM {TABLE_AI_FUND_USAGE_LOGS}")
    else: raise ValueError("不支援的管理操作。")
