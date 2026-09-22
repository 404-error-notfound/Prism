"""Real-time dashboard metrics — aggregated in Redis, served via Admin API.

Design choices
--------------
- Redis is the single source of truth for real-time counters and rolling windows.
- ES (prism_audit) stores raw documents for deep-dive / historical analysis.
- A background APScheduler job cleans up expired keys so Redis memory stays flat.

Redis key layout
----------------
  dashboard:daily:{yyyy-mm-dd}:total
  dashboard:daily:{yyyy-mm-dd}:apikey:{hash16}
  dashboard:daily:{yyyy-mm-dd}:engine:{eng}
  dashboard:daily:{yyyy-mm-dd}:cache_hit
  dashboard:daily:{yyyy-mm-dd}:cache_miss

  dashboard:weekly:{yyyy-ww}:total
  dashboard:weekly:{yyyy-ww}:apikey:{hash16}

  dashboard:monthly:{yyyy-mm}:total
  dashboard:monthly:{yyyy-mm}:apikey:{hash16}

  dashboard:latency:daily:{yyyy-mm-dd}      -> zset(ts, json)
  dashboard:latency:apikey:{hash16}:daily:{yyyy-mm-dd} -> zset(ts, json)

  dashboard:stages:daily:{yyyy-mm-dd}:{stage} -> zset(ts, ms)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import date
from typing import Any

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

# Keep rolling windows this long (seconds)
_DAILY_TTL = 90 * 86400    # 90 days
_WEEKLY_TTL = 365 * 86400  # 1 year
_MONTHLY_TTL = 730 * 86400  # 2 years
_LATENCY_TTL = 7 * 86400    # 7 days


def _api_key_hash(api_key: str) -> str:
    """Short stable hash for Redis key suffix."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _date_parts() -> tuple[str, str, str]:
    """Return (daily_key, weekly_key, monthly_key)."""
    today = date.today()
    weekly = f"{today.year}-{today.isocalendar()[1]:02d}"
    monthly = f"{today.year}-{today.month:02d}"
    return today.isoformat(), weekly, monthly


async def record_search_metrics(
    tenant_id: str,
    query_id: str,
    elapsed_ms: int,
    result_count: int,
    cached: bool,
    engines_used: list[str],
    sub_engines: list[str],
    stages: list[dict[str, Any]],
) -> None:
    """Fire-and-forget: increment counters and push latency samples to Redis.

    In Cluster mode we avoid pipeline (CROSSSLOT risk) and send commands
    individually via asyncio.gather for acceptable throughput.
    """
    if not tenant_id or not tenant_id.strip():
        logger.warning("dashboard: skip metrics for empty tenant_id query_id=%s", query_id)
        return
    try:
        r = get_redis()
        daily, weekly, monthly = _date_parts()
        ts = time.time()

        tasks = []

        def _run(cmd, *a, **kw):
            tasks.append(cmd(*a, **kw))

        # ---- daily counters ----
        _run(r.incr, f"dashboard:daily:{daily}:total")
        _run(r.incr, f"dashboard:daily:{daily}:tenant:{tenant_id}")
        _run(r.expire, f"dashboard:daily:{daily}:total", _DAILY_TTL)
        _run(r.expire, f"dashboard:daily:{daily}:tenant:{tenant_id}", _DAILY_TTL)

        # ---- weekly counters ----
        _run(r.incr, f"dashboard:weekly:{weekly}:total")
        _run(r.incr, f"dashboard:weekly:{weekly}:tenant:{tenant_id}")
        _run(r.expire, f"dashboard:weekly:{weekly}:total", _WEEKLY_TTL)
        _run(r.expire, f"dashboard:weekly:{weekly}:tenant:{tenant_id}", _WEEKLY_TTL)

        # ---- monthly counters ----
        _run(r.incr, f"dashboard:monthly:{monthly}:total")
        _run(r.incr, f"dashboard:monthly:{monthly}:tenant:{tenant_id}")
        _run(r.expire, f"dashboard:monthly:{monthly}:total", _MONTHLY_TTL)
        _run(r.expire, f"dashboard:monthly:{monthly}:tenant:{tenant_id}", _MONTHLY_TTL)

        # ---- engine counters (sub-engine level) ----
        for eng in sub_engines:
            k = f"dashboard:daily:{daily}:engine:{eng}"
            _run(r.incr, k)
            _run(r.expire, k, _DAILY_TTL)

        # ---- cache hit / miss ----
        if cached:
            k = f"dashboard:daily:{daily}:cache_hit"
            _run(r.incr, k)
            _run(r.expire, k, _DAILY_TTL)
        else:
            k = f"dashboard:daily:{daily}:cache_miss"
            _run(r.incr, k)
            _run(r.expire, k, _DAILY_TTL)

        # ---- latency samples (zset) ----
        latency_member = json.dumps({"ms": elapsed_ms, "qid": query_id, "tenant_id": tenant_id}, ensure_ascii=False)
        k_lat = f"dashboard:latency:daily:{daily}"
        _run(r.zadd, k_lat, {latency_member: ts})
        _run(r.expire, k_lat, _LATENCY_TTL)

        k_lat_tenant = f"dashboard:latency:tenant:{tenant_id}:daily:{daily}"
        _run(r.zadd, k_lat_tenant, {latency_member: ts})
        _run(r.expire, k_lat_tenant, _LATENCY_TTL)

        # ---- stage latency samples ----
        for st in stages:
            stage_name = st.get("stage", "unknown")
            stage_ms = st.get("elapsed_ms", 0)
            k_stage = f"dashboard:stages:daily:{daily}:{stage_name}"
            _run(r.zadd, k_stage, {json.dumps({"ms": stage_ms, "qid": query_id}): ts})
            _run(r.expire, k_stage, _LATENCY_TTL)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                logger.warning("dashboard: task %d failed: %s", idx, res)
    except Exception as exc:
        logger.warning("dashboard: record metrics failed: %s", exc)



