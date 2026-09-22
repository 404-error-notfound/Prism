"""REST search endpoint with SSE streaming."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from app.api.compat import to_volc_response
from app.core.auth import TenantConfig, authenticate
from app.core.session import resolve_session_id
from app.core.validator import validate_query, validate_top_n, validate_user_id
from app.schemas.search import SearchRequest, VolcFilter, VolcSearchResponse
from app.services.metrics import cache_hits_total, cache_misses_total, search_requests_total
from app.services.orchestrator import get_orchestrator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])


@router.post("/search", response_model=VolcSearchResponse)
async def search(
    body: SearchRequest,
    request: Request,
    tenant: TenantConfig = Depends(authenticate),
) -> VolcSearchResponse:
    """Standard (non-streaming) search endpoint — returns Volcengine-compatible format."""
    query = validate_query(body.Query)
    top_n = validate_top_n(body.Count)
    user_id = validate_user_id(body.user_id)

    req = body.model_copy(update={"Query": query, "Count": top_n, "user_id": user_id})

    session_id = resolve_session_id(request, "http", tenant.tenant_id)
    orch = get_orchestrator()

    try:
        resp = await orch.search(req, tenant.tenant_id, session_id, tenant.api_key)
        status = "success"
        if resp.cached:
            cache_hits_total.labels(tenant=tenant.tenant_id).inc()
        else:
            cache_misses_total.labels(tenant=tenant.tenant_id).inc()
    except Exception:
        logger.exception("Search error tenant=%s query=%s", tenant.tenant_id, query)
        search_requests_total.labels(tenant=tenant.tenant_id, status="error").inc()
        raise

    search_requests_total.labels(tenant=tenant.tenant_id, status=status).inc()
    return to_volc_response(resp, req.SearchType)


_DAYS_TO_TIME_RANGE = {1: "OneDay", 7: "OneWeek", 30: "OneMonth", 365: "OneYear"}


def _days_to_time_range(days: int) -> str:
    """Map recency_days to nearest Volcengine TimeRange bucket."""
    if days <= 1:
        return "OneDay"
    if days <= 7:
        return "OneWeek"
    if days <= 30:
        return "OneMonth"
    if days <= 365:
        return "OneYear"
    return ""


@router.get("/search/stream")
async def search_stream(
    q: str,
    request: Request,
    top_n: int = 10,
    recency_days: int = 30,
    time_range: str = "",
    intent: str = "auto",
    user_id: str = "",
    sort_by: str = "hybrid",
    need_content: bool = False,
    need_summary: bool = False,
    need_url: bool = False,
    tenant: TenantConfig = Depends(authenticate),
) -> EventSourceResponse:
    """SSE streaming search endpoint.

    Accepts either `time_range` (OneDay/OneWeek/OneMonth/OneYear) or legacy
    `recency_days` integer — `time_range` takes precedence when both are given.
    """
    query = validate_query(q)
    top_n_val = validate_top_n(top_n)
    user_id_val = validate_user_id(user_id)
    resolved_time_range = time_range or _days_to_time_range(recency_days)

    req = SearchRequest(
        Query=query,
        Count=top_n_val,
        TimeRange=resolved_time_range,
        intent=intent,  # type: ignore[arg-type]
        user_id=user_id_val,
        sort_by=sort_by,  # type: ignore[arg-type]
        Filter=VolcFilter(NeedContent=need_content, NeedUrl=need_url),
        NeedSummary=need_summary,
    )
    session_id = resolve_session_id(request, "sse", tenant.tenant_id)
    orch = get_orchestrator()

    async def event_generator():
        event_id = 0
        try:
            async for event in orch.search_stream(req, tenant.tenant_id, session_id, tenant.api_key):
                event_id += 1
                yield {
                    "id": str(event_id),
                    "event": event["event"],
                    "data": json.dumps(event["data"], ensure_ascii=False),
                }
        except Exception as exc:
            logger.exception("Stream error: %s", exc)
            yield {
                "id": str(event_id + 1),
                "event": "error",
                "data": json.dumps({"message": str(exc)}),
            }

    return EventSourceResponse(event_generator())
