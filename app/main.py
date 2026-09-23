"""FastAPI entrypoint (Vercel loads `app` from here, see [tool.vercel] in pyproject.toml).

No startup work: migrations run separately (alembic), and nothing runs in the background.
"""

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.auth import auth_configured, require_session
from app.config import get_settings
from app.routers import auth, compare, products, shopping_list, stats, stores, trips

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Gasto Súper", docs_url=None, redoc_url=None, openapi_url=None)
    if not auth_configured(settings):
        log.warning("APP_PASSWORD_HASH / SESSION_SECRET not set (or secret < 32 chars): login is disabled")
    if settings.origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=settings.origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)  # login/logout/session: public
    protected = [Depends(require_session)]
    for module in (stores, products, trips, stats, compare, shopping_list):
        app.include_router(module.router, dependencies=protected)

    # On Vercel the CDN serves public/ directly; locally FastAPI serves it.
    if not settings.vercel and settings.public_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.public_dir, html=True), name="public")
    return app


app = create_app()
