"""Admin endpoints — API key management, denylist, circuit breaker reset, audit log."""

from __future__ import annotations

import json
import logging
import time
from statistics import mean

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.auth import TenantConfig, authenticate, create_api_key
from app.core.redis import get_redis
from app.engines.searxng import build_engines

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# Valid domain tier names (must match Redis key naming in ranker/commercial/fetcher)
_VALID_TIERS: set[str] = {
    "T1", "academic", "news", "commercial", "robots_skip",
}
_REDIS_KEY_MAP: dict[str, str] = {
    "T1": "domain:tier:T1",
    "academic": "domain:tier:academic",
    "news": "domain:tier:news",
    "commercial": "domain:tier:commercial",
    "robots_skip": "domain:tier:robots_skip",
}


def _require_admin(tenant: TenantConfig = Depends(authenticate)) -> TenantConfig:
    if tenant.tenant_id != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return tenant


def _tier_key(tier: str) -> str:
    if tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tier '{tier}'. Valid: {', '.join(sorted(_VALID_TIERS))}",
        )
    return _REDIS_KEY_MAP[tier]


class CreateKeyRequest(BaseModel):
    tenant_id: str
    quota_per_day: int = 1000
    rate_limit_qps: int = 5
    allow_browser: bool = False


class CreateKeyResponse(BaseModel):
    api_key: str
    tenant_id: str


@router.post("/keys", response_model=CreateKeyResponse)
async def create_key(
    body: CreateKeyRequest,
    _: TenantConfig = Depends(_require_admin),
) -> CreateKeyResponse:
    from app.core.auth import TenantConfig as TC

    cfg = TC(
        tenant_id=body.tenant_id,
        quota_per_day=body.quota_per_day,
        rate_limit_qps=body.rate_limit_qps,
        allow_browser=body.allow_browser,
    )
    key = await create_api_key(body.tenant_id, cfg)
    return CreateKeyResponse(api_key=key, tenant_id=body.tenant_id)


# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------


class DenylistUpdateRequest(BaseModel):
    domains: list[str]
    action: str  # "add" | "remove"


@router.post("/denylist")
async def update_denylist(
    body: DenylistUpdateRequest,
    _: TenantConfig = Depends(_require_admin),
):
    r = get_redis()
    if body.action == "add":
        await r.sadd("denylist:domains", *body.domains)
    elif body.action == "remove":
        await r.srem("denylist:domains", *body.domains)
    else:
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")
    return {"ok": True}


@router.get("/denylist")
async def list_denylist(
    _: TenantConfig = Depends(_require_admin),
):
    r = get_redis()
    domains = await r.smembers("denylist:domains")
    return {"domains": sorted(domains)}


# ---------------------------------------------------------------------------
# Circuit breaker reset
# ---------------------------------------------------------------------------


@router.post("/circuit/reset/{engine}")
async def reset_circuit(
    engine: str,
    _: TenantConfig = Depends(_require_admin),
):
    r = get_redis()
    await r.delete(f"circuit:{engine}")
    return {"ok": True, "engine": engine}


# ---------------------------------------------------------------------------
# Tenant config / quota
# ---------------------------------------------------------------------------


class TenantConfigResponse(BaseModel):
    tenant_id: str
    quota_per_day: int
    rate_limit_qps: int
    remaining_quota: int


@router.get("/tenants/{tenant_id}", response_model=TenantConfigResponse)
async def get_tenant_config(
    tenant_id: str,
    _: TenantConfig = Depends(_require_admin),
):
    r = get_redis()
    key = f"tenant:{tenant_id}:config"
    cfg = await r.hgetall(key)
    if not cfg:
        raise HTTPException(status_code=404, detail="Tenant not found")

    quota = int(cfg.get("quota_per_day", 1000))
    qps = int(cfg.get("rate_limit_qps", 5))
    today = time.strftime("%Y-%m-%d")
    used = int(await r.get(f"tenant:{tenant_id}:quota:{today}") or 0)
    return TenantConfigResponse(
        tenant_id=tenant_id,
        quota_per_day=quota,
        rate_limit_qps=qps,
        remaining_quota=max(0, quota - used),
    )


# ---------------------------------------------------------------------------
# Engine stats (latency + survival)
# ---------------------------------------------------------------------------


class EngineStat(BaseModel):
    engine: str
    last_latency_ms: int | None = None
    last_query_id: str | None = None
    avg_latency_ms: int | None = None
    avg_survival_rate: float | None = None
    avg_rank: float | None = None
    circuit_open: bool = False


class EngineStatsResponse(BaseModel):
    engines: list[str]
    stats: dict[str, EngineStat]


@router.get("/engines", response_model=EngineStatsResponse)
async def list_engine_stats(
    _: TenantConfig = Depends(_require_admin),
) -> EngineStatsResponse:
    r = get_redis()
    # 引擎清单与拓扑唯一来源（分片 = 各 searxng/<引擎> 片，非分片 = 单一 searxng）
    engine_names = [*build_engines(), "ddgs", "serper", "dataforseo", "volcengine", "tavily"]
    stats: dict[str, EngineStat] = {}

    # Circuit breaker state
    for eng in engine_names:
        cb = await r.get(f"circuit:{eng}")
        stats[eng] = EngineStat(engine=eng, circuit_open=bool(cb))

    # Latency records (last 500 per engine)
    cutoff = time.time() - 3600  # last hour
    for eng in engine_names:
        raw_records = await r.lrange(f"dashboard:engine_latency:{eng}", 0, -1)
        latencies = []
        last_qid = None
        for raw in raw_records:
            try:
                rec = json.loads(raw)
                if rec.get("ts", 0) < cutoff:
                    continue
                latencies.append(rec.get("latency_ms", 0))
                if rec.get("query_id"):
                    last_qid = rec["query_id"]
            except Exception:
                continue
        if latencies:
            stats[eng].last_latency_ms = latencies[0]
            stats[eng].avg_latency_ms = int(mean(latencies))
        if last_qid:
            stats[eng].last_query_id = last_qid

    # Aggregate survival_rate & avg_rank from engine_stats list
    stats_raw = await r.lrange("dashboard:engine_stats", 0, -1)
    survival_sums: dict[str, list[float]] = {}
    rank_sums: dict[str, list[float]] = {}
    last_qid_from_stats: dict[str, str | None] = {}
    for raw in stats_raw:
        try:
            rec = json.loads(raw)
            if rec.get("ts", 0) < cutoff:
                continue
            qid = rec.get("query_id")
            for eng, s in rec.get("engine_stats", {}).items():
                survival_sums.setdefault(eng, []).append(s.get("survival_rate", 0.0))
                rank_sums.setdefault(eng, []).append(s.get("avg_rank", 0.0))
                if qid:
                    last_qid_from_stats[eng] = qid
        except Exception:
            continue

    for eng, rates in survival_sums.items():
        if eng in stats:
            stats[eng].avg_survival_rate = round(mean(rates), 3)
    for eng, ranks in rank_sums.items():
        if eng in stats:
            stats[eng].avg_rank = round(mean(ranks), 1)

    # Merge last_query_id from engine_stats (takes precedence if latency record lacks it)
    for eng, qid in last_qid_from_stats.items():
        if eng in stats and qid:
            stats[eng].last_query_id = qid

    # Ensure at least empty stats for known engines even if no data
    all_engines = sorted(set(engine_names) | set(survival_sums.keys()))
    return EngineStatsResponse(engines=all_engines, stats=stats)





# ---------------------------------------------------------------------------
# Domain tiers
# ---------------------------------------------------------------------------

class DomainTierListResponse(BaseModel):
    tier: str
    domains: list[str]
    count: int


class DomainTierModifyRequest(BaseModel):
    domains: list[str]


@router.get("/domain-tiers/{tier}", response_model=DomainTierListResponse)
async def list_domain_tier(
    tier: str,
    _: TenantConfig = Depends(_require_admin),
) -> DomainTierListResponse:
    """List all domains in a given tier."""
    key = _tier_key(tier)
    r = get_redis()
    members = await r.smembers(key)
    domains = sorted(
        d.decode() if isinstance(d, bytes) else d for d in members
    )
    return DomainTierListResponse(tier=tier, domains=domains, count=len(domains))


@router.post("/domain-tiers/{tier}", status_code=status.HTTP_204_NO_CONTENT)
async def add_to_domain_tier(
    tier: str,
    body: DomainTierModifyRequest,
    _: TenantConfig = Depends(_require_admin),
) -> None:
    """Add one or more domains to a tier."""
    key = _tier_key(tier)
    r = get_redis()
    added = await r.sadd(key, *body.domains)
    logger.info("domain_tier.added tier=%s count=%d", tier, added)


@router.delete("/domain-tiers/{tier}/{domain}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_domain_tier(
    tier: str,
    domain: str,
    _: TenantConfig = Depends(_require_admin),
) -> None:
    """Remove a single domain from a tier."""
    key = _tier_key(tier)
    r = get_redis()
    removed = await r.srem(key, domain)
    logger.info("domain_tier.removed tier=%s domain=%s removed=%d", tier, domain, removed)


@router.post("/domain-tiers/{tier}/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_domain_tier(
    tier: str,
    _: TenantConfig = Depends(_require_admin),
) -> None:
    """Reset a tier to its built-in default set (replaces current Redis contents)."""
    _DEFAULT_TIER_DOMAINS: dict[str, list[str]] = {
        "T1": [
            "github.com", "stackoverflow.com", "docs.python.org", "arxiv.org",
            "wikipedia.org", "developer.mozilla.org", "docs.microsoft.com",
            "developer.android.com", "developer.apple.com",
            "zhihu.com", "juejin.cn", "csdn.net",
            "gov.cn", "edu.cn",
            "python.org", "nodejs.org", "rust-lang.org",
            "medium.com", "dev.to", "hackernews.com",
        ],
        "academic": [
            "arxiv.org", "scholar.google.com", "ieee.org", "acm.org",
            "nature.com", "science.org", "springer.com", "sciencedirect.com",
            "pubmed.ncbi.nlm.nih.gov", "edu.cn",
        ],
        "news": [
            "xinhuanet.com", "people.com.cn", "cctv.com", "thepaper.cn",
            "caixin.com", "yicai.com", "sina.com.cn", "163.com", "ifeng.com",
            "reuters.com", "bloomberg.com", "bbc.com", "nytimes.com",
        ],
        "commercial": [
            "jd.com", "tmall.com", "taobao.com", "pinduoduo.com", "suning.com",
            "amazon.com", "amazon.cn", "ebay.com", "aliexpress.com",
            "shopify.com", "shopee.com", "lazada.com",
        ],
        "robots_skip": [
            "github.com", "stackoverflow.com", "arxiv.org", "wikipedia.org",
            "developer.mozilla.org", "docs.microsoft.com", "docs.python.org",
            "zhihu.com", "juejin.cn", "csdn.net", "cnblogs.com",
            "medium.com", "dev.to", "hackernews.com", "reddit.com",
            "gov.cn", "edu.cn",
        ],
    }

    if tier not in _DEFAULT_TIER_DOMAINS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No default config for tier '{tier}'",
        )

    key = _REDIS_KEY_MAP[tier]
    r = get_redis()
    await r.delete(key)
    domains = _DEFAULT_TIER_DOMAINS[tier]
    if domains:
        await r.sadd(key, *domains)
    logger.info("domain_tier.reset tier=%s domains=%d", tier, len(domains))
