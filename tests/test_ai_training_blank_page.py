"""Regressions for the production AI Training blank-page cache failure."""

import asyncio
from datetime import datetime

import deploy.proxy as proxy
from api import ai_training_api
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


def test_training_data_normalises_nullable_database_values(monkeypatch):
    class Frame:
        def __init__(self, rows):
            self.rows = rows

        def to_dict(self, *, orient):
            assert orient == "records"
            return self.rows

    class Db:
        def query(self, sql, _params=None):
            if "FROM tts_scripts" in sql:
                return Frame(
                    [
                        {
                            "id": "short_001",
                            "category": "test",
                            "text": "測試句子",
                            "is_active": True,
                            "sort_order": 1,
                            "script_type": "short",
                            "manuscript_id": float("nan"),
                            "manuscript_title": float("nan"),
                        }
                    ]
                )
            return Frame(
                [
                    {
                        "id": 1,
                        "script_id": "short_001",
                        "status": "pending",
                        "created_at": datetime(2026, 7, 14, 12, 0),
                    }
                ]
            )

    monkeypatch.setattr(ai_training_api, "_ctx", lambda _request: ("member", Db()))
    monkeypatch.setattr(ai_training_api, "_users", lambda _db, _key: [])
    monkeypatch.setattr(
        ai_training_api, "_has_active_voice_consent", lambda _db, _user: False
    )
    monkeypatch.setattr(ai_training_api, "_load_ai_roadmap", lambda: "")
    monkeypatch.setattr(proxy, "bandwidth_budget_status", lambda **_kwargs: {})

    from core import r2_storage

    monkeypatch.setattr(r2_storage, "configured", lambda: False)

    result = ai_training_api.data(None)

    assert result["scripts"][0]["manuscript_id"] is None
    assert result["scripts"][0]["manuscript_title"] is None
    assert result["my_recordings"][0]["created_at"] == "2026-07-14T12:00:00"
