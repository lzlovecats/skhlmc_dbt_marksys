"""APIs for the privileged developer and database consoles.

All authority stays server-side: developer and SQL verifications are short-lived
HttpOnly sessions and the SQL runner applies strict statement/result guards.
"""
import datetime
import json
import re
import secrets
import threading
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from account_access import (
    NON_MEMBER_ACCOUNT_DB_KEYS, account_id_can_be_created,
    is_non_member_account, is_protected_account,
)
from core.auth_logic import hash_password, verify_password
from core.config_store import (
    get_config,
    get_configs,
    get_configs_from_connection,
    set_config,
    set_configs_on_connection,
)
from ai_model_config import (
    AI_MODEL_OPTIONS,
    DEFAULT_AI_MODEL,
    resolve_interactive_model_settings,
)
from schema import (
    TABLE_ACCOUNTS,
    TABLE_BUG_REPORTS,
    TABLE_PUSH_SUBSCRIPTIONS,
)
from version import APP_VERSION
from api.pagination import PAGE_SIZE, bounds, payload, scalar_count
from system_limits import (
    ACCOUNT_INVENTORY_LIMIT, ADMIN_RECENT_LOGIN_LIMIT, ADMIN_SESSION_TTL_SECONDS,
    MAX_ADMIN_CONSOLE_SESSIONS, SQL_RESULT_MAX_BYTES,
    SQL_RESULT_MAX_CELL_CHARS, SQL_RESULT_MAX_ROWS, SQL_STATEMENT_TIMEOUT_MS,
)

router = APIRouter(prefix="/api", tags=["admin-console"])
_SESSIONS = {}
_session_lock = threading.RLock()
TTL_SECONDS = ADMIN_SESSION_TTL_SECONDS
MAX_SERVER_SESSIONS = MAX_ADMIN_CONSOLE_SESSIONS


class PasswordBody(BaseModel): password: str = Field(max_length=512)
class SqlBody(BaseModel): sql: str = Field(max_length=100_000); confirmed: bool = False
class PasswordChange(BaseModel): current_password: str = Field(default="", max_length=512); new_password: str = Field(max_length=512); confirm_password: str = Field(default="", max_length=512)
class BugUpdate(BaseModel): status: str = Field(max_length=40); reply: str = Field(default="", max_length=5000); fixed_version: str = Field(default="", max_length=80)
class AccountBody(BaseModel): user_id: str = Field(max_length=200); password: str = Field(default="", max_length=512)
class AccountAccessBody(BaseModel): disabled: bool
class JsonSettings(BaseModel): values: dict
class PushBody(BaseModel): title: str = Field(max_length=200); body: str = Field(max_length=2000); url: str = Field(default="/vote", max_length=500); target_user: str | None = Field(default=None, max_length=200); confirmed: bool = False
class BypassBody(BaseModel): users: list[str] = Field(default_factory=list, max_length=ACCOUNT_INVENTORY_LIMIT); expires_at: str = Field(default="", max_length=40); revoke_user: str = Field(default="", max_length=200); revoke_all: bool = False


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()

def _now(): return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
def _config(db, key):
    return get_config(db,key)
def _configs(db, keys):
    return get_configs(db,keys)
def _set(db,key,value): set_config(db,key,value)


def _password_needs_upgrade(stored):
    return not str(stored or "").startswith(("$2a$","$2b$","$2y$"))


def _verify_config_password_and_upgrade(db,key,plain,stored):
    if not stored or not verify_password(plain,str(stored)):
        return False
    if _password_needs_upgrade(stored):
        set_config(db,key,hash_password(plain))
    return True
def _prune_sessions():
    """Drop expired/old console sessions so repeated logins cannot grow RAM forever."""
    with _session_lock:
        now = _now()
        for token, row in list(_SESSIONS.items()):
            if row["expires"] <= now:
                _SESSIONS.pop(token, None)
        overflow = len(_SESSIONS) - MAX_SERVER_SESSIONS
        if overflow > 0:
            oldest = sorted(_SESSIONS, key=lambda token: _SESSIONS[token]["created"])
            for token in oldest[:overflow]:
                _SESSIONS.pop(token, None)


def _issue(response, kind):
    with _session_lock:
        _prune_sessions()
        token=secrets.token_urlsafe(32); now=_now()
        _SESSIONS[token]={"kind":kind,"created":now,"expires":now+datetime.timedelta(seconds=TTL_SECONDS)}
        _prune_sessions()
    response.set_cookie(f"{kind}_session",token,max_age=TTL_SECONDS,path="/",samesite="lax",httponly=True); return token
def _has(request,kind):
    with _session_lock:
        _prune_sessions()
        row=_SESSIONS.get(request.cookies.get(f"{kind}_session", "")); return bool(row and row["kind"]==kind and row["expires"]>_now())


@router.get("/dev-settings/system-limits")
def system_limit_registry(request: Request):
    """Developer-only view of the values resolved when this worker started."""
    if not _has(request, "developer"):
        raise HTTPException(401, "未登入開發者設定")
    from system_limits import effective_limits
    return {"limits": effective_limits()}
def _revoke(request, kind):
    with _session_lock:
        _SESSIONS.pop(request.cookies.get(f"{kind}_session", ""), None)
def _require(request,kind):
    if not _has(request,kind): raise HTTPException(401,"未登入或驗證已過期")
def _rows(df): return [dict(x) for x in df.to_dict(orient="records")]
def _list_value(value):
    value=value or []
    if isinstance(value,str):
        try: value=json.loads(value or "[]")
        except (TypeError,ValueError,json.JSONDecodeError): value=[]
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip())) if isinstance(value,list) else []


def _json_list_config(db,key):
    return _list_value(_config(db,key))


def _display_config(value):
    """Keep the existing HTML response contract while storage becomes typed."""
    if value is None:
        return ""
    if isinstance(value,(list,dict)):
        return json.dumps(value,ensure_ascii=False,separators=(",",":"))
    if isinstance(value,bool):
        return "true" if value else "false"
    return str(value)


@router.post("/db-management/login")
def db_login(body:PasswordBody,response:Response):
    db=_db(); stored=_config(db,"admin_password")
    if not _verify_config_password_and_upgrade(db,"admin_password",body.password,stored): raise HTTPException(401,"密碼錯誤")
    _issue(response,"db_admin"); return {"ok":True}

@router.post("/db-management/verify")
def sql_verify(body:PasswordBody,request:Request,response:Response):
    _require(request,"db_admin"); db=_db(); stored=_config(db,"sql_password")
    if not _verify_config_password_and_upgrade(db,"sql_password",body.password,stored): raise HTTPException(401,"SQL 存取密碼錯誤")
    _issue(response,"sql"); return {"ok":True}

@router.get("/db-management/data")
def db_data(request:Request):
    _require(request,"sql"); db=_db()
    tables=_rows(db.query("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"))
    logs=_rows(db.query(
        "SELECT id,user_id,login_type,logged_in_at FROM login_records "
        "ORDER BY logged_in_at DESC LIMIT :limit",
        {"limit": ADMIN_RECENT_LOGIN_LIMIT},
    ))
    return {"tables":[x["table_name"] for x in tables],"logs":logs}

@router.get("/db-management/logs")
def db_logs(request:Request,page:int=1):
    _require(request,"sql"); db=_db(); page,_,offset=bounds(page)
    total=scalar_count(db,"SELECT COUNT(*) total FROM login_records")
    rows=_rows(db.query(
        "SELECT id,user_id,login_type,logged_in_at FROM login_records "
        "ORDER BY logged_in_at DESC LIMIT :limit OFFSET :offset",
        {"limit":PAGE_SIZE,"offset":offset},
    ))
    return payload(rows,page,total)

def _unsafe(sql):
    compact=sql.strip().rstrip(";"); upper=compact.upper()
    if not compact: return "請輸入 SQL"
    if ";" in compact: return "每次只可執行一條 SQL"
    if not re.match(r"^(SELECT|WITH|INSERT|UPDATE|DELETE)\b", upper):
        return "此頁只可執行 SELECT、WITH、INSERT、UPDATE 或 DELETE"
    if re.search(r"\b(SYSTEM_CONFIG|APP_CONFIG|SCHEMA_MIGRATIONS)\b",upper): return "此頁不可存取應用程式內部資料表"
    if re.search(r"\b(DROP|TRUNCATE|ALTER|CREATE)\b",upper): return "此頁不可執行 DDL 語句"
    return ""


def _sql_cell(value):
    """Make SQL-console values JSON-safe without copying legacy BYTEA payloads."""
    if value is None:
        return None
    if isinstance(value, memoryview):
        return f"<binary {value.nbytes} bytes omitted>"
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)} bytes omitted>"
    rendered = str(value)
    if len(rendered) > SQL_RESULT_MAX_CELL_CHARS:
        omitted = len(rendered) - SQL_RESULT_MAX_CELL_CHARS
        return rendered[:SQL_RESULT_MAX_CELL_CHARS] + f"… <{omitted} chars omitted>"
    return rendered

@router.post("/db-management/execute")
def sql_execute(body:SqlBody,request:Request):
    _require(request,"sql"); sql=body.sql.strip().rstrip(";"); error=_unsafe(sql)
    if error: raise HTTPException(400,error)
    upper=sql.upper(); dangerous=bool(re.search(r"\b(UPDATE\s+.+?\s+SET|DELETE\s+FROM)\b",upper,re.S)) and not bool(re.search(r"\bWHERE\b",upper))
    if dangerous and not body.confirmed: return {"requires_confirmation":True,"sql":sql}
    db=_db(); engine=db._engine
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('statement_timeout', :timeout, TRUE)"),
                         {"timeout": f"{SQL_STATEMENT_TIMEOUT_MS}ms"})
            is_select=upper.startswith("SELECT") or upper.startswith("WITH")
            executor = conn.execution_options(
                stream_results=True, max_row_buffer=min(SQL_RESULT_MAX_ROWS + 1, 100),
            ) if is_select else conn
            result=executor.execute(text(sql))
            if is_select:
                cols=list(result.keys()); fetched=result.fetchmany(SQL_RESULT_MAX_ROWS + 1)
                rows=[]; used_bytes=0; truncated=len(fetched) > SQL_RESULT_MAX_ROWS
                reason="row_limit" if truncated else ""
                for raw in fetched[:SQL_RESULT_MAX_ROWS]:
                    item={key:_sql_cell(value) for key,value in zip(cols,raw)}
                    item_bytes=len(json.dumps(item,ensure_ascii=False,default=str).encode("utf-8"))
                    if used_bytes + item_bytes > SQL_RESULT_MAX_BYTES:
                        truncated=True; reason="byte_limit"; break
                    rows.append(item); used_bytes += item_bytes
                return {"kind":"select","columns":cols,"rows":rows,"truncated":truncated,
                        "truncation_reason":reason,"row_limit":SQL_RESULT_MAX_ROWS,
                        "byte_limit":SQL_RESULT_MAX_BYTES,"returned_bytes":used_bytes}
            return {"kind":"dml","count":result.rowcount}
    except Exception as exc: raise HTTPException(400,f"執行失敗：{exc}") from exc


@router.post("/developer/login")
def dev_login(body:PasswordBody,response:Response):
    db=_db(); stored=_config(db,"developer_password")
    if not stored: raise HTTPException(503,"尚未設定開發者密碼")
    if not _verify_config_password_and_upgrade(db,"developer_password",body.password,stored): raise HTTPException(401,"密碼錯誤")
    _issue(response,"developer"); return {"ok":True}

@router.post("/developer/logout")
def dev_logout(request:Request,response:Response):
    _revoke(request,"developer"); response.delete_cookie("developer_session",path="/"); return {"ok":True}

@router.get("/developer/data")
def dev_data(request:Request):
    _require(request,"developer"); db=_db()
    config_keys=("maintenance_mode","maintenance_deadline","bypass_active_check_until","tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","lateness_fund_managers","ai_enabled_providers","ai_default_model")
    loaded_configs=_configs(db,(*config_keys,"login_disabled_accounts"))
    configs={key:_display_config(loaded_configs.get(key)) for key in config_keys}
    enabled_providers, effective_default_model = resolve_interactive_model_settings(
        loaded_configs.get("ai_enabled_providers"),
        loaded_configs.get("ai_default_model"),
    )
    # Show the effective runtime values even before these optional settings
    # have ever been saved, so the controls never appear blank/write-only.
    configs["ai_enabled_providers"] = _display_config(list(enabled_providers))
    configs["ai_default_model"] = _display_config(effective_default_model)
    disabled_values=loaded_configs.get("login_disabled_accounts") or []
    if isinstance(disabled_values,str):
        try: disabled_values=json.loads(disabled_values)
        except (TypeError,ValueError,json.JSONDecodeError): disabled_values=[]
    login_disabled={str(value).strip() for value in disabled_values if str(value).strip()} if isinstance(disabled_values,list) else set()
    account_rows=db.query(
        f"SELECT user_id,account_status FROM {TABLE_ACCOUNTS} "
        "WHERE user_id<>'' AND LOWER(user_id) <> ALL(:excluded_account_keys) "
        "AND COALESCE(account_disabled,FALSE)=FALSE "
        "ORDER BY user_id LIMIT :account_limit",
        {
            "excluded_account_keys": list(NON_MEMBER_ACCOUNT_DB_KEYS),
            "account_limit": ACCOUNT_INVENTORY_LIMIT,
        },
    )
    account_options=[str(value).strip() for value in account_rows.get("user_id",[]) if str(value).strip() and str(value).strip() not in login_disabled]
    inactive_accounts=[str(row.get("user_id") or "").strip() for _,row in account_rows.iterrows() if str(row.get("account_status") or "").strip()=="inactive" and str(row.get("user_id") or "").strip() not in login_disabled]
    providers=sorted({str(config.get("provider") or "").strip() for config in AI_MODEL_OPTIONS.values() if config.get("provider")})
    from deploy.proxy import _get_proxy_secret
    provider_keys={provider:next((str(config.get("api_key") or "") for config in AI_MODEL_OPTIONS.values() if config.get("provider")==provider),"") for provider in providers}
    models=[{"label":label,"provider":config.get("provider","")} for label,config in AI_MODEL_OPTIONS.items()]
    subs=_rows(db.query(f"SELECT user_id,COUNT(*) AS device_count FROM {TABLE_PUSH_SUBSCRIPTIONS} WHERE is_active=TRUE GROUP BY user_id ORDER BY user_id LIMIT :account_limit", {"account_limit": ACCOUNT_INVENTORY_LIMIT}))
    return {"version":APP_VERSION,"account_options":account_options,"inactive_accounts":inactive_accounts,"ai_options":{"providers":providers,"models":models,"default_model":effective_default_model,"key_status":{provider:bool(key and _get_proxy_secret(key)) for provider,key in provider_keys.items()},"key_names":provider_keys},"configs":configs,"subscriptions":subs}

@router.get("/developer/collection/{kind}")
def dev_collection(kind:str,request:Request,page:int=1):
    _require(request,"developer"); db=_db(); page,_,offset=bounds(page)
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
    db=_db()
    report=db.query(f"SELECT reporter_user_id FROM {TABLE_BUG_REPORTS} WHERE id=:id",{"id":bug_id})
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
    if not account_id_can_be_created(uid): raise HTTPException(400,"此用戶名稱保留作系統帳戶使用")
    db=_db(); exists=db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:uid",{"uid":uid})
    if not exists.empty: raise HTTPException(400,"此用戶名稱已存在")
    count=scalar_count(db,f"SELECT COUNT(*) total FROM {TABLE_ACCOUNTS}")
    if count >= ACCOUNT_INVENTORY_LIMIT: raise HTTPException(409,"帳戶已達保護上限，請先停用及整理舊帳戶")
    db.execute(f"INSERT INTO {TABLE_ACCOUNTS}(user_id,password_hash,account_status) VALUES(:uid,:pw,'inactive')",{"uid":uid,"pw":hash_password(body.password)}); return {"ok":True}

@router.post("/developer/accounts/{uid}/password")
def reset_account(uid:str,body:AccountBody,request:Request):
    _require(request,"developer");
    if not uid.strip() or is_protected_account(uid): raise HTTPException(400,"不可在此重設系統帳戶")
    if not body.password: raise HTTPException(400,"請輸入新密碼")
    db=_db()
    if db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:uid",{"uid":uid}).empty: raise HTTPException(404,"帳戶不存在")
    db.execute(f"UPDATE {TABLE_ACCOUNTS} SET password_hash=:pw WHERE user_id=:uid",{"pw":hash_password(body.password),"uid":uid}); return {"ok":True}

@router.patch("/developer/accounts/{uid}/access")
def set_account_access(uid:str,body:AccountAccessBody,request:Request):
    _require(request,"developer")
    uid=uid.strip()
    if not uid or is_protected_account(uid): raise HTTPException(400,"不可停用系統帳戶")
    db=_db()
    if db.query(f"SELECT 1 FROM {TABLE_ACCOUNTS} WHERE user_id=:uid",{"uid":uid}).empty: raise HTTPException(404,"帳戶不存在")
    access_keys=("login_disabled_accounts","tts_recording_reviewers","lateness_fund_managers","tts_recording_allowed_users","ai_fund_treasurers","bypass_active_check_until")
    with db.transaction() as conn:
        # Serialize account access-list updates. Reading before this lock
        # allowed concurrent PATCHes to overwrite one another's removals.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('account_access_config'))"))
        access_config=get_configs_from_connection(conn,access_keys)
        disabled=set(_list_value(access_config.get("login_disabled_accounts")))
        updates={}
        if body.disabled:
            for key,label in (("tts_recording_reviewers","AI 訓練管理員"),("lateness_fund_managers","遲到基金管理員")):
                members=_list_value(access_config.get(key))
                remaining=[member for member in members if member!=uid and member not in disabled]
                if uid in members and not remaining: raise HTTPException(400,f"請先加入另一位{label}，再停用此帳戶")
                if uid in members: updates[key]=[member for member in members if member!=uid]
            for key in ("tts_recording_allowed_users","ai_fund_treasurers"):
                members=_list_value(access_config.get(key))
                if uid in members: updates[key]=[member for member in members if member!=uid]
            bypass=access_config.get("bypass_active_check_until") or {}
            if isinstance(bypass,str):
                try: bypass=json.loads(bypass)
                except (TypeError,ValueError,json.JSONDecodeError): bypass={}
            if isinstance(bypass,dict) and uid in bypass:
                bypass=dict(bypass); bypass.pop(uid,None); updates["bypass_active_check_until"]=bypass
            disabled.add(uid)
        else:
            disabled.discard(uid)
        updates["login_disabled_accounts"]=sorted(disabled)
        set_configs_on_connection(conn,updates)
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


@router.post("/developer/render-bandwidth/sync")
async def sync_render_bandwidth(request: Request):
    _require(request, "developer")
    from deploy.proxy import sync_render_bandwidth_metrics
    try:
        result = await sync_render_bandwidth_metrics()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    except Exception:
        raise HTTPException(502, "Render bandwidth 官方數據同步失敗。")
    return {"ok": True, **result}

@router.post("/developer/bypass")
def update_bypass(body:BypassBody,request:Request):
    _require(request,"developer"); db=_db()
    current=_config(db,"bypass_active_check_until") or {}
    if isinstance(current,str):
        try: current=json.loads(current)
        except (TypeError,ValueError,json.JSONDecodeError): current={}
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
        valid={str(x).strip() for x in db.query(f"SELECT user_id FROM {TABLE_ACCOUNTS} WHERE account_status='inactive' AND COALESCE(account_disabled,FALSE)=FALSE LIMIT :account_limit", {"account_limit": ACCOUNT_INVENTORY_LIMIT}).get("user_id",[])}
        valid-=set(_json_list_config(db,"login_disabled_accounts"))
        invalid=sorted(set(users)-valid)
        if invalid: raise HTTPException(400,"只可為非活躍帳戶開放："+"、".join(invalid))
        current.update({uid:body.expires_at.strip() for uid in users})
    _set(db,"bypass_active_check_until",current)
    return {"ok":True,"bypasses":current}


@router.post("/developer/settings")
def developer_settings(body:JsonSettings,request:Request):
    _require(request,"developer"); db=_db(); allowed={"maintenance_mode","maintenance_deadline","tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","lateness_fund_managers","ai_enabled_providers","ai_default_model"}
    account_list_keys={"tts_recording_allowed_users","tts_recording_reviewers","ai_fund_treasurers","lateness_fund_managers"}
    cleaned={}
    for key,value in body.values.items():
        if key not in allowed: raise HTTPException(400,f"不允許的設定：{key}")
        if key in account_list_keys:
            if not isinstance(value,list): raise HTTPException(400,f"{key} 必須是帳戶清單")
            value=list(dict.fromkeys(str(item).strip()[:200] for item in value if str(item).strip()))
            if len(value) > ACCOUNT_INVENTORY_LIMIT: raise HTTPException(413,f"{key} 清單超過保護上限")
        cleaned[key]=value
    if "lateness_fund_managers" in cleaned and not cleaned["lateness_fund_managers"]:
        raise HTTPException(400,"至少保留一位遲到基金管理員")
    if "tts_recording_reviewers" in cleaned and not cleaned["tts_recording_reviewers"]:
        raise HTTPException(400,"至少保留一位 AI 訓練管理員")
    if "maintenance_mode" in cleaned:
        value=str(cleaned["maintenance_mode"]).strip().lower()
        if value not in ("true","false"): raise HTTPException(400,"維護模式值無效")
        cleaned["maintenance_mode"]=value
    deadline_was_supplied="maintenance_deadline" in cleaned
    if deadline_was_supplied or cleaned.get("maintenance_mode")=="true":
        value=str(cleaned.get("maintenance_deadline") or _config(db,"maintenance_deadline") or "").strip()
        if not value:
            raise HTTPException(400,"開啟維護模式前請設定預期完成時間")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",value):
            raise HTTPException(400,"請輸入有效的預期完成時間")
        try:
            deadline=datetime.datetime.fromisoformat(value)
        except (TypeError,ValueError):
            raise HTTPException(400,"請輸入有效的預期完成時間")
        if deadline.tzinfo is not None:
            raise HTTPException(400,"預期完成時間須使用香港本地時間")
        now_hk=datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
        if deadline <= now_hk:
            raise HTTPException(400,"預期完成時間必須在未來")
        if deadline_was_supplied:
            cleaned["maintenance_deadline"]=deadline.strftime("%Y-%m-%dT%H:%M")
    if "ai_enabled_providers" in cleaned or "ai_default_model" in cleaned:
        valid_providers={str(item.get("provider") or "") for item in AI_MODEL_OPTIONS.values()}
        if "ai_enabled_providers" in cleaned:
            providers=cleaned["ai_enabled_providers"]
            if not isinstance(providers,list): raise HTTPException(400,"Provider 設定無效")
            providers=list(dict.fromkeys(str(item).strip() for item in providers if str(item).strip()))
            if not providers or set(providers)-valid_providers: raise HTTPException(400,"請至少啟用一個有效 Provider")
        else:
            providers=list(resolve_interactive_model_settings(
                _config(db,"ai_enabled_providers"),
                _config(db,"ai_default_model"),
            )[0])
        model=str(cleaned.get("ai_default_model") or _config(db,"ai_default_model") or DEFAULT_AI_MODEL).strip()
        if model not in AI_MODEL_OPTIONS or AI_MODEL_OPTIONS[model].get("provider") not in providers: raise HTTPException(400,"預設模型必須屬於已啟用的 Provider")
        cleaned["ai_enabled_providers"]=providers; cleaned["ai_default_model"]=model
    requested={user for key,value in cleaned.items() if key in account_list_keys for user in value}
    if requested:
        rows=db.query(
            f"SELECT user_id FROM {TABLE_ACCOUNTS} "
            "WHERE user_id<>'' AND LOWER(user_id) <> ALL(:excluded_account_keys) "
            "AND COALESCE(account_disabled,FALSE)=FALSE LIMIT :account_limit",
            {
                "excluded_account_keys": list(NON_MEMBER_ACCOUNT_DB_KEYS),
                "account_limit": ACCOUNT_INVENTORY_LIMIT,
            },
        )
        valid={str(value).strip() for value in rows.get("user_id",[]) if str(value).strip()}
        valid-=set(_json_list_config(db,"login_disabled_accounts"))
        invalid=sorted(requested-valid)
        if invalid: raise HTTPException(400,"帳戶不存在或已停用："+"、".join(invalid))
    with db.transaction() as conn:
        set_configs_on_connection(conn,cleaned)
    return {"ok":True}
