from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Any Postgres URL. The web app uses Neon's *pooled* URL, collectors/migrations the direct one.
    database_url: str = "postgresql+psycopg://gasto:gasto@localhost:5433/gasto"
    allowed_origins: str = ""

    # --- auth (single user) ---
    # Output of `uv run python -m app.tools.hash_password`.
    app_password_hash: str = ""
    # Long random string: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    session_secret: str = ""
    session_max_age_days: int = 400
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_window_minutes: int = 15

    # --- external calls (must stay well inside a serverless request) ---
    off_user_agent: str = "GastoSuper/0.1 (personal spending tracker)"
    off_base_url: str = "https://world.openfoodfacts.org"
    off_timeout_seconds: float = 5.0
    refresh_timeout_seconds: float = 5.0
    # User-Agent for the price sources (collectors and "refresh now").
    sources_user_agent: str = "GastoSuper/0.1 (personal price comparison)"

    store_rules_path: Path = BASE_DIR / "app" / "store_rules.json"
    timezone: str = "Europe/Madrid"
    public_dir: Path = BASE_DIR / "public"
    # Vercel sets VERCEL=1; there the CDN serves public/ and the app must not mount it.
    vercel: str | None = None
    default_postal_code: str = "28020"
    stale_after_days: int = 7

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
