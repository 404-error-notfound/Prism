"""SearXNG 适配层：0 结果 + 子引擎大面积 unresponsive → ProxyQualityError。

验证码/IP 被风控时 searxng 返回 200 + 空 results + unresponsive_engines，
必须和"该 query 真的没有结果"区分开——前者要抛出让编排层刷新代理重试。
"""

import pytest

from app.core.config import Settings
from app.engines.base import ProxyQualityError, RawResult
from app.engines.searxng import SearXNGEngine, _is_suspended, _merge_dead


def _engine(monkeypatch, engines: str = "baidu,bing,sogou,quark") -> SearXNGEngine:
    monkeypatch.setattr("app.engines.searxng.settings.searxng_engines", engines)
    monkeypatch.setattr("app.engines.searxng.settings.searxng_urls", "http://s1")
    monkeypatch.setattr("app.engines.searxng._instance_urls", None)
    return SearXNGEngine()


def _shard(engine: str = "baidu", shard_count: int = 5) -> SearXNGEngine:
    return SearXNGEngine(
        engine=engine, base_url=f"http://searxng-{engine}", shard_count=shard_count
    )


def _page(results=None, dead=()):
    # dead 可为 {引擎名: 原因}（精确控制挂起/真实失败）或引擎名可迭代对象
    # （旧用法，默认全部按真实失败 "CAPTCHA" 处理）。
    if isinstance(dead, dict):
        return (results or [], dict(dead))
    return (results or [], {name: "CAPTCHA" for name in dead})


class TestSuspendedDetection:
    def test_is_suspended_prefix(self):
        assert _is_suspended("Suspended: CAPTCHA")
        assert _is_suspended("Suspended: timeout")
        assert not _is_suspended("CAPTCHA")
        assert not _is_suspended("timeout")

    def test_merge_dead_prefers_real_over_suspended(self):
        """同引擎跨页报告：真实失败优先于挂起态保留（挂起不该覆盖真实 captcha）。"""
        acc: dict[str, str] = {}
        _merge_dead(acc, {"baidu": "CAPTCHA"})
        _merge_dead(acc, {"baidu": "Suspended: CAPTCHA"})
        assert acc == {"baidu": "CAPTCHA"}

    def test_merge_dead_real_overwrites_prior_suspended(self):
        acc: dict[str, str] = {}
        _merge_dead(acc, {"baidu": "Suspended: CAPTCHA"})
        _merge_dead(acc, {"baidu": "CAPTCHA"})
        assert acc == {"baidu": "CAPTCHA"}


class TestProxyQualitySignal:
    async def test_zero_results_majority_dead_raises(self, monkeypatch):
        """0 结果 + 过半子引擎 unresponsive → ProxyQualityError（代理被风控）。"""
        engine = _engine(monkeypatch)

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(dead={"baidu", "bing", "sogou"})

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        with pytest.raises(ProxyQualityError):
            await engine.search("test query")

    async def test_zero_results_few_dead_returns_empty(self, monkeypatch):
        """0 结果但仅个别子引擎失败 → 真空，安静返回空（不误报代理问题）。"""
        engine = _engine(monkeypatch)

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(dead={"baidu"})

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        assert await engine.search("test query") == []

    async def test_results_present_no_raise_despite_dead(self, monkeypatch):
        """有结果时即使部分子引擎 unresponsive 也正常返回。"""
        engine = _engine(monkeypatch)
        item = RawResult(title="t", url="http://a.com/1", snippet="s")

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(results=[item], dead={"baidu", "bing", "sogou"})

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        results = await engine.search("test query")
        assert len(results) == 1

    async def test_all_pages_exception_returns_empty(self, monkeypatch):
        """所有页直接抛异常（超时/连接失败）→ 走原有空列表路径，不算代理质量。"""
        engine = _engine(monkeypatch)

        async def fake_fetch(base_params, pageno, base_url=None):
            raise TimeoutError("boom")

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        assert await engine.search("test query") == []

    async def test_zero_results_all_suspended_returns_empty(self, monkeypatch):
        """全挂起态（冷却期、无真实请求）→ 不算代理故障，安静返回空——换 IP 解不了挂起。"""
        engine = _engine(monkeypatch)

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(dead={"baidu": "Suspended: CAPTCHA", "bing": "Suspended: timeout"})

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        assert await engine.search("test query") == []

    async def test_zero_results_suspended_not_counted_toward_threshold(self, monkeypatch):
        """挂起不占真实失败名额：2 真实 + 2 挂起（4 引擎，阈 2）→ 2 真实 >= 2 仍触发。"""
        engine = _engine(monkeypatch)

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(
                dead={
                    "baidu": "CAPTCHA",
                    "bing": "CAPTCHA",
                    "sogou": "Suspended: CAPTCHA",
                    "quark": "Suspended: timeout",
                }
            )

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        with pytest.raises(ProxyQualityError):
            await engine.search("test query")

    async def test_zero_results_single_real_rest_suspended_no_raise(self, monkeypatch):
        """1 真实 + 3 挂起（4 引擎，阈 2）→ 真实不足阈，不误报。"""
        engine = _engine(monkeypatch)

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(
                dead={
                    "baidu": "CAPTCHA",
                    "bing": "Suspended: CAPTCHA",
                    "sogou": "Suspended: CAPTCHA",
                    "quark": "Suspended: CAPTCHA",
                }
            )

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        assert await engine.search("test query") == []


class TestShardMode:
    """一源一实例分片：独立逻辑引擎 searxng/<引擎>，分页公式分母 = 总分片数。"""

    def test_shard_identity(self):
        engine = _shard("baidu", shard_count=5)
        assert engine.name == "searxng/baidu"
        assert engine._engines == "baidu"  # 请求参数 engines= 只切片该源
        assert engine._urls() == ["http://searxng-baidu"]  # 只打自己的实例

    async def test_shard_sends_single_engine_param(self, monkeypatch):
        engine = _shard("sogou wechat", shard_count=5)
        seen = {}

        async def fake_fetch(base_params, pageno, base_url=None):
            seen["engines"] = base_params["engines"]
            seen["base_url"] = base_url
            return _page()

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        await engine.search("test query")
        assert seen["engines"] == "sogou wechat"
        assert seen["base_url"] == "http://searxng-sogou wechat"

    async def test_shard_pages_match_legacy_formula(self, monkeypatch):
        """分片后每片页数与旧全引擎模式一致（分母都 = 子引擎/分片总数），
        总 raw 量不变——避免每片按单引擎算导致请求量放大 5 倍。"""
        for num, expected_pages in ((10, 1), (50, 3), (100, 3)):
            legacy = _engine(monkeypatch, "baidu,bing,sogou,quark")
            shard = _shard("baidu", shard_count=4)
            pages = {}
            for label, engine in (("legacy", legacy), ("shard", shard)):
                calls = []

                async def fake_fetch(base_params, pageno, base_url=None, _c=calls):
                    _c.append(pageno)
                    return _page()

                monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
                await engine.search("test query", num=num)
                pages[label] = sorted(calls)
            assert pages["shard"] == pages["legacy"] == list(range(1, expected_pages + 1)), (
                f"num={num}"
            )

    async def test_shard_single_dead_engine_raises(self, monkeypatch):
        """分片：该片唯一引擎 unresponsive 即 ProxyQualityError（该片独占一个出口 IP）。"""
        engine = _shard("sogou", shard_count=5)

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(dead={"sogou"})

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        with pytest.raises(ProxyQualityError):
            await engine.search("test query")

    async def test_shard_suspended_engine_no_raise(self, monkeypatch):
        """分片：该片唯一引擎是挂起态（冷却期无真实请求）→ 不判代理质量，返回空。"""
        engine = _shard("baidu", shard_count=5)

        async def fake_fetch(base_params, pageno, base_url=None):
            return _page(dead={"baidu": "Suspended: CAPTCHA"})

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        assert await engine.search("test query") == []

    async def test_shard_all_pages_exception_returns_empty(self, monkeypatch):
        """分片：整片超时/连接失败走原有空列表路径（由编排层超时熔断处理）。"""
        engine = _shard("bing", shard_count=5)

        async def fake_fetch(base_params, pageno, base_url=None):
            raise TimeoutError("boom")

        monkeypatch.setattr(engine, "_fetch_page", fake_fetch)
        assert await engine.search("test query") == []


class TestShardMapConfig:
    """SEARXNG_SHARDS 解析："引擎名=实例URL" 逗号分隔，引擎名可含空格。"""

    def test_parsing_with_space_engine_name(self):
        s = Settings(
            searxng_shards=(
                "baidu=http://searxng-baidu:8080,"
                "sogou wechat=http://searxng-wechat:8080,"
                "bing=http://searxng-bing:8080/"
            )
        )
        assert s.searxng_shard_map == {
            "baidu": "http://searxng-baidu:8080",
            "sogou wechat": "http://searxng-wechat:8080",
            "bing": "http://searxng-bing:8080",
        }

    def test_parsing_skips_malformed_pairs(self):
        s = Settings(searxng_shards="baidu=http://a:8080,badpair,=http://no-engine,,bing=")
        assert s.searxng_shard_map == {"baidu": "http://a:8080"}

    def test_empty_means_non_shard(self):
        # 显式置空构造：本地 .env 可能带 SEARXNG_SHARDS（分片部署配置），
        # 裸 Settings() 会读到它，测试必须与运行环境解耦
        assert Settings(searxng_shards="").searxng_shard_map == {}
