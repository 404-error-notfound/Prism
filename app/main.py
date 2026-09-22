"""Prism — FastAPI application entry point.

Startup sequence:
  1. Configure logging
  2. Connect Redis (validate connection)
  3. Bootstrap admin API key (if ADMIN_BOOTSTRAP_KEY is set)
  4. Register routers (health, search, feedback, admin, metrics)
  5. Mount MCP SSE transport (if transport=http)
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from app.core.config import settings
from app.core.logging import (
    setup_logging,
    tenant_id_var,
    trace_id_var,
    client_ip_var,
    get_access_logger,
)
import time

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Prism Search Gateway",
    version="0.1.0",
    description="Self-hosted LLM web search MCP server",
    docs_url="/docs" if settings.is_dev else None,
    redoc_url=None,
)

# ---------------------------------------------------------------------------
# Trace ID middleware — generates/propagates X-Trace-Id per request
# ---------------------------------------------------------------------------

import uuid  # noqa: E402

def _get_client_ip(request: Request) -> str | None:
    """Return the real client IP, honouring reverse-proxy headers."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For can be a comma-separated list; the first is the original client.
        return forwarded.split(",")[0].strip() or None
    real_ip = request.headers.get("X-Real-Ip")
    if real_ip:
        return real_ip.strip() or None
    return request.client.host if request.client else None


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    tid = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
    token_trace = trace_id_var.set(tid)
    token_ip = client_ip_var.set(_get_client_ip(request) or "")
    try:
        response = await call_next(request)
    finally:
        client_ip_var.reset(token_ip)
        trace_id_var.reset(token_trace)
    response.headers["X-Trace-Id"] = tid
    return response


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """Log every HTTP request with latency and status (ECS-style)."""
    # 高频探测路径不记 access 日志（K8s/网关健康检查与 Prometheus 抓取纯属噪音）
    if request.url.path in ("/health", "/ready", "/metrics"):
        return await call_next(request)
    start = time.monotonic()
    status_code = 500
    tenant_id = ""
    try:
        response = await call_next(request)
        status_code = response.status_code
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        access_logger = get_access_logger()
        access_logger.info(
            "http.access",
            extra={
                "event": {"category": "web", "type": "access"},
                "http": {
                    "request.method": request.method,
                    "request.path": request.url.path,
                    "response.status_code": status_code,
                    "response.duration_ms": duration_ms,
                },
                "client": {
                    "ip": _get_client_ip(request),
                    "user_agent": request.headers.get("user-agent"),
                },
                "tenant_id": tenant_id_var.get(""),
            },
        )
    return response


# CORS (dev-permissive; restrict in prod via env)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_dev else [],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

_log_shipper_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup() -> None:
    global _log_shipper_task
    logger.info("Starting Prism v0.1.0 env=%s transport=%s",
                settings.app_env, settings.mcp_transport)

    # ES 日志 shipper（独立于 hot_news 索引，写 prism_logs）
    if settings.log_es_enabled:
        from app.core.logging import es_log_queue
        from app.services.log_shipper import run as _run_shipper
        _log_shipper_task = asyncio.create_task(
            _run_shipper(
                es_log_queue,
                es_url=settings.es_url,
                es_index=settings.log_es_index,
                batch_size=settings.log_es_batch_size,
                flush_interval=settings.log_es_flush_interval_s,
                es_user=settings.es_user,
                es_password=settings.es_password,
            ),
            name="log-shipper",
        )

    # 代理池由独立 proxy-hub 服务托管（本进程不跑任何代理后台任务）；
    # PROXY_SERVICE_URL 是 proxy-hub 拓扑唯一事实源——未配时上报/唤热/强制刷新
    # 与隧道出网（由它推导，端口换 8101）全部停用，直连出网
    if not settings.proxy_service_url:
        logger.warning(
            "PROXY_SERVICE_URL is empty — proxy-hub 未接入：活跃上报/强制刷新停用，"
            "出网无代理隧道（直连）"
        )

    from app.core.redis import wait_redis_ready
    if not await wait_redis_ready():
        logger.error("Redis not reachable at %s — aborting startup", settings.redis_url)
        # 用 os._exit 而不是 sys.exit：uvicorn 的 lifespan 会吞掉 startup 阶段的
        # SystemExit（uvicorn/lifespan/on.py 里 startup_failed 为真就 return），
        # 进程会以退出码 0 干净退出——监管侧（systemd Restart=on-failure、
        # k8s restartPolicy: OnFailure、按退出码判定的部署门禁）全都看不见这次失败。
        # 生产实测：ExitCode=0 + 日志写 "Application startup failed"，只能靠 compose 的
        # restart: unless-stopped（不看退出码）拉起来。fail-fast 就要 fail 得明白。
        os._exit(1)

    from app.core.auth import bootstrap_admin_key
    await bootstrap_admin_key()

    # 加载活动配置到内存（从 Redis，由 prism-admin 同步）
    from app.services.commercial import load_commercial_config_from_redis
    from app.services.fetcher import load_robots_skip_domains_from_redis
    from app.services.ranker import load_domain_tiers_from_redis
    await asyncio.gather(
        load_robots_skip_domains_from_redis(),
        load_domain_tiers_from_redis(),
        load_commercial_config_from_redis(),
    )

    if settings.mcp_transport == "http":
        _mount_mcp_sse()

    from app.services.scheduler import setup_scheduler
    _scheduler = setup_scheduler()
    _scheduler.start()
    logger.info("Scheduler started with %d jobs", len(_scheduler.get_jobs()))

    # Pre-warm tldextract Public Suffix List so the first real request
    # doesn't pay the ~1-2s disk/network load penalty.
    try:
        import tldextract
        tldextract.extract("example.com")
        logger.info("tldextract PSL warm-up done")
    except Exception as exc:
        logger.warning("tldextract PSL warm-up failed: %s", exc)

    # Pre-warm Crawl4AI browser to avoid ~2-4s cold-start on first detail fetch
    try:
        from app.services.fetcher import _get_crawler
        await _get_crawler()
    except Exception as exc:
        logger.warning("crawl4ai pre-warm failed: %s", exc)

    # Pre-warm embedding / reranker models（在线程池中加载，不阻塞事件循环）
    # 加载耗时：bge-small-zh ~3-5s，bge-reranker-v2-m3 ~5-10s（仅首次）
    if settings.embed_model or settings.reranker_model != "none":
        from app.services.embedder import warm_up as _embedder_warm_up
        await _embedder_warm_up(
            embed_model=settings.embed_model,
            reranker_model=settings.reranker_model,
            num_threads=settings.embed_num_threads,
            max_concurrent=settings.embed_max_concurrent,
        )

    logger.info("Prism ready at http://%s:%s", settings.host, settings.port)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task in (_log_shipper_task,):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    from app.core.redis import close_pool
    from app.engines.searxng import close_client as close_searxng
    from app.services.fetcher import close_crawler, close_http_client
    from app.services.proxy_client import close_client as close_proxy_client
    from app.services.scheduler import get_scheduler
    if sched := get_scheduler():
        sched.shutdown(wait=False)
    await close_pool()
    await close_searxng()
    await close_crawler()
    await close_http_client()
    await close_proxy_client()
    logger.info("Prism shutdown complete")


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

from app.api import admin, feedback, fetch, health, search  # noqa: E402

app.include_router(health.router)
app.include_router(search.router)
app.include_router(feedback.router)
app.include_router(admin.router)
app.include_router(fetch.router)

# Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


# ---------------------------------------------------------------------------
# MCP SSE transport mount
# ---------------------------------------------------------------------------

def _mount_mcp_sse() -> None:
    try:
        from mcp.server.sse import SseServerTransport
        from starlette.routing import Mount, Route

        from app.mcp.server import server

        sse_transport = SseServerTransport("/mcp/messages")

        async def handle_sse(request: Request):
            async with sse_transport.connect_session(
                request.scope, request.receive, request._send  # type: ignore[attr-defined]
            ) as (read, write):
                from mcp.server.models import InitializationOptions
                await server.run(
                    read, write,
                    InitializationOptions(
                        server_name="prism",
                        server_version="0.1.0",
                        capabilities=server.get_capabilities(
                            notification_options=None,
                            experimental_capabilities={},
                        ),
                    ),
                )

        app.router.routes.append(Route("/mcp/sse", endpoint=handle_sse))
        app.router.routes.append(
            Mount("/mcp/messages", app=sse_transport.handle_post_message)
        )
        logger.info("MCP SSE transport mounted at /mcp/sse")
    except Exception as exc:
        logger.warning("Could not mount MCP SSE transport: %s", exc)


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"code": -32000, "message": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if settings.mcp_transport == "stdio":
        import asyncio
        from app.mcp.server import run_stdio
        asyncio.run(run_stdio())
    else:
        import uvicorn
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            # Single worker — scale via container replicas, not workers
            workers=1,
        )


if __name__ == "__main__":
    main()
