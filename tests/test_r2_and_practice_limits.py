import unittest
from unittest.mock import patch

from core import r2_storage
from deploy import proxy
from schema import (
    CREATE_MATCH_PHOTOS,
    CREATE_PRACTICE_DAILY_USAGE,
    CREATE_TTS_VOICE_RECORDINGS,
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
        self.assertEqual(proxy.SOLO_MOCK_MONTHLY_LIMIT, 4)
        self.assertEqual(proxy.MULTIPLAYER_FREE_MONTHLY_ROOMS, 10)
        self.assertEqual(proxy.MULTIPLAYER_MOCK_MONTHLY_ROOMS, 3)
        self.assertIn("每星期", proxy.SOLO_LIMIT_MESSAGE)
        self.assertEqual(proxy.GEMINI_RELAY_MAX_BYTES, 96 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
