from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import col, delete, func, select

from app.auth import (
    COOKIE_NAME,
    auth_configured,
    clear_session_cookie,
    client_ip,
    session_valid,
    set_session_cookie,
    verify_password,
)
from app.deps import SessionDep, SettingsDep
from app.models import LoginAttempt, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Cap on failures from all IPs together, against distributed guessing.
GLOBAL_FAILURE_FACTOR = 10


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class SessionOut(BaseModel):
    authenticated: bool


@router.get("/session")
def session_status(request: Request, settings: SettingsDep) -> SessionOut:
    return SessionOut(authenticated=session_valid(request.cookies.get(COOKIE_NAME), settings))


@router.post("/login", status_code=204)
def login(body: LoginIn, request: Request, response: Response, session: SessionDep, settings: SettingsDep) -> None:
    if not auth_configured(settings):
        raise HTTPException(status_code=503, detail="auth_not_configured")
    ip = client_ip(request)
    now = utcnow()
    since = now - timedelta(minutes=settings.login_window_minutes)
    # Housekeeping keeps the table tiny.
    session.exec(delete(LoginAttempt).where(col(LoginAttempt.attempted_at) < now - timedelta(days=1)))

    failures = select(func.count()).select_from(LoginAttempt).where(
        col(LoginAttempt.success).is_(False), col(LoginAttempt.attempted_at) >= since
    )
    ip_failures = session.exec(failures.where(LoginAttempt.ip == ip)).one()
    all_failures = session.exec(failures).one()
    if ip_failures >= settings.login_max_failures or all_failures >= settings.login_max_failures * GLOBAL_FAILURE_FACTOR:
        session.commit()
        raise HTTPException(status_code=429, detail="too_many_attempts", headers={"Retry-After": str(settings.login_window_minutes * 60)})

    ok = verify_password(body.password, settings.app_password_hash)
    session.add(LoginAttempt(ip=ip, success=ok, attempted_at=now))
    session.commit()
    if not ok:
        raise HTTPException(status_code=401, detail="wrong_password")
    set_session_cookie(response, settings)


@router.post("/logout", status_code=204)
def logout(response: Response, settings: SettingsDep) -> None:
    clear_session_cookie(response, settings)
