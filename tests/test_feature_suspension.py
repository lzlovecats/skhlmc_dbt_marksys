"""Scheduled Vote and AI Coach suspension contracts."""

import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi import Request

from api import access
from ai_name import LMC_AI_MENTION_TAG
from core.feature_suspension import (
    SUSPENSION_END_KEY,
    SUSPENSION_START_KEY,
    suspension_status,
    validate_suspension_window,
)
from deploy import proxy


ROOT = Path(__file__).resolve().parents[1]
HKT = ZoneInfo("Asia/Hong_Kong")


class _ConfigDb:
    def __init__(self, values):
        self.values = values

    def query(self, _sql, params=None):
        keys = set((params or {}).values())
        return pd.DataFrame(
            [
                {"key": key, "value": value}
                for key, value in self.values.items()
                if key in keys
            ]
        )


def test_suspension_window_uses_half_open_hong_kong_interval():
    values = {
        SUSPENSION_START_KEY: "2026-07-18T14:00",
        SUSPENSION_END_KEY: "2026-07-18T16:00",
    }

    before = suspension_status(
        _ConfigDb(values), dt.datetime(2026, 7, 18, 13, 59, tzinfo=HKT)
    )
    active = suspension_status(
        _ConfigDb(values), dt.datetime(2026, 7, 18, 14, 0, tzinfo=HKT)
    )
    ended = suspension_status(
        _ConfigDb(values), dt.datetime(2026, 7, 18, 16, 0, tzinfo=HKT)
    )

    assert before["scheduled"] is True and before["active"] is False
    assert active["active"] is True and active["retry_after_seconds"] == 7200
    assert ended["active"] is False
    assert "2026年7月18日 16:00" in active["message"]


def test_suspension_window_validation_allows_clear_and_rejects_partial_or_reverse():
    assert validate_suspension_window("", "") == ("", "")
    with pytest.raises(ValueError, match="開始及結束"):
        validate_suspension_window("2026-07-18T14:00", "")
    with pytest.raises(ValueError, match="結束時間必須遲於開始時間"):
        validate_suspension_window(
            "2026-07-18T16:00", "2026-07-18T14:00",
            now=dt.datetime(2026, 7, 18, 12, 0, tzinfo=HKT),
        )
    with pytest.raises(ValueError, match="結束時間必須在未來"):
        validate_suspension_window(
            "2026-07-18T10:00", "2026-07-18T11:00",
            now=dt.datetime(2026, 7, 18, 12, 0, tzinfo=HKT),
        )


def test_developer_ui_groups_roles_and_bypass_under_accounts_and_has_schedule_controls():
    source = (ROOT / "frontend" / "dev_settings" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'data-pane="accounts">帳戶及權限' in source
    assert 'data-pane="aiServices">AI 服務' in source
    assert 'data-pane="system">系統及通知' in source
    assert 'id="featureSuspensionStart"' in source
    assert 'id="featureSuspensionEnd"' in source
    assert 'id="saveFeatureSuspension"' in source
    assert 'id="clearFeatureSuspension"' in source


def test_temporary_bypass_uses_checkbox_picker_for_true_multi_select():
    source = (ROOT / "frontend" / "dev_settings" / "index.html").read_text(
        encoding="utf-8"
    )

    assert '<select id="bypassUsers"' not in source
    assert 'id="bypassUsersSearch"' in source
    assert 'id="bypassUsersOptions" class="account-options"' in source
    assert 'id="bypassUsersSummary" class="caption"' in source
    assert re.search(
        r'renderAccountPicker\(\s*"bypassUsers",\s*\[\],\s*'
        r'data\.inactive_accounts\s*\|\|\s*\[\]\s*,?\s*\)',
        source,
    )
    assert 'const users = selectedAccounts("bypassUsers");' in source


def test_feature_pages_have_server_gate_calls_and_versioned_shells():
    proxy_source = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")
    vote = (ROOT / "frontend" / "vote" / "index.html").read_text(encoding="utf-8")
    coach = (ROOT / "frontend" / "ai_coach" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "_scheduled_feature_page_block" in proxy_source
    assert "require_interactive_features_available" in proxy_source
    for source in (vote, coach):
        assert '/shared/app-shell.css?v=__APP_VERSION__' in source


def test_active_window_returns_no_store_503_page_with_retry_after(monkeypatch):
    monkeypatch.setattr(proxy, "interactive_features_suspension", lambda _request: {
        "active": True,
        "message": "暫停至指定時間。",
        "retry_after_seconds": 90,
    })
    request = Request({
        "type": "http", "method": "GET", "path": "/vote",
        "query_string": b"", "headers": [],
    })

    response = asyncio.run(proxy.vote_page(request))

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "90"
    assert "暫停至指定時間" in response.body.decode("utf-8")


def test_vote_shell_versions_shared_assets_when_window_is_inactive(monkeypatch):
    monkeypatch.setattr(
        proxy, "interactive_features_suspension", lambda _request: {"active": False}
    )
    request = Request({
        "type": "http", "method": "GET", "path": "/vote",
        "query_string": b"", "headers": [],
    })

    html = asyncio.run(proxy.vote_page(request)).body.decode("utf-8")

    assert f'/shared/app-shell.css?v={proxy.APP_VERSION}' in html
    assert f'/shared/vote-ui.js?v={proxy.APP_VERSION}' in html
    assert f'/shared/markdown.js?v={proxy.APP_VERSION}' in html
    assert "__APP_VERSION__" not in html
    assert "__LMC_AI_MENTION_TAG_JSON__" not in html
    assert (
        f"const LOCAL_AI_MENTION_TAG = "
        f"{json.dumps(LMC_AI_MENTION_TAG, ensure_ascii=False)};"
    ) in html


def test_developer_session_bypasses_only_the_schedule_gate(monkeypatch):
    monkeypatch.setattr(access, "has_developer_session", lambda _request: True)

    status = access.interactive_features_suspension(object())

    assert status["active"] is False
    assert status["developer_bypass"] is True


def test_database_acquisition_failure_keeps_fail_open_contract(monkeypatch):
    monkeypatch.setattr(access, "has_developer_session", lambda _request: False)

    def unavailable_db():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(proxy, "get_vote_db", unavailable_db)

    status = access.interactive_features_suspension(object())

    assert status["configured"] is False
    assert status["active"] is False
    assert status["retry_after_seconds"] == 0
