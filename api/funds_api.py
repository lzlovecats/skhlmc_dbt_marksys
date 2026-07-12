"""Committee-authenticated endpoints for the HTML fund ledgers."""

import httpx

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count

router = APIRouter(prefix="/api", tags=["funds"])


def _context(request):
    from deploy.proxy import _require_committee_user, get_vote_db
    return _require_committee_user(request), get_vote_db()


class LatenessRecord(BaseModel):
    late_date: str
    member_user_id: str
    late_minutes: int
    paid_amount: float = 0
    note: str = ""


class LatenessExpense(BaseModel):
    expense_date: str
    amount: float
    note: str = ""


class AmountBody(BaseModel):
    amount: float


@router.get("/lateness-fund/data")
def lateness_data(request: Request, year: int | None = None):
    from core import funds_logic as logic
    _, db = _context(request)
    return logic.lateness_data(year, db=db)


@router.get("/lateness-fund/records")
def lateness_records(request: Request, year: int, page: int = 1):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _, db = _context(request); logic._ensure_lateness(db); start, end = logic.fiscal_range(year)
    params = {"start": start.isoformat(), "end": end.isoformat()}; page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end", params)
    params.update(limit=PAGE_SIZE, offset=offset)
    rows = db.query(f"""WITH ranked AS (SELECT id,late_date,member_user_id,late_minutes,COALESCE(paid_amount,0) paid_amount,note,created_by,created_at,updated_at,ROW_NUMBER() OVER (PARTITION BY member_user_id,(CASE WHEN EXTRACT(MONTH FROM late_date)>=9 THEN EXTRACT(YEAR FROM late_date) ELSE EXTRACT(YEAR FROM late_date)-1 END) ORDER BY late_date,id) late_no FROM {TABLE_LATENESS_FUND_RECORDS}) SELECT *,late_no*late_minutes penalty_amount,paid_amount-(late_no*late_minutes) record_balance FROM ranked WHERE late_date BETWEEN :start AND :end ORDER BY late_date DESC,id DESC LIMIT :limit OFFSET :offset""", params)
    return payload(logic._rows(rows), page, total)


@router.get("/lateness-fund/expenses")
def lateness_expenses(request: Request, year: int, page: int = 1):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_EXPENSES
    _, db = _context(request); logic._ensure_lateness(db); start, end = logic.fiscal_range(year)
    params = {"start": start.isoformat(), "end": end.isoformat()}; page, _, offset = bounds(page)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_LATENESS_FUND_EXPENSES} WHERE expense_date BETWEEN :start AND :end", params)
    params.update(limit=PAGE_SIZE, offset=offset)
    rows = db.query(f"SELECT id,expense_date,amount_hkd,note,created_by,created_at FROM {TABLE_LATENESS_FUND_EXPENSES} WHERE expense_date BETWEEN :start AND :end ORDER BY expense_date DESC,id DESC LIMIT :limit OFFSET :offset", params)
    return payload(logic._rows(rows), page, total)

@router.get("/lateness-fund/summary")
def lateness_summary(request: Request, year: int, page: int = 1):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _,db=_context(request);logic._ensure_lateness(db);start,end=logic.fiscal_range(year);page,_,offset=bounds(page);params={"start":start.isoformat(),"end":end.isoformat()}
    total=scalar_count(db,f"SELECT COUNT(DISTINCT member_user_id) total FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end",params);params.update(limit=PAGE_SIZE,offset=offset)
    rows=db.query(f"""WITH ranked AS (SELECT member_user_id,late_minutes,COALESCE(paid_amount,0) paid_amount,ROW_NUMBER() OVER(PARTITION BY member_user_id ORDER BY late_date,id) late_no FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end), grouped AS (SELECT member_user_id,COUNT(*) late_count,SUM(late_minutes) total_late_minutes,SUM(late_no*late_minutes) penalty_amount,SUM(paid_amount) paid_amount FROM ranked GROUP BY member_user_id) SELECT DENSE_RANK() OVER(ORDER BY total_late_minutes DESC) late_rank,*,paid_amount-penalty_amount balance FROM grouped ORDER BY total_late_minutes DESC,member_user_id LIMIT :limit OFFSET :offset""",params)
    return payload(logic._rows(rows),page,total)

@router.get("/lateness-fund/member-count")
def lateness_member_count(request:Request,year:int,member:str):
    from core import funds_logic as logic
    from schema import TABLE_LATENESS_FUND_RECORDS
    _,db=_context(request);start,end=logic.fiscal_range(year)
    return {"count":scalar_count(db,f"SELECT COUNT(*) total FROM {TABLE_LATENESS_FUND_RECORDS} WHERE late_date BETWEEN :start AND :end AND member_user_id=:member",{"start":start.isoformat(),"end":end.isoformat(),"member":member})}


@router.post("/lateness-fund/opening/{year}")
def lateness_opening(year: int, body: AmountBody, request: Request):
    from core import funds_logic as logic
    _, db = _context(request); logic.set_lateness_opening(year, body.amount, db=db)
    return {"ok": True}


@router.post("/lateness-fund/carry/{year}")
def lateness_carry(year: int, request: Request):
    from core import funds_logic as logic
    _, db = _context(request); return {"ok": True, "amount": logic.carry_lateness_opening(year, db=db)}


@router.post("/lateness-fund/records")
def lateness_record(body: LatenessRecord, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    try: logic.add_lateness_record(user, body.late_date, body.member_user_id, body.late_minutes, body.paid_amount, body.note, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@router.patch("/lateness-fund/records/{record_id}")
def lateness_record_update(record_id: int, body: AmountBody, request: Request):
    from core import funds_logic as logic
    _, db = _context(request); return {"ok": bool(logic.update_lateness_paid(record_id, body.amount, db=db))}


@router.delete("/lateness-fund/records/{record_id}")
def lateness_record_delete(record_id: int, request: Request):
    from core import funds_logic as logic
    _, db = _context(request); return {"ok": bool(logic.delete_lateness("record", record_id, db=db))}


@router.post("/lateness-fund/expenses")
def lateness_expense(body: LatenessExpense, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    try: logic.add_lateness_expense(user, body.expense_date, body.amount, body.note, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@router.delete("/lateness-fund/expenses/{expense_id}")
def lateness_expense_delete(expense_id: int, request: Request):
    from core import funds_logic as logic
    _, db = _context(request); return {"ok": bool(logic.delete_lateness("expense", expense_id, db=db))}


class AiTransaction(BaseModel):
    transaction_type: str
    amount: float
    provider: str = "general"
    payment_method: str = ""
    reference_no: str = ""
    note: str = ""


class StatusBody(BaseModel):
    status: str
    note: str = ""


class AdminBody(BaseModel):
    kind: str
    target_hkd: float | None = None
    low_balance_hkd: float | None = None
    payment_instruction: str = ""
    balance_usd: float | None = None


@router.get("/ai-fund/data")
def ai_data(request: Request):
    from core import funds_logic as logic
    user, db = _context(request); return logic.ai_data(user, db=db)


@router.get("/ai-fund/transactions")
def ai_transactions(request: Request, page: int = 1, status: str | None = None, transaction_type: str | None = None):
    from core import funds_logic as logic
    from schema import TABLE_AI_FUND_TRANSACTIONS
    user, db = _context(request); treasurer = logic.ai_data(user, db=db)["is_treasurer"]
    clauses, params = ([] if treasurer else ["created_by=:user"]), ({} if treasurer else {"user": user})
    if status: clauses.append("status=:status"); params["status"] = status
    if transaction_type: clauses.append("transaction_type=:transaction_type"); params["transaction_type"] = transaction_type
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    page, _, offset = bounds(page); params["limit"] = PAGE_SIZE; params["offset"] = offset
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_AI_FUND_TRANSACTIONS} {where}", params)
    rows = db.query(f"SELECT * FROM {TABLE_AI_FUND_TRANSACTIONS} {where} ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset", params)
    return payload(logic._rows(rows), page, total)


@router.get("/ai-fund/usage")
def ai_usage(request: Request, page: int = 1):
    from core import funds_logic as logic
    from schema import TABLE_AI_FUND_USAGE_LOGS
    user, db = _context(request); treasurer = logic.ai_data(user, db=db)["is_treasurer"]
    where, params = ("", {}) if treasurer else ("WHERE user_id=:user", {"user": user})
    page, _, offset = bounds(page); params["limit"] = PAGE_SIZE; params["offset"] = offset
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_AI_FUND_USAGE_LOGS} {where}", params)
    rows = db.query(f"SELECT * FROM {TABLE_AI_FUND_USAGE_LOGS} {where} ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset", params)
    return payload(logic._rows(rows), page, total)


@router.get("/ai-fund/openrouter-credit")
async def openrouter_credit(request: Request):
    """Same live OpenRouter credit lookup used by the Streamlit fund overview."""
    _context(request)
    from deploy.proxy import _get_proxy_secret
    key = _get_proxy_secret("OPENROUTER_MANAGEMENT_KEY")
    if not key:
        return {"ok": False, "message": "未設定 OPENROUTER_MANAGEMENT_KEY，未能讀取 OpenRouter credits。"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
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
    if not logic.ai_data(user, db=db)["is_treasurer"]: raise HTTPException(403, "只有 AI基金管理員可新增已確認交易。")
    if not body.note.strip(): raise HTTPException(400, "請填寫原因 / 備註。")
    try: logic.add_ai_transaction(user, body.transaction_type, body.amount, body.provider, body.payment_method, body.reference_no, body.note, True, db=db)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@router.post("/ai-fund/admin/transactions/{transaction_id}/status")
def ai_status(transaction_id: int, body: StatusBody, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    if not logic.ai_data(user, db=db)["is_treasurer"]: raise HTTPException(403, "只有 AI基金管理員可處理入數。")
    return {"ok": bool(logic.set_ai_transaction_status(transaction_id, body.status, user, body.note, db=db))}


@router.post("/ai-fund/admin/settings")
def ai_settings(body: AdminBody, request: Request):
    from core import funds_logic as logic
    user, db = _context(request)
    try: logic.save_ai_admin(user, body.model_dump(), db=db)
    except PermissionError as exc: raise HTTPException(403, str(exc))
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"ok": True}
