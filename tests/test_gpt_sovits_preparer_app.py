"""Security and workflow tests for the localhost GPT-SoVITS wrapper."""

from __future__ import annotations

import http.client
import json
import stat
import threading
import time
from pathlib import Path

import pytest

from tools import gpt_sovits_preparer_app as app


TOKEN = "test-only-preparer-token"


def _system(_output_root: Path) -> dict[str, object]:
    return {
        "hardware": {
            "system": "Linux",
            "machine": "x86_64",
            "cpu_count": 8,
            "memory_gb": 32.0,
            "nvidia_vram_gb": [12.0],
        },
        "dependencies": {
            "ffmpeg": {"available": True, "path": "/fake/ffmpeg"},
            "ffprobe": {"available": True, "path": "/fake/ffprobe"},
        },
        "disk": {"total_gb": 500.0, "free_gb": 300.0},
        "preflight": [],
        "ready": True,
        "initial_recommendation": {"sovits_batch": 2, "gpt_batch": 2},
        "output_root": str(_output_root),
        "limits": {"json_bytes": 10_000_000, "zip_bytes": 10_000_000},
    }


def _successful_worker(input_path: Path, workspace: Path, progress_path: Path) -> int:
    assert input_path.is_file()
    progress_path.write_text(
        json.dumps({"percent": 65, "message": "正在轉檔"}), encoding="utf-8"
    )
    (workspace / "preparation_result.json").write_text(
        json.dumps({
            "quality_report": {
                "rows": 1,
                "total_minutes": 1.5,
                "splits": {"train": 1, "validation": 0, "test": 0},
            },
            "recommended_params": {"sovits_batch": 2, "gpt_batch": 2},
            "unsafe_test_value": "https://r2.example/object?signature=secret",
        }),
        encoding="utf-8",
    )
    return 0


@pytest.fixture
def local_server(tmp_path):
    server = app.create_server(
        port=0,
        output_root=tmp_path / "private",
        token=TOKEN,
        worker=_successful_worker,
        system_provider=_system,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server, method, path, *, body=b"", headers=None):
    connection = http.client.HTTPConnection(
        app.LOOPBACK_HOST, server.server_port, timeout=3
    )
    request_headers = dict(headers or {})
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    result = response.status, dict(response.getheaders()), payload
    connection.close()
    return result


def _api_headers(server, **extra):
    return {
        "X-Preparer-Token": TOKEN,
        **extra,
    }


def _upload_headers(server, filename="recordings.json", **extra):
    return _api_headers(
        server,
        Origin=server.expected_origin,
        **{"X-File-Name": filename, "Content-Type": "application/json"},
        **extra,
    )


def _manifest_bytes():
    return json.dumps({
        "items": [{
            "id": 1,
            "speaker_user_id": "speaker",
            "download_url": "https://r2.example/audio?signature=secret",
        }]
    }).encode("utf-8")


def _wait_for_terminal_job(server, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = server.jobs.snapshot()
        if job["status"] not in app.ACTIVE_STATUSES:
            return job
        time.sleep(0.01)
    raise AssertionError("local preparation job did not finish")


def test_server_factory_refuses_non_loopback_bind(tmp_path):
    with pytest.raises(ValueError, match="127.0.0.1"):
        app.PreparerHTTPServer(
            ("0.0.0.0", 0),
            token=TOKEN,
            output_root=tmp_path,
            worker=_successful_worker,
            system_provider=_system,
        )


def test_html_and_api_require_exact_host_and_token(local_server):
    status, headers, body = _request(
        local_server, "GET", f"/?token={TOKEN}"
    )
    assert status == 200
    assert b"GPT-SoVITS" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"

    status, _headers, _body = _request(local_server, "GET", "/api/system")
    assert status == 403

    status, _headers, _body = _request(
        local_server,
        "GET",
        "/api/system",
        headers={"Host": "attacker.example", "X-Preparer-Token": TOKEN},
    )
    assert status == 403

    status, _headers, body = _request(
        local_server,
        "GET",
        "/api/system",
        headers=_api_headers(local_server),
    )
    assert status == 200
    assert json.loads(body)["system"]["ready"] is True


def test_prepare_requires_same_origin_and_supported_filename(local_server):
    body = _manifest_bytes()
    status, _headers, _body = _request(
        local_server,
        "POST",
        "/api/prepare",
        body=body,
        headers=_api_headers(
            local_server,
            **{"X-File-Name": "recordings.json", "Content-Type": "application/json"},
        ),
    )
    assert status == 403

    status, _headers, payload = _request(
        local_server,
        "POST",
        "/api/prepare",
        body=body,
        headers=_upload_headers(local_server, filename="recordings.txt"),
    )
    assert status == 415
    assert "只接受" in json.loads(payload)["error"]


def test_prepare_stops_before_upload_when_preflight_is_not_ready(tmp_path):
    def not_ready(output_root):
        snapshot = _system(output_root)
        snapshot["ready"] = False
        return snapshot

    server = app.create_server(
        port=0,
        output_root=tmp_path / "private",
        token=TOKEN,
        worker=lambda *_args: pytest.fail("worker must not start"),
        system_provider=not_ready,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, payload = _request(
            server,
            "POST",
            "/api/prepare",
            body=_manifest_bytes(),
            headers=_upload_headers(server),
        )
        assert status == 409
        assert "預檢未通過" in json.loads(payload)["error"]
        assert list(server.jobs.incoming_dir.iterdir()) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_malformed_upload_is_rejected_and_temp_is_removed(local_server):
    status, _headers, payload = _request(
        local_server,
        "POST",
        "/api/prepare",
        body=b"not json",
        headers=_upload_headers(local_server),
    )
    assert status == 400
    assert "格式不正確" in json.loads(payload)["error"]
    assert list(local_server.jobs.incoming_dir.iterdir()) == []


def test_multi_speaker_manifest_fails_closed_before_worker(local_server):
    body = json.dumps({
        "items": [
            {"id": 1, "speaker_user_id": "speaker-a"},
            {"id": 2, "speaker_user_id": "speaker-b"},
        ]
    }).encode("utf-8")
    status, _headers, payload = _request(
        local_server,
        "POST",
        "/api/prepare",
        body=body,
        headers=_upload_headers(local_server),
    )
    assert status == 400
    assert "多個錄音者" in json.loads(payload)["error"]
    assert local_server.jobs.snapshot()["status"] == "idle"
    assert list(local_server.jobs.incoming_dir.iterdir()) == []


def test_valid_upload_runs_worker_redacts_urls_and_deletes_temp(local_server):
    status, _headers, payload = _request(
        local_server,
        "POST",
        "/api/prepare",
        body=_manifest_bytes(),
        headers=_upload_headers(local_server),
    )
    assert status == 202
    assert json.loads(payload)["ok"] is True

    job = _wait_for_terminal_job(local_server)
    assert job["status"] == "completed"
    assert job["result"]["recommended_params"]["sovits_batch"] == 2
    assert "r2.example" not in json.dumps(job, ensure_ascii=False)
    assert job["result"]["unsafe_test_value"] == "[已隱藏 URL]"
    assert list(local_server.jobs.incoming_dir.iterdir()) == []

    workspace = Path(job["workspace"])
    assert workspace.is_dir()
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700

    status, _headers, body = _request(
        local_server,
        "GET",
        "/api/job",
        headers=_api_headers(local_server),
    )
    assert status == 200
    assert b"signature=secret" not in body


def test_only_one_background_job_can_run(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking_worker(_input_path, workspace, _progress_path):
        started.set()
        assert release.wait(timeout=2)
        (workspace / "preparation_result.json").write_text(
            json.dumps({"recommended_params": {"sovits_batch": 1}}),
            encoding="utf-8",
        )
        return 0

    server = app.create_server(
        port=0,
        output_root=tmp_path / "private",
        token=TOKEN,
        worker=blocking_worker,
        system_provider=_system,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = _manifest_bytes()
        status, _headers, _payload = _request(
            server,
            "POST",
            "/api/prepare",
            body=body,
            headers=_upload_headers(server),
        )
        assert status == 202
        assert started.wait(timeout=1)

        status, _headers, payload = _request(
            server,
            "POST",
            "/api/prepare",
            body=body,
            headers=_upload_headers(server),
        )
        assert status == 409
        assert "進行中" in json.loads(payload)["error"]
    finally:
        release.set()
        _wait_for_terminal_job(server)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_no_route_can_serve_workspace_audio(local_server):
    status, _headers, _body = _request(
        local_server,
        "GET",
        "/audio/recording.wav",
        headers=_api_headers(local_server),
    )
    assert status == 404
