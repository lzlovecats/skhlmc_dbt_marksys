"""Committee-authenticated JSON endpoints for the HTML bug-report page."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/bug-report", tags=["bug-report"])


class BugReportBody(BaseModel):
    affected_page: str = Field(min_length=1, max_length=120)
    device_info: str = Field(default="", max_length=1000)
    reproduction_steps: str = Field(min_length=1, max_length=5000)
    expected_result: str = Field(min_length=1, max_length=3000)
    actual_result: str = Field(min_length=1, max_length=5000)
    extra_notes: str = Field(default="", max_length=3000)


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
