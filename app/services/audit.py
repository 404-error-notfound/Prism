"""Search audit — writes complete request pipeline to Elasticsearch.

Each /api/search call produces one document in the `prism_audit` index,
containing the full chain: query → engines → dedup → rank → results.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_es_client: Any = None


async def _get_es() -> Any:
    """Lazy-init ES client (singleton)."""
    global _es_client
    if _es_client is not None:
        return _es_client
    from elasticsearch import AsyncElasticsearch

    kwargs: dict[str, Any] = {
        "hosts": [settings.es_url],
        "request_timeout": 5,
        "retry_on_timeout": False,
        "max_retries": 0,
    }
    if settings.es_user and settings.es_password:
        kwargs["basic_auth"] = (settings.es_user, settings.es_password)
    _es_client = AsyncElasticsearch(**kwargs)
    return _es_client


# prism_audit：一次请求一文档（请求账本）。stages/engine_stats 存而不索引。
_AUDIT_MAPPING = {
    "@timestamp": {"type": "date"},
    "query_id":   {"type": "keyword"},
    "tenant_id":  {"type": "keyword"},
    "session_id": {"type": "keyword"},
    "query":      {"type": "text"},
    "elapsed_ms": {"type": "integer"},
    "cached":     {"type": "boolean"},
    "engines_used": {"type": "keyword"},
    "result_count": {"type": "integer"},
    "client_ip":  {"type": "keyword"},
    "api_key":    {"type": "keyword"},
    "stages":     {"type": "object", "enabled": False},
    "engine_stats": {"type": "object", "enabled": False},
    "error":      {"type": "text", "index": False},
}

# prism_drop_trace：一次请求 × 每条 URL 一文档，全字段索引供 Kibana 过滤/聚合/全文搜。
_DROP_TRACE_MAPPING = {
    "@timestamp": {"type": "date"},
    "query_id": {"type": "keyword"},
    "query":    {"type": "text"},
    "url":      {"type": "keyword"},
    "title":    {"type": "text"},
    "snippet":  {"type": "text"},
    "engine":   {"type": "keyword"},
    "stage":    {"type": "keyword"},
    "reason":   {"type": "keyword"},
    "score":    {"type": "float"},
}


async def _ensure_index(index: str, mappings: dict) -> bool:
    """Create `index` with the given property mappings if absent.

    If the index already exists, try to PUT any missing fields so dynamic
    mappings are upgraded to explicit keyword/date/etc. types.
    """
    es = await _get_es()
    try:
        exists = await es.indices.exists(index=index)
        if not exists:
            body = {
                "mappings": {"properties": mappings},
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            }
            await es.indices.create(index=index, body=body)
            logger.info("audit: created index %s", index)
            return True

        # Index exists: ensure new fields (e.g. client_ip) have explicit mappings.
        try:
            await es.indices.put_mapping(index=index, properties=mappings)
        except Exception as exc:
            logger.debug("audit: put_mapping for %s skipped: %s", index, exc)
        return True
    except Exception as exc:
        logger.warning("audit: index %s check failed: %s", index, exc)
        return False


async def save_drop_trace(query_id: str, query: str, records: list[dict]) -> None:
    """Fire-and-forget bulk write of per-result drop verdicts to ES. Never blocks.

    每条 URL = 一个 prism_drop_trace 文档（url/title/snippet/stage/reason/score），
    Kibana 直接按 stage/reason 过滤、title/snippet 全文搜、按 stage 聚合"哪层丢最多"。
    """
    if not records:
        return
    try:
        await _ensure_index("prism_drop_trace", _DROP_TRACE_MAPPING)
        es = await _get_es()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        ops: list[dict] = []
        for rec in records:
            ops.append({"index": {"_index": "prism_drop_trace"}})
            ops.append({"@timestamp": ts, "query_id": query_id, "query": query, **rec})
        await es.bulk(operations=ops)
    except Exception as exc:
        logger.warning("audit: failed to write drop_trace: %s", exc)


@dataclass
class StageTiming:
    """One pipeline stage."""

    stage: str = ""
    started_at: float = 0.0
    elapsed_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchAuditRecord:
    """Full audit document for a single search request."""

    query_id: str = ""
    tenant_id: str = ""
    session_id: str = ""
    query: str = ""
    started_at: float = 0.0
    elapsed_ms: int = 0
    stages: list[StageTiming] = field(default_factory=list)
    engines_used: list[str] = field(default_factory=list)
    sub_engines: list[str] = field(default_factory=list)
    engine_stats: dict[str, Any] = field(default_factory=dict)
    result_count: int = 0
    cached: bool = False
    error: str | None = None
    client_ip: str = ""
    api_key: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)


async def save_search_audit(record: SearchAuditRecord) -> None:
    """Fire-and-forget write to ES + Redis dashboard counters. Never blocks the caller."""
    try:
        await _ensure_index("prism_audit", _AUDIT_MAPPING)
        es = await _get_es()
        doc: dict[str, Any] = {
            "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            **asdict(record),
        }
        await es.index(index="prism_audit", document=doc)
    except Exception as exc:
        logger.warning("audit: failed to write record: %s", exc)

    # Push real-time counters to Redis for dashboard
    try:
        from app.services.dashboard import record_search_metrics
        await record_search_metrics(
            tenant_id=record.tenant_id,
            query_id=record.query_id,
            elapsed_ms=record.elapsed_ms,
            result_count=record.result_count,
            cached=record.cached,
            engines_used=record.engines_used,
            sub_engines=record.sub_engines,
            stages=[asdict(s) for s in record.stages],
        )
    except Exception as exc:
        logger.warning("audit: dashboard metrics failed: %s", exc)


async def close_audit_es() -> None:
    """Close ES client on shutdown."""
    global _es_client
    if _es_client is not None:
        await _es_client.close()
        _es_client = None
