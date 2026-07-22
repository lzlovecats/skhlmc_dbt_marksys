from __future__ import annotations

import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import io

from workstation.config import AsrConfig
from workstation.workloads.asr import Qwen3AsrAdapter
from workstation.workloads import asr_worker


ROOT = Path(__file__).resolve().parents[2]


def test_qwen_asr_health_needs_only_local_model_and_official_runtime(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "runtime/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"python")
    model = tmp_path / "Qwen3-ASR-1.7B"
    model.mkdir()
    (model / "config.json").write_text("{}")
    calls = []

    class Completed:
        returncode = 0

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr("workstation.workloads.asr.subprocess.run", run)
    adapter = Qwen3AsrAdapter(AsrConfig(
        enabled=True,
        model=str(model),
        runtime_python=runtime,
    ))

    assert adapter.health() == {
        "ok": True,
        "model": str(model),
        "backend": "qwen3-asr",
    }
    assert calls[0][0][-1] == "from qwen_asr import Qwen3ASRModel"
    assert calls[0][1]["env"]["HF_HUB_OFFLINE"] == "1"


def test_qwen_worker_uses_official_package_with_forced_cantonese(
    tmp_path, monkeypatch,
):
    model = tmp_path / "Qwen3-ASR-1.7B"
    model.mkdir()
    (model / "config.json").write_text("{}")
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF")
    output = tmp_path / "result.json"
    calls = {}

    torch = ModuleType("torch")
    torch.float16 = object()
    torch.bfloat16 = object()
    torch.float32 = object()
    qwen_asr = ModuleType("qwen_asr")

    class Model:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls["load"] = (path, kwargs)
            return cls()

        def transcribe(self, **kwargs):
            calls["transcribe"] = kwargs
            return [SimpleNamespace(text="我方立場成立。", language="Cantonese")]

    qwen_asr.Qwen3ASRModel = Model
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "qwen_asr", qwen_asr)
    monkeypatch.setattr(sys, "argv", [
        "asr_worker.py",
        "--model", str(model),
        "--audio", str(audio),
        "--output", str(output),
        "--device", "cuda",
        "--compute-type", "float16",
    ])

    assert asr_worker.main() == 0
    result = json.loads(output.read_text())
    assert result == {
        "ok": True,
        "text": "我方立場成立。",
        "language": "Cantonese",
    }
    assert calls["load"][0] == str(model.resolve())
    assert calls["load"][1]["device_map"] == "cuda:0"
    assert calls["load"][1]["local_files_only"] is True
    assert calls["load"][1]["max_inference_batch_size"] == 1
    assert calls["transcribe"] == {
        "audio": str(audio.resolve()),
        "language": "Cantonese",
    }


def test_whisper_benchmark_and_approval_paths_are_removed():
    source = (ROOT / "workstation/workloads/asr_worker.py").read_text()
    assert "qwen_asr" in source
    assert "whisper" not in source.casefold()
    for relative in (
        "workstation/scripts/benchmark_asr.py",
        "workstation/scripts/approve_asr_profile.py",
        "workstation/workloads/asr_integrity.py",
        "workstation/config/asr_benchmark_corpus.example.json",
    ):
        assert not (ROOT / relative).exists()


def test_qwen_asr_prewarm_reuses_one_loaded_worker(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"python")
    model = tmp_path / "Qwen3-ASR-1.7B"
    model.mkdir()
    (model / "config.json").write_text("{}")
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF")

    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "workstation.workloads.asr.subprocess.run",
        lambda *_args, **_kwargs: Completed(),
    )
    processes = []

    class Stdin(io.BytesIO):
        def __init__(self, process):
            super().__init__()
            self.process = process

        def close(self):
            command = json.loads(self.getvalue())
            Path(command["output"]).write_text(json.dumps({
                "ok": True, "text": "預載成功", "language": "Cantonese",
            }))
            self.process.returncode = 0
            super().close()

    class Process:
        def __init__(self, command, **_kwargs):
            self.command = command
            self.returncode = None
            self.stdout = io.BytesIO(b"READY\n")
            self.stdin = Stdin(self)
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("workstation.workloads.asr.subprocess.Popen", Process)
    monkeypatch.setattr(
        "workstation.workloads.asr.select.select",
        lambda *_args, **_kwargs: ([processes[-1].stdout], [], []),
    )
    adapter = Qwen3AsrAdapter(AsrConfig(
        enabled=True, model=str(model), runtime_python=runtime,
    ))

    assert adapter.prepare()["prepared"] is True
    assert "--serve" in processes[0].command
    result = adapter.transcribe(audio)
    assert result["text"] == "預載成功"
    assert len(processes) == 1
    assert adapter._prepared_process is None
