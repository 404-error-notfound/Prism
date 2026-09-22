"""Redis connection — singleton shared across async tasks."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.cluster import RedisCluster

from app.core.config import settings


def _cluster_url_with_password(url: str, password: str) -> str:
    # redis-py 8.x initializes startup nodes from URL before applying kwargs,
    # so CLUSTER SLOTS is sent without auth unless the password is embedded.
    if password and "@" not in url:
        return url.replace("://", f"://:{password}@", 1)
    return url

logger = logging.getLogger(__name__)

_pool: aioredis.ConnectionPool | None = None
_raw_client: aioredis.Redis | RedisCluster | None = None
_prefixed_client: PrefixedRedis | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_standalone_pool() -> aioredis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            password=settings.redis_password or None,
            max_connections=settings.redis_max_conn,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_keepalive=True,
            health_check_interval=30,
            protocol=2,
        )
    return _pool  # type: ignore[return-value]


def _build_raw_client() -> aioredis.Redis | RedisCluster:
    global _raw_client
    if _raw_client is not None:
        return _raw_client
    if settings.redis_cluster_mode:
        _raw_client = RedisCluster.from_url(
            _cluster_url_with_password(settings.redis_url, settings.redis_password),
            password=settings.redis_password or None,
            max_connections=settings.redis_max_conn,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_keepalive=True,
            health_check_interval=30,
            protocol=2,
        )
        logger.info("redis: cluster mode")
    else:
        _raw_client = aioredis.Redis(connection_pool=_get_standalone_pool())
        logger.info("redis: standalone mode")
    return _raw_client


# ---------------------------------------------------------------------------
# Key-prefix proxy
# ---------------------------------------------------------------------------

class _PrefixBase:
    """Shared prefix helper for PrefixedRedis and _PrefixedPipeline."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def _pk(self, key: str) -> str:
        return f"{self._prefix}{key}"


class _PrefixedPipeline(_PrefixBase):
    """Pipeline wrapper that prepends the namespace prefix to every key."""

    def __init__(self, pipe: Any, prefix: str) -> None:
        super().__init__(prefix)
        self._pipe = pipe

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pipe, name)

    # single-key
    def get(self, key, **kw):        self._pipe.get(self._pk(key), **kw);          return self
    def set(self, key, val, **kw):   self._pipe.set(self._pk(key), val, **kw);     return self
    def setex(self, key, t, val):    self._pipe.setex(self._pk(key), t, val);      return self
    def setnx(self, key, val):       self._pipe.setnx(self._pk(key), val);         return self
    def delete(self, *keys):         self._pipe.delete(*[self._pk(k) for k in keys]); return self
    def exists(self, *keys):         self._pipe.exists(*[self._pk(k) for k in keys]); return self
    def expire(self, key, t):        self._pipe.expire(self._pk(key), t);          return self
    def ttl(self, key):              self._pipe.ttl(self._pk(key));                return self
    def incr(self, key, **kw):       self._pipe.incr(self._pk(key), **kw);        return self
    def incrby(self, key, amt):      self._pipe.incrby(self._pk(key), amt);       return self
    def decr(self, key, **kw):       self._pipe.decr(self._pk(key), **kw);        return self
    # hash
    def hget(self, name, field):     self._pipe.hget(self._pk(name), field);      return self
    def hset(self, name, key=None, value=None, mapping=None, **kw):
        self._pipe.hset(self._pk(name), key=key, value=value, mapping=mapping, **kw); return self
    def hmset(self, name, mapping):  self._pipe.hmset(self._pk(name), mapping);   return self
    def hmget(self, name, *fields):  self._pipe.hmget(self._pk(name), *fields);   return self
    def hgetall(self, name):         self._pipe.hgetall(self._pk(name));          return self
    def hdel(self, name, *fields):   self._pipe.hdel(self._pk(name), *fields);    return self
    def hexists(self, name, key):    self._pipe.hexists(self._pk(name), key);     return self
    # sorted set
    def zadd(self, name, mapping, **kw):     self._pipe.zadd(self._pk(name), mapping, **kw); return self
    def zrange(self, name, *a, **kw):        self._pipe.zrange(self._pk(name), *a, **kw);    return self
    def zrangebyscore(self, name, *a, **kw): self._pipe.zrangebyscore(self._pk(name), *a, **kw); return self
    def zrem(self, name, *vals):             self._pipe.zrem(self._pk(name), *vals);          return self
    def zcard(self, name):                   self._pipe.zcard(self._pk(name));                return self
    def zscore(self, name, val):             self._pipe.zscore(self._pk(name), val);          return self
    def zremrangebyscore(self, name, *a, **kw): self._pipe.zremrangebyscore(self._pk(name), *a, **kw); return self
    # set
    def sadd(self, name, *vals):     self._pipe.sadd(self._pk(name), *vals);      return self
    def smembers(self, name):        self._pipe.smembers(self._pk(name));         return self
    def srem(self, name, *vals):     self._pipe.srem(self._pk(name), *vals);      return self
    def sismember(self, name, val):  self._pipe.sismember(self._pk(name), val);   return self
    # list
    def lrange(self, name, *a):      self._pipe.lrange(self._pk(name), *a);       return self
    def lpush(self, name, *vals):    self._pipe.lpush(self._pk(name), *vals);     return self
    def rpush(self, name, *vals):    self._pipe.rpush(self._pk(name), *vals);     return self
    def llen(self, name):            self._pipe.llen(self._pk(name));             return self
    def ltrim(self, name, s, e):     self._pipe.ltrim(self._pk(name), s, e);      return self

    async def execute(self, **kw):   return await self._pipe.execute(**kw)
    async def __aenter__(self):      await self._pipe.__aenter__(); return self
    async def __aexit__(self, *a):   return await self._pipe.__aexit__(*a)


class PrefixedRedis(_PrefixBase):
    """Thin proxy that prepends a namespace prefix to every Redis key."""

    def __init__(self, client: aioredis.Redis | RedisCluster, prefix: str) -> None:
        super().__init__(prefix)
        self._client = client
        self._cluster_mode = isinstance(client, RedisCluster)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    # single-key
    async def get(self, key, **kw):       return await self._client.get(self._pk(key), **kw)
    async def set(self, key, val, **kw):  return await self._client.set(self._pk(key), val, **kw)
    async def setex(self, key, t, val):   return await self._client.setex(self._pk(key), t, val)
    async def setnx(self, key, val):      return await self._client.setnx(self._pk(key), val)
    async def exists(self, *keys):        return await self._client.exists(*[self._pk(k) for k in keys])
    async def delete(self, *keys):        return await self._client.delete(*[self._pk(k) for k in keys])
    async def expire(self, key, t):       return await self._client.expire(self._pk(key), t)
    async def ttl(self, key):             return await self._client.ttl(self._pk(key))
    async def incr(self, key, **kw):      return await self._client.incr(self._pk(key), **kw)
    async def incrby(self, key, amt):     return await self._client.incrby(self._pk(key), amt)
    async def decr(self, key, **kw):      return await self._client.decr(self._pk(key), **kw)
    # hash
    async def hget(self, name, field):    return await self._client.hget(self._pk(name), field)
    async def hset(self, name, key=None, value=None, mapping=None, **kw):
        return await self._client.hset(self._pk(name), key=key, value=value, mapping=mapping, **kw)
    async def hmset(self, name, mapping): return await self._client.hmset(self._pk(name), mapping)
    async def hmget(self, name, *fields): return await self._client.hmget(self._pk(name), *fields)
    async def hgetall(self, name):        return await self._client.hgetall(self._pk(name))
    async def hdel(self, name, *fields):  return await self._client.hdel(self._pk(name), *fields)
    async def hexists(self, name, key):   return await self._client.hexists(self._pk(name), key)
    # sorted set
    async def zadd(self, name, mapping, **kw):      return await self._client.zadd(self._pk(name), mapping, **kw)
    async def zrange(self, name, *a, **kw):         return await self._client.zrange(self._pk(name), *a, **kw)
    async def zrangebyscore(self, name, *a, **kw):  return await self._client.zrangebyscore(self._pk(name), *a, **kw)
    async def zrem(self, name, *vals):              return await self._client.zrem(self._pk(name), *vals)
    async def zcard(self, name):                    return await self._client.zcard(self._pk(name))
    async def zscore(self, name, val):              return await self._client.zscore(self._pk(name), val)
    async def zremrangebyscore(self, name, *a, **kw): return await self._client.zremrangebyscore(self._pk(name), *a, **kw)
    # set
    async def sadd(self, name, *vals):    return await self._client.sadd(self._pk(name), *vals)
    async def smembers(self, name):       return await self._client.smembers(self._pk(name))
    async def srem(self, name, *vals):    return await self._client.srem(self._pk(name), *vals)
    async def sismember(self, name, val): return await self._client.sismember(self._pk(name), val)
    # list
    async def lrange(self, name, *a):    return await self._client.lrange(self._pk(name), *a)
    async def lpush(self, name, *vals):  return await self._client.lpush(self._pk(name), *vals)
    async def rpush(self, name, *vals):  return await self._client.rpush(self._pk(name), *vals)
    async def llen(self, name):          return await self._client.llen(self._pk(name))
    async def ltrim(self, name, s, e):   return await self._client.ltrim(self._pk(name), s, e)
    # scan
    def scan_iter(self, match=None, **kw):
        prefix = self._prefix
        raw_match = self._pk(match) if match else None
        async def _unwrap():
            async for key in self._client.scan_iter(match=raw_match, **kw):
                if isinstance(key, str) and key.startswith(prefix):
                    yield key[len(prefix):]
                else:
                    yield key
        return _unwrap()
    # pipeline
    def pipeline(self, transaction=True):
        if self._cluster_mode:
            transaction = False
        pipe = self._client.pipeline(transaction=transaction)
        return _PrefixedPipeline(pipe, self._prefix)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class _ClusterPoolMock:
    """Placeholder returned by get_pool() in cluster mode for the health check."""
    _created_connections: int = 0

    def __init__(self, max_connections: int) -> None:
        self.max_connections = max_connections


def get_redis() -> aioredis.Redis | RedisCluster | PrefixedRedis:
    global _prefixed_client
    client = _build_raw_client()
    if settings.redis_key_prefix:
        if _prefixed_client is None:
            _prefixed_client = PrefixedRedis(client, settings.redis_key_prefix)
        return _prefixed_client
    return client


def get_pool() -> aioredis.ConnectionPool:
    """Return the underlying connection pool (standalone only; used by health check)."""
    if settings.redis_cluster_mode:
        return _ClusterPoolMock(settings.redis_max_conn)  # type: ignore[return-value]
    return _get_standalone_pool()


async def ping_redis() -> bool:
    try:
        return await get_redis().ping()
    except Exception as exc:
        logger.warning("redis ping failed: %s", exc)
        return False


# 启动自检专用：带退避地等 Redis 就绪。
# 不要改成给 ping_redis() 本身加重试——它同时是 /health 的依赖，探活必须快且诚实
# （Redis 真挂了要立刻返回 false，不能拖几秒）。
# 为什么启动自检必须重试：cluster 模式下第一次 ping 要先做 CLUSTER SLOTS 拓扑发现，
# socket_connect_timeout=2s；宿主机负载高（vfs 创建容器、torch 冷启动抢 CPU）时
# 这一次很容易输。2026-09-17 生产现场：gateway 冷启动首次探测失败 → on_startup 直接
# sys.exit(1) → 容器退出 → 靠 restart: unless-stopped 拉起才起来（RestartCount=1），
# 部署脚本一度误判为失败。一次网络抖动不该等于一次启动失败。
async def wait_redis_ready(attempts: int = 5, delay_s: float = 2.0) -> bool:
    for i in range(1, attempts + 1):
        if await ping_redis():
            if i > 1:
                logger.warning("redis ready after %d attempts", i)
            return True
        if i < attempts:
            # 探测失败后丢弃已缓存的客户端：cluster 客户端可能停在"拓扑初始化失败"
            # 的坏状态，重建比复用好（close_pool 会清掉 _raw_client/_pool）
            await close_pool()
            await asyncio.sleep(delay_s)
    logger.error("redis not reachable after %d attempts (%.1fs interval)", attempts, delay_s)
    return False


async def close_pool() -> None:
    global _pool, _raw_client, _prefixed_client
    if _raw_client is not None:
        await _raw_client.aclose()
        _raw_client = None
    if _pool is not None:
        await _pool.aclose()
        _pool = None
    _prefixed_client = None


def redis_key(key: str) -> str:
    """Prepend the configured Redis namespace prefix to *key*."""
    prefix = settings.redis_key_prefix
    return f"{prefix}{key}" if prefix else key
