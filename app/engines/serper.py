"""Serper.dev adapter — Google SERP via API (~¥2-7/千次)."""
from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.engines.base import BaseEngine, RawResult

logger = logging.getLogger(__name__)

_API_URL = "https://google.serper.dev/search"
_TIMEOUT = httpx.Timeout(8.0, connect=5.0)


class SerperEngine(BaseEngine):
    name = "serper"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.serper_api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, num: int = 20, **kwargs) -> list[RawResult]:
        if not self.is_configured():
            return []

        payload = {"q": query, "num": min(num, 10)}
        if kwargs.get("recency_days") and kwargs["recency_days"] <= 7:
            payload["tbs"] = "qdr:w"
        elif kwargs.get("recency_days") and kwargs["recency_days"] <= 31:
            payload["tbs"] = "qdr:m"

        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(_API_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Serper error: %s", exc)
            return []

        results: list[RawResult] = []
        for item in data.get("organic", [])[:num]:
            results.append(RawResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                published_at=item.get("date"),
                engine="serper",
            ))
        return results
