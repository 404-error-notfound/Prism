"""DataForSEO adapter — Google/Bing SERP (~¥0.15/千次, cheapest Tier 2)."""
from __future__ import annotations

import base64
import logging

import httpx

from app.core.config import settings
from app.engines.base import BaseEngine, RawResult

logger = logging.getLogger(__name__)

_API_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class DataForSEOEngine(BaseEngine):
    name = "dataforseo"

    def __init__(self, login: str | None = None, password: str | None = None) -> None:
        self.login = login or settings.dataforseo_login
        self.password = password or settings.dataforseo_password

    def is_configured(self) -> bool:
        return bool(self.login and self.password)

    def _auth_header(self) -> str:
        creds = f"{self.login}:{self.password}"
        return "Basic " + base64.b64encode(creds.encode()).decode()

    async def search(self, query: str, num: int = 20, **kwargs) -> list[RawResult]:
        if not self.is_configured():
            return []

        payload = [{"keyword": query, "language_code": "zh-cn", "depth": min(num, 100)}]
        headers = {"Authorization": self._auth_header(), "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(_API_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("DataForSEO error: %s", exc)
            return []

        results: list[RawResult] = []
        try:
            items = data["tasks"][0]["result"][0]["items"]
        except (KeyError, IndexError, TypeError):
            return []

        for item in items[:num]:
            if item.get("type") != "organic":
                continue
            results.append(RawResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                published_at=item.get("timestamp"),
                engine="dataforseo",
            ))
        return results
