"""2026-09-08 生产质量事故修复的回归测试。

事故链：代理出口被风控 → 5 分片全 ProxyQualityError → 全员熔断 → 后续请求
122ms 秒回 rc=0；同时引擎慢把正文预算挤到 0.3s → 响应无正文。修复：
1. 全员熔断且无其他 tier1 来源时 fail-open（_tier1_searxng_shards）；
2. 正文抓取预算下限（_content_budget + content_fetch_min_budget_s）；
3. Count 下限 10（SearchRequest._enforce_min_count）。
"""
import logging

from app.core.config import settings
from app.schemas.search import SearchRequest
from app.services.orchestrator import Orchestrator, _content_budget


def _orch(monkeypatch):
    monkeypatch.setattr(
        settings, "searxng_shards",
        "baidu=http://searxng-baidu:8080,bing=http://searxng-bing:8080",
    )
    return Orchestrator()


def test_failopen_when_all_circuit_open(monkeypatch, caplog):
    orch = _orch(monkeypatch)
    fused = {"searxng/baidu", "searxng/bing"}
    with caplog.at_level(logging.WARNING, "app.services.orchestrator"):
        shards = orch._tier1_searxng_shards(fused, fallback_available=False)
    assert set(shards) == fused  # 忽略熔断，返回全部分片
    assert "engine.circuit_bypass" in caplog.text


def test_no_failopen_when_fallback_available(monkeypatch, caplog):
    orch = _orch(monkeypatch)
    fused = {"searxng/baidu", "searxng/bing"}
    with caplog.at_level(logging.WARNING, "app.services.orchestrator"):
        shards = orch._tier1_searxng_shards(fused, fallback_available=True)
    assert shards == {}  # ddgs/chinaso 可用时尊重熔断
    assert "circuit_bypass" not in caplog.text


def test_partial_fuse_no_failopen(monkeypatch):
    orch = _orch(monkeypatch)
    shards = orch._tier1_searxng_shards({"searxng/baidu"}, fallback_available=False)
    assert set(shards) == {"searxng/bing"}  # 只跳过熔断的那片


def test_content_budget_floor(monkeypatch):
    monkeypatch.setattr(settings, "content_fetch_budget_s", 4.0)
    monkeypatch.setattr(settings, "content_fetch_min_budget_s", 1.5)
    assert _content_budget(0.3, True) == 1.5   # 被挤没时 floor 兜底
    assert _content_budget(3.0, True) == 3.0   # 正常余量不变
    assert _content_budget(9.0, True) == 4.0   # 仍受 budget 上限约束
    assert _content_budget(0.3, False) == 0    # 未要求正文时不给预算


def test_count_min_10():
    assert SearchRequest(Query="x", Count=5).Count == 10
    assert SearchRequest(Query="x", Count=1).Count == 10
    assert SearchRequest(Query="x").Count == 10        # 默认值不变
    assert SearchRequest(Query="x", Count=20).Count == 20  # 高于下限不动
    assert SearchRequest(Query="x").top_n == 10
