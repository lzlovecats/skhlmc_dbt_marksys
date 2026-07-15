import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from account_access import (
    ADMIN_ACCOUNT_ID,
    AI_COMMENT_ACCOUNT_ID,
    DEVELOPER_ACCOUNT_ID,
    KIOSK_ACCOUNT_ID,
)
from api import admin_console_api, auth_api, funds_api
from core import auth_logic
from deploy import proxy


def _authenticate_as(monkeypatch, user_id):
    monkeypatch.setattr(proxy, "_require_committee_user", lambda _request: user_id)


def test_kiosk_cannot_open_member_profile_or_funds(monkeypatch):
    _authenticate_as(monkeypatch, KIOSK_ACCOUNT_ID)
    with pytest.raises(HTTPException) as member_error:
        auth_api.me(object())
    assert member_error.value.status_code == 403

    with pytest.raises(HTTPException) as fund_error:
        funds_api._context(object())
    assert fund_error.value.status_code == 403


@pytest.mark.parametrize(
    ("handler", "status"),
    ((proxy.video_view, 403), (proxy.projector_list_matches, 401)),
)
def test_kiosk_cannot_use_video_or_projector_control(monkeypatch, handler, status):
    _authenticate_as(monkeypatch, KIOSK_ACCOUNT_ID)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(handler(object()))
    assert denied.value.status_code == status


@pytest.mark.parametrize(
    "user_id",
    (ADMIN_ACCOUNT_ID, DEVELOPER_ACCOUNT_ID, AI_COMMENT_ACCOUNT_ID),
)
def test_privileged_and_pseudo_accounts_cannot_use_tts_or_ai_rooms(monkeypatch, user_id):
    _authenticate_as(monkeypatch, user_id)
    for call in (
        lambda: proxy.azure_tts(object()),
        lambda: proxy.room_create(object()),
    ):
        with pytest.raises(HTTPException) as denied:
            asyncio.run(call())
        assert denied.value.status_code == 403


def test_kiosk_reaches_the_allowed_tts_and_ai_room_handlers(monkeypatch):
    _authenticate_as(monkeypatch, KIOSK_ACCOUNT_ID)
    monkeypatch.setattr(proxy, "_bandwidth_live_gate_error", lambda: "test limit")
    with pytest.raises(HTTPException) as tts_gate:
        asyncio.run(proxy.azure_tts(object()))
    assert tts_gate.value.status_code == 429

    monkeypatch.setattr(proxy, "ROOMS", {})
    with pytest.raises(HTTPException) as missing_room:
        asyncio.run(proxy.room_info("NONE", object()))
    assert missing_room.value.status_code == 404


@pytest.mark.parametrize(
    ("user_id", "should_reach_rooms"),
    (
        (ADMIN_ACCOUNT_ID, False),
        (DEVELOPER_ACCOUNT_ID, False),
        (AI_COMMENT_ACCOUNT_ID, False),
        (KIOSK_ACCOUNT_ID, True),
    ),
)
def test_room_websocket_applies_the_same_central_policy(
    monkeypatch, user_id, should_reach_rooms,
):
    class Socket:
        cookies = {"committee_user": "signed"}

        def __init__(self):
            self.closed = []

        async def close(self, code):
            self.closed.append(code)

    class Rooms:
        def __init__(self):
            self.called = False

        def get(self, _code):
            self.called = True
            return None

    rooms = Rooms()
    socket = Socket()
    monkeypatch.setattr(proxy, "_verify_committee_token", lambda _token: user_id)
    monkeypatch.setattr(proxy, "ROOMS", rooms)
    asyncio.run(proxy.room_ws(socket, "ROOM"))
    assert socket.closed == [1008]
    assert rooms.called is should_reach_rooms


@pytest.mark.parametrize("reserved", ("admin", "ADMIN", "Developer", "GEMINI", "KIOSK"))
def test_developer_account_creation_rejects_reserved_lookalikes(monkeypatch, reserved):
    monkeypatch.setattr(admin_console_api, "_require", lambda *_args: None)
    monkeypatch.setattr(
        admin_console_api,
        "_db",
        lambda: (_ for _ in ()).throw(AssertionError("reserved id must fail before DB access")),
    )
    body = admin_console_api.AccountBody(user_id=reserved, password="secret")
    with pytest.raises(HTTPException) as denied:
        admin_console_api.create_account(body, object())
    assert denied.value.status_code == 400


def test_developer_account_creation_can_provision_exact_kiosk(monkeypatch):
    executed = []

    class Db:
        def query(self, *_args, **_kwargs):
            return SimpleNamespace(empty=True)

        def execute(self, sql, params):
            executed.append((sql, params))

    monkeypatch.setattr(admin_console_api, "_require", lambda *_args: None)
    monkeypatch.setattr(admin_console_api, "_db", Db)
    monkeypatch.setattr(admin_console_api, "scalar_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(admin_console_api, "hash_password", lambda _password: "hash")
    result = admin_console_api.create_account(
        admin_console_api.AccountBody(user_id=KIOSK_ACCOUNT_ID, password="secret"),
        object(),
    )
    assert result == {"ok": True}
    assert executed[0][1] == {"uid": KIOSK_ACCOUNT_ID, "pw": "hash"}


def test_system_account_login_skips_member_activity_lifecycle(monkeypatch):
    executed = []

    class Db:
        def query(self, *_args, **_kwargs):
            raise AssertionError("system accounts must not query member vote activity")

        def execute(self, sql, params):
            executed.append((sql, params))

    monkeypatch.setattr(auth_logic, "append_login_record", lambda *_args, **_kwargs: None)
    auth_logic.record_login("KIOSK", db=Db())
    assert executed
    assert all("account_status" not in sql for sql, _params in executed)
