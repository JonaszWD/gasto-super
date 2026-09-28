"""Common interface for price sources, plus a polite HTTP client.

Every adapter implements the same three operations:
- search(query)            -> matching listings (optional; raise SearchNotSupported)
- fetch_catalog()          -> every listing it covers for the configured postal code
- fetch_product(product_id)-> one listing, fresh (used for "refresh now")

Adapters never write to the database; the collector runner (collectors/) and the refresh
endpoint do that. One adapter failing must never affect another, so callers isolate them.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.services.catalog import RawListing

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "GastoSuper/0.1 (personal price comparison)"


class SourceError(Exception):
    pass


class SearchNotSupported(SourceError):
    pass


class LiveRefreshNotSupported(SourceError):
    pass


class KeyValueCache:
    """Tiny persistent cache interface (backed by the app_setting table in production)."""

    def get(self, key: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class MemoryCache(KeyValueCache):
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class PoliteClient:
    """Serial HTTP client: fixed gap between requests, retries with backoff, honours Retry-After.

    One instance per source, and requests are awaited one after another, so we never hit the
    same chain in parallel.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval: float = 1.5,
        timeout: float = 15.0,
        retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )
        self.min_interval = min_interval
        self.retries = retries
        self._sleep = sleep
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.request_count = 0

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._lock:
            attempt = 0
            while True:
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    await self._sleep(wait)
                self._last = time.monotonic()
                self.request_count += 1
                try:
                    resp = await self._client.request(method, url, **kwargs)
                except httpx.TransportError as exc:
                    if attempt >= self.retries:
                        raise SourceError(f"{method} {url}: {exc}") from exc
                    attempt += 1
                    await self._sleep(2**attempt)
                    continue
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    attempt += 1
                    await self._sleep(_retry_after(resp) or 2**attempt * 2)
                    continue
                return resp

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", url, **kwargs)
        if resp.status_code >= 400:
            raise SourceError(f"GET {url}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"GET {url}: invalid JSON") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(float(value), 120.0)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


@dataclass
class SourceContext:
    postal_code: str
    http: PoliteClient
    cache: KeyValueCache
    options: dict[str, Any] = field(default_factory=dict)
    # Only these chains (None = all the source covers).
    chains: set[str] | None = None
    # Non-fatal problems the runner should report (e.g. one chain's data skipped).
    warnings: list[str] = field(default_factory=list)


class SourceAdapter(ABC):
    id: str
    chains: tuple[str, ...]
    location_specific: bool = False
    supports_live_refresh: bool = False
    min_interval: float = 1.5
    # Overrides the configured SOURCES_USER_AGENT for sources that only answer browsers.
    user_agent: str | None = None

    async def search(self, ctx: SourceContext, query: str) -> list[RawListing]:
        raise SearchNotSupported(self.id)

    @abstractmethod
    def fetch_catalog(self, ctx: SourceContext) -> AsyncIterator[RawListing]: ...

    async def fetch_product(self, ctx: SourceContext, chain_product_id: str) -> RawListing | None:
        raise LiveRefreshNotSupported(self.id)

    def wants_chain(self, ctx: SourceContext, chain_id: str) -> bool:
        return ctx.chains is None or chain_id in ctx.chains


def euros_to_cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return round(float(str(value).strip()) * 100)
    except ValueError:
        return None
