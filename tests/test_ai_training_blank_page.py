"""Regressions for the production AI Training blank-page cache failure."""

import asyncio

import deploy.proxy as proxy
from version import APP_VERSION


def test_ai_training_page_has_visible_fallback_and_versioned_script():
    response = asyncio.run(proxy.ai_training_page())
    html = response.body.decode("utf-8")

    # If an old cached script crashes before load(), users must still see a
    # recovery action instead of the previous all-hidden blank page.
    assert '<section id="loadFallback" class="card">' in html
    assert "重新載入" in html
    assert f'/ai-training/app.js?v={APP_VERSION}' in html
    assert "__APP_VERSION__" not in html
    assert response.headers["cache-control"] == "no-cache"


def test_ai_training_script_revalidates_and_keeps_adult_only_consent():
    response = asyncio.run(proxy.ai_training_script())
    assert response.headers["cache-control"] == "no-cache"
    assert "immutable" not in response.headers["cache-control"]

    page = (proxy.BASE_DIR / "frontend" / "ai_training" / "index.html").read_text(
        encoding="utf-8"
    )
    script = (proxy.BASE_DIR / "frontend" / "ai_training" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "minorStatus" not in page + script
    assert "guardianConfirmed" not in page + script
    assert '$("loadFallback")' in script
