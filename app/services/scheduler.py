"""APScheduler async jobs.

Jobs:
  1. reputation_decay  — monthly: multiply all reputation good/bad × 0.9
  2. engine_health     — every 5 min: probe each engine, log unhealthy ones
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio.cluster import RedisCluster

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Job 1: reputation decay
# ---------------------------------------------------------------------------

async def _decay_reputation() -> None:
    """Multiply all domain reputation counters by 0.9 (monthly decay)."""
    r = get_redis()
    decayed = 0

    # 适配 Redis Cluster：分别扫描每个主节点
    if isinstance(r, RedisCluster):
        try:
            nodes = r.get_primaries()
        except AttributeError:
            nodes = [r]
    else:
        nodes = [r]

    for node in nodes:
        cursor = 0
        while True:
            cursor, keys = await node.scan(cursor, match="reputation:domain:*", count=200)
            for key in keys:
                data = await node.hgetall(key)
                if not data:
                    continue
                good = float(data.get("good", 0))
                bad = float(data.get("bad", 0))
                if good == 0 and bad == 0:
                    continue
                pipe = node.pipeline()
                pipe.hset(key, "good", round(good * 0.9, 4))
                pipe.hset(key, "bad", round(bad * 0.9, 4))
                await pipe.execute()
                decayed += 1
            if cursor == 0:
                break

    logger.info("scheduler.decay_done domains_decayed=%d", decayed)


# ---------------------------------------------------------------------------
# Job 2: engine health probe
# ---------------------------------------------------------------------------

async def _probe_engines() -> None:
    """Health-check all configured engines; log warnings for unhealthy ones."""
    from app.engines.searxng import build_engines
    from app.engines.ddgs import DDGSEngine

    engines = [*build_engines().values(), DDGSEngine()]
    for engine in engines:
        try:
            ok = await engine.health_check()
            if not ok:
                logger.warning("scheduler.probe engine=%s status=unhealthy", engine.name)
            else:
                logger.debug("scheduler.probe engine=%s status=ok", engine.name)
        except Exception as exc:
            logger.warning("scheduler.probe engine=%s error=%s", engine.name, exc)


# ---------------------------------------------------------------------------
# Public setup
# ---------------------------------------------------------------------------

def setup_scheduler() -> AsyncIOScheduler:
    global _scheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    scheduler.add_job(
        _decay_reputation,
        trigger="interval",
        days=30,
        id="reputation_decay",
        replace_existing=True,
    )
    scheduler.add_job(
        _probe_engines,
        trigger="interval",
        minutes=5,
        id="engine_health",
        replace_existing=True,
    )

    _scheduler = scheduler
    return scheduler


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler
