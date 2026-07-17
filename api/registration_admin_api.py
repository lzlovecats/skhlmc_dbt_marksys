"""Organiser-authenticated JSON endpoints for registration management."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from api.access import require_competition_staff
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count
from api.resource_limits import EXPORT_MAX_ROWS, csv_response, require_row_limit
from system_limits import REGISTRATION_ADMIN_SESSION_TTL_SECONDS

router = APIRouter(prefix="/api/registration-admin", tags=["registration-admin"])
COOKIE_NAME = "registration_admin"


class LoginBody(BaseModel):
    password: str = Field(max_length=512)


class SettingsBody(BaseModel):
    competition_edition: int
    registration_start: str = Field(max_length=40)
    registration_end: str = Field(max_length=40)


class StatusBody(BaseModel):
    registration_id: int
    status: str = Field(max_length=40)


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


def _require_admin(request: Request):
    return require_competition_staff(request)


def _record_filters(edition: int, status: str, search: str = ""):
    from core.registration_logic import STATUS_LABELS
    params = {"edition": edition}
    where = "WHERE competition_edition=:edition"
    if status in STATUS_LABELS:
        where += " AND status=:status"
        params["status"] = status
    search = (search or "").strip()[:100]
    if search:
        params["search"] = f"%{search}%"
        where += """ AND (
            CAST(id AS TEXT) ILIKE :search OR team_name ILIKE :search OR
            main_debater_name ILIKE :search OR first_deputy_name ILIKE :search OR
            second_deputy_name ILIKE :search OR closing_debater_name ILIKE :search OR
            contact_name ILIKE :search OR contact_class ILIKE :search OR
            contact_phone ILIKE :search
        )"""
    return where, params


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    from core import registration_logic as logic
    from core.auth_logic import login_rate_limit_retry_after
    from deploy.proxy import _sign_registration_admin_token
    retry_after = login_rate_limit_retry_after(request, "registration_admin")
    if retry_after is not None:
        raise HTTPException(
            429,
            "登入嘗試次數過多，請稍後再試。",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        result = logic.check_admin_password(body.password, db=_db())
    except Exception as exc:
        raise HTTPException(503, "登入服務暫時未能使用。") from exc
    if not result["ok"]:
        raise HTTPException(401, result["message"])
    token = _sign_registration_admin_token()
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用")
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=REGISTRATION_ADMIN_SESSION_TTL_SECONDS,
        path="/",
        samesite="lax",
        httponly=True,
        secure=True,
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(
        COOKIE_NAME, path="/", samesite="lax", httponly=True, secure=True,
    )
    return {"ok": True}


@router.get("/data")
def data(request: Request, edition: int | None = None, status: str = "全部"):
    from core import registration_logic as logic
    _require_admin(request)
    try:
        return logic.registration_admin_data(edition, status, db=_db())
    except Exception as exc:
        raise HTTPException(503, f"連線錯誤: {exc}") from exc


@router.get("/records")
def records(request: Request, edition: int, status: str = "全部", search: str = "", page: int = 1):
    from core.registration_logic import _record_payload
    from schema import TABLE_COMPETITION_REGISTRATIONS
    _require_admin(request); db = _db(); page, _, offset = bounds(page)
    where, params = _record_filters(edition, status, search)
    total = scalar_count(db, f"SELECT COUNT(*) total FROM {TABLE_COMPETITION_REGISTRATIONS} {where}", params)
    params.update(limit=PAGE_SIZE, offset=offset)
    frame = db.query(f"SELECT id,competition_edition,team_name,main_debater_name,first_deputy_name,second_deputy_name,closing_debater_name,contact_name,contact_class,contact_phone,status,submitted_at,updated_at FROM {TABLE_COMPETITION_REGISTRATIONS} {where} ORDER BY submitted_at DESC,id DESC LIMIT :limit OFFSET :offset", params)
    return payload([_record_payload(row) for _, row in frame.iterrows()], page, total)

@router.get("/export")
def export_records(request: Request, edition: int, status: str = "全部", search: str = ""):
    from core.registration_logic import _record_payload
    from schema import TABLE_COMPETITION_REGISTRATIONS
    _require_admin(request); db=_db(); where,params=_record_filters(edition,status,search)
    params["export_limit"] = EXPORT_MAX_ROWS + 1
    frame=db.query(f"SELECT id,competition_edition,team_name,main_debater_name,first_deputy_name,second_deputy_name,closing_debater_name,contact_name,contact_class,contact_phone,status,submitted_at,updated_at FROM {TABLE_COMPETITION_REGISTRATIONS} {where} ORDER BY submitted_at DESC,id DESC LIMIT :export_limit",params)
    require_row_limit(frame, label="報名紀錄匯出")
    columns=[("id","編號"),("competition_edition","屆數"),("team_name","隊名"),("main_debater_name","主辯"),("first_deputy_name","一副"),("second_deputy_name","二副"),("closing_debater_name","結辯"),("contact_name","聯絡人"),("contact_class","班別"),("contact_phone","聯絡電話"),("status_label","狀態"),("submitted_at","提交時間"),("updated_at","更新時間")]
    export_rows=[]
    for _,row in frame.iterrows():
        item=_record_payload(row); export_rows.append([item.get(key,"") for key,_ in columns])
    return csv_response(f"competition_registrations_{edition}.csv",[x[1] for x in columns],export_rows)


@router.post("/settings")
def save_settings(body: SettingsBody, request: Request):
    from core import registration_logic as logic
    _require_admin(request)
    try:
        return logic.save_registration_settings(
            body.competition_edition, body.registration_start, body.registration_end, db=_db()
        )
    except Exception as exc:
        raise HTTPException(503, f"儲存報名設定失敗：{exc}") from exc


@router.post("/status")
def update_status(body: StatusBody, request: Request):
    from core import registration_logic as logic
    _require_admin(request)
    try:
        return logic.update_registration_status(body.registration_id, body.status, db=_db())
    except Exception as exc:
        raise HTTPException(503, f"更新報名狀態失敗：{exc}") from exc
