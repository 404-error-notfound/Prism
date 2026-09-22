"""Liveness and readiness probes."""
from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.core.redis import ping_redis
from app.engines.searxng import check_any_instance

router = APIRouter(tags=["health"])


@router.api_route("/health", methods=["GET", "HEAD"])
async def liveness() -> dict:
    """Liveness probe — always 200 if process is up."""
    return {"status": "ok", "service": settings.service_name}


@router.api_route("/ready", methods=["GET", "HEAD"])
async def readiness(response: Response) -> dict:
    """Readiness probe — checks Redis + SearXNG."""
    checks: dict[str, bool] = {}

    checks["redis"] = await ping_redis()

    # _ping_url 吞掉全部异常（含 gather return_exceptions），不可能抛出
    checks["searxng"] = await check_any_instance()

    # Redis connection pool capacity
    try:
        from app.core.redis import get_pool
        pool = get_pool()
        checks["redis_pool"] = pool._created_connections < pool.max_connections * 0.9
    except Exception:
        checks["redis_pool"] = True  # non-critical

    all_ok = all(checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if all_ok else "degraded", "checks": checks}
