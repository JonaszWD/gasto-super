from sqlmodel import Session

from app.models import AppSetting
from app.sources.base import KeyValueCache


class DbCache(KeyValueCache):
    """Persistent key/value cache in the app_setting table (e.g. postal code -> warehouse)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> str | None:
        row = self.session.get(AppSetting, key)
        return row.value if row else None

    def set(self, key: str, value: str) -> None:
        row = self.session.get(AppSetting, key) or AppSetting(key=key, value=value)
        row.value = value
        self.session.add(row)
        self.session.flush()
