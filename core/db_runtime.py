"""Lightweight ownership of the shared SQLAlchemy engine.

Maintenance tools import this module without importing the FastAPI app and all
of its routers. HTTP-specific error handling stays in ``deploy.proxy``.
"""

from __future__ import annotations

import threading

from sqlalchemy import create_engine, event, text

from core.runtime_secrets import get_database_url
from system_limits import (
    DB_MAX_OVERFLOW,
    DB_POOL_RECYCLE,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT,
)


_engine = None
_engine_lock = threading.Lock()


class RuntimeDb:
    """Small transaction/query adapter retained for existing domain modules."""

    def __init__(self, engine):
        self._engine = engine

    def query(self, sql_str, params=None):
        import pandas as pd

        with self._engine.connect() as conn:
            result = conn.execute(text(sql_str), params or {})
            rows = result.fetchall()
            columns = list(result.keys())
        return pd.DataFrame(rows, columns=columns)

    def execute(self, sql_str, params=None):
        with self._engine.begin() as conn:
            conn.execute(text(sql_str), params or {})

    def execute_count(self, sql_str, params=None):
        with self._engine.begin() as conn:
            result = conn.execute(text(sql_str), params or {})
            return result.rowcount

    def transaction(self):
        return self._engine.begin()


def get_db_engine():
    """Return one process-local engine, or ``None`` when DB is unconfigured."""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        db_url = get_database_url()
        if not db_url:
            return None
        engine = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_size=DB_POOL_SIZE,
            max_overflow=DB_MAX_OVERFLOW,
            pool_timeout=DB_POOL_TIMEOUT,
            pool_recycle=DB_POOL_RECYCLE,
        )

        @event.listens_for(engine, "connect")
        def _set_search_path(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET search_path TO public, extensions")
            finally:
                cursor.close()

        _engine = engine
        return _engine


def dispose_db_engine() -> None:
    """Close pooled connections and allow a clean engine to be created later."""
    global _engine
    with _engine_lock:
        engine, _engine = _engine, None
    if engine is not None:
        engine.dispose()


def get_runtime_db() -> RuntimeDb | None:
    engine = get_db_engine()
    return RuntimeDb(engine) if engine is not None else None
