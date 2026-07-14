"""Offline HTML cache-buster contracts for versioned shared assets."""

import asyncio

import deploy.proxy as proxy


def test_ai_coach_html_versions_shared_ai_parity_without_placeholder():
    response = asyncio.run(proxy.ai_coach_page())
    html = response.body.decode("utf-8")

    assert f'src="/shared/ai-parity.js?v={proxy.APP_VERSION}"' in html
    assert "__APP_VERSION__" not in html
