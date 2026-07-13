"""Committee-authenticated R2-only endpoints for match photos."""

import os
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count
from system_limits import (
    PHOTO_BATCH_MAX_ITEMS, PHOTO_DAILY_USER_LIMIT, PHOTO_MAX_BYTES,
    PHOTO_MAX_DIMENSION, PHOTO_MONTHLY_GLOBAL_LIMIT,
    PHOTO_THUMBNAIL_MAX_BYTES, PHOTO_THUMBNAIL_MAX_DIMENSION,
    R2_MEDIA_LINK_TTL_SECONDS,
    R2_OBJECT_CACHE_MAX_AGE_SECONDS, R2_UPLOAD_CLAIM_TTL_SECONDS,
)

router = APIRouter(prefix="/api/match-photos", tags=["match-photos"])


class PhotoUploadIntentBody(BaseModel):
    file_name: str = Field(max_length=240)
    mime_type: str = Field(default="image/webp", max_length=80)
    byte_size: int = Field(gt=0, le=PHOTO_MAX_BYTES)
    thumbnail_byte_size: int = Field(gt=0, le=PHOTO_THUMBNAIL_MAX_BYTES)
    sha256: str = Field(min_length=64, max_length=64)
    thumbnail_sha256: str = Field(min_length=64, max_length=64)
    width: int = Field(gt=0, le=PHOTO_MAX_DIMENSION)
    height: int = Field(gt=0, le=PHOTO_MAX_DIMENSION)


class PhotoCompleteBody(BaseModel):
    album_label: str = Field(min_length=1, max_length=200)
    match_video_id: int | None = None
    photo_date: str = Field(default="", max_length=20)
    photo_title: str = Field(default="", max_length=300)
    caption: str = Field(default="", max_length=2000)
    upload_tokens: list[str] = Field(min_length=1, max_length=PHOTO_BATCH_MAX_ITEMS)


def _context(request: Request):
    from deploy.proxy import _require_committee_user, get_vote_db
    return _require_committee_user(request), get_vote_db()


@router.get("/data")
def data(request: Request):
    from core import media_logic as logic
    from core import r2_storage
    _user_id, db = _context(request)
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定，相片功能已暫停。")
    result = logic.photo_data(db=db)
    result["storage"] = "r2"
    result["storage_budget"] = r2_storage.storage_budget_status(db, refresh=True)
    result["limits"] = {
        "batch_max_items": PHOTO_BATCH_MAX_ITEMS,
        "daily_user_limit": PHOTO_DAILY_USER_LIMIT,
        "monthly_global_limit": PHOTO_MONTHLY_GLOBAL_LIMIT,
        "photo_max_bytes": PHOTO_MAX_BYTES,
        "photo_max_dimension": PHOTO_MAX_DIMENSION,
        "thumbnail_max_bytes": PHOTO_THUMBNAIL_MAX_BYTES,
        "thumbnail_max_dimension": PHOTO_THUMBNAIL_MAX_DIMENSION,
        "r2_object_cache_max_age_seconds": R2_OBJECT_CACHE_MAX_AGE_SECONDS,
    }
    from deploy.proxy import bandwidth_budget_status
    result["bandwidth_budget"] = bandwidth_budget_status(notify=True)
    return result

@router.get("/photos")
def photos(request: Request, page: int = 1, album: str = "全部", search: str = "", sort: str = "date_desc"):
    from core import media_logic as logic
    from schema import TABLE_MATCH_PHOTOS
    _user, db = _context(request); logic.ensure_match_photos_table(db); page,_,offset=bounds(page)
    album = str(album or "")[:200]
    search = str(search or "")[:100]
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


@router.post("/upload-intent")
def upload_intent(body: PhotoUploadIntentBody, request: Request):
    """Issue two short-lived direct R2 PUTs: original and gallery thumbnail."""
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret

    user_id, _db = _context(request)
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 尚未完成設定。")
    storage_budget = r2_storage.storage_budget_status(_db, refresh=True)
    if storage_budget["blocked"]:
        stop_gb = storage_budget["stop_bytes"] / 1_000_000_000
        raise HTTPException(429, f"R2儲存量已達{stop_gb:g}GB保護上限，暫停新相片上載。")
    if body.mime_type not in {"image/webp", "image/jpeg", "image/png"}:
        raise HTTPException(400, "圖片格式只支援 JPEG、PNG 或 WebP。")
    if not 1_000 <= body.byte_size <= PHOTO_MAX_BYTES:
        raise HTTPException(400, f"每張圖片壓縮後不可超過 {PHOTO_MAX_BYTES // (1024 * 1024)}MB。")
    if not 500 <= body.thumbnail_byte_size <= PHOTO_THUMBNAIL_MAX_BYTES:
        raise HTTPException(400, f"圖片縮圖不可超過 {PHOTO_THUMBNAIL_MAX_BYTES // 1024}KB。")
    if not 1 <= body.width <= PHOTO_MAX_DIMENSION or not 1 <= body.height <= PHOTO_MAX_DIMENSION:
        raise HTTPException(400, f"圖片最長邊不可超過 {PHOTO_MAX_DIMENSION}px。")
    if not re.fullmatch(r"[0-9a-f]{64}", body.sha256.lower()) or not re.fullmatch(
        r"[0-9a-f]{64}", body.thumbnail_sha256.lower()
    ):
        raise HTTPException(400, "圖片雜湊格式不正確。")
    safe_user = re.sub(r"[^A-Za-z0-9_-]", "_", str(user_id))[:48] or "member"
    object_id = uuid.uuid4().hex
    ext = "webp" if body.mime_type == "image/webp" else "jpg" if body.mime_type == "image/jpeg" else "png"
    original_key = f"photos/original/{safe_user}/{object_id}.{ext}"
    thumbnail_key = f"photos/thumb/{safe_user}/{object_id}.{ext}"
    pending_original_key = f"pending/{original_key}"
    pending_thumbnail_key = f"pending/{thumbnail_key}"
    secret = _get_relay_cookie_secret()
    if not secret:
        raise HTTPException(503, "系統簽署設定不可用。")
    claim = {
        "kind": "photo", "intent_id": object_id, "user": str(user_id), "file_name": body.file_name[:240],
        "mime_type": body.mime_type, "byte_size": body.byte_size,
        "thumbnail_byte_size": body.thumbnail_byte_size, "sha256": body.sha256.lower(),
        "thumbnail_sha256": body.thumbnail_sha256.lower(), "width": body.width,
        "height": body.height, "r2_key": original_key,
        "thumbnail_r2_key": thumbnail_key, "pending_r2_key": pending_original_key,
        "pending_thumbnail_r2_key": pending_thumbnail_key,
    }
    token = r2_storage.sign_upload_claim(claim, secret, expires=R2_UPLOAD_CLAIM_TTL_SECONDS)
    reserved, scope = r2_storage.reserve_upload_intent(
        _db, intent_id=object_id, user_id=str(user_id), media_kind="photo",
        object_keys=[pending_original_key, pending_thumbnail_key],
        declared_bytes=body.byte_size + body.thumbnail_byte_size,
        user_daily_limit=PHOTO_DAILY_USER_LIMIT,
        global_monthly_limit=PHOTO_MONTHLY_GLOBAL_LIMIT,
    )
    if not reserved:
        stop_gb = storage_budget["stop_bytes"] / 1_000_000_000
        message = f"R2儲存量已達{stop_gb:g}GB保護上限，暫停新相片上載。" if scope == "storage_global" else (
            f"你今日申請的圖片上載次數已達{PHOTO_DAILY_USER_LIMIT}次，請翌日再試。"
            if scope == "user_daily" else "本月全系統圖片上載申請已達上限。"
        )
        raise HTTPException(429, message)
    return {
        "upload_token": token,
        "original": {
            "url": r2_storage.presign_put(pending_original_key, body.mime_type, body.sha256, body.byte_size),
            "key": pending_original_key,
            "headers": {
                "Cache-Control": f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
                "x-amz-meta-sha256": body.sha256.lower(),
            },
        },
        "thumbnail": {
            "url": r2_storage.presign_put(pending_thumbnail_key, body.mime_type, body.thumbnail_sha256, body.thumbnail_byte_size),
            "key": pending_thumbnail_key,
            "headers": {
                "Cache-Control": f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
                "x-amz-meta-sha256": body.thumbnail_sha256.lower(),
            },
        },
        "required_headers": {
            "Content-Type": body.mime_type,
            "Cache-Control": f"private, max-age={R2_OBJECT_CACHE_MAX_AGE_SECONDS}",
        },
    }


@router.post("/upload-complete")
def upload_complete(body: PhotoCompleteBody, request: Request):
    from core import media_logic as logic
    from core import r2_storage
    from deploy.proxy import _get_relay_cookie_secret

    user_id, db = _context(request)
    if not body.upload_tokens or len(body.upload_tokens) > PHOTO_BATCH_MAX_ITEMS:
        raise HTTPException(400, f"每次必須上載一至{PHOTO_BATCH_MAX_ITEMS}張圖片。")
    if any(len(token) > 20_000 for token in body.upload_tokens):
        raise HTTPException(400, "圖片上載憑證格式無效。")
    logic.ensure_match_photos_table(db)
    now = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
    if body.photo_date:
        try:
            datetime.strptime(body.photo_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(400, "相片日期格式無效。") from exc
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    usage = db.query("""SELECT
        COUNT(*) FILTER (WHERE uploaded_by=:user AND created_at>=:day_start) AS user_today,
        COUNT(*) FILTER (WHERE created_at>=:month_start) AS global_month
        FROM match_photos""", {
        "user": user_id, "day_start": day_start, "month_start": month_start,
    })
    user_today = int(usage.iloc[0]["user_today"] or 0) if not usage.empty else 0
    global_month = int(usage.iloc[0]["global_month"] or 0) if not usage.empty else 0
    if user_today + len(body.upload_tokens) > PHOTO_DAILY_USER_LIMIT:
        raise HTTPException(429, f"為控制儲存及網絡傳輸量，每位委員每日最多可上載{PHOTO_DAILY_USER_LIMIT}張相片。")
    if global_month + len(body.upload_tokens) > PHOTO_MONTHLY_GLOBAL_LIMIT:
        raise HTTPException(429, "本月全系統相片上載限額已用完，請於下月再試。")
    secret = _get_relay_cookie_secret()
    files = []
    seen_intents = set()

    def discard_claims(items, *, include_final=False, mark_deleted=True):
        for item in items:
            keys = [item.get("pending_r2_key"), item.get("pending_thumbnail_r2_key")]
            if include_final:
                keys.extend((item.get("r2_key"), item.get("thumbnail_r2_key")))
            for key in keys:
                if key:
                    try: r2_storage.delete(key)
                    except Exception: pass
            if mark_deleted:
                try: r2_storage.mark_upload_intent_deleted(db, str(item.get("intent_id") or ""))
                except Exception: pass

    for token in body.upload_tokens:
        claim = r2_storage.verify_upload_claim(token, secret or "")
        if not claim or claim.get("kind") != "photo" or claim.get("user") != str(user_id):
            discard_claims(files)
            raise HTTPException(400, "圖片上載憑證無效或已過期。")
        intent_id = str(claim.get("intent_id") or "")
        if not intent_id or intent_id in seen_intents:
            discard_claims(files)
            raise HTTPException(400, "圖片上載憑證重複或缺少intent。")
        seen_intents.add(intent_id)
        try:
            original = r2_storage.head(claim["pending_r2_key"])
            thumbnail = r2_storage.head(claim["pending_thumbnail_r2_key"])
        except Exception as exc:
            discard_claims(files)
            # A missing pending object can be a replay of an already completed
            # claim. Never remove its final object or alter its completed intent.
            discard_claims([claim], mark_deleted=False)
            raise HTTPException(400, "R2 未能確認圖片已完成上載。") from exc
        original_sha = str((original.get("Metadata") or {}).get("sha256") or "")
        thumb_sha = str((thumbnail.get("Metadata") or {}).get("sha256") or "")
        if (
            int(original.get("ContentLength") or 0) != int(claim["byte_size"])
            or int(thumbnail.get("ContentLength") or 0) != int(claim["thumbnail_byte_size"])
            or str(original.get("ContentType") or "").split(";", 1)[0] != claim["mime_type"]
            or str(thumbnail.get("ContentType") or "").split(";", 1)[0] != claim["mime_type"]
            or original_sha != claim["sha256"]
            or thumb_sha != claim["thumbnail_sha256"]
        ):
            discard_claims([*files, claim])
            raise HTTPException(400, "R2 圖片大小或雜湊驗證失敗。")
        files.append(claim)

    def discard_batch():
        discard_claims(files, include_final=True)

    try:
        for claim in files:
            r2_storage.promote(claim["pending_r2_key"], claim["r2_key"])
            r2_storage.promote(claim["pending_thumbnail_r2_key"], claim["thumbnail_r2_key"])
    except Exception as exc:
        discard_batch()
        raise HTTPException(502, "R2圖片由暫存區轉入正式儲存失敗。") from exc
    try:
        result = logic.register_r2_photos(
            user_id, body.album_label, body.match_video_id, body.photo_date,
            body.photo_title, body.caption, files, db=db,
        )
    except Exception as exc:
        discard_batch()
        raise HTTPException(502, "圖片metadata未能以交易方式寫入，已清理本批R2檔案。") from exc
    if not result.get("ok"):
        discard_batch()
        raise HTTPException(400, result.get("message") or "圖片metadata不正確。")
    return result


@router.get("/image/{photo_id}")
def image(photo_id: int, request: Request, download: bool = False, thumbnail: bool = False):
    from core import media_logic as logic
    from core import r2_storage
    _user_id, db = _context(request)
    media = logic.photo_media(photo_id, db=db)
    if not media:
        raise HTTPException(404, "找不到圖片")
    r2_key = media.get("thumbnail_r2_key") if thumbnail and not download else media.get("r2_key")
    if not r2_storage.configured():
        raise HTTPException(503, "Cloudflare R2 暫時不可用。")
    if not r2_key:
        raise HTTPException(409, "相片尚未完成R2遷移。")
    return RedirectResponse(r2_storage.presign_get(
        r2_key, mime_type=media["mime_type"], file_name=media["file_name"],
        download=download, expires=R2_MEDIA_LINK_TTL_SECONDS,
    ), status_code=307, headers={"Cache-Control": "private, no-store"})
