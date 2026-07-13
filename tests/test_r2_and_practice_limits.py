import unittest
import datetime
import inspect
import pathlib
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
from zoneinfo import ZoneInfo

from api import ai_coach_api, ai_training_api
from core import r2_storage
from deploy import proxy
from schema import (
    CREATE_BANDWIDTH_USAGE_LOGS,
    CREATE_MATCH_PHOTOS,
    CREATE_PRACTICE_DAILY_USAGE,
    CREATE_R2_UPLOAD_INTENTS,
    CREATE_TTS_VOICE_RECORDINGS,
    MIGRATIONS,
    MEDIA_R2_STARTUP_MIGRATIONS,
)


class R2UploadClaimTests(unittest.TestCase):
    def test_signed_upload_claim_round_trip_and_tamper_rejection(self):
        token = r2_storage.sign_upload_claim(
            {"kind": "photo", "user": "member", "r2_key": "photos/x.webp"},
            "test-secret",
        )
        claim = r2_storage.verify_upload_claim(token, "test-secret")
        self.assertEqual(claim["r2_key"], "photos/x.webp")
        self.assertIsNone(r2_storage.verify_upload_claim(token + "x", "test-secret"))
        self.assertIsNone(r2_storage.verify_upload_claim(token, "wrong-secret"))

    def test_expired_upload_claim_is_rejected(self):
        with patch("core.r2_storage.time.time", return_value=1000):
            token = r2_storage.sign_upload_claim({"kind": "tts"}, "secret", expires=60)
        with patch("core.r2_storage.time.time", return_value=1061):
            self.assertIsNone(r2_storage.verify_upload_claim(token, "secret"))

    def test_presigned_put_cryptographically_binds_content_length(self):
        config = {
            "account_id": "account", "access_key_id": "key",
            "secret_access_key": "secret", "bucket": "bucket",
            "endpoint": "https://account.r2.cloudflarestorage.com",
        }
        r2_storage.client.cache_clear()
        with patch("core.r2_storage.settings", return_value=config):
            url = r2_storage.presign_put(
                "photos/x.webp", "image/webp", "a" * 64, 12345,
            )
        r2_storage.client.cache_clear()
        signed = parse_qs(urlparse(url).query)["X-Amz-SignedHeaders"][0]
        self.assertIn("content-length", signed.split(";"))
        with self.assertRaises(ValueError):
            r2_storage.presign_put("x", "image/webp", "a" * 64, 0)


class MediaSchemaTests(unittest.TestCase):
    def test_media_schema_is_r2_only(self):
        self.assertNotIn("image_data", CREATE_MATCH_PHOTOS)
        self.assertIn("thumbnail_r2_key", CREATE_MATCH_PHOTOS)
        self.assertNotIn("audio_data", CREATE_TTS_VOICE_RECORDINGS)
        self.assertIn("r2_key", CREATE_TTS_VOICE_RECORDINGS)

    def test_daily_limit_is_split_between_free_and_mock(self):
        self.assertIn("multiplayer_free", CREATE_PRACTICE_DAILY_USAGE)
        self.assertIn("multiplayer_mock", CREATE_PRACTICE_DAILY_USAGE)
        self.assertEqual(proxy._practice_kind("free"), "multiplayer_free")
        self.assertEqual(proxy._practice_kind("mock"), "multiplayer_mock")
        self.assertIn("營運預算", proxy.PRACTICE_DAILY_LIMIT_MESSAGE)
        self.assertEqual(proxy.MAX_ROOMS, 2)
        self.assertEqual(proxy.SOLO_FREE_MONTHLY_LIMIT, 20)
        self.assertEqual(proxy.SOLO_MOCK_MONTHLY_LIMIT, 10)
        self.assertEqual(proxy.MULTIPLAYER_FREE_MONTHLY_ROOMS, 20)
        self.assertEqual(proxy.MULTIPLAYER_MOCK_MONTHLY_ROOMS, 10)
        self.assertIn("每星期", proxy.SOLO_LIMIT_MESSAGE)
        self.assertEqual(proxy.GEMINI_RELAY_MAX_BYTES, 96 * 1024 * 1024)

    def test_hkt_quota_boundaries_are_compared_as_utc(self):
        now = datetime.datetime(2026, 7, 1, 0, 5, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        daily, month = proxy._solo_quota_boundaries(now, False)
        self.assertEqual(daily, datetime.datetime(2026, 6, 30, 16, 0))
        self.assertEqual(month, datetime.datetime(2026, 6, 30, 16, 0))
        weekly, _ = proxy._solo_quota_boundaries(now, True)
        self.assertEqual(weekly, datetime.datetime(2026, 6, 28, 16, 0))

    def test_automatic_migrations_drop_legacy_not_null_only_if_columns_exist(self):
        migrations = "\n".join(MIGRATIONS)
        self.assertIn("column_name='image_data'", migrations)
        self.assertIn("ALTER COLUMN image_data DROP NOT NULL", migrations)
        self.assertIn("column_name='audio_data'", migrations)
        self.assertIn("ALTER COLUMN audio_data DROP NOT NULL", migrations)
        self.assertEqual(MIGRATIONS[:2], MEDIA_R2_STARTUP_MIGRATIONS)
        startup = inspect.getsource(proxy.run_safe_startup_migrations)
        self.assertIn("MEDIA_R2_STARTUP_MIGRATIONS", startup)

    def test_bandwidth_ledger_and_thresholds_are_present(self):
        self.assertIn("bytes_out", CREATE_BANDWIDTH_USAGE_LOGS)
        self.assertEqual(proxy.BANDWIDTH_WARN_BYTES, 3_000_000_000)
        self.assertEqual(proxy.BANDWIDTH_STOP_LIVE_BYTES, 3_500_000_000)
        self.assertEqual(proxy.BANDWIDTH_ESSENTIAL_ONLY_BYTES, 4_000_000_000)
        for path in (
            "/api/ai-coach/run", "/api/ai-training/recordings/quality-check",
            "/api/ai-training/rag/reindex", "/api/vote/ai-review",
        ):
            self.assertIn(path, proxy.ESSENTIAL_ONLY_BLOCKED_PATHS)
        self.assertEqual(proxy.BANDWIDTH_CHECKPOINT_SECONDS, 30)

    def test_bandwidth_baseline_snapshot_does_not_double_count(self):
        class Scalar:
            def scalar(self):
                return 900

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Scalar()

        class Begin:
            def __enter__(self):
                return Connection()
            def __exit__(self, *_args):
                return False

        class Engine:
            def begin(self):
                return Begin()

        values = {
            "BANDWIDTH_MONTH_BASE_BYTES": "2000",
            "BANDWIDTH_BASELINE_AS_OF": "2026-07-13T12:00:00+08:00",
            "BANDWIDTH_BASELINE_TRACKED_BYTES": "700",
        }
        with patch("deploy.proxy._get_db_engine", return_value=Engine()), \
             patch("deploy.proxy._bandwidth_month_context", return_value=("2026-07", datetime.datetime(2026, 6, 30, 16))), \
             patch("deploy.proxy._get_proxy_secret", side_effect=lambda key, default="": values.get(key, default)):
            status = proxy.bandwidth_budget_status()
        self.assertEqual(status["tracked_after_baseline_bytes"], 200)
        self.assertEqual(status["total_bytes"], 2200)
        self.assertTrue(status["baseline_snapshot_ready"])

    def test_r2_intents_are_persisted_and_capped_even_without_completion(self):
        self.assertIn("declared_bytes", CREATE_R2_UPLOAD_INTENTS)
        self.assertIn("status", CREATE_R2_UPLOAD_INTENTS)
        source = inspect.getsource(r2_storage.reserve_upload_intent)
        self.assertIn("pg_advisory_xact_lock", source)
        self.assertIn("user_daily_limit", source)
        self.assertIn("global_monthly_limit", source)
        self.assertIn("storage_global", source)
        self.assertEqual(r2_storage.R2_STORAGE_WARN_BYTES, 7_000_000_000)
        self.assertEqual(r2_storage.R2_STORAGE_STOP_BYTES, 8_000_000_000)
        self.assertIn("pending/", inspect.getsource(r2_storage.promote))

    def test_recording_metadata_and_review_are_server_authoritative(self):
        source = inspect.getsource(ai_training_api.recording)
        self.assertIn("download_bytes", source)
        self.assertIn("_probe_audio", source)
        self.assertIn("verify_upload_claim", source)
        self.assertNotIn("body.ai_review", source)
        self.assertIn("review_secret = _get_relay_cookie_secret()", source)
        self.assertIn("r2_storage.promote", source)

    def test_room_quota_and_tokens_wait_until_precheck_succeeds(self):
        creation = inspect.getsource(proxy.room_create)
        activation = inspect.getsource(proxy._room_start_active)
        precheck = inspect.getsource(proxy._room_handle_precheck_result)
        self.assertNotIn("_reserve_room_practice_slots", creation)
        self.assertNotIn("_mint_gemini_live_token", creation)
        self.assertIn("_room_start_active", precheck)
        self.assertLess(
            activation.index("_reserve_room_practice_slots"),
            activation.index("_room_mint_gemini_tokens"),
        )
        self.assertIn("_release_room_practice_slots", activation)
        self.assertIn("min(float(LIVE_FREE_MAX_MINUTES), max(0.5, free_minutes))", creation)

    def test_relay_claim_binds_member_type_and_server_deadline(self):
        with patch("deploy.proxy._get_relay_cookie_secret", return_value="secret"), \
             patch("deploy.proxy.time.time", return_value=1000):
            signed = proxy._sign_relay_token("token", "member", "solo_free", 600, "practice")
            claim = proxy._verify_relay_signature("token", signed)
        self.assertEqual(claim["user_id"], "member")
        self.assertEqual(claim["practice_kind"], "solo_free")
        self.assertEqual(claim["max_seconds"], 600)
        with patch("deploy.proxy._get_relay_cookie_secret", return_value="secret"):
            self.assertIsNone(proxy._verify_relay_signature("other-token", signed))

    def test_coach_and_tts_have_separate_concurrency_caps(self):
        self.assertEqual(ai_coach_api.AI_COACH_CONCURRENCY, 3)
        self.assertEqual(ai_training_api.TTS_REVIEW_CONCURRENCY, 2)
        prepare = inspect.getsource(ai_coach_api._reserve_prepare_live)
        self.assertIn("hour_utc", prepare)
        self.assertIn("day_utc", prepare)

    def test_every_limited_media_and_practice_page_explains_the_limits(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        coach = (root / "frontend/ai_coach/index.html").read_text(encoding="utf-8")
        training = (root / "frontend/ai_training/index.html").read_text(encoding="utf-8")
        photos = (root / "frontend/match_photos/index.html").read_text(encoding="utf-8")
        self.assertIn("全系統每月quota", coach)
        self.assertIn("伺服器集中限制", coach)
        self.assertIn("伺服器集中設定", training)
        self.assertIn('id="photoLimitSummary"', photos)


class RequestBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_body_is_rejected_by_actual_bytes(self):
        inner_called = False

        async def inner(_scope, _receive, _send):
            nonlocal inner_called
            inner_called = True

        messages = iter([
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ])
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        limiter = proxy.RequestBodyLimitMiddleware(inner, max_bytes=5)
        await limiter({"type": "http", "headers": []}, receive, send)
        self.assertFalse(inner_called)
        self.assertEqual(sent[0]["status"], 413)

    async def test_body_at_limit_is_replayed_to_application(self):
        replayed = []

        async def inner(_scope, receive, _send):
            replayed.append(await receive())
            replayed.append(await receive())

        messages = iter([
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"de", "more_body": False},
        ])

        async def receive():
            return next(messages)

        async def send(_message):
            pass

        limiter = proxy.RequestBodyLimitMiddleware(inner, max_bytes=5)
        await limiter({"type": "http", "headers": []}, receive, send)
        self.assertEqual(b"".join(message["body"] for message in replayed), b"abcde")


if __name__ == "__main__":
    unittest.main()
