"""Central account identities and page-access policy.

Special service accounts are deliberately identified by account id instead of
the mutable ``account_status`` column.  Keep every page rule here so adding a
new system account or changing what it may open does not require hunting for
string comparisons across API modules and front-end code.
"""

from dataclasses import dataclass
from typing import Iterable


ADMIN_ACCOUNT_ID = "admin"
DEVELOPER_ACCOUNT_ID = "developer"
KIOSK_ACCOUNT_ID = "kiosk"
AI_COMMENT_ACCOUNT_ID = "Gemini"

# These identities are infrastructure/service accounts, not voting committee
# members.  The tuple keeps a stable display/SQL order and preserves Gemini's
# canonical mixed-case id used by the motion-comments foreign key.
NON_MEMBER_ACCOUNT_IDS = (
    ADMIN_ACCOUNT_ID,
    DEVELOPER_ACCOUNT_ID,
    KIOSK_ACCOUNT_ID,
    AI_COMMENT_ACCOUNT_ID,
)

# Developer account-management may rotate/disable the kiosk credential, but
# must not mutate these privileged or pseudo-user identities through the normal
# committee-account controls.
PROTECTED_ACCOUNT_IDS = (
    ADMIN_ACCOUNT_ID,
    DEVELOPER_ACCOUNT_ID,
    AI_COMMENT_ACCOUNT_ID,
)


def normalize_account_id(user_id: object) -> str:
    """Return the comparison form for an account id (case-insensitive)."""
    return str(user_id or "").strip().casefold()


def _normalized(values: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_account_id(value) for value in values)


NON_MEMBER_ACCOUNT_KEYS = _normalized(NON_MEMBER_ACCOUNT_IDS)
NON_MEMBER_ACCOUNT_DB_KEYS = tuple(
    dict.fromkeys(normalize_account_id(value) for value in NON_MEMBER_ACCOUNT_IDS)
)
PROTECTED_ACCOUNT_KEYS = _normalized(PROTECTED_ACCOUNT_IDS)


@dataclass(frozen=True)
class PageAccessPolicy:
    """One centrally managed rule; an allow-list takes precedence."""

    allowed_accounts: frozenset[str] | None = None
    denied_accounts: frozenset[str] = frozenset()
    denial_message: str = "此帳戶不能使用此頁面。"


_MEMBER_ONLY = NON_MEMBER_ACCOUNT_KEYS
_AI_COACH_DENIED = _normalized((
    ADMIN_ACCOUNT_ID,
    DEVELOPER_ACCOUNT_ID,
    AI_COMMENT_ACCOUNT_ID,
))

# Public pages do not call this policy.  Authenticated surfaces must use one of
# these explicit contexts; an unknown context fails closed in
# ``account_can_access`` so a typo cannot silently open a page.
PAGE_ACCESS_POLICIES = {
    "committee_login": PageAccessPolicy(
        denied_accounts=_MEMBER_ONLY,
        denial_message="請使用內部委員會成員帳戶登入。",
    ),
    "vote": PageAccessPolicy(
        denied_accounts=_MEMBER_ONLY,
        denial_message="此系統帳戶不能使用投票頁面。",
    ),
    "member_profile": PageAccessPolicy(
        denied_accounts=_MEMBER_ONLY,
        denial_message="此系統帳戶不能使用委員帳戶功能。",
    ),
    "funds": PageAccessPolicy(
        denied_accounts=_MEMBER_ONLY,
        denial_message="此系統帳戶不能使用基金頁面。",
    ),
    "bug_report": PageAccessPolicy(denied_accounts=_MEMBER_ONLY),
    "video_replay": PageAccessPolicy(denied_accounts=_MEMBER_ONLY),
    "match_photos": PageAccessPolicy(denied_accounts=_MEMBER_ONLY),
    "ai_training": PageAccessPolicy(denied_accounts=_MEMBER_ONLY),
    # The dedicated appliance account may use AI Coach, but admin/developer and
    # the Gemini comment pseudo-account may not impersonate a committee member.
    "ai_coach": PageAccessPolicy(
        denied_accounts=_AI_COACH_DENIED,
        denial_message="此系統帳戶不能使用 AI 練習頁面。",
    ),
    "ai_room": PageAccessPolicy(
        denied_accounts=_AI_COACH_DENIED,
        denial_message="此系統帳戶不能加入 AI 練習房間。",
    ),
    "tts": PageAccessPolicy(
        denied_accounts=_AI_COACH_DENIED,
        denial_message="此系統帳戶不能使用語音合成功能。",
    ),
    "projector": PageAccessPolicy(
        denied_accounts=_MEMBER_ONLY,
        denial_message="此系統帳戶不能控制比賽投影畫面。",
    ),
    "kiosk": PageAccessPolicy(
        allowed_accounts=_normalized((KIOSK_ACCOUNT_ID,)),
        denial_message="此頁面只限 kiosk 帳戶使用。",
    ),
}


def account_can_access(user_id: object, page: str) -> bool:
    """Whether ``user_id`` may use an authenticated page context.

    Empty identities and unknown page names are denied.  Account comparison is
    case-insensitive so a differently-cased service id cannot bypass a rule.
    """
    key = normalize_account_id(user_id)
    policy = PAGE_ACCESS_POLICIES.get(str(page or "").strip())
    if not key or policy is None:
        return False
    if policy.allowed_accounts is not None:
        return key in policy.allowed_accounts
    return key not in policy.denied_accounts


def access_denial_message(page: str) -> str:
    policy = PAGE_ACCESS_POLICIES.get(str(page or "").strip())
    return policy.denial_message if policy else "此帳戶不能使用此頁面。"


def is_non_member_account(user_id: object) -> bool:
    return normalize_account_id(user_id) in NON_MEMBER_ACCOUNT_KEYS


def is_protected_account(user_id: object) -> bool:
    return normalize_account_id(user_id) in PROTECTED_ACCOUNT_KEYS


def account_id_can_be_created(user_id: object) -> bool:
    """Whether the developer account form may create this exact id.

    Normal member ids are allowed.  The canonical lower-case kiosk identity is
    the sole service account provisioned through that form; differently-cased
    spellings are rejected so they cannot become policy-bypass lookalikes.
    """
    value = str(user_id or "").strip()
    return bool(value) and (
        value == KIOSK_ACCOUNT_ID or normalize_account_id(value) not in NON_MEMBER_ACCOUNT_KEYS
    )


def sql_account_id_literals(account_ids: Iterable[str]) -> str:
    """Trusted SQL literals for static DDL such as the activity view.

    Runtime queries should bind parameters.  This helper exists because CREATE
    VIEW statements cannot carry bind parameters; values still come solely
    from the fixed constants above and are defensively escaped.
    """
    return ", ".join(
        "'" + str(account_id).replace("'", "''") + "'"
        for account_id in account_ids
    )
