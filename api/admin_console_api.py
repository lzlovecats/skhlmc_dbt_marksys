"""HTML APIs for the last two privileged Streamlit consoles.

All authority stays server-side: developer and SQL verifications are short-lived
HttpOnly sessions and the SQL runner deliberately keeps the legacy guards.
"""
import datetime
import json
import re
import secrets
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from core.auth_logic import hash_password, verify_password
from ai_model_config import AI_MODEL_OPTIONS, DEFAULT_AI_MODEL
from schema import (
    CREATE_BUG_REPORTS,
    TABLE_ACCOUNTS,
    TABLE_BUG_REPORTS,
    TABLE_PUSH_SUBSCRIPTIONS,
    init_db,
)
from version import APP_VERSION
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count

router = APIRouter(prefix="/api", tags=["admin-console"])
_SESSIONS = {}
TTL_SECONDS = 60 * 60 * 4


class PasswordBody(BaseModel): password: str
class SqlBody(BaseModel): sql: str; confirmed: bool = False
class PasswordChange(BaseModel): current_password: str = ""; new_password: str; confirm_password: str = ""
class BugUpdate(BaseModel): status: str; reply: str = ""; fixed_version: str = ""
class AccountBody(BaseModel): user_id: str; password: str = ""
class AccountAccessBody(BaseModel): disabled: bool
class JsonSettings(BaseModel): values: dict
class PushBody(BaseModel): title: str; body: str; url: str = "/vote"; target_user: str | None = None; confirmed: bool = False
class BypassBody(BaseModel): users: list[str] = Field(default_factory=list); expires_at: str = ""; revoke_user: str = ""; revoke_all: bool = False


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
def _json_list_config(db,key):
    try: value=json.loads(_config(db,key) or "[]")
    except Exception: value=[]
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip())) if isinstance(value,list) else []


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
    configs={k:_config(db,k) or "" for k in ("maintenance_mode","bypass_active_check_until","tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","lateness_fund_managers","ai_enabled_providers","ai_default_model")}
    account_rows=db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE user_id NOT IN ('admin','developer','') AND COALESCE(account_disabled,FALSE)=FALSE ORDER BY user_id")
    login_disabled=set(_json_list_config(db,"login_disabled_accounts"))
    account_options=[str(value).strip() for value in account_rows.get("user_id",[]) if str(value).strip() and str(value).strip() not in login_disabled]
    inactive_rows=db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE account_status='inactive' AND COALESCE(account_disabled,FALSE)=FALSE ORDER BY user_id")
    inactive_accounts=[str(value).strip() for value in inactive_rows.get("user_id",[]) if str(value).strip() and str(value).strip() not in login_disabled]
    providers=sorted({str(config.get("provider") or "").strip() for config in AI_MODEL_OPTIONS.values() if config.get("provider")})
    from deploy.proxy import _get_proxy_secret
    provider_keys={provider:next((str(config.get("api_key") or "") for config in AI_MODEL_OPTIONS.values() if config.get("provider")==provider),"") for provider in providers}
    models=[{"label":label,"provider":config.get("provider","")} for label,config in AI_MODEL_OPTIONS.items()]
    subs=_rows(db.query(f"SELECT user_id,COUNT(*) AS device_count FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE is_active=TRUE GROUP BY user_id ORDER BY user_id"))
    return {"version":APP_VERSION,"bugs":bugs,"accounts":accounts,"account_options":account_options,"inactive_accounts":inactive_accounts,"ai_options":{"providers":providers,"models":models,"default_model":DEFAULT_AI_MODEL,"key_status":{provider:bool(key and _get_proxy_secret(key)) for provider,key in provider_keys.items()},"key_names":provider_keys},"configs":configs,"subscriptions":subs}

@router.get("/developer/collection/{kind}")
def dev_collection(kind:str,request:Request,page:int=1):
    _require(request,"developer"); db=_db(); db.execute(CREATE_BUG_REPORTS); page,_,offset=bounds(page)
    specs={
      "bugs":(TABLE_BUG_REPORTS,"CASE status WHEN 'open' THEN 1 WHEN 'investigating' THEN 2 WHEN 'not_reproducible' THEN 3 WHEN 'fixed' THEN 4 WHEN 'duplicate' THEN 5 WHEN 'closed' THEN 6 ELSE 7 END, created_at DESC","id,reporter_user_id,affected_page,device_info,reproduction_steps,expected_result,actual_result,extra_notes,status,developer_reply,fixed_version,created_at,updated_at,resolved_at"),
      "accounts":(TABLE_ACCOUNTS,"user_id","user_id,account_status,active_since,last_login_at,account_disabled"),
    }
    if kind=="subscriptions":
        total=scalar_count(db,f"SELECT COUNT(DISTINCT user_id) total FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE is_active=TRUE")
        rows=_rows(db.query(f"SELECT user_id,COUNT(*) AS device_count FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE is_active=TRUE GROUP BY user_id ORDER BY user_id LIMIT :limit OFFSET :offset",{"limit":PAGE_SIZE,"offset":offset}))
        return payload(rows,page,total)
    if kind not in specs: raise HTTPException(404,"資料集不存在")
    table,order,cols=specs[kind]; total=scalar_count(db,f"SELECT COUNT(*) total FROM {table}")
    rows=_rows(db.query(f"SELECT {cols} FROM {table} ORDER BY {order} LIMIT :limit OFFSET :offset",{"limit":PAGE_SIZE,"offset":offset}))
    if kind=="accounts":
        disabled=set(_json_list_config(db,"login_disabled_accounts"))
        for row in rows: row["login_disabled"]=str(row.get("user_id") or "") in disabled
    return payload(rows,page,total)

@router.post("/developer/bugs/{bug_id}")
def update_bug(bug_id:int,body:BugUpdate,request:Request):
    _require(request,"developer")
    allowed=("open","investigating","fixed","not_reproducible","duplicate","closed")
    labels={"open":"待處理","investigating":"調查中","fixed":"已修正","not_reproducible":"未能重現","duplicate":"重複回報","closed":"已關閉"}
    closed=("fixed","closed","duplicate","not_reproducible")
    if body.status not in allowed: raise HTTPException(400,"無效的 Bug 狀態")
    if body.status=="fixed" and not body.fixed_version.strip(): raise HTTPException(400,"標記已修正時必須填寫修正版本")
    if body.status in closed and not body.reply.strip(): raise HTTPException(400,"請填寫回覆")
    db=_db(); report=db.query(f"SELECT reporter_user_id FROM {TABLE_BUG_REPORTS} WHERE id=:id",{"id":bug_id})
    if report.empty: raise HTTPException(404,"Bug 回報不存在")
    now=_now(); db.execute(f"UPDATE {TABLE_BUG_REPORTS} SET status=:status,developer_reply=:reply,fixed_version=:version,updated_at=:now,resolved_at=:resolved WHERE id=:id",{"status":body.status,"reply":body.reply.strip(),"version":body.fixed_version.strip(),"now":now,"resolved":now if body.status in closed else None,"id":bug_id})
    try:
        from core.push import notify_committee
        from deploy.proxy import _get_vapid
        notify_committee(db,_get_vapid(),"Bug 回報已更新",f"#{bug_id} {labels[body.status]}"+(f"｜修正版本：{body.fixed_version.strip()}" if body.fixed_version.strip() else ""),target_user=str(report.iloc[0]["reporter_user_id"] or "") or None,tag=f"bug-report-{bug_id}",url="/bug-report")
    except Exception:
        pass
    return {"ok":True}

@router.post("/developer/password/{key}")
def change_system_password(key:str,body:PasswordChange,request:Request):
    _require(request,"developer")
    if key not in ("admin_password","developer_password","sql_password"): raise HTTPException(404,"設定不存在")
    if not body.new_password: raise HTTPException(400,"請輸入新密碼")
    if body.new_password != body.confirm_password: raise HTTPException(400,"兩次輸入的密碼不一致")
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
    if uid in ("admin","developer",""): raise HTTPException(400,"不可在此重設系統帳戶")
    if not body.password: raise HTTPException(400,"請輸入新密碼")
    db=_db()
    if db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:uid",{"uid":uid}).empty: raise HTTPException(404,"帳戶不存在")
    db.execute(f"UPDATE {TABLE_ACCOUNTS} SET password_hash=:pw WHERE user_id=:uid",{"pw":hash_password(body.password),"uid":uid}); return {"ok":True}

@router.patch("/developer/accounts/{uid}/access")
def set_account_access(uid:str,body:AccountAccessBody,request:Request):
    _require(request,"developer")
    uid=uid.strip()
    if uid in ("admin","developer",""): raise HTTPException(400,"不可停用系統帳戶")
    db=_db()
    if db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:uid",{"uid":uid}).empty: raise HTTPException(404,"帳戶不存在")
    disabled=set(_json_list_config(db,"login_disabled_accounts"))
    updates={}
    if body.disabled:
        for key,label in (("tts_recording_reviewers","AI 訓練管理員"),("lateness_fund_managers","遲到基金管理員")):
            members=_json_list_config(db,key)
            remaining=[member for member in members if member!=uid and member not in disabled]
            if uid in members and not remaining: raise HTTPException(400,f"請先加入另一位{label}，再停用此帳戶")
            if uid in members: updates[key]=[member for member in members if member!=uid]
        for key in ("tts_recording_allowed_users","ai_fund_treasurers"):
            members=_json_list_config(db,key)
            if uid in members: updates[key]=[member for member in members if member!=uid]
        try: bypass=json.loads(_config(db,"bypass_active_check_until") or "{}")
        except Exception: bypass={}
        if isinstance(bypass,dict) and uid in bypass:
            bypass.pop(uid,None); updates["bypass_active_check_until"]=bypass
        disabled.add(uid)
    else:
        disabled.discard(uid)
    updates["login_disabled_accounts"]=sorted(disabled)
    with db.transaction() as conn:
        for key,value in updates.items():
            conn.execute(text("INSERT INTO system_config(key,value,updated_at) VALUES(:key,:value,:now) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at"),{"key":key,"value":json.dumps(value,ensure_ascii=False),"now":_now()})
        if body.disabled:
            conn.execute(text(f"UPDATE {TABLE_PUSH_SUBSCRIPTIONS} SET is_active=FALSE,updated_at=:now WHERE user_id=:uid"),{"uid":uid,"now":_now()})
    return {"ok":True,"disabled":body.disabled}

@router.post("/developer/push")
def send_manual_push(body:PushBody,request:Request):
    _require(request,"developer")
    if not body.confirmed: raise HTTPException(400,"請先確認立即發送")
    if not body.title.strip() or not body.body.strip(): raise HTTPException(400,"請輸入通知標題及內容")
    url=body.url.strip()
    if not url.startswith("/") or url.startswith("//") or "\\" in url: raise HTTPException(400,"開啟路徑必須是站內 / 路徑")
    db=_db()
    if body.target_user and db.query(f"SELECT 1 FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE user_id=:uid AND is_active=TRUE",{"uid":body.target_user}).empty: raise HTTPException(400,"指定委員目前沒有有效推送訂閱")
    from core.push import notify_committee
    from deploy.proxy import _get_vapid
    sent=notify_committee(db,_get_vapid(),body.title.strip(),body.body.strip(),target_user=body.target_user,tag=f"manual-push-{_now().strftime('%Y%m%d%H%M%S')}",url=url)
    return {"ok":True,"sent":sent}

@router.post("/developer/init-db")
def developer_init_db(request:Request):
    _require(request,"developer"); db=_db()
    with db._engine.connect() as conn: init_db(conn)
    return {"ok":True}

@router.post("/developer/bypass")
def update_bypass(body:BypassBody,request:Request):
    _require(request,"developer"); db=_db()
    try: current=json.loads(_config(db,"bypass_active_check_until") or "{}")
    except Exception: current={}
    if not isinstance(current,dict): current={}
    if body.revoke_all: current={}
    elif body.revoke_user: current.pop(body.revoke_user,None)
    else:
        users=list(dict.fromkeys(str(uid).strip() for uid in body.users if str(uid).strip()))
        if not users: raise HTTPException(400,"請選擇至少一位委員")
        try: expires=datetime.datetime.strptime(body.expires_at.strip(),"%Y-%m-%d %H:%M")
        except ValueError as exc: raise HTTPException(400,"到期時間格式不正確") from exc
        now_hk=datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
        if expires <= now_hk: raise HTTPException(400,"到期時間必須在未來")
        valid={str(x).strip() for x in db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE account_status='inactive' AND COALESCE(account_disabled,FALSE)=FALSE").get("user_id",[])}
        valid-=set(_json_list_config(db,"login_disabled_accounts"))
        invalid=sorted(set(users)-valid)
        if invalid: raise HTTPException(400,"只可為非活躍帳戶開放："+"、".join(invalid))
        current.update({uid:body.expires_at.strip() for uid in users})
    _set(db,"bypass_active_check_until",json.dumps(current,ensure_ascii=False))
    return {"ok":True,"bypasses":current}

@router.post("/developer/settings")
def developer_settings(body:JsonSettings,request:Request):
    _require(request,"developer"); db=_db(); allowed={"maintenance_mode","tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","lateness_fund_managers","ai_enabled_providers","ai_default_model"}
    account_list_keys={"tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","lateness_fund_managers"}
    cleaned={}
    for key,value in body.values.items():
        if key not in allowed: raise HTTPException(400,f"不允許的設定：{key}")
        if key in account_list_keys:
            if not isinstance(value,list): raise HTTPException(400,f"{key} 必須是帳戶清單")
            value=list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        cleaned[key]=value
    if "lateness_fund_managers" in cleaned and not cleaned["lateness_fund_managers"]:
        raise HTTPException(400,"至少保留一位遲到基金管理員")
    if "tts_recording_reviewers" in cleaned and not cleaned["tts_recording_reviewers"]:
        raise HTTPException(400,"至少保留一位 AI 訓練管理員")
    if "maintenance_mode" in cleaned:
        value=str(cleaned["maintenance_mode"]).strip().lower()
        if value not in ("true","false"): raise HTTPException(400,"維護模式值無效")
        cleaned["maintenance_mode"]=value
    if "ai_enabled_providers" in cleaned or "ai_default_model" in cleaned:
        providers=cleaned.get("ai_enabled_providers")
        if providers is None:
            try: providers=json.loads(_config(db,"ai_enabled_providers") or "[]")
            except Exception: providers=[]
        if not isinstance(providers,list): raise HTTPException(400,"Provider 設定無效")
        providers=list(dict.fromkeys(str(item).strip() for item in providers if str(item).strip()))
        valid_providers={str(item.get("provider") or "") for item in AI_MODEL_OPTIONS.values()}
        if not providers or set(providers)-valid_providers: raise HTTPException(400,"請至少啟用一個有效 Provider")
        model=str(cleaned.get("ai_default_model") or _config(db,"ai_default_model") or DEFAULT_AI_MODEL).strip()
        if model not in AI_MODEL_OPTIONS or AI_MODEL_OPTIONS[model].get("provider") not in providers: raise HTTPException(400,"預設模型必須屬於已啟用的 Provider")
        cleaned["ai_enabled_providers"]=providers; cleaned["ai_default_model"]=model
    requested={user for key,value in cleaned.items() if key in account_list_keys for user in value}
    if requested:
        rows=db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE user_id NOT IN ('admin','developer','') AND COALESCE(account_disabled,FALSE)=FALSE")
        valid={str(value).strip() for value in rows.get("user_id",[]) if str(value).strip()}
        valid-=set(_json_list_config(db,"login_disabled_accounts"))
        invalid=sorted(requested-valid)
        if invalid: raise HTTPException(400,"帳戶不存在或已停用："+"、".join(invalid))
    with db.transaction() as conn:
        for key,value in cleaned.items():
            stored=json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else str(value)
            conn.execute(text("INSERT INTO system_config(key,value,updated_at) VALUES(:key,:value,:now) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at"),{"key":key,"value":stored,"now":_now()})
    return {"ok":True}
