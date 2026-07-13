import inspect
import unittest
from unittest.mock import Mock, patch

from core import db_runtime
from deploy import proxy


class DatabaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        db_runtime._engine = None

    def tearDown(self):
        db_runtime._engine = None

    def test_unconfigured_database_does_not_create_engine(self):
        with patch.object(db_runtime, "get_database_url", return_value=None), patch.object(
            db_runtime, "create_engine"
        ) as create:
            self.assertIsNone(db_runtime.get_db_engine())
        create.assert_not_called()

    def test_engine_is_created_once_with_bounded_pool(self):
        engine = Mock()
        with patch.object(
            db_runtime, "get_database_url", return_value="postgresql://db/test"
        ), patch.object(db_runtime, "create_engine", return_value=engine) as create, patch.object(
            db_runtime.event, "listens_for", side_effect=lambda *_args: lambda fn: fn
        ):
            self.assertIs(db_runtime.get_db_engine(), engine)
            self.assertIs(db_runtime.get_db_engine(), engine)
        create.assert_called_once_with(
            "postgresql://db/test",
            pool_pre_ping=True,
            pool_size=db_runtime.DB_POOL_SIZE,
            max_overflow=db_runtime.DB_MAX_OVERFLOW,
            pool_timeout=db_runtime.DB_POOL_TIMEOUT,
            pool_recycle=db_runtime.DB_POOL_RECYCLE,
        )

    def test_dispose_releases_pool_and_clears_singleton(self):
        engine = Mock()
        db_runtime._engine = engine
        db_runtime.dispose_db_engine()
        engine.dispose.assert_called_once_with()
        self.assertIsNone(db_runtime._engine)

    def test_every_connection_sets_the_expected_search_path(self):
        listeners = {}

        def register(_engine, event_name):
            def decorator(function):
                listeners[event_name] = function
                return function

            return decorator

        with patch.object(
            db_runtime, "get_database_url", return_value="postgresql://db/test"
        ), patch.object(db_runtime, "create_engine", return_value=Mock()), patch.object(
            db_runtime.event, "listens_for", side_effect=register
        ):
            db_runtime.get_db_engine()
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value = cursor
        listeners["connect"](connection, None)
        cursor.execute.assert_called_once_with(
            "SET search_path TO public, extensions"
        )
        cursor.close.assert_called_once_with()

    def test_proxy_keeps_small_compatibility_wrapper(self):
        source = inspect.getsource(proxy._get_db_engine)
        self.assertIn("get_db_engine()", source)
        self.assertNotIn("create_engine", source)

    def test_runtime_db_is_available_without_http_layer(self):
        engine = Mock()
        with patch.object(db_runtime, "get_db_engine", return_value=engine):
            db = db_runtime.get_runtime_db()
        self.assertIsInstance(db, db_runtime.RuntimeDb)
        self.assertIs(db._engine, engine)


class DatabaseLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_is_disposed_after_normal_shutdown(self):
        with patch.object(proxy, "run_safe_startup_migrations"), patch.object(
            proxy, "dispose_db_engine"
        ) as dispose:
            async with proxy._lifespan(None):
                pass
        dispose.assert_called_once_with()

    async def test_pool_is_disposed_when_startup_migration_fails(self):
        with patch.object(
            proxy, "run_safe_startup_migrations", side_effect=RuntimeError("failed")
        ), patch.object(proxy, "dispose_db_engine") as dispose:
            with self.assertRaises(RuntimeError):
                async with proxy._lifespan(None):
                    pass
        dispose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
