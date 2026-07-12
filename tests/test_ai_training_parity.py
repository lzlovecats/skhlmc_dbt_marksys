from pathlib import Path

from api.ai_training_api import DEFAULT_SCRIPT_BANK, _segments

ROOT = Path(__file__).resolve().parents[1]


def test_default_script_bank_matches_legacy_and_is_unique():
    assert len(DEFAULT_SCRIPT_BANK) == 37
    ids = [row[0] for row in DEFAULT_SCRIPT_BANK]
    assert len(set(ids)) == len(ids)
    assert (ids[0], ids[-1]) == ("free_001", "prosody_003")


def test_manuscript_segmentation_uses_legacy_limit_and_punctuation():
    source = "第一句有逗號，第二句有頓號、第三句有分號；第四句最後完結。" * 3
    parts = _segments(source)
    assert "".join(parts) == source
    assert all(0 < len(part) <= 35 for part in parts)
    assert len(parts) > 1


def test_admin_has_five_sections_and_single_renderer():
    html = (ROOT / "frontend/ai_training/index.html").read_text(encoding="utf-8")
    assert html.count("data-admin=") == 5
    assert "讀音字典管理" in html
    assert "server-tables.js" not in html and "ai-parity.js" not in html
    assert html.count("/ai-training/app.js") == 1


def test_fallback_duplicate_guard_withdraw_and_export_parity():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    assert "此資料已提交，請勿重複提交" in api
    assert '@router.delete("/llm/{submission_id}")' in api
    assert "manualAudioSubmit" in js and "manualLlmSubmit" in js
    assert "alert(" not in js and "prompt(" not in js and "confirm(" not in js
    assert 'def export_recordings(request: Request, speaker: str = "")' in api
    assert 'archive.writestr("metadata.csv"' in api


def test_recording_gate_and_public_training_guidance_match_legacy():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    html = (ROOT / "frontend/ai_training/index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    for marker in ("_audio_payload(body)", "matches_prompt", "duration_seconds", "tts_review", "llm_review"):
        assert marker in api
    for marker in ('id="lexicon-view"', 'id="rdPlan"', 'id="resetSkipped"', 'id="clearLlm"'):
        assert marker in html
    for marker in ("recordedSeconds", "resetRecording", "重新錄製跳過的句子", "SafeMarkdown.render"):
        assert marker in html + js


def test_admin_ai_planning_is_selective_and_protects_recorded_scripts():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    assert '@router.post("/coverage/ai")' in api
    assert "build_tts_coverage_prompt" in api
    assert "build_tts_regenerate_prompt" in api
    assert "deactivate_candidates" in api
    assert "status IN ('pending','accepted')" in api
    assert "deactivate_ids" in api and "data-suggestion" in js


def test_recording_review_uses_streamlit_page_size():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    assert "ADMIN_RECORDING_PAGE_SIZE = 5" in api
    assert '"page_size": ADMIN_RECORDING_PAGE_SIZE' in api
