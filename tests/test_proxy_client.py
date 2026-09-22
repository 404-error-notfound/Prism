"""proxy_client 测试：活跃信号节流、强制刷新、隧道出网地址。

代理池由独立 proxy-hub 服务托管，gateway 侧只剩这些交互。
"""
from __future__ import annotations

import asyncio

import app.services.proxy_client as pc
from app.core.config import settings


def _set_url(monkeypatch, url: str):
    monkeypatch.setattr(settings, "proxy_service_url", url)


class TestNotifyActivity:
    async def test_no_url_noop(self, monkeypatch):
        _set_url(monkeypatch, "")
        pc._last_activity_mono = 0.0
        pc.notify_activity()  # 不抛、不建任务
        assert pc._last_activity_mono == 0.0

    async def test_throttled(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")
        posted = []

        async def fake_post(path, timeout=None, json_body=None):
            posted.append(path)
            return None

        monkeypatch.setattr(pc, "_post", fake_post)
        pc._last_activity_mono = 0.0
        pc.notify_activity()
        pc.notify_activity()  # 10s 节流窗口内第二次被吞
        await asyncio.sleep(0)  # 让 fire-and-forget 任务跑完
        assert posted == ["/activity"]


class TestRequestForceRefresh:
    async def test_no_url_false(self, monkeypatch):
        _set_url(monkeypatch, "")
        assert await pc.request_force_refresh() is False

    async def test_ok(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")

        class Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

        async def fake_post(path, timeout=None, json_body=None):
            assert path == "/refresh"
            return Resp()

        monkeypatch.setattr(pc, "_post", fake_post)
        assert await pc.request_force_refresh() is True

    async def test_query_id_forwarded(self, monkeypatch):
        """query_id/query 经 ContextVar 自动进信号 body（proxy-hub 侧日志关联锚点）。

        不显式传参：与日志埋点同一机制，orchestrator 之外的路径（如唤热）
        只要在请求上下文内也自动带上。"""
        from app.core.logging import query_id_var, query_text_var

        _set_url(monkeypatch, "http://proxy-hub:8100")
        bodies = []

        class Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

        async def fake_post(path, timeout=None, json_body=None):
            bodies.append(json_body)
            return Resp()

        monkeypatch.setattr(pc, "_post", fake_post)
        token = query_id_var.set("q-123")
        ttoken = query_text_var.set("Python asyncio 最佳实践")
        try:
            assert await pc.request_force_refresh() is True
            assert bodies == [{"query_id": "q-123", "query": "Python asyncio 最佳实践",
                               "wait_s": 2.0}]  # 默认 timeout 3s → 服务端等待 2s
        finally:
            query_id_var.reset(token)
            query_text_var.reset(ttoken)
        assert await pc.request_force_refresh(timeout_s=8.0) is True
        assert bodies[-1] == {"wait_s": 7.0}  # 无请求上下文 → 只有 wait_s

    async def test_unreachable_false(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")

        async def fake_post(path, timeout=None, json_body=None):
            return None  # 连接失败

        monkeypatch.setattr(pc, "_post", fake_post)
        assert await pc.request_force_refresh() is False


class TestEnsureWarm:
    def setup_method(self):
        pc._status_cache = (0.0, True)

    def _fake_client(self, monkeypatch, status_json=None, post_json=None):
        """构造假 httpx client：GET /status 与 POST /refresh 的返回值可指定。"""

        class Resp:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            is_closed = False

            async def get(self, url, headers=None):
                assert url.endswith("/status")
                return Resp(status_json or {"available": True})

            async def post(self, url, headers=None, timeout=None, json=None):
                assert url.endswith("/refresh")
                return Resp(post_json or {"ok": True})

        monkeypatch.setattr(pc, "_get_client", lambda: FakeClient())

    async def test_no_url_always_true(self, monkeypatch):
        _set_url(monkeypatch, "")
        assert await pc.ensure_warm() is True

    async def test_warm_pool_no_refresh(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")
        self._fake_client(monkeypatch, status_json={"available": True})
        assert await pc.ensure_warm() is True

    async def test_cold_pool_triggers_refresh(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")
        self._fake_client(
            monkeypatch, status_json={"available": False}, post_json={"ok": True}
        )
        assert await pc.ensure_warm() is True
        # 刷新成功后缓存为可用，5s 内不再访问
        assert pc._status_cache[1] is True

    async def test_cold_pool_refresh_sends_wait_s(self, monkeypatch):
        """唤热 /refresh 必须带 wait_s：撞维护锁时服务端等持锁方落地再应答，
        而不是秒返"池非空但全禁区"的假阳性 ok（2026-09-08 禁区波根因修复，
        与 request_force_refresh 同口径）。"""
        _set_url(monkeypatch, "http://proxy-hub:8100")
        captured: dict = {}

        class Resp:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            is_closed = False

            async def get(self, url, headers=None):
                return Resp({"available": False})

            async def post(self, url, headers=None, timeout=None, json=None):
                captured.update(json or {})
                return Resp({"ok": True})

        monkeypatch.setattr(pc, "_get_client", lambda: FakeClient())
        assert await pc.ensure_warm(max_wait_s=8.0) is True
        assert captured.get("wait_s") == 7.0

    async def test_refresh_failure_not_blocking(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")
        self._fake_client(
            monkeypatch, status_json={"available": False}, post_json={"ok": False}
        )
        assert await pc.ensure_warm() is False  # 返回 False 但不抛，查询路径继续

    async def test_status_unreachable_passes(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")

        class FakeClient:
            is_closed = False

            async def get(self, url, headers=None):
                raise ConnectionError("down")

        monkeypatch.setattr(pc, "_get_client", lambda: FakeClient())
        assert await pc.ensure_warm() is True  # proxy-hub 挂了不阻塞查询

    async def test_multi_url_failover(self, monkeypatch):
        """多副本地址：第一个挂了自动试第二个。"""
        _set_url(monkeypatch, "http://hub-1:8100, http://hub-2:8100")
        tried = []

        class Resp:
            status_code = 200

            def json(self):
                return {"available": True, "batch_id": "b1"}

        class FakeClient:
            is_closed = False

            async def get(self, url, headers=None):
                tried.append(url)
                if "hub-1" in url:
                    raise ConnectionError("down")
                return Resp()

        monkeypatch.setattr(pc, "_get_client", lambda: FakeClient())
        assert await pc.ensure_warm() is True
        assert tried == ["http://hub-1:8100/status", "http://hub-2:8100/status"]
        from app.core.logging import proxy_batch_var
        assert proxy_batch_var.get() == "b1"
        proxy_batch_var.set("")


class TestTunnelProxyUrl:
    """隧道地址从 PROXY_SERVICE_URL 推导（host 相同、端口固定换 8101，
    账密 prism:<token> 注入）；旧字段 PROXY_TUNNEL_URL 仅作空推导时的回退。"""

    def test_derived_from_service_url(self, monkeypatch):
        monkeypatch.setattr(
            settings, "proxy_service_url", "http://10.0.0.1:8100"
        )
        monkeypatch.setattr(settings, "proxy_project_token", "tok123")
        monkeypatch.setattr(settings, "proxy_tunnel_url", "")
        assert (
            pc.get_tunnel_proxy_url() == "http://prism:tok123@10.0.0.1:8101"
        )

    def test_any_control_port_becomes_8101(self, monkeypatch):
        """隧道固定监听 8101（proxy-hub 端口契约），控制面配什么端口都照换。"""
        monkeypatch.setattr(settings, "proxy_service_url", "http://hub.example:9999")
        monkeypatch.setattr(settings, "proxy_project_token", "tok123")
        monkeypatch.setattr(settings, "proxy_tunnel_url", "")
        assert pc.get_tunnel_proxy_url() == "http://prism:tok123@hub.example:8101"

    def test_multi_replicas_take_first(self, monkeypatch):
        """gateway 侧暂只用第一条（多隧道故障转移未做）；SearXNG 侧消费全列表。"""
        monkeypatch.setattr(
            settings,
            "proxy_service_url",
            "http://10.0.0.1:8100, http://10.0.0.2:8100",
        )
        monkeypatch.setattr(settings, "proxy_project_token", "tok123")
        monkeypatch.setattr(settings, "proxy_tunnel_url", "")
        assert (
            pc.get_tunnel_proxy_url() == "http://prism:tok123@10.0.0.1:8101"
        )
        assert pc._tunnel_urls() == [
            "http://prism:tok123@10.0.0.1:8101",
            "http://prism:tok123@10.0.0.2:8101",
        ]

    def test_legacy_fallback_when_not_derived(self, monkeypatch):
        """PROXY_SERVICE_URL 空 + 旧字段有值 → 原样透传（deprecated 回退）。"""
        monkeypatch.setattr(settings, "proxy_service_url", "")
        monkeypatch.setattr(
            settings, "proxy_tunnel_url", "http://prism:tok123@10.0.0.1:8101"
        )
        monkeypatch.setattr(pc, "_drift_warned", False)
        assert (
            pc.get_tunnel_proxy_url() == "http://prism:tok123@10.0.0.1:8101"
        )

    def test_derived_wins_over_legacy(self, monkeypatch):
        """两者都配且漂移 → 以推导为准。"""
        monkeypatch.setattr(
            settings, "proxy_service_url", "http://10.0.0.1:8100"
        )
        monkeypatch.setattr(settings, "proxy_project_token", "tok123")
        monkeypatch.setattr(settings, "proxy_tunnel_url", "http://stale:1@0.0.0.0:8101")
        monkeypatch.setattr(pc, "_drift_warned", False)
        assert (
            pc.get_tunnel_proxy_url() == "http://prism:tok123@10.0.0.1:8101"
        )

    def test_empty_config_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_service_url", "")
        monkeypatch.setattr(settings, "proxy_tunnel_url", "")
        assert pc.get_tunnel_proxy_url() is None


class TestAuthHeaders:
    def test_token_produces_bearer_header(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_project_token", "tok123")
        assert pc._auth_headers() == {"Authorization": "Bearer tok123"}

    def test_empty_token_no_header(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_project_token", "")
        assert pc._auth_headers() == {}

    async def test_status_call_sends_auth_header(self, monkeypatch):
        """PROXY_PROJECT_TOKEN 配置后，/status 调用带 Authorization 头。"""
        _set_url(monkeypatch, "http://proxy-hub:8100")
        monkeypatch.setattr(settings, "proxy_project_token", "tok123")
        pc._status_cache = (0.0, True)
        seen = []

        class Resp:
            status_code = 200

            def json(self):
                return {"available": True}

        class FakeClient:
            is_closed = False

            async def get(self, url, headers=None):
                seen.append(headers)
                return Resp()

        monkeypatch.setattr(pc, "_get_client", lambda: FakeClient())
        assert await pc.ensure_warm() is True
        assert seen == [{"Authorization": "Bearer tok123"}]


class TestReportBlock:
    """风控信号上报：/block-signal，按 target 进程内节流（5s）。"""

    def setup_method(self):
        pc._last_block_signal.clear()

    async def test_report_block_posts_target_throttled(self, monkeypatch):
        _set_url(monkeypatch, "http://proxy-hub:8100")
        sent: list[tuple] = []

        async def fake_post(path, timeout=None, json_body=None):
            sent.append((path, json_body))

        monkeypatch.setattr(pc, "_post", fake_post)
        pc.report_block("baidu")
        pc.report_block("baidu")   # 节流：5s 内同 target 第二次不发
        pc.report_block("bing")    # 不同 target 不受影响
        await asyncio.sleep(0.05)  # 让 fire-and-forget task 跑完
        assert sent == [("/block-signal", {"target": "baidu"}),
                        ("/block-signal", {"target": "bing"})]

    async def test_report_block_no_url_noop(self, monkeypatch):
        _set_url(monkeypatch, "")
        sent = []
        monkeypatch.setattr(pc, "_post", lambda *a, **k: sent.append(a))
        pc.report_block("baidu")
        await asyncio.sleep(0.05)
        assert sent == []
