"""Single-user login: scrypt password hash in an env var, signed long-lived session cookie.

- APP_PASSWORD_HASH: output of `uv run python -m app.tools.hash_password`
- SESSION_SECRET: random string used to sign the cookie (rotate it to log out every device)
Changing the password also invalidates existing sessions (the token embeds a hash fingerprint).
If either variable is missing, nobody can log in (fail closed).
"""

import base64
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings, get_settings

COOKIE_NAME = "gs_session"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    # ":"-separated (no "$"), so the value survives .env files and shells without quoting.
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{enc(salt)}:{enc(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split(":")
        if scheme != "scrypt":
            return False
        dec = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        expected = dec(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=dec(salt_b64), n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def auth_configured(settings: Settings) -> bool:
    return bool(settings.app_password_hash and settings.session_secret and len(settings.session_secret) >= 32)


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="gasto-super-session")


def _fingerprint(settings: Settings) -> str:
    return hashlib.sha256(settings.app_password_hash.encode()).hexdigest()[:16]


def make_session_token(settings: Settings) -> str:
    return _serializer(settings).dumps({"v": 1, "pw": _fingerprint(settings)})


def session_valid(token: str | None, settings: Settings) -> bool:
    if not token or not auth_configured(settings):
        return False
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_max_age_days * 86400)
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and hmac.compare_digest(str(data.get("pw", "")), _fingerprint(settings))


def set_session_cookie(response: Response, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(settings),
        max_age=settings.session_max_age_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")


def require_session(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not session_valid(request.cookies.get(COOKIE_NAME), settings):
        raise HTTPException(status_code=401, detail="unauthorized")


def client_ip(request: Request) -> str:
    # Vercel sets X-Forwarded-For to the real client IP (it overwrites any client-supplied value).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]
