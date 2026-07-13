"""Streamlit-free Web Push sender.

Faithful port of ``functions.notify_committee_vote_event`` /
``send_push_notification`` that takes the DB executor and VAPID config as
arguments instead of reading ``st.secrets`` / ``st.session_state``. Lets the
proxy (uvicorn) send committee push notifications — e.g. after an API-triggered
vote resolution — without importing Streamlit.

``vapid`` is a dict: {"public_key", "private_key", "subject"}.
"""

import json
import os
import re

from schema import TABLE_PUSH_SUBSCRIPTIONS
from system_limits import PUSH_RECIPIENT_LIMIT



def push_title_with_emoji(title):
    title = str(title or "").strip()
    if not title:
        return "🔔 聖呂中辯"
    if re.match(r"^[^\w\s]", title):
        return title
    emoji_map = [
        ("新留言", "💬"),
        ("新辯題", "📝"),
        ("辯題投票通過", "✅"),
        ("辯題投票否決", "❌"),
        ("辯題投票逾期", "⏰"),
        ("新罷免動議", "✂️"),
        ("罷免動議通過", "🗑️"),
        ("罷免動議否決", "🛡️"),
        ("罷免動議逾期", "⏰"),
    ]
    for prefix, emoji in emoji_map:
        if title.startswith(prefix):
            return f"{emoji} {title}"
    return f"🔔 {title}"


def send_web_push(subscription, title, body, vapid, url="/vote", tag=None):
    """Send one push. Returns (ok: bool, error: str)."""
    if not vapid:
        return False, "VAPID keys are not configured"
    try:
        from pywebpush import WebPushException, webpush
    except Exception as e:  # pragma: no cover - depends on runtime deps
        return False, f"pywebpush unavailable: {e}"

    payload = {"title": push_title_with_emoji(title), "body": body, "url": url}
    if tag:
        payload["tag"] = tag

    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=vapid["private_key"],
            vapid_claims={"sub": vapid["subject"]},
            headers={"Urgency": "high"},
            ttl=60 * 60 * 24,
            timeout=(5, 10),
        )
        return True, ""
    except WebPushException as e:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        return False, f"{status_code or ''} {e}".strip()
    except Exception as e:
        return False, str(e)


def notify_committee(db, vapid, title, body, exclude_user=None, target_user=None,
                     tag=None, url="/vote", send_fn=None):
    """Send ``title``/``body`` to matching active push subscriptions.

    Prunes subscriptions that return 404/410 (gone). Returns the number sent.
    ``send_fn`` is injectable for testing; defaults to :func:`send_web_push`.
    """
    if not vapid:
        return 0
    send = send_fn or send_web_push

    params = {}
    where = "WHERE is_active = TRUE"
    if exclude_user:
        where += " AND user_id != :exclude_user"
        params["exclude_user"] = exclude_user
    if target_user:
        where += " AND user_id = :target_user"
        params["target_user"] = target_user

    try:
        rows = db.query(
            f"SELECT endpoint,user_id,subscription_json FROM {TABLE_PUSH_SUBSCRIPTIONS} {where} "
            "ORDER BY updated_at DESC LIMIT :recipient_limit",
            {**params, "recipient_limit": PUSH_RECIPIENT_LIMIT},
        )
    except Exception:
        return 0
    if rows.empty:
        return 0

    sent = 0
    for _, row in rows.iterrows():
        endpoint = row["endpoint"]
        try:
            subscription = json.loads(row["subscription_json"])
        except Exception as e:
            db.execute(
                f"UPDATE {TABLE_PUSH_SUBSCRIPTIONS} SET is_active = FALSE, last_error = :error WHERE endpoint = :endpoint",
                {"endpoint": endpoint, "error": f"Invalid subscription JSON: {e}"},
            )
            continue

        ok, error = send(subscription, title, body, vapid, url=url, tag=tag)
        if ok:
            sent += 1
            db.execute(
                f"UPDATE {TABLE_PUSH_SUBSCRIPTIONS} SET last_error = NULL WHERE endpoint = :endpoint",
                {"endpoint": endpoint},
            )
        else:
            should_disable = error.startswith("404") or error.startswith("410")
            db.execute(
                f"UPDATE {TABLE_PUSH_SUBSCRIPTIONS} "
                "SET is_active = CASE WHEN :disable THEN FALSE ELSE is_active END, last_error = :error "
                "WHERE endpoint = :endpoint",
                {"endpoint": endpoint, "disable": should_disable, "error": error[:1000]},
            )

    return sent
