"""Single-user login and session protection."""

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.auth import (
    COOKIE_NAME,
    hash_password,
    make_session_token,
    session_valid,
    set_session_cookie,
    verify_password,
)
from app.config import Settings, get_settings
from tests.conftest import TEST_PASSWORD

PROTECTED = [
    ("GET", "/api/stores"),
    ("GET", "/api/trips"),
    ("GET", "/api/trips/current"),
    ("GET", "/api/scan/5449000000996"),
    ("GET", "/api/stats"),
    ("GET", "/api/export.csv"),
    ("GET", "/api/compare/search?q=leche"),
    ("GET", "/api/shopping-list"),
    ("GET", "/api/settings"),
    ("POST", "/api/trips"),
    ("DELETE", "/api/purchases/1"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_api_requires_session(anon_client: TestClient, method: str, path: str) -> None:
    r = anon_client.request(method, path, json={} if method != "GET" else None)
    assert r.status_code == 401
    assert r.json()["detail"] == "unauthorized"


def test_public_routes(anon_client: TestClient) -> None:
    assert anon_client.get("/healthz").status_code == 200
    assert anon_client.get("/api/auth/session").json() == {"authenticated": False}


def test_login_sets_long_lived_httponly_cookie(anon_client: TestClient) -> None:
    r = anon_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=34560000" in cookie  # 400 days
    assert anon_client.get("/api/auth/session").json() == {"authenticated": True}
    assert anon_client.get("/api/stores").status_code == 200


def test_cookie_is_secure_in_production() -> None:
    settings = get_settings().model_copy(update={"cookie_secure": True})
    resp = Response()
    set_session_cookie(resp, settings)
    assert "Secure" in resp.headers["set-cookie"]


def test_wrong_password(anon_client: TestClient) -> None:
    r = anon_client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == "wrong_password"
    assert COOKIE_NAME not in r.cookies


def test_login_rate_limit(anon_client: TestClient) -> None:
    for _ in range(5):
        assert anon_client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    r = anon_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 429  # even the right password is refused while locked out
    assert r.json()["detail"] == "too_many_attempts"
    assert "retry-after" in r.headers


def test_rate_limit_is_per_ip(anon_client: TestClient) -> None:
    for _ in range(5):
        anon_client.post("/api/auth/login", json={"password": "wrong"}, headers={"x-forwarded-for": "203.0.113.9"})
    r = anon_client.post("/api/auth/login", json={"password": TEST_PASSWORD}, headers={"x-forwarded-for": "198.51.100.7"})
    assert r.status_code == 204


def test_logout(client: TestClient) -> None:
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/stores").status_code == 401


def test_tampered_cookie_rejected(anon_client: TestClient) -> None:
    anon_client.cookies.set(COOKIE_NAME, make_session_token(get_settings()) + "x")
    assert anon_client.get("/api/stores").status_code == 401


def test_password_change_invalidates_sessions() -> None:
    settings = get_settings()
    token = make_session_token(settings)
    assert session_valid(token, settings)
    changed = settings.model_copy(update={"app_password_hash": hash_password("another password")})
    assert not session_valid(token, changed)


def test_auth_fails_closed_without_config(anon_client: TestClient) -> None:
    empty = Settings(app_password_hash="", session_secret="")
    assert not session_valid(make_session_token(get_settings()), empty)
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: empty
    try:
        r = anon_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert r.status_code == 503
    finally:
        app.dependency_overrides.pop(get_settings)


def test_hash_roundtrip() -> None:
    stored = hash_password("s3cret-password")
    assert stored.startswith("scrypt:") and "$" not in stored
    assert verify_password("s3cret-password", stored)
    assert not verify_password("s3cret-passwore", stored)
    assert not verify_password("x", "garbage")
