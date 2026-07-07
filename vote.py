import json
import math
import streamlit as st
import streamlit.components.v1 as components
from functions import show_noti_popup, hash_password, get_connection, execute_query, execute_query_count, get_active_user_count, get_member_participation_stats, CATEGORIES, DIFFICULTY_OPTIONS, DIFFICULTY_CRITERIA, render_page_guidance, _verify_config_password, query_params, is_bypass_active_check, get_bypass_active_until, get_vapid_public_key, notify_committee_vote_event, get_system_config
from auth import require_committee, del_cookie, committee_cookie_manager, render_committee_auth_bridge, _sign_cookie
from ai_coach_helpers import generate_general_ai_reply, get_ai_model_settings, is_successful_ai_result
from schema import (
    TABLE_ACCOUNTS,
    TABLE_MOTION_COMMENTS,
    TABLE_TOPIC_REMOVAL_VOTE_BALLOTS,
    TABLE_TOPIC_REMOVAL_VOTES,
    TABLE_TOPIC_VOTE_BALLOTS,
    TABLE_TOPIC_VOTES,
    TABLE_TOPICS,
)
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from prompts import (
    VOTE_BANK_ANALYSIS_SYSTEM_PROMPT,
    VOTE_DISCUSSION_SYSTEM_PROMPT,
    VOTE_TOPIC_REVIEW_SYSTEM_PROMPT,
    build_vote_bank_analysis_prompt,
    build_vote_discussion_prompt,
    build_vote_topic_review_prompt,
)

st.header("辯題徵集、投票及罷免")
render_page_guidance(
    [
        "請先使用內部委員會成員帳戶登入，再按需要切換至提出辯題、辯題投票、罷免投票或帳戶管理分頁。",
        "活躍成員可提出新辯題或罷免動議；所有成員均可參與投票。",
        "每項動議均設 7 日截止日期，達門檻且票數過半時會自動更新狀態。",
        "可在「帳戶管理」分頁啟用背景通知，新辯題及投票結果會推送到你的裝置（iPhone / iPad 需先加入主畫面）。",
    ],
)

TOPIC_REJECTION_REASONS = [
    "表述或界定不清",
    "正反責任失衡",
    "與現有題目重複或相似",
    "討論價值不足",
    "題目表述可再修訂",
    "類別分類不當",
    "難度分類不當",
]

DEPOSE_REASONS = [
    "題目已過時",
    "表述或界定不清",
    "正反責任失衡",
    "與現有題目重複或相似",
    "討論價值不足",
    "已有更佳版本可取代",
    "類別分類不當",
    "難度分類不當",
]

AI_COMMENT_USER_ID = "Gemini"
AI_DISCUSSION_MODEL = "Gemini 3.5 Flash"


def parse_reason_map(raw_value):
    if isinstance(raw_value, dict):
        return raw_value
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def parse_reason_list(raw_value):
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (TypeError, json.JSONDecodeError):
        pass
    return [str(raw_value).strip()] if str(raw_value).strip() else []


def dump_json(data):
    return json.dumps(data, ensure_ascii=False)


def collect_reasons(selected_reasons, other_reason):
    reasons = [reason.strip() for reason in selected_reasons if reason.strip()]
    other_reason = other_reason.strip()
    if other_reason:
        reasons.append(f"其他：{other_reason}")
    return reasons


def render_reason_lines(reason_map, empty_text):
    if not reason_map:
        st.caption(empty_text)
        return
    from collections import Counter
    all_reasons = []
    for reasons in reason_map.values():
        all_reasons.extend(parse_reason_list(reasons))
    if not all_reasons:
        st.caption(empty_text)
        return
    for reason, count in Counter(all_reasons).most_common():
        suffix = f"（{count} 人）" if count > 1 else ""
        st.caption(f"• {reason}{suffix}")


def parse_deadline_row(row, key="deadline_date"):
    # row: the row of the vote data
    """Returns (deadline_passed: bool, deadline_str: str)."""
    deadline_val = row.get(key, "")
    deadline_passed = False
    deadline_str = ""
    if deadline_val and deadline_val != "":
        try:
            if hasattr(deadline_val, 'date'):
                deadline_date = deadline_val.date() if hasattr(deadline_val, 'hour') else deadline_val
            else:
                deadline_date = datetime.strptime(str(deadline_val)[:10], "%Y-%m-%d").date()
            today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
            deadline_passed = today_hk > deadline_date
            deadline_str = deadline_date.strftime("%Y-%m-%d")
        except Exception:
            pass
    return deadline_passed, deadline_str


def clear_caches():
    get_vote_data.clear()
    get_pending_vote_count.clear()
    get_pending_depose_count.clear()
    from functions import get_active_user_count, get_member_participation_stats, _compute_all_user_stats
    get_active_user_count.clear()
    get_member_participation_stats.clear()
    _compute_all_user_stats.clear()


def queue_toast(message, icon=None):
    st.session_state["vote_action_toast"] = {"message": message, "icon": icon}


def show_queued_toast():
    toast = st.session_state.pop("vote_action_toast", None)
    if toast:
        st.toast(toast["message"], icon=toast.get("icon"))


def notify_vote_event(title, body, exclude_user=None, tag=None):
    try:
        notify_committee_vote_event(title, body, exclude_user=exclude_user, tag=tag, url="/vote")
    except Exception:
        pass


def render_push_notification_settings():
    vapid_public_key = get_vapid_public_key()
    if not vapid_public_key:
        st.info("尚未設定 Web Push 金鑰，暫時未能啟用背景通知。")
        return

    current_user = st.session_state.get("committee_user")
    if not current_user:
        st.info("請先登入以設定通知。")
        return
    auth_token = _sign_cookie(current_user)

    html = """
    <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
        <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
            <button id="enablePush" style="border:1px solid #d1d5db; border-radius:6px; padding:8px 12px; background:#111827; color:white; cursor:pointer;">啟用通知</button>
            <button id="disablePush" style="border:1px solid #d1d5db; border-radius:6px; padding:8px 12px; background:white; color:#111827; cursor:pointer;">取消通知</button>
        </div>
        <div id="pushStatus" style="margin-top:8px; color:#4b5563; font-size:14px;">正在檢查通知狀態...</div>
    </div>
    <script>
    (function () {
        const win = window.parent;
        const doc = document;
        const statusEl = doc.getElementById("pushStatus");
        const enableBtn = doc.getElementById("enablePush");
        const disableBtn = doc.getElementById("disablePush");
        const publicKey = __VAPID_PUBLIC_KEY__;
        const authToken = __PUSH_AUTH_TOKEN__;

        function setStatus(message) {
            statusEl.textContent = message;
        }

        function urlBase64ToUint8Array(base64String) {
            const padding = "=".repeat((4 - base64String.length % 4) % 4);
            const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
            const rawData = win.atob(base64);
            const outputArray = new Uint8Array(rawData.length);
            for (let i = 0; i < rawData.length; ++i) {
                outputArray[i] = rawData.charCodeAt(i);
            }
            return outputArray;
        }

        async function getRegistration() {
            if (!("serviceWorker" in win.navigator)) {
                throw new Error("此瀏覽器不支援 Service Worker。");
            }
            await win.navigator.serviceWorker.register("/sw.js");
            return await win.navigator.serviceWorker.ready;
        }

        async function refreshStatus() {
            if (!("Notification" in win) || !("PushManager" in win)) {
                setStatus("此瀏覽器不支援 Web Push 通知。");
                enableBtn.disabled = true;
                disableBtn.disabled = true;
                return;
            }
            if (!win.isSecureContext) {
                setStatus("Web Push 需要 HTTPS，請使用正式網址開啟。");
                enableBtn.disabled = true;
                return;
            }
            const registration = await getRegistration();
            const subscription = await registration.pushManager.getSubscription();
            if (subscription) {
                setStatus("通知已啟用。");
            } else if (win.Notification.permission === "denied") {
                setStatus("通知權限已被封鎖，請到瀏覽器或系統設定重新允許。");
            } else {
                setStatus("通知尚未啟用。");
            }
        }

        enableBtn.addEventListener("click", async function () {
            try {
                if (!("Notification" in win) || !("PushManager" in win)) {
                    setStatus("此瀏覽器不支援 Web Push 通知。");
                    return;
                }
                if (!win.isSecureContext) {
                    setStatus("Web Push 需要 HTTPS，請使用正式網址開啟。");
                    return;
                }
                const permission = await win.Notification.requestPermission();
                if (permission !== "granted") {
                    setStatus("尚未允許通知。");
                    return;
                }
                const registration = await getRegistration();
                let subscription = await registration.pushManager.getSubscription();
                if (!subscription) {
                    subscription = await registration.pushManager.subscribe({
                        userVisibleOnly: true,
                        applicationServerKey: urlBase64ToUint8Array(publicKey)
                    });
                }
                const response = await win.fetch("/api/push/subscribe", {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + authToken
                    },
                    body: JSON.stringify(subscription)
                });
                if (!response.ok) {
                    throw new Error("訂閱儲存失敗：" + response.status);
                }
                setStatus("通知已啟用。");
            } catch (error) {
                setStatus(error.message || "未能啟用通知。");
            }
        });

        disableBtn.addEventListener("click", async function () {
            try {
                const registration = await getRegistration();
                const subscription = await registration.pushManager.getSubscription();
                const endpoint = subscription ? subscription.endpoint : "";
                if (subscription) {
                    await subscription.unsubscribe();
                }
                const response = await win.fetch("/api/push/unsubscribe", {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + authToken
                    },
                    body: JSON.stringify({ endpoint: endpoint })
                });
                if (!response.ok) {
                    throw new Error("取消訂閱儲存失敗：" + response.status);
                }
                setStatus("通知已取消。");
            } catch (error) {
                setStatus(error.message || "未能取消通知。");
            }
        });

        refreshStatus().catch(function (error) {
            setStatus(error.message || "未能檢查通知狀態。");
        });
    })();
    </script>
    """.replace("__VAPID_PUBLIC_KEY__", json.dumps(vapid_public_key)) \
       .replace("__PUSH_AUTH_TOKEN__", json.dumps(auth_token))
    components.html(html, height=96)


def _clear_vote_cache_only():
    get_vote_data.clear()
    get_pending_vote_count.clear()
    get_pending_depose_count.clear()


def _after_vote_light():
    _clear_vote_cache_only()
    st.rerun()


def _after_vote():
    clear_caches()
    st.rerun()


def render_refresh_button(key):
    if st.button("🔄 重新整理", key=key):
        clear_caches()
        st.rerun()


def _get_comment_counts(motion_type):
    df = query_params(
        f"SELECT motion_key, COUNT(*) AS cnt FROM {TABLE_MOTION_COMMENTS} "
        "WHERE motion_type = :type GROUP BY motion_key",
        {"type": motion_type},
    )
    if df.empty:
        return {}
    return dict(zip(df["motion_key"], df["cnt"].astype(int)))


def render_discussion(motion_type, motion_key, user_id, idx, comment_count):
    label = f"💬 討論區 ({comment_count})" if comment_count else "💬 討論區"
    with st.expander(label, expanded=False):
        comments = query_params(
            f"SELECT user_id, comment_text, created_at FROM {TABLE_MOTION_COMMENTS} "
            "WHERE motion_type = :type AND motion_key = :key ORDER BY created_at ASC",
            {"type": motion_type, "key": motion_key},
        )
        if not comments.empty:
            for _, c in comments.iterrows():
                ts = c["created_at"]
                ts_str = ts.strftime("%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
                st.caption(f"**{c['user_id']}**　{ts_str}")
                st.text(c["comment_text"])
                st.divider()
        else:
            st.caption("暫時未有討論。")
        new_comment = st.text_area(
            "發表意見",
            key=f"comment_{motion_type}_{idx}",
            placeholder="就此議案發表意見。如要問 AI，可喺留言加「@Gemini 你的問題」（例如：@Gemini 呢條辯題嘅難度應否調高？），或按 Tag Gemini 取得中立分析。",
        )
        post_col, ai_col = st.columns(2)
        with post_col:
            post_comment = st.button("發表", key=f"post_comment_{motion_type}_{idx}", use_container_width=True)
        with ai_col:
            tag_ai = st.button("Tag Gemini", key=f"tag_ai_{motion_type}_{idx}", use_container_width=True)
        if post_comment:
            if new_comment.strip():
                comment_text = new_comment.strip()
                hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
                execute_query(
                    f"INSERT INTO {TABLE_MOTION_COMMENTS} (motion_type, motion_key, user_id, comment_text, created_at) "
                    "VALUES (:type, :key, :uid, :text, :now)",
                    {"type": motion_type, "key": motion_key, "uid": user_id, "text": comment_text, "now": hk_now},
                )
                snippet = comment_text if len(comment_text) <= 40 else comment_text[:40] + "⋯"
                topic_label = motion_key if len(str(motion_key)) <= 20 else str(motion_key)[:20] + "⋯"
                notify_vote_event(
                    "💬 新留言",
                    f"{user_id} 在「{topic_label}」發表意見：{snippet}",
                    exclude_user=user_id,
                    tag=f"comment-{motion_type}-{motion_key}",
                )
                # If the member tagged @Gemini, let the AI answer their question inline.
                gemini_question = _extract_gemini_question(comment_text)
                ai_failed = False
                if gemini_question is not None:
                    ensure_ai_comment_account()
                    comments = query_params(
                        f"SELECT user_id, comment_text, created_at FROM {TABLE_MOTION_COMMENTS} "
                        "WHERE motion_type = :type AND motion_key = :key ORDER BY created_at ASC",
                        {"type": motion_type, "key": motion_key},
                    )
                    with st.spinner("AI 正在回應提問，請稍候⋯"):
                        ai_text, _usage = ai_discussion_reply(
                            motion_type, motion_key, comments, question=gemini_question
                        )
                    if is_successful_ai_result(ai_text):
                        ai_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
                        execute_query(
                            f"INSERT INTO {TABLE_MOTION_COMMENTS} (motion_type, motion_key, user_id, comment_text, created_at) "
                            "VALUES (:type, :key, :uid, :text, :now)",
                            {"type": motion_type, "key": motion_key, "uid": AI_COMMENT_USER_ID, "text": ai_text, "now": ai_now},
                        )
                    else:
                        ai_failed = True
                if ai_failed:
                    st.warning("留言已儲存，但 AI 回應失敗，可稍後再按 Tag Gemini 或重新提問。")
                    st.error(ai_text)
                else:
                    queue_toast("已成功留言", icon="☑️")
                    st.rerun()
            else:
                st.warning("請輸入內容。")
        if tag_ai:
            ensure_ai_comment_account()
            if new_comment.strip():
                hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
                execute_query(
                    f"INSERT INTO {TABLE_MOTION_COMMENTS} (motion_type, motion_key, user_id, comment_text, created_at) "
                    "VALUES (:type, :key, :uid, :text, :now)",
                    {"type": motion_type, "key": motion_key, "uid": user_id, "text": new_comment.strip(), "now": hk_now},
                )
                comments = query_params(
                    f"SELECT user_id, comment_text, created_at FROM {TABLE_MOTION_COMMENTS} "
                    "WHERE motion_type = :type AND motion_key = :key ORDER BY created_at ASC",
                    {"type": motion_type, "key": motion_key},
                )
            with st.spinner("AI 正在分析討論內容，請稍候⋯"):
                ai_text, _usage = ai_discussion_reply(motion_type, motion_key, comments)
            if is_successful_ai_result(ai_text):
                hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
                execute_query(
                    f"INSERT INTO {TABLE_MOTION_COMMENTS} (motion_type, motion_key, user_id, comment_text, created_at) "
                    "VALUES (:type, :key, :uid, :text, :now)",
                    {"type": motion_type, "key": motion_key, "uid": AI_COMMENT_USER_ID, "text": ai_text, "now": hk_now},
                )
                queue_toast("AI 已回覆討論", icon="☑️")
                st.rerun()
            else:
                st.error(ai_text)


def _ballot_delete(table, topic, user_id):
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        execute_query(f"DELETE FROM {TABLE_TOPIC_VOTE_BALLOTS} WHERE topic_text = :topic_text AND user_id = :user_id", params)
    else:
        execute_query(f"DELETE FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} WHERE topic_text = :topic_text AND user_id = :user_id", params)


def _ballot_upsert(table, topic, user_id, vote, reasons=None):
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        if vote == "agree":
            execute_query(
                f"INSERT INTO {TABLE_TOPIC_VOTE_BALLOTS} (topic_text, user_id, vote_choice) VALUES (:topic_text, :user_id, 'agree')"
                " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = 'agree'",
                params,
            )
        else:
            execute_query(
                f"INSERT INTO {TABLE_TOPIC_VOTE_BALLOTS} (topic_text, user_id, vote_choice, against_reasons) VALUES (:topic_text, :user_id, 'against', :reasons)"
                " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = 'against', against_reasons = EXCLUDED.against_reasons",
                {**params, "reasons": reasons or "[]"},
            )
    else:
        execute_query(
            f"INSERT INTO {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} (topic_text, user_id, vote_choice) VALUES (:topic_text, :user_id, :vote)"
            " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = :vote",
            {**params, "vote": vote},
        )


def _ballot_switch_agree(table, topic, user_id):
    params = {"user_id": user_id, "topic_text": topic}
    if table == TABLE_TOPIC_VOTES:
        execute_query(
            f"UPDATE {TABLE_TOPIC_VOTE_BALLOTS} SET vote_choice = 'agree', against_reasons = '[]' WHERE topic_text = :topic_text AND user_id = :user_id",
            params,
        )
    else:
        execute_query(
            f"UPDATE {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS} SET vote_choice = 'agree' WHERE topic_text = :topic_text AND user_id = :user_id",
            params,
        )


def _check_category_would_exceed(category):
    """Check if adding one more topic of this category would push it past 20% of the bank."""
    conn = get_connection()
    all_topics_df = conn.query(f"SELECT category FROM {TABLE_TOPICS}", ttl=5)
    if all_topics_df.empty:
        return False, 0.0, 0, 0
    total = len(all_topics_df)
    cat_count = int((all_topics_df["category"] == category).sum())
    new_ratio = (cat_count + 1) / (total + 1)
    return new_ratio > 0.2, new_ratio, cat_count, total


@st.dialog("類別佔比提醒")
def _confirm_agree_category_warning(topic, user_id, category, ratio, cat_count, total, is_switch, table, after_vote_fn):
    st.warning(
        f"⚠️ 若此辯題通過，類別「{category}」將佔辯題庫 **{ratio*100:.1f}%**"
        f"（現有 {total} 題中已有 {cat_count} 題同類）。\n\n"
        "繼續投同意票可能令辯題庫失衡。是否確認？"
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ 確認投同意票", use_container_width=True):
            with st.spinner("處理你的投票中，請稍等⋯"):
                if is_switch:
                    _ballot_switch_agree(table, topic, user_id)
                    queue_toast("已轉投同意票！", icon="↪️️")
                else:
                    _ballot_upsert(table, topic, user_id, "agree")
                    queue_toast("已投下同意票！", icon="☑️")
                after_vote_fn()
    with col2:
        if st.button("❌ 取消", use_container_width=True):
            st.rerun()


def render_vote_buttons(i, user_id, topic, agree_list, against_list, against_reason_map,
                        table, agree_label, against_label, after_vote_fn, col2, col3,
                        against_dialog_fn=None, agree_switch_toast="已轉投同意票！",
                        topic_category=None):
    """Renders the agree (col2) and against (col3) vote button columns."""

    with col2:
        if user_id in agree_list:
            if st.button("已同意 (點擊撤回)", key=f"{table}_f_done_{i}"):
                with st.spinner("撤回投票中..."):
                    _ballot_delete(table, topic, user_id)
                    queue_toast("已撤回同意票！", icon="↩️")
                    after_vote_fn()
        elif user_id in against_list:
            if st.button("轉投同意", key=f"{table}_switch_to_f_{i}"):
                if topic_category and table == TABLE_TOPIC_VOTES:
                    exceeds, ratio, cat_count, total = _check_category_would_exceed(topic_category)
                    if exceeds:
                        _confirm_agree_category_warning(
                            topic, user_id, topic_category, ratio, cat_count, total,
                            is_switch=True, table=table, after_vote_fn=after_vote_fn)
                        return
                with st.spinner("更改投票中..."):
                    _ballot_switch_agree(table, topic, user_id)
                    queue_toast(agree_switch_toast, icon="↪️️")
                    after_vote_fn()
        else:
            if st.button(f"✅ {agree_label}", key=f"{table}_vote_f_{i}"):
                if topic_category and table == TABLE_TOPIC_VOTES:
                    exceeds, ratio, cat_count, total = _check_category_would_exceed(topic_category)
                    if exceeds:
                        _confirm_agree_category_warning(
                            topic, user_id, topic_category, ratio, cat_count, total,
                            is_switch=False, table=table, after_vote_fn=after_vote_fn)
                        return
                with st.spinner("處理你的投票中，請稍等⋯"):
                    _ballot_upsert(table, topic, user_id, "agree")
                    queue_toast("已投下同意票！", icon="☑️")
                    after_vote_fn()

    with col3:
        if user_id in against_list:
            if st.button("已反對 (點擊撤回)", key=f"{table}_a_done_{i}"):
                with st.spinner("撤回投票中..."):
                    _ballot_delete(table, topic, user_id)
                    queue_toast("已撤回不同意票！", icon="↩️")
                    after_vote_fn()
        elif user_id in agree_list:
            if st.button("轉投反對", key=f"{table}_switch_to_a_{i}"):
                if against_dialog_fn:
                    against_dialog_fn(topic, user_id, against_reason_map, is_switch=True)
                else:
                    with st.spinner("更改投票中..."):
                        _ballot_upsert(table, topic, user_id, "against")
                        queue_toast("已轉投不同意票！", icon="↪️️")
                        after_vote_fn()
        else:
            if st.button(f"❌ {against_label}", key=f"{table}_vote_a_{i}"):
                if against_dialog_fn:
                    against_dialog_fn(topic, user_id, against_reason_map, is_switch=False)
                else:
                    with st.spinner("處理你的投票中，請稍等⋯"):
                        _ballot_upsert(table, topic, user_id, "against")
                        queue_toast("已投下不同意票！", icon="☑️")
                        after_vote_fn()


def check_vote_resolution(agree_count, against_count, threshold, topic, agree_list, against_list,
                          mode, author=None, category=None, difficulty=None):
    """Check vote counts and auto-resolve if threshold met. mode: 'topic' or 'depose'."""
    if mode == "topic":
        if agree_count >= threshold and agree_count > against_count:
            st.success(f"辯題「{topic}」已獲得足夠票數，正在寫入辯題庫...")
            execute_query(
                f"INSERT INTO {TABLE_TOPICS} (topic_text, author, category, difficulty) VALUES (:topic_text, :author, :category, :difficulty)",
                {"topic_text": topic, "author": author, "category": category, "difficulty": difficulty}
            )
            updated = execute_query_count(
                f"UPDATE {TABLE_TOPIC_VOTES} SET status = 'passed' WHERE topic_text = :topic_text AND status = 'pending'",
                {"topic_text": topic}
            )
            if updated:
                notify_vote_event(
                    "辯題投票通過",
                    f"「{topic}」已通過並加入辯題庫。",
                    tag=f"topic-vote-passed-{topic}",
                )
            clear_caches()
            st.balloons()
            st.rerun()
        if against_count >= threshold and against_count > agree_count:
            st.error(f"辯題「{topic}」已獲得{against_count}票不同意票，正在刪除辯題...")
            updated = execute_query_count(
                f"UPDATE {TABLE_TOPIC_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
                {"topic_text": topic}
            )
            if updated:
                notify_vote_event(
                    "辯題投票否決",
                    f"「{topic}」已被否決。",
                    tag=f"topic-vote-rejected-{topic}",
                )
            clear_caches()
            st.rerun()
    elif mode == "depose":
        if agree_count >= threshold and agree_count > against_count:
            st.error(f"罷免動議「{topic}」已獲通過，正在從辯題庫刪除該辯題...")
            updated = execute_query_count(
                f"UPDATE {TABLE_TOPIC_REMOVAL_VOTES} SET status = 'passed' WHERE topic_text = :topic_text AND status = 'pending'",
                {"topic_text": topic},
            )
            # Always remove the topic from the bank when a removal passes.
            # Decoupled from `updated` so it self-heals even if the status row was
            # already resolved (e.g. a prior run/session), preventing stale entries.
            execute_query(f"DELETE FROM {TABLE_TOPICS} WHERE topic_text = :topic_text", {"topic_text": topic})
            if updated:
                notify_vote_event(
                    "罷免動議通過",
                    f"「{topic}」已被罷免並從辯題庫移除。",
                    tag=f"topic-removal-passed-{topic}",
                )
            clear_caches()
            st.rerun()
        if against_count >= threshold and against_count > agree_count:
            st.success(f"罷免動議「{topic}」已被否決，正在刪除該罷免動議...")
            updated = execute_query_count(
                f"UPDATE {TABLE_TOPIC_REMOVAL_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
                {"topic_text": topic},
            )
            if updated:
                notify_vote_event(
                    "罷免動議否決",
                    f"「{topic}」的罷免動議已被否決。",
                    tag=f"topic-removal-rejected-{topic}",
                )
            clear_caches()
            st.balloons()
            st.rerun()


# Get committee cookie manager first
cm = committee_cookie_manager()


def get_vote_ai_model():
    settings = get_ai_model_settings()
    return settings.get("default_model") or "Gemini 2.5 Flash"


def _gather_topic_review_context(category, difficulty):
    """Bank composition (duplicate/ratio check) + historical proposal pass-rate stats."""
    conn = get_connection()
    lines = []

    # Existing bank: category ratio + same-category topics for duplicate/overlap check
    bank_df = conn.query(f"SELECT topic_text, category FROM {TABLE_TOPICS}", ttl=5)
    if not bank_df.empty:
        total = len(bank_df)
        same_cat = bank_df[bank_df["category"] == category]
        cat_count = len(same_cat)
        ratio = cat_count / total * 100 if total else 0.0
        lines.append(f"現有辯題庫共 {total} 條；類別「{category}」佔 {cat_count} 條（{ratio:.0f}%，上限 20%）。")
        sample = same_cat["topic_text"].tolist()[:15]
        if sample:
            lines.append("同類別現有辯題（用嚟檢查重複／重疊）：")
            lines.extend(f"- {t}" for t in sample)
    else:
        lines.append("現有辯題庫暫時無資料。")

    # Historical proposal outcomes (resolved votes only)
    votes_df = conn.query(
        f"SELECT status, category, difficulty FROM {TABLE_TOPIC_VOTES} "
        "WHERE status IN ('passed', 'rejected')",
        ttl=5,
    )
    if not votes_df.empty:
        def _rate(df):
            n = len(df)
            passed = int((df["status"] == "passed").sum())
            return passed, n, (passed / n * 100 if n else 0.0)

        passed, n, rate = _rate(votes_df)
        lines.append(f"歷史提案通過率：整體 {passed}/{n}（{rate:.0f}%）。")
        cat_df = votes_df[votes_df["category"] == category]
        if not cat_df.empty:
            passed, n, rate = _rate(cat_df)
            lines.append(f"　同類別「{category}」：{passed}/{n}（{rate:.0f}%）。")
        try:
            diff_df = votes_df[votes_df["difficulty"] == int(difficulty)]
        except (TypeError, ValueError):
            diff_df = votes_df.iloc[0:0]
        if not diff_df.empty:
            passed, n, rate = _rate(diff_df)
            diff_label = DIFFICULTY_OPTIONS.get(int(difficulty), str(difficulty))
            lines.append(f"　同難度「{diff_label}」：{passed}/{n}（{rate:.0f}%）。")
    else:
        lines.append("歷史提案投票數據不足，通過機率只能作定性判斷。")

    return "\n".join(lines)


def ai_review_topic(topic, category, difficulty):
    difficulty_label = DIFFICULTY_OPTIONS.get(int(difficulty), str(difficulty))
    analytics_context = _gather_topic_review_context(category, difficulty)
    user_text = build_vote_topic_review_prompt(
        topic,
        category,
        difficulty_label,
        category_options=CATEGORIES,
        difficulty_definitions=DIFFICULTY_CRITERIA,
        analytics_context=analytics_context,
    )
    return generate_general_ai_reply(VOTE_TOPIC_REVIEW_SYSTEM_PROMPT, user_text, get_vote_ai_model())


def _find_stale_removed_topics():
    """Topics still in the bank despite a passed removal motion (data-integrity check)."""
    df = get_connection().query(
        f"SELECT t.topic_text FROM {TABLE_TOPICS} t "
        f"JOIN {TABLE_TOPIC_REMOVAL_VOTES} r ON r.topic_text = t.topic_text "
        "WHERE r.status = 'passed'",
        ttl=5,
    )
    return df["topic_text"].tolist() if not df.empty else []


def _gather_bank_analysis_context():
    """Build a summary + topic list of the current topic bank for AI analysis."""
    conn = get_connection()
    bank_df = conn.query(f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS}", ttl=5)
    if bank_df.empty:
        return "辯題庫暫時無題目。", []

    total = len(bank_df)
    summary_lines = [f"總題目數：{total}", "類別分佈："]
    cat_counts = bank_df["category"].value_counts()
    for cat in CATEGORIES:
        c = int(cat_counts.get(cat, 0))
        pct = c / total * 100 if total else 0
        flag = "（已超過 20% 上限）" if pct > 20 else ""
        summary_lines.append(f"- {cat}：{c}（{pct:.0f}%）{flag}")
    for cat, c in cat_counts.items():
        if cat not in CATEGORIES:
            summary_lines.append(f"- {cat or '（未分類）'}：{int(c)}（{int(c) / total * 100:.0f}%）")

    summary_lines.append("難度分級標準：")
    for lvl in (1, 2, 3):
        summary_lines.append(f"- {DIFFICULTY_CRITERIA.get(lvl, lvl)}")

    summary_lines.append("難度分佈：")
    for lvl in (1, 2, 3):
        c = int((bank_df["difficulty"] == lvl).sum())
        pct = c / total * 100 if total else 0
        summary_lines.append(f"- {DIFFICULTY_OPTIONS.get(lvl, lvl)}：{c}（{pct:.0f}%）")

    votes_df = conn.query(
        f"SELECT status FROM {TABLE_TOPIC_VOTES} WHERE status IN ('passed', 'rejected')",
        ttl=5,
    )
    if not votes_df.empty:
        n = len(votes_df)
        passed = int((votes_df["status"] == "passed").sum())
        summary_lines.append(f"歷史提案通過率：{passed}/{n}（{passed / n * 100:.0f}%）")

    topic_lines = []
    for _, r in bank_df.iterrows():
        try:
            diff_label = DIFFICULTY_OPTIONS.get(int(r["difficulty"]), str(r["difficulty"]))
        except (TypeError, ValueError):
            diff_label = "—"
        topic_lines.append(f"- {r['topic_text']}（{r.get('category') or '—'}｜{diff_label}）")

    return "\n".join(summary_lines), topic_lines


def ai_analyze_topic_bank():
    bank_summary, topic_lines = _gather_bank_analysis_context()
    user_text = build_vote_bank_analysis_prompt(bank_summary, topic_lines)
    return generate_general_ai_reply(VOTE_BANK_ANALYSIS_SYSTEM_PROMPT, user_text, get_vote_ai_model())


def save_bank_analysis(analysis_text, user_id):
    """Persist the latest bank analysis to system_config so all committee members share it."""
    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")
    for key, val in (
        ("vote_bank_analysis", analysis_text),
        ("vote_bank_analysis_at", hk_now),
        ("vote_bank_analysis_by", user_id or ""),
    ):
        execute_query(
            "INSERT INTO system_config (key, value, updated_at) VALUES (:k, :v, :u) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            {"k": key, "v": val, "u": hk_now},
        )
    return hk_now


def load_bank_analysis():
    """Return (analysis_text, analysed_at, analysed_by) — the shared saved analysis."""
    return (
        get_system_config("vote_bank_analysis"),
        get_system_config("vote_bank_analysis_at"),
        get_system_config("vote_bank_analysis_by"),
    )


def _extract_gemini_question(comment):
    """Return the question after an @Gemini mention, or None if not tagged.

    Falls back to the whole comment when @Gemini is present without trailing text.
    """
    idx = comment.lower().find("@gemini")
    if idx == -1:
        return None
    question = comment[idx + len("@gemini"):].strip().lstrip("：:，, ").strip()
    return question or comment.strip()


def _build_motion_background(motion_type, motion_key):
    """Category + current difficulty (with the full rubric) for the discussed motion."""
    src = TABLE_TOPIC_VOTES if motion_type == "topic_vote" else TABLE_TOPICS
    meta = query_params(
        f"SELECT category, difficulty FROM {src} WHERE topic_text = :t LIMIT 1",
        {"t": motion_key},
    )
    lines = []
    if not meta.empty:
        row = meta.iloc[0]
        if row.get("category"):
            lines.append(f"辯題類別：{row['category']}")
        try:
            diff_label = DIFFICULTY_OPTIONS.get(int(row["difficulty"]))
        except (TypeError, ValueError):
            diff_label = None
        if diff_label:
            lines.append(f"目前難度：{diff_label}")
    lines.append("難度分級標準：")
    lines.extend(f"- {DIFFICULTY_CRITERIA[lvl]}" for lvl in (1, 2, 3))
    return "\n".join(lines)


def ai_discussion_reply(motion_type, motion_key, comments, question=None):
    discussion_lines = []
    for _, c in comments.iterrows():
        discussion_lines.append(f"{c['user_id']}：{c['comment_text']}")
    removal_reasons = None
    if motion_type == "topic_removal":
        reason_rows = query_params(
            f"SELECT removal_reasons FROM {TABLE_TOPIC_REMOVAL_VOTES} "
            "WHERE topic_text = :topic AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            {"topic": motion_key},
        )
        if not reason_rows.empty:
            removal_reasons = parse_reason_list(reason_rows.iloc[0]["removal_reasons"])
    background = _build_motion_background(motion_type, motion_key)
    user_text = build_vote_discussion_prompt(
        motion_type, motion_key, discussion_lines,
        removal_reasons=removal_reasons, question=question, background=background,
    )
    return generate_general_ai_reply(VOTE_DISCUSSION_SYSTEM_PROMPT, user_text, AI_DISCUSSION_MODEL)


def ensure_ai_comment_account():
    if st.session_state.get("_ai_comment_account_ready"):
        return
    execute_query(
        f"""
        INSERT INTO {TABLE_ACCOUNTS} (user_id, password_hash, account_status, account_disabled)
        VALUES (:uid, '', 'inactive', TRUE)
        ON CONFLICT (user_id) DO UPDATE SET account_disabled = TRUE
        """,
        {"uid": AI_COMMENT_USER_ID},
    )
    st.session_state["_ai_comment_account_ready"] = True

@st.dialog("投反對票")
def cast_against_vote_dialog(topic, user_id, against_reason_map, is_switch=False):
    st.write(f"**{topic}**")
    if is_switch:
        st.info("你目前已投同意票，確認後將轉為反對票。")
    # Use inline pills instead of st.multiselect: the multiselect dropdown popover
    # renders behind/outside the dialog modal, making the options impossible to click.
    selected_reasons = st.pills(
        "請選擇不同意原因（至少選一項）",
        options=TOPIC_REJECTION_REASONS,
        selection_mode="multi",
        key=f"against_reasons_{topic}",
    )
    other_reason = st.text_area(
        "其他原因（如有）",
        placeholder="如需要，可補充具體修訂意見。",
        key=f"against_other_{topic}",
    )
    if st.button("確認投票", type="primary"):
        reasons = collect_reasons(selected_reasons, other_reason)
        if not reasons:
            st.warning("請至少選擇或輸入一個不同意原因。")
        else:
            with st.spinner("處理你的投票中，請稍等⋯"):
                execute_query(
                    f"INSERT INTO {TABLE_TOPIC_VOTE_BALLOTS} (topic_text, user_id, vote_choice, against_reasons)"
                    " VALUES (:topic_text, :user_id, 'against', :reasons)"
                    " ON CONFLICT (topic_text, user_id) DO UPDATE SET vote_choice = 'against', against_reasons = EXCLUDED.against_reasons",
                    {"topic_text": topic, "user_id": user_id, "reasons": dump_json(reasons)}
                )
                queue_toast("已轉投不同意票！" if is_switch else "已投下不同意票！", icon="↪️️" if is_switch else "☑️")
                _clear_vote_cache_only()
                st.rerun()

user_id = require_committee()
show_noti_popup(user_id)
show_queued_toast()
st.caption("活躍成員標準：整體投票率達 40%，且最近十次投票至少參與三次。")
st.info(f"已登入帳戶：**{user_id}**")

_active_count, active_user_list = get_active_user_count()
_naturally_active = user_id == "admin" or user_id in active_user_list
_bypass = is_bypass_active_check(user_id)
is_active = _naturally_active or _bypass
ENTRY_THRESHOLD = max(5, math.ceil(_active_count * 0.4))
DEPOSE_THRESHOLD = max(6, math.ceil(_active_count * 0.5))

if user_id != "admin":
    if _naturally_active:
        st.success("帳戶狀態：活躍成員")
    elif _bypass:
        _bypass_until = get_bypass_active_until(user_id)
        st.info(f"帳戶狀態：非活躍成員（提案限制已被臨時解除，至 {_bypass_until.strftime('%Y-%m-%d %H:%M')}）")
    else:
        st.warning("帳戶狀態：非活躍成員，你將不能提出新辯題或罷免動議，但仍可參與投票。")

@st.cache_data(ttl=5)
def get_vote_data():
    conn = get_connection()
    df = conn.query(
        f"""
        SELECT
            topic_text,
            proposer_user_id,
            status,
            created_at,
            deadline_date,
            approval_threshold,
            category,
            difficulty
        FROM {TABLE_TOPIC_VOTES}
        ORDER BY created_at DESC
        """,
        ttl=5,
    )
    df = df.fillna("")

    # Load ballots for pending topics only — historical ballots are not needed for the UI
    ballots = conn.query(
        f"SELECT b.topic_text, b.user_id, b.vote_choice, b.against_reasons"
        f" FROM {TABLE_TOPIC_VOTE_BALLOTS} b"
        f" JOIN {TABLE_TOPIC_VOTES} tv ON b.topic_text = tv.topic_text"
        " WHERE tv.status = 'pending'",
        ttl=0
    )
    agree_map, against_map, reasons_map = {}, {}, {}
    if not ballots.empty:
        for _, b in ballots.iterrows():
            t, uid, v = b["topic_text"], b["user_id"], b["vote_choice"]
            if v == "agree":
                agree_map.setdefault(t, []).append(uid)
            else:
                against_map.setdefault(t, []).append(uid)
                raw = b.get("against_reasons")
                r = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
                if r:
                    reasons_map.setdefault(t, {})[uid] = r

    pending, passed, rejected = [], [], []
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        t = row_dict["topic_text"]
        row_dict["agree_users"] = agree_map.get(t, [])
        row_dict["against_users"] = against_map.get(t, [])
        row_dict["against_reasons"] = reasons_map.get(t, {})
        status = row_dict.get("status", "")
        if status == "pending":
            pending.append(row_dict)
        elif status == "passed":
            passed.append(t)
        elif status == "rejected":
            rejected.append(t)

    return pending, passed, rejected


@st.cache_data(ttl=5)
def get_pending_vote_count():
    df = get_connection().query(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_VOTES} WHERE status = 'pending'",
        ttl=5,
    )
    return int(df.iloc[0]["cnt"]) if not df.empty else 0


@st.cache_data(ttl=5)
def get_pending_depose_count():
    df = get_connection().query(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_TOPIC_REMOVAL_VOTES} WHERE status = 'pending'",
        ttl=5,
    )
    return int(df.iloc[0]["cnt"]) if not df.empty else 0


# Pre-fetch pending counts for tab badges
_pending_vote_count = get_pending_vote_count()
_pending_depose_count = get_pending_depose_count()

_tab_options = ["proposal", "topic_vote", "depose_vote", "bank_analysis", "member_stats", "account"]


def format_tab_label(tab_name):
    if tab_name == "proposal":
        return "📝 提案"
    if tab_name == "topic_vote":
        return f"📊 辯題投票 ({_pending_vote_count})" if _pending_vote_count else "📊 辯題投票"
    if tab_name == "depose_vote":
        return f"✂️ 罷免投票 ({_pending_depose_count})" if _pending_depose_count else "✂️ 罷免投票"
    if tab_name == "bank_analysis":
        return "🔍 辯題庫分析"
    if tab_name == "member_stats":
        return "👥 參與率"
    return "🔐 帳戶"


if hasattr(st, "segmented_control"):
    selected_tab = st.segmented_control(
        "頁面",
        options=_tab_options,
        default="proposal",
        format_func=format_tab_label,
        key="vote_selected_tab",
        label_visibility="collapsed",
        width="stretch",
    )
else:
    selected_tab = st.radio(
        "頁面",
        options=_tab_options,
        format_func=format_tab_label,
        key="vote_selected_tab",
        horizontal=True,
        label_visibility="collapsed",
    )

if selected_tab is None:
    selected_tab = "proposal"

if selected_tab == "proposal":
    with st.container(border=True):
        st.subheader("提出新辯題")
        st.caption(f"目前活躍成員：{_active_count} 人 ｜ 入庫門檻：{ENTRY_THRESHOLD} 票")
        st.caption("甲乙辯題格式：（甲）XXX／（乙）YYY，請使用全形中文符號。")
        st.caption("")
        new_topic = st.text_input("請輸入完整辯題")
        new_category = st.selectbox("辯題類別", options=CATEGORIES)
        st.caption("辯題難度標準：")
        for _lvl in (1, 2, 3):
            st.caption(DIFFICULTY_CRITERIA[_lvl])
        new_difficulty = st.selectbox(
            "辯題難度",
            options=[1, 2, 3],
            format_func=lambda x: DIFFICULTY_OPTIONS[x]
        )
        if st.button("AI 審查提案", key="ai_review_new_topic"):
            if not new_topic.strip():
                st.warning("請先輸入完整辯題。")
            else:
                with st.spinner("AI 正在審查提案，請稍候⋯"):
                    ai_review, _usage = ai_review_topic(new_topic.strip(), new_category, new_difficulty)
                st.session_state["proposal_ai_review"] = {
                    "topic": new_topic.strip(),
                    "category": new_category,
                    "difficulty": new_difficulty,
                    "review": ai_review,
                }
        review_data = st.session_state.get("proposal_ai_review")
        if (
            review_data
            and review_data.get("topic") == new_topic.strip()
            and review_data.get("category") == new_category
            and review_data.get("difficulty") == new_difficulty
        ):
            with st.expander("AI 審查結果", expanded=True):
                st.markdown(review_data["review"])

    # If there are >= 10 pending topics, block new submissions and remind voting first.
    pending_vote_data, _, _ = get_vote_data()
    pending_count = len(pending_vote_data) if pending_vote_data else 0
    submit_disabled = pending_count >= 10 or not is_active
    if not is_active:
        st.info("非活躍成員不能提出新辯題。")
    elif pending_count >= 10:
        st.warning(
            f"目前已有 **{pending_count}** 個待表決辯題。"
            "請先到「📊 辯題投票」完成投票，直到待表決辯題數量少於10個後再提交新辯題。"
        )

    if "confirm_imbalance" not in st.session_state:
        st.session_state["confirm_imbalance"] = False

    if st.button("提交辯題", disabled=submit_disabled):
        if not new_topic.strip():
            st.warning("你未輸入任何文字！")
        else:
            conn = get_connection()
            all_topics_df = conn.query(
                f"SELECT topic_text, category FROM {TABLE_TOPICS}",
                ttl=5,
            )
            all_votes_df = conn.query(
                f"SELECT topic_text FROM {TABLE_TOPIC_VOTES} WHERE status = 'pending'",
                ttl=5,
            )

            existing_topics = all_topics_df["topic_text"].tolist() if not all_topics_df.empty else []
            existing_votes = all_votes_df["topic_text"].tolist() if not all_votes_df.empty else []

            if new_topic in existing_votes or new_topic in existing_topics:
                st.error("此辯題已存在！")
            else:
                if not all_topics_df.empty:
                    total_topics = len(all_topics_df)
                    cat_count = int((all_topics_df["category"] == new_category).sum())
                    cat_ratio = cat_count / total_topics
                else:
                    total_topics = 0
                    cat_count = 0
                    cat_ratio = 0

                if cat_ratio > 0.2:
                    st.session_state["confirm_imbalance"] = True
                    st.session_state["pending_topic_data"] = {
                        "new_topic": new_topic, "new_category": new_category, "new_difficulty": new_difficulty
                    }
                    st.warning(
                        f"⚠️ 類別「{new_category}」目前已佔辯題庫 **{cat_ratio*100:.1f}%**"
                        f"（共 {total_topics} 題中有 {cat_count} 題）。"
                        "繼續新增同類辯題將令辯題庫失衡。"
                    )
                else:
                    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                    hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                    deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
                    query = (
                        f"INSERT INTO {TABLE_TOPIC_VOTES} "
                        "(topic_text, proposer_user_id, status, created_at, deadline_date, approval_threshold, category, difficulty) "
                        "VALUES (:new_topic, :user_id, 'pending', :created_at, :deadline, :threshold, :category, :difficulty)"
                    )
                    param = {"new_topic": new_topic, "user_id": user_id, "created_at": hk_time, "deadline": deadline, "threshold": ENTRY_THRESHOLD, "category": new_category, "difficulty": new_difficulty}
                    execute_query(query, param)
                    notify_vote_event(
                        "新辯題待投票",
                        f"「{new_topic}」已加入投票區，截止日期為 {deadline}。",
                        exclude_user=user_id,
                        tag=f"topic-vote-new-{new_topic}",
                    )
                    clear_caches()
                    st.success("辯題已加入投票區！")

    if st.session_state.get("confirm_imbalance"):
        pending_topic_data = st.session_state["pending_topic_data"]
        st.warning(
            f"⚠️ 類別「{pending_topic_data['new_category']}」目前佔比已超過 20%，繼續新增同類辯題將令辯題庫失衡。是否確認繼續？"
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ 確認繼續提交"):
                hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
                query = (
                    f"INSERT INTO {TABLE_TOPIC_VOTES} "
                    "(topic_text, proposer_user_id, status, created_at, deadline_date, approval_threshold, category, difficulty) "
                    "VALUES (:new_topic, :user_id, 'pending', :created_at, :deadline, :threshold, :category, :difficulty)"
                )
                topic_params = {"new_topic": pending_topic_data["new_topic"], "user_id": user_id, "created_at": hk_time, "deadline": deadline, "threshold": ENTRY_THRESHOLD, "category": pending_topic_data["new_category"], "difficulty": pending_topic_data["new_difficulty"]}
                execute_query(query, topic_params)
                notify_vote_event(
                    "新辯題待投票",
                    f"「{pending_topic_data['new_topic']}」已加入投票區，截止日期為 {deadline}。",
                    exclude_user=user_id,
                    tag=f"topic-vote-new-{pending_topic_data['new_topic']}",
                )
                clear_caches()
                st.session_state["confirm_imbalance"] = False
                st.success("辯題已加入投票區！")
        with col2:
            if st.button("❌ 取消"):
                st.session_state["confirm_imbalance"] = False
                st.rerun()

    with st.container(border=True):
        st.subheader("提出罷免動議")
        st.caption(f"目前活躍成員：{_active_count} 人 ｜ 罷免門檻：{DEPOSE_THRESHOLD} 票")

        try:
            conn = get_connection()
            df = conn.query(f"SELECT topic_text FROM {TABLE_TOPICS}", ttl=5)
        except Exception as e:
            st.error(f"連線錯誤: {e}")
            st.stop()

        topics_to_depose = st.multiselect(
                "請選擇要提出罷免動議的辯題 (可多選)",
                options=df["topic_text"].to_list()
            )
        depose_reason_choices = st.multiselect(
            "請選擇提出罷免動議的原因（可多選）",
            options=DEPOSE_REASONS,
            key="depose_reason_choices"
        )
        depose_reason_other = st.text_area(
            "其他補充原因（如有）",
            key="depose_reason_other",
            placeholder="例如：題目最近已在其他比賽打過。"
        )

        if not is_active:
            st.info("非活躍成員不能提出罷免動議。")

        if st.button("提出罷免動議", disabled=not is_active):
            if not topics_to_depose:
                st.warning("你未選擇任何辯題！")
            elif not collect_reasons(depose_reason_choices, depose_reason_other):
                st.warning("請至少交代一個罷免原因。")
            else:
                conn = get_connection()
                exist_votes = conn.query(
                    f"SELECT topic_text FROM {TABLE_TOPIC_REMOVAL_VOTES} WHERE status = 'pending'",
                    ttl=5,
                )
                exist_depose_topics = exist_votes["topic_text"].tolist()
                if len(exist_depose_topics) >= 10:
                    st.warning("目前已有10個辯題罷免動議。請先到「✂️ 罷免投票」完成投票，直到辯題罷免動議數量少於10個後再提交新動議。")
                    st.stop()
                proposed = True
                proposal_reasons = collect_reasons(depose_reason_choices, depose_reason_other)
                for t in topics_to_depose:
                    if t in exist_depose_topics:
                        proposed = False
                    else:
                        hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                        hk_time = hk_now.strftime("%Y-%m-%d %H:%M:%S")
                        deadline = (hk_now.date() + timedelta(days=7)).strftime("%Y-%m-%d")
                        query = f"""
                        INSERT INTO {TABLE_TOPIC_REMOVAL_VOTES} (
                            topic_text, proposer_user_id, status, created_at, removal_reasons, deadline_date, approval_threshold
                        ) VALUES (
                            :topic, :user_id, 'pending', :created_at, :proposal_reasons, :deadline, :threshold
                        )
                        """
                        param = {
                            "topic": t,
                            "user_id": user_id,
                            "created_at": hk_time,
                            "proposal_reasons": dump_json(proposal_reasons),
                            "deadline": deadline,
                            "threshold": DEPOSE_THRESHOLD
                        }
                        execute_query(query, param)
                        notify_vote_event(
                            "新罷免動議待投票",
                            f"「{t}」已提出罷免動議，截止日期為 {deadline}。",
                            exclude_user=user_id,
                            tag=f"topic-removal-new-{t}",
                        )
                clear_caches()
                if proposed:
                    st.success("罷免動議已提出！")
                else:
                    st.warning("有辯題已存在於罷免動議區，該辯題將不會被重複提出。其他辯題已成功提出罷免動議。")


elif selected_tab == "topic_vote":
    st.subheader("待表決辯題")
    st.caption("當同意票數達入庫門檻，且同意票多於不同意票時，系統會自動將辯題寫入辯題庫。")
    st.caption("當不同意票數達入庫門檻，且不同意票多於同意票時，系統會自動否決該辯題。")

    render_refresh_button("refresh_vote_tab2")
    st.divider()
    
    vote_data, passed_list, rejected_list = get_vote_data()
    _tv_comment_counts = _get_comment_counts("topic_vote") if vote_data else {}

    if not vote_data:
        st.info("目前沒有待表決的辯題。")
    else:
        for i, row in enumerate(vote_data):
            topic = row["topic_text"]
            author = row["proposer_user_id"]

            agree_list = row["agree_users"]
            against_list = row["against_users"]
            against_reason_map = parse_reason_map(row.get("against_reasons", ""))

            agree_count = len(agree_list)
            against_count = len(against_list)
            row_threshold = int(row.get("approval_threshold") or ENTRY_THRESHOLD)

            deadline_passed, deadline_str = parse_deadline_row(row)

            # Auto-reject expired topics before rendering the card (avoids flash)
            if deadline_passed:
                st.warning(f"辯題「{topic}」投票期限（{deadline_str} 23:59）已過，未達入庫標準，系統自動否決。")
                updated = execute_query_count(
                    f"UPDATE {TABLE_TOPIC_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
                    {"topic_text": topic},
                )
                if updated:
                    notify_vote_event(
                        "辯題投票逾期",
                        f"「{topic}」未達入庫標準，已自動否決。",
                        tag=f"topic-vote-expired-{topic}",
                    )
                clear_caches()
                st.rerun()

            with st.container(border=True):
                st.markdown(f"#### {topic}")
                cat = row.get("category") or "—"
                diff = row.get("difficulty")
                diff_label = DIFFICULTY_OPTIONS.get(int(diff), "—") if diff else "—"
                deadline_display = f" ｜ 截止：{deadline_str} 23:59" if deadline_str else ""
                st.caption(f"🏷️ {cat} ｜ {diff_label}{deadline_display}")
                st.caption(f"提出者：{author} ｜ 入庫門檻：{row_threshold} 票")

                agree_progress = min(agree_count / row_threshold, 1.0)
                against_progress = min(against_count / row_threshold, 1.0)

                st.progress(agree_progress, text=f"同意票進度：{agree_count} / {row_threshold}")
                st.progress(against_progress, text=f"不同意票進度：{against_count} / {row_threshold}")

                btn_col1, btn_col2 = st.columns(2)
                render_vote_buttons(
                    i, user_id, topic, agree_list, against_list, against_reason_map,
                    table=TABLE_TOPIC_VOTES, agree_label="同意", against_label="不同意",
                    after_vote_fn=_after_vote_light, col2=btn_col1, col3=btn_col2,
                    against_dialog_fn=cast_against_vote_dialog,
                    topic_category=row.get("category"),
                )

            if against_reason_map:
                with st.expander(f"查看「{topic}」不同意理由", expanded=False):
                    render_reason_lines(against_reason_map, "暫時未有已記錄的不同意理由。")

            render_discussion("topic_vote", topic, user_id, i, _tv_comment_counts.get(topic, 0))

            check_vote_resolution(agree_count, against_count, row_threshold, topic, agree_list, against_list,
                                   mode="topic", author=author,
                                   category=row.get("category"), difficulty=row.get("difficulty"))

    st.divider()

    with st.expander("📜 投票歷史記錄（最近二十個）", expanded=False):
        from functions import query_params as _qp
        history = _qp(f"""
            SELECT tv.topic_text, tv.status, tv.created_at, tv.approval_threshold, tv.category,
                   (SELECT COUNT(*) FROM {TABLE_TOPIC_VOTE_BALLOTS} b WHERE b.topic_text = tv.topic_text AND b.vote_choice = 'agree') AS agree,
                   (SELECT COUNT(*) FROM {TABLE_TOPIC_VOTE_BALLOTS} b WHERE b.topic_text = tv.topic_text AND b.vote_choice != 'agree') AS against
            FROM {TABLE_TOPIC_VOTES} tv
            WHERE tv.status != 'pending'
            ORDER BY tv.created_at DESC
            LIMIT 20
        """)
        if not history.empty:
            for _, h in history.iterrows():
                icon = "✅" if h["status"] == "passed" else "❌"
                date_str = str(h["created_at"])[:10] if h["created_at"] else ""
                cat = h.get("category") or ""
                st.caption(f"{icon} {h['topic_text']}　｜　{cat}　｜　同意：{h['agree']} ／ 不同意：{h['against']} ／ 門檻：{h['approval_threshold']}　｜　{date_str}")
        else:
            st.caption("暫無記錄")


elif selected_tab == "depose_vote":
    st.subheader("罷免投票")
    st.caption("當同意罷免票數達罷免門檻，且同意票多於不同意票時，系統會自動刪除辯題。")
    st.caption("當不同意票數達罷免門檻，且不同意票多於同意票時，系統會自動否決罷免動議。")

    render_refresh_button("refresh_vote_tab3")

    conn = get_connection()
    df_depose = conn.query(
        f"""
        SELECT
            topic_text,
            proposer_user_id,
            status,
            removal_reasons,
            created_at,
            deadline_date,
            approval_threshold
        FROM {TABLE_TOPIC_REMOVAL_VOTES}
        WHERE status = 'pending'
        ORDER BY created_at DESC
        """,
        ttl=5,
    )
    depose_ballots = conn.query(
        f"SELECT topic_text, user_id, vote_choice FROM {TABLE_TOPIC_REMOVAL_VOTE_BALLOTS}",
        ttl=0,
    )
    agree_depose, against_depose = {}, {}
    if not depose_ballots.empty:
        for _, b in depose_ballots.iterrows():
            t = b["topic_text"]
            if b["vote_choice"] == "agree":
                agree_depose.setdefault(t, []).append(b["user_id"])
            else:
                against_depose.setdefault(t, []).append(b["user_id"])
    vote_data = []
    for _, row in df_depose.iterrows():
        row_dict = row.to_dict()
        t = row_dict["topic_text"]
        row_dict["agree_users"] = agree_depose.get(t, [])
        row_dict["against_users"] = against_depose.get(t, [])
        vote_data.append(row_dict)

    topics_meta_df = conn.query(f"SELECT topic_text, category, difficulty FROM {TABLE_TOPICS}", ttl=5)
    topic_meta = {r["topic_text"]: (r.get("category"), r.get("difficulty")) for _, r in topics_meta_df.iterrows()}

    _tr_comment_counts = _get_comment_counts("topic_removal") if vote_data else {}

    if not vote_data:
        st.info("目前沒有待罷免的辯題。")
    else:
        for i, row in enumerate(vote_data):
            topic = row["topic_text"]
            mover = row["proposer_user_id"]
            proposal_reasons = parse_reason_list(row.get("removal_reasons", ""))

            agree_list = row["agree_users"]
            against_list = row["against_users"]

            agree_count = len(agree_list)
            against_count = len(against_list)
            row_depose_threshold = int(row.get("approval_threshold") or DEPOSE_THRESHOLD)

            depose_deadline_passed, depose_deadline_str = parse_deadline_row(row)

            # Auto-dismiss expired motions before rendering the card (avoids flash)
            # Note: expired depose motions are hard-deleted (no audit trail needed).
            # Topic vote expiries use UPDATE status='rejected' to preserve the rejection log in tab2.
            if depose_deadline_passed:
                st.warning(f"罷免動議「{topic}」投票期限（{depose_deadline_str} 23:59）已過，未達罷免標準，動議自動取消。")
                updated = execute_query_count(
                    f"UPDATE {TABLE_TOPIC_REMOVAL_VOTES} SET status = 'rejected' WHERE topic_text = :topic_text AND status = 'pending'",
                    {"topic_text": topic},
                )
                if updated:
                    notify_vote_event(
                        "罷免動議逾期",
                        f"「{topic}」的罷免動議未達標準，已自動取消。",
                        tag=f"topic-removal-expired-{topic}",
                    )
                clear_caches()
                st.rerun()

            with st.container(border=True):
                st.markdown(f"#### {topic}")
                meta = topic_meta.get(topic, (None, None))
                depose_cat = meta[0] or "—"
                depose_diff = meta[1]
                depose_diff_label = DIFFICULTY_OPTIONS.get(int(depose_diff), "—") if depose_diff else "—"
                depose_deadline_display = f" ｜ 截止：{depose_deadline_str} 23:59" if depose_deadline_str else ""
                st.caption(f"🏷️ {depose_cat} ｜ {depose_diff_label}{depose_deadline_display}")
                st.caption(f"提出者：{mover} ｜ 罷免門檻：{row_depose_threshold} 票")

                agree_progress = min(agree_count / row_depose_threshold, 1.0)
                against_progress = min(against_count / row_depose_threshold, 1.0)

                st.progress(agree_progress, text=f"同意罷免進度：{agree_count} / {row_depose_threshold}")
                st.progress(against_progress, text=f"不同意罷免進度：{against_count} / {row_depose_threshold}")

                btn_col1, btn_col2 = st.columns(2)
                render_vote_buttons(
                    i, user_id, topic, agree_list, against_list, against_reason_map={},
                    table=TABLE_TOPIC_REMOVAL_VOTES, agree_label="同意罷免", against_label="不同意罷免",
                    after_vote_fn=_after_vote_light, col2=btn_col1, col3=btn_col2,
                    agree_switch_toast="已轉投同意罷免票！"
                )

            if proposal_reasons:
                with st.expander(f"查看「{topic}」提出原因", expanded=False):
                    st.caption("；".join(proposal_reasons))

            render_discussion("topic_removal", topic, user_id, i, _tr_comment_counts.get(topic, 0))

            check_vote_resolution(agree_count, against_count, row_depose_threshold, topic, agree_list, against_list,
                                   mode="depose")



elif selected_tab == "bank_analysis":
    st.subheader("分析現有辯題庫")
    st.caption("由 AI 分析辯題庫嘅類別／難度分佈、題目質素，並提出未來方向同即時可做建議。分析結果會儲存並與所有委員共享。")

    render_refresh_button("refresh_vote_bank_analysis")

    stale_topics = _find_stale_removed_topics()
    if stale_topics:
        st.warning(
            "⚠️ 偵測到以下題目已被罷免通過，但仍留喺辯題庫，建議手動核實並刪除：\n"
            + "\n".join(f"- {t}" for t in stale_topics)
        )

    saved_analysis, analysed_at, analysed_by = load_bank_analysis()
    if analysed_at:
        by_txt = f"（由 {analysed_by} 執行）" if analysed_by else ""
        st.caption(f"🕒 上次分析時間：{analysed_at}{by_txt}")
    else:
        st.caption("🕒 尚未有分析紀錄。")

    if st.button("AI 分析辯題庫", key="ai_analyze_bank"):
        with st.spinner("AI 正在分析辯題庫，請稍候⋯"):
            bank_analysis, _usage = ai_analyze_topic_bank()
        if is_successful_ai_result(bank_analysis):
            save_bank_analysis(bank_analysis, user_id)
            queue_toast("已更新辯題庫分析", icon="☑️")
            st.rerun()
        else:
            st.error(bank_analysis)

    if saved_analysis:
        with st.expander("AI 分析結果", expanded=True):
            st.markdown(saved_analysis)


elif selected_tab == "member_stats":
    st.subheader("成員參與率")
    st.caption("計算辯題投票及罷免投票的整體參與情況。活躍成員標準：整體投票率 ≥ 40% 且 最近10次投票至少參與3次。")

    if st.button("🔄 重新整理", key="refresh_member_stats"):
        clear_caches()

    member_stats, total_topic_votes = get_member_participation_stats()
    num_of_active, _ = get_active_user_count()
    st.caption(f"辯題投票 + 罷免投票總數：{total_topic_votes} 個")
    st.caption(f"目前活躍成員：{num_of_active} 人")

    if member_stats and user_id != "admin":
        current_user_stats = next(
            (s for s in member_stats if str(s["用戶"]).strip() == str(user_id).strip()),
            None
        )
        if current_user_stats:
            st.subheader("我的參與情況")
            row1_c1, row1_c2 = st.columns(2)
            row1_c1.metric("整體投票率", current_user_stats["整體投票率"])
            row1_c2.metric("最近10次參與", f"{current_user_stats['最近10次參與']} / 10")
            row2_c1, row2_c2 = st.columns(2)
            row2_c1.metric("投票同意率", current_user_stats["投票同意率"])
            row2_c2.metric("活躍狀態", current_user_stats["活躍狀態"])
            st.divider()

    if member_stats:
        st.dataframe(member_stats, use_container_width=True, hide_index=True)
    else:
        st.info("暫無成員資料。")


elif selected_tab == "account":
    st.subheader("帳戶管理")

    with st.expander("通知設定", expanded=True):
        st.caption("啟用後，新議案及投票結果可在 PWA 未開啟時推送到你的裝置。iPhone / iPad 需先將系統加入主畫面。")
        render_push_notification_settings()

    with st.expander("更改密碼", expanded=False):
        with st.form("change_user_password"):
            current_pw = st.text_input("目前密碼", type="password")
            new_pw = st.text_input("新密碼", type="password")
            confirm_pw = st.text_input("確認新密碼", type="password")
            submit_new_pw = st.form_submit_button("確認更改")

        if submit_new_pw:
            if not current_pw.strip():
                st.warning("請輸入目前密碼！")
            elif not new_pw.strip():
                st.warning("請輸入新密碼！")
            elif new_pw.strip() != confirm_pw.strip():
                st.error("兩次輸入的新密碼不一致。")
            else:
                acc_row = query_params(
                    f"SELECT password_hash FROM {TABLE_ACCOUNTS} WHERE user_id = :user_id",
                    {"user_id": user_id},
                )
                if acc_row.empty or not _verify_config_password(current_pw.strip(), str(acc_row.iloc[0]["password_hash"])):
                    st.error("目前密碼錯誤。")
                else:
                    try:
                        execute_query(
                            f"UPDATE {TABLE_ACCOUNTS} SET password_hash = :password_hash WHERE user_id = :user_id",
                            {"password_hash": hash_password(new_pw.strip()), "user_id": user_id},
                        )
                        st.success("帳戶密碼已更改！下次登入請使用新密碼！")
                    except Exception as e:
                        st.error(f"無法連接至資料庫：{e}")
    
    st.divider()
    if st.button("登出", type="primary"):
        st.session_state["committee_user"] = None
        del_cookie(cm, "committee_user")
        render_committee_auth_bridge(clear=True)
        st.rerun()
