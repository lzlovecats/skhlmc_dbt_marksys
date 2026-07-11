"""Committee-authenticated JSON endpoints for the HTML bug-report page."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/bug-report", tags=["bug-report"])


class BugReportBody(BaseModel):
    affected_page: str
    device_info: str = ""
    reproduction_steps: str
    expected_result: str
    actual_result: str
    extra_notes: str = ""


def _context(request: Request):
    from deploy.proxy import _require_committee_user, get_vote_db
    return _require_committee_user(request), get_vote_db()


@router.get("/data")
def data(request: Request):
    from core import bug_report_logic as logic
    user_id, db = _context(request)
    return {
        "user_id": user_id,
        "page_options": logic.PAGE_OPTIONS,
        "status_labels": logic.STATUS_LABELS,
        "reports": logic.reports_for_user(user_id, db=db),
    }


@router.post("/submit")
def submit(body: BugReportBody, request: Request):
    from core import bug_report_logic as logic
    user_id, db = _context(request)
    return logic.submit_report(user_id, body.affected_page, body.device_info,
                               body.reproduction_steps, body.expected_result,
                               body.actual_result, body.extra_notes, db=db)
