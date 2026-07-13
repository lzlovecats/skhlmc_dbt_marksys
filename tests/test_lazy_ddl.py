import unittest

from core import media_logic
from deploy import proxy


class _Db:
    def __init__(self):
        self._engine = object()
        self.executed = []

    def execute(self, statement, params=None):
        self.executed.append(str(statement))


class _Connection:
    def __init__(self):
        self.engine = object()
        self.executed = []

    def execute(self, statement, params=None):
        self.executed.append(str(statement))


class LazyDdlTests(unittest.TestCase):
    def tearDown(self):
        media_logic._MEDIA_SCHEMA_READY.clear()
        proxy._DDL_READY_ENGINES.clear()

    def test_media_compatibility_ddl_runs_once_per_engine(self):
        media_logic._MEDIA_SCHEMA_READY.clear()
        db = _Db()
        media_logic.ensure_video_interaction_tables(db)
        first_count = len(db.executed)
        media_logic.ensure_video_interaction_tables(db)
        self.assertGreater(first_count, 0)
        self.assertEqual(len(db.executed), first_count)

    def test_proxy_compatibility_ddl_runs_once_per_engine(self):
        proxy._DDL_READY_ENGINES.clear()
        connection = _Connection()
        proxy._ensure_push_subscriptions_table(connection)
        proxy._ensure_push_subscriptions_table(connection)
        self.assertEqual(len(connection.executed), 3)


if __name__ == "__main__":
    unittest.main()
