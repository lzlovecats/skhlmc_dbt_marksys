"""Offline HTML cache-buster contracts for versioned shared assets."""

import asyncio

import deploy.proxy as proxy


def test_ai_coach_html_versions_shared_ai_parity_without_placeholder():
    response = asyncio.run(proxy.ai_coach_page())
    html = response.body.decode("utf-8")

    assert f'src="/shared/ai-parity.js?v={proxy.APP_VERSION}"' in html
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
