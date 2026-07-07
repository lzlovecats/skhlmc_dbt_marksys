"""Low-level database primitives.

The lowest layer of the app: a thin wrapper around Streamlit's SQL connection.
Everything else (functions.py, auth.py, pages) depends on this module, and it
depends on nothing internal — so it never causes a circular import.
"""

import pandas as pd
import streamlit as st
from sqlalchemy import text


def get_connection():
    conn = st.connection("postgresql", type="sql")
    return conn


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
