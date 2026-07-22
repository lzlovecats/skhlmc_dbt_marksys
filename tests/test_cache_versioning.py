"""Offline HTML cache-buster contracts for versioned shared assets."""

import asyncio

from fastapi import Request

import deploy.proxy as proxy


def _request(path):
    return Request({
        "type": "http", "method": "GET", "path": path,
        "query_string": b"", "headers": [],
    })


def test_ai_coach_html_versions_shared_ai_parity_without_placeholder(monkeypatch):
    monkeypatch.setattr(
        proxy, "interactive_features_suspension", lambda _request: {"active": False}
    )
    response = asyncio.run(proxy.ai_coach_page(_request("/ai-coach")))
    html = response.body.decode("utf-8")

    assert f'href="/shared/app-shell.css?v={proxy.APP_VERSION}"' in html
    assert f'src="/shared/vote-ui.js?v={proxy.APP_VERSION}"' in html
    assert f'src="/shared/markdown.js?v={proxy.APP_VERSION}"' in html
    assert f'src="/shared/ai-parity.js?v={proxy.APP_VERSION}"' in html
    assert proxy.LMC_AI_PRACTICE_LABEL in html
    assert "__APP_VERSION__" not in html
    assert "__LMC_AI_PRACTICE_LABEL__" not in html


def test_local_ai_practice_versions_assets_and_server_limits(monkeypatch):
    monkeypatch.setattr(
        proxy, "interactive_features_suspension", lambda _request: {"active": False}
    )
    monkeypatch.setattr(proxy, "require_page_user", lambda *_args: "alice")
    response = asyncio.run(
        proxy.local_ai_practice_page(_request("/ai-coach/local-practice"))
    )
    html = response.body.decode("utf-8")

    assert response.headers["cache-control"] == "no-store"
    assert f'href="/shared/app-shell.css?v={proxy.APP_VERSION}"' in html
    assert (
        f'src="/ai-coach/local-practice/app.js?v={proxy.APP_VERSION}"' in html
    )
    assert f'maxlength="{proxy.LMC_AI_MESSAGE_MAX_CHARS}"' in html
    assert proxy.LMC_AI_PRACTICE_LABEL in html
    assert "__APP_VERSION__" not in html
    assert "__LMC_AI_PRACTICE_LABEL__" not in html
    assert "__LOCAL_PRACTICE_MESSAGE_MAX_CHARS__" not in html


def test_score_sheet_confirmation_versions_shared_assets_and_is_private():
    response = asyncio.run(proxy.score_sheet_confirmation_page())
    html = response.body.decode("utf-8")

    assert response.headers["cache-control"] == "no-store"
    assert f'href="/shared/app-shell.css?v={proxy.APP_VERSION}"' in html
    assert f'src="/shared/vote-ui.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html


def test_ai_training_versions_shared_vote_ui_without_placeholder():
    response = asyncio.run(proxy.ai_training_page())
    html = response.body.decode("utf-8")

    assert f'src="/shared/vote-ui.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html


def test_lmc_ai_versions_the_assistant_avatar_without_placeholder(monkeypatch):
    monkeypatch.setattr(
        proxy, "interactive_features_suspension", lambda _request: {"active": False}
    )
    response = asyncio.run(proxy.lmc_ai_page(_request("/lmc-ai")))
    html = response.body.decode("utf-8")

    assert (
        f'data-avatar-src="/lmc-ai/shiba-avatar.jpg?v={proxy.APP_VERSION}"'
        in html
    )
    assert "__APP_VERSION__" not in html


def test_developer_settings_revalidates_during_inline_api_contract_transition():
    response = asyncio.run(proxy.developer_settings_page())
    html = response.body.decode("utf-8")

    assert response.headers["cache-control"] == "no-cache"
    assert "__APP_VERSION__" not in html


def test_match_photos_html_versions_metadata_editor_script():
    response = asyncio.run(proxy.match_photos_page())
    html = response.body.decode("utf-8")

    assert f'src="/shared/server-tables.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html


def test_video_replay_html_versions_shared_return_navigation_script():
    response = asyncio.run(proxy.video_replay_page())
    html = response.body.decode("utf-8")

    assert f'src="/shared/server-tables.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html


def test_team_history_html_versions_shared_return_navigation_script():
    response = asyncio.run(proxy.team_history_page())
    html = response.body.decode("utf-8")

    assert f'src="/shared/server-tables.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html


def test_judging_html_versions_shared_judging_script():
    response = asyncio.run(proxy.judging_page())
    html = response.body.decode("utf-8")

    assert f'src="/shared/judging-ux.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html


def test_chairperson_html_versions_all_shared_assets():
    response = asyncio.run(proxy.chairperson_page())
    html = response.body.decode("utf-8")

    assert f'href="/shared/app-shell.css?v={proxy.APP_VERSION}"' in html
    assert f'src="/shared/vote-ui.js?v={proxy.APP_VERSION}"' in html
    assert f'src="/shared/markdown.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html


def test_date_input_pages_version_mobile_layout_and_shared_scripts():
    pages = (
        proxy.developer_settings_page,
        proxy.lateness_fund_page,
        proxy.match_info_page,
        proxy.match_photos_page,
        proxy.recent_matches_page,
        proxy.registration_admin_page,
        proxy.team_history_page,
    )

    for page in pages:
        response = asyncio.run(page())
        html = response.body.decode("utf-8")
        assert f'href="/shared/app-shell.css?v={proxy.APP_VERSION}"' in html
        assert f'src="/shared/vote-ui.js?v={proxy.APP_VERSION}"' in html
        assert "__APP_VERSION__" not in html
