"""Offline regressions for Render memory, queue and cache safeguards."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime
import inspect
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import deploy.proxy as proxy
from core import push
from core import media_logic
import system_limits


ROOT = Path(__file__).resolve().parents[1]


async def _measure_body_handler_concurrency(headers, *, request_count=20):
    active = 0
    maximum = 0
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def handler(_scope, receive, _send):
        nonlocal active, maximum
        message = await receive()
        assert message["type"] == "http.request"
        active += 1
        maximum = max(maximum, active)
        if active == system_limits.REQUEST_BODY_BUFFER_CONCURRENCY:
            saturated.set()
        await release.wait()
        active -= 1

    middleware = proxy.RequestBodyLimitMiddleware(handler, max_bytes=1024)

    async def invoke():
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": b"abc",
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        await middleware(
            {"type": "http", "method": "POST", "headers": headers},
            receive,
            send,
        )

    tasks = [asyncio.create_task(invoke()) for _ in range(request_count)]
    await asyncio.wait_for(saturated.wait(), timeout=1)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tasks)
    return maximum


def test_twenty_declared_bodies_hold_at_most_four_handler_buffers():
    maximum = asyncio.run(
        _measure_body_handler_concurrency([(b"content-length", b"3")])
    )
    assert system_limits.REQUEST_BODY_BUFFER_CONCURRENCY == 4
    assert maximum == 4


def test_missing_length_body_is_not_treated_as_empty():
    maximum = asyncio.run(_measure_body_handler_concurrency([]))
    assert maximum == system_limits.REQUEST_BODY_BUFFER_CONCURRENCY == 4


def test_false_zero_content_length_body_still_holds_one_of_four_slots():
    maximum = asyncio.run(
        _measure_body_handler_concurrency([(b"content-length", b"0")])
    )
    assert maximum == system_limits.REQUEST_BODY_BUFFER_CONCURRENCY == 4


def test_missing_length_first_chunks_are_bounded_before_receive():
    async def scenario():
        active_receives = 0
        maximum_receives = 0
        saturated = asyncio.Event()
        release = asyncio.Event()

        async def handler(_scope, receive, _send):
            assert (await receive())["body"] == b"x" * 1024

        middleware = proxy.RequestBodyLimitMiddleware(handler, max_bytes=2048)

        async def invoke():
            async def receive():
                nonlocal active_receives, maximum_receives
                active_receives += 1
                maximum_receives = max(maximum_receives, active_receives)
                if active_receives == system_limits.REQUEST_BODY_BUFFER_CONCURRENCY:
                    saturated.set()
                await release.wait()
                active_receives -= 1
                return {
                    "type": "http.request",
                    "body": b"x" * 1024,
                    "more_body": False,
                }

            async def send(_message):
                return None

            await middleware(
                {"type": "http", "method": "POST", "headers": []},
                receive,
                send,
            )

        tasks = [asyncio.create_task(invoke()) for _ in range(20)]
        await asyncio.wait_for(saturated.wait(), timeout=1)
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        return maximum_receives

    assert asyncio.run(scenario()) == system_limits.REQUEST_BODY_BUFFER_CONCURRENCY == 4


def test_get_head_and_explicit_empty_requests_bypass_body_slots():
    async def scenario():
        entered = 0
        all_entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(_scope, _receive, _send):
            nonlocal entered
            entered += 1
            if entered == 20:
                all_entered.set()
            await release.wait()

        middleware = proxy.RequestBodyLimitMiddleware(handler, max_bytes=8)

        async def invoke(method, headers):
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(_message):
                return None

            await middleware(
                {"type": "http", "method": method, "headers": headers},
                receive,
                send,
            )

        requests = (
            [("GET", [(b"content-length", b"999")])] * 7
            + [("HEAD", [(b"content-length", b"999")])] * 6
            + [("POST", [(b"content-length", b"0")])] * 7
        )
        tasks = [asyncio.create_task(invoke(*request)) for request in requests]
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        release.set()
        await asyncio.gather(*tasks)
        return entered

    assert asyncio.run(scenario()) == 20


def test_startup_contract_is_named_and_includes_uvicorn_queue():
    output = subprocess.run(
        [sys.executable, "system_limits.py", "--startup"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    parsed = dict(line.split("=", 1) for line in output)
    assert tuple(parsed) == system_limits.STARTUP_LIMIT_NAMES
    assert parsed["UVICORN_WS_MAX_QUEUE"] == "4"

    script = (ROOT / "deploy" / "start.sh").read_text(encoding="utf-8")
    for name in system_limits.STARTUP_LIMIT_NAMES:
        assert f"{name})" in script
    assert '--ws-max-queue "$UVICORN_WS_MAX_QUEUE"' in script


def test_multiplayer_server_gemini_and_media_connectors_are_removed():
    source = (ROOT / "deploy/proxy.py").read_text(encoding="utf-8")
    assert not hasattr(proxy, "_room_start_gemini_if_needed")
    assert not hasattr(proxy, "_room_gemini_pump")
    assert not hasattr(proxy, "_room_handle_audio")
    assert "import websockets" not in source
    assert "peer_audio" not in source and "serverContent" not in source


def test_hong_kong_month_start_is_shared_by_bandwidth_writes():
    hk = ZoneInfo("Asia/Hong_Kong")
    just_after_midnight = datetime.datetime(2026, 7, 1, 0, 30, tzinfo=hk)
    created_at, period_start = proxy._bandwidth_write_context(just_after_midnight)
    assert created_at == datetime.datetime(2026, 6, 30, 16, 30)
    assert period_start == datetime.datetime(2026, 6, 30, 16, 0)
    assert proxy._bandwidth_month_context(just_after_midnight) == (
        "2026-07",
        period_start,
    )


class _Rows:
    empty = False

    def __init__(self, count):
        self.count = count

    def iterrows(self):
        for index in range(self.count):
            yield index, {
                "endpoint": f"https://push.invalid/{index}",
                "user_id": f"u{index}",
                "subscription_json": "{}",
            }


class _PushDb:
    def __init__(self, count):
        self.rows = _Rows(count)

    def query(self, _sql, _params):
        return self.rows

    def execute(self, _sql, _params):
        return None


def test_overlapping_pushes_share_one_process_bounded_executor():
    active = 0
    maximum = 0
    lock = threading.Lock()
    saturated = threading.Event()
    release = threading.Event()

    def send(_subscription, _title, _body, _vapid, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == system_limits.PUSH_SEND_CONCURRENCY:
                saturated.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return True, ""

    vapid = {"public_key": "p", "private_key": "s", "subject": "https://x"}
    with ThreadPoolExecutor(max_workers=2) as callers:
        futures = [
            callers.submit(
                push.notify_committee,
                _PushDb(system_limits.PUSH_SEND_CONCURRENCY + 2),
                vapid,
                "title",
                "body",
                send_fn=send,
            )
            for _ in range(2)
        ]
        assert saturated.wait(timeout=1)
        release.set()
        assert all(future.result(timeout=2) for future in futures)
    assert maximum == system_limits.PUSH_SEND_CONCURRENCY


def test_html_and_lateness_manager_cache_contract(monkeypatch):
    response = asyncio.run(proxy.developer_settings_page())
    html = response.body.decode("utf-8")
    assert response.headers["cache-control"].startswith("private,")
    assert f"/dev-settings/lateness-managers.js?v={proxy.APP_VERSION}" in html
    assert "__APP_VERSION__" not in html

    script = asyncio.run(proxy.developer_lateness_managers_script())
    assert script.headers["cache-control"] == "no-cache"
    assert "immutable" not in script.headers["cache-control"]

    monkeypatch.setattr(proxy, "_require_committee_user", lambda _request: "u")
    monkeypatch.setitem(
        proxy.ROOMS,
        "ABC",
        SimpleNamespace(code="ABC", phase="lobby", mode="A"),
    )
    request = proxy.Request(
        {"type": "http", "method": "GET", "path": "/ai-coach/room/ABC", "headers": []}
    )
    room_page = asyncio.run(proxy.ai_coach_room_page("ABC", request))
    assert room_page.headers["cache-control"] == "no-store"


def test_registry_contains_new_bounded_limits_and_no_dead_finalizer_limit():
    specs = system_limits.effective_limits()
    assert not [name for name, spec in specs.items() if spec["maximum"] is None]
    assert specs["UVICORN_WS_MAX_QUEUE"]["value"] == 4
    assert "GEMINI_WS_MAX_QUEUE" not in specs
    assert "ROOM_AUDIO_FRAME_MAX_BYTES" not in specs
    assert specs["REQUEST_BODY_BUFFER_CONCURRENCY"]["maximum"] == 4
    assert specs["RAG_SCHEMA_CHECK_TTL_SECONDS"]["value"] == 300
    assert "R2_FINALIZER_BATCH_SIZE" not in specs
    assert system_limits.LIVE_FREE_MAX_MINUTES == 10
    assert specs["LIVE_FREE_MAX_MINUTES"]["maximum"] == 10
    assert (
        inspect.signature(media_logic.video_admin_data).parameters["page_size"].default
        == system_limits.API_PAGE_SIZE
    )
