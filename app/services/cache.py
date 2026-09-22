"""Result cache with tiered TTL by query intent.

TTL policy:
  news      →  10 minutes
  general   →  1 hour   (default)
  reference →  1 hour
  sparse (<3 results) → 5 minutes
"""
from __future__ import annotations

import hashlib
import json
import logging

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_TTL = {
    "news": 600,
}
_DEFAULT_TTL = 3600
_SPARSE_TTL = 300   # result count < this threshold → short TTL to avoid caching low-quality responses
_SPARSE_THRESHOLD = 3
_STALE_TTL = 86400


def _cache_key(
    tenant_id: str,
    query: str,
    top_n: int,
    recency_days: int,
    *,
    need_content: bool = False,
    need_summary: bool = False,
    need_url: bool = False,
    content_formats: str = "text",
    sites: str = "",
    block_hosts: str = "",
    auth_level: int = 0,
    sort_by: str = "hybrid",
    intent: str = "auto",
) -> str:
    # Shape-affecting fields must all be part of the key. Changing NeedSummary
    # false→true must NOT hit a cached response that lacks SummarizedAnswer.
    parts = [
        tenant_id,
        query.lower().strip(),
        str(top_n),
        str(recency_days),
        f"nc={int(need_content)}",
        f"ns={int(need_summary)}",
        f"nu={int(need_url)}",
        f"cf={content_formats.lower()}",
        f"st={sites.lower()}",
        f"bh={block_hosts.lower()}",
        f"al={auth_level}",
        f"sb={sort_by}",
        f"in={intent}",
    ]
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"cache:search:{digest}"


async def get_cached(
    tenant_id: str,
    query: str,
    top_n: int,
    recency_days: int,
    **key_fields,
) -> dict | None:
    """Return cached payload {"results": [...], "summary": "..."} or None."""
    key = _cache_key(tenant_id, query, top_n, recency_days, **key_fields)
    r = get_redis()
    raw = await r.get(key)
    if not raw:
        return None
    logger.debug("Cache hit key=%s", key)
    payload = json.loads(raw)
    # Backward-compat: older entries stored a bare list of results.
    if isinstance(payload, list):
        return {"results": payload, "summary": ""}
    return payload


async def get_stale_cached(
    tenant_id: str,
    query: str,
    top_n: int,
    recency_days: int,
    **key_fields,
) -> dict | None:
    """Return stale payload for resilience fallback, or None."""
    key = _cache_key(tenant_id, query, top_n, recency_days, **key_fields) + ":stale"
    r = get_redis()
    raw = await r.get(key)
    if not raw:
        return None
    logger.debug("Stale cache hit key=%s", key)
    payload = json.loads(raw)
    if isinstance(payload, list):
        return {"results": payload, "summary": ""}
    return payload

async def set_cached(
    tenant_id: str,
    query: str,
    top_n: int,
    recency_days: int,
    results: list[dict],
    intent: str = "general",
    summary: str = "",
    **key_fields,
) -> None:
    # 空结果不缓存：引擎故障期间的 0 结果会污染缓存长达 TTL（最长 24h），
    # 故障恢复后仍持续返回空。空结果重查引擎的代价远小于持续返回空的代价。
    if not results:
        return
    key = _cache_key(tenant_id, query, top_n, recency_days, intent=intent, **key_fields)
    if recency_days <= 1 or len(results) < _SPARSE_THRESHOLD:
        ttl = _SPARSE_TTL
    else:
        ttl = _TTL.get(intent, _DEFAULT_TTL)
    payload = {"results": results, "summary": summary}
    r = get_redis()
    await r.setex(key, ttl, json.dumps(payload, ensure_ascii=False))
    await r.setex(key + ":stale", _STALE_TTL, json.dumps(payload, ensure_ascii=False))
    logger.debug("Cache set key=%s ttl=%s", key, ttl)
