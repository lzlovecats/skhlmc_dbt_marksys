"""Public bearer-link API for scheduled topic reveal and one team veto."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel


router = APIRouter(prefix="/api/match-topic-release", tags=["match-topic-release"])


class VetoBody(BaseModel):
    confirm: Literal[True]


def _db():
    from deploy.proxy import get_vote_db

    return get_vote_db()


@router.get("/data")
def data(
    response: Response,
    token: str = Query(default="", max_length=128),
):
    from core.match_topic_release import TopicReleaseError, public_data

    try:
        payload = public_data(token, db=_db())
    except TopicReleaseError as exc:
        raise HTTPException(409, str(exc), headers={"Cache-Control": "no-store"}) from exc
    except Exception as exc:
        raise HTTPException(
            503,
            "辯題公布功能尚未完成資料庫準備。",
            headers={"Cache-Control": "no-store"},
        ) from exc
    if payload is None:
        raise HTTPException(
            404,
            "辯題連結無效或已被重新產生，請向賽會索取最新連結。",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    return payload


@router.post("/veto")
def veto(
    body: VetoBody,
    response: Response,
    token: str = Query(default="", max_length=128),
):
    from core.match_topic_release import TopicReleaseError, submit_veto

    try:
        result = submit_veto(token, db=_db())
    except TopicReleaseError as exc:
        raise HTTPException(409, str(exc), headers={"Cache-Control": "no-store"}) from exc
    except Exception as exc:
        raise HTTPException(
            503,
            "辯題否決功能暫時不可用。",
            headers={"Cache-Control": "no-store"},
        ) from exc
    if not result.get("ok"):
        status_code = {
            "invalid": 404,
            "expired": 410,
            "not_revealed": 409,
            "closed": 409,
            "used": 409,
        }.get(result.get("reason"), 409)
        raise HTTPException(
            status_code,
            result.get("message") or "未能提交辯題否決。",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    return result
