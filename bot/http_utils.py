from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

import httpx

log = logging.getLogger(__name__)
T = TypeVar("T")
BACKOFF_SCHEDULE = [1, 2, 4, 8, 16, 30]


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = len(BACKOFF_SCHEDULE),
) -> T | None:
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            return await func()
        except Exception as exc:
            last_error = exc
            delay = BACKOFF_SCHEDULE[min(i, len(BACKOFF_SCHEDULE) - 1)]
            log.warning("%s failed (attempt %s/%s): %s — retrying in %ss", what, i + 1, attempts, exc, delay)
            await asyncio.sleep(delay)
    log.error("%s permanently failed after %s attempts: %s", what, attempts, last_error)
    return None


def run_sync_with_retry(func: Callable[[], T], *, what: str, attempts: int = 6) -> T | None:
    import time
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            delay = BACKOFF_SCHEDULE[min(i, len(BACKOFF_SCHEDULE) - 1)]
            log.warning("%s failed (attempt %s/%s): %s — retrying in %ss", what, i + 1, attempts, exc, delay)
            time.sleep(delay)
    log.error("%s permanently failed: %s", what, last_error)
    return None


async def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any | None:
    async def _do() -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                log.error("HTTP %s from %s — body: %s", resp.status_code, url, resp.text[:800])
                resp.raise_for_status()
            return resp.json()
    return await retry_async(_do, what=f"GET {url}")
