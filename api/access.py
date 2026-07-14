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
