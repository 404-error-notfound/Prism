"""Structured JSON logging setup (ECS-inspired).

Output channels:
  stdout              — text (dev) or JSON (prod)
  logs/prism.log      — JSON always, rotating 20 MB × 10, all levels
  logs/prism.err.log  — JSON always, rotating 20 MB × 5, ERROR+ only
  logs/timing.log     — JSON always, rotating 10 MB × 5, timing records only
  logs/access.log     — JSON always, rotating 20 MB × 5, HTTP access records only
  ES (prism_logs)     — 非阻塞队列 + log_shipper 异步批量写（可选，log_es_enabled）

文件与 ES 两路字段名完全一致（level/logger/msg/service/...），Kibana 与
grep 文件用同一套字段。一条日志 = 一个事件：msg 是人读短句，结构化数据走字段。

Structured fields are extracted from logging ``extra=`` kwargs:
  event={"category": "engine", "type": "search"}
  metrics={"latency_ms": 125, "result_count": 30}
  engine={"name": "searxng", "tier": "tier1"}
  http={"request.method": "POST", "response.status_code": 200}
  client={"ip": "10.0.0.1"}
  error={"type": "TimeoutError", "message": "..."}

Query a full request chain by query_id（一次搜索全链路）or trace_id（HTTP 链路）:
  grep '"query_id":"abc123"' logs/prism.log | jq '{@timestamp,msg,metrics}'
  Kibana: query_id : "abc123"
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import queue
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings

# ---------------------------------------------------------------------------
# Context variables (injected by middleware)
# ---------------------------------------------------------------------------
# 有界队列：超出容量时丢弃新日志而不是无限堆积内存
# 10 000 条 × ~500B ≈ 5MB；ES 宕机期间最多缓存这么多
_ES_QUEUE_MAXSIZE = 10_000
es_log_queue: queue.Queue = queue.Queue(maxsize=_ES_QUEUE_MAXSIZE)

# Per-request trace ID — set by middleware, auto-injected into every log record
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")
client_ip_var: ContextVar[str] = ContextVar("client_ip", default="")
# 搜索请求 ID —— orchestrator 入口生成，全链路埋点自动携带（不再靠手工 extra 透传）
query_id_var: ContextVar[str] = ContextVar("query_id", default="")
# 当前查询词 —— 与 query_id 一起注入；proxy_client 上报 proxy-hub 时随信号
# body 携带，proxyhub_logs 可直接按查询词模糊检索（人可读锚点，补 query_id
# 需要跨系统复制 uuid 的不便）
query_text_var: ContextVar[str] = ContextVar("query_text", default="")
# 当前代理批次 —— proxy_client.ensure_warm 从 proxy-hub /status 读入，
# 使 prism_logs（query_id）与 proxyhub_logs（batch_id）可互相跳转
proxy_batch_var: ContextVar[str] = ContextVar("proxy_batch", default="")

# ---------------------------------------------------------------------------
# Structured extra keys that are hoisted into the JSON document
# ---------------------------------------------------------------------------
_STRUCTURED_KEYS = frozenset(
    ("event", "metrics", "engine", "http", "client", "error")
)
_TIMING_LOGGERS = {
    "app.services.orchestrator",
    "app.services.fetcher",
    "app.services.cache",
    "app.services.ranker",
}
_TIMING_PREFIXES = (
    "search.",
    "engine.",
    "cache.",
    "fetch.",
    "temporal.",
    "scheduler.",
    "signal.",
)

# Dedicated logger name for HTTP access records
ACCESS_LOGGER_NAME = "prism.access"


def _copy_structured(record: logging.LogRecord, doc: dict[str, Any]) -> None:
    """把 extra= 传入的结构化块（event/metrics/engine/...）提取到日志文档。
    JsonFormatter（文件/stdout）与 ESLogHandler（ES 索引）共用，保证两路字段一致。"""
    for key in _STRUCTURED_KEYS:
        if hasattr(record, key):
            doc[key] = getattr(record, key)


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """一行一个 JSON 文档，字段名与 ES 文档（ESLogHandler）完全对齐：
    @timestamp/level/logger/msg/service + trace_id/tenant_id/session_id/query_id
    + 结构化块（event/metrics/engine/http/client/error）。

    @timestamp 用本地时间+时区偏移（如 +08:00），文件里人读友好且无歧义；
    ES 侧是 UTC（Kibana 按浏览器时区显示），两边可直接对照。
    """

    def format(self, record: logging.LogRecord) -> str:
        import json

        doc: dict[str, Any] = {
            "@timestamp": datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": settings.service_name,
            "environment": settings.app_env,
            "host": socket.gethostname(),
            "pid": os.getpid(),
        }

        # Request context（_TraceFilter 已从 ContextVar 注入 record 属性）
        for key in ("trace_id", "tenant_id", "session_id", "query_id", "proxy_batch"):
            val = getattr(record, key, None)
            if val:
                doc[key] = val

        # Structured extra blocks (event, metrics, engine, http, client, error)
        _copy_structured(record, doc)

        # Exception stack traces
        if record.exc_info:
            doc["exc"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(doc, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
class _TraceFilter(logging.Filter):
    """Inject trace_id (and friends) from ContextVar into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get("")  # type: ignore[attr-defined]
        record.tenant_id = tenant_id_var.get("")  # type: ignore[attr-defined]
        record.session_id = session_id_var.get("")  # type: ignore[attr-defined]
        record.query_id = query_id_var.get("")  # type: ignore[attr-defined]
        record.proxy_batch = proxy_batch_var.get("")  # type: ignore[attr-defined]
        return True


class _TimingFilter(logging.Filter):
    """Pass only timing-relevant log records to timing.log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name in _TIMING_LOGGERS:
            return True
        msg = record.getMessage()
        return any(msg.startswith(prefix) for prefix in _TIMING_PREFIXES)


class _ErrorFilter(logging.Filter):
    """Pass only ERROR and above."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


class _AccessFilter(logging.Filter):
    """Pass only access logger records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == ACCESS_LOGGER_NAME


# Playwright 任务被取消时孤儿异常的特征消息（导航/页面被撕裂）
_PLAYWRIGHT_CANCEL_MARKERS = ("net::ERR_ABORTED", "frame was detached", "Target closed")


class _PlaywrightOrphanFilter(logging.Filter):
    """Drop asyncio 'Future exception was never retrieved' noise from Playwright.

    批量抓取超预算时 task.cancel() 会撕裂进行中的 page.goto，Playwright 内部
    Future 的异常无人接收，asyncio 在 GC 时记一条 ERROR。该 URL 的抓取失败
    已在 fetcher 里降级处理，这条日志纯属噪音。只压取消特征的异常，
    Playwright 其他报错照常放行。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.exc_info:
            return True
        exc = record.exc_info[1]
        if not getattr(type(exc), "__module__", "").startswith("playwright"):
            return True
        if not any(m in str(exc) for m in _PLAYWRIGHT_CANCEL_MARKERS):
            return True
        # asyncio 对孤儿 Future/Task 分别用 "Future/Task exception was never retrieved"
        return "exception was never retrieved" not in record.getMessage()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_file_handler(
    path: str,
    max_bytes: int,
    backup_count: int,
    extra_filter: logging.Filter | None = None,
    level: int | None = None,
) -> logging.handlers.RotatingFileHandler:
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_TraceFilter())
    if level is not None:
        handler.setLevel(level)
    if extra_filter:
        handler.addFilter(extra_filter)
    return handler


def get_access_logger() -> logging.Logger:
    """Return the dedicated HTTP access logger."""
    return logging.getLogger(ACCESS_LOGGER_NAME)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
class ESLogHandler(logging.Handler):
    """非阻塞 ES 日志 handler：将结构化记录放入队列，由 log_shipper 异步批量写入。"""

    def __init__(self, log_queue: queue.SimpleQueue, min_level: int = logging.INFO) -> None:
        super().__init__(min_level)
        self._queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            doc: dict[str, Any] = {
                "@timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "service": settings.service_name,
            }
            for key in ("trace_id", "tenant_id", "session_id", "query_id", "proxy_batch"):
                val = getattr(record, key, None)
                if val:
                    doc[key] = val
            _copy_structured(record, doc)
            if record.exc_info:
                import traceback
                doc["exc"] = "".join(traceback.format_exception(*record.exc_info))
            try:
                self._queue.put_nowait(doc)
            except queue.Full:
                pass  # ES 不可用时丢弃日志，绝不阻塞请求路径
        except Exception:
            self.handleError(record)


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.addFilter(_TraceFilter())
    if settings.app_env == "prod":
        stdout_handler.setFormatter(JsonFormatter())
    else:
        stdout_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
            )
        )

    handlers: list[logging.Handler] = [stdout_handler]

    if settings.log_file_enabled:
        log_dir = settings.log_dir
        os.makedirs(log_dir, exist_ok=True)

        # prism.log — everything
        handlers.append(
            _make_file_handler(
                os.path.join(log_dir, "prism.log"),
                max_bytes=settings.log_max_bytes,
                backup_count=settings.log_backup_count,
            )
        )

        # timing.log — chain timing only (smaller, faster to grep)
        handlers.append(
            _make_file_handler(
                os.path.join(log_dir, "timing.log"),
                max_bytes=settings.log_max_bytes // 2,
                backup_count=5,
                extra_filter=_TimingFilter(),
            )
        )

    # --- ES log handler（非阻塞，异步 shipper 消费） ---
    if settings.log_es_enabled:
        es_min_level = getattr(logging, settings.log_es_min_level.upper(), logging.INFO)
        es_handler = ESLogHandler(es_log_queue, es_min_level)
        es_handler.addFilter(_TraceFilter())  # ES 路也要带上 trace/query 上下文
        handlers.append(es_handler)

    if settings.log_file_enabled:
        # access.log — HTTP access records（原来错误地挂在 ES 开关下，关 ES 就没了）
        handlers.append(
            _make_file_handler(
                os.path.join(log_dir, "access.log"),
                max_bytes=settings.log_max_bytes,
                backup_count=5,
                extra_filter=_AccessFilter(),
            )
        )
        # prism.err.log — ERROR+ 专属（排障时先看这个，不用在 INFO 海里捞）
        handlers.append(
            _make_file_handler(
                os.path.join(log_dir, "prism.err.log"),
                max_bytes=settings.log_max_bytes,
                backup_count=5,
                extra_filter=_ErrorFilter(),
            )
        )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)

    # Quiet noisy third-party libraries
    # elastic_transport 尤其重要：log_shipper 写 ES 的动作会产生它的请求日志，
    # 不静音则"写日志→产生日志→再写"稳态自增殖，把 prism_logs 灌满 HTTP 请求行
    for noisy in (
        "httpx", "httpcore", "uvicorn.access", "sentence_transformers.base.model",
        "elastic_transport", "elastic_transport.node", "elastic_transport.node_pool",
        "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # asyncio 的孤儿 Future 报错中，过滤掉 Playwright 导航被取消产生的噪音
    logging.getLogger("asyncio").addFilter(_PlaywrightOrphanFilter())
