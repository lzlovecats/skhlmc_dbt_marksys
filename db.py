"""Low-level database primitives.

The lowest layer of the app: a thin wrapper around Streamlit's SQL connection.
Everything else (functions.py, auth.py, pages) depends on this module, and it
depends on nothing internal — so it never causes a circular import.
"""

from contextlib import contextmanager

import pandas as pd
import streamlit as st
from sqlalchemy import text

DB_SEARCH_PATH = "public, extensions"


def _set_search_path(session):
    session.execute(text(f"SET search_path TO {DB_SEARCH_PATH}"))


class _SchemaSessionContext:
    def __init__(self, context):
        self.context = context
        self.session = None

    def __enter__(self):
        self.session = self.context.__enter__()
        _set_search_path(self.session)
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return self.context.__exit__(exc_type, exc, tb)


class _SchemaAwareConnection:
    def __init__(self, conn):
        self.conn = conn

    @property
    def session(self):
        return _SchemaSessionContext(self.conn.session)

    def query(self, sql_str, ttl=None, params=None, **kwargs):
        with self.session as s:
            result = s.execute(text(sql_str), params or {})
            rows = result.fetchall()
            columns = list(result.keys())
        return pd.DataFrame(rows, columns=columns)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def get_connection():
    conn = st.connection("postgresql", type="sql")
    return _SchemaAwareConnection(conn)


def execute_query(sql_str, params=None):
    conn = get_connection()
    with conn.session as s:
        s.execute(text(sql_str), params or {})
        s.commit()


def query_params(sql_str, params=None):
    conn = get_connection()
    with conn.session as s:
        result = s.execute(text(sql_str), params or {})
        rows = result.fetchall()
        columns = list(result.keys())
    return pd.DataFrame(rows, columns=columns)


def execute_query_count(sql_str, params=None):
    conn = get_connection()
    with conn.session as s:
        result = s.execute(text(sql_str), params or {})
        count = result.rowcount
        s.commit()
    return count


class StreamlitDb:
    """DB executor over Streamlit's connection, matching the duck-typed contract
    consumed by ``core`` domain logic: ``query`` / ``execute`` / ``execute_count``.

    Lets the same domain functions run under Streamlit (this class) or under the
    streamlit-free proxy (its own engine-backed executor) without either process
    importing the other's runtime.
    """

    def query(self, sql_str, params=None):
        return query_params(sql_str, params)

    def execute(self, sql_str, params=None):
        execute_query(sql_str, params)

    def execute_count(self, sql_str, params=None):
        return execute_query_count(sql_str, params)

    @contextmanager
    def transaction(self):
        """Yield and commit one SQLAlchemy session for atomic domain writes."""
        conn = get_connection()
        with conn.session as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise


def default_db():
    """The Streamlit-runtime DB executor used when domain logic is called without
    an explicit ``db`` (i.e. from the Streamlit pages)."""
    return StreamlitDb()
