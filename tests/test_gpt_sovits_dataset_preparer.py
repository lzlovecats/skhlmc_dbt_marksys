"""Offline regressions for the GPT-SoVITS dataset preparation helper."""

import hashlib
import json
import stat
from types import SimpleNamespace

import pytest

from tools import prepare_gpt_sovits_dataset as preparer


def _manifest_item(
    recording_id: int,
    *,
    speaker: str = "speaker-a",
    audio_sha256: str | None = None,
    size_bytes: int = 4,
) -> dict[str, object]:
    return {
        "id": recording_id,
        "speaker_user_id": speaker,
        "script_id": f"script-{recording_id}",
        "manuscript_id": f"manuscript-{recording_id}",
        "prompt_text": f"第 {recording_id} 句",
        "mime_type": "audio/webm",
        "file_ext": "webm",
        "size_bytes": size_bytes,
        "audio_sha256": audio_sha256 or f"{recording_id:064x}",
        "download_url": f"https://r2.example/audio/{recording_id}?token=secret",
    }


def _write_manifest(path, items):
    path.write_text(
        json.dumps({"storage": "r2", "expires_seconds": 3600, "items": items}),
        encoding="utf-8",
    )


def test_clean_text_replaces_pipe_with_chinese_comma():
    assert preparer._clean_text("  第一句|第二句\n  第三句  ") == "第一句，第二句 第三句"


def test_split_for_is_stable_and_keeps_a_group_together():
    assert preparer._split_for("group-0") == "train"
    assert preparer._split_for("group-2") == "test"
    assert preparer._split_for("group-26") == "validation"
    assert {
        preparer._split_for("same-manuscript")
        for _segment_id in ("segment-1", "segment-2", "segment-3")
    } == {"train"}


def test_duplicate_audio_sha_fails_before_network(tmp_path, monkeypatch):
    manifest_path = tmp_path / "recordings.json"
    duplicate_sha = "a" * 64
    _write_manifest(
        manifest_path,
        [
            _manifest_item(1, audio_sha256=duplicate_sha),
            _manifest_item(2, audio_sha256=duplicate_sha),
        ],
    )
    monkeypatch.setattr(
        preparer,
        "_download_https_audio",
        lambda *_args, **_kwargs: pytest.fail("network/download must not be attempted"),
    )

    with pytest.raises(ValueError, match="duplicate audio SHA-256"):
        preparer._materialize_recordings_manifest(
            manifest_path,
            tmp_path / "dataset",
            speaker=None,
            overwrite=False,
        )


def test_multiple_speakers_fail_before_network(tmp_path, monkeypatch):
    manifest_path = tmp_path / "recordings.json"
    _write_manifest(
        manifest_path,
        [
            _manifest_item(1, speaker="speaker-a"),
            _manifest_item(2, speaker="speaker-b"),
        ],
    )
    monkeypatch.setattr(
        preparer,
        "_download_https_audio",
        lambda *_args, **_kwargs: pytest.fail("network/download must not be attempted"),
    )

    with pytest.raises(ValueError, match="Multiple speakers found"):
        preparer._materialize_recordings_manifest(
            manifest_path,
            tmp_path / "dataset",
            speaker=None,
            overwrite=False,
        )


def test_prepare_stops_before_network_when_ffmpeg_is_missing(tmp_path, monkeypatch):
    manifest_path = tmp_path / "recordings.json"
    _write_manifest(manifest_path, [_manifest_item(1)])
    monkeypatch.setattr(preparer.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        preparer,
        "_materialize_recordings_manifest",
        lambda *_args, **_kwargs: pytest.fail("download must not start"),
    )
    args = SimpleNamespace(
        input_file=str(manifest_path),
        output_dir=str(tmp_path / "output"),
        speaker=None,
        experiment=None,
        language="yue",
        text_column="prompt_text",
        list_name=None,
        progress_file=None,
        overwrite=False,
    )

    with pytest.raises(RuntimeError, match="ffmpeg, ffprobe"):
        preparer.prepare_dataset(args)


def test_normalize_audio_uses_only_required_conversion_options(tmp_path, monkeypatch):
    source = tmp_path / "source.webm"
    target = tmp_path / "wav" / "1.wav"
    source.write_bytes(b"source")
    commands = []

    monkeypatch.setattr(preparer.shutil, "which", lambda executable: f"/usr/bin/{executable}")

    def fake_run(command, **_kwargs):
        commands.append(command)
        target.write_bytes(b"pcm-wave")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preparer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        preparer,
        "_probe_audio_file",
        lambda _path: {
            "duration_seconds": 3.25,
            "sample_rate_hz": 32000,
            "channels": 1,
            "sample_format": "s16",
            "codec": "pcm_s16le",
            "format": "wav",
        },
    )

    quality = preparer._normalize_audio(source, target)

    assert quality["sample_rate_hz"] == 32000
    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("-map") + 1] == "0:a:0"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "32000"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert "-af" not in command
    assert "loudnorm" not in " ".join(command).lower()


def test_hardware_recommendation_selects_largest_gpu_and_caps_batch_for_ram():
    hardware = {
        "system": "Linux",
        "machine": "x86_64",
        "memory_gb": 24.0,
        "disk_free_gb": 500.0,
        "gpus": [
            {
                "index": 3,
                "name": "RTX 3060",
                "memory_total_gb": 12.0,
                "compute_capability": "8.6",
                "backend": "cuda",
                "verified_by_pytorch": True,
            },
            {
                "index": 7,
                "name": "RTX 4090",
                "memory_total_gb": 24.0,
                "compute_capability": "8.9",
                "backend": "cuda",
                "verified_by_pytorch": True,
            },
        ],
    }

    short_run = preparer._recommended_params(hardware, total_minutes=5)
    long_run = preparer._recommended_params(hardware, total_minutes=180)

    assert short_run["gpu_info"] == "7"
    assert short_run["selected_gpu"]["index"] == 7
    assert short_run["sovits_batch"] == 2
    assert short_run["gpt_batch"] == 2
    assert short_run["precision"] == "16-mixed"
    assert any("below the 32 GB" in warning for warning in short_run["warnings"])
    for recommendation in (short_run, long_run):
        assert recommendation["sovits_epochs"] == preparer.UPSTREAM_PROFILE["sovits_epochs"]
        assert recommendation["gpt_epochs"] == preparer.UPSTREAM_PROFILE["gpt_epochs"]


def test_hardware_recommendation_does_not_emit_gpu_zero_without_cuda():
    recommendation = preparer._recommended_params(
        {
            "system": "Linux",
            "machine": "x86_64",
            "memory_gb": 64.0,
            "disk_free_gb": 500.0,
            "gpus": [
                {
                    "index": 0,
                    "name": "ROCm GPU",
                    "memory_total_gb": 24.0,
                    "backend": "rocm",
                    "verified_by_pytorch": True,
                }
            ],
        },
        total_minutes=60,
    )

    assert recommendation["gpu_info"] == ""
    assert recommendation["selected_gpu"] is None
    assert recommendation["training_recommended"] is False
    assert "No CUDA GPU" in recommendation["device_note"]


@pytest.mark.parametrize("empty_split", ["validation", "test"])
def test_readiness_blocks_an_empty_validation_or_test_split(empty_split):
    split_lines = {
        "train": ["train"],
        "validation": ["validation"],
        "test": ["test"],
    }
    split_lines[empty_split] = []

    readiness = preparer._readiness(60, split_lines)

    assert readiness["status"] == "BLOCKED_SPLIT"
    assert readiness["production_ready"] is False
    assert readiness["manual_gates_complete"] is False
    assert "never move test clips into train" in readiness["warnings"][0]


def test_manifest_provenance_strips_urls_and_hash_matches(tmp_path, monkeypatch):
    manifest_path = tmp_path / "recordings.json"
    audio = b"test"
    item = _manifest_item(1, audio_sha256=hashlib.sha256(audio).hexdigest())
    _write_manifest(manifest_path, [item])

    def fake_download(url, target, **_kwargs):
        assert url == item["download_url"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio)
        return len(audio)

    monkeypatch.setattr(preparer, "_download_https_audio", fake_download)
    output_dir = tmp_path / "dataset"

    preparer._materialize_recordings_manifest(
        manifest_path,
        output_dir,
        speaker=None,
        overwrite=False,
    )

    lock_path = output_dir / "provenance" / "manifest.lock.json"
    checksum_path = output_dir / "provenance" / "manifest.lock.sha256"
    lock_bytes = lock_path.read_bytes()
    locked = json.loads(lock_bytes)
    expected_digest = checksum_path.read_text(encoding="utf-8").split()[0]

    assert "download_url" not in lock_bytes.decode("utf-8")
    assert "download_url" not in locked["items"][0]
    assert hashlib.sha256(lock_bytes).hexdigest() == expected_digest
    raw_audio = next((output_dir / "raw" / "audio").iterdir())
    assert stat.S_IMODE(raw_audio.stat().st_mode) == 0o400
