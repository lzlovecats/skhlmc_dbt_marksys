"""Organiser-authenticated JSON endpoints for registration management."""

from fastapi import APIRouter, HTTPException, Request, Response
import csv, io
from pydantic import BaseModel
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count

router = APIRouter(prefix="/api/registration-admin", tags=["registration-admin"])
COOKIE_NAME = "registration_admin"


class LoginBody(BaseModel):
    password: str


class SettingsBody(BaseModel):
    competition_edition: int
    registration_start: str
    registration_end: str


class StatusBody(BaseModel):
    registration_id: int
    status: str


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


def _require_admin(request: Request):
    from deploy.proxy import _verify_registration_admin_token
    if not _verify_registration_admin_token(request.cookies.get(COOKIE_NAME) or ""):
        raise HTTPException(401, "未登入")


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
def login(body: LoginBody, response: Response):
    from core import registration_logic as logic
    from deploy.proxy import _sign_registration_admin_token
    try:
        result = logic.check_admin_password(body.password, db=_db())
    except Exception as exc:
        raise HTTPException(503, f"登入失敗：{exc}") from exc
    if not result["ok"]:
        raise HTTPException(401, result["message"])
    token = _sign_registration_admin_token()
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用")
    # Streamlit's admin gate is session-scoped, so this deliberately has no max_age.
    response.set_cookie(COOKIE_NAME, token, path="/", samesite="lax", httponly=True)
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
    frame=db.query(f"SELECT id,competition_edition,team_name,main_debater_name,first_deputy_name,second_deputy_name,closing_debater_name,contact_name,contact_class,contact_phone,status,submitted_at,updated_at FROM {TABLE_COMPETITION_REGISTRATIONS} {where} ORDER BY submitted_at DESC,id DESC",params)
    columns=[("id","編號"),("competition_edition","屆數"),("team_name","隊名"),("main_debater_name","主辯"),("first_deputy_name","一副"),("second_deputy_name","二副"),("closing_debater_name","結辯"),("contact_name","聯絡人"),("contact_class","班別"),("contact_phone","聯絡電話"),("status_label","狀態"),("submitted_at","提交時間"),("updated_at","更新時間")]
    output=io.StringIO(); writer=csv.writer(output); writer.writerow([x[1] for x in columns])
    for _,row in frame.iterrows():
        item=_record_payload(row); writer.writerow([item.get(key,"") for key,_ in columns])
    return Response(content="\ufeff"+output.getvalue(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="competition_registrations_{edition}.csv"'})


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
