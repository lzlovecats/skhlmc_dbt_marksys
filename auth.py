"""Centralised access-control / authentication module.

Holds all login, cookie and password-gate logic that used to live scattered in
functions.py, plus one-line page guards (`require_committee`, `require_admin`).

Low-level primitives that are also used by non-auth code (`get_connection`,
`get_system_config`, `hash_password`, `_verify_config_password`,
`refresh_acc_type`, account-lifecycle helpers) stay in functions.py and are
imported here — the dependency is one-directional (auth → functions), so there
is no circular import.
"""

import base64
import hashlib
import hmac
import json
import secrets
import datetime
import time
from http.cookies import SimpleCookie
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components
import extra_streamlit_components as stx

from schema import TABLE_ACCOUNTS, TABLE_LOGIN_RECORDS
from functions import (
    get_connection,
    execute_query,
    get_system_config,
    _verify_config_password,
    refresh_acc_type,
    return_expire_day,
    ensure_account_lifecycle_columns,
    update_committee_login_time,
    disable_dormant_committee_accounts,
)

_verify_password = _verify_config_password


# ─────────────────────────────────────────────────────────────
# Cookie primitives
# ─────────────────────────────────────────────────────────────
def get_cookie(cookie_manager, key, default=None):
    try:
        value = cookie_manager.get(key)
        return default if value is None else value
    except Exception:
        return default


def set_cookie(cookie_manager, key, value, expires_at=None):
    try:
        if expires_at is None:
            cookie_manager.set(key, value)
        else:
            cookie_manager.set(key, value, expires_at=expires_at)
        return True
    except Exception:
        return False


def del_cookie(cookie_manager, key):
    try:
        cookie_manager.delete(key)
        return True
    except Exception:
        return False


def committee_cookie_manager():
    if "committee_cookie_manager" not in st.session_state:
        st.session_state["committee_cookie_manager"] = stx.CookieManager(key="committee_cookies")
    return st.session_state["committee_cookie_manager"]


def render_committee_auth_bridge(token=None, clear=False):
    token_json = json.dumps(token or "")
    clear_json = json.dumps(bool(clear))
    components_html = f"""
    <script>
    (function () {{
        const win = window.parent;
        const token = {token_json};
        const shouldClear = {clear_json};
        const cookieName = "committee_user";
        if (shouldClear) {{
            win.localStorage.removeItem(cookieName);
            win.document.cookie = cookieName + "=; Max-Age=0; Path=/; SameSite=Lax";
            return;
        }}
        if (token) {{
            win.localStorage.setItem(cookieName, token);
            win.document.cookie = cookieName + "=" + encodeURIComponent(token) + "; Max-Age=15552000; Path=/; SameSite=Lax";
        }}
    }})();
    </script>
    """
    components.html(components_html, height=0, width=0)


def render_committee_auth_restore_bridge():
    components_html = """
    <script>
    (function () {
        const win = window.parent;
        const cookieName = "committee_user";
        const token = win.localStorage.getItem(cookieName);
        if (!token) return;
        if (win.document.cookie.indexOf(cookieName + "=") !== -1) return;
        win.document.cookie = cookieName + "=" + encodeURIComponent(token) + "; Max-Age=15552000; Path=/; SameSite=Lax";
        const reloadKey = "committee_cookie_restored_at";
        const now = Date.now();
        const lastReload = Number(win.sessionStorage.getItem(reloadKey) || "0");
        if (now - lastReload > 3000) {
            win.sessionStorage.setItem(reloadKey, String(now));
            // reload 之前蓋一個全屏「恢復緊登入」遮罩，避免用戶見到登入表單閃過。
            // reload 後係全新 document，遮罩自然消失。
            try {
                const doc = win.document;
                if (!doc.getElementById("skh-auth-restoring-overlay")) {
                    const overlay = doc.createElement("div");
                    overlay.id = "skh-auth-restoring-overlay";
                    overlay.style.cssText = "position:fixed;inset:0;z-index:2147483647;display:flex;"
                        + "align-items:center;justify-content:center;background:#0e1117;color:#fafafa;"
                        + "font-size:1rem;font-family:'Source Sans Pro',sans-serif;";
                    overlay.textContent = "🔄 正在恢復登入狀態，請稍候…";
                    doc.body.appendChild(overlay);
                }
            } catch (e) {}
            setTimeout(function () { win.location.reload(); }, 80);
        }
    })();
    </script>
    """
    components.html(components_html, height=0, width=0)


def _get_cookie_secret() -> str:
    secret = get_system_config("cookie_secret")
    if secret:
        return secret
    new_secret = secrets.token_hex(32)
    execute_query(
        "INSERT INTO system_config (key, value, updated_at) "
        "VALUES ('cookie_secret', :value, :updated_at) "
        "ON CONFLICT (key) DO NOTHING",
        {"value": new_secret, "updated_at": datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")}
    )
    stored = get_system_config("cookie_secret")
    return stored if stored else new_secret


def _sign_cookie(user_id: str) -> str:
    secret = _get_cookie_secret()
    sig = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}:{sig}"


def _verify_cookie(cookie_value: str) -> str | None:
    if not cookie_value or ":" not in cookie_value:
        return None
    user_id, sig = cookie_value.rsplit(":", 1)
    secret = _get_cookie_secret()
    expected = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return user_id
    return None


def sign_relay_token(
    token: str, user_id: str, practice_kind: str, max_seconds: int,
    practice_id: str,
) -> str:
    """簽發帶身份、練習類型及server deadline嘅Gemini relay claim。"""
    if practice_kind not in ("solo_free", "solo_mock"):
        raise ValueError("invalid relay practice kind")
    secret = _get_cookie_secret()
    payload = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "user_id": str(user_id),
        "practice_kind": practice_kind,
        "practice_id": str(practice_id),
        "max_seconds": max(30, min(int(max_seconds), 30 * 60)),
        "exp": int(time.time()) + 2 * 60 * 60,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        secret.encode(), encoded.encode("ascii"), hashlib.sha256,
    ).digest()
    signed = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{encoded}.{signed}"


def committee_bearer_token(user_id: str) -> str:
    """公開介面：產生一個 ``user_id:sig`` token，畀 Streamlit 進程用 Bearer header
    調用同一容器內 proxy 嘅內部 API（例如聯機房間 /api/room/*）。proxy 用
    _verify_committee_token 以共用 cookie_secret 驗證，等同瀏覽器嘅 committee_user
    cookie。"""
    return _sign_cookie(user_id)


def get_committee_cookie_from_context():
    try:
        cookies = getattr(st.context, "cookies", None)
        if cookies and cookies.get("committee_user"):
            return cookies.get("committee_user")
    except Exception:
        pass
    try:
        headers = getattr(st.context, "headers", None)
        raw_cookie = headers.get("cookie") if headers else ""
        parsed = SimpleCookie()
        parsed.load(raw_cookie or "")
        if "committee_user" in parsed:
            return parsed["committee_user"].value
    except Exception:
        pass
    return None


def _log_login(user_id: str, login_type: str):
    login_time = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        f"INSERT INTO {TABLE_LOGIN_RECORDS} (user_id, login_type, logged_in_at) "
        "VALUES (:user_id, :login_type, :login_time)",
        {"user_id": user_id, "login_type": login_type, "login_time": login_time}
    )


# ─────────────────────────────────────────────────────────────
# Password gate UI
# ─────────────────────────────────────────────────────────────
def render_password_gate(title, description, field_label, button_label, form_key):
    with st.container(border=True):
        st.subheader(title)
        st.caption(description)
        with st.form(form_key):
            pwd = st.text_input(field_label, type="password")
            submitted = st.form_submit_button(button_label, width="stretch")
        if submitted:
            return pwd
    return None


# ─────────────────────────────────────────────────────────────
# Staff (賽會人員) login
# ─────────────────────────────────────────────────────────────
def check_admin():
    if "admin_logged_in" not in st.session_state:
        st.session_state["admin_logged_in"] = False

    if not st.session_state["admin_logged_in"]:
        pwd = render_password_gate(
            "賽會人員登入",
            "請輸入賽會人員密碼以進入管理頁面。",
            "請輸入賽會人員密碼",
            "登入",
            form_key="admin_login_gate",
        )
        if pwd is not None:
            stored = get_system_config("admin_password")
            if stored is None:
                st.error("系統錯誤：未能讀取密碼，請聯絡開發人員")
            elif _verify_config_password(pwd, stored):
                st.session_state["admin_logged_in"] = True
                _log_login("admin", "admin")
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True


# ─────────────────────────────────────────────────────────────
# Committee member (內部委員會成員) login
# ─────────────────────────────────────────────────────────────
def check_committee_login():
    ensure_account_lifecycle_columns()
    # Sweep dormant (180-day inactive) accounts once per session rather than
    # hiding a write inside the cached participation-stats query.
    if not st.session_state.get("_dormant_sweep_done"):
        st.session_state["_dormant_sweep_done"] = True
        disable_dormant_committee_accounts()
    cookie_manager = committee_cookie_manager()

    if "committee_user" not in st.session_state:
        st.session_state["committee_user"] = None

    # Check cookies for auto-login. CookieManager returns default {} on first run until the
    # browser component runs; give it one rerun so the component can return real cookies.
    if st.session_state["committee_user"] is None:
        context_cookie = get_committee_cookie_from_context()
        verified_context_user = _verify_cookie(context_cookie) if context_cookie else None
        if verified_context_user:
            st.session_state["committee_user"] = verified_context_user
            update_committee_login_time(verified_context_user)
            st.rerun()
        if not st.session_state.get("_committee_cookie_rerun_done"):
            st.session_state["_committee_cookie_rerun_done"] = True
            st.rerun()
        cookie_manager.get_all(key="committee_cookies_get")
        raw_cookie = get_cookie(cookie_manager, "committee_user")
        verified_user = _verify_cookie(raw_cookie) if raw_cookie else None
        if verified_user:
            st.session_state["committee_user"] = verified_user
            update_committee_login_time(verified_user)
            st.rerun()
        render_committee_auth_restore_bridge()

    if st.session_state["committee_user"]:
        render_committee_auth_bridge(_sign_cookie(st.session_state["committee_user"]))
        return True

    st.subheader("內部委員會成員登入")

    # PWA / 慢速裝置有時 CookieManager 元件未及載入，自動登入會失敗。
    # 提供一個手動按鈕，重新讀取已儲存的 cookie 再試一次。
    if st.button("🔄 用已儲存的登入資料登入", key="committee_cookie_login"):
        cookie_manager.get_all(key="committee_cookies_manual_get")
        raw_cookie = get_cookie(cookie_manager, "committee_user")
        verified_user = _verify_cookie(raw_cookie) if raw_cookie else None
        if verified_user:
            st.session_state["committee_user"] = verified_user
            update_committee_login_time(verified_user)
            st.rerun()
        else:
            st.warning("找不到有效的登入紀錄，請於下方重新輸入帳號密碼。")

    with st.form("committee_login"):
        uid = st.text_input("用戶名稱")
        upw = st.text_input("密碼", type="password")
        submitted = st.form_submit_button("登入")

        if submitted:
            conn = get_connection()
            acc_row = conn.query(
                f"SELECT password_hash FROM {TABLE_ACCOUNTS} WHERE user_id = :uid",
                params={"uid": uid.strip()},
                ttl=0,
            )
            login_success = (
                not acc_row.empty
                and _verify_password(upw.strip(), str(acc_row.iloc[0]["password_hash"]))
            )

            if login_success:
                refresh_acc_type(uid.strip())
                _log_login(uid.strip(), "committee")
                update_committee_login_time(uid.strip())
                st.session_state["committee_user"] = uid.strip()
                auth_token = _sign_cookie(uid.strip())
                set_cookie(cookie_manager, "committee_user", auth_token, expires_at=return_expire_day())
                render_committee_auth_bridge(auth_token)
                st.success(f"歡迎，{uid.strip()}。")
                return True
            else:
                st.error("用戶名稱或密碼錯誤。")


# ─────────────────────────────────────────────────────────────
# One-line page guards
# ─────────────────────────────────────────────────────────────
def require_admin() -> None:
    """Guard a 賽會人員 page. Halts the page if not logged in."""
    if not check_admin():
        st.stop()


def require_committee() -> str:
    """Guard a 內部委員會成員 page.

    Halts the page until a committee member is logged in, rejects 賽會人員
    accounts (with a logout button), and returns the committee user_id.
    """
    if not check_committee_login():
        st.stop()

    user_id = st.session_state["committee_user"]
    if user_id == "admin":
        st.error("賽會人員帳戶不能使用此頁面。請改用內部委員會成員帳戶登入。")
        if st.button("登出", width="stretch"):
            st.session_state["committee_user"] = None
            del_cookie(committee_cookie_manager(), "committee_user")
            render_committee_auth_bridge(clear=True)
            st.rerun()
        st.stop()

    return user_id
