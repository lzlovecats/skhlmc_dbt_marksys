"""Competition Prep contracts: retention, privacy, UI placement and strict drill mode."""

import asyncio
import json
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import account_access
from api import competition_prep_api
import deploy.proxy as proxy
import prompts
import schema
from core import competition_prep_logic as logic


ROOT = Path(__file__).resolve().parents[1]


def test_retention_keeps_seven_full_post_match_days_in_hong_kong():
    expiry = logic.expiry_for_match_date(date(2026, 7, 18))

    assert expiry == datetime(2026, 7, 26, 0, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    assert expiry.utcoffset().total_seconds() == 8 * 60 * 60


def test_competition_prep_is_member_only_and_has_three_collaborator_roles():
    assert logic.ROLES == ("owner", "editor", "viewer")
    assert account_access.account_can_access("ordinary-member", "competition_prep")
    assert not account_access.account_can_access(
        account_access.KIOSK_ACCOUNT_ID, "competition_prep",
    )


def test_schema_and_migration_keep_structured_prep_data_out_of_r2():
    up = (ROOT / "migrations" / "20260718_0002_competition_prep.up.sql").read_text()
    down = (ROOT / "migrations" / "20260718_0002_competition_prep.down.sql").read_text()
    expected = {
        schema.TABLE_COMPETITION_PREP_PROJECTS,
        schema.TABLE_COMPETITION_PREP_MEMBERS,
        schema.TABLE_COMPETITION_PREP_MANUSCRIPTS,
        schema.TABLE_COMPETITION_PREP_STRATEGY_CARDS,
        schema.TABLE_COMPETITION_PREP_EVIDENCE_CARDS,
        schema.TABLE_COMPETITION_PREP_WEAKNESSES,
        schema.TABLE_COMPETITION_PREP_AI_RUNS,
    }
    for table in expected:
        assert f"CREATE TABLE {table}" in up
        assert f"REVOKE ALL PRIVILEGES ON TABLE {table}" in up
        assert f"DROP TABLE IF EXISTS {table}" in down
    assert "competition_prep" in schema.CREATE_AI_FUND_USAGE_LOGS
    assert "r2_key" not in up.lower()
    assert "bytea" not in up.lower()


def test_competition_prep_migration_guards_optional_browser_roles():
    up = (ROOT / "migrations" / "20260718_0002_competition_prep.up.sql").read_text()

    assert "FROM PUBLIC, anon, authenticated" not in up
    assert "FROM PUBLIC;" in up
    assert "SELECT rolname FROM pg_roles" in up
    assert "rolname IN ('anon', 'authenticated')" in up


def test_ui_has_nested_mobile_prep_tabs_and_separate_search_tools():
    html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text()
    script = (ROOT / "frontend" / "shared" / "competition-prep.js").read_text()

    assert 'data-pane="prep"' in html
    assert 'data-pane="strategy"' not in html
    assert 'data-pane="review"' not in html
    assert html.count("data-prep-pane=") == 5
    for pane in (
        "prepProject", "prepManuscripts", "prepStrategy", "prepEvidence",
        "prepWeakness",
    ):
        assert f'data-prep-pane="{pane}"' in html
    for subpane in (
        "prepProjectOverview", "prepProjectCreate", "prepProjectMembers",
        "prepManuscriptListPane", "prepManuscriptEditPane", "prepAuditPane",
        "prepReviewPane", "prepStrategyListPane", "prepStrategyCreatePane",
        "prepStrategyPlanPane", "prepAttackPane", "prepEvidenceListPane",
        "prepEvidenceCreatePane", "prepWeaknessListPane",
        "prepWeaknessCreatePane",
    ):
        assert f'data-prep-subpane="{subpane}"' in html
    assert 'id="bandwidthPanel"' in html
    assert 'id="modelPanel"' in html
    assert "function switchPrepSubPane" in script
    assert 'matchMedia("(max-width: 800px)")' in script
    assert 'panel.open = false' in script
    assert "scrollIntoView" in script
    assert 'data-pane="research"' in html
    assert 'data-pane="fact"' in html
    assert "AI模擬攻擊" in html
    assert "我方論證責任" in html
    assert "competition-prep.js?v=__APP_VERSION__" in html


def test_project_overview_supports_inline_detail_editing_for_editors():
    html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text()
    script = (ROOT / "frontend" / "shared" / "competition-prep.js").read_text()

    for element_id in (
        "prepEditProject", "prepEditProjectForm", "prepEditTitle",
        "prepEditOpponent", "prepEditMatchDate", "prepEditMatchTime",
        "prepEditSide", "prepEditFormat", "prepEditTopic",
        "prepCancelProjectEdit",
    ):
        assert f'id="{element_id}"' in html
    assert 'method: "PATCH"' in script
    assert 'revision: Number(project.revision)' in script
    assert '$("prepEditProject").disabled = !canEdit()' in script
    assert '項目資料已更新。' in script


def test_prep_manuscript_contract_has_other_notes_and_third_deputy_only_for_joint_school():
    html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text()
    script = (ROOT / "frontend" / "shared" / "competition-prep.js").read_text()
    api_source = (ROOT / "api" / "competition_prep_api.py").read_text()
    migration = (
        ROOT / "migrations" / "20260718_0002_competition_prep.up.sql"
    ).read_text()

    assert '"other"' in api_source
    assert '"other"' in script
    assert "其他比賽有關資料" in script
    assert "'interaction', 'other'" in migration
    assert 'item.value !== "dep3" || debateFormat === "聯中"' in script
    assert '$("reviewFormat").value === "聯中"' in (
        ROOT / "frontend" / "shared" / "ai-parity.js"
    ).read_text()
    assert 'id="reviewStage"' not in html
    assert 'id="speechTimer"' not in html
    assert 'id="speechClock"' not in html


def test_prep_ui_separates_edit_and_practice_and_identifies_evidence_target():
    html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text()
    script = (ROOT / "frontend" / "shared" / "competition-prep.js").read_text()
    select_manuscript = script.split("function selectManuscript", 1)[1].split(
        "function resetManuscript", 1,
    )[0]

    assert 'button("編輯"' in script
    assert 'button("練習"' in script
    assert "編輯／練習" not in script
    assert 'prepManuscriptSlot").addEventListener("change"' in script
    assert 'switchPrepPane("prepManuscripts", { scroll: false })' in select_manuscript
    assert 'switchPrepSubPane("prepManuscripts", subPaneId)' in select_manuscript
    assert 'target === "practice" ? "prepReviewPane"' in select_manuscript
    assert "檢視資料出處" in script and "開來源" not in script
    assert "目前項目：" in script
    assert "只有項目擁有者或編輯者" in html
    assert 'className = "notice warn"' in script
    assert "#prepAttack" in html and "margin-top" in html


def test_new_prep_captions_use_written_chinese():
    html = (ROOT / "frontend" / "ai_coach" / "index.html").read_text()
    script = (ROOT / "frontend" / "shared" / "competition-prep.js").read_text()

    for colloquial in (
        "每個會項目", "「搵料易」同", "你可由呢度", "唔會提示答案",
        "你係${roleLabels", "Free De：AI 進行期間", "而家未有比賽場次",
        "呢個模型暫時未能使用", "而家模型為", "所需嘅搜尋模型",
    ):
        assert colloquial not in html + script

    assert "manuscriptStatusLabels" in script
    assert "weaknessCategoryLabels" in script
    assert "weaknessStatusLabels" in script


def test_team_audit_uses_chinese_burden_term_and_drill_withholds_coaching():
    audit = prompts.COMPETITION_PREP_TEAM_AUDIT_SYSTEM_PROMPT
    weakness = prompts.build_weakness_live_prompt(
        "測試辯題", "正方",
        {"title": "因果鏈跳步", "description": "欠缺中間機制"},
        "項目材料",
    )

    assert "我方論證責任" in audit
    assert "burden" not in audit.lower()
    assert "練習進行期間只可以扮演對手攻擊" in weakness
    assert "絕對唔可以畀提示、教學、評分、示範答案" in weakness
    assert "只有系統宣告完場" in weakness
    assert "通過／部分通過／未通過" in prompts.LIVE_RUNTIME_PROMPTS["feedback_weakness"]


class _PrepResult:
    def __init__(self, *, scalar=0, row=None):
        self._scalar = scalar
        self._row = row

    def scalar_one(self):
        return self._scalar

    def fetchone(self):
        return self._row


class _PrepConnection:
    def __init__(self, *, existing_member=None):
        self.events = []
        self.existing_member = existing_member

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).lower().split())
        if "pg_advisory_xact_lock" in sql:
            self.events.append("lock")
            return _PrepResult()
        if sql.startswith("select role from competition_prep_members"):
            self.events.append("existing")
            row = (self.existing_member,) if self.existing_member else None
            return _PrepResult(row=row)
        if sql.startswith("select count"):
            self.events.append("count")
            return _PrepResult(scalar=1)
        if sql.startswith("insert into competition_prep_projects"):
            self.events.append("insert-project")
            return _PrepResult(row=(7,))
        if sql.startswith("insert into competition_prep_members"):
            self.events.append("insert-member")
            return _PrepResult()
        if sql.startswith("insert into"):
            self.events.append("insert")
            return _PrepResult(scalar=9)
        raise AssertionError(f"unexpected transaction SQL: {sql}")


class _PrepDb:
    def __init__(self, *, existing_member=None, debate_format="聯中"):
        self.connection = _PrepConnection(existing_member=existing_member)
        self.debate_format = debate_format

    def query(self, sql, _params=None):
        normalized = " ".join(sql.lower().split())
        if "select m.role" in normalized:
            return pd.DataFrame([{"role": "owner"}])
        if "select debate_format" in normalized:
            return pd.DataFrame([{"debate_format": self.debate_format}])
        raise AssertionError(f"limit-sensitive query escaped transaction: {normalized}")

    @contextmanager
    def transaction(self):
        yield self.connection


def test_project_limit_check_and_insert_share_a_user_advisory_lock():
    db = _PrepDb()

    project_id = logic.create_project(db, "alice", {
        "title": "測試項目", "topic_text": "測試辯題", "our_side": "pro",
        "debate_format": "聯中", "match_date": "2099-01-02",
    })

    assert project_id == 7
    assert db.connection.events == ["lock", "count", "insert-project", "insert-member"]


def test_member_limit_check_and_upsert_share_a_project_advisory_lock():
    db = _PrepDb()

    logic.set_member(db, 3, "alice", "bob", "editor")

    assert db.connection.events == ["lock", "existing", "count", "insert-member"]


def test_collection_limit_checks_and_inserts_share_project_advisory_locks():
    calls = (
        (logic.save_manuscript, {
            "slot": "main", "title": "主辯稿", "body": "內容", "status": "draft",
        }),
        (logic.save_strategy_card, {"kind": "argument", "title": "論點"}),
        (logic.save_evidence_card, {"claim_text": "論據"}),
        (logic.save_weakness, {"title": "弱點", "category": "logic"}),
    )
    for save, payload in calls:
        db = _PrepDb()
        assert save(db, 3, "alice", payload) == 9
        assert db.connection.events == ["lock", "count", "insert"]


def test_non_joint_school_project_rejects_third_deputy_manuscript():
    with pytest.raises(logic.PrepError, match="三副"):
        logic.save_manuscript(_PrepDb(debate_format="校園隨想"), 3, "alice", {
            "slot": "dep3", "title": "三副稿", "body": "內容", "status": "draft",
        })


def test_competition_prep_ui_invalidates_stale_loads_and_captures_mutation_context():
    source = (ROOT / "frontend" / "shared" / "competition-prep.js").read_text()
    clear_project = source.split("function clearProject()", 1)[1].split(
        "async function loadProject", 1,
    )[0]
    mutate = source.split("async function mutate", 1)[1].split(
        "async function removeMember", 1,
    )[0]
    run_ai = source.split("async function runPrepAi", 1)[1].split(
        "async function startWeakness", 1,
    )[0]

    assert "state.loadGeneration += 1" in clear_project
    assert "const projectId = state.projectId" in mutate
    assert "await loadProject(projectId)" in mutate
    assert "state.projectId !== projectId" in mutate
    guard = (
        "if (state.projectId !== projectId || "
        "state.loadGeneration !== generation) return;"
    )
    assert guard in run_ai
    assert run_ai.index(guard) < run_ai.index('$(outputId).innerHTML')


def test_ai_input_fingerprint_tracks_context_without_storing_plaintext():
    first = logic.ai_input_fingerprint({"context": "第一版全隊稿件"})
    second = logic.ai_input_fingerprint({"context": "第二版全隊稿件"})

    assert first != second
    assert len(first) == 64
    assert "稿件" not in first


def test_prep_ai_persists_result_before_usage_and_reuses_completed_operation(monkeypatch):
    events = []
    snapshots = []
    bundle = {
        "project": {"revision": 2}, "role": "owner",
        "manuscripts": [], "strategy_cards": [], "evidence_cards": [], "weaknesses": [],
    }
    monkeypatch.setattr(competition_prep_api, "_user", lambda _request: "alice")
    monkeypatch.setattr(competition_prep_api, "_db", lambda: object())
    monkeypatch.setattr(
        competition_prep_api, "require_interactive_features_available", lambda _request: None,
    )
    monkeypatch.setattr(logic, "project_bundle", lambda *_args: bundle)
    monkeypatch.setattr(logic, "build_ai_context", lambda _bundle: "context")
    def claim(*args, **_kwargs):
        events.append("claim")
        snapshots.append(args[6])
        return {"state": "claimed"}

    monkeypatch.setattr(logic, "claim_ai_run", claim, raising=False)
    monkeypatch.setattr(
        logic, "complete_ai_run",
        lambda *_args, **_kwargs: events.append("complete"),
        raising=False,
    )
    monkeypatch.setattr(logic, "release_ai_run", lambda *_args: events.append("release"), raising=False)
    monkeypatch.setattr(proxy, "_bandwidth_essential_gate_error", lambda: None)
    from api import ai_coach_api
    monkeypatch.setattr(ai_coach_api, "_runtime_model_settings", lambda _db: (("gemini",), "model"))
    monkeypatch.setattr(ai_coach_api, "_config", lambda *_args: {"provider": "gemini"})
    monkeypatch.setattr(ai_coach_api, "_require_enabled_model", lambda *_args: None)

    async def generate(*_args, on_provider_attempt=None, **_kwargs):
        events.append("provider")
        on_provider_attempt()
        return "result", {}

    monkeypatch.setattr(ai_coach_api, "_generate", generate)
    monkeypatch.setattr(ai_coach_api, "_usage", lambda *_args, **_kwargs: events.append("usage"))
    body = competition_prep_api.AiRunBody(
        run_type="team_audit", model_label="model", operation_id="prep-operation-12345",
    )

    response = asyncio.run(competition_prep_api.ai_run(3, body, object()))

    assert json.loads(response.body)["markdown"] == "result"
    assert events == ["claim", "provider", "complete", "usage"]
    assert snapshots == [{
        "project_revision": 2,
        "input_sha256": logic.ai_input_fingerprint({"context": "context"}),
    }]

    monkeypatch.setattr(
        logic, "claim_ai_run",
        lambda *_args, **_kwargs: {"state": "completed", "output": "cached"},
        raising=False,
    )

    async def should_not_call_provider(*_args, **_kwargs):
        raise AssertionError("completed operation must not call provider again")

    monkeypatch.setattr(ai_coach_api, "_generate", should_not_call_provider)
    cached = asyncio.run(competition_prep_api.ai_run(3, body, object()))
    assert json.loads(cached.body) == {
        "ok": True, "run_id": "prep-operation-12345",
        "markdown": "cached", "cached": True,
    }


def test_live_renderer_selects_weakness_feedback_without_leaking_placeholder():
    html = proxy._render_live_debate_html(
        "", "weakness prompt", 2.5, [], False,
        session_label="弱點訓練", feedback_mode="weakness",
    )

    assert 'const LIVE_FEEDBACK_MODE = "weakness"' in html
    assert "LIVE_PROMPTS.feedback_weakness" in html
    assert "__LIVE_FEEDBACK_MODE__" not in html
    assert "開始弱點訓練" in html
