"""Tavily adapter — high-quality answer-oriented search (Tier 2 / high_quality)."""
from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.engines.base import BaseEngine, RawResult

logger = logging.getLogger(__name__)

_API_URL = "https://api.tavily.com/search"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


class TavilyEngine(BaseEngine):
    name = "tavily"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.tavily_api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, num: int = 10, **kwargs) -> list[RawResult]:
        if not self.is_configured():
            return []

        # Tavily caps max_results at 20; "advanced" is slower but better-ranked.
        payload = {
            "query": query,
            "max_results": min(num, 20),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        if kwargs.get("recency_days"):
            days = kwargs["recency_days"]
            if days <= 1:
                payload["days"] = 1
                payload["topic"] = "news"
            elif days <= 7:
                payload["days"] = 7
            elif days <= 30:
                payload["days"] = 30

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(_API_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Tavily error: %s", exc)
            return []

        results: list[RawResult] = []
        for item in data.get("results", [])[:num]:
            results.append(RawResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                published_at=item.get("published_date"),
                engine="tavily",
                extra={"tavily_score": item.get("score")},
            ))
        return results
