import contextlib
import io
import unittest
from unittest.mock import patch

from tools import finalize_r2_media as finalizer


class FinalizeR2ObjectTests(unittest.TestCase):
    def setUp(self):
        self.remote = {
            "ContentLength": 123,
            "ContentType": "image/webp",
            "CacheControl": finalizer.EXPECTED_CACHE_CONTROL,
            "Metadata": {"sha256": "a" * 64},
        }

    def test_object_head_matches_database_metadata(self):
        with patch.object(finalizer.r2_storage, "head", return_value=self.remote):
            size = finalizer._verify_object(
                "photos/original/member/photo.webp",
                label="photo 1 original",
                expected_prefix="photos/original/",
                expected_mime="image/webp; charset=binary",
                expected_size=123,
                expected_sha="a" * 64,
            )
        self.assertEqual(size, 123)

    def test_database_size_and_hash_are_required_before_head(self):
        head = unittest.mock.Mock()
        with patch.object(finalizer.r2_storage, "head", head):
            with self.assertRaisesRegex(finalizer.VerificationError, "byte size"):
                finalizer._verify_object(
                    "audio/tts/member/1.webm",
                    label="audio 1",
                    expected_prefix="audio/tts/",
                    expected_mime="audio/webm",
                    expected_size=0,
                    expected_sha="a" * 64,
                )
            with self.assertRaisesRegex(finalizer.VerificationError, "database SHA"):
                finalizer._verify_object(
                    "audio/tts/member/1.webm",
                    label="audio 1",
                    expected_prefix="audio/tts/",
                    expected_mime="audio/webm",
                    expected_size=123,
                    expected_sha="",
                )
        head.assert_not_called()

    def test_head_rejects_each_metadata_mismatch(self):
        cases = {
            "content length": {"ContentLength": 124},
            "SHA-256 differs": {"Metadata": {"sha256": "b" * 64}},
            "content type": {"ContentType": "image/jpeg"},
            "cache-control": {"CacheControl": "public, max-age=60"},
        }
        for expected_message, replacement in cases.items():
            with self.subTest(expected_message):
                remote = {**self.remote, **replacement}
                with patch.object(finalizer.r2_storage, "head", return_value=remote):
                    with self.assertRaisesRegex(
                        finalizer.VerificationError, expected_message
                    ):
                        finalizer._verify_object(
                            "photos/original/member/photo.webp",
                            label="photo 1 original",
                            expected_prefix="photos/original/",
                            expected_mime="image/webp",
                            expected_size=123,
                            expected_sha="a" * 64,
                        )

    def test_thumbnail_still_requires_valid_remote_sha(self):
        remote = {**self.remote, "Metadata": {}}
        with patch.object(finalizer.r2_storage, "head", return_value=remote):
            with self.assertRaisesRegex(finalizer.VerificationError, "R2 SHA"):
                finalizer._verify_object(
                    "photos/thumb/member/photo.webp",
                    label="photo 1 thumbnail",
                    expected_prefix="photos/thumb/",
                    expected_mime="image/webp",
                )


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _Connection:
    def __init__(self, columns=None):
        self.columns = columns or set()
        self.statements = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "SELECT EXISTS" in sql:
            return _Scalar((params["table"], params["column"]) in self.columns)
        return _Scalar(None)


class _Context:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _Context(self.connection)


class _MappedRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _PagedConnection:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, _statement, params):
        self.engine.after_ids.append(params["after_id"])
        page = self.engine.pages.get(params["after_id"], [])
        return _MappedRows(page[: params["limit"]])


class _PagedContext:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.engine.open_connections += 1
        return _PagedConnection(self.engine)

    def __exit__(self, *_args):
        self.engine.open_connections -= 1
        return False


class _PagedEngine:
    def __init__(self):
        self.pages = {
            0: [{"id": 1}, {"id": 2}],
            2: [],
        }
        self.after_ids = []
        self.open_connections = 0

    def connect(self):
        return _PagedContext(self)


class FinalizeR2SafetyTests(unittest.TestCase):
    def test_invalid_apply_confirmation_attempts_no_external_access(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(
            finalizer.r2_storage, "configured"
        ) as configured, patch.object(finalizer, "_get_db_engine") as get_engine:
            status = finalizer.main(["--apply", "--confirm", "wrong"])
        self.assertEqual(status, 2)
        configured.assert_not_called()
        get_engine.assert_not_called()
        self.assertIn("no R2 or database access", stderr.getvalue())

    def test_drops_legacy_columns_atomically_with_short_lock_timeout(self):
        columns = {
            ("match_photos", "image_data"),
            ("tts_voice_recordings", "audio_data"),
        }
        connection = _Connection(columns)
        dropped = finalizer.drop_legacy_columns(_Engine(connection))
        sql = "\n".join(connection.statements)
        self.assertEqual(
            dropped,
            ["match_photos.image_data", "tts_voice_recordings.audio_data"],
        )
        self.assertIn("SET LOCAL lock_timeout = '5s'", sql)
        self.assertIn("SET LOCAL statement_timeout = '60s'", sql)
        self.assertIn("ALTER TABLE match_photos DROP COLUMN image_data", sql)
        self.assertIn(
            "ALTER TABLE tts_voice_recordings DROP COLUMN audio_data", sql
        )

    def test_dry_run_report_contains_aggregates_not_object_keys(self):
        report = finalizer.build_report(
            {
                "photos": {"rows": 2, "objects": 4, "bytes": 400},
                "audio": {"rows": 3, "objects": 3, "bytes": 600},
            },
            {
                "match_photos.image_data": True,
                "tts_voice_recordings.audio_data": True,
            },
        )
        encoded = finalizer.json.dumps(report, sort_keys=True)
        self.assertEqual(report["totals"], {"rows": 5, "objects": 7, "bytes": 1000})
        self.assertTrue(report["ready_to_drop"])
        self.assertNotIn("r2_key", encoded)
        self.assertNotIn("photos/original", encoded)

    def test_keyset_batch_releases_database_before_r2_verification(self):
        engine = _PagedEngine()

        def verify(_row):
            self.assertEqual(engine.open_connections, 0)
            return 1, 10

        result = finalizer._verify_rows(engine, "SELECT", verify, batch_size=10)
        self.assertEqual(result, {"rows": 2, "objects": 2, "bytes": 20})
        self.assertEqual(engine.after_ids, [0, 2])


if __name__ == "__main__":
    unittest.main()
