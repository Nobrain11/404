"""Exponential-backoff retry for all outbound calls."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx

log = logging.getLogger(__name__)
T = TypeVar("T")
BACKOFF = [1, 2, 4, 8, 16, 30]


async def retry_async(func: Callable[[], Awaitable[T]], *, what: str, attempts: int = 6) -> T | None:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await func()
        except Exception as exc:
            last = exc
            delay = BACKOFF[min(i, len(BACKOFF) - 1)]
            log.warning("%s failed (%s/%s): %s — retry in %ss", what, i+1, attempts, exc, delay)
            await asyncio.sleep(delay)
    log.error("%s permanently failed: %s", what, last)
    return None


def run_sync_retry(func: Callable[[], T], *, what: str, attempts: int = 6) -> T | None:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return func()
        except Exception as exc:
            last = exc
            delay = BACKOFF[min(i, len(BACKOFF) - 1)]
            log.warning("%s failed (%s/%s): %s — retry in %ss", what, i+1, attempts, exc, delay)
            time.sleep(delay)
    log.error("%s permanently failed: %s", what, last)
    return None


async def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any | None:
    async def _do():
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params=params)
            if r.status_code >= 400:
                log.error("HTTP %s from %s — %s", r.status_code, url, r.text[:500])
                r.raise_for_status()
            return r.json()
    return await retry_async(_do, what=f"GET {url}")
