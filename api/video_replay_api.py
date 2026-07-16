"""Committee-authenticated JSON endpoints for the HTML replay page."""

from fastapi import APIRouter, Query, Request
from api.access import require_page_user
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/video-replay", tags=["video-replay"])


class VoteBody(BaseModel):
    video_id: int
    vote_choice: str = Field(max_length=20)


class CommentBody(BaseModel):
    video_id: int
    comment_text: str = Field(min_length=1, max_length=1000)


class ChapterItem(BaseModel):
    chapter_label: str = Field(max_length=80)
    enabled: bool
    time_text: str = Field(default="", max_length=20)
    is_best_debater: bool | None = None


class RosterItem(BaseModel):
    role_label: str = Field(max_length=40)
    user_id: str | None = Field(default=None, max_length=200)


class ChaptersBody(BaseModel):
    video_id: int
    chapters: list[ChapterItem] = Field(max_length=30)
    best_debater_role: str | None = Field(default=None, max_length=40)
    assignments: list[RosterItem] | None = Field(default=None, max_length=10)


def _context(request: Request):
    from deploy.proxy import get_vote_db
    return require_page_user(request, "video_replay"), get_vote_db()


@router.get("/data")
def data(
    request: Request,
    video_id: int | None = None,
    mine_only: bool = False,
    member_user_ids: list[str] | None = Query(default=None),
):
    from core import media_logic as logic
    user_id, db = _context(request)
    return logic.replay_data(
        user_id,
        video_id,
        mine_only=mine_only,
        participant_user_ids=member_user_ids,
        db=db,
    )

@router.get("/comments")
def comments(request: Request, video_id: int, page: int = 1):
    from core import media_logic as logic
    from schema import TABLE_VIDEO_COMMENTS
    _user, db = _context(request); page,_,offset=bounds(page)
    params={"video_id":video_id}; total=scalar_count(db,f"SELECT COUNT(*) total FROM {TABLE_VIDEO_COMMENTS} WHERE video_id=:video_id",params)
    params.update(limit=PAGE_SIZE,offset=offset)
    frame=db.query(f"SELECT user_id,comment_text,created_at FROM {TABLE_VIDEO_COMMENTS} WHERE video_id=:video_id ORDER BY created_at DESC LIMIT :limit OFFSET :offset",params)
    items=[{"user_id":logic.clean_text(r["user_id"]),"comment_text":logic.clean_text(r["comment_text"]),"created_at":logic.format_time(r.get("created_at"))} for _,r in frame.iterrows()]
    return payload(items,page,total)


@router.post("/vote")
def vote(body: VoteBody, request: Request):
    from core import media_logic as logic
    user_id, db = _context(request)
    return logic.save_vote(body.video_id, user_id, body.vote_choice, db=db)


@router.post("/comment")
def comment(body: CommentBody, request: Request):
    from core import media_logic as logic
    user_id, db = _context(request)
    return logic.add_comment(body.video_id, user_id, body.comment_text, db=db)


@router.post("/chapters")
def chapters(body: ChaptersBody, request: Request):
    from core import media_logic as logic
    _user_id, db = _context(request)
    best_role = (
        body.best_debater_role
        if "best_debater_role" in body.model_fields_set
        else logic.PRESERVE_BEST_DEBATER
    )
    result = logic.save_chapters(
        body.video_id,
        [item.model_dump(exclude_unset=True) for item in body.chapters],
        best_debater_role=best_role,
        db=db,
    )
    if not result.get("ok") or body.assignments is None:
        return result
    roster_result = logic.save_video_roster(
        body.video_id,
        [item.model_dump() for item in body.assignments],
        db=db,
    )
    if not roster_result.get("ok"):
        return {
            "ok": False,
            "message": (
                "章節時間表已更新，但委員帳戶連結未能儲存："
                + roster_result.get("message", "未知錯誤")
            ),
        }
    return {
        **result,
        "message": "章節時間表及委員帳戶連結已更新。",
        "roster": roster_result.get("roster", []),
    }
