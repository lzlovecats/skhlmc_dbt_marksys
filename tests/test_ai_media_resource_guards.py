import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd

from core import ai_provider, media_logic, push, r2_storage, rag
from deploy import proxy
from api import ai_coach_api, ai_training_api
from tools import prepare_gpt_sovits_dataset as dataset_tool


ROOT = Path(__file__).resolve().parents[1]


class _AsyncStreamResponse:
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class _AsyncStreamClient:
    def __init__(self, chunks):
        self.response = _AsyncStreamResponse(chunks)
        self.request = None

    def stream(self, method, url, **kwargs):
        self.request = (method, url, kwargs)
        return self.response


class ProviderResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_json_is_streamed_under_one_byte_ceiling(self):
        client = _AsyncStreamClient([b'{"ok":', b"true}"])
        result = await ai_provider.post_json_bounded(client, "https://provider.test", max_bytes=11)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.request[0], "POST")

        oversized = _AsyncStreamClient([b'{"long":"', b"0123456789", b'"}'])
        with self.assertRaisesRegex(ValueError, "exceeds server limit"):
            await ai_provider.post_json_bounded(
                oversized, "https://provider.test", max_bytes=12
            )

    async def test_tts_rejects_text_before_provider_or_lexicon_work(self):
        with patch("deploy.proxy.TTS_TEXT_MAX_CHARS", 3), patch(
            "deploy.proxy._preprocess_tts_text"
        ) as preprocess:
            with self.assertRaises(proxy.TtsUnavailable):
                await proxy._synthesize_tts("four")
        preprocess.assert_not_called()


class R2IntentCleanupTests(unittest.TestCase):
    def test_failed_object_delete_keeps_intent_open_for_orphan_retry(self):
        with patch("core.r2_storage.delete", side_effect=[None, RuntimeError("R2 timeout")]), \
             patch("core.r2_storage.mark_upload_intent_deleted") as close:
            self.assertFalse(
                r2_storage.delete_intent_objects(object(), "intent-1", ("pending/a", "audio/a"))
            )
        close.assert_not_called()

    def test_all_objects_must_delete_before_intent_is_closed(self):
        db = object()
        with patch("core.r2_storage.delete") as delete, patch(
            "core.r2_storage.mark_upload_intent_deleted"
        ) as close:
            self.assertTrue(
                r2_storage.delete_intent_objects(db, "intent-1", ("pending/a", "audio/a"))
            )
        self.assertEqual(delete.call_count, 2)
        close.assert_called_once_with(db, "intent-1")

    def test_upload_intents_record_pending_and_final_cleanup_keys(self):
        tts = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
        photos = (ROOT / "api/match_photos_api.py").read_text(encoding="utf-8")
        self.assertIn("object_keys=[pending_key, key]", tts)
        self.assertIn(
            "pending_original_key, pending_thumbnail_key, original_key, thumbnail_key",
            photos,
        )


class PromptAndLexiconTests(unittest.TestCase):
    def test_system_and_user_share_one_prompt_budget(self):
        with patch("core.ai_provider.AI_PROVIDER_PROMPT_MAX_CHARS", 10):
            system, user = ai_provider._bounded_prompt_pair("123456", "abcdefgh")
        self.assertEqual(system, "123456")
        self.assertEqual(user, "abcd")
        self.assertEqual(len(system) + len(user), 10)

    def test_tts_lexicon_regex_is_compiled_once_per_snapshot(self):
        proxy._compiled_lexicon.cache_clear()
        rows = (("人工智能", "AI"), ("人工", "人-工"))
        with patch("deploy.proxy._load_lexicon_overrides", return_value=list(rows)):
            self.assertEqual(proxy._preprocess_tts_text("人工智能與人工"), "AI與人-工")
            first = proxy._compiled_lexicon.cache_info()
            self.assertEqual(proxy._preprocess_tts_text("人工智能"), "AI")
            second = proxy._compiled_lexicon.cache_info()
        self.assertEqual(first.misses, 1)
        self.assertEqual(second.hits, 1)
        proxy._compiled_lexicon.cache_clear()

    def test_training_page_reads_only_ai_sections_from_unified_roadmap(self):
        ai_training_api._load_ai_roadmap.cache_clear()
        roadmap = ai_training_api._load_ai_roadmap()
        self.assertTrue(roadmap.startswith("## P3."))
        self.assertIn("## P4.", roadmap)
        self.assertIn("## P5.", roadmap)
        self.assertNotIn("## P2.", roadmap)
        self.assertNotIn("## P6.", roadmap)
        source = (ROOT / "api/ai_training_api.py").read_text(encoding="utf-8")
        self.assertNotIn("assets/tts_rd_plan.md", source)


class R2DownloadTests(unittest.TestCase):
    class Body:
        def __init__(self, data):
            self.data = data
            self.read_size = None
            self.closed = False

        def read(self, size=-1):
            self.read_size = size
            return self.data if size < 0 else self.data[:size]

        def close(self):
            self.closed = True

    def test_bounded_download_reads_only_limit_plus_one_and_closes(self):
        body = self.Body(b"123456")
        client = type(
            "Client",
            (),
            {"get_object": lambda _self, **_kwargs: {"Body": body}},
        )()
        with patch("core.r2_storage.client", return_value=client):
            with self.assertRaisesRegex(ValueError, "exceeds download limit"):
                r2_storage.download_bytes("audio/test.webm", max_bytes=5)
        self.assertEqual(body.read_size, 6)
        self.assertTrue(body.closed)

    def test_declared_oversize_is_rejected_without_reading_and_body_is_closed(self):
        body = self.Body(b"ignored")
        client = type(
            "Client",
            (),
            {
                "get_object": lambda _self, **_kwargs: {
                    "Body": body,
                    "ContentLength": 20,
                }
            },
        )()
        with patch("core.r2_storage.client", return_value=client):
            with self.assertRaisesRegex(ValueError, "exceeds download limit"):
                r2_storage.download_bytes("audio/test.webm", max_bytes=5)
        self.assertIsNone(body.read_size)
        self.assertTrue(body.closed)


class TypedConfigIntegrationTests(unittest.TestCase):
    def test_r2_snapshot_and_training_roles_use_typed_fake_db_values(self):
        class Db:
            def __init__(self):
                self.queries = []
                self.executions = []

            def query(self, sql, params=None):
                self.queries.append((sql, params or {}))
                if "SUM(declared_bytes)" in sql:
                    return pd.DataFrame([{"total": 25}])
                if "FROM app_config" in sql:
                    key = (params or {}).get("key")
                    values = {
                        "r2_storage_usage_snapshot": {
                            "bytes": 100,
                            "intent_bytes_snapshot": 20,
                            "as_of": "2026-07-13T00:00:00+00:00",
                        },
                        "tts_recording_allowed_users": ["alice", "bob"],
                    }
                    return pd.DataFrame(
                        [] if key not in values else [{"value": values[key]}]
                    )
                raise AssertionError(sql)

            def execute(self, sql, params=None):
                self.executions.append((sql, params or {}))

        db = Db()
        status = r2_storage.storage_budget_status(db, refresh=False)
        self.assertEqual(status["total_bytes"], 105)
        self.assertEqual(ai_training_api._users(db, "tts_recording_allowed_users"), [
            "alice",
            "bob",
        ])
        self.assertTrue(all("system_config" not in sql for sql, _ in db.queries))

    def test_bandwidth_warning_claim_writes_typed_config_on_fake_connection(self):
        class Result:
            def fetchall(self):
                return []

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params=None):
                self.calls.append((str(statement), params or {}))
                return Result()

        class Begin:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self.connection

            def __exit__(self, *_args):
                return False

        connection = Connection()
        engine = type("Engine", (), {"begin": lambda _self: Begin(connection)})()
        status = {"period": "2026-07", "total_bytes": proxy.BANDWIDTH_WARN_BYTES}
        with patch("deploy.proxy._get_db_engine", return_value=engine), patch(
            "deploy.proxy.get_vote_db", return_value=object()
        ), patch("deploy.proxy._get_vapid", return_value={}), patch(
            "core.push.notify_committee"
        ):
            proxy._send_bandwidth_warning_once(status)
        writes = [params for sql, params in connection.calls if "INSERT INTO app_config" in sql]
        self.assertEqual(len(writes), 2)
        self.assertEqual({params["namespace"] for params in writes}, {"resource"})
        self.assertTrue(
            any(params["key"] == "bandwidth_3gb_push_sent:2026-07" for params in writes)
        )

    def test_scoped_modules_have_no_direct_legacy_config_queries(self):
        for relative in (
            "core/r2_storage.py",
            "api/ai_training_api.py",
            "api/ai_coach_api.py",
            "deploy/proxy.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertNotIn("FROM system_config", source)
                self.assertNotIn("INTO system_config", source)


class PushResourceTests(unittest.TestCase):
    def test_push_delivery_uses_bounded_parallel_network_workers(self):
        subscriptions = [
            {
                "endpoint": f"https://push.example/{index}",
                "user_id": f"user-{index}",
                "subscription_json": json.dumps(
                    {
                        "endpoint": f"https://push.example/{index}",
                        "keys": {"p256dh": "key", "auth": "auth"},
                    }
                ),
            }
            for index in range(4)
        ]

        class Db:
            def __init__(self):
                self.executions = []

            def query(self, _sql, _params=None):
                return pd.DataFrame(subscriptions)

            def execute(self, sql, params=None):
                self.executions.append((sql, params or {}))

        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = 0
        peak = 0

        def send(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            return True, ""

        db = Db()
        with patch("core.push.PUSH_SEND_CONCURRENCY", 4):
            sent = push.notify_committee(
                db,
                {"private_key": "key", "subject": "mailto:test@example.com"},
                "Title",
                "Body",
                send_fn=send,
            )
        self.assertEqual(sent, 4)
        self.assertEqual(peak, 4)
        self.assertEqual(len(db.executions), 4)


class RagSchemaGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_schema_skips_paid_embedding(self):
        class Db:
            def __init__(self):
                self.queries = []

            def query(self, sql, params=None):
                self.queries.append((sql, params or {}))
                return pd.DataFrame([{"table_0": False, "table_1": False}])

        db = Db()
        embed = AsyncMock(return_value=[1.0, 0.0])
        with patch.dict(rag._RAG_SCHEMA_CACHE, {"ready": False, "checked_at": 0}, clear=True), \
             patch("core.rag.time.monotonic", return_value=1_000), \
             patch("core.rag._embed", new=embed):
            context = await rag.retrieve_rag_context(
                db, "key", "query", top_k=1, min_similarity=0.1
            )
        self.assertEqual(context, "")
        embed.assert_not_awaited()
        # No migration has been released for RAG, so the explicit code marker
        # disables it without spending even one catalog round-trip.
        self.assertEqual(len(db.queries), 0)

    async def test_vector_failure_does_not_download_json_fallback(self):
        class Db:
            def __init__(self):
                self.queries = []

            def query(self, sql, params=None):
                self.queries.append((sql, params or {}))
                if "obj_description" in sql:
                    return pd.DataFrame([{"applied": True}])
                if "to_regclass" in sql:
                    return pd.DataFrame([{
                        "relation_0": True, "relation_1": True,
                        "table_0": True, "table_1": True,
                    }])
                if "pg_extension" in sql:
                    return pd.DataFrame([{
                        "vector_extension_ready": True,
                        "embedding_column_ready": True,
                    }])
                raise RuntimeError("vector query unavailable")

        db = Db()
        with patch.dict(rag._RAG_SCHEMA_CACHE, {"ready": False, "checked_at": 0}, clear=True), \
             patch.dict("core.schema_features.FEATURE_MIGRATION_VERSIONS", {"rag": "20260714_0001"}, clear=False), \
             patch("core.rag.time.monotonic", return_value=1_000), \
             patch("core.rag._embed", new=AsyncMock(return_value=[1.0, 0.0])):
            context = await rag.retrieve_rag_context(db, "key", "query")

        self.assertEqual(context, "")
        self.assertFalse(any("embedding_json" in sql for sql, _ in db.queries))

    async def test_stale_positive_readiness_is_rechecked_before_embedding(self):
        class Db:
            def __init__(self): self.queries = []
            def query(self, sql, params=None):
                self.queries.append((sql, params or {}))
                return pd.DataFrame([{"applied": False}])

        db = Db()
        embed = AsyncMock(return_value=[1.0, 0.0])
        with patch.dict(rag._RAG_SCHEMA_CACHE, {"ready": True, "checked_at": 999}, clear=True), \
             patch.dict("core.schema_features.FEATURE_MIGRATION_VERSIONS", {"rag": "20260714_0001"}, clear=False), \
             patch("core.rag.time.monotonic", return_value=1_000), \
             patch("core.rag._embed", new=embed):
            context = await rag.retrieve_rag_context(db, "key", "query")
        self.assertEqual(context, "")
        embed.assert_not_awaited()
        self.assertEqual(len(db.queries), 1)


class CustomModelGateTests(unittest.TestCase):
    def test_custom_llm_cannot_use_unversioned_legacy_registry(self):
        class Db:
            def query(self, *_args, **_kwargs):
                raise AssertionError("disabled feature must not query legacy tables")

        values = {
            "CUSTOM_LLM_BASE_URL": "https://llm.internal",
            "CUSTOM_LLM_MODEL": "llm-v1",
            "CUSTOM_LLM_API_KEY": "secret",
        }
        with patch("deploy.proxy._get_proxy_secret", side_effect=lambda key, *_: values.get(key, "")):
            with self.assertRaisesRegex(Exception, "registry尚未由正式migration啟用"):
                ai_coach_api._config("自家辯論 LLM", Db())

    def test_custom_tts_cannot_use_unversioned_legacy_registry(self):
        with patch("deploy.proxy.get_vote_db", return_value=object()), patch(
            "deploy.proxy._get_db_engine"
        ) as engine:
            self.assertFalse(proxy._model_is_deployable("tts-v1", "tts"))
        engine.assert_not_called()


class MediaTransactionTests(unittest.TestCase):
    def test_global_comment_quota_has_matching_user_time_index(self):
        from schema import CREATE_INDICES

        self.assertIn("idx_video_comments_user_created", CREATE_INDICES)

    def test_comment_user_quota_is_global_and_locked(self):
        class Result:
            def mappings(self):
                return self

            def first(self):
                return {"video_count": 0, "user_day_count": 50}

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params=None):
                self.calls.append((str(statement), params))
                return Result()

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self.connection

            def __exit__(self, *_args):
                return False

        class Db:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        db = Db()
        with patch("core.media_logic.VIDEO_COMMENT_MAX_PER_USER_DAY", 50):
            result = media_logic.add_comment(7, "member", "hello", db=db)
        sql = "\n".join(call[0] for call in db.connection.calls)
        self.assertFalse(result["ok"])
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("WHERE user_id=:user_id AND created_at>=:user_cutoff", sql)
        self.assertNotIn("INSERT INTO video_comments", sql)

    def test_chapter_replacement_is_one_transaction_and_one_batch_insert(self):
        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params=None):
                self.calls.append((str(statement), params))

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self.connection

            def __exit__(self, *_args):
                return False

        connection = Connection()
        db = type("Db", (), {"transaction": lambda _self: Transaction(connection)})()
        chapters = [
            {"chapter_label": "正主", "enabled": True, "time_text": "1:00"},
            {"chapter_label": "反主", "enabled": True, "time_text": "2:00"},
        ]
        result = media_logic.save_chapters(8, chapters, db=db)
        self.assertTrue(result["ok"])
        self.assertEqual(len(connection.calls), 2)
        self.assertIn("DELETE FROM video_chapters", connection.calls[0][0])
        self.assertEqual(len(connection.calls[1][1]), 2)


class OfflineDatasetSafetyTests(unittest.TestCase):
    class DownloadResponse:
        def __init__(self, data, final_url="https://r2.example/audio.webm"):
            self.data = data
            self.offset = 0
            self.final_url = final_url
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.final_url

        def read(self, size=-1):
            if self.offset >= len(self.data):
                return b""
            end = len(self.data) if size < 0 else self.offset + size
            block = self.data[self.offset:end]
            self.offset += len(block)
            return block

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "dataset.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "unsafe")
            with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
                dataset_tool._extract_zip(archive, root / "output", overwrite=False)
            self.assertFalse((root / "escape.txt").exists())

    def test_metadata_row_count_has_a_hard_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "metadata.csv"
            metadata.write_text(
                "speaker_user_id,prompt_text,audio_file\n"
                "a,one,a.wav\n"
                "a,two,b.wav\n",
                encoding="utf-8",
            )
            with patch.object(dataset_tool, "DATASET_ARCHIVE_MAX_ITEMS", 1):
                with self.assertRaisesRegex(ValueError, "more than 1 rows"):
                    dataset_tool._read_rows(metadata)

    def test_current_recordings_manifest_download_is_verified_and_token_not_persisted(self):
        audio = b"small-webm-fixture"
        digest = hashlib.sha256(audio).hexdigest()
        signed_url = "https://r2.example/audio.webm?X-Amz-Signature=secret-token"
        item = {
            "id": 12,
            "speaker_user_id": "speaker-a",
            "script_id": "script-1",
            "prompt_text": "測試句子",
            "download_url": signed_url,
            "mime_type": "audio/webm",
            "file_ext": "webm",
            "size_bytes": len(audio),
            "audio_sha256": digest,
            "duration_seconds": 1.2,
            "sample_rate_hz": 48_000,
            "channel_count": 1,
            "detected_format": "matroska,webm",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "recordings.json"
            manifest.write_text(
                json.dumps({"storage": "r2", "items": [item]}), encoding="utf-8"
            )
            with patch(
                "tools.prepare_gpt_sovits_dataset.urllib.request.urlopen",
                return_value=self.DownloadResponse(audio),
            ) as urlopen:
                dataset_tool._materialize_recordings_manifest(
                    manifest,
                    root / "dataset",
                    speaker="speaker-a",
                    overwrite=False,
                )
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, signed_url)
            self.assertEqual((root / "dataset/audio/12.webm").read_bytes(), audio)
            metadata = (root / "dataset/metadata.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("download_url", metadata)
            self.assertNotIn("secret-token", metadata)
            self.assertIn(digest, metadata)

    def test_download_error_does_not_expose_signed_url(self):
        token = "do-not-log-this-token"
        with tempfile.TemporaryDirectory() as tmp, patch(
            "tools.prepare_gpt_sovits_dataset.urllib.request.urlopen",
            side_effect=dataset_tool.urllib.error.URLError(
                f"https://r2.example/audio?token={token}"
            ),
        ):
            with self.assertRaises(RuntimeError) as error:
                dataset_tool._download_https_audio(
                    f"https://r2.example/audio?token={token}",
                    Path(tmp) / "audio.webm",
                    recording_id="12",
                    expected_sha256="a" * 64,
                )
        self.assertNotIn(token, str(error.exception))
        self.assertIsNone(error.exception.__cause__)

    def test_manifest_item_and_total_byte_limits_fail_before_download(self):
        item = {
            "id": 1,
            "speaker_user_id": "speaker-a",
            "prompt_text": "test",
            "download_url": "https://r2.example/audio",
            "audio_sha256": "a" * 64,
            "size_bytes": 11,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "recordings.json"
            manifest.write_text(
                json.dumps({"items": [item, {**item, "id": 2}]}), encoding="utf-8"
            )
            with patch.object(dataset_tool, "DATASET_ARCHIVE_MAX_ITEMS", 1):
                with self.assertRaisesRegex(ValueError, "contains 2 items"):
                    dataset_tool._materialize_recordings_manifest(
                        manifest, root / "items", speaker="speaker-a", overwrite=False
                    )
            manifest.write_text(json.dumps({"items": [item]}), encoding="utf-8")
            with patch.object(dataset_tool, "DATASET_ARCHIVE_MAX_BYTES", 10), patch(
                "tools.prepare_gpt_sovits_dataset.urllib.request.urlopen"
            ) as urlopen:
                with self.assertRaisesRegex(ValueError, "declare 11 bytes"):
                    dataset_tool._materialize_recordings_manifest(
                        manifest, root / "bytes", speaker="speaker-a", overwrite=False
                    )
            urlopen.assert_not_called()

    def test_manifest_audio_metadata_is_checked_before_normalization(self):
        audio = b"verified"
        row = {
            "id": "7",
            "size_bytes": str(len(audio)),
            "audio_sha256": hashlib.sha256(audio).hexdigest(),
            "duration_seconds": "2.0",
            "sample_rate_hz": "48000",
            "channel_count": "1",
            "detected_format": "matroska,webm",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.webm"
            path.write_bytes(audio)
            with patch.object(
                dataset_tool,
                "_probe_audio_file",
                return_value={
                    "duration_seconds": 2.0,
                    "sample_rate_hz": 48_000,
                    "channels": 1,
                    "format": "matroska,webm",
                },
            ):
                dataset_tool._verify_manifest_audio_metadata(path, row)
            with patch.object(
                dataset_tool,
                "_probe_audio_file",
                return_value={
                    "duration_seconds": 2.0,
                    "sample_rate_hz": 44_100,
                    "channels": 1,
                    "format": "matroska,webm",
                },
            ):
                with self.assertRaisesRegex(ValueError, "sample_rate_hz"):
                    dataset_tool._verify_manifest_audio_metadata(path, row)


class FrontendResourceTests(unittest.TestCase):
    def test_media_pages_use_bounded_recording_and_thumbnail_paths(self):
        training = (ROOT / "frontend/ai_training/app.js").read_text(encoding="utf-8")
        coach = (ROOT / "frontend/shared/ai-parity.js").read_text(encoding="utf-8")
        photos = (ROOT / "frontend/shared/server-tables.js").read_text(encoding="utf-8")
        replay = (ROOT / "frontend/video_replay/index.html").read_text(encoding="utf-8")
        for source in (training, coach):
            self.assertIn("start(1000)", source)
            self.assertIn("recordStopTimer = setTimeout", source)
            self.assertIn("URL.revokeObjectURL", source)
        self.assertIn("?thumbnail=1", photos)
        self.assertIn('loading="lazy"', photos)
        self.assertIn("clearInterval(progressTimer)", replay)

    def test_reviewed_html_is_readable_and_not_line_minified(self):
        paths = [
            ROOT / "frontend/ai_coach/index.html",
            ROOT / "frontend/ai_fund/index.html",
            ROOT / "frontend/ai_training/index.html",
            ROOT / "frontend/match_photos/index.html",
            ROOT / "frontend/video_admin/index.html",
            ROOT / "frontend/video_replay/index.html",
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertGreater(len(lines), 100)
                self.assertLessEqual(max(map(len, lines)), 500)


if __name__ == "__main__":
    unittest.main()
