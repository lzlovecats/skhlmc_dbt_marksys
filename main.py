import json
import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import urlparse
from functions import is_maintenance_mode, render_maintenance_notice, show_manual, show_rules
from version import APP_VERSION

# Set up basic structure of the webpage
st.set_page_config(page_title="聖呂中辯電子賽務系統", layout="wide", page_icon="📑")


def render_pwa_install_listener():
    components.html(
        """
        <style>
        input, textarea, select {
            font-size: 16px !important;
        }
        </style>
        <script>
        (function () {
            const win = window.parent;
            const doc = win.document;

            function setSingleMeta(name, content) {
                const selector = 'meta[name="' + name + '"]';
                const metas = Array.prototype.slice.call(doc.querySelectorAll(selector));
                const meta = metas.length ? metas[0] : doc.createElement("meta");
                meta.setAttribute("name", name);
                meta.setAttribute("content", content);
                if (!metas.length) doc.head.appendChild(meta);
                for (let i = 1; i < metas.length; i++) metas[i].remove();
                return meta;
            }
            // Lock zoom at 1 and DO NOT extend content under the status bar.
            // With `viewport-fit=cover` + a translucent status bar the page
            // draws beneath the notch, which shifts the hit-test area of
            // BaseWeb's fixed dropdown popovers below where the option text is
            // painted — the "要撳低啲先撳到" symptom. Dropping cover keeps the
            // paint and touch coordinates aligned. Inputs still don't auto-zoom
            // because their font-size is >= 16px.
            setSingleMeta("viewport", "width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no");
            setSingleMeta("theme-color", "#000000");
            setSingleMeta("color-scheme", "dark");
            setSingleMeta("apple-mobile-web-app-capable", "yes");
            setSingleMeta("mobile-web-app-capable", "yes");
            setSingleMeta("apple-mobile-web-app-status-bar-style", "black");
            doc.documentElement.style.backgroundColor = "#000000";
            if (doc.body) doc.body.style.backgroundColor = "#000000";

            if (!doc.getElementById("skh-mobile-input-zoom-fix")) {
                const style = doc.createElement("style");
                style.id = "skh-mobile-input-zoom-fix";
                style.textContent = `
                    html, body, #root, .stApp { background: #000000 !important; }
                    input, textarea, select { font-size: 16px !important; }
                `;
                doc.head.appendChild(style);
            }

            // iOS: stop BaseWeb selectbox from summoning the keyboard/AutoFill.
            // Each st.selectbox contains a search input. On iPhone, focusing it
            // can show the AutoFill accessory and resize the visual viewport; the
            // portal-positioned menu then paints above its real tap targets.
            if (!win.__skhSelectKeyboardFixReady) {
                win.__skhSelectKeyboardFixReady = true;
                const hardenSelects = function () {
                    const inputs = win.document.querySelectorAll('[data-baseweb="select"] input');
                    for (let i = 0; i < inputs.length; i++) {
                        const el = inputs[i];
                        el.setAttribute("inputmode", "none");
                        el.setAttribute("autocomplete", "off");
                        el.setAttribute("autocorrect", "off");
                        el.setAttribute("autocapitalize", "none");
                        el.setAttribute("spellcheck", "false");
                        el.readOnly = true;
                    }
                };
                hardenSelects();
                let scheduled = false;
                const observer = new win.MutationObserver(function () {
                    if (scheduled) return;
                    scheduled = true;
                    win.requestAnimationFrame(function () {
                        scheduled = false;
                        hardenSelects();
                    });
                });
                observer.observe(win.document.body, { childList: true, subtree: true });
                win.addEventListener("pageshow", hardenSelects);
                win.addEventListener("focusin", function (event) {
                    const target = event.target;
                    if (target && target.matches && target.matches('[data-baseweb="select"] input')) {
                        hardenSelects();
                    }
                }, true);
            }

            if (!win.__skhPwaInstallListenerReady) {
                win.__skhPwaInstallListenerReady = true;
                win.__skhPwaDeferredPrompt = null;
                win.__skhPwaInstalled = (
                    win.matchMedia("(display-mode: standalone)").matches ||
                    win.navigator.standalone === true
                );

                win.addEventListener("beforeinstallprompt", function (event) {
                    event.preventDefault();
                    win.__skhPwaDeferredPrompt = event;
                    win.dispatchEvent(new Event("skh-pwa-install-ready"));
                });

                win.addEventListener("appinstalled", function () {
                    win.__skhPwaInstalled = true;
                    win.__skhPwaDeferredPrompt = null;
                    win.dispatchEvent(new Event("skh-pwa-installed"));
                });

                win.__skhPromptPwaInstall = async function () {
                    if (!win.__skhPwaDeferredPrompt) {
                        return { available: false };
                    }
                    const promptEvent = win.__skhPwaDeferredPrompt;
                    win.__skhPwaDeferredPrompt = null;
                    promptEvent.prompt();
                    const choice = await promptEvent.userChoice;
                    return { available: true, outcome: choice && choice.outcome };
                };
            }
        })();
        </script>
        """,
        height=0,
        width=0,
    )


def render_draft_autosave():
    """全域「草稿自動保存」層。

    手機 / PWA 切去其他 App 後被系統凍結，返嚟一撳掣，Streamlit 條 WebSocket
    重連會開一個全新 session，`st.session_state` 同所有 widget 內容都會清空 ——
    用戶未撳掣、仲打緊嗰段字（例如留言）只存在於瀏覽器 DOM，server / DB 都救唔到。

    呢個組件喺最頂層 document 上做 event delegation：用戶打字即時寫入 localStorage，
    reconnect 重繪 widget 時再自動填返落空欄位。純 client-side，唔依賴 Streamlit session，
    所以捱得過 session reset。密碼欄一律唔保存。成功送出後由 `clear_field_draft` 清走草稿。
    """
    components.html(
        """
        <script>
        (function () {
            const win = window.parent;
            if (win.__skhDraftAutosaveReady) return;
            win.__skhDraftAutosaveReady = true;

            const doc = win.document;
            const PREFIX = "skh_draft::";
            const MAX_AGE_MS = 12 * 60 * 60 * 1000;   // 草稿 12 小時後過期
            const DEBOUNCE_MS = 400;
            const SUPPRESS_MS = 8000;                 // 剛清除嘅 key 短暫唔還原，贏 restore race

            // 唔保存草稿嘅頁面（敏感／共用裝置風險）：
            //  registration＝公開報名（含電話等 PII）、db_mgmt＝SQL 主控台、dev_settings＝開發者設定。
            const EXCLUDED_PAGES = ["registration", "db_mgmt", "dev_settings"];
            function isExcludedPage() {
                const parts = win.location.pathname.split("/").filter(Boolean);
                const last = parts.length ? parts[parts.length - 1] : "";
                return EXCLUDED_PAGES.indexOf(last) !== -1;
            }

            // 標籤含「密碼 / password」嘅欄位即使 type=text 都唔保存（例如評判入場密碼）。
            function isSensitiveLabel(el) {
                const label = (el.getAttribute("aria-label") || "") + " " + (el.getAttribute("placeholder") || "");
                return /密碼|password/i.test(label);
            }

            function isCandidate(el) {
                if (!el) return false;
                const tag = el.tagName;
                let ok = false;
                if (tag === "TEXTAREA") {
                    ok = true;
                } else if (tag === "INPUT") {
                    const type = (el.getAttribute("type") || "text").toLowerCase();
                    ok = type === "text";           // 排除 password / number / search 等
                }
                if (!ok) return false;
                if (isSensitiveLabel(el)) return false;
                return true;
            }

            // 為每個輸入框計一個穩定 id：優先用 st-key-<key> class（keyed widget），
            // 否則用 aria-label（欄位標籤）+ 同標籤中嘅排序，做 best-effort。
            function fieldId(el) {
                let node = el;
                for (let i = 0; i < 6 && node; i++) {
                    if (node.classList) {
                        for (const cls of node.classList) {
                            if (cls.indexOf("st-key-") === 0) {
                                return "k:" + cls.slice("st-key-".length);
                            }
                        }
                    }
                    node = node.parentElement;
                }
                const label = el.getAttribute("aria-label") || el.getAttribute("placeholder") || "";
                if (!label) return null;
                const sameLabel = doc.querySelectorAll(
                    '[aria-label="' + label.replace(/"/g, '\\\\"') + '"]'
                );
                let idx = 0;
                for (const other of sameLabel) { if (other === el) break; idx++; }
                return "a:" + label + "#" + idx;
            }

            function storageKey(id) {
                return PREFIX + win.location.pathname + "::" + id;
            }

            const timers = new WeakMap();
            function scheduleSave(el) {
                if (timers.get(el)) clearTimeout(timers.get(el));
                timers.set(el, setTimeout(function () {
                    const id = fieldId(el);
                    if (!id) return;
                    const key = storageKey(id);
                    const val = el.value;
                    try {
                        if (val && val.length) {
                            win.localStorage.setItem(key, JSON.stringify({ v: val, t: Date.now() }));
                        } else {
                            win.localStorage.removeItem(key);
                        }
                    } catch (e) {}
                }, DEBOUNCE_MS));
            }

            doc.addEventListener("input", function (ev) {
                if (isExcludedPage()) return;
                const el = ev.target;
                if (isCandidate(el) && !el.dataset.skhRestoring) scheduleSave(el);
            }, true);

            // 用 native setter 寫值再派 input event，令 React / Streamlit 認得。
            function setValue(el, val) {
                const proto = el.tagName === "TEXTAREA"
                    ? win.HTMLTextAreaElement.prototype
                    : win.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
                setter.call(el, val);
                el.dispatchEvent(new Event("input", { bubbles: true }));
            }

            function restoreAll() {
                if (isExcludedPage()) return;
                const suppress = win.__skhSuppressRestore || {};
                const nodes = doc.querySelectorAll('textarea, input[type="text"]');
                nodes.forEach(function (el) {
                    if (!isCandidate(el)) return;
                    if (el.value && el.value.length) return;          // 只填空欄位，唔覆蓋用戶
                    const id = fieldId(el);
                    if (!id) return;
                    if (suppress[id] && Date.now() - suppress[id] < SUPPRESS_MS) return;  // 剛清除，唔還原
                    if (el.dataset.skhRestored === id) return;         // 每個欄位每個 id 只還原一次
                    let raw;
                    try { raw = win.localStorage.getItem(storageKey(id)); } catch (e) { return; }
                    if (!raw) return;
                    let data;
                    try { data = JSON.parse(raw); } catch (e) { return; }
                    if (!data || !data.v) return;
                    if (Date.now() - (data.t || 0) > MAX_AGE_MS) {
                        try { win.localStorage.removeItem(storageKey(id)); } catch (e) {}
                        return;
                    }
                    el.dataset.skhRestored = id;
                    el.dataset.skhRestoring = "1";
                    try { setValue(el, data.v); } catch (e) {}
                    delete el.dataset.skhRestoring;
                });
            }

            let restoreTimer = null;
            function scheduleRestore() {
                if (restoreTimer) clearTimeout(restoreTimer);
                restoreTimer = setTimeout(restoreAll, 150);
            }

            // Streamlit rerun / 換頁會重繪 widget，用 MutationObserver 捕捉。
            const observer = new win.MutationObserver(scheduleRestore);
            observer.observe(doc.body, { childList: true, subtree: true });

            win.addEventListener("pageshow", scheduleRestore);
            win.addEventListener("visibilitychange", function () {
                if (doc.visibilityState === "visible") scheduleRestore();
            });

            // 供 clear_field_draft 呼叫：清走指定 key（跨路徑）嘅草稿。
            win.__skhClearDrafts = function (keys) {
                win.__skhSuppressRestore = win.__skhSuppressRestore || {};
                (keys || []).forEach(function (k) {
                    const suffix = "::k:" + k;
                    const toRemove = [];
                    for (let i = 0; i < win.localStorage.length; i++) {
                        const storeKey = win.localStorage.key(i);
                        if (storeKey && storeKey.indexOf(PREFIX) === 0 && storeKey.endsWith(suffix)) {
                            toRemove.push(storeKey);
                        }
                    }
                    toRemove.forEach(function (sk) {
                        try { win.localStorage.removeItem(sk); } catch (e) {}
                    });
                    // 標記剛清除：即使 restore 剛好搶先跑，都會因 suppress 而跳過（見 restoreAll）。
                    win.__skhSuppressRestore["k:" + k] = Date.now();
                    // 清走已還原標記，等對應欄位可以重新接受新輸入。
                    doc.querySelectorAll('[data-skh-restored="k:' + k + '"]').forEach(function (el) {
                        delete el.dataset.skhRestored;
                    });
                });
            };

            scheduleRestore();
        })();
        </script>
        """,
        height=0,
        width=0,
    )


def flush_pending_draft_clears():
    """喺完整 run 頂部 render 草稿清除組件（見 functions.py `clear_field_draft`）。

    清除嘅呼叫位緊接 `st.rerun()`，嗰個 run render 嘅組件會被丟棄；所以改為喺下一個
    run（一定會完整跑完並 flush 到瀏覽器）頂部、頁面 widget 重繪之前先 render，令清除
    早過 restore 執行。組件本身仲會 set suppress flag，就算 restore 搶先都會跳過該 key。
    """
    keys = st.session_state.pop("_pending_draft_clears", None)
    if not keys:
        return
    keys_json = json.dumps(keys)
    components.html(
        f"""
        <script>
        (function () {{
            const win = window.parent;
            if (win.__skhClearDrafts) win.__skhClearDrafts({keys_json});
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


render_pwa_install_listener()
render_draft_autosave()
flush_pending_draft_clears()

if is_maintenance_mode():
    st.title("聖呂中辯電子賽務系統")
    render_maintenance_notice()
    st.stop()

# Every product page is now served directly by FastAPI HTML routes.  This
# Streamlit entry remains only for backwards-compatible infrastructure hooks.
pg = st.navigation({"系統": [st.Page("legacy_streamlit/html_migration_notice.py", title="HTML 版系統已接管")]})

if st.session_state.get("committee_user"):
    with st.sidebar:
        components.html(
            """
            <button id="skh-update-app-button" type="button" style="
                width:100%; min-height:2.75rem; border:1px solid rgba(148,163,184,.45);
                border-radius:8px; background:#111827; color:white; font-weight:700;
                cursor:pointer; margin:.25rem 0 .5rem;
            ">更新應用程式</button>
            <div id="skh-update-app-status" style="font-size:13px;color:#94a3b8;"></div>
            <script>
            (function () {
                const win = window.parent;
                const doc = document;
                const btn = doc.getElementById("skh-update-app-button");
                const status = doc.getElementById("skh-update-app-status");
                if (!btn) return;
                btn.addEventListener("click", async function () {
                    btn.disabled = true;
                    status.textContent = "正在更新...";
                    try {
                        if ("serviceWorker" in win.navigator) {
                            const regs = await win.navigator.serviceWorker.getRegistrations();
                            await Promise.all(regs.map(function (reg) { return reg.update(); }));
                        }
                        const url = new URL(win.location.href);
                        url.searchParams.set("_app_refresh", Date.now().toString());
                        win.location.replace(url.toString());
                    } catch (e) {
                        win.location.reload();
                    }
                });
            })();
            </script>
            """,
            height=74,
        )

# Show logout when admin logged in
if st.session_state.get("admin_logged_in"):
    with st.sidebar:
        st.write("")
        if st.button("登出賽會人員帳戶", width="stretch"):
            st.session_state["admin_logged_in"] = False
            st.rerun()

# Show manual
with st.sidebar:
    if st.button("📖 閱讀使用手冊", width="stretch"):
        show_manual()

with st.sidebar:
    if st.button("📋 查看賽規", width="stretch"):
        show_rules()

# Show caption
with st.sidebar:
    st.caption(f"🛠️ 系統版本：{APP_VERSION}")
    st.caption("🛜 開發及維護：[lzlovecats](https://github.com/lzlovecats) @ 2026")

pg.run()
