"""Regressions for the production AI Training blank-page cache failure."""

import asyncio

import deploy.proxy as proxy
from api import ai_training_api
from core import r2_storage
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


def test_training_data_survives_optional_usage_telemetry_outage(monkeypatch):
    class EmptyFrame:
        def to_dict(self, *, orient):
            assert orient == "records"
            return []

    class Db:
        def query(self, _sql, _params=None):
            return EmptyFrame()

    refresh_values = []

    def storage_failure(_db, *, refresh=False):
        refresh_values.append(refresh)
        raise RuntimeError("R2 status unavailable")

    monkeypatch.setattr(ai_training_api, "_ctx", lambda _request: ("member", Db()))
    monkeypatch.setattr(ai_training_api, "_users", lambda _db, _key: [])
    monkeypatch.setattr(
        ai_training_api, "_has_active_voice_consent", lambda _db, _user: False
    )
    monkeypatch.setattr(ai_training_api, "_load_ai_roadmap", lambda: "")
    monkeypatch.setattr(
        proxy,
        "bandwidth_budget_status",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bandwidth unavailable")),
    )
    monkeypatch.setattr(r2_storage, "configured", lambda: True)
    monkeypatch.setattr(r2_storage, "storage_budget_status", storage_failure)

    result = ai_training_api.data(None)

    assert result["user_id"] == "member"
    assert result["bandwidth_budget"] == {"unavailable": True}
    assert result["recording_storage_ready"] is True
    assert result["storage_budget"] == {"unavailable": True}
    assert refresh_values == [False]
