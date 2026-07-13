import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import runtime_secrets


class RuntimeSecretsTests(unittest.TestCase):
    def tearDown(self):
        runtime_secrets.file_secrets.cache_clear()

    def test_environment_value_takes_precedence_over_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            path.write_text('TOKEN = "from-file"\n', encoding="utf-8")
            with patch.object(runtime_secrets, "_candidate_paths", return_value=(path,)), patch.dict(
                os.environ, {"TOKEN": "from-environment"}, clear=False
            ):
                runtime_secrets.file_secrets.cache_clear()
                self.assertEqual(runtime_secrets.get_secret("TOKEN"), "from-environment")

    def test_database_url_is_built_from_toml_and_credentials_are_escaped(self):
        content = """
[connections.postgresql]
dialect = "postgresql"
username = "user@example.com"
password = "p@ss/word"
host = "database.example"
port = 5432
database = "marksys"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            path.write_text(content, encoding="utf-8")
            with patch.object(runtime_secrets, "_candidate_paths", return_value=(path,)), patch.dict(
                os.environ, {}, clear=True
            ):
                runtime_secrets.file_secrets.cache_clear()
                self.assertEqual(
                    runtime_secrets.get_database_url(),
                    "postgresql://user%40example.com:p%40ss%2Fword@database.example:5432/marksys",
                )

    def test_invalid_secret_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            path.write_text("not valid = [", encoding="utf-8")
            with patch.object(runtime_secrets, "_candidate_paths", return_value=(path,)):
                runtime_secrets.file_secrets.cache_clear()
                self.assertEqual(runtime_secrets.file_secrets(), {})


if __name__ == "__main__":
    unittest.main()
