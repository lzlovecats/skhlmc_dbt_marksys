"""Password-scoped API for reviewing and exporting final score sheets."""

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import Response as BinaryResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/review", tags=["review"])
COOKIE = "review_match"


class LoginBody(BaseModel):
    match_id: str = Field(max_length=200)
    password: str = Field(max_length=512)


def db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


def scope(request):
    from deploy.proxy import _verify_review_token

    value = _verify_review_token(request.cookies.get(COOKIE) or "")
    if not value:
        raise HTTPException(401, "請先驗證查閱分紙密碼。")
    return value


@router.get("/matches")
def matches():
    from core.review_logic import available_matches

    return {"matches": available_matches(db())}


@router.post("/login")
def login(body: LoginBody, response: Response):
    from core.review_logic import verify_review_access
    from deploy.proxy import _sign_review_token

    result = verify_review_access(body.match_id, body.password, db())
    if not result["ok"]:
        raise HTTPException(401, result["message"])
    token = _sign_review_token(body.match_id)
    if not token:
        raise HTTPException(503, "登入服務暫時未能使用。")
    response.set_cookie(COOKIE, token, path="/", samesite="lax", httponly=True)
    return {"ok": True}


@router.get("/data")
def data(request: Request, judge_name: str | None = None):
    from core.review_logic import review_data

    return review_data(scope(request), judge_name, db())


@router.get('/pdf')
def pdf(request: Request, judge_name: str | None = None):
    from core.review_logic import review_data
    from score_sheet_pdf import build_score_sheet_pdf

    match_id = scope(request)
    database = db()
    payload = review_data(match_id, judge_name, database)
    if not payload.get("has_scores") or not payload.get("record"):
        raise HTTPException(404, "此場次暫未有評分紀錄。請稍後再試或向賽會人員查詢。")
    if payload.get("missing_sides"):
        missing = "、".join(payload["missing_sides"])
        raise HTTPException(409, f"此評判的最終分紙細項資料不完整（缺少：{missing}），請聯絡賽會人員。")

    record = payload["record"]
    try:
        content = build_score_sheet_pdf(
            record,
            record,
            payload["sides"]["正方"],
            payload["sides"]["反方"],
        )
    except Exception as exc:
        raise HTTPException(500, f"產生 PDF 失敗：{exc}") from exc

    safe_match = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(match_id))
    safe_judge = "".join(
        ch if ch.isalnum() or ch in "-_" else "_"
        for ch in str(payload["selected_judge"])
    )
    filename = f"{safe_match}_{safe_judge}_評判評分表.pdf"
    return BinaryResponse(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Content-Encoding": "identity",
        },
    )


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
