"""Contracts for the authenticated committee RAG and SFT submission link."""

from pathlib import Path

import pytest
from fastapi import HTTPException

from api import ai_training_api
from core.config_store import CONFIG_SPECS


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "frontend" / "ai_training" / "index.html").read_text("utf-8")
SCRIPT = (ROOT / "frontend" / "ai_training" / "app.js").read_text("utf-8")
MANUAL = (ROOT / "assets" / "user_manual.md").read_text("utf-8")


def test_rag_sft_submission_is_top_level_but_settings_are_in_admin_tab():
    assert 'data-pane="rag-sft-submit"' in PAGE
    assert '<section id="rag-sft-submit" class="pane">' in PAGE
    assert "提交 RAG 及 SFT 資料" in PAGE
    assert 'id="ragSftSubmissionLink"' in PAGE
    assert 'target="_blank"' in PAGE
    assert 'rel="noopener noreferrer"' in PAGE
    assert 'id="ragSftSubmissionAdminForm"' in PAGE
    assert 'id="ragSftSubmissionUrl"' in PAGE
    assert 'id="clearRagSftSubmissionUrl"' in PAGE

    submission_section = PAGE.split(
        '<section id="rag-sft-submit" class="pane">', 1
    )[1].split('<section id="admin" class="pane">', 1)[0]
    admin_section = PAGE.split('<section id="admin" class="pane">', 1)[1]
    assert 'id="ragSftSubmissionAdminForm"' not in submission_section
    assert 'id="ragSftSubmissionAdminForm"' in admin_section

    # The edit URL must arrive only after /api/ai-training/data authenticates
    # the committee member. It must never be embedded in the public page.
    assert "https://docs.google.com/document/" not in PAGE
    assert "data.rag_sft_submission_google_doc_url" in SCRIPT
    assert (
        '$("ragSftSubmissionAdminForm").classList.toggle("hidden", !data.is_admin)'
        in SCRIPT
    )
    assert '"/api/ai-training/rag-sft-submission-link"' in SCRIPT
    assert "提交 RAG 及 SFT 資料" in MANUAL
    assert "「管理員」分頁" in MANUAL
    assert "只可由 AI 訓練管理員更新或清除" in MANUAL
    assert "不會自動成為 RAG bundle 或 SFT 訓練集" in MANUAL


def test_rag_sft_submission_link_is_registered_as_sensitive_typed_config():
    spec = CONFIG_SPECS["rag_sft_submission_google_doc_url"]
    assert spec.namespace == "ai"
    assert spec.value_type == "string"
    assert spec.secret is True


@pytest.mark.parametrize(
    "url",
    (
        "http://docs.google.com/document/d/abc/edit",
        "https://drive.google.com/file/d/abc/view",
        "https://docs.google.com/spreadsheets/d/abc/edit",
        "https://docs.google.com/document/u/0/",
        "https://docs.google.com.evil.test/document/d/abc/edit",
        "https://user:pass@docs.google.com/document/d/abc/edit",
        "https://docs.google.com:444/document/d/abc/edit",
    ),
)
def test_rag_sft_submission_link_rejects_non_google_doc_urls(url):
    with pytest.raises(HTTPException) as raised:
        ai_training_api._validated_rag_sft_submission_google_doc_url(url)
    assert raised.value.status_code == 400


def test_rag_sft_submission_link_accepts_google_doc_and_can_be_cleared():
    url = "https://docs.google.com/document/d/Abc_123-xyz/edit?usp=sharing"
    assert ai_training_api._validated_rag_sft_submission_google_doc_url(url) == url
    assert ai_training_api._validated_rag_sft_submission_google_doc_url("") == ""


def test_only_ai_training_admin_can_update_rag_sft_submission_link(monkeypatch):
    saved = {}
    audit = {}

    class Transaction:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class Db:
        def transaction(self):
            return Transaction()

    db = Db()
    monkeypatch.setattr(
        ai_training_api, "_admin", lambda _request: ("ai-admin", db)
    )
    monkeypatch.setattr(
        ai_training_api,
        "set_configs_on_connection",
        lambda connection, values: saved.update(values),
    )
    monkeypatch.setattr(
        ai_training_api,
        "_audit",
        lambda _db, actor, action, target_type, target_id="", details=None, **kwargs: audit.update(
            {
                "actor": actor,
                "action": action,
                "target_type": target_type,
                "details": details,
                "connection": kwargs.get("conn"),
            }
        ),
    )
    monkeypatch.setattr(ai_training_api, "_prune_audit", lambda _db: None)

    url = "https://docs.google.com/document/d/Abc_123-xyz/edit"
    result = ai_training_api.save_rag_sft_submission_link(
        ai_training_api.RagSftSubmissionLinkBody(url=url), None
    )

    assert result == {"ok": True, "url": url}
    assert saved == {"rag_sft_submission_google_doc_url": url}
    assert audit["actor"] == "ai-admin"
    assert audit["action"] == "rag_sft_submission_link_updated"
    assert audit["target_type"] == "rag_sft_submission_link"
    assert audit["details"] == {"configured": True}
    assert audit["connection"] is not None


def test_non_admin_rag_sft_submission_update_fails_before_write(monkeypatch):
    def denied(_request):
        raise HTTPException(403, "只有管理員可執行此操作")

    monkeypatch.setattr(ai_training_api, "_admin", denied)

    with pytest.raises(HTTPException) as raised:
        ai_training_api.save_rag_sft_submission_link(
            ai_training_api.RagSftSubmissionLinkBody(url=""), None
        )
    assert raised.value.status_code == 403


def test_authenticated_training_data_returns_only_valid_configured_link(
    monkeypatch,
):
    class Frame:
        empty = True

        def to_dict(self, *, orient):
            assert orient == "records"
            return []

    class Db:
        def query(self, _sql, _params=None):
            return Frame()

    url = "https://docs.google.com/document/d/Abc_123-xyz/edit"
    monkeypatch.setattr(ai_training_api, "_ctx", lambda _request: ("member", Db()))
    monkeypatch.setattr(ai_training_api, "_users", lambda _db, _key: [])
    monkeypatch.setattr(ai_training_api, "_is_admin", lambda _db, _user: False)
    monkeypatch.setattr(
        ai_training_api, "_has_active_voice_consent", lambda _db, _user: False
    )
    monkeypatch.setattr(
        ai_training_api,
        "get_config",
        lambda _db, key, default: (
            url
            if key == ai_training_api.RAG_SFT_SUBMISSION_GOOGLE_DOC_CONFIG_KEY
            else default
        ),
    )

    from core import r2_storage
    import deploy.proxy as proxy

    monkeypatch.setattr(r2_storage, "configured", lambda: False)
    monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_kwargs: {})

    result = ai_training_api.data(None)

    assert result["rag_sft_submission_google_doc_url"] == url
