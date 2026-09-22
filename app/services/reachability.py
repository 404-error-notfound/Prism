"""URL reachability checker for Filter.NeedUrl=true.

Issues parallel HEAD requests (falls back to GET on 405) and marks each
SearchResult.reachable = True/False. Bounded concurrency so a flood of slow
hosts can't blow the latency budget; per-URL timeout is short.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import BROWSER_USER_AGENT
from app.schemas.search import SearchResult

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
_CONCURRENCY = 10
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
    return _client


async def close_reachability_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _check(url: str, sem: asyncio.Semaphore) -> bool:
    if not url:
        return False
    async with sem:
        client = _get_client()
        try:
            resp = await client.head(url)
            # Some servers don't allow HEAD — retry with a tiny GET
            if resp.status_code in (403, 405, 501):
                resp = await client.get(url, headers={"Range": "bytes=0-0"})
            return 200 <= resp.status_code < 400
        except Exception:
            return False


async def annotate_reachability(results: list[SearchResult], timeout: float = 2.5) -> None:
    """Mutate `results` in place, setting r.reachable on each.

    Best-effort: any URL that doesn't finish within `timeout` is left as None
    so callers can distinguish "unchecked" from "verified unreachable".
    """
    if not results:
        return
    sem = asyncio.Semaphore(_CONCURRENCY)
    tasks = {r.url: asyncio.create_task(_check(r.url, sem)) for r in results if r.url}
    if not tasks:
        return

    done, pending = await asyncio.wait(tasks.values(), timeout=timeout)
    for t in pending:
        t.cancel()

    for r in results:
        task = tasks.get(r.url)
        if task is None:
            r.reachable = None
        elif task in done:
            try:
                r.reachable = task.result()
            except Exception:
                r.reachable = False
        else:
            r.reachable = False  # timed out
