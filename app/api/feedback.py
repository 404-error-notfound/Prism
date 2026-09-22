"""Explicit search feedback endpoint (optional MCP supplement)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import TenantConfig, authenticate, hmac_hash
from app.core.redis import get_redis
from app.schemas.search import FeedbackRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["feedback"])

_REPUTATION_KEY = "reputation:domain:{domain}"
_FEEDBACK_TTL = 90 * 86400  # 90 days


@router.post("/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def submit_feedback(
    body: FeedbackRequest,
    tenant: TenantConfig = Depends(authenticate),
) -> None:
    """
    Explicit feedback from LLM or end-user on a search result URL.
    Weight: × 1.5 multiplier on top of session behavior signals (Stage 4+).
    """
    if not body.url:
        raise HTTPException(status_code=400, detail="url is required")

    from app.services.dedup import extract_domain
    domain = extract_domain(body.url)

    # Hash PII before storing
    uid_hash = hmac_hash(body.user_id) if body.user_id else "anonymous"

    r = get_redis()
    key = _REPUTATION_KEY.format(domain=domain)
    field = "good" if body.useful else "bad"

    pipe = r.pipeline()
    pipe.hincrby(key, field, 1)
    pipe.expire(key, _FEEDBACK_TTL)
    await pipe.execute()

    logger.debug(
        "Feedback domain=%s useful=%s tenant=%s uid=%s",
        domain, body.useful, tenant.tenant_id, uid_hash,
    )
