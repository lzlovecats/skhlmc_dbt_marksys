"""Focused authentication throttling and revocable committee sessions."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine, text

from api import auth_api, judging_api, kiosk_api, registration_admin_api
from core import auth_logic, judging_logic
from deploy import proxy
from system_limits import (
    COMMITTEE_SESSION_MAX_AGE_SECONDS,
    JUDGING_SESSION_TTL_SECONDS,
    REGISTRATION_ADMIN_SESSION_TTL_SECONDS,
)


def _request(host="203.0.113.10"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


@pytest.fixture(autouse=True)
def _clear_login_limiter():
    auth_logic._reset_login_rate_limit_state()
    yield
    auth_logic._reset_login_rate_limit_state()


def test_login_limiter_enforces_pair_client_and_global_bounds(monkeypatch):
    monkeypatch.setattr(auth_logic, "LOGIN_RATE_WINDOW_SECONDS", 60)
    monkeypatch.setattr(auth_logic, "LOGIN_RATE_MAX_PER_CLIENT_ACCOUNT", 2)
    monkeypatch.setattr(auth_logic, "LOGIN_RATE_MAX_PER_CLIENT", 3)
    monkeypatch.setattr(auth_logic, "LOGIN_RATE_MAX_GLOBAL", 4)

    client = _request("203.0.113.1")
    assert auth_logic.login_rate_limit_retry_after(client, "alice", now=100) is None
    assert auth_logic.login_rate_limit_retry_after(client, "alice", now=101) is None
    assert auth_logic.login_rate_limit_retry_after(client, "alice", now=102) == 58
    assert auth_logic.login_rate_limit_retry_after(client, "bob", now=102) is None
    assert auth_logic.login_rate_limit_retry_after(client, "carol", now=103) == 57

    assert auth_logic.login_rate_limit_retry_after(_request("203.0.113.2"), "dave", now=103) is None
    assert auth_logic.login_rate_limit_retry_after(_request("203.0.113.3"), "erin", now=104) == 56
    assert auth_logic.login_rate_limit_retry_after(_request("203.0.113.3"), "erin", now=161) is None


def test_regular_login_rejects_reserved_account_before_password_check(monkeypatch):
    monkeypatch.setattr(
        auth_logic,
        "authenticate_login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reserved account must not reach bcrypt")
        ),
    )
    monkeypatch.setattr(
        proxy,
        "get_vote_db",
        lambda: (_ for _ in ()).throw(
            AssertionError("reserved account must fail before DB access")
        ),
    )
    with pytest.raises(HTTPException) as denied:
        auth_api.login(
            auth_api.LoginBody(user_id="KIOSK", password="correct-or-not"),
            _request(),
            Response(),
        )
    assert denied.value.status_code == 403


def test_kiosk_limiter_runs_before_password_check(monkeypatch):
    monkeypatch.setattr(auth_logic, "login_rate_limit_retry_after", lambda *_args: 42)
    monkeypatch.setattr(
        auth_logic,
        "authenticate_login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rate-limited request must not reach bcrypt")
        ),
    )
    with pytest.raises(HTTPException) as limited:
        kiosk_api.login(
            kiosk_api.KioskLoginBody(password="guess"), _request(), Response(),
        )
    assert limited.value.status_code == 429
    assert limited.value.headers == {"Retry-After": "42"}


def test_judging_limiter_runs_before_match_lookup_and_password_check(monkeypatch):
    seen = []

    def limited(_request, account_id):
        seen.append(account_id)
        return 42

    monkeypatch.setattr(auth_logic, "login_rate_limit_retry_after", limited)
    monkeypatch.setattr(
        judging_logic,
        "verify_match_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rate-limited request must not query or check bcrypt")
        ),
    )
    monkeypatch.setattr(
        judging_api,
        "_db",
        lambda: (_ for _ in ()).throw(
            AssertionError("rate-limited request must not open the database")
        ),
    )
    with pytest.raises(HTTPException) as blocked:
        judging_api.login(
            judging_api.LoginBody(match_id=" M1 ", password="guess"),
            _request(),
            Response(),
        )
    assert blocked.value.status_code == 429
    assert blocked.value.headers == {"Retry-After": "42"}
    assert seen == ["judging:M1"]


def _auth_engine(password_hash="hash-v1"):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT)"))
        conn.execute(
            text("CREATE TABLE accounts (user_id TEXT PRIMARY KEY, password_hash TEXT)")
        )
        conn.execute(
            text("INSERT INTO app_config(key,value) VALUES ('cookie_secret','secret')")
        )
        conn.execute(
            text(
                "INSERT INTO app_config(key,value) "
                "VALUES ('login_disabled_accounts','[]')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO accounts(user_id,password_hash) "
                "VALUES ('alice',:password_hash)"
            ),
            {"password_hash": password_hash},
        )
    return engine


def _judging_engine(access_code_hash="judge-hash-v1"):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT)"))
        conn.execute(
            text(
                "CREATE TABLE matches ("
                "match_id TEXT PRIMARY KEY, access_code_hash TEXT)"
            )
        )
        conn.execute(
            text("INSERT INTO app_config(key,value) VALUES ('cookie_secret','secret')")
        )
        conn.execute(
            text(
                "INSERT INTO matches(match_id,access_code_hash) "
                "VALUES ('M1',:access_code_hash)"
            ),
            {"access_code_hash": access_code_hash},
        )
    return engine


def _registration_admin_engine(password_hash="admin-hash-v1"):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT)"))
        conn.execute(
            text(
                "INSERT INTO app_config(key,value) VALUES "
                "('cookie_secret','secret'),('admin_password',:password_hash)"
            ),
            {"password_hash": password_hash},
        )
    return engine


def test_versioned_session_expires_and_password_rotation_revokes(monkeypatch):
    engine = _auth_engine()
    monkeypatch.setattr(proxy, "_get_db_engine", lambda: engine)
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)

    token = proxy._sign_committee_token("alice")
    assert token.startswith("ct1.")
    assert proxy._verify_committee_token(token) == "alice"
    assert proxy._verify_committee_token("alice:legacy-signature") is None

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE accounts SET password_hash='hash-v2' WHERE user_id='alice'")
        )
    assert proxy._verify_committee_token(token) is None
    assert proxy._sign_committee_token("alice", credential_hash="hash-v1") is None

    replacement = proxy._sign_committee_token(
        "alice", credential_hash="hash-v2",
    )
    assert proxy._verify_committee_token(replacement) == "alice"
    monkeypatch.setattr(
        proxy.time, "time", lambda: 1_000 + COMMITTEE_SESSION_MAX_AGE_SECONDS,
    )
    assert proxy._verify_committee_token(replacement) is None


def test_disabling_account_revokes_current_session(monkeypatch):
    engine = _auth_engine()
    monkeypatch.setattr(proxy, "_get_db_engine", lambda: engine)
    token = proxy._sign_committee_token("alice")
    assert proxy._verify_committee_token(token) == "alice"

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app_config SET value='[\"ALICE\"]' "
                "WHERE key='login_disabled_accounts'"
            )
        )
    assert proxy._verify_committee_token(token) is None


def test_judging_session_expires_and_access_code_changes_revoke(monkeypatch):
    engine = _judging_engine()
    monkeypatch.setattr(proxy, "_get_db_engine", lambda: engine)
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)

    token = proxy._sign_judging_token(
        "M1", credential_hash="judge-hash-v1",
    )
    assert token.startswith("jt1.")
    assert proxy._verify_judging_token(token) == "M1"
    assert judging_api._match_scope(
        SimpleNamespace(cookies={judging_api.COOKIE_NAME: token})
    ) == "M1"
    assert proxy._verify_judging_token("judging:M1:legacy-signature") is None
    assert proxy._verify_judging_token(token + "tampered") is None
    prefix, encoded, _signature = token.split(".", 2)
    assert proxy._verify_judging_token(f"{prefix}.{encoded}.é") is None
    overflow_payload = proxy._claim_b64(
        (
            '{"v":1,"sub":"M1","iat":1e1000,"exp":2,'
            '"cred":"' + "0" * 64 + '"}'
        ).encode()
    )
    assert proxy._verify_judging_token(
        f"jt1.{overflow_payload}.{'A' * 43}"
    ) is None

    monkeypatch.setattr(
        proxy.time, "time", lambda: 1_000 + JUDGING_SESSION_TTL_SECONDS,
    )
    assert proxy._verify_judging_token(token) is None
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE matches SET access_code_hash='judge-hash-v2' "
                "WHERE match_id='M1'"
            )
        )
    assert proxy._verify_judging_token(token) is None
    assert proxy._sign_judging_token(
        "M1", credential_hash="judge-hash-v1",
    ) is None
    replacement = proxy._sign_judging_token(
        "M1", credential_hash="judge-hash-v2",
    )
    assert proxy._verify_judging_token(replacement) == "M1"

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE matches SET access_code_hash=NULL WHERE match_id='M1'")
        )
    assert proxy._verify_judging_token(replacement) is None
    with pytest.raises(HTTPException) as closed:
        judging_api._match_scope(
            SimpleNamespace(cookies={judging_api.COOKIE_NAME: replacement})
        )
    assert closed.value.status_code == 401

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE matches SET access_code_hash='judge-hash-v3' "
                "WHERE match_id='M1'"
            )
        )
    current = proxy._sign_judging_token(
        "M1", credential_hash="judge-hash-v3",
    )
    assert proxy._verify_judging_token(current) == "M1"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM matches WHERE match_id='M1'"))
    assert proxy._verify_judging_token(current) is None


def test_registration_admin_session_expires_and_password_rotation_revokes(monkeypatch):
    engine = _registration_admin_engine()
    monkeypatch.setattr(proxy, "_get_db_engine", lambda: engine)
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)

    token = proxy._sign_registration_admin_token()
    assert token.startswith("ra1.")
    assert proxy._verify_registration_admin_token(token) is True
    assert proxy._verify_registration_admin_token("registration_admin:legacy") is False

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE app_config SET value='admin-hash-v2' WHERE key='admin_password'")
        )
    assert proxy._verify_registration_admin_token(token) is False

    replacement = proxy._sign_registration_admin_token()
    assert proxy._verify_registration_admin_token(replacement) is True
    monkeypatch.setattr(
        proxy.time,
        "time",
        lambda: 1_000 + REGISTRATION_ADMIN_SESSION_TTL_SECONDS,
    )
    assert proxy._verify_registration_admin_token(replacement) is False


def test_registration_admin_limiter_runs_before_password_check(monkeypatch):
    monkeypatch.setattr(auth_logic, "login_rate_limit_retry_after", lambda *_args: 42)
    monkeypatch.setattr(
        "core.registration_logic.check_admin_password",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rate-limited request must not reach password verification")
        ),
    )
    with pytest.raises(HTTPException) as limited:
        registration_admin_api.login(
            registration_admin_api.LoginBody(password="guess"),
            _request(),
            Response(),
        )
    assert limited.value.status_code == 429
    assert limited.value.headers == {"Retry-After": "42"}


def test_registration_admin_cookie_and_delete_are_secure(monkeypatch):
    monkeypatch.setattr(
        "core.registration_logic.check_admin_password",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(registration_admin_api, "_db", lambda: object())
    monkeypatch.setattr(
        proxy,
        "_sign_registration_admin_token",
        lambda: "ra1.payload.signature",
    )

    response = Response()
    registration_admin_api.login(
        registration_admin_api.LoginBody(password="secret"),
        _request(),
        response,
    )
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert f"Max-Age={REGISTRATION_ADMIN_SESSION_TTL_SECONDS}" in cookie

    logout = Response()
    registration_admin_api.logout(logout)
    deletion = logout.headers["set-cookie"]
    assert "Secure" in deletion and "HttpOnly" in deletion and "SameSite=lax" in deletion


def test_judging_cookie_and_delete_are_secure(monkeypatch):
    monkeypatch.setattr(
        judging_logic,
        "verify_match_access",
        lambda *_args, **_kwargs: {
            "ok": True, "access_code_hash": "judge-hash-v1",
        },
    )
    monkeypatch.setattr(
        proxy,
        "_sign_judging_token",
        lambda _match, **_kwargs: "jt1.payload.signature",
    )
    monkeypatch.setattr(judging_api, "_db", lambda: object())

    response = Response()
    judging_api.login(
        judging_api.LoginBody(match_id="M1", password="secret"),
        _request(),
        response,
    )
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert f"Max-Age={JUDGING_SESSION_TTL_SECONDS}" in cookie

    logout = Response()
    judging_api.logout(logout)
    deletion = logout.headers["set-cookie"]
    assert "Secure" in deletion and "HttpOnly" in deletion and "SameSite=lax" in deletion


def test_committee_cookie_and_delete_are_secure(monkeypatch):
    db = object()
    monkeypatch.setattr(proxy, "get_vote_db", lambda: db)
    monkeypatch.setattr(
        proxy,
        "_sign_committee_token",
        lambda _user, **_kwargs: "ct1.payload.sig",
    )
    monkeypatch.setattr(
        auth_logic, "authenticate_login", lambda *_args, **_kwargs: "verified-hash",
    )
    monkeypatch.setattr(auth_logic, "record_login", lambda *_args, **_kwargs: None)

    response = Response()
    auth_api.login(
        auth_api.LoginBody(user_id="alice", password="secret"),
        _request(),
        response,
    )
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie

    logout = Response()
    auth_api.logout(logout)
    deletion = logout.headers["set-cookie"]
    assert "Secure" in deletion and "HttpOnly" in deletion and "SameSite=lax" in deletion
