"""Official match-format persistence used by competition-day tools."""

from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from api.match_info_api import SaveBody
from core import match_logic
from debate_timing import DEBATE_FORMATS


ROOT = Path(__file__).resolve().parents[1]


def test_save_body_accepts_only_supported_format_and_linked_free_minutes():
    linked = SaveBody(
        match_id="M1", debate_format="聯中", free_debate_minutes=5,
    )
    assert linked.debate_format == "聯中"
    assert linked.free_debate_minutes == 5

    with pytest.raises(ValidationError, match="有效的賽制"):
        SaveBody(match_id="M1", debate_format="未知賽制")
    with pytest.raises(ValidationError, match="只有聯中"):
        SaveBody(
            match_id="M1",
            debate_format="校園隨想",
            free_debate_minutes=5,
        )
    with pytest.raises(ValidationError):
        SaveBody(
            match_id="M1", debate_format="聯中", free_debate_minutes=10.1,
        )


def test_match_records_include_official_format_and_free_minutes():
    class Db:
        def query(self, statement, _params=None):
            if "FROM matches" in statement and "FROM debaters" not in statement:
                return pd.DataFrame([
                    {
                        "match_id": "M1",
                        "match_date": "2026-07-14",
                        "match_time": "16:00",
                        "topic_text": "辯題",
                        "pro_team": "甲隊",
                        "con_team": "乙隊",
                        "debate_format": "聯中",
                        "free_debate_minutes": 6.5,
                        "access_code_hash": None,
                        "review_password_hash": None,
                    }
                ])
            return pd.DataFrame(
                columns=["match_id", "side", "position", "debater_name"]
            )

    records = match_logic._match_records(Db())
    assert records[0]["debate_format"] == "聯中"
    assert records[0]["free_debate_minutes"] == 6.5


def test_domain_save_persists_valid_format_and_clears_irrelevant_minutes():
    statements = []

    class Result:
        def fetchone(self):
            return type("Row", (), {
                "_mapping": {
                    "access_code_hash": None,
                    "review_password_hash": None,
                }
            })()

        def scalar(self):
            return False

    class Session:
        def execute(self, statement, params=None):
            statements.append((" ".join(str(statement).split()), params))
            return Result()

    class Db:
        @contextmanager
        def transaction(self):
            yield Session()

    result = match_logic.save_match(
        {
            "match_id": "M1",
            "debate_format": "星島",
            "free_debate_minutes": None,
        },
        db=Db(),
    )

    assert result["ok"] is True
    update = next(item for item in statements if item[0].startswith("UPDATE matches"))
    assert "debate_format=:format" in update[0]
    assert "free_debate_minutes=:free_minutes" in update[0]
    assert update[1]["format"] == "星島"
    assert update[1]["free_minutes"] is None


@pytest.mark.parametrize(
    ("debate_format", "minutes", "message"),
    [
        ("未知", None, "有效的賽制"),
        ("聯中", 1.5, "2 至 10"),
        ("聯中", float("nan"), "2 至 10"),
        ("校園隨想", 5, "只有聯中"),
    ],
)
def test_domain_save_rejects_invalid_format_metadata(
    debate_format, minutes, message,
):
    result = match_logic.save_match(
        {
            "match_id": "M1",
            "debate_format": debate_format,
            "free_debate_minutes": minutes,
        },
        db=object(),
    )
    assert result["ok"] is False
    assert message in result["message"]


def test_schema_migration_and_match_ui_share_the_same_contract():
    schema = (ROOT / "schema.py").read_text(encoding="utf-8")
    migration = (
        ROOT / "migrations" / "20260714_0005_match_debate_format.up.sql"
    ).read_text(encoding="utf-8")
    rollback = (
        ROOT / "migrations" / "20260714_0005_match_debate_format.down.sql"
    ).read_text(encoding="utf-8")
    ui = (ROOT / "frontend" / "match_info" / "index.html").read_text(
        encoding="utf-8"
    )

    for source in (schema, migration):
        assert "debate_format" in source
        assert "free_debate_minutes" in source
        assert "matches_debate_format_check" in source
        assert "matches_free_debate_minutes_check" in source
        for debate_format in DEBATE_FORMATS:
            assert debate_format in source
    assert "DROP COLUMN free_debate_minutes" in rollback
    assert "DROP COLUMN debate_format" in rollback
    assert 'id="format"' in ui
    assert 'id="freeMinutes"' in ui
    assert 'min="2"' in ui and 'max="10"' in ui and 'value="5"' in ui
    assert 'body.debate_format === "聯中"' in ui
