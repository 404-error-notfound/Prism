"""日志体系测试：JsonFormatter 字段规范、query_id contextvar 透传、ES handler 行为。"""
from __future__ import annotations

import json
import logging
import queue

from app.core.logging import (
    ESLogHandler,
    JsonFormatter,
    _TraceFilter,
    query_id_var,
    trace_id_var,
)


def _record(msg: str = "hello", **kwargs) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in kwargs.items():
        setattr(record, k, v)
    return record


class TestJsonFormatter:
    def test_flat_schema_fields(self):
        """文件 JSON 与 ES 文档同名字段：level/logger/msg/service（不再有 log.level/message）。"""
        doc = json.loads(JsonFormatter().format(_record("搜索完成")))
        assert doc["level"] == "INFO"
        assert doc["logger"] == "app.test"
        assert doc["msg"] == "搜索完成"
        assert doc["service"]
        assert "@timestamp" in doc
        # 旧 schema 字段必须消失
        for legacy in ("log.level", "log.logger", "message", "service.name"):
            assert legacy not in doc

    def test_timestamp_has_timezone_offset(self):
        """@timestamp 带时区偏移（本地时间+偏移，与 ES 的 UTC 可对照）。"""
        doc = json.loads(JsonFormatter().format(_record()))
        ts = doc["@timestamp"]
        assert "T" in ts and ("+" in ts or ts.endswith("Z") or "-" in ts[10:])

    def test_context_ids_included_when_set(self):
        _TraceFilter().filter(r := _record())
        doc = json.loads(JsonFormatter().format(r))
        assert "trace_id" not in doc  # 空值不写
        trace_id_var.set("tid-1")
        query_id_var.set("qid-1")
        try:
            _TraceFilter().filter(r2 := _record())
            doc2 = json.loads(JsonFormatter().format(r2))
            assert doc2["trace_id"] == "tid-1"
            assert doc2["query_id"] == "qid-1"
        finally:
            trace_id_var.set("")
            query_id_var.set("")

    def test_structured_blocks_hoisted(self):
        r = _record(metrics={"latency_ms": 5}, event={"category": "search", "type": "done"})
        doc = json.loads(JsonFormatter().format(r))
        assert doc["metrics"]["latency_ms"] == 5
        assert doc["event"]["type"] == "done"


class TestESLogHandler:
    def test_doc_shape(self):
        q: queue.Queue = queue.Queue(maxsize=10)
        handler = ESLogHandler(q)
        record = _record("pool refreshed", query_id="q1")
        handler.emit(record)
        doc = q.get_nowait()
        assert doc["msg"] == "pool refreshed"
        assert doc["level"] == "INFO"
        assert doc["query_id"] == "q1"
        assert doc["service"]

    def test_queue_full_drops_silently(self):
        """ES 宕机队列满：丢弃不阻塞、不抛。"""
        q: queue.Queue = queue.Queue(maxsize=1)
        handler = ESLogHandler(q)
        handler.emit(_record("first"))
        handler.emit(_record("dropped"))  # 不抛
        assert q.qsize() == 1

    def test_min_level_filter(self):
        """级别过滤经 Logger.callHandlers 生效（Handler.handle 本身不查 level）。"""
        q: queue.Queue = queue.Queue(maxsize=10)
        handler = ESLogHandler(q, min_level=logging.WARNING)
        logger = logging.getLogger("test.es_handler_level")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.info("below min level")      # 被 handler level 拦
            logger.warning("above min level")   # 进队列
        finally:
            logger.removeHandler(handler)
        assert q.qsize() == 1
        assert q.get_nowait()["msg"] == "above min level"
