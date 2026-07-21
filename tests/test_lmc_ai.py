from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pydantic import ValidationError

import ai_model_config
import ai_name
import schema
import system_limits
from api import lmc_ai_api as lmc_api
from api.lmc_ai_api import ChatRequest, _resolve_thinking_enabled
from ai_model_config import (
    LMC_AI_CONTEXT_LENGTH,
    LMC_AI_DEFAULT_MODE,
    LMC_AI_FALLBACK_MODEL,
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


ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "migrations/20260720_0010_add_lmc_ai_nodes.up.sql"
DOWN = ROOT / "migrations/20260720_0010_add_lmc_ai_nodes.down.sql"
PAGE = (ROOT / "frontend/lmc_ai/index.html").read_text("utf-8")
SCRIPT = (ROOT / "frontend/lmc_ai/app.js").read_text("utf-8")
DEV_SETTINGS = (ROOT / "frontend/dev_settings/index.html").read_text("utf-8")
API_SOURCE = (ROOT / "api/lmc_ai_api.py").read_text("utf-8")
NODE_SOURCE = (ROOT / "local_ai/lmc_ai_node.py").read_text("utf-8")


def test_identity_and_persona_have_one_runtime_source():
    assert ai_name.LMC_AI_NAME
    assert ai_name.LMC_AI_EMOJI
    assert ai_name.LMC_AI_MENTION_TAG == f"@{ai_name.LMC_AI_NAME}".casefold()
    assert ai_name.LMC_AI_NAME in SYSTEM_PROMPT
    assert ai_name.LMC_AI_EMOJI in SYSTEM_PROMPT
    assert "{{" not in SYSTEM_PROMPT
    assert "system prompt" in SYSTEM_PROMPT
    assert "隱藏推理" in SYSTEM_PROMPT
    assert "RAG：未啟用" in SYSTEM_PROMPT
    assert len(PERSONA_VERSION) == 64

    runtime_files = (
        ROOT / "frontend/lmc_ai/index.html",
        ROOT / "frontend/vote/index.html",
        ROOT / "frontend/home/index.html",
        ROOT / "api/lmc_ai_api.py",
        ROOT / "core/lmc_ai_runtime.py",
        ROOT / "core/vote_ai.py",
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
    assert schema_features.FEATURE_MIGRATION_VERSIONS["lmc_ai"] == "20260720_0010"
    assert "refusing to remove used local AI node or usage data" in down
    assert "key = 'lmc_ai_active_node_id'" in down
    assert "feature = 'lmc_ai_chat'" in down
    assert down.index("DELETE FROM public.app_config") < down.index("DROP TABLE public.lmc_ai_nodes")


def test_limits_and_models_are_centralized_at_the_decided_values():
    assert LMC_AI_PRIMARY_MODEL == "qwen3.5:4b"
    assert LMC_AI_FALLBACK_MODEL == "qwen3.5:9b"
    assert LMC_AI_DEFAULT_MODE == "daily"
    assert LMC_AI_MODE_OPTIONS == {
        "daily": {"label": "日常預設", "model": "qwen3.5:4b", "thinking": False},
        "complex": {"label": "複雜問題", "model": "qwen3.5:4b", "thinking": True},
        "deep": {"label": "深入思考", "model": "qwen3.5:9b", "thinking": True},
    }
    assert LMC_AI_CONTEXT_LENGTH == 4096
    assert system_limits.LMC_AI_NODE_MAX == 8
    assert system_limits.LMC_AI_QUEUE_MAX == 2
    assert system_limits.LMC_AI_ACTIVE_GENERATIONS == 1
    assert system_limits.LMC_AI_MESSAGE_MAX_CHARS == 3000
    assert system_limits.LMC_AI_CONTEXT_MAX_CHARS == 3000
    assert system_limits.LMC_AI_REQUEST_MESSAGES_MAX == 40
    assert system_limits.LMC_AI_REQUEST_TIMEOUT_SECONDS == 180
    assert system_limits.LMC_AI_OUTPUT_MAX_BYTES == 256 * 1024
    assert system_limits.LMC_AI_BROWSER_HISTORY_MAX_MESSAGES == 100
    assert system_limits.LMC_AI_BROWSER_HISTORY_MAX_CHARS == 200_000
    assert "lmc_ai_chat" in funds_logic.AI_USAGE_FEATURES
    assert funds_logic.AI_FEATURE_LABELS["lmc_ai_chat"]


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
        assert start["context_length"] == 4096
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


def test_runtime_routes_deep_mode_to_9b_and_refuses_it_on_a_4b_only_node():
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
        limited_node = await limited.register(
            "node-2", limited_socket,
            _hello(models=[LMC_AI_PRIMARY_MODEL]),
        )
        with pytest.raises(lmc_runtime.NodeUnavailableError, match="回答模式"):
            await limited.submit(
                node_id="node-2",
                expected_fingerprint="",
                actor_id="member-1",
                usage_user_id="member-1",
                operation_stage="member_chat",
                messages=[{"role": "user", "content": "deep"}],
                finish_callback=_noop_finish,
                model=LMC_AI_FALLBACK_MODEL,
                thinking_enabled=True,
            )
        await limited.unregister(limited_node, "test cleanup")

    asyncio.run(scenario())


def test_shared_availability_requires_selected_ready_node_and_tracks_modes(monkeypatch):
    runtime = LocalAIRuntime()
    monkeypatch.setattr(lmc_ai_client, "RUNTIME", runtime)
    monkeypatch.setattr(lmc_ai_client, "require_lmc_ai_schema", lambda _db: None)
    selected = {"node_id": ""}
    monkeypatch.setattr(
        lmc_ai_client, "get_active_node_id", lambda _db: selected["node_id"],
    )

    async def scenario():
        missing = await lmc_ai_client.local_ai_availability(object())
        assert missing["available"] is False
        assert missing["selected"] is False
        assert "尚未選用" in missing["message"]

        selected["node_id"] = "node-1"
        offline = await lmc_ai_client.local_ai_availability(object())
        assert offline["state"] == "offline"
        assert "離線" in offline["message"]

        socket = _FakeWebSocket()
        node = await runtime.register(
            "node-1", socket,
            _hello(models=[LMC_AI_PRIMARY_MODEL]),
        )
        node.active = object()
        busy = await lmc_ai_client.local_ai_availability(object())
        assert busy["available"] is True
        assert busy["state"] == "busy"
        modes = {item["id"]: item for item in busy["modes"]}
        assert modes["daily"]["available"] is True
        assert modes["complex"]["available"] is True
        assert modes["deep"]["available"] is False
        assert "未提供「深入思考」" in modes["deep"]["message"]

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
                "node-1", socket, _hello(models=[LMC_AI_PRIMARY_MODEL]),
            )

        owner_loop.run_until_complete(register())
        owner_ready.set()
        owner_loop.run_forever()

    thread = threading.Thread(target=run_owner_loop, daemon=True)
    thread.start()
    assert owner_ready.wait(timeout=2)
    monkeypatch.setattr(lmc_ai_client, "RUNTIME", runtime)
    monkeypatch.setattr(lmc_ai_client, "require_lmc_ai_schema", lambda _db: None)
    monkeypatch.setattr(lmc_ai_client, "get_active_node_id", lambda _db: "node-1")
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
        assert start["model"] == LMC_AI_PRIMARY_MODEL
        assert start["think"] is True
    finally:
        async def cleanup():
            await runtime.unregister(holder["node"], "test cleanup")
            await asyncio.sleep(0)

        asyncio.run_coroutine_threadsafe(cleanup(), owner_loop).result(timeout=2)
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        thread.join(timeout=2)
        owner_loop.close()


def test_clearing_selection_keeps_active_generation_and_fails_only_queue():
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
        await runtime.fail_queued("node-1", "selection cleared")

        assert node.active is active
        assert list(node.queue) == []
        assert queued.finished is True
        assert await queued.events.get() == ("queued", {"position": 1})
        assert await queued.events.get() == ("error", {"message": "selection cleared"})
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


def test_node_hello_requires_current_model_profile_and_mandatory_4b():
    assert ai_model_config.LMC_AI_MODEL_PROFILE_VERSION == 2
    assert LocalAIRuntime.validate_hello(_hello())["ready"] is True

    missing_profile = _hello()
    missing_profile.pop("model_profile_version")
    with pytest.raises(ValueError, match="model profile"):
        LocalAIRuntime.validate_hello(missing_profile)
    with pytest.raises(ValueError, match="model profile"):
        LocalAIRuntime.validate_hello(_hello(model_profile_version=1))
    with pytest.raises(ValueError, match="default model"):
        LocalAIRuntime.validate_hello(_hello(
            model=LMC_AI_FALLBACK_MODEL,
            models=[LMC_AI_FALLBACK_MODEL],
        ))

    assert '"model_profile_version": MODEL_PROFILE_VERSION' in NODE_SOURCE


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
    assert _resolve_thinking_enabled(None, True) is True
    assert _resolve_thinking_enabled(None, False) is False
    assert _resolve_thinking_enabled("thinking", False) is True
    assert _resolve_thinking_enabled("fast", True) is False
    with pytest.raises(ValidationError):
        ChatRequest(**request, mode="unlimited")


def test_legacy_thinking_setting_stays_typed_for_cached_pre_mode_clients(monkeypatch):
    writes = []
    monkeypatch.setattr(lmc_ai_store, "require_lmc_ai_schema", lambda _db: None)
    monkeypatch.setattr(
        lmc_ai_store,
        "get_config",
        lambda _db, key, default: key == "lmc_ai_thinking_enabled",
    )
    monkeypatch.setattr(
        lmc_ai_store,
        "set_config",
        lambda _db, key, value: writes.append((key, value)),
    )

    assert config_store.CONFIG_SPECS["lmc_ai_thinking_enabled"].value_type == "boolean"
    assert lmc_ai_store.get_thinking_enabled(object()) is True
    lmc_ai_store.set_thinking_enabled(object(), False)
    assert writes == [("lmc_ai_thinking_enabled", False)]


def test_cached_pre_mode_api_contract_remains_functional(monkeypatch):
    monkeypatch.setattr(lmc_api, "_developer_required", lambda _request: None)
    monkeypatch.setattr(lmc_api, "_db", lambda: object())
    monkeypatch.setattr(lmc_api, "list_node_rows", lambda _db: [])
    monkeypatch.setattr(lmc_api, "get_active_node_id", lambda _db: "")
    monkeypatch.setattr(lmc_api, "get_thinking_enabled", lambda _db: True)
    writes = []
    monkeypatch.setattr(
        lmc_api,
        "set_thinking_enabled",
        lambda _db, enabled: writes.append(enabled),
    )

    async def snapshots():
        return {}

    monkeypatch.setattr(lmc_api.RUNTIME, "all_snapshots", snapshots)
    nodes = asyncio.run(lmc_api.developer_lmc_ai_nodes(object()))
    saved = asyncio.run(
        lmc_api.developer_set_lmc_ai_thinking(
            lmc_api.ThinkingSetting(enabled=False), object()
        )
    )

    assert nodes["thinking_enabled"] is True
    assert saved == {"ok": True, "thinking_enabled": False}
    assert writes == [False]


def test_member_operation_status_lists_every_registered_node_without_ids(monkeypatch):
    monkeypatch.setattr(lmc_api, "list_node_rows", lambda _db: [
        {"node_id": "node-a", "display_name": "AI 01"},
        {"node_id": "node-b", "display_name": "AI 02"},
    ])

    async def snapshots():
        return {
            "node-a": {
                "name": "AI 01", "ready": True, "busy": True,
                "queue_length": 1,
                "models": [LMC_AI_PRIMARY_MODEL, LMC_AI_FALLBACK_MODEL],
            }
        }

    monkeypatch.setattr(lmc_api.RUNTIME, "all_snapshots", snapshots)
    result = asyncio.run(lmc_api._public_nodes(object(), "node-a"))

    assert [item["name"] for item in result] == ["AI 01", "AI 02"]
    assert result[0]["state"] == "busy" and result[0]["selected"] is True
    assert result[1]["state"] == "offline" and result[1]["selected"] is False
    assert all("node_id" not in item for item in result)


def test_cached_pre_mode_bootstrap_uses_the_legacy_global_fingerprint(monkeypatch):
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
        return "node-1", {"online": True, "ready": True}, True, {
            "daily": "f" * 64,
            "complex": "t" * 64,
            "fast": "f" * 64,
            "thinking": "t" * 64,
        }

    monkeypatch.setattr(lmc_api, "_active_service", active_service)

    async def public_nodes(_db, _active_node_id):
        return []

    monkeypatch.setattr(lmc_api, "_public_nodes", public_nodes)
    result = asyncio.run(lmc_api.lmc_ai_bootstrap(object()))

    assert result["backend_fingerprint"] == "t" * 64
    assert result["backend_fingerprints"] == {
        "daily": "f" * 64,
        "complex": "t" * 64,
        "fast": "f" * 64,
        "thinking": "t" * 64,
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
    assert "localStorage" in SCRIPT
    assert "identity.id" in SCRIPT
    assert "backendChanged" in SCRIPT
    assert "context.trimmed" in SCRIPT
    assert "has_history: conversation.messages.length > 0" in SCRIPT
    assert "confirm(" in SCRIPT
    assert "較舊訊息" in SCRIPT
    assert "RAG 同 Fine-tune 暫未啟用" in PAGE
    assert "node_id" not in SCRIPT
    assert "runtime" not in SCRIPT
    assert "last_model" not in SCRIPT
    assert "effective_model" not in SCRIPT


def test_browser_offers_three_conversation_scoped_model_modes_and_node_status():
    assert 'id="thinkingMode"' in PAGE
    assert '<option value="daily">日常預設（4B）</option>' in PAGE
    assert '<option value="complex">複雜問題（4B Thinking）</option>' in PAGE
    assert '<option value="deep">深入思考（9B Thinking）</option>' in PAGE
    assert 'data-panel="operationsPanel"' in PAGE
    assert 'id="nodeGrid"' in PAGE
    assert "backend_fingerprints" in API_SOURCE
    assert "_resolve_thinking_enabled" in API_SOURCE
    assert "mode: conversation.mode" in SCRIPT
    assert "normalizeMode" in SCRIPT
    assert "switchConversationMode" in SCRIPT
    assert "切換回答模式" in SCRIPT
    assert "conversation.messages.length && !confirm(" in SCRIPT
    assert "conversation.mode" in SCRIPT


def test_browser_clear_invalidates_and_aborts_an_inflight_conversation():
    assert "let conversationGeneration = 0" in SCRIPT
    assert "const requestGeneration = conversationGeneration" in SCRIPT
    assert "requestGeneration !== conversationGeneration" in SCRIPT
    clear_block = SCRIPT.split("function clearConversation", 1)[1].split(
        '$("messageInput")', 1
    )[0]
    assert "conversationGeneration += 1" in clear_block
    assert "abortController?.abort()" in clear_block


def test_lmc_node_load_failure_does_not_masquerade_as_developer_logout():
    load_block = DEV_SETTINGS.split("async function load()", 1)[1].split(
        "async function mutate", 1
    )[0]
    assert load_block.index('$("login").classList.remove("hidden")') < load_block.index(
        "await loadLmcNodes()"
    )
    assert "自家 AI 電腦資料暫時未能讀取" in load_block


def test_new_developer_ui_hides_global_thinking_but_keeps_cached_page_api_compatibility():
    assert 'id="refreshLmcNodes"' in DEV_SETTINGS
    assert 'id="clearLmcSelection"' in DEV_SETTINGS
    assert 'id="lmcThinkingEnabled"' not in DEV_SETTINGS
    assert 'id="saveLmcThinking"' not in DEV_SETTINGS
    assert "let lmcLoadGeneration = 0" in DEV_SETTINGS
    assert "loadGeneration !== lmcLoadGeneration" in DEV_SETTINGS
    assert 'JSON.stringify({ node_id: "" })' in DEV_SETTINGS
    assert '"/api/developer/lmc-ai/active-node"' in DEV_SETTINGS
    assert '"/api/developer/lmc-ai/thinking"' not in DEV_SETTINGS

    assert "class ThinkingSetting" in API_SOURCE
    assert "node_id: str = Field(max_length=64)" in API_SOURCE
    assert "set_thinking_enabled" in API_SOURCE
    assert "get_thinking_enabled" in API_SOURCE
    assert '"thinking_enabled": thinking_enabled' in API_SOURCE
    assert '@router.post("/api/developer/lmc-ai/thinking")' in API_SOURCE


def test_node_cli_has_no_runtime_pull_or_output_token_cap_and_config_is_private(tmp_path):
    assert "ollama pull" not in NODE_SOURCE
    assert '"think": False' in NODE_SOURCE
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


def test_node_preflight_requires_4b_and_treats_9b_as_optional(tmp_path, monkeypatch):
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
        lambda: {LMC_AI_PRIMARY_MODEL, LMC_AI_FALLBACK_MODEL},
    )

    def optional_deep_probe(model):
        if model == LMC_AI_FALLBACK_MODEL:
            raise RuntimeError("out of memory")

    monkeypatch.setattr(lmc_ai_node, "_model_probe", optional_deep_probe)
    assert lmc_ai_node.preflight(SimpleNamespace(config=path)) == 0
    selected = lmc_ai_node._load(path)
    assert selected["preflight_ready"] is True
    assert selected["effective_model"] == LMC_AI_PRIMARY_MODEL
    assert selected["available_models"] == [LMC_AI_PRIMARY_MODEL]
    assert selected["model_profile_version"] == lmc_ai_node.MODEL_PROFILE_VERSION

    monkeypatch.setattr(
        lmc_ai_node,
        "_model_probe",
        lambda model: (_ for _ in ()).throw(RuntimeError(f"{model} failed")),
    )
    with pytest.raises(SystemExit, match="日常預設 model"):
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
