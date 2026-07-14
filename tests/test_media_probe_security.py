"""Offline security contracts for the shared audio probe and AI Coach byte gate."""

import asyncio
import base64
import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from api import ai_coach_api
from core import ai_provider, media_probe
import deploy.proxy as proxy


def _probe_result(*, format_name="webm,matroska", duration=5, returncode=0):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps({
            "format": {
                "format_name": format_name,
                "duration": str(duration),
            },
            "streams": [{
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 1,
            }],
        }),
        stderr="",
    )


def test_shared_probe_accepts_valid_audio_and_canonicalizes_browser_mime(monkeypatch):
    payload = b"offline-valid-webm"

    def fake_run(command, **kwargs):
        assert command[0] == "ffprobe"
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": media_probe.MEDIA_PROBE_TIMEOUT_SECONDS,
            "check": False,
        }
        with open(command[-1], "rb") as handle:
            assert handle.read() == payload
        return _probe_result()

    monkeypatch.setattr(media_probe.subprocess, "run", fake_run)
    result = media_probe.probe_audio(
        payload,
        " Audio/WebM ; codecs=opus ",
        5,
        max_seconds=60,
    )

    assert result == {
        "duration": 5.0,
        "sample_rate": 48000,
        "channels": 1,
        "format": "matroska,webm",
        "mime": "audio/webm",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_shared_probe_rejects_corrupt_ffprobe_result(monkeypatch):
    monkeypatch.setattr(
        media_probe.subprocess,
        "run",
        lambda *_args, **_kwargs: _probe_result(returncode=1),
    )

    with pytest.raises(media_probe.MediaProbeError, match="損壞") as raised:
        media_probe.probe_audio(b"corrupt", "audio/webm", 5, max_seconds=60)

    assert raised.value.service_unavailable is False


def test_shared_probe_rejects_fake_mime_container_pair(monkeypatch):
    monkeypatch.setattr(
        media_probe.subprocess,
        "run",
        lambda *_args, **_kwargs: _probe_result(format_name="mov,mp4,m4a"),
    )

    with pytest.raises(media_probe.MediaProbeError, match="宣稱格式與實際檔案格式不符"):
        media_probe.probe_audio(b"actually-mp4", "audio/webm", 5, max_seconds=60)


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("ffprobe", 10),
        OSError("ffprobe unavailable"),
    ],
    ids=["timeout", "oserror"],
)
def test_shared_probe_runtime_failures_are_service_unavailable(monkeypatch, failure):
    def fail_probe(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(media_probe.subprocess, "run", fail_probe)

    with pytest.raises(media_probe.MediaProbeError, match="未能執行音訊格式驗證") as raised:
        media_probe.probe_audio(b"audio", "audio/webm", 5, max_seconds=60)

    assert raised.value.service_unavailable is True


def test_shared_probe_rejects_claimed_actual_duration_mismatch(monkeypatch):
    monkeypatch.setattr(
        media_probe.subprocess,
        "run",
        lambda *_args, **_kwargs: _probe_result(duration=10),
    )

    with pytest.raises(media_probe.MediaProbeError, match="實際長度與瀏覽器回報不符"):
        media_probe.probe_audio(b"audio", "audio/webm", 2, max_seconds=60)


def test_missing_duration_fallback_reads_progress_not_decoded_pcm(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "ffprobe":
            return _probe_result(duration=0)
        assert command[0] == "ffmpeg"
        assert command[command.index("-progress") + 1] == "pipe:1"
        assert command[command.index("-f") + 1] == "null"
        assert "s16le" not in command
        assert "pipe:1" not in command[command.index("-f") + 2 :]
        return SimpleNamespace(
            returncode=0,
            stdout="out_time_us=5000000\nprogress=end\n",
            stderr="",
        )

    monkeypatch.setattr(media_probe.subprocess, "run", fake_run)
    result = media_probe.probe_audio(
        b"metadata-free-webm", "audio/webm", 5, max_seconds=60,
    )

    assert result["duration"] == 5.0
    assert len(calls) == 2
    assert calls[1][1]["text"] is True
    assert calls[1][1]["timeout"] == media_probe.MEDIA_TRANSCODE_TIMEOUT_SECONDS


def test_provider_transcode_is_mono_16kbps_mp3_and_output_bounded(monkeypatch):
    payload = b"validated-browser-audio"
    converted = b"ID3" + b"m" * 200

    def fake_run(command, **kwargs):
        assert command[0] == "ffmpeg"
        assert command[command.index("-i") + 1].endswith("source.webm")
        with open(command[command.index("-i") + 1], "rb") as source:
            assert source.read() == payload
        assert command[command.index("-ac") + 1] == "1"
        assert command[command.index("-ar") + 1] == "16000"
        assert command[command.index("-b:a") + 1] == "16k"
        assert command[command.index("-fs") + 1] == "1025"
        assert command[command.index("-f") + 1] == "mp3"
        with open(command[-1], "wb") as output:
            output.write(converted)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": media_probe.MEDIA_TRANSCODE_TIMEOUT_SECONDS,
            "check": False,
        }
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(media_probe.subprocess, "run", fake_run)
    result, mime = media_probe.transcode_audio_for_provider(
        payload,
        "audio/webm;codecs=opus",
        max_output_bytes=1024,
    )

    assert result == converted
    assert mime == "audio/mpeg"


def test_provider_transcode_rejects_output_over_bound_before_read(monkeypatch):
    def fake_run(command, **_kwargs):
        with open(command[-1], "wb") as output:
            output.write(b"x" * 65)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(media_probe.subprocess, "run", fake_run)
    with pytest.raises(media_probe.MediaProbeError, match="超出大小上限"):
        media_probe.transcode_audio_for_provider(
            b"audio", "audio/webm", max_output_bytes=64,
        )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("ffmpeg", 120),
        OSError("ffmpeg unavailable"),
    ],
    ids=["timeout", "oserror"],
)
def test_provider_transcode_runtime_failures_are_service_unavailable(
    monkeypatch, failure,
):
    monkeypatch.setattr(
        media_probe.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(media_probe.MediaProbeError, match="未能執行音訊格式轉換") as raised:
        media_probe.transcode_audio_for_provider(
            b"audio", "audio/webm", max_output_bytes=1024,
        )

    assert raised.value.service_unavailable is True


def _request():
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/ai-coach/run",
        "query_string": b"",
        "headers": [],
        "scheme": "https",
        "server": ("testserver", 443),
    })


def test_ai_coach_rejects_decoded_audio_over_two_mib_before_probe_or_provider(monkeypatch):
    oversized = b"x" * (ai_coach_api.MAX_COACH_AUDIO_BYTES + 1)
    provider_calls = []
    probe_calls = []

    monkeypatch.setattr(ai_coach_api, "_context", lambda _request: "alice")
    monkeypatch.setattr(proxy, "get_vote_db", lambda: object())
    monkeypatch.setattr(proxy, "_get_proxy_secret", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    monkeypatch.setattr(ai_coach_api, "_config", lambda *_args, **_kwargs: {
        "provider": "gemini",
        "model": "offline-model",
        "api_key": "GEMINI_API_KEY",
        "supports_audio": True,
        "supports_web_search": False,
    })
    monkeypatch.setattr(ai_coach_api, "_message", lambda *_args, **_kwargs: ("system", "user"))
    monkeypatch.setattr(ai_coach_api, "_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        ai_coach_api,
        "probe_audio",
        lambda *_args, **_kwargs: probe_calls.append(1),
    )

    from core import rag

    async def no_rag(*_args, **_kwargs):
        return ""

    async def no_provider(*_args, **_kwargs):
        provider_calls.append(1)
        return "must not run", {}

    monkeypatch.setattr(rag, "retrieve_rag_context", no_rag)
    monkeypatch.setattr(ai_provider, "generate_text", no_provider)
    body = ai_coach_api.CoachRequest(
        feature="speech_review",
        audio_base64=base64.b64encode(oversized).decode("ascii"),
        audio_mime="audio/webm",
        audio_duration_seconds=1,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(ai_coach_api.run(body, _request()))

    assert raised.value.status_code == 413
    assert provider_calls == []
    assert probe_calls == []
