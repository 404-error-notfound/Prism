"""DuckDuckGo Search adapter (ddgs library) — free Tier 1 fallback."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from app.engines.base import BaseEngine, RawResult

logger = logging.getLogger(__name__)

# Dedicated thread pool so DDGS blocking calls don't compete with other I/O threads
_DDGS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ddgs")
_DDGS_TIMEOUT = 6.0  # tighter than orchestrator's 8s so we fail fast


class DDGSEngine(BaseEngine):
    name = "ddgs"

    async def search(self, query: str, num: int = 20, **kwargs) -> list[RawResult]:
        if not _ddgs_available():
            logger.debug("ddgs not installed, skipping")
            return []

        try:
            from duckduckgo_search import DDGS

            loop = asyncio.get_event_loop()
            results = await asyncio.wait_for(
                loop.run_in_executor(_DDGS_POOL, lambda: list(DDGS().text(query, max_results=num))),
                timeout=_DDGS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("DDGS timed out after %.1fs", _DDGS_TIMEOUT)
            raise
        except Exception as exc:
            logger.warning("DDGS error: %s", exc)
            raise

        return [
            RawResult(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", ""),
                published_at=None,
                engine="ddgs",
            )
            for r in results
        ]


def _ddgs_available() -> bool:
    try:
        import duckduckgo_search  # noqa: F401
        return True
    except ImportError:
        return False
