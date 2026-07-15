"""Bounded Web Push sender.

The caller supplies the DB executor and VAPID config; the sender can therefore
be used by any domain event without global request state.

``vapid`` is a dict: {"public_key", "private_key", "subject"}.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading

from account_access import NON_MEMBER_ACCOUNT_DB_KEYS, sql_account_id_literals
from schema import TABLE_ACCOUNTS, TABLE_PUSH_SUBSCRIPTIONS
from system_limits import PUSH_RECIPIENT_LIMIT, PUSH_SEND_CONCURRENCY


_PUSH_EXECUTOR = ThreadPoolExecutor(
    max_workers=PUSH_SEND_CONCURRENCY, thread_name_prefix="web-push"
)
_PUSH_SUBMIT_SLOTS = threading.BoundedSemaphore(PUSH_SEND_CONCURRENCY)


def _submit_push(deliver, item):
    """Submit without allowing the process-global work queue to grow unbounded."""
    _PUSH_SUBMIT_SLOTS.acquire()

    def run():
        try:
            return deliver(item)
        finally:
            _PUSH_SUBMIT_SLOTS.release()

    try:
        return _PUSH_EXECUTOR.submit(run)
    except BaseException:
        _PUSH_SUBMIT_SLOTS.release()
        raise


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
                     tag=None, url="/vote", send_fn=None,
                     committee_only=False):
    """Send ``title``/``body`` to matching active push subscriptions.

    Prunes subscriptions that return 404/410 (gone). Returns the number sent.
    ``send_fn`` is injectable for testing; defaults to :func:`send_web_push`.
    """
    if not vapid:
        return 0
    send = send_fn or send_web_push

    params = {}
    subscription_table = f"{TABLE_PUSH_SUBSCRIPTIONS} p"
    select_columns = "p.endpoint,p.user_id,p.subscription_json"
    where = "WHERE p.is_active = TRUE"
    if committee_only:
        excluded = sql_account_id_literals((*NON_MEMBER_ACCOUNT_DB_KEYS, ""))
        subscription_table += f" JOIN {TABLE_ACCOUNTS} a ON a.user_id=p.user_id"
        where += (
            " AND COALESCE(a.account_disabled,FALSE)=FALSE"
            " AND a.account_status IN ('active','admin')"
            f" AND LOWER(a.user_id) NOT IN ({excluded})"
        )
    if exclude_user:
        where += " AND p.user_id != :exclude_user"
        params["exclude_user"] = exclude_user
    if target_user:
        where += " AND p.user_id = :target_user"
        params["target_user"] = target_user

    try:
        rows = db.query(
            f"SELECT {select_columns} FROM {subscription_table} {where} "
            "ORDER BY p.updated_at DESC LIMIT :recipient_limit",
            {**params, "recipient_limit": PUSH_RECIPIENT_LIMIT},
        )
    except Exception:
        return 0
    if rows.empty:
        return 0

    deliveries = []
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
        deliveries.append((endpoint, subscription))

    def deliver(item):
        endpoint, subscription = item
        try:
            ok, error = send(subscription, title, body, vapid, url=url, tag=tag)
            return endpoint, bool(ok), str(error or "")
        except Exception as exc:
            return endpoint, False, str(exc)

    sent = 0
    # Every caller shares one pool. Submission slots also bound its work queue,
    # so overlapping notifications cannot multiply threads or retain an
    # unbounded number of payloads while providers are slow.
    futures = [_submit_push(deliver, item) for item in deliveries]
    for future in futures:
        endpoint, ok, error = future.result()
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
