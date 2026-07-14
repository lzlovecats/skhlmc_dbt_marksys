"""Regressions for the accepted-recording R2 manifest export."""

from decimal import Decimal
from pathlib import Path

import pandas as pd
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

from api import ai_training_api
from core import r2_storage


ROOT = Path(__file__).resolve().parents[1]


class _Frame:
    def to_dict(self, *, orient):
        assert orient == "records"
        return [
            {
                "id": 1,
                "speaker_user_id": "speaker",
                "script_id": "manuscript_001",
                "prompt_text": "第一句",
                "r2_key": "audio/one.webm",
                "mime_type": "audio/webm",
                "file_ext": "webm",
                "size_bytes": Decimal("1024"),
                "audio_sha256": "a" * 64,
                "duration_seconds": Decimal("4.25"),
                "sample_rate_hz": 48_000,
                "channel_count": 1,
                "detected_format": "webm",
                "ai_transcript": "第一句",
                "manuscript_id": "manuscript",
                "manuscript_title": "完整稿",
                "category": "完整稿",
            },
            {
                "id": 2,
                "speaker_user_id": "speaker",
                "script_id": "short_001",
                "prompt_text": "第二句",
                "r2_key": "audio/two.webm",
                "mime_type": "audio/webm",
                "file_ext": "webm",
                "size_bytes": Decimal("2048"),
                "audio_sha256": "b" * 64,
                "duration_seconds": Decimal("3.5"),
                "sample_rate_hz": pd.NA,
                "channel_count": pd.NA,
                "detected_format": pd.NA,
                "ai_transcript": "第二句",
                "manuscript_id": float("nan"),
                "manuscript_title": pd.NA,
                "category": "短句",
            },
        ]


class _Db:
    def query(self, sql, params):
        assert "WHERE r.status='accepted'" in sql
        assert params["export_limit"] == ai_training_api.RECORDING_MANIFEST_MAX_ROWS + 1
        return _Frame()


def test_recording_manifest_normalises_mixed_nullable_metadata(monkeypatch):
    monkeypatch.setattr(ai_training_api, "_admin", lambda _request: ("admin", _Db()))
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(
        r2_storage,
        "presign_get",
        lambda key, **_kwargs: f"https://r2.example/{key}",
    )

    result = ai_training_api.export_recording_manifest(None)

    assert result["items"][0]["size_bytes"] == 1024
    assert result["items"][0]["duration_seconds"] == 4.25
    assert result["items"][1]["sample_rate_hz"] is None
    assert result["items"][1]["channel_count"] is None
    assert result["items"][1]["detected_format"] is None
    assert result["items"][1]["manuscript_id"] is None
    assert result["items"][1]["manuscript_title"] is None

    # Exercise the same final encoder path FastAPI uses in production.
    JSONResponse(jsonable_encoder(result))


def test_admin_speaker_filter_is_a_readiness_backed_dropdown():
    page = (ROOT / "frontend" / "ai_training" / "index.html").read_text()
    script = (ROOT / "frontend" / "ai_training" / "app.js").read_text()

    assert '<select id="speakerFilter">' in page
    assert '<input id="speakerFilter"' not in page
    assert '<option value="">全部錄音者</option>' in page
    assert "speakerFilter.innerHTML" in script
    assert "ready.speakers || []" in script
    assert '$("speakerFilter").onchange' in script
    assert "syncRecordingExport();" in script
