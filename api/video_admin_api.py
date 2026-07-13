"""Organiser-authenticated JSON endpoints for HTML video management."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/video-admin", tags=["video-admin"])


class VideoBody(BaseModel):
    video_source: str = Field(max_length=40)
    match_id: str | None = Field(default=None, max_length=200)
    match_label: str = Field(default="", max_length=200)
    standalone_topic_text: str = Field(default="", max_length=500)
    standalone_pro_team: str = Field(default="", max_length=100)
    standalone_con_team: str = Field(default="", max_length=100)
    video_title: str = Field(default="", max_length=300)
    youtube_url: str = Field(default="", max_length=1000)
    display_order: int = 0
    is_visible: bool = True


class ImportBody(BaseModel):
    csv_text: str = Field(default="", max_length=500_000)
    parse_from_title: bool = True


class ChapterItem(BaseModel):
    chapter_label: str = Field(max_length=40)
    enabled: bool
    time_text: str = Field(default="", max_length=20)


class ChaptersBody(BaseModel):
    chapters: list[ChapterItem] = Field(max_length=30)


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


def _require_admin(request: Request):
    from deploy.proxy import _verify_registration_admin_token
    if not _verify_registration_admin_token(request.cookies.get("registration_admin") or ""):
        raise HTTPException(401, "未登入")


@router.get("/data")
def data(request: Request, video_id: int | None = None, page: int = 1):
    from core import media_logic as logic
    _require_admin(request)
    return logic.video_admin_data(video_id, page=page, page_size=20, db=_db())


@router.post("/videos")
def add_video(body: VideoBody, request: Request):
    from core import media_logic as logic
    _require_admin(request)
    return logic.add_video(body.model_dump(), db=_db())


@router.put("/videos/{video_id}")
def update_video(video_id: int, body: VideoBody, request: Request):
    from core import media_logic as logic
    _require_admin(request)
    return logic.update_video(video_id, body.model_dump(), db=_db())


@router.delete("/videos/{video_id}")
def delete_video(video_id: int, request: Request):
    from core import media_logic as logic
    _require_admin(request)
    return logic.delete_video(video_id, db=_db())


@router.post("/import")
def import_videos(body: ImportBody, request: Request):
    from core import media_logic as logic
    _require_admin(request)
    return logic.import_videos(body.csv_text, body.parse_from_title, db=_db())


@router.post("/videos/{video_id}/chapters")
def save_chapters(video_id: int, body: ChaptersBody, request: Request):
    from core import media_logic as logic
    _require_admin(request)
    return logic.save_chapters(video_id, [item.model_dump() for item in body.chapters], db=_db())
