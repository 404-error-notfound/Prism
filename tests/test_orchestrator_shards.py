"""分片拓扑下 Orchestrator 的 SearXNG 注册。

SEARXNG_SHARDS 非空：每片注册为独立逻辑引擎（独立实例/熔断/ProxyQualityError）；
为空：保持单一 "searxng" 引擎。走代理引擎清单随拓扑变化（驱动熔断→refresh 联动）。
"""
from app.core.config import settings
from app.services.orchestrator import Orchestrator


def test_shard_registration(monkeypatch):
    monkeypatch.setattr(
        settings, "searxng_shards",
        "baidu=http://searxng-baidu:8080,sogou=http://searxng-sogou:8080",
    )
    orch = Orchestrator()
    assert set(orch._searxng_engines) == {"searxng/baidu", "searxng/sogou"}
    assert set(orch._proxy_dependent_names) == {"searxng/baidu", "searxng/sogou", "chinaso"}
    assert orch._searxng_engines["searxng/baidu"]._own_urls == ["http://searxng-baidu:8080"]


def test_non_shard_registration(monkeypatch):
    monkeypatch.setattr(settings, "searxng_shards", "")
    orch = Orchestrator()
    assert set(orch._searxng_engines) == {"searxng"}
    assert set(orch._proxy_dependent_names) == {"searxng", "chinaso"}
