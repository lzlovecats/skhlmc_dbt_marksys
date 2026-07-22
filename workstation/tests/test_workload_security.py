from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from system_limits import LMC_AI_OUTPUT_MAX_BYTES
from workstation.config import AsrConfig, GptSoVitsConfig, OllamaConfig
from workstation.workloads.asr import Qwen3AsrAdapter
from workstation.workloads.errors import WorkloadError
from workstation.workloads.gpt_sovits import GptSoVitsAdapter
from workstation.workloads.ollama import OllamaAdapter
from workstation.workloads.r2_transfer import download_to_path
from workstation.scripts import approve_gpt_sovits_voice


def test_direct_r2_transfer_rejects_non_https_before_network(tmp_path):
    with pytest.raises(WorkloadError, match="invalid media"):
        download_to_path("http://127.0.0.1/private", tmp_path / "audio", max_bytes=100)


def test_ollama_chat_preserves_bounded_stage_durations(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield json.dumps({
                "message": {"content": "答案"},
                "done": True,
                "prompt_eval_count": 12,
                "eval_count": 5,
                "total_duration": 90_000_000,
                "load_duration": 10_000_000,
                "prompt_eval_duration": 20_000_000,
                "eval_duration": 30_000_000,
            })

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("workstation.workloads.ollama.httpx.Client", Client)
    text, usage = OllamaAdapter(OllamaConfig()).chat(
        model="approved:model",
        messages=[{"role": "user", "content": "測試"}],
        think=False,
        keep_alive="0",
        timeout_seconds=30,
    )
    assert text == "答案"
    assert usage["duration_ms"] == 90
    assert usage["load_duration_ms"] == 10
    assert usage["prompt_eval_duration_ms"] == 20
    assert usage["generation_duration_ms"] == 30


def test_ollama_chat_rejects_output_above_protocol_limit(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield json.dumps({
                "message": {"content": "x" * (LMC_AI_OUTPUT_MAX_BYTES + 1)},
                "done": False,
            })

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("workstation.workloads.ollama.httpx.Client", Client)
    with pytest.raises(WorkloadError) as raised:
        OllamaAdapter(OllamaConfig()).chat(
            model="approved:model",
            messages=[{"role": "user", "content": "測試"}],
            think=False,
            keep_alive="0",
            timeout_seconds=30,
        )
    assert raised.value.code == "output_too_large"


def test_training_command_cannot_escape_pinned_upstream(tmp_path):
    root = tmp_path / "GPT-SoVITS"
    (root / "runtime" / "bin").mkdir(parents=True)
    (root / "runtime" / "bin" / "python").touch()
    (root / "GPT_SoVITS").mkdir()
    (root / "GPT_SoVITS" / "s1_train.py").touch()
    (root / "GPT_SoVITS" / "s2_train.py").touch()
    inside = root / "configs" / "train.yaml"
    inside.parent.mkdir()
    inside.touch()
    outside = tmp_path / "outside.yaml"
    outside.touch()
    adapter = GptSoVitsAdapter(GptSoVitsConfig(
        enabled=True,
        url="http://127.0.0.1:9880",
        runtime_root=root,
        model_version="approved-v1",
        reference_audio=tmp_path / "reference.wav",
        reference_text_file=tmp_path / "reference.txt",
    ))
    command = adapter.training_command(stage="gpt", dataset_id="speaker-1", config_file=inside)
    assert command[0].endswith("runtime/bin/python")
    with pytest.raises(WorkloadError, match="outside"):
        adapter.training_command(stage="gpt", dataset_id="speaker-1", config_file=outside)
    with pytest.raises(WorkloadError, match="identifier"):
        adapter.training_command(stage="gpt", dataset_id="../escape", config_file=inside)

    experiment = tmp_path / "checkpoints/run-1"
    experiment.mkdir(parents=True)
    generated = experiment / "train.yaml"
    generated.touch()
    generated_command = adapter.training_command(
        stage="gpt",
        dataset_id="speaker-1",
        config_file=generated,
        allowed_config_root=experiment,
    )
    assert generated_command[0] == str(root / "runtime/bin/python")
    assert generated_command[2] == str(root / "GPT_SoVITS/s1_train.py")


def test_training_pipeline_runs_all_pinned_v2pro_preprocessing_stages(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "GPT-SoVITS"
    runtime.mkdir()
    training_list = tmp_path / "dataset/metadata/train.list"
    training_list.parent.mkdir(parents=True)
    training_list.write_text("/audio.wav|speaker|yue|測試\n")
    recommendation = tmp_path / "dataset/recommended_config.json"
    recommendation.write_text(json.dumps({
        "dataset_readiness": "READY_FOR_BASELINE",
        "gpu_info": "0",
        "precision": "16-mixed",
    }))
    experiment = tmp_path / "checkpoints/speaker-1"
    adapter = GptSoVitsAdapter(GptSoVitsConfig(
        enabled=True,
        runtime_root=runtime,
        model_version="voice-v1",
        reference_audio=tmp_path / "reference.wav",
        reference_text_file=tmp_path / "reference.txt",
    ))
    monkeypatch.setattr(adapter, "training_health", lambda: {"ok": True})
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        joined = " ".join(command)
        if "1-get-text.py" in joined:
            (experiment / "2-name2text-0.txt").write_text("item\ttext\n")
        elif "2-get-hubert-wav32k.py" in joined:
            for name in ("3-bert", "4-cnhubert", "5-wav32k"):
                (experiment / name).mkdir()
                (experiment / name / "item.feature").write_bytes(b"feature")
        elif "3-get-semantic.py" in joined:
            (experiment / "6-name2semantic-0.tsv").write_text("item\t1 2 3\n")
        elif "gpt_sovits_profile_worker.py" in joined:
            profiles = experiment / "profiles"
            profiles.mkdir()
            (profiles / "gpt.yaml").write_text("{}")
            (profiles / "sovits.json").write_text("{}")

    monkeypatch.setattr(adapter, "_run_process", run)
    gpt, sovits = adapter.prepare_training(
        dataset_id="speaker-1",
        training_list=training_list,
        recommendation_file=recommendation,
        experiment_root=experiment,
        timeout_seconds=60,
        cancel_event=threading.Event(),
    )
    assert gpt == experiment / "profiles/gpt.yaml"
    assert sovits == experiment / "profiles/sovits.json"
    assert [
        next((part for part in command if part.endswith(".py")), "")
        for command in commands
    ] == [
        "GPT_SoVITS/prepare_datasets/1-get-text.py",
        "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
        "GPT_SoVITS/prepare_datasets/2-get-sv.py",
        "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
        str(Path(__file__).parents[1] / "workloads/gpt_sovits_profile_worker.py"),
    ]
    assert (experiment / "2-name2text.txt").is_file()
    assert (experiment / "6-name2semantic.tsv").read_text().startswith(
        "item_name\tsemantic_audio\n"
    )


def test_voice_activation_is_explicit_receipted_and_digest_gated(
    tmp_path, monkeypatch, capsys,
):
    runtime = tmp_path / "GPT-SoVITS"
    for relative in (
        "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "GPT_SoVITS/pretrained_models/chinese-hubert-base",
    ):
        (runtime / relative).mkdir(parents=True)
    (runtime / "APPROVED_COMMIT").write_text(
        "d7c2210da8c013e81a94bfc7b811a477c99fd506\n"
    )
    checkpoints = tmp_path / "checkpoints/run-1"
    checkpoints.mkdir(parents=True)
    gpt = checkpoints / "voice.ckpt"
    sovits = checkpoints / "voice.pth"
    gpt.write_bytes(b"gpt-weight")
    sovits.write_bytes(b"sovits-weight")
    reference_audio = tmp_path / "reference.wav"
    reference_text = tmp_path / "reference.txt"
    reference_audio.write_bytes(b"wave")
    reference_text.write_text("參考讀音")
    output = tmp_path / "models/gpt-sovits"
    monkeypatch.setattr("sys.argv", [
        "approve_gpt_sovits_voice.py",
        "--runtime-root", str(runtime),
        "--checkpoint-root", str(tmp_path / "checkpoints"),
        "--output-root", str(output),
        "--gpt-weight", str(gpt),
        "--sovits-weight", str(sovits),
        "--reference-audio", str(reference_audio),
        "--reference-text", str(reference_text),
        "--model-version", "voice-v1",
    ])
    assert approve_gpt_sovits_voice.main() == 0
    assert json.loads(capsys.readouterr().out)["service_restarted"] is False
    adapter = GptSoVitsAdapter(GptSoVitsConfig(
        enabled=True,
        runtime_root=runtime,
        model_version="voice-v1",
        reference_audio=reference_audio,
        reference_text_file=reference_text,
        inference_config=output / "tts_infer.json",
        approval_receipt=output / "active-receipt.json",
    ))
    assert adapter.health()["ok"] is True
    assert adapter.verify_artifacts()["ok"] is True
    gpt.write_bytes(b"tampered!!")
    assert adapter.health()["code"] == "voice_approval_mismatch"


def test_tts_response_is_streamed_with_a_hard_output_bound(tmp_path, monkeypatch):
    reference_audio = tmp_path / "reference.wav"
    reference_text = tmp_path / "reference.txt"
    reference_audio.write_bytes(b"wave")
    reference_text.write_text("參考讀音")
    adapter = GptSoVitsAdapter(GptSoVitsConfig(
        enabled=True,
        url="http://127.0.0.1:9880",
        runtime_root=tmp_path,
        model_version="voice-v1",
        reference_audio=reference_audio,
        reference_text_file=reference_text,
    ))

    class Response:
        headers = {"content-length": "5"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"audio"

    monkeypatch.setattr("workstation.workloads.gpt_sovits.httpx.stream", lambda *_a, **_k: Response())
    result = adapter.synthesize("測試", output_dir=tmp_path)
    assert Path(result["path"]).read_bytes() == b"audio"

    Response.headers = {"content-length": str(5 * 1024 * 1024)}
    with pytest.raises(WorkloadError, match="too large"):
        adapter.synthesize("測試", output_dir=tmp_path)


def test_model_pull_is_explicit_localhost_stream_and_digest_gated(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(('{"status":"pulling manifest"}', '{"status":"success"}'))

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url, json):
            calls.append((method, url, json))
            return Response()

    monkeypatch.setattr("workstation.workloads.ollama.httpx.Client", Client)
    adapter = OllamaAdapter(OllamaConfig(url="http://127.0.0.1:11434"))
    monkeypatch.setattr(adapter, "inventory", lambda: {"approved:latest": "a" * 64})
    adapter.pull_approved(
        "approved:latest",
        expected_digest="a" * 64,
        cancel_event=threading.Event(),
    )
    assert calls == [(
        "POST",
        "http://127.0.0.1:11434/api/pull",
        {"model": "approved:latest", "stream": True, "insecure": False},
    )]

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(WorkloadError) as raised:
        adapter.pull_approved(
            "approved:latest", expected_digest="a" * 64,
            cancel_event=cancelled,
        )
    assert raised.value.code == "cancelled"


def test_asr_uses_pinned_worker_python_and_bounded_result(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"pinned python")
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    adapter = Qwen3AsrAdapter(AsrConfig(
        enabled=True,
        model=str(model),
        runtime_python=runtime,
    ))

    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "workstation.workloads.asr.subprocess.run", lambda *_args, **_kwargs: Completed(),
    )
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **_kwargs):
            commands.append(command)
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps({
                "ok": True, "text": "廣東話轉錄", "language": "Cantonese",
            }))

        def poll(self):
            return 0

    monkeypatch.setattr("workstation.workloads.asr.subprocess.Popen", Process)
    result = adapter.transcribe(audio)
    assert result["text"] == "廣東話轉錄"
    assert commands[0][0] == str(runtime)
    assert commands[0][commands[0].index("--model") + 1] == str(model)
    assert commands[0][commands[0].index("--compute-type") + 1] == "float16"


def test_asr_worker_start_failure_is_typed_and_does_not_leak_local_error(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "runtime/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"pinned python")
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    adapter = Qwen3AsrAdapter(AsrConfig(
        enabled=True,
        model=str(model),
        runtime_python=runtime,
    ))

    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "workstation.workloads.asr.subprocess.run", lambda *_args, **_kwargs: Completed(),
    )
    monkeypatch.setattr(
        "workstation.workloads.asr.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret path")),
    )
    with pytest.raises(WorkloadError) as raised:
        adapter.transcribe(audio)
    assert raised.value.code == "asr_failed"
    assert "secret path" not in str(raised.value)
