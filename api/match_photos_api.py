"""Committee-authenticated JSON and image endpoints for match photos."""

import base64

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count

router = APIRouter(prefix="/api/match-photos", tags=["match-photos"])


class PhotoFileBody(BaseModel):
    file_name: str
    mime_type: str = "image/jpeg"
    image_base64: str


class UploadBody(BaseModel):
    album_label: str
    match_video_id: int | None = None
    photo_date: str = ""
    photo_title: str = ""
    caption: str = ""
    files: list[PhotoFileBody]


def _context(request: Request):
    from deploy.proxy import _require_committee_user, get_vote_db
    return _require_committee_user(request), get_vote_db()


@router.get("/data")
def data(request: Request):
    from core import media_logic as logic
    _user_id, db = _context(request)
    return logic.photo_data(db=db)

@router.get("/photos")
def photos(request: Request, page: int = 1, album: str = "全部", search: str = "", sort: str = "date_desc"):
    from core import media_logic as logic
    from schema import TABLE_MATCH_PHOTOS
    _user, db = _context(request); logic.ensure_match_photos_table(db); page,_,offset=bounds(page)
    clauses=[];params={}
    if album!="全部": clauses.append("album_label=:album");params["album"]=album
    if search.strip(): clauses.append("LOWER(COALESCE(album_label,'')||' '||COALESCE(photo_title,'')||' '||COALESCE(caption,'')||' '||COALESCE(uploaded_by,'')||' '||COALESCE(file_name,'')) LIKE :search");params["search"]="%"+search.strip().lower()+"%"
    where="WHERE "+" AND ".join(clauses) if clauses else ""
    orders={"date_asc":"photo_date ASC NULLS LAST,created_at DESC,id DESC","created_desc":"created_at DESC,id DESC","created_asc":"created_at ASC,id ASC"};order=orders.get(sort,"photo_date DESC NULLS LAST,created_at DESC,id DESC")
    total=scalar_count(db,f"SELECT COUNT(*) total FROM {TABLE_MATCH_PHOTOS} {where}",params)
    params.update(limit=PAGE_SIZE,offset=offset)
    frame=db.query(f"SELECT id,album_label,match_video_id,photo_date,photo_title,caption,file_name,mime_type,uploaded_by,created_at FROM {TABLE_MATCH_PHOTOS} {where} ORDER BY {order} LIMIT :limit OFFSET :offset",params)
    items=[]
    for _,r in frame.iterrows():
        items.append({k:logic.format_value(r.get(k)) for k in ("id","album_label","match_video_id","photo_date","photo_title","caption","file_name","mime_type","uploaded_by","created_at")})
    return payload(items,page,total)


@router.post("/upload")
def upload(body: UploadBody, request: Request):
    from core import media_logic as logic
    user_id, db = _context(request)
    files = []
    for file in body.files:
        try:
            image_data = base64.b64decode(file.image_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(400, "圖片資料格式無效。") from exc
        files.append({"file_name": file.file_name, "mime_type": file.mime_type, "image_data": image_data})
    return logic.upload_photos(
        user_id, body.album_label, body.match_video_id, body.photo_date,
        body.photo_title, body.caption, files, db=db,
    )


@router.get("/image/{photo_id}")
def image(photo_id: int, request: Request, download: bool = False):
    from core import media_logic as logic
    _user_id, db = _context(request)
    photo = logic.photo_bytes(photo_id, db=db)
    if not photo:
        raise HTTPException(404, "找不到圖片")
    headers = {"Content-Disposition": f'attachment; filename="{photo["file_name"]}"'} if download else {}
    return Response(content=photo["image_data"], media_type=photo["mime_type"], headers=headers)
