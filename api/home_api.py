"""Public, read-only endpoints for the direct HTML home page."""

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/home", tags=["home"])


def _db():
    from deploy.proxy import get_vote_db
    return get_vote_db()


@router.get("/data")
def data():
    from core import home_logic as logic
    try:
        return logic.home_data(db=_db())
    except Exception as exc:
        raise HTTPException(503, f"連線錯誤: {exc}") from exc


@router.post("/status-check")
def status_check():
    from core import home_logic as logic
    return logic.run_status_checks(db=_db())


@router.get("/manual")
def manual(role: str = Query("評判")):
    from core import home_logic as logic
    return {"markdown": logic.manual_for_role(role)}


@router.get("/rules")
def rules(role: str = Query("評判")):
    from core import home_logic as logic
    return {"markdown": logic.rules_for_role(role)}
