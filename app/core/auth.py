"""API Key authentication + per-tenant quota management.

Key lifecycle
-------------
1. `create_api_key()` generates a random key, stores sha256(key) in Redis.
2. Every request validates via `authenticate()` which hashes the incoming key and looks it up.
3. Tenant config is stored as a Redis Hash under `tenant:{tenant_id}:config`.
4. Quota enforced via a daily counter `tenant:{tenant_id}:quota:{date}` (TTL 25h).
5. Rate limiting via sliding-window ZSET `tenant:{tenant_id}:ratelimit`.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import date

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "apikey:"
_TENANT_CONFIG_PREFIX = "tenant:"


# ---------------------------------------------------------------------------
# Data model (thin dict wrapper — no ORM needed at Stage 1)
# ---------------------------------------------------------------------------

class TenantConfig:
    __slots__ = (
        "tenant_id", "api_key", "quota_per_day", "rate_limit_qps",
        "allowed_engines", "allow_browser", "allow_tavily",
        "commercial_filter_strength",
    )

    def __init__(
        self,
        tenant_id: str,
        api_key: str = "",
        quota_per_day: int = 0,
        rate_limit_qps: int = 0,
        allowed_engines: str = "",
        allow_browser: bool = False,
        allow_tavily: bool = False,
        commercial_filter_strength: float = 0.5,
    ) -> None:
        self.tenant_id = tenant_id
        self.api_key = api_key
        self.quota_per_day = quota_per_day or settings.default_quota_per_day
        self.rate_limit_qps = rate_limit_qps or settings.default_rate_limit_qps
        self.allowed_engines = allowed_engines
        self.allow_browser = allow_browser
        self.allow_tavily = allow_tavily
        self.commercial_filter_strength = commercial_filter_strength

    @classmethod
    def from_redis_hash(cls, tenant_id: str, data: dict[str, str]) -> "TenantConfig":
        return cls(
            tenant_id=tenant_id,
            quota_per_day=int(data.get("quota_per_day", 0)),
            rate_limit_qps=int(data.get("rate_limit_qps", 0)),
            allowed_engines=data.get("allowed_engines", ""),
            allow_browser=data.get("allow_browser", "0") == "1",
            allow_tavily=data.get("allow_tavily", "0") == "1",
            commercial_filter_strength=float(data.get("commercial_filter_strength", 0.5)),
        )

    def to_redis_hash(self) -> dict[str, str]:
        return {
            "quota_per_day": str(self.quota_per_day),
            "rate_limit_qps": str(self.rate_limit_qps),
            "allowed_engines": self.allowed_engines,
            "allow_browser": "1" if self.allow_browser else "0",
            "allow_tavily": "1" if self.allow_tavily else "0",
            "commercial_filter_strength": str(self.commercial_filter_strength),
        }


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _hash_key(raw_key: str) -> str:
    """sha256(raw_key) — stored in Redis, never the plaintext key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _tenant_config_key(tenant_id: str) -> str:
    return f"{_TENANT_CONFIG_PREFIX}{tenant_id}:config"


def _quota_key(tenant_id: str) -> str:
    return f"{_TENANT_CONFIG_PREFIX}{tenant_id}:quota:{date.today().isoformat()}"


def _ratelimit_key(tenant_id: str) -> str:
    return f"{_TENANT_CONFIG_PREFIX}{tenant_id}:ratelimit"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def create_api_key(
    tenant_id: str,
    config: TenantConfig | None = None,
) -> str:
    """Generate a new API key for *tenant_id*. Returns the plaintext key (shown once)."""
    raw_key = "prism_" + os.urandom(24).hex()
    key_hash = _hash_key(raw_key)

    r = get_redis()
    pipe = r.pipeline()
    pipe.set(f"{_KEY_PREFIX}{key_hash}", tenant_id)

    cfg = config or TenantConfig(tenant_id=tenant_id)
    pipe.hset(_tenant_config_key(tenant_id), mapping=cfg.to_redis_hash())
    await pipe.execute()

    logger.info("Created API key for tenant=%s", tenant_id)
    return raw_key


async def get_tenant_config(tenant_id: str) -> TenantConfig:
    r = get_redis()
    data = await r.hgetall(_tenant_config_key(tenant_id))
    if not data:
        # Auto-create default config for unknown tenants (permissive for dev)
        cfg = TenantConfig(tenant_id=tenant_id)
        await r.hset(_tenant_config_key(tenant_id), mapping=cfg.to_redis_hash())
        return cfg
    return TenantConfig.from_redis_hash(tenant_id, data)


# ---------------------------------------------------------------------------
# Authentication + quota enforcement
# ---------------------------------------------------------------------------

async def authenticate(request: Request) -> TenantConfig:
    """FastAPI dependency — validates API key, enforces quota + rate limit."""
    raw_key = _extract_key(request)
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": -32001, "message": "AuthenticationFailed: missing API key"},
        )

    key_hash = _hash_key(raw_key)
    r = get_redis()
    tenant_id = await r.get(f"{_KEY_PREFIX}{key_hash}")

    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": -32001, "message": "AuthenticationFailed: invalid API key"},
        )

    config = await get_tenant_config(tenant_id)
    config.api_key = raw_key

    # Daily quota check
    quota_key = _quota_key(tenant_id)
    current = await r.get(quota_key)
    used = int(current) if current else 0
    if used >= config.quota_per_day:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "86400"},
            detail={"code": -32003, "message": "QuotaExceeded"},
        )

    # Sliding-window rate limit (QPS)
    await _check_rate_limit(r, tenant_id, config.rate_limit_qps)

    # Increment quota counter (TTL 25h to survive across midnight)
    pipe = r.pipeline()
    pipe.incr(quota_key)
    pipe.expire(quota_key, 25 * 3600)
    await pipe.execute()

    return config


def _extract_key(request: Request) -> str:
    """Accept key from Authorization header or ?api_key= query param."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.query_params.get("api_key", "")


async def _check_rate_limit(r, tenant_id: str, max_qps: int) -> None:
    """Sliding-window rate limit using sorted set (score = timestamp ms)."""
    key = _ratelimit_key(tenant_id)
    now_ms = int(time.time() * 1000)
    window_ms = 1000  # 1 second window

    pipe = r.pipeline()
    pipe.zremrangebyscore(key, 0, now_ms - window_ms)
    pipe.zadd(key, {str(now_ms): now_ms})
    pipe.zcard(key)
    pipe.expire(key, 10)
    results = await pipe.execute()

    count = results[2]
    if count > max_qps:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "1"},
            detail={"code": -32002, "message": "RateLimited"},
        )


# ---------------------------------------------------------------------------
# Bootstrap (called once on first start)
# ---------------------------------------------------------------------------

async def bootstrap_admin_key() -> None:
    """If ADMIN_BOOTSTRAP_KEY is set and not yet in Redis, register it."""
    if not settings.admin_bootstrap_key:
        return

    raw_key = settings.admin_bootstrap_key
    key_hash = _hash_key(raw_key)
    r = get_redis()

    exists = await r.exists(f"{_KEY_PREFIX}{key_hash}")
    if not exists:
        cfg = TenantConfig(
            tenant_id="admin",
            quota_per_day=999999,
            rate_limit_qps=100,
            allow_browser=True,
            allow_tavily=True,
        )
        await r.set(f"{_KEY_PREFIX}{key_hash}", "admin")
        await r.hset(_tenant_config_key("admin"), mapping=cfg.to_redis_hash())
        logger.info("Bootstrap admin key registered")


# ---------------------------------------------------------------------------
# HMAC helper (for PII hashing)
# ---------------------------------------------------------------------------

def hmac_hash(value: str) -> str:
    salt = settings.hmac_salt or "prism-default-salt"
    return hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()
