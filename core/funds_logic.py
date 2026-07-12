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


def _now():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")


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


def lateness_data(selected_year=None, db=None):
    db = _resolve_db(db); _ensure_lateness(db)
    years = {fiscal_start(date.today())}
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
    return {"year": year, "years": sorted(years, reverse=True), "label": fiscal_label(year), "range": [start.isoformat(), end.isoformat()],
            "metrics": {"opening": opening, "received": received, "expenses": expense_total, "closing": opening + received - expense_total, "penalties": penalties, "outstanding": penalties - received, "count": int(totals["count"] or 0)},
            "summary": [], "records": [], "expenses": [],
            "members": [str(value).strip() for value in accounts.get("user_id", []) if str(value).strip()]}


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
    if not str(member_user_id or "").strip() or int(late_minutes or 0) < 1: raise ValueError("請選擇帳戶並輸入有效遲到分鐘。")
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_RECORDS}(late_date,member_user_id,late_minutes,paid_amount,note,created_by,created_at) VALUES(:date,:member,:minutes,:paid,:note,:user,:now)", {"date": str(late_date)[:10], "member": str(member_user_id).strip(), "minutes": int(late_minutes), "paid": _float(paid_amount), "note": str(note or "").strip(), "user": user_id, "now": _now()})


def add_lateness_expense(user_id, expense_date, amount, note, db=None):
    if _float(amount) <= 0: raise ValueError("請輸入大於 0 的支出金額。")
    db = _resolve_db(db); _ensure_lateness(db)
    db.execute(f"INSERT INTO {TABLE_LATENESS_FUND_EXPENSES}(expense_date,amount_hkd,note,created_by,created_at) VALUES(:date,:amount,:note,:user,:now)", {"date":str(expense_date)[:10],"amount":_float(amount),"note":str(note or "").strip(),"user":user_id,"now":_now()})


def update_lateness_paid(record_id, amount, db=None):
    return _resolve_db(db).execute_count(f"UPDATE {TABLE_LATENESS_FUND_RECORDS} SET paid_amount=:amount,updated_at=:now WHERE id=:id", {"id":int(record_id),"amount":_float(amount),"now":_now()})


def delete_lateness(kind, row_id, db=None):
    table = TABLE_LATENESS_FUND_RECORDS if kind == "record" else TABLE_LATENESS_FUND_EXPENSES
    return _resolve_db(db).execute_count(f"DELETE FROM {table} WHERE id=:id", {"id":int(row_id)})


def _ensure_ai(db):
    db.execute(CREATE_AI_FUND_TRANSACTIONS); db.execute(CREATE_AI_FUND_USAGE_LOGS)
    db.execute(f"ALTER TABLE {TABLE_AI_FUND_TRANSACTIONS} ADD COLUMN IF NOT EXISTS provider TEXT")
    db.execute(f"ALTER TABLE {TABLE_AI_FUND_USAGE_LOGS} ADD COLUMN IF NOT EXISTS cost_source TEXT DEFAULT 'estimate'")


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
