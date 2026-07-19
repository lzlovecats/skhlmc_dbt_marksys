"""Shared HTTP enforcement for the central account/page policy."""

from fastapi import HTTPException, Request

from account_access import account_can_access, access_denial_message


def require_page_user(request: Request, page: str) -> str:
    """Return the signed committee identity when it may use ``page``."""
    # Lazy import avoids a cycle while deploy.proxy is registering API routers.
    from deploy.proxy import _require_committee_user

    user_id = _require_committee_user(request)
    if not account_can_access(user_id, page):
        raise HTTPException(403, access_denial_message(page))
    return user_id


def has_developer_session(request: Request) -> bool:
    """Whether this request carries a live Developer settings session."""
    if not getattr(request, "cookies", None):
        return False
    from api.admin_console_api import developer_session_active

    return developer_session_active(request)


def interactive_features_suspension(request: Request) -> dict:
    """Return the shared Vote/AI suspension state with Developer bypass."""
    if has_developer_session(request):
        return {
            "configured": False,
            "scheduled": False,
            "active": False,
            "message": "",
            "retry_after_seconds": 0,
            "developer_bypass": True,
        }
    from core.feature_suspension import suspension_status
    from deploy.proxy import get_vote_db

    try:
        db = get_vote_db()
    except Exception:
        # The suspension schedule is deliberately fail-open.  Database
        # acquisition belongs inside that boundary as well as config reads.
        db = None
    return suspension_status(db)


def require_interactive_features_available(request: Request) -> dict:
    """Fail new Vote/AI operations during the configured suspension window."""
    status = interactive_features_suspension(request)
    if status.get("active"):
        retry_after = max(1, int(status.get("retry_after_seconds") or 1))
        raise HTTPException(
            503,
            status.get("message") or "Vote 及 AI 辯論易暫停使用。",
            headers={"Retry-After": str(retry_after)},
        )
    return status


def require_page_user_or_developer(request: Request, page: str) -> str:
    """Allow the Developer management identity or a normal page member."""
    if has_developer_session(request):
        return "developer"
    return require_page_user(request, page)


def require_competition_staff(request: Request) -> str:
    """Require the same organiser session used by 主席主持易.

    Competition-day controls deliberately use the dedicated organiser gate,
    rather than a committee account cookie, so every person who can operate
    主席主持易 receives the same bounded competition-control capability.
    Keeping this check here prevents projector/recording endpoints from each
    growing a subtly different authentication rule.
    """
    if has_developer_session(request):
        return "developer"

    from deploy.proxy import _verify_registration_admin_token

    cookies = getattr(request, "cookies", {}) or {}
    token = cookies.get("registration_admin") or ""
    if not _verify_registration_admin_token(token):
        raise HTTPException(401, "未登入賽會人員帳戶，請先到主席主持易登入。")
    return "registration_admin"
