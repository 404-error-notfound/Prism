"""Deep-fetch endpoint — returns clean Markdown for a single URL.

POST /api/fetch
  {"url": "https://example.com", "use_browser": false}

Requires a valid API key. use_browser=true requires tenant.allow_browser.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.auth import TenantConfig, authenticate
from app.core.validator import validate_fetch_url_async
from app.services.fetcher import fetch_page_content

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["fetch"])


class FetchRequest(BaseModel):
    url: str
    use_browser: bool = False   # force Crawl4AI even for static pages


class FetchResponse(BaseModel):
    url: str
    content: str | None
    fetched: bool


@router.post("/fetch", response_model=FetchResponse)
async def fetch_url(
    body: FetchRequest,
    tenant: TenantConfig = Depends(authenticate),
) -> FetchResponse:
    # SSRF guard（async 版内部把阻塞 DNS 卸到线程池，避免冻结事件循环）
    try:
        await validate_fetch_url_async(body.url)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Invalid or disallowed URL")

    if body.use_browser and not tenant.allow_browser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Browser-based fetching not enabled for this tenant",
        )

    logger.info("fetch.request url=%s use_browser=%s tenant=%s",
                body.url, body.use_browser, tenant.tenant_id)

    # fetch_page_content 返回 (content, robots_allowed)；本端点是用户显式指定 URL，
    # 无需按 robots 脱敏 URL，丢弃第二个返回值即可。
    content, _robots_allowed = await fetch_page_content(body.url)
    return FetchResponse(url=body.url, content=content, fetched=content is not None)
