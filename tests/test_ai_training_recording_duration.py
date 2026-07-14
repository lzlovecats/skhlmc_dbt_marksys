"""Regressions for MediaRecorder WebM files without duration metadata."""

import json
from types import SimpleNamespace

import pytest

from api import ai_training_api
from core import media_probe, r2_storage
from deploy import proxy


def _live_webm_probe_result():
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "format": {"format_name": "matroska,webm"},
                "streams": [
                    {
                        "codec_type": "audio",
                        "sample_rate": "48000",
                        "channels": 1,
                    }
                ],
            }
        ),
        stderr="",
    )


def test_live_webm_without_duration_is_measured_by_bounded_decode(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "ffprobe":
            return _live_webm_probe_result()
        assert command[0] == "ffmpeg"
        assert command[command.index("-progress") + 1] == "pipe:1"
        assert command[command.index("-t") + 1] == "61"
        assert command[command.index("-f") + 1] == "null"
        assert "s16le" not in command
        assert kwargs["text"] is True
        assert kwargs["timeout"] == media_probe.MEDIA_TRANSCODE_TIMEOUT_SECONDS
        return SimpleNamespace(
            returncode=0,
            stdout="out_time_us=4000000\nprogress=end\n",
            stderr="",
        )

    monkeypatch.setattr(media_probe.subprocess, "run", fake_run)

    result = media_probe.probe_audio(
        b"live-webm", "audio/webm", None, max_seconds=60,
    )

    assert result["duration"] == 4.0
    assert [command[0] for command, _kwargs in calls] == ["ffprobe", "ffmpeg"]


def test_bounded_decode_still_rejects_audio_over_server_limit(monkeypatch):
    def fake_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return _live_webm_probe_result()
        return SimpleNamespace(
            returncode=0,
            stdout="out_time_us=61000000\nprogress=end\n",
            stderr="",
        )

    monkeypatch.setattr(media_probe.subprocess, "run", fake_run)

    with pytest.raises(media_probe.MediaProbeError, match="錄音實際長度必須"):
        media_probe.probe_audio(
            b"overlong-live-webm", "audio/webm", None, max_seconds=60,
        )


def test_ai_training_claim_does_not_reject_missing_browser_duration(monkeypatch):
    claim = {
        "kind": "tts",
        "intent_id": "intent",
        "user": "member",
        "script_id": "short_001",
        "mime_type": "audio/webm",
        "byte_size": 4_000,
        "sha256": "a" * 64,
        "r2_key": "audio/tts/member/recording.webm",
        "pending_r2_key": "pending/audio/tts/member/recording.webm",
    }
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "secret")
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "verify_upload_claim", lambda *_args: claim)
    monkeypatch.setattr(
        r2_storage,
        "head",
        lambda _key: {
            "ContentLength": claim["byte_size"],
            "ContentType": claim["mime_type"],
            "Metadata": {"sha256": claim["sha256"]},
        },
    )
    body = ai_training_api.RecordingBody(
        script_id=claim["script_id"],
        mime_type=claim["mime_type"],
        duration_seconds=0,
        r2_upload_token="signed",
    )

    assert ai_training_api._verified_r2_audio_claim(body, "member") == claim
