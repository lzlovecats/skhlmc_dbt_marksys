"""Committee-authenticated endpoints for the HTML fund ledgers."""

import httpx

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from api.access import require_page_user_or_developer
from core.roles import is_ai_manager, is_senior_committee
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count
from api.resource_limits import EXPORT_MAX_ROWS, csv_response, require_row_limit
from system_limits import OPENROUTER_CREDIT_TIMEOUT_SECONDS

router = APIRouter(prefix="/api", tags=["funds"])
AI_TX_COLUMNS = """id,transaction_type,status,provider,amount_hkd,
    LEFT(COALESCE(payment_method,''),200) payment_method,
    LEFT(COALESCE(reference_no,''),200) reference_no,
    LEFT(COALESCE(note,''),2000) note,created_by,created_at,
    confirmed_by,confirmed_at,rejected_by,rejected_at,
    LEFT(COALESCE(status_note,''),2000) status_note"""
AI_USAGE_COLUMNS = """id,user_id,feature,model_label,provider,estimated_cost_usd,
    estimated_cost_hkd,input_tokens,output_tokens,audio_tokens,billable_characters,
    search_calls,operation_id,operation_stage,cost_source,status,
    LEFT(COALESCE(error_message,''),1000) error_message,created_at"""


def _csv_response(filename, headers, rows):
    require_row_limit(rows, label="基金紀錄匯出")
    return csv_response(filename, headers, rows)


def _context(request):
    from deploy.proxy import get_vote_db
    return require_page_user_or_developer(request, "funds"), get_vote_db()


def _lateness_context(request, manager=False):
    from core import funds_logic as logic
    user, db = _context(request)
    if manager and not is_senior_committee(user, db=db):
        raise HTTPException(403, "只有高級委員可執行此操作。")
    return user, db


class LatenessRecord(BaseModel):
    late_date: str = Field(max_length=10)
    member_user_id: str = Field(max_length=200)
    late_minutes: int = Field(gt=0, le=1440)
    paid_amount: float = 0
    note: str = Field(default="", max_length=2000)


class LatenessExpense(BaseModel):
    expense_date: str = Field(max_length=10)
    amount: float
    note: str = Field(default="", max_length=2000)


class AmountBody(BaseModel):
    amount: float


class PaidBody(BaseModel):
    amount: float = Field(ge=0)


class LatenessNotifyBody(BaseModel):
    target: str = Field(default="outstanding", max_length=20)
    message: str = Field(default="", max_length=2000)


@router.get("/lateness-fund/data")
def lateness_data(request: Request, year: int | None = None):
    from core import funds_logic as logic
    user, db = _lateness_context(request)
    return logic.lateness_data(year, user_id=user, db=db)


@router.get("/lateness-fund/records")
def lateness_records(request: Request, year: int, page: int = 1, member: str | None = None):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _, db = _context(request); start, end = logic.fiscal_range(year)
    params = {"start": start.isoformat(), "end": end.isoformat()}; page, _, offset = bounds(page)
    member_clause = " AND member_user_id=:member" if str(member or "").strip() else ""
    if member_clause: params["member"] = str(member).strip()
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end{member_clause}", params)
    params.update(limit=PAGE_SIZE, offset=offset)
    rows = db.query(
        f"""
        WITH ranked AS (
            SELECT
                id, late_date, member_user_id, late_minutes,
                COALESCE(paid_amount, 0) AS paid_amount,
                note, created_by, created_at, updated_at,
                ROW_NUMBER() OVER (
                    PARTITION BY member_user_id ORDER BY late_date, id
                ) AS late_no
            FROM {TABLE_LATENESS_FUND_RECORDS}
            WHERE late_date BETWEEN :start AND :end
        )
        SELECT
            id, late_date, member_user_id, late_minutes, paid_amount, note,
            created_by, created_at, updated_at, late_no,
            late_no * late_minutes AS penalty_amount,
            paid_amount - (late_no * late_minutes) AS record_balance
        FROM ranked
        WHERE TRUE{member_clause}
        ORDER BY late_date DESC, id DESC
        LIMIT :limit OFFSET :offset
        """,
        params,
    )
    return payload(logic._rows(rows), page, total)


@router.get("/lateness-fund/expenses")
def lateness_expenses(request: Request, year: int, page: int = 1):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_EXPENSES
    _, db = _context(request); start, end = logic.fiscal_range(year)
    params = {"start": start.isoformat(), "end": end.isoformat()}; page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_LATENESS_FUND_EXPENSES} WHERE expense_date BETWEEN :start AND :end", params)
    params.update(limit=PAGE_SIZE, offset=offset)
    rows = db.query(f"SELECT id,expense_date,amount_hkd,note,created_by,created_at FROM {TABLE_LATENESS_FUND_EXPENSES} WHERE expense_date BETWEEN :start AND :end ORDER BY expense_date DESC,id DESC LIMIT :limit OFFSET :offset", params)
    return payload(logic._rows(rows), page, total)

@router.get("/lateness-fund/summary")
def lateness_summary(request: Request, year: int, page: int = 1):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _,db=_context(request);start,end=logic.fiscal_range(year);page,_,offset=bounds(page);params={"start":start.isoformat(),"end":end.isoformat()}
    total=scalar_count(db,f"SELECT COUNT(DISTINCT member_user_id) total FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end",params);params.update(limit=PAGE_SIZE,offset=offset)
    rows=db.query(f"""WITH ranked AS (SELECT member_user_id,late_minutes,COALESCE(paid_amount,0) paid_amount,ROW_NUMBER() OVER(PARTITION BY member_user_id ORDER BY late_date,id) late_no FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end), grouped AS (SELECT member_user_id,COUNT(*) late_count,SUM(late_minutes) total_late_minutes,SUM(late_no*late_minutes) penalty_amount,SUM(paid_amount) paid_amount FROM ranked GROUP BY member_user_id) SELECT DENSE_RANK() OVER(ORDER BY total_late_minutes DESC) late_rank,member_user_id,late_count,total_late_minutes,penalty_amount,paid_amount,paid_amount-penalty_amount balance FROM grouped ORDER BY total_late_minutes DESC,member_user_id LIMIT :limit OFFSET :offset""",params)
    return payload(logic._rows(rows),page,total)

@router.get("/lateness-fund/member-count")
def lateness_member_count(request:Request,member:str,year:int|None=None,late_date:str|None=None):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _,db=_lateness_context(request)
    try: resolved_year=logic.fiscal_start(logic._valid_date(late_date,"遲到日期")) if late_date else int(year)
    except (TypeError,ValueError): raise HTTPException(400,"請提供有效年度或遲到日期。")
    start,end=logic.fiscal_range(resolved_year)
    count=scalar_count(db,f"SELECT COUNT(*) total FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end AND member_user_id=:member",{"start":start.isoformat(),"end":end.isoformat(),"member":member})
    return {"count":count,"year":resolved_year,"label":logic.fiscal_label(resolved_year)}


@router.get("/lateness-fund/member-summary")
def lateness_member_summary(request:Request,year:int,member:str):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _,db=_lateness_context(request);start,end=logic.fiscal_range(year)
    rows=db.query(f"""WITH ranked AS (SELECT member_user_id,late_minutes,COALESCE(paid_amount,0) paid_amount,ROW_NUMBER() OVER(PARTITION BY member_user_id ORDER BY late_date,id) late_no FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end), grouped AS (SELECT member_user_id,COUNT(*) late_count,SUM(late_minutes) total_late_minutes,SUM(late_no*late_minutes) penalty_amount,SUM(paid_amount) paid_amount FROM ranked GROUP BY member_user_id), ranked_members AS (SELECT DENSE_RANK() OVER(ORDER BY total_late_minutes DESC) late_rank,member_user_id,late_count,total_late_minutes,penalty_amount,paid_amount,paid_amount-penalty_amount balance FROM grouped) SELECT late_rank,member_user_id,late_count,total_late_minutes,penalty_amount,paid_amount,balance FROM ranked_members WHERE member_user_id=:member""",{"start":start.isoformat(),"end":end.isoformat(),"member":member})
    return {"member":None if rows.empty else logic._rows(rows)[0]}


@router.post("/lateness-fund/opening/{year}")
def lateness_opening(year: int, body: AmountBody, request: Request):
    from core import funds_logic as logic
    _, db = _lateness_context(request, manager=True); logic.set_lateness_opening(year, body.amount, db=db)
    return {"ok": True}


@router.post("/lateness-fund/carry/{year}")
def lateness_carry(year: int, request: Request):
    from core import funds_logic as logic
    _, db = _lateness_context(request, manager=True); return {"ok": True, "amount": logic.carry_lateness_opening(year, db=db)}


@router.post("/lateness-fund/records")
def lateness_record(body: LatenessRecord, request: Request):
    from core import funds_logic as logic
    user, db = _lateness_context(request)
    if body.paid_amount != 0 and not is_senior_committee(user, db=db):
        raise HTTPException(403, "只有高級委員可在新增紀錄時登記已繳金額。")
    try: logic.add_lateness_record(user, body.late_date, body.member_user_id, body.late_minutes, body.paid_amount, body.note, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@router.patch("/lateness-fund/records/{record_id}")
def lateness_record_update(record_id: int, body: PaidBody, request: Request):
    from core import funds_logic as logic
    _, db = _lateness_context(request, manager=True)
    try: changed=logic.update_lateness_paid(record_id,body.amount,db=db)
    except ValueError as exc: raise HTTPException(400,str(exc))
    if not changed: raise HTTPException(404,"找不到要更新的紀錄。")
    return {"ok":True}


@router.delete("/lateness-fund/records/{record_id}")
def lateness_record_delete(record_id: int, request: Request):
    from core import funds_logic as logic
    _, db = _lateness_context(request, manager=True); changed=logic.delete_lateness("record",record_id,db=db)
    if not changed: raise HTTPException(404,"找不到要刪除的紀錄。")
    return {"ok":True}


@router.post("/lateness-fund/expenses")
def lateness_expense(body: LatenessExpense, request: Request):
    from core import funds_logic as logic
    user, db = _lateness_context(request, manager=True)
    try: logic.add_lateness_expense(user, body.expense_date, body.amount, body.note, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@router.delete("/lateness-fund/expenses/{expense_id}")
def lateness_expense_delete(expense_id: int, request: Request):
    from core import funds_logic as logic
    _, db = _lateness_context(request, manager=True); changed=logic.delete_lateness("expense",expense_id,db=db)
    if not changed: raise HTTPException(404,"找不到要刪除的支出紀錄。")
    return {"ok":True}


def _hkd(value):
    return f"HKD {float(value or 0):,.2f}"


def _display_date(value):
    text = str(value or "")[:10]
    try:
        year, month, day = text.split("-")
        return f"{day}/{month}/{year}"
    except ValueError:
        return text


@router.get("/lateness-fund/export/records.csv")
def lateness_records_csv(request:Request,year:int):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _,db=_lateness_context(request);start,end=logic.fiscal_range(year)
    rows=db.query(f"""WITH ranked AS (SELECT id,late_date,member_user_id,late_minutes,COALESCE(paid_amount,0) paid_amount,LEFT(COALESCE(note,''),2000) note,created_by,created_at,updated_at,ROW_NUMBER() OVER(PARTITION BY member_user_id ORDER BY late_date,id) late_no FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end) SELECT id,late_date,member_user_id,late_minutes,paid_amount,note,created_by,created_at,updated_at,late_no,late_no*late_minutes penalty_amount,paid_amount-(late_no*late_minutes) record_balance FROM ranked ORDER BY late_date DESC,id DESC LIMIT :export_limit""",{"start":start.isoformat(),"end":end.isoformat(),"export_limit":EXPORT_MAX_ROWS+1})
    require_row_limit(rows, label="遲到紀錄匯出")
    export_rows=[[row["id"],_display_date(row["late_date"]),row["member_user_id"],row["late_minutes"],row["late_no"],_hkd(row["penalty_amount"]),_hkd(row["paid_amount"]),_hkd(row["record_balance"]),row.get("note") or "",row.get("created_by") or "",row.get("created_at") or "",row.get("updated_at") or ""] for _,row in rows.iterrows()]
    filename=f"遲到罰款基金_遲到紀錄_{logic.fiscal_label(year)}.csv"
    return csv_response(filename,["ID","日期","帳戶","遲到分鐘","本年度第幾次","應繳罰款","已繳金額","本次結餘","備註","記錄人","記錄時間","更新時間"],export_rows)


@router.get("/lateness-fund/export/expenses.csv")
def lateness_expenses_csv(request:Request,year:int):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_EXPENSES
    _,db=_lateness_context(request);start,end=logic.fiscal_range(year)
    rows=db.query(f"SELECT id,expense_date,amount_hkd,LEFT(COALESCE(note,''),2000) note,created_by,created_at FROM {TABLE_LATENESS_FUND_EXPENSES} WHERE expense_date BETWEEN :start AND :end ORDER BY expense_date DESC,id DESC LIMIT :export_limit",{"start":start.isoformat(),"end":end.isoformat(),"export_limit":EXPORT_MAX_ROWS+1})
    require_row_limit(rows, label="支出紀錄匯出")
    export_rows=[[row["id"],_display_date(row["expense_date"]),_hkd(row["amount_hkd"]),row.get("note") or "",row.get("created_by") or "",row.get("created_at") or ""] for _,row in rows.iterrows()]
    filename=f"遲到罰款基金_支出紀錄_{logic.fiscal_label(year)}.csv"
    return csv_response(filename,["ID","支出日期","支出金額","備註","記錄人","記錄時間"],export_rows)


@router.post("/lateness-fund/notify/{year}")
def lateness_notify(year:int,body:LatenessNotifyBody,request:Request):
    from core import funds_logic as logic
    from core.push import notify_committee
    from deploy.proxy import _get_vapid
    user,db=_lateness_context(request,manager=True)
    if body.target not in {"outstanding","all"}: raise HTTPException(400,"不支援的通知對象。")
    data=logic.lateness_data(year,user_id=user,db=db);owed={str(row["member_user_id"]):float(row["owed"]) for row in data["outstanding_members"]}
    targets=list(owed) if body.target=="outstanding" else data["members"]
    custom=body.message.strip(); sent=0; notified=0
    all_default=(f"{data['label']} 年度尚有結欠遲到罰款的委員：{'、'.join(owed)}，請盡快繳交。" if owed else f"{data['label']} 年度暫無委員結欠遲到罰款。")
    for target in targets:
        message=custom or (f"你於 {data['label']} 年度尚欠遲到罰款 {_hkd(owed[target])}，請盡快繳交！" if body.target=="outstanding" else all_default)
        count=notify_committee(db,_get_vapid(),"💰 遲到罰款提醒",message,target_user=target,tag="lateness-fund-reminder",url="/lateness-fund")
        if count: notified+=1;sent+=count
    return {"ok":True,"target_count":len(targets),"notified_members":notified,"sent_devices":sent}


class AiTransaction(BaseModel):
    transaction_type: str = Field(max_length=40)
    amount: float
    provider: str = Field(default="general", max_length=80)
    payment_method: str = Field(default="", max_length=200)
    reference_no: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)


class StatusBody(BaseModel):
    status: str = Field(max_length=40)
    note: str = Field(default="", max_length=2000)


class AdminBody(BaseModel):
    kind: str = Field(max_length=80)
    target_hkd: float | None = None
    low_balance_hkd: float | None = None
    payment_instruction: str = Field(default="", max_length=4000)
    balance_usd: float | None = None


class AiBudgetBody(BaseModel):
    budget_month: str = Field(pattern=r"^\d{4}-\d{2}-01$")
    fx_hkd_per_usd: float = Field(default=7.8, gt=0, le=1000)
    allocations: dict = Field(default_factory=dict)


@router.get("/ai-fund/data")
def ai_data(request: Request):
    from core import funds_logic as logic
    user, db = _context(request); return logic.ai_data(user, db=db)


@router.get("/ai-fund/transactions")
def ai_transactions(request: Request, page: int = 1, status: str | None = None, transaction_type: str | None = None):
    from core import funds_logic as logic
    from schema import TABLE_AI_FUND_TRANSACTIONS
    user, db = _context(request)
    manager = is_ai_manager(user, db=db)
    clauses, params = ([] if manager else ["created_by=:user"]), ({} if manager else {"user": user})
    if status: clauses.append("status=:status"); params["status"] = status
    if transaction_type: clauses.append("transaction_type=:transaction_type"); params["transaction_type"] = transaction_type
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    page, _, offset = bounds(page); params["limit"] = PAGE_SIZE; params["offset"] = offset
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_AI_FUND_TRANSACTIONS} {where}", params)
    rows = db.query(f"SELECT {AI_TX_COLUMNS} FROM {TABLE_AI_FUND_TRANSACTIONS} {where} ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset", params)
    return payload(logic._rows(rows), page, total)


@router.get("/ai-fund/usage")
def ai_usage(request: Request, page: int = 1):
    from core import funds_logic as logic
    from schema import TABLE_AI_FUND_USAGE_LOGS
    user, db = _context(request)
    manager = is_ai_manager(user, db=db)
    where, params = ("", {}) if manager else ("WHERE user_id=:user", {"user": user})
    page, _, offset = bounds(page); params["limit"] = PAGE_SIZE; params["offset"] = offset
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_AI_FUND_USAGE_LOGS} {where}", params)
    rows = db.query(f"SELECT {AI_USAGE_COLUMNS} FROM {TABLE_AI_FUND_USAGE_LOGS} {where} ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset", params)
    return payload(logic._rows(rows), page, total)


@router.get("/ai-fund/usage-summary")
def ai_usage_summary(request: Request, page: int = 1):
    from core import funds_logic as logic
    user, db = _context(request)
    manager = is_ai_manager(user, db=db)
    page, _, offset = bounds(page)
    total = logic.ai_usage_summary_count(user, manager, db=db)
    rows = logic.ai_usage_summary(user, manager, db=db, limit=PAGE_SIZE, offset=offset)
    return payload(logic._rows(rows), page, total)


def _ai_export_context(request):
    from core import funds_logic as logic
    user, db = _context(request)
    return user, db, is_ai_manager(user, db=db), logic


@router.get("/ai-fund/export/transactions.csv")
def ai_transactions_csv(request: Request):
    from schema import TABLE_AI_FUND_TRANSACTIONS
    user, db, manager, logic = _ai_export_context(request)
    where, params = ("", {}) if manager else ("WHERE created_by=:user", {"user": user})
    params["export_limit"] = EXPORT_MAX_ROWS + 1
    rows = db.query(f"SELECT {AI_TX_COLUMNS} FROM {TABLE_AI_FUND_TRANSACTIONS} {where} ORDER BY created_at DESC,id DESC LIMIT :export_limit", params)
    require_row_limit(rows, label="AI基金交易匯出")
    labels = logic.AI_TRANSACTION_LABELS; providers = logic.AI_PROVIDER_LABELS
    statuses = {"pending":"待確認","confirmed":"已確認","rejected":"已拒絕"}
    return _csv_response("ai基金交易紀錄.csv",
        ["編號","類型","狀態","Provider","金額(HKD)","付款方式","Reference","備註","提交者","提交時間","確認者","確認時間","拒絕者","拒絕時間","狀態備註"],
        [[r.get("id"),labels.get(r.get("transaction_type"),r.get("transaction_type")),statuses.get(r.get("status"),r.get("status")),providers.get(r.get("provider"),r.get("provider")),r.get("amount_hkd"),r.get("payment_method"),r.get("reference_no"),r.get("note"),r.get("created_by"),r.get("created_at"),r.get("confirmed_by"),r.get("confirmed_at"),r.get("rejected_by"),r.get("rejected_at"),r.get("status_note")] for r in logic._rows(rows)])


@router.get("/ai-fund/export/usage.csv")
def ai_usage_csv(request: Request):
    from schema import TABLE_AI_FUND_USAGE_LOGS
    user, db, manager, logic = _ai_export_context(request)
    where, params = ("", {}) if manager else ("WHERE user_id=:user", {"user": user})
    params["export_limit"] = EXPORT_MAX_ROWS + 1
    rows = db.query(f"SELECT {AI_USAGE_COLUMNS} FROM {TABLE_AI_FUND_USAGE_LOGS} {where} ORDER BY created_at DESC,id DESC LIMIT :export_limit", params)
    require_row_limit(rows, label="AI用量匯出")
    statuses = {"success":"成功","failed":"失敗"}
    return _csv_response("ai用量估算紀錄.csv",
        ["編號","用戶","功能","模型","Provider","估算成本(USD)","估算成本(HKD)","Input tokens","Output tokens","Audio tokens","TTS計費字元","搜尋次數","任務ID","任務階段","成本來源","狀態","錯誤訊息","時間"],
        [[r.get("id"),r.get("user_id"),logic.AI_FEATURE_LABELS.get(r.get("feature"),r.get("feature")),r.get("model_label"),logic.AI_PROVIDER_LABELS.get(r.get("provider"),r.get("provider")),r.get("estimated_cost_usd"),r.get("estimated_cost_hkd"),r.get("input_tokens"),r.get("output_tokens"),r.get("audio_tokens"),r.get("billable_characters"),r.get("search_calls"),r.get("operation_id"),r.get("operation_stage"),r.get("cost_source"),statuses.get(r.get("status"),r.get("status")),r.get("error_message"),r.get("created_at")] for r in logic._rows(rows)])


@router.get("/ai-fund/export/usage-summary.csv")
def ai_usage_summary_csv(request: Request):
    user, db, manager, logic = _ai_export_context(request)
    rows = logic._rows(logic.ai_usage_summary(user, manager, db=db, limit=EXPORT_MAX_ROWS+1))
    require_row_limit(rows, label="AI用量統計匯出")
    return _csv_response("ai用量統計.csv", ["月份","用戶","Provider","功能","模型","任務數","成功呼叫","Provider呼叫","TTS計費字元","估算成本(HKD)"],
        [[r.get("month"),r.get("user_id"),logic.AI_PROVIDER_LABELS.get(r.get("provider"),r.get("provider")),logic.AI_FEATURE_LABELS.get(r.get("feature"),r.get("feature")),r.get("model_label"),r.get("tasks"),r.get("uses"),r.get("provider_calls"),r.get("billable_characters"),r.get("estimated_cost_hkd")] for r in rows])


@router.get("/ai-fund/openrouter-credit")
async def openrouter_credit(request: Request):
    """Fetch the live OpenRouter credit used by the fund overview."""
    _context(request)
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("OPENROUTER_MANAGEMENT_KEY")
    if not key:
        return {"ok": False, "message": "未設定 OPENROUTER_MANAGEMENT_KEY，未能讀取 OpenRouter credits。"}
    try:
        async with httpx.AsyncClient(timeout=OPENROUTER_CREDIT_TIMEOUT_SECONDS) as client:
            response = await client.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status(); payload = response.json()
        item = payload.get("data") or {}
        total = float(item.get("total_credits") or 0); used = float(item.get("total_usage") or 0)
        return {"ok": True, "total_credits_usd": total, "total_usage_usd": used, "remaining_credits_usd": total - used}
    except Exception as exc:
        return {"ok": False, "message": f"OpenRouter credits 讀取失敗：{exc}"}


@router.post("/ai-fund/deposits")
def ai_deposit(body: AiTransaction, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    try: logic.add_ai_transaction(user, "member_deposit", body.amount, "general", body.payment_method, body.reference_no, body.note, False, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@router.post("/ai-fund/admin/transactions")
def ai_admin_transaction(body: AiTransaction, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    if not is_ai_manager(user, db=db):
        raise HTTPException(403, "只有 AI管理員可新增已確認交易。")
    if not body.note.strip(): raise HTTPException(400, "請填寫原因 / 備註。")
    try: logic.add_ai_transaction(user, body.transaction_type, body.amount, body.provider, body.payment_method, body.reference_no, body.note, True, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@router.post("/ai-fund/admin/transactions/{transaction_id}/status")
def ai_status(transaction_id: int, body: StatusBody, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    if not is_ai_manager(user, db=db):
        raise HTTPException(403, "只有 AI管理員可處理入數。")
    try: updated = logic.set_ai_transaction_status(transaction_id, body.status, user, body.note, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    if not updated: raise HTTPException(409, "此入數已被處理。")
    return {"ok": True}


@router.post("/ai-fund/admin/settings")
def ai_settings(body: AdminBody, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    try: result = logic.save_ai_admin(user, body.model_dump(), db=db)
    except PermissionError as exc: raise HTTPException(403, str(exc))
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True, **(result or {})}


@router.post("/ai-fund/admin/budget")
def ai_budget_save(body: AiBudgetBody, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    try:
        budget = logic.save_ai_budget(user, body.model_dump(), db=db)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "monthly_budget": budget}


@router.post("/ai-fund/admin/budget/notify")
def ai_budget_notify(request: Request):
    from core import funds_logic as logic
    from deploy.proxy import _get_vapid
    user, db = _context(request)
    try:
        result = logic.notify_ai_budget(user, db, _get_vapid())
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True, **result}
