"""Static contracts for the manager-only V0 debate data factory UI."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "frontend" / "ai_training" / "index.html").read_text("utf-8")
SCRIPT = (ROOT / "frontend" / "ai_training" / "app.js").read_text("utf-8")
FACTORY_PAGE = PAGE.split('<section id="factory"', 1)[1].split(
    '</section>\n        </section>\n      </section>', 1
)[0]
FACTORY_SCRIPT = SCRIPT.split("const factoryRows", 1)[1].split(
    "function chooseScript", 1
)[0]


def test_factory_is_one_preserved_admin_tab_with_three_exact_sections():
    assert PAGE.count('data-admin="factory"') == 1
    tab = re.search(r'<button data-admin="factory">([^<]+)</button>', PAGE)
    assert tab and tab.group(1) == "資料工廠"

    assert 'data-admin="recordings"' in PAGE
    assert 'data-admin="scripts"' in PAGE
    assert 'data-admin="manuscripts"' in PAGE
    assert 'data-admin="lexicon"' in PAGE
    assert 'data-admin="llm-review"' in PAGE

    headings = re.findall(
        r'data-factory-pane="[^"]+">([^<]+)</button', FACTORY_PAGE
    )
    assert headings == ["建立工作", "待審核", "已批准資料"]


def test_factory_dom_contract_covers_every_javascript_reference():
    referenced_ids = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', SCRIPT))
    page_ids = set(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', PAGE))
    assert referenced_ids <= page_ids
    assert len(page_ids) == len(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', PAGE))


def test_factory_uses_the_v0_routes_and_no_hidden_automatic_call():
    required = (
        "/api/ai-training/factory/bootstrap",
        "/api/ai-training/factory/sources?page=",
        'api("/api/ai-training/factory/sources"',
        "/api/ai-training/factory/sources/${encodeURIComponent(sourceId)}/withdraw",
        "/api/ai-training/factory/jobs/preview",
        "/api/ai-training/factory/jobs/${encodeURIComponent(jobId)}/generate",
        "/api/ai-training/factory/jobs?page=",
        "/api/ai-training/factory/items?status=pending&page=",
        "/api/ai-training/factory/items?status=approved&page=",
        "/api/ai-training/factory/items/${encodeURIComponent(factoryId(item))}/review",
        "/api/ai-training/factory/items/${encodeURIComponent(factoryId(item))}/withdraw",
        'api("/api/ai-training/factory/releases"',
        "/api/ai-training/factory/releases?page=",
        "/export.jsonl",
        "/manifest.json",
    )
    for contract in required:
        assert contract in SCRIPT

    assert "/retry" not in FACTORY_SCRIPT
    assert "/critic" not in FACTORY_SCRIPT
    assert "fallback" not in FACTORY_SCRIPT.lower()


def test_factory_preview_requires_exact_payload_and_all_confirmations():
    assert 'id="factoryPreviewDialog"' in PAGE
    assert 'id="factorySystemPrompt"' in PAGE
    assert 'id="factoryUserPrompt"' in PAGE
    assert 'id="factoryPreviewMeta"' in PAGE
    assert 'id="factoryRightsConfirmed"' in PAGE
    assert 'id="factoryAnonymizedConfirmed"' in PAGE
    assert 'id="factoryThirdPartyConfirmed"' in PAGE
    assert 'id="factoryThirdPartyWarningText"' in PAGE
    assert 'id="factoryPiiOverrideReason"' in PAGE

    for field in (
        "preview_token",
        "rights_confirmed",
        "anonymized_confirmed",
        "third_party_confirmed",
        "pii_override_reason",
    ):
        assert field in FACTORY_SCRIPT
    assert "estimated_cost_hkd" in FACTORY_SCRIPT
    assert "source_sha256" in FACTORY_SCRIPT
    assert "preview_sha256" in FACTORY_SCRIPT
    assert "expires_at" in FACTORY_SCRIPT
    assert "preview.third_party_warning" in FACTORY_SCRIPT
    assert "factoryState.bootstrap?.third_party_warning" in FACTORY_SCRIPT


def test_factory_matches_generic_api_ids_and_pagination_shape():
    factory_id = FACTORY_SCRIPT.split("factoryId = (item)", 1)[1].split(
        "factoryPageCount", 1
    )[0]
    assert factory_id.index("item?.id") < factory_id.index("item?.item_id")
    assert factory_id.index("item?.id") < factory_id.index("item?.job_id")
    assert factory_id.index("item?.id") < factory_id.index("item?.source_id")
    assert "payload?.total_pages" in FACTORY_SCRIPT


def test_factory_prefers_product_recipe_labels_and_api_review_aliases():
    recipe_label = FACTORY_SCRIPT.split("function factoryRecipeLabel", 1)[1].split(
        "function factoryArtifactKind", 1
    )[0]
    assert recipe_label.index("FACTORY_RECIPE_LABELS[key]") < recipe_label.index(
        "item?.label"
    )
    assert "item.job_created_by" in FACTORY_SCRIPT
    assert "item.source_side" in FACTORY_SCRIPT


def test_failed_job_retry_restores_locked_fields_and_returns_to_preview():
    assert 'id="factoryCancelRetry"' in FACTORY_PAGE
    assert "重新預覽" in FACTORY_SCRIPT
    assert "function restoreFactoryRetryJob(job)" in FACTORY_SCRIPT
    assert "job.source_id" in FACTORY_SCRIPT
    assert "job.recipe_id || job.recipe_key" in FACTORY_SCRIPT
    assert "job.requested_count ?? job.item_count" in FACTORY_SCRIPT
    assert "job.model_label" in FACTORY_SCRIPT
    assert "job.instruction_text ?? job.manager_instruction" in FACTORY_SCRIPT
    assert "request.job_id = factoryState.retryJobId" in FACTORY_SCRIPT
    assert '$("factoryRecipe").disabled = retrying' in FACTORY_SCRIPT
    assert '$("factoryCount").disabled = retrying' in FACTORY_SCRIPT
    assert '$("factoryManagerInstruction").disabled = retrying' in FACTORY_SCRIPT
    assert '$("factoryManagerInstruction").value = managerInstruction' in FACTORY_SCRIPT
    assert "attempts >= 3" in FACTORY_SCRIPT
    preview = FACTORY_SCRIPT.split("async function previewFactoryJob", 1)[1].split(
        "async function generateFactoryJob", 1
    )[0]
    assert "setFactoryRetryMode();" in preview

    generate = FACTORY_SCRIPT.split("async function generateFactoryJob", 1)[1].split(
        "function renderFactoryJobs", 1
    )[0]
    failure = generate.split("} catch (error) {", 1)[1]
    assert "factoryState.preview = null;" in failure
    assert '$("factoryPreviewDialog").close();' in failure
    for confirmation in (
        "factoryRightsConfirmed",
        "factoryAnonymizedConfirmed",
        "factoryThirdPartyConfirmed",
    ):
        assert f'$("{confirmation}").checked = false;' in failure


def test_factory_fixed_language_recipes_and_batch_bounds_are_visible():
    assert 'min="1" max="5" value="3"' in FACTORY_PAGE
    assert 'id="factoryPasteSourceNote" maxlength="1000"' in FACTORY_PAGE
    assert "limits.source_note_max_chars" in FACTORY_SCRIPT
    assert 'id="factoryManagerInstruction" maxlength="500"' in FACTORY_PAGE
    assert 'manager_instruction: $("factoryManagerInstruction").value.trim()' in FACTORY_SCRIPT
    assert "yue-Hant-HK" in FACTORY_PAGE
    assert 'FACTORY_LANGUAGE = "yue-Hant-HK"' in SCRIPT
    for label in (
        "RAG來源知識卡",
        "RAG論證拆解卡",
        "演辭評改",
        "SFT攻防演練對話",
    ):
        assert label in SCRIPT
    assert "攻防演練只可使用已標示正方或反方的來源" in FACTORY_PAGE
    assert "Free Provider 不扣 AI Fund" in FACTORY_PAGE
    assert "候選資料仍須人工審核" in FACTORY_PAGE


def test_factory_pasted_source_exposes_all_side_values_and_defaults_to_not_applicable():
    select = FACTORY_PAGE.split('<select id="factoryPasteSide">', 1)[1].split(
        "</select>", 1
    )[0]
    assert re.findall(r'<option value="([^"]+)"', select) == [
        "not_applicable",
        "pro",
        "con",
        "neutral",
    ]
    assert '<option value="not_applicable" selected>' in select


def test_factory_pasted_source_language_is_explicit_and_sent_to_api():
    select = FACTORY_PAGE.split('<select id="factoryPasteLanguage"', 1)[1].split(
        "</select>", 1
    )[0]
    assert re.findall(r'<option value="([^"]+)"', select) == [
        "yue-Hant-HK",
        "zh-Hant",
        "en",
        "mixed",
        "other",
    ]
    assert '<option value="yue-Hant-HK" selected>' in select
    create_source = FACTORY_SCRIPT.split(
        "async function createFactoryPasteSource", 1
    )[1].split("async function loadFactoryWorkspace", 1)[0]
    assert 'language: $("factoryPasteLanguage").value' in create_source


def test_factory_source_withdraw_is_per_item_reasoned_and_excludes_virtual_rows():
    withdraw = FACTORY_SCRIPT.split("async function withdrawFactorySource", 1)[1].split(
        "function renderFactorySources", 1
    )[0]
    assert 'sourceId.startsWith("submission:")' in withdraw
    assert 'confirmAsk(' in withdraw
    assert "window.prompt(" in withdraw
    assert "請填寫撤回原因" in withdraw
    assert "JSON.stringify({ reason })" in withdraw
    for refresh in (
        "loadFactorySources(true)",
        "loadFactoryJobs(true)",
        "loadFactoryReviewItems(true)",
        "loadFactoryApprovedItems(true)",
        "loadFactoryReleases(true)",
    ):
        assert refresh in withdraw

    render = FACTORY_SCRIPT.split("function renderFactorySources", 1)[1].split(
        "async function loadFactorySources", 1
    )[0]
    assert '!sourceId.startsWith("submission:")' in render
    assert 'event.stopPropagation()' in render
    assert 'card.appendChild(label)' in render
    assert 'card.appendChild(actions)' in render


def test_factory_review_is_single_item_editable_and_preserves_original():
    assert "每次只審核一項" in FACTORY_PAGE
    assert 'id="factoryReviewSource"' in FACTORY_PAGE
    assert 'id="factoryReviewPayload"' in FACTORY_PAGE
    assert "factoryPrettyJson(factoryCandidatePayload(item))" in FACTORY_SCRIPT
    assert "JSON.parse($(\"factoryReviewPayload\").value)" in FACTORY_SCRIPT
    assert "reviewed_payload: reviewedPayload" in FACTORY_SCRIPT
    assert "expected_revision:" in FACTORY_SCRIPT
    assert 'reviewFactoryItem("approved")' in SCRIPT
    assert 'reviewFactoryItem("rejected")' in SCRIPT
    assert "bulk" not in FACTORY_PAGE.lower()


def test_factory_release_selection_is_manual_separate_and_warns_if_invalid():
    assert "手動勾選" in FACTORY_PAGE
    assert '<option value="rag">RAG</option>' in FACTORY_PAGE
    assert '<option value="sft">SFT</option>' in FACTORY_PAGE
    assert "factoryState.selectedApproved" in FACTORY_SCRIPT
    assert "dataset_kind: kind" in FACTORY_SCRIPT
    assert "invalidated" in FACTORY_SCRIPT
    assert "withdrawn" in FACTORY_SCRIPT
    assert "不可再下載" in FACTORY_SCRIPT


def test_factory_dynamic_content_uses_safe_dom_and_stale_response_guards():
    assert "factoryEl" in FACTORY_SCRIPT
    assert ".textContent" in FACTORY_SCRIPT
    assert ".innerHTML" not in FACTORY_SCRIPT
    assert "factoryRequestIsCurrent" in FACTORY_SCRIPT
    assert "factoryState.loading" in FACTORY_SCRIPT
    assert "JSON.stringify(warning)" in FACTORY_SCRIPT


def test_factory_has_no_transcript_studio_or_vector_index_dependency():
    combined = FACTORY_PAGE + FACTORY_SCRIPT
    assert "debate-transcript-studio" not in combined
    assert "pgvector" not in combined.lower()
    assert "/rag/reindex" not in combined
    assert "embedding" not in combined.lower()
