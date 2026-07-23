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
        "先了解 RAG 與 SFT 的分別",
        "RAG 資料製作流程",
        "SFT 資料製作流程",
        "發布前檢查",
    ):
        assert heading in PAGE


def test_rag_guide_matches_the_workstation_documents_jsonl_contract():
    assert "documents.jsonl" in PAGE
    for field in ('"id"', '"title"', '"text"', '"source_url"'):
        assert field in PAGE
    assert "每行為一個 JSON 物件" in PAGE
    assert "HTTPS" in PAGE
    assert "穩定且唯一" in PAGE
    assert "完整長文直接視為一個段落" in PAGE


def test_sft_guide_keeps_behaviour_training_separate_from_retrievable_knowledge():
    assert '"messages"' in PAGE
    for role in ('"system"', '"user"', '"assistant"'):
        assert role in PAGE
    assert "僅訓練 assistant 回覆" in PAGE
    assert "事實、規則、案例及引用資料應存放於 RAG" in PAGE
    assert "原始講稿或逐字稿並非天然的問答配對" in PAGE


def test_factory_guide_uses_written_chinese_for_user_facing_copy():
    guide = PAGE.split('<section id="factory" class="admin-pane">', 1)[1].split(
        "</section>\n        </section>\n      </section>", 1
    )[0]
    for colloquial_term in ("呢個", "唔", "喺", "嘅", "嚟", "點樣", "講咗", "發生咩事"):
        assert colloquial_term not in guide
    assert "；" not in guide


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
