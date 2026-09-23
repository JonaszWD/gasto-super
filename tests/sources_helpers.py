"""Helpers for adapter tests: recorded fixtures served through httpx.MockTransport (no network)."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from app.sources.base import MemoryCache, PoliteClient, SourceContext

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(path: str) -> object:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


async def no_sleep(_: float) -> None:
    return None


def make_ctx(
    handler: Callable[[httpx.Request], httpx.Response], postal_code: str = "28020", chains: set[str] | None = None
) -> tuple[SourceContext, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = PoliteClient(transport=httpx.MockTransport(record), min_interval=0, sleep=no_sleep, retries=2)
    return SourceContext(postal_code=postal_code, http=client, cache=MemoryCache(), chains=chains), seen
