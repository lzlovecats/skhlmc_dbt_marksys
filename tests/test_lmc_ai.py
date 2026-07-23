from __future__ import annotations

import asyncio
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pydantic import ValidationError

import ai_model_config
import ai_name
import schema
import system_limits
from api import lmc_ai_api as lmc_api
from api import ai_coach_api
from api.lmc_ai_api import ChatRequest, _resolve_chat_mode
from ai_model_config import (
    LMC_AI_CONTEXT_LENGTH,
    LMC_AI_DAILY_MODEL_TAG,
    LMC_AI_DEEP_MODEL_TAG,
    LMC_AI_DEFAULT_MODE,
    LMC_AI_FALLBACK_MODEL,
    LMC_AI_FAST_MODEL_TAG,
    LMC_AI_FEATURE_MODES,
    LMC_AI_MODE_OPTIONS,
    LMC_AI_PRIMARY_MODEL,
)
from core import (
    config_store,
    funds_logic,
    lmc_ai_client,
    lmc_ai_runtime as lmc_runtime,
    schema_features,
)
from core.lmc_ai_documents import (
    build_docx_export,
    build_markdown_export,
    build_pdf_export,
)
from core.db_migrations import browser_privilege_revokes, created_table_names
from core.lmc_ai_runtime import (
    LocalAIRuntime,
    PERSONA_VERSION,
    QueueFullError,
    SYSTEM_PROMPT,
    backend_fingerprint,
)
from core import lmc_ai_store
from local_ai import lmc_ai_node
from fastapi import HTTPException
from prompts import LMC_AI_PROMPT_TEMPLATES


ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "migrations/20260720_0010_add_lmc_ai_nodes.up.sql"
DOWN = ROOT / "migrations/20260720_0010_add_lmc_ai_nodes.down.sql"
SINGLE_WORKSTATION_UP = (
    ROOT / "migrations/20260722_0003_enforce_single_ai_workstation.up.sql"
)
PAGE = (ROOT / "frontend/lmc_ai/index.html").read_text("utf-8")
SCRIPT = (ROOT / "frontend/lmc_ai/app.js").read_text("utf-8")
DEV_SETTINGS = (ROOT / "frontend/dev_settings/index.html").read_text("utf-8")
API_SOURCE = (ROOT / "api/lmc_ai_api.py").read_text("utf-8")
NODE_SOURCE = (ROOT / "local_ai/lmc_ai_node.py").read_text("utf-8")


def test_identity_and_persona_have_one_runtime_source():
    assert ai_name.LMC_AI_NAME
    assert ai_name.LMC_AI_EMOJI
    assert ai_name.LMC_AI_MENTION_TAG == f"@{ai_name.LMC_AI_NAME}".casefold()
    assert ai_name.LMC_AI_PRACTICE_LABEL == f"同{ai_name.LMC_AI_NAME}練習"
    assert ai_name.LMC_AI_NAME in SYSTEM_PROMPT
    assert ai_name.LMC_AI_EMOJI in SYSTEM_PROMPT
    assert "{{" not in SYSTEM_PROMPT
    assert "system prompt" in SYSTEM_PROMPT
    assert "隱藏推理" in SYSTEM_PROMPT
    assert "由聖呂中辯技術人員開發嘅內部人工智能助手" in SYSTEM_PROMPT
    assert "RAG：未啟用" in SYSTEM_PROMPT
    assert len(PERSONA_VERSION) == 64

    runtime_files = (
        ROOT / "frontend/lmc_ai/index.html",
        ROOT / "frontend/vote/index.html",
        ROOT / "frontend/home/index.html",
        ROOT / "api/lmc_ai_api.py",
        ROOT / "core/lmc_ai_runtime.py",
        ROOT / "core/vote_ai.py",
        ROOT / "frontend/ai_coach/index.html",
        ROOT / "frontend/local_ai_practice/index.html",
    )
    for path in runtime_files:
        source = path.read_text("utf-8")
        assert ai_name.LMC_AI_NAME not in source
        assert ai_name.LMC_AI_EMOJI not in source


def test_persona_enforces_hong_kong_traditional_cantonese_register():
    assert "所有自然語言內容必須使用香港正體中文" in SYSTEM_PROMPT
    assert "絕對唔可以混入簡體字" in SYSTEM_PROMPT
    assert "書面普通話句式" in SYSTEM_PROMPT
    assert "標題、項目符號、表格同例子" in SYSTEM_PROMPT
    assert "輸出前要靜默自查" in SYSTEM_PROMPT
    assert "我哋、佢哋、係咪、冇、睇落、點樣、好多時、離題、評判" in SYSTEM_PROMPT
    assert "我們、他們、是不是、沒有、看起來、如何、很多時候、跑題、法官" in SYSTEM_PROMPT


def test_backend_fingerprint_is_opaque_and_tracks_node_model_and_persona():
    first = backend_fingerprint("node-a", LMC_AI_PRIMARY_MODEL)
    assert len(first) == 64
    assert first != backend_fingerprint("node-b", LMC_AI_PRIMARY_MODEL)
    assert first != backend_fingerprint("node-a", LMC_AI_FALLBACK_MODEL)
    assert first != backend_fingerprint("node-a", LMC_AI_PRIMARY_MODEL, True)


def test_schema_migration_and_bootstrap_are_private_and_fail_safe():
    up = UP.read_text("utf-8")
    down = DOWN.read_text("utf-8")
    assert created_table_names(up) == {"lmc_ai_nodes"}
    assert browser_privilege_revokes(up) == {"lmc_ai_nodes"}
    assert "skhlmc-feature:lmc_ai:20260720_0010" in up
    assert "prompt" not in schema.CREATE_LMC_AI_NODES.lower()
    assert "conversation" not in schema.CREATE_LMC_AI_NODES.lower()
    assert "lmc_ai_chat" in schema.CREATE_AI_FUND_USAGE_LOGS
    assert "provider_duration_ms" in schema.CREATE_AI_FUND_USAGE_LOGS
    assert "ADD COLUMN provider_duration_ms" in up
    assert schema.TABLE_LMC_AI_NODES == "lmc_ai_nodes"
    assert schema.CREATE_LMC_AI_NODES in schema.ALL_SCHEMAS
    assert schema.TABLE_WORKSTATION_R2_HEALTH_PROBES == "workstation_r2_health_probes"
    assert schema.CREATE_WORKSTATION_R2_HEALTH_PROBES in schema.ALL_SCHEMAS
    assert schema_features.FEATURE_MIGRATION_VERSIONS["lmc_ai"] == "20260722_0003"
    assert "uq_lmc_ai_single_enabled_workstation" in schema.CREATE_LMC_AI_NODES
    assert "refusing to remove used local AI node or usage data" in down
    assert "key = 'lmc_ai_active_node_id'" in down
    assert "feature = 'lmc_ai_chat'" in down
    assert down.index("DELETE FROM public.app_config") < down.index("DROP TABLE public.lmc_ai_nodes")
    singleton = SINGLE_WORKSTATION_UP.read_text("utf-8")
    assert "enabled_count > 1" in singleton
    assert "uq_lmc_ai_single_enabled_workstation" in singleton


def test_limits_and_models_are_centralized_at_the_decided_values():
    assert LMC_AI_PRIMARY_MODEL == LMC_AI_FAST_MODEL_TAG
    assert LMC_AI_FALLBACK_MODEL == LMC_AI_DEEP_MODEL_TAG
    assert LMC_AI_DEFAULT_MODE == "fast"
    assert LMC_AI_FAST_MODEL_TAG == "gemma4:e2b-it-qat"
    assert LMC_AI_DAILY_MODEL_TAG == "gemma4:12b-it-qat"
    assert LMC_AI_DEEP_MODEL_TAG == "gemma4:12b-it-qat"
    assert LMC_AI_MODE_OPTIONS == {
        "fast": {"label": "快速回應", "model": LMC_AI_FAST_MODEL_TAG, "thinking": False},
        "daily": {"label": "日常預設", "model": LMC_AI_DAILY_MODEL_TAG, "thinking": False},
        "deep": {"label": "深入思考", "model": LMC_AI_DEEP_MODEL_TAG, "thinking": True},
    }
    assert LMC_AI_FEATURE_MODES == {
        "lmc_ai": "fast", "vote": "fast", "ai_coach": "fast",
    }
    runbook = (ROOT / "docs/AI_WORKSTATION_RUNBOOK.md").read_text("utf-8")
    assert "lmc_ai_required_models" in runbook
    for tag in {
        LMC_AI_FAST_MODEL_TAG,
        LMC_AI_DAILY_MODEL_TAG,
        LMC_AI_DEEP_MODEL_TAG,
    }:
        assert tag not in runbook
    assert ai_coach_api.AI_COACH_LOCAL_MODE == LMC_AI_FEATURE_MODES["ai_coach"]
    assert LMC_AI_CONTEXT_LENGTH == 8192
    assert system_limits.LMC_AI_NODE_MAX == 1
    assert system_limits.LMC_AI_QUEUE_MAX == 2
    assert system_limits.LMC_AI_ACTIVE_GENERATIONS == 1
    assert system_limits.LMC_AI_MESSAGE_MAX_CHARS == 3000
    assert system_limits.LMC_AI_CONTEXT_MAX_CHARS == 3000
    assert system_limits.LMC_AI_REQUEST_MESSAGES_MAX == 40
    assert system_limits.LMC_AI_REQUEST_TIMEOUT_SECONDS == 180
    assert system_limits.LMC_AI_OUTPUT_MAX_BYTES == 256 * 1024
    assert system_limits.LMC_AI_BROWSER_HISTORY_MAX_MESSAGES == 100
    assert system_limits.LMC_AI_BROWSER_HISTORY_MAX_CHARS == 200_000
    assert system_limits.LMC_AI_BROWSER_CONVERSATION_MAX == 20
    assert system_limits.LMC_AI_BROWSER_DOCUMENT_MAX == 20
    assert "lmc_ai_chat" in funds_logic.AI_USAGE_FEATURES
    assert funds_logic.AI_FEATURE_LABELS["lmc_ai_chat"]


def test_workstation_r2_health_claim_is_node_scoped_verified_and_deleted(
    monkeypatch,
):
    from core import r2_storage
    from deploy import proxy

    request = SimpleNamespace(headers={"authorization": "Bearer node-token"})
    db = object()
    monkeypatch.setattr(lmc_api, "_db", lambda: db)
    monkeypatch.setattr(
        lmc_api, "authenticate_node",
        lambda _db, token: {"node_id": "node-1"} if token == "node-token" else None,
    )
    monkeypatch.setattr(proxy, "_get_relay_cookie_secret", lambda: "claim-secret")
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "sign_upload_claim", lambda claim, _secret: "x" * 40)
    issued = {}
    monkeypatch.setattr(
        r2_storage,
        "reserve_workstation_r2_health_probe",
        lambda used_db, **values: issued.update({"db": used_db, **values}) or True,
    )
    monkeypatch.setattr(
        r2_storage, "presign_put",
        lambda key, mime, sha, size: issued.update(
            {"key": key, "mime": mime, "sha": sha, "size": size}
        ) or "https://r2.example/upload",
    )
    monkeypatch.setattr(
        r2_storage, "presign_get",
        lambda key, **_kwargs: "https://r2.example/download",
    )
    digest = "a" * 64
    started = lmc_api.workstation_r2_health_start(
        lmc_api.WorkstationR2ProbeStart(
            sha256=digest,
            byte_size=system_limits.WORKSTATION_R2_HEALTH_PROBE_BYTES,
        ),
        request,
    )
    payload = json.loads(started.body)
    assert payload["claim"] == "x" * 40
    assert issued["key"].startswith("pending/workstation-health/node-1/")
    assert issued["sha"] == digest
    assert issued["db"] is db
    assert issued["intent_id"] in issued["key"]
    assert issued["object_key"] == issued["key"]

    claim = {
        "kind": "workstation_r2_health",
        "intent_id": issued["intent_id"],
        "node_id": "node-1",
        "key": issued["key"],
        "sha256": digest,
        "byte_size": system_limits.WORKSTATION_R2_HEALTH_PROBE_BYTES,
    }
    deleted = []
    monkeypatch.setattr(r2_storage, "verify_upload_claim", lambda _value, _secret: claim)
    monkeypatch.setattr(
        r2_storage,
        "get_workstation_r2_health_probe",
        lambda used_db, **_values: {
            "object_key": issued["key"],
            "sha256": digest,
            "byte_size": system_limits.WORKSTATION_R2_HEALTH_PROBE_BYTES,
        } if used_db is db else None,
    )
    monkeypatch.setattr(
        r2_storage,
        "head",
        lambda _key: {
            "ContentLength": system_limits.WORKSTATION_R2_HEALTH_PROBE_BYTES,
            "Metadata": {"sha256": digest},
        },
    )
    monkeypatch.setattr(
        r2_storage,
        "delete_workstation_r2_health_probe",
        lambda used_db, **values: deleted.append((used_db, values)) or True,
    )
    assert lmc_api.workstation_r2_health_finish(
        lmc_api.WorkstationR2ProbeFinish(claim="y" * 40), request,
    ) == {"ok": True, "deleted": True}
    assert deleted == [(db, {
        "intent_id": issued["intent_id"],
        "node_id": "node-1",
        "object_key": issued["key"],
    })]

    deleted.clear()
    monkeypatch.setattr(
        r2_storage, "head",
        lambda _key: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    with pytest.raises(HTTPException) as raised:
        lmc_api.workstation_r2_health_finish(
            lmc_api.WorkstationR2ProbeFinish(claim="h" * 40), request,
        )
    assert raised.value.status_code == 409
    assert deleted == [(db, {
        "intent_id": issued["intent_id"],
        "node_id": "node-1",
        "object_key": issued["key"],
    })]

    monkeypatch.setattr(
        r2_storage,
        "delete_workstation_r2_health_probe",
        lambda _db, **_values: False,
    )
    monkeypatch.setattr(
        r2_storage,
        "head",
        lambda _key: {
            "ContentLength": system_limits.WORKSTATION_R2_HEALTH_PROBE_BYTES,
            "Metadata": {"sha256": digest},
        },
    )
    with pytest.raises(HTTPException) as raised:
        lmc_api.workstation_r2_health_finish(
            lmc_api.WorkstationR2ProbeFinish(claim="d" * 40), request,
        )
    assert raised.value.status_code == 409

    monkeypatch.setattr(
        r2_storage, "reserve_workstation_r2_health_probe",
        lambda _db, **_values: False,
    )
    with pytest.raises(HTTPException) as raised:
        lmc_api.workstation_r2_health_start(
            lmc_api.WorkstationR2ProbeStart(
                sha256=digest,
                byte_size=system_limits.WORKSTATION_R2_HEALTH_PROBE_BYTES,
            ),
            request,
        )
    assert raised.value.status_code == 409

    claim["node_id"] = "node-2"
    with pytest.raises(HTTPException) as raised:
        lmc_api.workstation_r2_health_finish(
            lmc_api.WorkstationR2ProbeFinish(claim="z" * 40), request,
        )
    assert raised.value.status_code == 400


class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_text(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, **kwargs):
        self.closed.append(kwargs)


async def _noop_finish(_job, _success, _usage, _error):
    return None


def _hello(**overrides):
    value = {
        "type": "hello",
        "protocol": 1,
        "model_profile_version": ai_model_config.LMC_AI_MODEL_PROFILE_VERSION,
        "name": "PopOS AI 01",
        "runtime": "ollama",
        "runtime_version": "0.12",
        "model": LMC_AI_PRIMARY_MODEL,
        "models": [LMC_AI_PRIMARY_MODEL, LMC_AI_FALLBACK_MODEL],
        "ready": True,
        "draining": False,
        "capabilities": {
            "chat": True,
            "rag": False,
            "fine_tuned": False,
            "thinking_control": True,
        },
    }
    value.update(overrides)
    return value


def _workstation_hello(**overrides):
    value = _hello(
        protocol=2,
        workstation_version="1.0.0",
        runtime="lmc-ai-workstation",
        runtime_version="1.0.0",
        capabilities={
            "chat": True,
            "rag": True,
            "asr": True,
            "local_tts": True,
            "tts_training": True,
            "direct_r2": True,
            "fine_tuned": False,
            "thinking_control": True,
            "manager": True,
        },
        manager={
            "revision": 7,
            "mode": "idle",
            "draining": False,
            "voice_session_active": False,
            "voice_session_pending": False,
            "active_operation": None,
            "sleep_inhibited": False,
            "reconcile_required": False,
            "last_error_code": "",
            "updated_epoch": 1,
        },
    )
    value.update(overrides)
    return value


def test_runtime_enforces_fifo_capacity_and_server_owned_prompt():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _hello())
        fingerprint = node.fingerprint

        jobs = []
        for index in range(3):
            job, position = await runtime.submit(
                node_id="node-1",
                expected_fingerprint=fingerprint,
                actor_id=f"member-{index}",
                usage_user_id=f"member-{index}",
                operation_stage="member_chat",
                messages=[{"role": "user", "content": f"message {index}"}],
                finish_callback=_noop_finish,
                has_history=index > 0,
            )
            jobs.append(job)
            assert position == index
        with pytest.raises(QueueFullError):
            await runtime.submit(
                node_id="node-1",
                expected_fingerprint=fingerprint,
                actor_id="overflow",
                usage_user_id="overflow",
                operation_stage="member_chat",
                messages=[{"role": "user", "content": "full"}],
                finish_callback=_noop_finish,
            )

        start = socket.sent[0]
        assert start["type"] == "chat.start"
        assert start["operation_id"] == jobs[0].operation_id
        assert start["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
        assert start["think"] is False
        assert start["context_length"] == 8192
        assert start["allow_model_fallback"] is False
        assert start["model"] == LMC_AI_PRIMARY_MODEL
        assert "num_predict" not in start
        assert "max_tokens" not in start

        await runtime.handle_node_message(
            node, {"type": "chat.started", "operation_id": jobs[0].operation_id}
        )
        await runtime.handle_node_message(
            node,
            {
                "type": "chat.complete",
                "operation_id": jobs[0].operation_id,
                "model": LMC_AI_PRIMARY_MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
        assert socket.sent[1]["operation_id"] == jobs[1].operation_id
        assert socket.sent[1]["allow_model_fallback"] is False
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_runtime_sends_server_selected_thinking_mode_to_the_node():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _hello())
        job, position = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=backend_fingerprint(
                "node-1", LMC_AI_PRIMARY_MODEL, True
            ),
            actor_id="member-1",
            usage_user_id="member-1",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "reason carefully"}],
            finish_callback=_noop_finish,
            thinking_enabled=True,
        )

        assert position == 0
        assert job.thinking_enabled is True
        assert socket.sent[0]["think"] is True
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_runtime_records_usage_when_thinking_finishes_without_a_final_answer():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        finished = []

        async def finish(_job, success, usage, error):
            finished.append((success, usage, error))

        node = await runtime.register("node-1", socket, _hello())
        job, _position = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=backend_fingerprint(
                "node-1", LMC_AI_PRIMARY_MODEL, True
            ),
            actor_id="member-1",
            usage_user_id="member-1",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "reason carefully"}],
            finish_callback=finish,
            thinking_enabled=True,
        )
        await runtime.handle_node_message(node, {
            "type": "chat.started",
            "operation_id": job.operation_id,
            "model": LMC_AI_PRIMARY_MODEL,
        })
        await runtime.handle_node_message(node, {
            "type": "chat.error",
            "operation_id": job.operation_id,
            "code": "empty_response",
            "model": LMC_AI_PRIMARY_MODEL,
            "usage": {
                "input_tokens": 721,
                "output_tokens": 7471,
                "duration_ms": 55_000,
            },
        })

        assert await job.events.get() == ("status", {"state": "starting"})
        assert await job.events.get() == ("status", {"state": "generating"})
        event, payload = await job.events.get()
        assert event == "error"
        assert "未有產生正式答案" in payload["message"]
        assert finished == [(
            False,
            {
                "input_tokens": 721,
                "output_tokens": 7471,
                "duration_ms": 55_000,
                "model": LMC_AI_PRIMARY_MODEL,
            },
            "AI 思考完成但未有產生正式答案，請改用日常模式或開始新對話再試。",
        )]
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_runtime_routes_deep_mode_to_12b_and_refuses_incomplete_model_profile():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _hello())
        job, _ = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=backend_fingerprint(
                "node-1", LMC_AI_FALLBACK_MODEL, True
            ),
            actor_id="member-1",
            usage_user_id="member-1",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "deep"}],
            finish_callback=_noop_finish,
            model=LMC_AI_FALLBACK_MODEL,
            thinking_enabled=True,
        )
        assert job.model == LMC_AI_FALLBACK_MODEL
        assert socket.sent[0]["model"] == LMC_AI_FALLBACK_MODEL
        assert socket.sent[0]["think"] is True
        await runtime.unregister(node, "test cleanup")

        limited = LocalAIRuntime()
        limited_socket = _FakeWebSocket()
        with pytest.raises(ValueError, match="model profile is incomplete"):
            await limited.register(
                "node-2", limited_socket,
                _hello(models=[LMC_AI_PRIMARY_MODEL]),
            )

    asyncio.run(scenario())


def test_shared_availability_requires_configured_ready_workstation_and_tracks_modes(monkeypatch):
    runtime = LocalAIRuntime()
    monkeypatch.setattr(lmc_ai_client, "RUNTIME", runtime)
    monkeypatch.setattr(lmc_ai_client, "require_lmc_ai_schema", lambda _db: None)
    configured = {"node_id": ""}
    monkeypatch.setattr(
        lmc_ai_client, "get_workstation_id", lambda _db: configured["node_id"],
    )

    async def scenario():
        missing = await lmc_ai_client.local_ai_availability(object())
        assert missing["available"] is False
        assert missing["configured"] is False
        assert "尚未設定" in missing["message"]

        configured["node_id"] = "node-1"
        offline = await lmc_ai_client.local_ai_availability(object())
        assert offline["state"] == "offline"
        assert "離線" in offline["message"]

        socket = _FakeWebSocket()
        node = await runtime.register(
            "node-1", socket,
            _hello(),
        )
        node.active = object()
        busy = await lmc_ai_client.local_ai_availability(object())
        assert busy["available"] is True
        assert busy["state"] == "busy"
        modes = {item["id"]: item for item in busy["modes"]}
        assert modes["fast"]["available"] is True
        assert modes["daily"]["available"] is True
        assert modes["deep"]["available"] is True

        node.queue.extend([object()] * system_limits.LMC_AI_QUEUE_MAX)
        full = await lmc_ai_client.local_ai_availability(object())
        assert full["available"] is False
        assert full["state"] == "full"
        assert "排隊已滿" in full["message"]
        node.queue.clear()

        node.active = None
        node.draining = True
        draining = await lmc_ai_client.local_ai_availability(object())
        assert draining["available"] is False
        assert draining["state"] == "draining"
        assert "暫停接單" in draining["message"]
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_shared_local_client_bridges_sync_worker_loop_to_runtime_owner(monkeypatch):
    runtime = LocalAIRuntime()
    owner_loop = asyncio.new_event_loop()
    owner_ready = threading.Event()
    holder = {}

    class _ReplyingWebSocket(_FakeWebSocket):
        async def send_text(self, raw):
            await super().send_text(raw)
            payload = self.sent[-1]
            if payload.get("type") != "chat.start":
                return
            operation_id = payload["operation_id"]
            node = holder["node"]
            await runtime.handle_node_message(node, {
                "type": "chat.started",
                "operation_id": operation_id,
                "model": payload["model"],
            })
            await runtime.handle_node_message(node, {
                "type": "chat.delta",
                "operation_id": operation_id,
                "text": "跨 loop 回覆",
            })
            await runtime.handle_node_message(node, {
                "type": "chat.complete",
                "operation_id": operation_id,
                "model": payload["model"],
                "usage": {
                    "input_tokens": 6,
                    "output_tokens": 4,
                    "duration_ms": 25,
                },
            })

    def run_owner_loop():
        asyncio.set_event_loop(owner_loop)

        async def register():
            socket = _ReplyingWebSocket()
            holder["socket"] = socket
            holder["node"] = await runtime.register(
                "node-1", socket, _hello(),
            )

        owner_loop.run_until_complete(register())
        owner_ready.set()
        owner_loop.run_forever()

    thread = threading.Thread(target=run_owner_loop, daemon=True)
    thread.start()
    assert owner_ready.wait(timeout=2)
    monkeypatch.setattr(lmc_ai_client, "RUNTIME", runtime)
    monkeypatch.setattr(lmc_ai_client, "require_lmc_ai_schema", lambda _db: None)
    monkeypatch.setattr(lmc_ai_client, "get_workstation_id", lambda _db: "node-1")
    attempts = []
    try:
        text, usage = asyncio.run(lmc_ai_client.generate_local_text(
            object(),
            actor_id="vote-ai",
            system_prompt="system",
            user_prompt="user",
            mode="complex",
            operation_stage="vote_review",
            on_provider_attempt=lambda: attempts.append(True),
        ))
        assert text == "跨 loop 回覆"
        assert usage["input_tokens"] == 6
        assert usage["output_tokens"] == 4
        assert usage["cost_source"] == "local_zero_cost"
        assert attempts == [True]
        start = next(
            item for item in holder["socket"].sent if item.get("type") == "chat.start"
        )
        assert start["model"] == LMC_AI_FALLBACK_MODEL
        assert start["think"] is False
    finally:
        async def cleanup():
            await runtime.unregister(holder["node"], "test cleanup")
            await asyncio.sleep(0)

        asyncio.run_coroutine_threadsafe(cleanup(), owner_loop).result(timeout=2)
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        thread.join(timeout=2)
        owner_loop.close()


def test_draining_workstation_keeps_active_generation_and_fails_only_queue():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _hello())
        active, _ = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=node.fingerprint,
            actor_id="member-1",
            usage_user_id="member-1",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "active"}],
            finish_callback=_noop_finish,
        )
        queued, _ = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=node.fingerprint,
            actor_id="member-2",
            usage_user_id="member-2",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "queued"}],
            finish_callback=_noop_finish,
        )

        await runtime.block_new_jobs("node-1")
        await runtime.fail_queued("node-1", "workstation draining")

        assert node.active is active
        assert list(node.queue) == []
        assert queued.finished is True
        assert await queued.events.get() == ("queued", {"position": 1})
        assert await queued.events.get() == ("error", {"message": "workstation draining"})
        assert not any(item.get("type") == "chat.cancel" for item in socket.sent)
        with pytest.raises(lmc_runtime.NodeUnavailableError):
            await runtime.submit(
                node_id="node-1",
                expected_fingerprint=node.fingerprint,
                actor_id="member-3",
                usage_user_id="member-3",
                operation_stage="member_chat",
                messages=[{"role": "user", "content": "late arrival"}],
                finish_callback=_noop_finish,
            )
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_forced_timeout_waits_for_node_cancel_ack_before_starting_next_job(
    monkeypatch,
):
    monkeypatch.setattr(lmc_runtime, "LMC_AI_REQUEST_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _hello())
        first, _ = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=node.fingerprint,
            actor_id="member-1",
            usage_user_id="member-1",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "first"}],
            finish_callback=_noop_finish,
        )
        second, _ = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=node.fingerprint,
            actor_id="member-2",
            usage_user_id="member-2",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "second"}],
            finish_callback=_noop_finish,
        )
        second.timeout_task.cancel()
        await runtime.handle_node_message(
            node, {"type": "chat.started", "operation_id": first.operation_id}
        )

        await asyncio.sleep(0.03)

        assert any(
            item.get("type") == "chat.cancel"
            and item.get("operation_id") == first.operation_id
            for item in socket.sent
        )
        assert not any(
            item.get("type") == "chat.start"
            and item.get("operation_id") == second.operation_id
            for item in socket.sent
        )

        await runtime.handle_node_message(
            node,
            {
                "type": "chat.error",
                "operation_id": first.operation_id,
                "code": "cancelled",
            },
        )
        assert any(
            item.get("type") == "chat.start"
            and item.get("operation_id") == second.operation_id
            for item in socket.sent
        )
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_node_hello_refuses_future_or_unsafe_capabilities():
    assert LocalAIRuntime.validate_hello(_hello())["ready"] is True
    with pytest.raises(ValueError):
        LocalAIRuntime.validate_hello(_hello(protocol=2))
    with pytest.raises(ValueError):
        LocalAIRuntime.validate_hello(
            _hello(capabilities={"chat": True, "rag": True, "fine_tuned": False})
        )
    with pytest.raises(ValueError):
        LocalAIRuntime.validate_hello(
            _hello(
                capabilities={"chat": True, "rag": False, "fine_tuned": False}
            )
        )


def test_workstation_protocol_v2_hello_is_strict_and_sanitized():
    clean = LocalAIRuntime.validate_hello(_workstation_hello())
    assert clean["protocol"] == 2
    assert clean["workstation_version"] == "1.0.0"
    assert clean["manager"]["mode"] == "idle"
    assert clean["capabilities"]["direct_r2"] is True
    without_r2 = _workstation_hello()
    without_r2["capabilities"]["direct_r2"] = False
    assert LocalAIRuntime.validate_hello(without_r2)["capabilities"]["direct_r2"] is False

    unsafe = _workstation_hello()
    unsafe["capabilities"] = {**unsafe["capabilities"], "shell": True}
    with pytest.raises(ValueError, match="Workstation capabilities"):
        LocalAIRuntime.validate_hello(unsafe)
    with pytest.raises(ValueError, match="manager mode"):
        LocalAIRuntime.validate_hello(_workstation_hello(manager={"mode": "unknown"}))


def test_workstation_manager_mode_blocks_new_text_jobs():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        hello = _workstation_hello()
        hello["manager"] = {**hello["manager"], "mode": "voice_coach"}
        node = await runtime.register("node-1", socket, hello)

        with pytest.raises(lmc_runtime.NodeUnavailableError, match="語音或維護"):
            await runtime.submit(
                node_id="node-1",
                expected_fingerprint=node.fingerprint,
                actor_id="member-1",
                usage_user_id="member-1",
                operation_stage="member_chat",
                messages=[{"role": "user", "content": "new text job"}],
                finish_callback=_noop_finish,
            )
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_workstation_job_upload_handshake_and_terminal_result():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _workstation_hello())
        uploads = []
        verified_uploads = []

        async def authorize(job, media):
            uploads.append((job.operation_id, media))
            return {
                "intent_id": "intent-1",
                "upload_url": "https://r2.example.invalid/upload",
                "headers": {"content-type": "audio/wav"},
            }

        async def verify_upload(job, result):
            verified_uploads.append((job.operation_id, result))
            return dict(result)

        job = await runtime.submit_workstation(
            node_id="node-1",
            operation_id="tts.session-1.turn-1",
            job_kind="tts",
            session_id="session-1",
            turn_id="turn-1",
            stage="synthesis",
            payload={"text": "測試讀音"},
            upload_callback=authorize,
            upload_finish_callback=verify_upload,
        )
        assert socket.sent[-1]["type"] == "workstation.job.start"

        await runtime.handle_node_message(node, {
            "type": "workstation.job.started",
            "operation_id": job.operation_id,
        })
        media = {
            "mime_type": "audio/wav",
            "size_bytes": 4096,
            "sha256": "a" * 64,
            "duration_ms": 800,
        }
        await runtime.handle_node_message(node, {
            "type": "workstation.upload.request",
            "operation_id": job.operation_id,
            "media": media,
        })
        assert uploads == [(job.operation_id, media)]
        assert socket.sent[-1] == {
            "type": "workstation.upload.authorized",
            "operation_id": job.operation_id,
            "intent_id": "intent-1",
            "upload_url": "https://r2.example.invalid/upload",
            "headers": {"content-type": "audio/wav"},
        }

        result = {"intent_id": "intent-1", "uploaded": True}
        await runtime.handle_node_message(node, {
            "type": "workstation.upload.complete",
            "operation_id": job.operation_id,
            "result": result,
        })
        assert verified_uploads == [(job.operation_id, result)]
        assert socket.sent[-1] == {
            "type": "workstation.upload.verified",
            "operation_id": job.operation_id,
        }
        await runtime.handle_node_message(node, {
            "type": "workstation.job.complete",
            "operation_id": job.operation_id,
            "result": result,
        })
        assert await job.events.get() == ("started", {"job_kind": "tts"})
        assert await job.events.get() == (
            "complete",
            {"result": result},
        )
        assert node.workstation_active is None
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_workstation_tts_cannot_finish_before_server_verifies_r2_output():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _workstation_hello())

        async def authorize(_job, _media):
            return {"intent_id": "intent-1", "upload": {"url": "https://r2.invalid"}}

        job = await runtime.submit_workstation(
            node_id="node-1",
            operation_id="tts.unverified",
            job_kind="tts",
            session_id="session-1",
            turn_id="turn-1",
            stage="synthesis",
            payload={"text": "測試"},
            upload_callback=authorize,
            upload_finish_callback=lambda _job, _result: None,
        )
        await runtime.handle_node_message(node, {
            "type": "workstation.job.complete",
            "operation_id": job.operation_id,
            "result": {"output": {"intent_id": "intent-1"}},
        })
        assert await job.events.get() == (
            "error",
            {"message": "自家讀音上載尚未完成 server 驗證。"},
        )
        assert node.workstation_active is None
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_remote_control_has_a_separate_slot_from_voice_work():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _workstation_hello())
        voice = await runtime.submit_workstation(
            node_id="node-1",
            operation_id="asr.session-1.turn-1",
            job_kind="asr.prepare",
            session_id="session-1",
            turn_id="turn-1",
            stage="asr_model_load",
            payload={},
        )
        control = await runtime.submit_workstation(
            node_id="node-1",
            operation_id="control.drain-1",
            job_kind="control",
            session_id="remote-control",
            turn_id="",
            stage="drain",
            payload={"command": {"action": "drain"}},
        )
        assert node.workstation_active is voice
        assert node.control_active is control
        assert [frame["job_kind"] for frame in socket.sent[-2:]] == [
            "asr.prepare", "control",
        ]
        await runtime.handle_node_message(node, {
            "type": "workstation.job.complete",
            "operation_id": control.operation_id,
            "result": {"manager": {"draining": True}},
        })
        assert node.control_active is None
        assert node.workstation_active is voice
        await runtime.cancel_workstation("node-1", voice)
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_workstation_jobs_require_v2_and_voice_reservation_may_wait_for_text():
    async def scenario():
        legacy_runtime = LocalAIRuntime()
        legacy_socket = _FakeWebSocket()
        legacy = await legacy_runtime.register("legacy", legacy_socket, _hello())
        with pytest.raises(lmc_runtime.NodeUnavailableError):
            await legacy_runtime.submit_workstation(
                node_id="legacy",
                operation_id="reserve.legacy",
                job_kind="voice.reserve",
                session_id="session-1",
                turn_id="",
                stage="reserve",
                payload={},
            )
        await legacy_runtime.unregister(legacy, "test cleanup")

        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _workstation_hello())
        chat, _ = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=node.fingerprint,
            actor_id="member-1",
            usage_user_id="member-1",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "finish me first"}],
            finish_callback=_noop_finish,
        )
        queued, position = await runtime.submit(
            node_id="node-1",
            expected_fingerprint=node.fingerprint,
            actor_id="member-2",
            usage_user_id="member-2",
            operation_stage="member_chat",
            messages=[{"role": "user", "content": "queued text"}],
            finish_callback=_noop_finish,
        )
        assert position == 1
        reserve = await runtime.submit_workstation(
            node_id="node-1",
            operation_id="reserve.session-1",
            job_kind="voice.reserve",
            session_id="session-1",
            turn_id="",
            stage="reserve",
            payload={},
        )
        assert await queued.events.get() == ("queued", {"position": 1})
        assert (await queued.events.get())[0] == "error"
        with pytest.raises(lmc_runtime.NodeUnavailableError):
            await runtime.submit(
                node_id="node-1",
                expected_fingerprint=node.fingerprint,
                actor_id="member-3",
                usage_user_id="member-3",
                operation_stage="member_chat",
                messages=[{"role": "user", "content": "new text"}],
                finish_callback=_noop_finish,
            )
        await runtime.handle_node_message(node, {
            "type": "workstation.job.complete",
            "operation_id": reserve.operation_id,
            "result": {"reserved": True},
        })
        with pytest.raises(lmc_runtime.WorkstationBusyError, match="文字工作"):
            await runtime.submit_workstation(
                node_id="node-1",
                operation_id="asr.session-1.turn-1",
                job_kind="asr",
                session_id="session-1",
                turn_id="turn-1",
                stage="transcribe",
                payload={"download_url": "https://r2.example.invalid/audio"},
            )
        await runtime.cancel("node-1", chat)
        await runtime.unregister(node, "test cleanup")

    asyncio.run(scenario())


def test_node_hello_requires_current_complete_model_profile():
    assert ai_model_config.LMC_AI_MODEL_PROFILE_VERSION == 6
    assert LocalAIRuntime.validate_hello(_hello())["ready"] is True

    missing_profile = _hello()
    missing_profile.pop("model_profile_version")
    with pytest.raises(ValueError, match="model profile"):
        LocalAIRuntime.validate_hello(missing_profile)
    with pytest.raises(ValueError, match="model profile"):
        LocalAIRuntime.validate_hello(_hello(model_profile_version=1))
    with pytest.raises(ValueError, match="model profile is incomplete"):
        LocalAIRuntime.validate_hello(_hello(
            model=LMC_AI_FALLBACK_MODEL,
            models=[LMC_AI_FALLBACK_MODEL],
        ))
    gemma_models = list(ai_model_config.lmc_ai_required_models())
    clean = LocalAIRuntime.validate_hello(_hello(
        model=gemma_models[0], models=gemma_models,
    ))
    assert clean["model"] == LMC_AI_FAST_MODEL_TAG

    with pytest.raises(ValueError, match="runtime identity"):
        LocalAIRuntime.validate_hello(_workstation_hello(
            runtime="ollama", runtime_version="1.0.0",
        ))

    assert '"model_profile_version": MODEL_PROFILE_VERSION' in NODE_SOURCE


def test_node_websocket_accepts_current_profile_after_sanitizing_hello(monkeypatch):
    class NodeSocket(_FakeWebSocket):
        query_params = {}
        headers = {"authorization": "Bearer node-token"}

        def __init__(self):
            super().__init__()
            self.accepted = False
            self.incoming = [json.dumps(_hello(model_digests={
                LMC_AI_PRIMARY_MODEL: "a" * 64,
                LMC_AI_FALLBACK_MODEL: "b" * 64,
            }))]

        async def accept(self):
            self.accepted = True

        async def receive_text(self):
            if self.incoming:
                return self.incoming.pop(0)
            raise lmc_api.WebSocketDisconnect()

    runtime = LocalAIRuntime()
    socket = NodeSocket()
    updated = []
    monkeypatch.setattr(lmc_api, "RUNTIME", runtime)
    monkeypatch.setattr(lmc_api, "_db", lambda: object())
    monkeypatch.setattr(
        lmc_api, "authenticate_node",
        lambda _db, _token: {"node_id": "node-1", "display_name": "AI 01"},
    )
    monkeypatch.setattr(
        lmc_api, "update_node_hello",
        lambda _db, _node_id, _token, hello: updated.append(hello),
    )
    monkeypatch.setattr(lmc_api, "mark_node_disconnected", lambda _db, _node_id: None)

    asyncio.run(lmc_api.lmc_ai_node_connect(socket))

    assert socket.accepted is True
    assert updated and updated[0]["model"] == LMC_AI_PRIMARY_MODEL
    assert any(item.get("type") == "hello.accepted" for item in socket.sent)
    assert not any(item.get("code") == 1007 for item in socket.closed)


def test_pending_node_cannot_serve_or_activate_after_concurrent_revocation():
    async def scenario():
        runtime = LocalAIRuntime()
        socket = _FakeWebSocket()
        node = await runtime.register("node-1", socket, _hello(), pending=True)
        assert (await runtime.snapshot("node-1"))["ready"] is False

        await runtime.disconnect_node("node-1", "revoked during hello")

        assert await runtime.activate(node, ready=True, draining=False) is False
        assert await runtime.snapshot("node-1") is None

    asyncio.run(scenario())


def test_node_tokens_are_random_once_and_only_digest_reaches_database(monkeypatch):
    class Db:
        params = None

        def query(self, _sql, _params=None):
            return pd.DataFrame([{"count": 0}])

        def execute(self, _sql, params=None):
            self.params = params

    monkeypatch.setattr(lmc_ai_store, "require_lmc_ai_schema", lambda _db: None)
    db = Db()
    node, token = lmc_ai_store.create_node(db, "PopOS AI 01")
    assert node["display_name"] == "PopOS AI 01"
    assert token
    assert db.params["token_hash"] != token
    assert len(db.params["token_hash"]) == 64
    assert token not in json.dumps(db.params)


def test_store_refuses_a_second_enabled_workstation(monkeypatch):
    class Db:
        def query(self, _sql, _params=None):
            return pd.DataFrame([{"count": 1}])

        def execute(self, _sql, _params=None):
            raise AssertionError("a second enabled Workstation must not be inserted")

    monkeypatch.setattr(lmc_ai_store, "require_lmc_ai_schema", lambda _db: None)
    with pytest.raises(ValueError, match="已經設定"):
        lmc_ai_store.create_node(Db(), "Second AI")


def test_node_hello_revalidates_the_current_token_before_registration(monkeypatch):
    class Db:
        sql = ""
        params = None

        def execute_count(self, sql, params=None):
            self.sql = sql
            self.params = params
            return 0

    monkeypatch.setattr(lmc_ai_store, "require_lmc_ai_schema", lambda _db: None)
    db = Db()
    with pytest.raises(LookupError, match="憑證"):
        lmc_ai_store.update_node_hello(db, "node-1", "old-token", _hello())
    assert "token_hash=:token_hash" in db.sql
    assert db.params["token_hash"] == lmc_ai_store._token_digest("old-token")


def test_chat_mode_is_allowlisted_and_legacy_requests_remain_distinguishable():
    request = {
        "messages": [{"role": "user", "content": "hello"}],
        "expected_fingerprint": "0" * 64,
        "has_history": False,
    }
    assert ChatRequest(**request).mode is None
    assert ChatRequest(**request, mode="thinking").mode == "thinking"
    assert ChatRequest(**request, mode="fast").mode == "fast"
    assert _resolve_chat_mode(None)[0] == "fast"
    assert _resolve_chat_mode("thinking") == ("deep", LMC_AI_MODE_OPTIONS["deep"])
    assert _resolve_chat_mode("complex") == ("daily", LMC_AI_MODE_OPTIONS["daily"])
    assert _resolve_chat_mode("fast") == ("fast", LMC_AI_MODE_OPTIONS["fast"])
    with pytest.raises(ValidationError):
        ChatRequest(**request, mode="unlimited")


def test_developer_workstation_control_body_is_closed():
    assert lmc_api.WorkstationControlBody(
        command={"action": "drain"}
    ).command == {"action": "drain"}
    with pytest.raises(ValidationError):
        lmc_api.WorkstationControlBody(
            command={"action": "drain"}, shell="id",
        )


def test_parallel_local_ai_runtime_settings_are_removed():
    for key in (
        "lmc_ai_active_node_id", "lmc_ai_model_set", "lmc_ai_thinking_enabled",
    ):
        assert key not in config_store.CONFIG_SPECS
    assert "get_active_node_id" not in API_SOURCE
    assert "ModelSetSelection" not in API_SOURCE
    assert "ThinkingSetting" not in API_SOURCE
    assert '@router.post("/api/developer/lmc-ai/active-node")' not in API_SOURCE
    assert '@router.post("/api/developer/lmc-ai/model-set")' not in API_SOURCE
    assert '@router.post("/api/developer/lmc-ai/thinking")' not in API_SOURCE


def test_developer_api_returns_only_the_single_enabled_workstation(monkeypatch):
    monkeypatch.setattr(lmc_api, "_developer_required", lambda _request: None)
    monkeypatch.setattr(lmc_api, "_db", lambda: object())
    monkeypatch.setattr(
        lmc_api, "list_node_rows", lambda _db: [{
            "node_id": "node-a", "display_name": "AI 01",
            "last_connected_at": None, "last_disconnected_at": None,
        }],
    )

    async def snapshot(_node_id):
        return {
            "name": "AI 01", "ready": True, "busy": False,
            "models": list(ai_model_config.lmc_ai_required_models()),
        }

    monkeypatch.setattr(lmc_api.RUNTIME, "snapshot", snapshot)
    result = asyncio.run(lmc_api.developer_lmc_ai_nodes(object()))

    assert result["workstation"]["node_id"] == "node-a"
    assert result["workstation"]["models_ready"] is True
    assert "nodes" not in result


def test_member_operation_status_exposes_one_workstation_without_private_id(monkeypatch):
    monkeypatch.setattr(lmc_api, "list_node_rows", lambda _db: [{
        "node_id": "node-a", "display_name": "AI 01",
        "last_connected_at": "now", "last_disconnected_at": None,
    }])

    async def snapshot(_node_id):
        return {
            "name": "AI 01", "ready": True, "busy": True,
            "queue_length": 1,
            "models": list(ai_model_config.lmc_ai_required_models()),
        }

    monkeypatch.setattr(lmc_api.RUNTIME, "snapshot", snapshot)
    result = asyncio.run(lmc_api._public_workstation(object(), "node-a"))

    assert result["name"] == "AI 01"
    assert result["state"] == "busy"
    assert result["queue_length"] == 1
    assert "node_id" not in result


def test_cached_pre_mode_bootstrap_uses_server_default_fast_fingerprint(monkeypatch):
    monkeypatch.setattr(
        lmc_api,
        "require_page_user_or_developer",
        lambda _request, _page: "member-1",
    )
    monkeypatch.setattr(lmc_api, "_db", lambda: object())
    monkeypatch.setattr(lmc_api, "require_lmc_ai_schema", lambda _db: None)
    monkeypatch.setattr(
        lmc_api,
        "interactive_features_suspension",
        lambda _request: {"active": False},
    )

    async def active_service(_db):
        return "node-1", {"online": True, "ready": True}, {
            "fast": "f" * 64,
            "daily": "t" * 64,
            "deep": "d" * 64,
            "complex": "t" * 64,
            "thinking": "d" * 64,
        }

    monkeypatch.setattr(lmc_api, "_active_service", active_service)

    async def public_workstation(_db, _node_id):
        return {"name": "AI 01", "state": "online"}

    monkeypatch.setattr(lmc_api, "_public_workstation", public_workstation)
    result = asyncio.run(lmc_api.lmc_ai_bootstrap(object()))

    assert result["backend_fingerprint"] == "f" * 64
    assert result["workstation"] == {"name": "AI 01", "state": "online"}
    assert result["backend_fingerprints"] == {
        "fast": "f" * 64,
        "daily": "t" * 64,
        "deep": "d" * 64,
        "complex": "t" * 64,
        "thinking": "d" * 64,
    }
    assert result["history_limits"]["conversations"] == 20
    assert result["history_limits"]["documents"] == 20
    assert {item["id"] for item in result["prompt_templates"]} == {
        "analyse_motion", "write_case", "create_document", "prepare_clash",
    }


def test_member_api_and_browser_contract_keep_node_metadata_and_thinking_trace_private():
    assert 'role == "system"' in API_SOURCE
    assert '"provider": "custom"' in API_SOURCE
    assert 'usage_user_id=None if actor_id == "developer"' in API_SOURCE
    assert '"duration_ms": usage.get("duration_ms", 0)' in API_SOURCE
    assert '"X-Accel-Buffering": "no"' in API_SOURCE
    assert 'websocket.query_params.get("token")' in API_SOURCE
    assert 'websocket.headers.get("authorization")' in API_SOURCE
    assert "thinking_trace" not in API_SOURCE
    assert "tool_calls" not in API_SOURCE

    assert "SafeMarkdown.render" in SCRIPT
    assert "indexedDB.open" in SCRIPT
    assert "localStorage" in SCRIPT  # one-time migration from the v1 conversation
    assert "identity.id" in SCRIPT
    assert "backendChanged" in SCRIPT
    assert "context.trimmed" in SCRIPT
    assert "has_history: baseMessages.length > 0" in SCRIPT
    assert "confirm(" in SCRIPT
    assert "較舊訊息" in SCRIPT
    assert "RAG 及 Fine-tune 暫未啟用" in PAGE
    assert "node_id" not in SCRIPT
    assert "runtime" not in SCRIPT
    assert "last_model" not in SCRIPT
    assert "effective_model" not in SCRIPT


def test_quick_prompts_are_colloquial_editable_and_use_a_four_minute_case_default():
    templates = {item["id"]: item for item in LMC_AI_PROMPT_TEMPLATES}
    assert "大約 4 分鐘" in templates["write_case"]["prompt"]
    assert "時間" not in templates["write_case"]["description"]
    assert "我哋開始啦！" in SCRIPT
    assert "揀上面嘅 Prompt，或者直接講低你想處理嘅內容。" in SCRIPT
    assert 'placeholder="有咩要__LMC_AI_NAME__幫手？"' in PAGE
    assert "幫我" in templates["analyse_motion"]["prompt"]
    assert "下面啲資料" in templates["write_case"]["prompt"]
    assert "點答" in templates["prepare_clash"]["prompt"]
    assert 'button.onclick = () => insertPrompt(template.prompt)' in SCRIPT
    prompt_render = SCRIPT.split("function renderPromptCards", 1)[1].split(
        "function renderDocuments", 1,
    )[0]
    assert "sendMessage" not in prompt_render
    assert 'id="promptDialog"' in PAGE
    assert 'id="appendPrompt"' in PAGE
    assert 'id="replacePrompt"' in PAGE
    assert 'type="number"' not in PAGE


def test_browser_workspace_keeps_twenty_chats_and_documents_without_auto_deletion():
    assert 'const DB_NAME = "lmc-ai-workspace"' in SCRIPT
    assert "EDITOR_LEASE_TTL_MS" in SCRIPT
    assert "acquireOrRenewEditorLease" in SCRIPT
    assert "releaseEditorLease" in SCRIPT
    assert "workspaceEditable" in SCRIPT
    assert 'id="workspaceReadOnlyBanner"' in PAGE
    assert "workspace.conversations.length >= bootstrap.history_limits.conversations" in SCRIPT
    assert "workspace.documents.length >= bootstrap.history_limits.documents" in SCRIPT
    assert "舊對話不會被自動刪除" in SCRIPT
    assert "舊文件不會被自動刪除" in SCRIPT
    assert "workspace.conversations.shift" not in SCRIPT
    assert "workspace.documents.shift" not in SCRIPT
    assert 'data-mobile-target="chat"' in PAGE
    assert 'data-mobile-target="documents"' in PAGE
    assert 'data-mobile-target="recent"' in PAGE
    assert 'id="documentEditor"' in PAGE
    assert 'id="documentPreview"' in PAGE
    assert '"/api/lmc-ai/documents/export"' in SCRIPT


def test_document_exports_are_valid_bounded_formats_and_escape_docx_xml():
    markdown = build_markdown_export("立論稿", "第一段")
    assert markdown.startswith(b"\xef\xbb\xbf# ")
    assert "立論稿" in markdown.decode("utf-8-sig")

    pdf = build_pdf_export("立論稿", "第一段\n第二段")
    assert pdf.startswith(b"%PDF")

    docx = build_docx_export("立論稿", "A & B < C")
    with ZipFile(BytesIO(docx)) as archive:
        assert {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/styles.xml",
            "word/_rels/document.xml.rels",
        }.issubset(archive.namelist())
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "立論稿" in document_xml
    assert "A &amp; B &lt; C" in document_xml


def test_docx_export_removes_characters_forbidden_by_xml_1_0():
    docx = build_docx_export("控制字元", "保留\tTab，移除\x00NUL及\x0bVT")
    with ZipFile(BytesIO(docx)) as archive:
        document_xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(document_xml)
    rendered = "".join(root.itertext())
    assert "保留\tTab，移除NUL及VT" in rendered


def test_document_export_requires_page_access_and_is_stateless(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lmc_api,
        "require_page_user_or_developer",
        lambda request, page: calls.append((request, page)) or "member-1",
    )
    monkeypatch.setattr(
        lmc_api,
        "bounded_download_response",
        lambda filename, content, media_type: {
            "filename": filename, "content": content, "media_type": media_type,
        },
    )
    request = object()
    result = lmc_api.lmc_ai_document_export(
        lmc_api.DocumentExportRequest(
            title="立論稿", content="內容", format="markdown",
        ),
        request,
    )
    assert calls == [(request, "lmc_ai")]
    assert result["filename"] == "立論稿.md"
    assert result["content"].startswith(b"\xef\xbb\xbf")
    assert "_db()" not in API_SOURCE.split(
        "def lmc_ai_document_export", 1,
    )[1].split("async def _record_usage", 1)[0]


def test_browser_offers_three_conversation_scoped_model_modes_and_node_status():
    assert 'id="thinkingMode"' in PAGE
    assert 'aria-label="回答模式" disabled' in PAGE
    assert 'id="statusDialog"' in PAGE
    assert 'id="nodeGrid"' in PAGE
    assert "backend_fingerprints" in API_SOURCE
    assert "_resolve_chat_mode" in API_SOURCE
    assert "mode: conversation.mode" in SCRIPT
    assert "normalizeMode" in SCRIPT
    assert "renderModeOptions" in SCRIPT
    assert "bootstrap?.service?.modes" in SCRIPT
    assert "switchConversationMode" in SCRIPT
    assert "另開新對話" in SCRIPT
    assert "createConversation(nextMode)" in SCRIPT
    assert "conversation.mode" in SCRIPT


def test_lmc_browser_status_never_appends_the_model_set_label():
    render_status = SCRIPT.split("function renderStatus()", 1)[1].split(
        "function renderNodes()", 1,
    )[0]

    assert "service.model_set_label" not in render_status


def test_lmc_browser_requires_the_server_owned_default_mode():
    assert 'const DEFAULT_MODE = "daily"' not in SCRIPT
    assert "bootstrap?.default_mode ||" not in SCRIPT
    assert "function requireDefaultMode" in SCRIPT
    assert "requireDefaultMode(data)" in SCRIPT
    assert 'throw new Error("自家 AI 回答模式設定無效。")' in SCRIPT


def test_browser_switches_are_generation_guarded_and_blocked_during_inflight_work():
    assert "let conversationGeneration = 0" in SCRIPT
    assert "const requestGeneration = conversationGeneration" in SCRIPT
    assert "requestGeneration !== conversationGeneration" in SCRIPT
    select_block = SCRIPT.split("function selectConversation", 1)[1].split(
        "function deleteConversation", 1,
    )[0]
    assert "if (abortController)" in select_block
    assert "conversationGeneration += 1" in select_block


def test_regeneration_keeps_persisted_messages_until_the_replacement_completes():
    regenerate_block = SCRIPT.split("function regenerateAnswer", 1)[1].split(
        "function openPromptDialog", 1,
    )[0]
    send_block = SCRIPT.split("async function sendMessage", 1)[1].split(
        "function createConversation", 1,
    )[0]
    complete_block = send_block.split('event === "complete"', 1)[1].split(
        'event === "error"', 1,
    )[0]
    assert "conversation.messages =" not in regenerate_block
    assert "persistWorkspace()" not in regenerate_block
    assert "replacementFromIndex" in regenerate_block
    assert "conversation.messages =" in complete_block


def test_lmc_node_load_failure_does_not_masquerade_as_developer_logout():
    load_block = DEV_SETTINGS.split("async function load()", 1)[1].split(
        "async function mutate", 1
    )[0]
    assert load_block.index('$("login").classList.remove("hidden")') < load_block.index(
        "await loadLmcNodes()"
    )
    assert "自家 AI 電腦資料暫時未能讀取" in load_block


def test_new_developer_ui_has_only_single_workstation_credential_controls():
    assert 'id="refreshLmcNodes"' in DEV_SETTINGS
    assert 'id="clearLmcSelection"' not in DEV_SETTINGS
    assert 'id="lmcThinkingEnabled"' not in DEV_SETTINGS
    assert 'id="saveLmcThinking"' not in DEV_SETTINGS
    assert "let lmcLoadGeneration = 0" in DEV_SETTINGS
    assert "loadGeneration !== lmcLoadGeneration" in DEV_SETTINGS
    assert 'data-lmc-select' not in DEV_SETTINGS
    assert '"/api/developer/lmc-ai/active-node"' not in DEV_SETTINGS
    assert '"/api/developer/lmc-ai/thinking"' not in DEV_SETTINGS
    assert 'id="lmcModelSet"' not in DEV_SETTINGS
    assert '"/api/developer/lmc-ai/model-set"' not in DEV_SETTINGS

    assert "class ThinkingSetting" not in API_SOURCE
    assert "class ModelSetSelection" not in API_SOURCE
    assert "get_workstation_id" in API_SOURCE
    assert 'return {"workstation": None}' in API_SOURCE
    assert '@router.post("/api/developer/lmc-ai/thinking")' not in API_SOURCE
    assert '@router.post("/api/developer/lmc-ai/model-set")' not in API_SOURCE


def test_chat_page_serves_and_renders_the_versioned_assistant_avatar():
    avatar = ROOT / "frontend/lmc_ai/shiba-avatar.jpg"
    assert avatar.is_file()
    assert avatar.stat().st_size < 100_000
    assert 'data-avatar-src="/lmc-ai/shiba-avatar.jpg?v=__APP_VERSION__"' in PAGE
    assert 'className = "assistant-avatar"' in SCRIPT
    assert 'className = `message-wrap ${item.role}`' in SCRIPT
    assert '@app.get("/lmc-ai/shiba-avatar.jpg")' in (
        ROOT / "deploy/proxy.py"
    ).read_text("utf-8")


def test_node_cli_has_no_runtime_pull_or_output_token_cap_and_config_is_private(tmp_path):
    assert "ollama pull" not in NODE_SOURCE
    assert '"think": bool(think)' in NODE_SOURCE
    assert "for mode in MODE_OPTIONS.values()" in NODE_SOURCE
    assert "_model_probe(model, think=thinking)" in NODE_SOURCE
    assert "available_model_sets" not in NODE_SOURCE
    assert 'thinking_enabled = payload.get("think") is True' in NODE_SOURCE
    assert '"think": thinking_enabled' in NODE_SOURCE
    assert '"thinking_control": True' in NODE_SOURCE
    assert '"num_ctx": CONTEXT_LENGTH' in NODE_SOURCE
    assert "num_predict" not in NODE_SOURCE
    assert "max_tokens" not in NODE_SOURCE
    assert "async def cancel_active" in NODE_SOURCE
    assert "await self.cancel_active(" in NODE_SOURCE
    assert lmc_ai_node.NodeClient._fallback_allowed(
        [{"role": "system"}, {"role": "user"}], False, True
    )
    assert not lmc_ai_node.NodeClient._fallback_allowed(
        [{"role": "system"}, {"role": "user"}, {"role": "assistant"}, {"role": "user"}],
        False,
        True,
    )
    assert not lmc_ai_node.NodeClient._fallback_allowed(
        [{"role": "system"}, {"role": "user"}], True, True
    )
    assert not lmc_ai_node.NodeClient._fallback_allowed(
        [{"role": "system"}, {"role": "user"}], False, False
    )
    path = tmp_path / "node.json"
    lmc_ai_node._save(path, {"token": "secret", "name": "AI 01"})
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert "secret" not in json.dumps(lmc_ai_node._public_config(lmc_ai_node._load(path)))


def test_node_rejects_thinking_only_completion_instead_of_reporting_blank_success(
    tmp_path, monkeypatch,
):
    path = tmp_path / "node.json"
    lmc_ai_node._save(path, {
        "effective_model": LMC_AI_PRIMARY_MODEL,
        "available_models": [LMC_AI_PRIMARY_MODEL],
        "preflight_ready": True,
        "draining": False,
    })

    class _Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield json.dumps({
                "message": {"thinking": "只有隱藏推理", "content": ""},
                "done": True,
                "prompt_eval_count": 721,
                "eval_count": 7471,
                "total_duration": 1_000_000,
            })

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return _Response()

    class _Socket:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    monkeypatch.setattr(lmc_ai_node.httpx, "AsyncClient", _Client)
    client = lmc_ai_node.NodeClient(path)
    client.websocket = _Socket()
    asyncio.run(client.generate({
        "operation_id": "thinking-empty",
        "messages": [{"role": "user", "content": "請分析"}],
        "model": LMC_AI_PRIMARY_MODEL,
        "think": True,
    }))

    assert [item["type"] for item in client.websocket.sent] == [
        "chat.started", "chat.error",
    ]
    assert client.websocket.sent[-1]["code"] == "empty_response"
    assert client.websocket.sent[-1]["usage"]["output_tokens"] == 7471


def test_node_server_url_normalization_is_idempotent_and_repairs_duplicate_path():
    expected = "wss://example.test/api/lmc-ai/nodes/connect"
    assert lmc_ai_node._normalise_server("https://example.test") == expected
    assert lmc_ai_node._normalise_server(expected) == expected
    assert lmc_ai_node._normalise_server(expected + "/api/lmc-ai/nodes/connect") == expected


def test_node_preflight_accepts_the_complete_gemma_profile_and_reports_failure(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    lmc_ai_node._save(
        path,
        {
            "server_url": "wss://example.test/api/lmc-ai/nodes/connect",
            "name": "AI 01",
            "token": "secret",
            "effective_model": "",
            "preflight_ready": False,
            "preflight_at": "",
            "draining": False,
        },
    )
    monkeypatch.setattr(lmc_ai_node, "_run_checked", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(lmc_ai_node, "_verify_local_binding", lambda: None)
    monkeypatch.setattr(
        lmc_ai_node,
        "_installed_models",
        lambda: {
            model: str(index) * 64
            for index, model in enumerate(ai_model_config.lmc_ai_all_models(), 1)
        },
    )

    probes = []

    def gemma_probe(model, *, think):
        probes.append((model, think))

    monkeypatch.setattr(lmc_ai_node, "_model_probe", gemma_probe)
    assert lmc_ai_node.preflight(SimpleNamespace(config=path)) == 0
    selected = lmc_ai_node._load(path)
    assert selected["preflight_ready"] is True
    assert selected["effective_model"] == LMC_AI_PRIMARY_MODEL
    assert selected["available_models"] == [LMC_AI_PRIMARY_MODEL, LMC_AI_FALLBACK_MODEL]
    assert "available_model_sets" not in selected
    assert selected["model_profile_version"] == lmc_ai_node.MODEL_PROFILE_VERSION
    assert probes == [
        (LMC_AI_FAST_MODEL_TAG, False),
        (LMC_AI_DAILY_MODEL_TAG, False),
        (LMC_AI_DEEP_MODEL_TAG, True),
    ]

    monkeypatch.setattr(
        lmc_ai_node,
        "_model_probe",
        lambda model, *, think: (_ for _ in ()).throw(
            RuntimeError(f"{model} thinking={think} failed")
        ),
    )
    with pytest.raises(SystemExit, match="Gemma profile 未通過"):
        lmc_ai_node.preflight(SimpleNamespace(config=path))
    refused = lmc_ai_node._load(path)
    assert refused["preflight_ready"] is False
    assert refused["effective_model"] == ""


def test_node_systemd_unit_runs_as_ai_account_and_depends_on_ollama(tmp_path):
    unit = lmc_ai_node._systemd_unit(
        "debate-ai",
        Path("/opt/lmc-ai/venv/bin/python"),
        Path("/opt/lmc-ai/lmc_ai_node.py"),
        tmp_path / "node.json",
    )
    assert "User=debate-ai" in unit
    assert "Requires=ollama.service" in unit
    assert "After=network-online.target ollama.service" in unit
    assert "Restart=always" in unit
    assert "NoNewPrivileges=true" in unit
    assert " run\n" in unit

    units = lmc_ai_node._service_files(
        "debate-ai",
        Path("/opt/lmc-ai/venv/bin/python"),
        Path("/opt/lmc-ai/lmc_ai_node.py"),
        tmp_path / "node.json",
    )
    assert "23:55:00 Asia/Hong_Kong" in units[lmc_ai_node.AUTO_DRAIN_TIMER]
    assert "00:00:00 Asia/Hong_Kong" in units[lmc_ai_node.AUTO_SUSPEND_TIMER]
    assert "08:00:00 Asia/Hong_Kong" in units[lmc_ai_node.AUTO_RESUME_TIMER]
    assert "scheduled-suspend" in units[lmc_ai_node.AUTO_SUSPEND_SERVICE]
    assert "User=debate-ai" not in units[lmc_ai_node.AUTO_SUSPEND_SERVICE]


def test_auto_power_wake_timestamp_is_next_0800_hong_kong():
    zone = ZoneInfo("Asia/Hong_Kong")
    after_midnight = datetime(2026, 7, 21, 0, 0, tzinfo=zone)
    after_wake = datetime(2026, 7, 21, 12, 0, tzinfo=zone)
    assert datetime.fromtimestamp(
        lmc_ai_node._next_wake_timestamp(after_midnight), zone
    ) == datetime(2026, 7, 21, 8, 0, tzinfo=zone)
    assert datetime.fromtimestamp(
        lmc_ai_node._next_wake_timestamp(after_wake), zone
    ) == datetime(2026, 7, 22, 8, 0, tzinfo=zone)
