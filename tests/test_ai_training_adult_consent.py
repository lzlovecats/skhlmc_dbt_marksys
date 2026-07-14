"""Regressions for the adult-only AI Training recording flow."""

from pathlib import Path

from api.ai_training_api import CONSENT_TEXT, CONSENT_VERSION, ConsentBody


ROOT = Path(__file__).resolve().parents[1]


def test_adult_consent_payload_no_longer_requires_minor_metadata():
    consent = ConsentBody(
        agreed=True,
        voice_cloning_confirmed=True,
        cloud_processing_confirmed=True,
    )

    assert consent.agreed is True
    assert CONSENT_VERSION == "tts_voice_v4_2026_07"
    assert "未成年" not in CONSENT_TEXT


def test_ai_training_ui_has_no_minor_consent_controls():
    html = (ROOT / "frontend/ai_training/index.html").read_text()
    script = (ROOT / "frontend/ai_training/app.js").read_text()

    assert "錄音者是否未滿 18 歲" not in html
    assert "minorStatus" not in html + script
    assert "guardianConfirmed" not in html + script
