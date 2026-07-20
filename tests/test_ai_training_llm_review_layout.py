"""Contracts for the recording-parity LLM review workspace."""

from pathlib import Path

from api import ai_training_api


ROOT = Path(__file__).resolve().parents[1]


def test_llm_review_tab_matches_recording_review_structure():
    page = (ROOT / "frontend" / "ai_training" / "index.html").read_text()
    script = (ROOT / "frontend" / "ai_training" / "app.js").read_text()

    section = page.split('<section id="llm-review"', 1)[1].split(
        '</section>\n        </section>', 1
    )[0]
    assert "LLM 資料狀態統計" in section
    assert '<select id="llmFilter">' in section
    assert '<select id="llmSubmitterFilter">' in section
    assert '<option value="">全部提交者</option>' in section
    assert 'id="adminLlm"' in section
    assert "LLM 審核準則（管理員必讀）" in section
    assert 'id="llmExport"' in section

    assert "function loadLlmSubmissions" in script
    assert "/api/ai-training/admin/submissions?status=" in script
    assert "stats.llm_submitters || []" in script
    assert '$("llmFilter").onchange' in script
    assert '$("llmSubmitterFilter").onchange' in script
    assert "syncLlmExport();" in script
    assert "沒有 AI 預檢 JSON" in script


def test_admin_submission_filters_are_parameterized_and_bounded(monkeypatch):
    calls = []

    class Frame:
        def to_dict(self, *, orient):
            assert orient == "records"
            return []

    class Db:
        def query(self, sql, params):
            calls.append((sql, dict(params)))
            return Frame()

    db = Db()
    monkeypatch.setattr(ai_training_api, "_admin", lambda _request: ("admin", db))
    monkeypatch.setattr(ai_training_api, "scalar_count", lambda *_args, **_kwargs: 0)

    result = ai_training_api.admin_submissions(
        None, page=2, status="pending", submitter=" member "
    )

    sql, params = calls[0]
    assert "status=:status" in sql
    assert "submitted_by=:submitter" in sql
    assert params == {
        "status": "pending",
        "submitter": "member",
        "limit": ai_training_api.AI_TRAINING_ADMIN_PAGE_SIZE,
        "offset": ai_training_api.AI_TRAINING_ADMIN_PAGE_SIZE,
    }
    assert result["page"] == 2
    assert result["page_size"] == ai_training_api.AI_TRAINING_ADMIN_PAGE_SIZE


def test_llm_export_can_be_scoped_to_one_submitter(monkeypatch):
    calls = []

    class Frame:
        def __init__(self, rows):
            self.rows = rows

        def to_dict(self, *, orient):
            assert orient == "records"
            return self.rows

    class Db:
        def query(self, sql, params):
            calls.append((sql, dict(params)))
            if "AS row_count" in sql:
                return Frame([{"row_count": 0, "payload_bytes": 0}])
            return Frame([])

    db = Db()
    monkeypatch.setattr(ai_training_api, "_admin", lambda _request: ("admin", db))

    response = ai_training_api.export_llm(None, submitter=" member ")

    assert all("submitted_by=:submitter" in sql for sql, _params in calls)
    assert all(params["submitter"] == "member" for _sql, params in calls)
    assert response.media_type.startswith("application/x-ndjson")
