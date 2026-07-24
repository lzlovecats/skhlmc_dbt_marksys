from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_model_config import (
    LMC_AI_CONTEXT_LENGTH,
    LMC_AI_FAST_MODEL_TAG,
    LMC_AI_RAG_EMBEDDING_MODEL_TAG,
)
from workstation.config import OllamaConfig, RagConfig, parse_config
from workstation.manager.arbiter import ModeArbiter
from workstation.manager.ipc import ManagerApplication
from workstation.manager.state_store import StateStore
from workstation.node.client import WorkstationNodeClient
from workstation.workloads.errors import WorkloadError
from workstation.workloads.ollama import OllamaAdapter
from workstation.workloads.rag import LocalRagIndex


def test_ollama_chat_sets_the_exact_approved_context(monkeypatch):
    payloads = []

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
            })

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, _method, _url, *, json):
            payloads.append(json)
            return Response()

    monkeypatch.setattr("workstation.workloads.ollama.httpx.Client", Client)

    OllamaAdapter(OllamaConfig()).chat(
        model=LMC_AI_FAST_MODEL_TAG,
        messages=[{"role": "user", "content": "測試"}],
        think=False,
        keep_alive="5m",
        context_length=LMC_AI_CONTEXT_LENGTH,
        timeout_seconds=30,
    )

    assert payloads[0]["options"] == {"num_ctx": LMC_AI_CONTEXT_LENGTH}


def test_embedding_never_silently_truncates_and_can_stay_warm(monkeypatch):
    payloads = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [[1.0, 2.0]]}

    def post(_url, *, json, timeout):
        payloads.append((json, timeout))
        return Response()

    monkeypatch.setattr("workstation.workloads.ollama.httpx.post", post)

    result = OllamaAdapter(OllamaConfig()).embed(
        LMC_AI_RAG_EMBEDDING_MODEL_TAG,
        ["已由人手切成獨立段落"],
        keep_alive="5m",
    )

    assert result == [[1.0, 2.0]]
    assert payloads[0][0]["truncate"] is False
    assert payloads[0][0]["keep_alive"] == "5m"


def test_ollama_unloads_every_resident_model_except_the_target(monkeypatch):
    unloaded = []
    adapter = OllamaAdapter(OllamaConfig())
    monkeypatch.setattr(
        adapter,
        "resident_models",
        lambda: (
            LMC_AI_FAST_MODEL_TAG,
            "gemma4:12b-it-qat",
            LMC_AI_RAG_EMBEDDING_MODEL_TAG,
        ),
    )
    monkeypatch.setattr(adapter, "unload", unloaded.append)

    adapter.unload_except(LMC_AI_FAST_MODEL_TAG)

    assert unloaded == [
        "gemma4:12b-it-qat",
        LMC_AI_RAG_EMBEDDING_MODEL_TAG,
    ]


def test_rag_build_keeps_embedding_warm_across_batches_then_unloads(tmp_path):
    events = []

    class Ollama:
        def unload_except(self, model):
            events.append(("prepare", model))

        def embed(self, model, texts, *, keep_alive="0"):
            events.append(("embed", model, len(texts), keep_alive))
            return [[1.0, float(index + 1)] for index, _text in enumerate(texts)]

        def unload(self, model):
            events.append(("unload", model))

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with (bundle / "documents.jsonl").open("w", encoding="utf-8") as stream:
        for index in range(17):
            stream.write(json.dumps({
                "id": f"document-{index}",
                "title": f"段落 {index}",
                "text": f"已審核而且可以獨立理解的段落 {index}",
                "source_url": "",
            }, ensure_ascii=False) + "\n")

    LocalRagIndex(
        RagConfig(enabled=True, embedding_model=LMC_AI_RAG_EMBEDDING_MODEL_TAG),
        Ollama(),
    ).build(bundle, tmp_path / "output", bundle_version="rag-v1")

    assert events == [
        ("prepare", LMC_AI_RAG_EMBEDDING_MODEL_TAG),
        ("embed", LMC_AI_RAG_EMBEDDING_MODEL_TAG, 16, "5m"),
        ("embed", LMC_AI_RAG_EMBEDDING_MODEL_TAG, 1, "5m"),
        ("unload", LMC_AI_RAG_EMBEDDING_MODEL_TAG),
    ]


def test_node_forwards_only_the_exact_server_context_to_manager():
    async def scenario():
        requests = []
        sent = []

        class Manager:
            async def stream(self, request):
                requests.append(request)
                yield {"event": "result", "usage": {}}

        class Socket:
            async def send(self, raw):
                sent.append(json.loads(raw))

        client = WorkstationNodeClient(SimpleNamespace(), Manager())
        client.websocket = Socket()
        payload = {
            "operation_id": "chat-1",
            "model": LMC_AI_FAST_MODEL_TAG,
            "messages": [{"role": "user", "content": "測試"}],
            "think": False,
            "context_length": LMC_AI_CONTEXT_LENGTH,
        }

        await client.run_chat(payload)

        assert requests[0]["context_length"] == LMC_AI_CONTEXT_LENGTH
        assert sent[-1]["type"] == "chat.complete"

        await client.run_chat({**payload, "operation_id": "chat-2", "context_length": 4096})

        assert len(requests) == 1
        assert sent[-1] == {
            "type": "chat.error",
            "operation_id": "chat-2",
            "code": "context_length_mismatch",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_context", ["8192", 8192.0, True])
def test_manager_rejects_non_integer_chat_context(
    tmp_path, monkeypatch, invalid_context,
):
    config = parse_config({
        "schema_version": 1,
        "node": {
            "name": "AI Workstation",
            "server_url": "https://example.com",
            "token_file": str(tmp_path / "token"),
        },
        "paths": {
            "state": str(tmp_path / "state"),
            "cache": str(tmp_path / "cache"),
            "data": str(tmp_path / "data"),
            "releases": str(tmp_path / "releases"),
        },
        "power": {},
        "workloads": {},
        "gui": {},
    })
    application = ManagerApplication(
        config,
        ModeArbiter(StateStore(tmp_path / "manager.json")),
    )
    calls = []
    monkeypatch.setattr(
        application.executor,
        "run_chat",
        lambda **kwargs: calls.append(kwargs) or ("", {}),
    )

    with pytest.raises(WorkloadError) as raised:
        application._chat({
            "operation_id": "chat-invalid",
            "model": LMC_AI_FAST_MODEL_TAG,
            "messages": [{"role": "user", "content": "測試"}],
            "think": False,
            "context_length": invalid_context,
            "deadline_epoch": 2_000_000_000,
        }, lambda *_args: None)

    assert raised.value.code == "context_length_mismatch"
    assert calls == []


def test_ollama_systemd_profile_allows_only_one_resident_model():
    drop_in = (
        Path(__file__).resolve().parents[1]
        / "packaging/ollama.service.d/lmc-ai-workstation.conf"
    ).read_text()

    assert 'Environment="OLLAMA_MAX_LOADED_MODELS=1"' in drop_in
