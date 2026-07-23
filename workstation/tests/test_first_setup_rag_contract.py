from __future__ import annotations

import json
from pathlib import Path

import ai_model_config


ROOT = Path(__file__).resolve().parents[2]
CONFIG_EXAMPLE = json.loads(
    (ROOT / "workstation/config/config.example.json").read_text("utf-8")
)
WORKSTATION_RUNBOOK = (
    ROOT / "docs/AI_WORKSTATION_RUNBOOK.md"
).read_text("utf-8")
TRAINING_RUNBOOK = (ROOT / "docs/AI_TRAINING_RUNBOOK.md").read_text("utf-8")
TRAINING_PAGE = (ROOT / "frontend/ai_training/index.html").read_text("utf-8")
GUI_SCRIPT = (ROOT / "workstation/gui/static/app.js").read_text("utf-8")
GITIGNORE = (ROOT / ".gitignore").read_text("utf-8")


def test_first_setup_profile_requires_the_dedicated_rag_embedding_model():
    assert (
        ai_model_config.LMC_AI_RAG_EMBEDDING_MODEL_TAG
        == "embeddinggemma:300m"
    )
    assert set(ai_model_config.lmc_ai_workstation_required_models()) == {
        *ai_model_config.lmc_ai_required_models(),
        ai_model_config.LMC_AI_RAG_EMBEDDING_MODEL_TAG,
    }
    assert (
        ai_model_config.LMC_AI_RAG_EMBEDDING_MODEL_TAG
        not in ai_model_config.lmc_ai_all_models()
    )


def test_new_workstation_config_enables_rag_with_the_decided_model():
    rag = CONFIG_EXAMPLE["workloads"]["rag"]
    assert rag == {
        "enabled": True,
        "embedding_model": "embeddinggemma:300m",
        "active_link": "/srv/lmc-ai/rag/current",
    }


def test_first_setup_runbook_blocks_go_live_until_signed_nonempty_rag_v1():
    first_setup = WORKSTATION_RUNBOOK.split(
        "#### 步驟 6：安裝文字及 RAG embedding 模型", 1
    )[1].split("## 2.", 1)[0]
    assert "建立並啟用 RAG v1" in first_setup
    assert "documents.jsonl" in first_setup
    assert "非空白" in first_setup
    assert "未通過不得 Resume" in first_setup
    assert "lmc_ai_workstation_required_models" in WORKSTATION_RUNBOOK


def test_gui_requires_inspection_and_confirmation_before_rag_install():
    assert "inspectedArtifactCatalog?.rag_bundle" in GUI_SCRIPT
    assert "RAG bundle ID" in GUI_SCRIPT
    assert "SHA-256" in GUI_SCRIPT


def test_rag_and_sft_guides_define_every_jsonl_line_field():
    for source in (TRAINING_PAGE, TRAINING_RUNBOOK):
        assert "每行只可包含一個 JSON 物件" in source
        for field in ("id", "title", "text", "source_url"):
            assert f"<code>{field}</code>" in source or f"`{field}`" in source
        assert "最外層只可包含" in source
        assert "messages" in source
        for role in ("system", "user", "assistant"):
            assert f"<code>{role}</code>" in source or f"`{role}`" in source
        assert "提交者" in source
        assert "不應寫入 SFT JSONL" in source
        assert "虛構示例" in source
        assert "rules-overtime-2026-001" not in source


def test_private_ai_jsonl_loader_files_are_ignored():
    assert "private_ai_data/" in GITIGNORE
    assert "\ndocuments.jsonl\n" in f"\n{GITIGNORE}"
    assert "\nsft.jsonl\n" in f"\n{GITIGNORE}"
