"""火山引擎联网搜索适配器（原名：融合信息搜索 API）

APIKey 接入（推荐）：
  Endpoint : https://open.feedcoopapi.com/search_api/web_search
  Auth     : Authorization: Bearer <API_KEY>

免费额度：
  - 个人认证账号：每月 500 次（web / web_summary 各自独立计算）
  - 企业认证账号：付费开通前累计 5000 次
  - QPS 默认 5，需扩容可提工单

错误码 10406 = 免费额度用尽 → 抛 QuotaExhaustedError，由上层切换到下一个引擎。
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.engines.base import BaseEngine, RawResult

logger = logging.getLogger(__name__)

_ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            http2=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


class QuotaExhaustedError(Exception):
    """引擎免费配额耗尽（HTTP 200 但错误码 10406）。"""


class VolcEngineEngine(BaseEngine):
    name = "volcengine"

    def is_configured(self) -> bool:
        return bool(settings.volcengine_api_key)

    async def search(self, query: str, num: int = 10, **kwargs) -> list[RawResult]:
        if not self.is_configured():
            logger.debug("volcengine: API key not set, skipping")
            return []

        payload: dict = {
            "Query": query,
            "SearchType": "web",
            "Count": min(num, 50),
            "NeedSummary": True,   # 返回 ~500 字模型摘要，比 Snippet 更适合 LLM
            "Filter": {"NeedUrl": True},
        }
        time_range = _recency_to_time_range(kwargs.get("recency_days", 30))
        if time_range:
            payload["TimeRange"] = time_range

        headers = {
            "Authorization": f"Bearer {settings.volcengine_api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = await _get_client().post(_ENDPOINT, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("volcengine: HTTP error %s", exc)
            raise
        except Exception as exc:
            logger.warning("volcengine: request error %s", exc)
            raise

        # API 层错误（HTTP 200 但 ResponseMetadata 含 Error）
        error = data.get("ResponseMetadata", {}).get("Error")
        if error:
            code = error.get("CodeN") or int(error.get("Code", 0))
            msg = error.get("Message", "")
            if code == 10406:
                logger.warning("volcengine: free quota exhausted")
                raise QuotaExhaustedError("volcengine free quota exhausted")
            logger.warning("volcengine: API error code=%s msg=%s", code, msg)
            raise RuntimeError(f"volcengine error {code}: {msg}")

        web_results = (data.get("Result") or {}).get("WebResults") or []
        return [
            RawResult(
                title=item.get("Title", ""),
                url=item.get("Url", ""),
                # Summary ~500字，Snippet ~100字；优先用 Summary
                snippet=item.get("Summary") or item.get("Snippet", ""),
                published_at=item.get("PublishTime"),
                engine="volcengine",
                extra={
                    "site_name": item.get("SiteName", ""),
                    "rank_score": item.get("RankScore"),
                    "auth_level": item.get("AuthInfoLevel"),  # 1=非常权威 … 4=一般不权威
                },
            )
            for item in web_results
            if item.get("Url")
        ]

    async def health_check(self) -> bool:
        return self.is_configured()


def _recency_to_time_range(days: int) -> str:
    if days <= 1:
        return "OneDay"
    if days <= 7:
        return "OneWeek"
    if days <= 31:
        return "OneMonth"
    if days <= 365:
        return "OneYear"
    return ""  # 不限制时间
