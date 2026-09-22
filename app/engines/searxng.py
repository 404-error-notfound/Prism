"""SearXNG adapter — self-hosted meta-search engine."""

from __future__ import annotations

import asyncio
import itertools
import logging
import math

import httpx

from app.core.config import settings
from app.engines.base import BaseEngine, ProxyQualityError, RawResult

logger = logging.getLogger(__name__)

_request_counter = itertools.count()

_TIMEOUT = httpx.Timeout(7.0, connect=3.0)

# SearXNG 对"挂起态"引擎（上一轮失败进入冷却期，本轮根本没发真实请求）在
# unresponsive_engines 里把原因拼成 "Suspended: <原因>"（get_translated_errors 的
# gettext('Suspended') + ': '），真实失败只报 "<原因>"（如 "CAPTCHA"）。
# 挂起 ≠ 代理故障：换 IP 不解挂起，反而空转 IP 池。default_locale 空 → 英文输出
# （生产实测 unresponsive: [['baidu', 'CAPTCHA']]），故按英文前缀判。
_SUSPENDED_PREFIX = "Suspended"


def _is_suspended(reason: str) -> bool:
    """SearXNG 上报的 unresponsive 原因是挂起态（引擎冷却期、无真实请求）吗？"""
    return reason.startswith(_SUSPENDED_PREFIX)


def _merge_dead(acc: dict[str, str], page_dead: dict[str, str]) -> None:
    """合并各页 unresponsive，真实失败（非挂起）优先于挂起态保留。

    同一引擎可能在 page 1 真被 captcha（报 "CAPTCHA"）、page 2 才进入冷却
    报 "Suspended: CAPTCHA"——纯 dict.update 会让挂起覆盖真实失败，漏掉本应
    触发 block-signal 的代理质量信号。
    """
    for name, reason in page_dead.items():
        existing = acc.get(name)
        if existing is None or (_is_suspended(existing) and not _is_suspended(reason)):
            acc[name] = reason


# Persistent client — reuses TCP connections across requests (major latency win)
_client: httpx.AsyncClient | None = None

# SearXNG returns ~10 results per engine per page
_RESULTS_PER_PAGE = 10


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            http2=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


_instance_urls: list[str] | None = None


def _get_instance_urls() -> list[str]:
    """返回各实例 URL 列表。解析结果缓存，进程生命周期内不重算。"""
    global _instance_urls
    if _instance_urls is None:
        urls_str = settings.searxng_urls.strip()
        if urls_str:
            _instance_urls = [u.strip().rstrip("/") for u in urls_str.split(",") if u.strip()]
        if not _instance_urls:
            _instance_urls = [settings.searxng_url.rstrip("/")]
    return _instance_urls


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


class SearXNGEngine(BaseEngine):
    name = "searxng"
    meta_engine = True
    proxy_dependent = True

    def __init__(
        self, engine: str | None = None, base_url: str | None = None, shard_count: int = 0
    ) -> None:
        # 分片模式（engine 非空）：一源一实例——该实例只跑这一个引擎（由请求参数
        # engines= 切片，容器同镜像同配置），独立熔断/超时/ProxyQualityError，
        # 慢源或实例卡死只损失该源。shard_count = 总分片数，分页公式分母用它
        # （= 旧模式的子引擎总数），保证分片前后总 raw 量等价。
        # _own_urls 非 None 即分片模式（判别唯一来源）：[唯一实例 URL] vs None。
        if engine:
            self.name = f"searxng/{engine}"
            self._own_urls = [base_url.rstrip("/")]  # 编排层（build_engines）必传
            self._engines = engine
            self._formula_engines = shard_count or 1
        else:
            # 全引擎多实例（旧行为）：base_url 参数保留向后兼容，实际多实例路由由
            # _get_instance_urls() 决定，page N → 实例 N
            self._default_url = (base_url or settings.searxng_url).rstrip("/")
            self._own_urls = None
            engines = settings.searxng_engines_list
            self._engines = ",".join(engines)
            self._formula_engines = len(engines)

    def _urls(self) -> list[str]:
        return self._own_urls if self._own_urls is not None else _get_instance_urls()

    async def search(self, query: str, num: int = 20, **kwargs) -> list[RawResult]:
        # 每页每引擎约 10 条，考虑约 40% 去重损耗，计算需要几页。
        # 分母用总分片数（分片模式 = 片数，旧模式 = 子引擎数），各片都按此页数翻，
        # 总 raw 量与旧模式一致，避免每片按单引擎算导致请求量放大。
        raw_per_page = self._formula_engines * _RESULTS_PER_PAGE
        pages_needed = max(1, math.ceil(num / (raw_per_page * 0.6)))
        pages_needed = min(pages_needed, 3)  # 最多 3 页，避免过多请求

        instance_urls = self._urls()

        base_offset = next(_request_counter) % len(instance_urls)

        base_params: dict = {
            "q": query,
            "format": "json",
            "engines": self._engines,
        }
        if kwargs.get("recency_days"):
            base_params["time_range"] = _recency_to_time_range(kwargs["recency_days"])

        # 所有页同时并发发出，page N → instance N（不同代理 IP）
        tasks = [
            self._fetch_page(
                base_params,
                pageno,
                base_url=instance_urls[(base_offset + pageno - 1) % len(instance_urls)],
            )
            for pageno in range(1, pages_needed + 1)
        ]
        pages = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并去重（按 URL）；同时收集各页上报的 unresponsive 子引擎
        # （proxy error / captcha）——0 结果时用来区分"真空"和"代理被风控"
        seen_urls: set[str] = set()
        results: list[RawResult] = []
        dead_engines: dict[str, str] = {}
        for page in pages:
            if isinstance(page, Exception):
                continue
            page_results, page_dead = page
            _merge_dead(dead_engines, page_dead)
            for item in page_results:
                if item.url and item.url not in seen_urls:
                    seen_urls.add(item.url)
                    results.append(item)

        # Fallback: 若带 time_range 返回为空，尝试去掉时间限制再搜一次
        if not results and base_params.get("time_range"):
            logger.info(
                "SearXNG empty with time_range=%s, retrying without", base_params["time_range"]
            )
            fallback_params = {k: v for k, v in base_params.items() if k != "time_range"}
            fallback_tasks = [
                self._fetch_page(
                    fallback_params,
                    pageno,
                    base_url=instance_urls[(base_offset + pageno - 1) % len(instance_urls)],
                )
                for pageno in range(1, pages_needed + 1)
            ]
            fallback_pages = await asyncio.gather(*fallback_tasks, return_exceptions=True)
            for page in fallback_pages:
                if isinstance(page, Exception):
                    continue
                page_results, page_dead = page
                _merge_dead(dead_engines, page_dead)
                for item in page_results:
                    if item.url and item.url not in seen_urls:
                        seen_urls.add(item.url)
                        results.append(item)

        # 0 结果且子引擎大面积 unresponsive → 不是"没结果"，是出口 IP 被风控/验证码。
        # 抛出让编排层走"刷新代理 + 延长预算重试"路径（隧道换 IP 后大概率恢复）。
        # 分片模式（_own_urls 非 None）：该片唯一引擎 unresponsive 即质量失败（该片
        # 独占一个出口 IP）；非分片：过半子引擎 unresponsive 才判（单个源抖动不误报）。
        #
        # 只统计真实失败（原因不带 "Suspended" 前缀）。挂起态是引擎上一轮失败后
        # 进入冷却、本轮根本没发请求——换 IP 解不了挂起，却会触发 block-signal
        # 让 proxy-hub 白白 replace 独享 IP（一次真实验证码 → 挂起 30s → 冷却期内
        # 每条 query 都 fast-fail 报挂起 → 放大成 IP 空转烧池）。全挂起态按普通
        # 0 结果返回，交给引擎侧 30s 自愈。
        if not results:
            suspended = {n for n, r in dead_engines.items() if _is_suspended(r)}
            real_dead = {n for n, r in dead_engines.items() if not _is_suspended(r)}
            min_dead = (
                1 if self._own_urls is not None else max(2, math.ceil(self._formula_engines / 2))
            )
            if len(real_dead) >= min_dead:
                detail = (
                    f"0 results, {len(real_dead)}/{self._formula_engines} engines "
                    f"unresponsive: {sorted(real_dead)}"
                )
                if suspended:
                    detail += f" (suspended: {sorted(suspended)})"
                raise ProxyQualityError(detail)
        return results[:num]

    async def _fetch_page(
        self, base_params: dict, pageno: int, base_url: str | None = None
    ) -> tuple[list[RawResult], dict[str, str]]:
        """Returns (results, dead_engines) — dead_engines 为 {引擎名: 原因}。"""
        url = base_url or self._urls()[0]
        params = {**base_params, "pageno": pageno}
        _fwd = settings.searxng_forwarded_for
        try:
            resp = await _get_client().get(
                f"{url}/search",
                params=params,
                headers={"X-Forwarded-For": _fwd, "X-Real-IP": _fwd},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("SearXNG HTTP error page=%d instance=%s: %s", pageno, url, exc)
            raise
        except Exception as exc:
            logger.warning("SearXNG error page=%d instance=%s: %s", pageno, url, exc)
            raise

        dead_engines: dict[str, str] = {}
        for e in data.get("unresponsive_engines", []):
            name = e[0]
            reason = e[1] if len(e) > 1 else ""
            dead_engines[name] = reason

        results = [
            RawResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                published_at=item.get("publishedDate") or item.get("published_date"),
                engine=f"searxng/{item.get('engine', '')}",
            )
            for item in data.get("results", [])
        ]
        return results, dead_engines

    async def health_check(self) -> bool:
        return await _ping_url(self._urls()[0])


async def _ping_url(url: str) -> bool:
    try:
        resp = await _get_client().get(
            f"{url}/healthz",
            timeout=httpx.Timeout(3.0),
        )
        return resp.status_code == 200
    except Exception:
        return False


async def check_any_instance() -> bool:
    """就绪探针/定时巡检用：ping 注册表里全部实例；任一存活即 True——单片宕机由
    熔断兜底，不应拖垮 gateway readiness。各实例并发探测。"""
    results = await asyncio.gather(
        *(e.health_check() for e in build_engines().values()), return_exceptions=True
    )
    return any(r is True for r in results)


def build_engines() -> dict[str, SearXNGEngine]:
    """按当前配置构建 SearXNG 逻辑引擎注册表（拓扑与命名的唯一来源）。

    分片模式（SEARXNG_SHARDS 非空）：每片一个 searxng/<引擎名>；非分片：单一
    "searxng"。编排层（Tier1 注册/熔断清单）与 admin/巡检（实例清单）共用本工厂，
    避免命名规则多处漂移。
    """
    shard_map = settings.searxng_shard_map
    if not shard_map:
        return {"searxng": SearXNGEngine()}
    out: dict[str, SearXNGEngine] = {}
    for engine, url in shard_map.items():
        eng = SearXNGEngine(engine=engine, base_url=url, shard_count=len(shard_map))
        out[eng.name] = eng
    return out


def _recency_to_time_range(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"
