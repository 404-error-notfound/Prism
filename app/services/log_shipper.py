"""异步日志投递：从内存队列批量写入 Elasticsearch prism_logs 索引。

索引与 hot_news（热点内容索引）完全独立，只用于存储 Prism 自身的运行日志。

故障隔离保证：
- emit() 非阻塞：队列满时静默丢弃，绝不阻塞请求路径
- ES 宕机时：flush 失败静默记录，shipper 继续运行（消费队列以防堆积）
- shipper task 意外崩溃时：外层守护循环自动重启，最多等待 60s
- ES 连接超时设为 5s，不因 ES 无响应而长时间阻塞事件循环

字段约定（遵循 ECS / Kibana 习惯）：
  @timestamp  — ISO-8601 UTC 时间
  level       — INFO / WARNING / ERROR …
  logger      — Python logger 名（如 app.services.orchestrator）
  msg         — 日志正文
  service     — 固定为 "prism"
  trace_id    — 请求链路 ID（可选）
  tenant_id   — 租户 ID（可选）
  exc         — 异常堆栈（可选）
"""
from __future__ import annotations

import asyncio
import logging
import queue
from typing import Any

logger = logging.getLogger(__name__)

_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "@timestamp": {"type": "date"},
            "level":      {"type": "keyword"},
            "logger":     {"type": "keyword"},
            "msg":        {"type": "text", "analyzer": "ik_max_word"},
            "service":    {"type": "keyword"},
            "trace_id":   {"type": "keyword"},
            "tenant_id":  {"type": "keyword"},
            "session_id": {"type": "keyword"},
            "query_id":   {"type": "keyword"},
            "exc":        {"type": "text", "index": False},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
}

# 连续 flush 失败多少次后开始退避等待
_BACKOFF_AFTER_FAILURES = 3
# 退避等待时间上限（秒）；期间队列继续被消费但记录会被丢弃
_BACKOFF_MAX_S = 60.0


def _make_client(es_url: str, es_user: str, es_password: str) -> Any:
    from elasticsearch import AsyncElasticsearch
    kwargs: dict[str, Any] = {
        "hosts": [es_url],
        "request_timeout": 5,       # 单次请求最多等 5s，ES 挂掉时不长时间阻塞
        "retry_on_timeout": False,  # 超时直接报错，由上层处理，不重复阻塞
        "max_retries": 0,
    }
    if es_user and es_password:
        kwargs["basic_auth"] = (es_user, es_password)
    return AsyncElasticsearch(**kwargs)


async def _ensure_index(es: Any, index: str) -> bool:
    """建立索引（若不存在）。返回 True 表示索引就绪，False 表示 ES 不可用。"""
    try:
        if await es.indices.exists(index=index):
            return True
        try:
            await es.indices.create(index=index, body=_INDEX_MAPPING)
            logger.info("log_shipper: created index %s", index)
        except Exception:
            # IK 分词插件不存在时降级为 standard analyzer
            fallback = dict(_INDEX_MAPPING)
            fallback["mappings"]["properties"]["msg"] = {"type": "text"}
            await es.indices.create(index=index, body=fallback)
            logger.info("log_shipper: created index %s (standard analyzer, no IK)", index)
        return True
    except Exception as e:
        logger.warning("log_shipper: ES unavailable, _ensure_index failed: %s", e)
        return False


async def _flush(es: Any, index: str, batch: list[dict]) -> bool:
    """返回 True 表示写入成功，False 表示 ES 不可用。"""
    try:
        from elasticsearch.helpers import async_bulk
        actions = [{"_index": index, "_source": doc} for doc in batch]
        _ok, errors = await async_bulk(es, actions, raise_on_error=False)
        if errors:
            logger.debug("log_shipper: %d bulk errors (partial write)", len(errors))
        return True
    except Exception as e:
        logger.debug("log_shipper: flush failed (ES down?): %s", e)
        return False


async def _run_once(
    log_queue: queue.Queue,
    es_url: str,
    es_index: str,
    batch_size: int,
    flush_interval: float,
    es_user: str,
    es_password: str,
) -> None:
    """单次运行循环。意外崩溃时会向上抛出，由外层守护负责重启。"""
    es = _make_client(es_url, es_user, es_password)
    index_ready = await _ensure_index(es, es_index)
    consecutive_failures = 0

    try:
        while True:
            batch: list[dict] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + flush_interval

            # 收集一批记录
            while len(batch) < batch_size:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    batch.append(log_queue.get_nowait())
                except queue.Empty:
                    await asyncio.sleep(min(0.05, remaining))

            if not batch:
                continue

            # 索引尚未建好时尝试补建
            if not index_ready:
                index_ready = await _ensure_index(es, es_index)

            if index_ready:
                ok = await _flush(es, es_index, batch)
                if ok:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures == _BACKOFF_AFTER_FAILURES:
                        logger.warning(
                            "log_shipper: ES unreachable after %d attempts, "
                            "backing off %ds (logs will be dropped if queue fills)",
                            consecutive_failures, int(_BACKOFF_MAX_S),
                        )
                    if consecutive_failures >= _BACKOFF_AFTER_FAILURES:
                        # ES 持续不可用：等待后重建连接，让队列在此期间自然溢出
                        await asyncio.sleep(_BACKOFF_MAX_S)
                        index_ready = False  # 重连后重新确认索引
                        await es.close()
                        es = _make_client(es_url, es_user, es_password)
                        consecutive_failures = 0
            # index 未就绪时记录已从队列取出但无法写入，只能丢弃——
            # 这比让队列无限堆积阻塞内存更安全

    except asyncio.CancelledError:
        # 优雅关闭：把队列里剩余记录一次性冲刷
        drain: list[dict] = []
        try:
            while True:
                drain.append(log_queue.get_nowait())
        except queue.Empty:
            pass
        if drain and index_ready:
            await _flush(es, es_index, drain)
        raise
    finally:
        await es.close()


async def run(
    log_queue: queue.Queue,
    es_url: str,
    es_index: str,
    *,
    batch_size: int = 100,
    flush_interval: float = 5.0,
    es_user: str = "",
    es_password: str = "",
) -> None:
    """守护循环：_run_once 意外崩溃时自动重启，不影响主业务。"""
    logger.info(
        "log_shipper: started → %s/%s (batch=%d, interval=%.1fs)",
        es_url, es_index, batch_size, flush_interval,
    )
    restart_wait = 5.0
    while True:
        try:
            await _run_once(
                log_queue, es_url, es_index,
                batch_size, flush_interval, es_user, es_password,
            )
        except asyncio.CancelledError:
            logger.info("log_shipper: stopped")
            return
        except Exception as exc:
            logger.error(
                "log_shipper: unexpected crash (restarting in %.0fs): %s",
                restart_wait, exc, exc_info=True,
            )
            await asyncio.sleep(restart_wait)
            restart_wait = min(restart_wait * 2, 60.0)  # 指数退避，最多 60s
