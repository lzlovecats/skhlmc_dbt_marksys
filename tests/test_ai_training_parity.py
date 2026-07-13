import inspect
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import HTTPException

from api.ai_training_api import (
    CONSENT_TEXT,
    ConsentBody,
    _feature_schema_state,
    _has_active_voice_consent,
    _prune_audit,
    _require_rag_vector_schema,
    _segments,
    consent,
    recording,
    readiness,
    withdraw,
    withdraw_llm,
)
from core.ai_training_defaults import DEFAULT_TTS_SCRIPT_BANK

ROOT = Path(__file__).resolve().parents[1]


def test_default_script_bank_matches_legacy_and_is_unique():
    assert len(DEFAULT_TTS_SCRIPT_BANK) == 37
    ids = [row[0] for row in DEFAULT_TTS_SCRIPT_BANK]
    assert len(set(ids)) == len(ids)
    assert (ids[0], ids[-1]) == ("free_001", "prosody_003")


def test_manuscript_segmentation_uses_legacy_limit_and_punctuation():
    source = "第一句有逗號，第二句有頓號、第三句有分號；第四句最後完結。" * 3
    parts = _segments(source)
    assert "".join(parts) == source
    assert all(0 < len(part) <= 35 for part in parts)
    assert len(parts) > 1


def test_admin_has_five_sections_and_single_renderer():
    html = (ROOT / "frontend/ai_training/index.html").read_text(encoding="utf-8")
    assert html.count("data-admin=") == 5
    assert "讀音字典管理" in html
    assert "server-tables.js" not in html and "ai-parity.js" not in html
    assert html.count("/ai-training/app.js") == 1


def test_r2_only_duplicate_guard_withdraw_and_export_parity():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    assert "此資料已提交，請勿重複提交" in api
    assert '@router.delete("/llm/{submission_id}")' in api
    assert "manualAudioSubmit" in js and "manualLlmSubmit" in js
    assert "alert(" not in js and "prompt(" not in js and "confirm(" not in js
    assert 'def export_recording_manifest(request: Request, speaker: str = "")' in api
    assert '"download_url"' in api
    assert "audio_data" not in api


def test_recording_gate_and_public_training_guidance_match_legacy():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    html = (ROOT / "frontend/ai_training/index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    for marker in ("_verified_r2_audio_claim(body, user)", "matches_prompt", "duration_seconds", "tts_review", "llm_review"):
        assert marker in api
    for marker in ('id="lexicon-view"', 'id="rdPlan"', 'id="resetSkipped"', 'id="clearLlm"'):
        assert marker in html
    for marker in ("recordedSeconds", "resetRecording", "重新錄製跳過的句子", "SafeMarkdown.render"):
        assert marker in html + js


def test_admin_ai_planning_is_selective_and_protects_recorded_scripts():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    assert '@router.post("/coverage/ai")' in api
    assert "build_tts_coverage_prompt" in api
    assert "build_tts_regenerate_prompt" in api
    assert "deactivate_candidates" in api
    assert "status IN ('pending','accepted')" in api
    assert "deactivate_ids" in api and "data-suggestion" in js


def test_recording_review_uses_shared_page_size():
    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    assert "ADMIN_RECORDING_PAGE_SIZE = AI_TRAINING_ADMIN_PAGE_SIZE" in api
    assert '"page_size": ADMIN_RECORDING_PAGE_SIZE' in api


def test_rag_vector_schema_is_read_only_and_fails_closed():
    class Db:
        def __init__(self, extension=True, column=True, tables=True):
            self.extension = extension
            self.column = column
            self.tables = tables
            self.queries = []

        def query(self, sql, params=None):
            self.queries.append((sql, params))
            if "obj_description" in sql:
                return pd.DataFrame([{"applied": True}])
            if "to_regclass" in sql:
                return pd.DataFrame([{
                    "relation_0": self.tables,
                    "relation_1": self.tables,
                    "table_0": self.tables,
                    "table_1": self.tables,
                }])
            return pd.DataFrame([{
                "vector_extension_ready": self.extension,
                "embedding_column_ready": self.column,
            }])

    with patch.dict(
        "core.schema_features.FEATURE_MIGRATION_VERSIONS",
        {"rag": "20260714_0001"}, clear=False,
    ):
        ready = Db()
        _require_rag_vector_schema(ready)
        assert len(ready.queries) == 3
        assert ready.queries[2][1] == {"table_name": "rag_chunks"}

        with pytest.raises(HTTPException) as error:
            _require_rag_vector_schema(Db(column=False))
        assert error.value.status_code == 503
        with pytest.raises(HTTPException):
            _require_rag_vector_schema(Db(tables=False))

    api = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" not in api
    assert "ADD COLUMN IF NOT EXISTS embedding" not in api
    assert "_ensure_training_schema" not in api
    assert "CREATE_AI_DATASET" not in api


def test_optional_feature_bundles_require_marker_and_complete_real_tables():
    class Db:
        def __init__(self, flags, *, marker=True, relations=None):
            self.flags = flags
            self.marker = marker
            self.relations = relations if relations is not None else flags

        def query(self, sql, _params=None):
            if "obj_description" in sql:
                return pd.DataFrame([{"applied": self.marker}])
            return pd.DataFrame([{
                **{f"relation_{index}": flag for index, flag in enumerate(self.relations)},
                **{f"table_{index}": flag for index, flag in enumerate(self.flags)},
            }])

    # Tables alone cannot enable a future feature while no migration version is released.
    assert _feature_schema_state(Db([False, False]), "eval") is False
    assert _feature_schema_state(Db([True, True]), "eval") is False
    with patch.dict(
        "core.schema_features.FEATURE_MIGRATION_VERSIONS",
        {"eval": "20260714_0002"}, clear=False,
    ):
        assert _feature_schema_state(Db([True, True], marker=False), "eval") is False
        assert _feature_schema_state(Db([True, True]), "eval") is True
        with pytest.raises(HTTPException) as error:
            _feature_schema_state(Db([True, False]), "eval")
        assert error.value.status_code == 503
        with pytest.raises(HTTPException):
            _feature_schema_state(Db([False, False], relations=[True, True]), "eval")


def test_consent_audit_is_written_only_for_a_real_state_transition():
    class Result:
        def __init__(self, row): self.row = row
        def fetchone(self): return self.row
        def mappings(self): return self
        def first(self): return self.row

    class Transaction:
        def __init__(self, changed):
            self.changed = changed
            self.executed = []
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=None):
            statement = str(sql)
            self.executed.append((statement, params or {}))
            if "SELECT consent_text" in statement:
                return Result({"consent_text": CONSENT_TEXT})
            if "INSERT INTO tts_voice_consents" in statement:
                return Result((1,) if self.changed else None)
            return Result(None)

    class Db:
        def __init__(self, changed): self.tx = Transaction(changed)
        def transaction(self): return self.tx

    body = ConsentBody(
        agreed=True,
        voice_cloning_confirmed=True,
        cloud_processing_confirmed=True,
        is_minor=False,
        guardian_confirmed=False,
    )
    for changed in (True, False):
        db = Db(changed)
        with patch("api.ai_training_api._ctx", return_value=("member", db)), \
             patch("api.ai_training_api._prune_audit") as prune:
            result = consent(body, object())
        assert result == {"ok": True, "changed": changed}
        audit_writes = [sql for sql, _ in db.tx.executed if "INSERT INTO ai_training_audit" in sql]
        assert bool(audit_writes) is changed
        assert prune.called is changed


def test_consent_text_change_requires_a_new_version_and_never_overwrites():
    class Result:
        def mappings(self): return self
        def first(self): return {"consent_text": "older wording"}

    class Transaction:
        def __init__(self): self.executed = []
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=None):
            self.executed.append((str(sql), params or {}))
            return Result()

    class Db:
        def __init__(self): self.tx = Transaction()
        def transaction(self): return self.tx

    db = Db()
    with patch("api.ai_training_api._ctx", return_value=("member", db)), \
         pytest.raises(HTTPException) as error:
        consent(ConsentBody(
            agreed=True,
            voice_cloning_confirmed=True,
            cloud_processing_confirmed=True,
            is_minor=False,
            guardian_confirmed=False,
        ), object())
    assert error.value.status_code == 500
    sql = "\n".join(statement for statement, _ in db.tx.executed)
    assert "INSERT INTO tts_voice_consents" not in sql
    assert "INSERT INTO ai_training_audit" not in sql


def test_repeated_consent_withdrawal_is_idempotent_and_does_not_grow_audit():
    class Result:
        rowcount = 0
        def fetchone(self): return None

    class Transaction:
        def __init__(self): self.executed = []
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=None):
            self.executed.append((str(sql), params or {}))
            return Result()

    class Db:
        def __init__(self): self.tx = Transaction()
        def transaction(self): return self.tx
        def query(self, _sql, _params=None):
            return pd.DataFrame([{"relation_0": False, "table_0": False}])

    db = Db()
    with patch("api.ai_training_api._ctx", return_value=("member", db)), \
         patch("api.ai_training_api._prune_audit") as prune:
        assert withdraw(object()) == {"ok": True, "changed": False}
    sql = "\n".join(statement for statement, _ in db.tx.executed)
    assert "INSERT INTO ai_training_audit" not in sql
    prune.assert_not_called()


def test_consent_metadata_is_explicit_and_minor_requires_guardian():
    with pytest.raises(Exception):
        ConsentBody(agreed=True)
    html = (ROOT / "frontend/ai_training/index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
    for marker in (
        'id="voiceCloningConfirmed"',
        'id="cloudProcessingConfirmed"',
        'id="minorStatus"',
        'id="guardianConfirmed"',
    ):
        assert marker in html
    for marker in (
        "voice_cloning_confirmed: true",
        "cloud_processing_confirmed: true",
        "is_minor: isMinor",
        "guardian_confirmed:",
    ):
        assert marker in js
    minor = ConsentBody(
        agreed=True,
        voice_cloning_confirmed=True,
        cloud_processing_confirmed=True,
        is_minor=True,
        guardian_confirmed=False,
    )
    with patch("api.ai_training_api._ctx", return_value=("minor", object())), \
         pytest.raises(HTTPException) as error:
        consent(minor, object())
    assert error.value.status_code == 400


def test_consent_grant_withdraw_and_recording_finalize_share_one_lock():
    grant_source = inspect.getsource(consent)
    withdraw_source = inspect.getsource(withdraw)
    recording_source = inspect.getsource(recording)
    for source in (grant_source, withdraw_source, recording_source):
        assert "_consent_lock_key(user)" in source
    assert withdraw_source.index("_consent_lock_key(user)") < withdraw_source.index(
        "UPDATE {TABLE_TTS_VOICE_CONSENTS}"
    )
    assert recording_source.index("_consent_lock_key(user)") < recording_source.index(
        "INSERT INTO {TABLE_TTS_VOICE_RECORDINGS}"
    )
    assert "consent_text=:consent_text" in recording_source
    assert "FOR SHARE" in recording_source


def test_audit_retention_runs_on_first_call_and_preserves_privacy_evidence():
    class Db:
        def __init__(self): self.executed = []
        def execute(self, sql, params=None): self.executed.append((sql, params or {}))

    db = Db()
    with patch("api.ai_training_api._AI_TRAINING_AUDIT_LAST_PRUNE", None), \
         patch("api.ai_training_api.time.monotonic", return_value=1):
        _prune_audit(db)
    assert len(db.executed) == 1
    sql = db.executed[0][0]
    for action in ("consent_granted", "consent_withdrawn", "submission_withdrawn"):
        assert action in sql


def test_llm_withdrawal_and_audit_share_one_transaction_when_features_are_off():
    class Transaction:
        def __init__(self):
            self.executed = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.executed.append((str(sql), params or {}))
            if "SELECT status" in str(sql):
                return self
            return None

        def mappings(self):
            return self

        def first(self):
            return {"status": "accepted"}

    class Db:
        def __init__(self):
            self.tx = Transaction()

        def query(self, sql, _params=None):
            count = 3 if "table_2" in sql else 2
            return pd.DataFrame([{
                **{f"relation_{index}": False for index in range(count)},
                **{f"table_{index}": False for index in range(count)},
            }])

        def transaction(self):
            return self.tx

    db = Db()
    with patch("api.ai_training_api._ctx", return_value=("member", db)), \
         patch("api.ai_training_api._prune_audit"):
        assert withdraw_llm(7, object()) == {"ok": True, "changed": True}
    sql = "\n".join(statement for statement, _ in db.tx.executed)
    assert "UPDATE llm_training_submissions" in sql
    assert "INSERT INTO ai_training_audit" in sql
    assert "rag_documents" not in sql
    assert "ai_dataset_snapshots" not in sql


def test_partial_future_schema_never_blocks_base_privacy_withdrawal():
    class Transaction:
        def __init__(self):
            self.executed = []

        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=None):
            self.executed.append((str(sql), params or {}))
            return self
        def mappings(self): return self
        def first(self): return {"status": "accepted"}

    class Db:
        def __init__(self):
            self.tx = Transaction()
            self.transaction_calls = 0

        def query(self, _sql, _params=None):
            return pd.DataFrame([{"relation_0": True, "table_0": False}])

        def transaction(self):
            self.transaction_calls += 1
            return self.tx

    db = Db()
    with patch("api.ai_training_api._ctx", return_value=("member", db)), \
         patch("api.ai_training_api._prune_audit"):
        result = withdraw_llm(7, object())
    assert result == {"ok": True, "changed": True}
    assert db.transaction_calls == 1
    sql = "\n".join(statement for statement, _ in db.tx.executed)
    assert "UPDATE llm_training_submissions" in sql
    assert "INSERT INTO ai_training_audit" in sql


def test_active_voice_consent_requires_all_explicit_confirmations():
    class Db:
        def __init__(self, active):
            self.active = active
            self.sql = ""

        def query(self, sql, params=None):
            self.sql = sql
            return pd.DataFrame([{"ok": 1}]) if self.active else pd.DataFrame()

    ready = Db(True)
    assert _has_active_voice_consent(ready, "member") is True
    for marker in (
        "voice_cloning_confirmed=TRUE",
        "cloud_processing_confirmed=TRUE",
        "is_minor=FALSE OR guardian_confirmed=TRUE",
        "withdrawn_at IS NULL",
    ):
        assert marker in ready.sql
    assert _has_active_voice_consent(Db(False), "member") is False


def test_readiness_joins_only_current_consent_version_once():
    source = inspect.getsource(readiness)
    assert "ON c.user_id=r.speaker_user_id AND c.consent_version=:version" in source
    assert "AND c.consent_text=:consent_text" in source
