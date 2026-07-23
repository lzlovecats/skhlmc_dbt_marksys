"""Static contracts for the retired data-factory tab and its replacement guide."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "frontend" / "ai_training" / "index.html").read_text("utf-8")
SCRIPT = (ROOT / "frontend" / "ai_training" / "app.js").read_text("utf-8")


def test_factory_tab_is_retained_as_one_static_admin_guide():
    assert PAGE.count('data-admin="factory"') == 1
    tab = re.search(r'<button data-admin="factory">([^<]+)</button>', PAGE)
    assert tab and tab.group(1) == "🏭 資料工廠"
    assert PAGE.count('<section id="factory" class="admin-pane">') == 1

    for heading in (
        "先分清 RAG 與 SFT",
        "RAG 資料製作流程",
        "SFT 資料製作流程",
        "發布前檢查",
    ):
        assert heading in PAGE


def test_rag_guide_matches_the_workstation_documents_jsonl_contract():
    assert "documents.jsonl" in PAGE
    for field in ('"id"', '"title"', '"text"', '"source_url"'):
        assert field in PAGE
    assert "一行一個 JSON object" in PAGE
    assert "HTTPS" in PAGE
    assert "穩定而且唯一" in PAGE
    assert "完整長文直接當成一段" in PAGE


def test_sft_guide_keeps_behaviour_training_separate_from_retrievable_knowledge():
    assert '"messages"' in PAGE
    for role in ('"system"', '"user"', '"assistant"'):
        assert role in PAGE
    assert "只訓練 assistant 回覆" in PAGE
    assert "事實、規則、案例同引用放入 RAG" in PAGE
    assert "原始講稿或逐字稿唔係天然問答配對" in PAGE


def test_retired_factory_has_no_browser_workflow_or_endpoint():
    combined = PAGE + SCRIPT
    for retired in (
        "/api/ai-training/factory",
        "factoryState",
        "loadFactoryWorkspace",
        "factoryPreviewDialog",
        "data-factory-pane",
        "factoryGenerateBtn",
    ):
        assert retired not in combined


def test_factory_guide_adds_no_javascript_dom_dependency():
    referenced_ids = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', SCRIPT))
    page_ids = set(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', PAGE))
    assert referenced_ids <= page_ids
    assert len(page_ids) == len(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', PAGE))
