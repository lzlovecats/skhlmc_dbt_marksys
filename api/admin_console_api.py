"""HTML APIs for the last two privileged Streamlit consoles.

All authority stays server-side: developer and SQL verifications are short-lived
HttpOnly sessions and the SQL runner deliberately keeps the legacy guards.
"""
import datetime
import json
import re
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.auth_logic import hash_password, verify_password
from schema import CREATE_BUG_REPORTS, TABLE_ACCOUNTS, TABLE_BUG_REPORTS, TABLE_PUSH_SUBSCRIPTIONS
from version import APP_VERSION
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count

router = APIRouter(prefix="/api", tags=["admin-console"])
_SESSIONS = {}
TTL_SECONDS = 60 * 60 * 4


class PasswordBody(BaseModel): password: str
class SqlBody(BaseModel): sql: str; confirmed: bool = False
class PasswordChange(BaseModel): current_password: str = ""; new_password: str
class BugUpdate(BaseModel): status: str; reply: str = ""; fixed_version: str = ""
class AccountBody(BaseModel): user_id: str; password: str = ""
class JsonSettings(BaseModel): values: dict
class PushBody(BaseModel): title: str; body: str; url: str = "/vote"; target_user: str | None = None; confirmed: bool = False


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()

def _now(): return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
def _config(db, key):
    x=db.query("SELECT value FROM system_config WHERE key=:key",{"key":key}); return None if x.empty else str(x.iloc[0]["value"])
def _set(db,key,value): db.execute("INSERT INTO system_config(key,value,updated_at) VALUES(:key,:value,:now) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",{"key":key,"value":value,"now":_now()})
def _issue(response, kind):
    token=secrets.token_urlsafe(32); _SESSIONS[token]={"kind":kind,"expires":_now()+datetime.timedelta(seconds=TTL_SECONDS)}
    response.set_cookie(f"{kind}_session",token,max_age=TTL_SECONDS,path="/",samesite="lax",httponly=True); return token
def _has(request,kind):
    row=_SESSIONS.get(request.cookies.get(f"{kind}_session", "")); return bool(row and row["kind"]==kind and row["expires"]>_now())
def _require(request,kind):
    if not _has(request,kind): raise HTTPException(401,"未登入或驗證已過期")
def _rows(df): return [dict(x) for x in df.to_dict(orient="records")]


@router.post("/db-management/login")
def db_login(body:PasswordBody,response:Response):
    db=_db(); stored=_config(db,"admin_password")
    if not stored or not verify_password(body.password,stored): raise HTTPException(401,"密碼錯誤")
    _issue(response,"db_admin"); return {"ok":True}

@router.post("/db-management/verify")
def sql_verify(body:PasswordBody,request:Request,response:Response):
    _require(request,"db_admin"); stored=_config(_db(),"sql_password")
    if not stored or not verify_password(body.password,stored): raise HTTPException(401,"SQL 存取密碼錯誤")
    _issue(response,"sql"); return {"ok":True}

@router.get("/db-management/data")
def db_data(request:Request):
    _require(request,"sql"); db=_db()
    tables=_rows(db.query("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"))
    logs=_rows(db.query("SELECT * FROM login_records ORDER BY logged_in_at DESC LIMIT 50"))
    return {"tables":[x["table_name"] for x in tables],"logs":logs}

@router.get("/db-management/logs")
def db_logs(request:Request,page:int=1):
    _require(request,"sql"); db=_db(); page,_,offset=bounds(page)
    total=scalar_count(db,"SELECT COUNT(*) total FROM login_records")
    rows=_rows(db.query("SELECT * FROM login_records ORDER BY logged_in_at DESC LIMIT :limit OFFSET :offset",{"limit":PAGE_SIZE,"offset":offset}))
    return payload(rows,page,total)

def _unsafe(sql):
    compact=sql.strip().rstrip(";"); upper=compact.upper()
    if not compact: return "請輸入 SQL"
    if ";" in compact: return "每次只可執行一條 SQL"
    if re.search(r"\bSYSTEM_CONFIG\b",upper): return "此頁不可存取 system_config"
    if re.search(r"\b(DROP|TRUNCATE|ALTER|CREATE)\b",upper): return "此頁不可執行 DDL 語句"
    return ""

@router.post("/db-management/execute")
def sql_execute(body:SqlBody,request:Request):
    _require(request,"sql"); sql=body.sql.strip(); error=_unsafe(sql)
    if error: raise HTTPException(400,error)
    upper=sql.upper(); dangerous=bool(re.search(r"\b(UPDATE\s+.+?\s+SET|DELETE\s+FROM)\b",upper,re.S)) and not bool(re.search(r"\bWHERE\b",upper))
    if dangerous and not body.confirmed: return {"requires_confirmation":True,"sql":sql}
    db=_db(); engine=db._engine
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            result=conn.execute(text(sql)); is_select=upper.startswith("SELECT") or upper.startswith("WITH")
            if is_select:
                cols=list(result.keys()); rows=[dict(zip(cols,row)) for row in result.fetchall()]
                return {"kind":"select","columns":cols,"rows":[{k:str(v) if v is not None else None for k,v in r.items()} for r in rows]}
            return {"kind":"dml","count":result.rowcount}
    except Exception as exc: raise HTTPException(400,f"執行失敗：{exc}") from exc


@router.post("/developer/login")
def dev_login(body:PasswordBody,response:Response):
    db=_db(); stored=_config(db,"developer_password")
    if not stored: raise HTTPException(503,"尚未設定開發者密碼")
    if not verify_password(body.password,stored): raise HTTPException(401,"密碼錯誤")
    _issue(response,"developer"); return {"ok":True}

@router.post("/developer/logout")
def dev_logout(response:Response): response.delete_cookie("developer_session",path="/"); return {"ok":True}

@router.get("/developer/data")
def dev_data(request:Request):
    _require(request,"developer"); db=_db(); db.execute(CREATE_BUG_REPORTS)
    bugs=[]; accounts=[]
    configs={k:_config(db,k) or "" for k in ("maintenance_mode","bypass_active_check_until","tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","ai_enabled_providers","ai_default_model")}
    subs=[]
    return {"version":APP_VERSION,"bugs":bugs,"accounts":accounts,"configs":configs,"subscriptions":subs}

@router.get("/developer/collection/{kind}")
def dev_collection(kind:str,request:Request,page:int=1):
    _require(request,"developer"); db=_db(); db.execute(CREATE_BUG_REPORTS); page,_,offset=bounds(page)
    specs={
      "bugs":(TABLE_BUG_REPORTS,"created_at DESC","id,reporter_user_id,affected_page,device_info,reproduction_steps,expected_result,actual_result,extra_notes,status,developer_reply,fixed_version,created_at,updated_at,resolved_at"),
      "accounts":(TABLE_ACCOUNTS,"user_id","user_id,account_status,active_since,last_login_at,account_disabled"),
    }
    if kind=="subscriptions":
        total=scalar_count(db,f"SELECT COUNT(DISTINCT user_id) total FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE is_active=TRUE")
        rows=_rows(db.query(f"SELECT user_id,COUNT(*) AS device_count FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE is_active=TRUE GROUP BY user_id ORDER BY user_id LIMIT :limit OFFSET :offset",{"limit":PAGE_SIZE,"offset":offset}))
        return payload(rows,page,total)
    if kind not in specs: raise HTTPException(404,"資料集不存在")
    table,order,cols=specs[kind]; total=scalar_count(db,f"SELECT COUNT(*) total FROM {table}")
    rows=_rows(db.query(f"SELECT {cols} FROM {table} ORDER BY {order} LIMIT :limit OFFSET :offset",{"limit":PAGE_SIZE,"offset":offset}))
    return payload(rows,page,total)

@router.post("/developer/bugs/{bug_id}")
def update_bug(bug_id:int,body:BugUpdate,request:Request):
    _require(request,"developer")
    closed=("fixed","closed","duplicate","not_reproducible")
    if body.status=="fixed" and not body.fixed_version.strip(): raise HTTPException(400,"標記已修正時必須填寫修正版本")
    if body.status in closed and not body.reply.strip(): raise HTTPException(400,"請填寫回覆")
    now=_now(); _db().execute(f"UPDATE {TABLE_BUG_REPORTS} SET status=:status,developer_reply=:reply,fixed_version=:version,updated_at=:now,resolved_at=:resolved WHERE id=:id",{"status":body.status,"reply":body.reply.strip(),"version":body.fixed_version.strip(),"now":now,"resolved":now if body.status in closed else None,"id":bug_id}); return {"ok":True}

@router.post("/developer/password/{key}")
def change_system_password(key:str,body:PasswordChange,request:Request):
    _require(request,"developer")
    if key not in ("admin_password","developer_password","sql_password"): raise HTTPException(404,"設定不存在")
    if not body.new_password: raise HTTPException(400,"請輸入新密碼")
    db=_db(); stored=_config(db,key)
    if key!="admin_password" and (not body.current_password or not stored or not verify_password(body.current_password,stored)): raise HTTPException(401,"目前密碼錯誤")
    _set(db,key,hash_password(body.new_password)); return {"ok":True}

@router.post("/developer/accounts")
def create_account(body:AccountBody,request:Request):
    _require(request,"developer"); uid=body.user_id.strip()
    if not uid or not body.password: raise HTTPException(400,"請輸入用戶名稱及密碼")
    db=_db(); exists=db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:uid",{"uid":uid})
    if not exists.empty: raise HTTPException(400,"此用戶名稱已存在")
    db.execute(f"INSERT INTO {TABLE_ACCOUNTS}(user_id,password_hash,account_status) VALUES(:uid,:pw,'inactive')",{"uid":uid,"pw":hash_password(body.password)}); return {"ok":True}

@router.post("/developer/accounts/{uid}/password")
def reset_account(uid:str,body:AccountBody,request:Request):
    _require(request,"developer");
    if not body.password: raise HTTPException(400,"請輸入新密碼")
    _db().execute(f"UPDATE {TABLE_ACCOUNTS} SET password_hash=:pw WHERE user_id=:uid",{"pw":hash_password(body.password),"uid":uid}); return {"ok":True}

@router.delete("/developer/accounts/{uid}")
def delete_account(uid:str,request:Request): _require(request,"developer"); _db().execute(f"DELETE FROM {TABLE_ACCOUNTS} WHERE user_id=:uid",{"uid":uid}); return {"ok":True}

@router.post("/developer/settings")
def developer_settings(body:JsonSettings,request:Request):
    _require(request,"developer"); db=_db(); allowed={"maintenance_mode","bypass_active_check_until","tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","ai_enabled_providers","ai_default_model"}
    for key,value in body.values.items():
        if key not in allowed: raise HTTPException(400,f"不允許的設定：{key}")
        _set(db,key,json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else str(value))
    return {"ok":True}
