"""Orchestrator — parallel engine dispatch with circuit breaker + Tier fallback.

Stage 1 implements:
  - Tier 1a: SearXNG (primary)
  - Tier 1b: DDGS (fallback)
  - Tier 2:  Serper / DataForSEO (if configured)
  - Cache layer (before + after)
  - URL dedup
  - Basic ranking
  - published_at fallback chain
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import AsyncGenerator

from app.core.config import settings
from app.core.redis import get_redis
from app.core.validator import detect_temporal_query
from app.engines.base import ProxyQualityError, RawResult
from app.engines.chinaso import ChinaSoEngine
from app.engines.dataforseo import DataForSEOEngine
from app.engines.ddgs import DDGSEngine
from app.engines.searxng import SearXNGEngine, build_engines
from app.engines.serper import SerperEngine
from app.engines.tavily import TavilyEngine
from app.engines.volcengine import VolcEngineEngine
from app.schemas.search import SearchRequest, SearchResponse, SearchResult
from app.services import cache as cache_svc
from app.services import proxy_client
from app.governance.aigc import apply_aigc_check
from app.governance.paths import PathConfig, get_config, select_path
from app.services.commercial import commercial_tags, compute_commercial_score
from app.services.content_format import format_content
from app.services.date_extractor import extract_published_at
from app.services.dedup import deduplicate, extract_domain
from app.services.fetcher import collect_page_fetches, start_page_fetches, stream_page_fetches
from app.services.ranker import _source_trust, is_trusted_domain, rank_results
from app.services.reachability import annotate_reachability
from app.services.drop_trace import DropTrace
from app.services.relevance_gate import apply_coverage_gate, tokenize_query
from app.services.rewriter import rewrite as rewrite_query
from app.services.spam_gate import apply_spam_gate
from app.services.audit import SearchAuditRecord, StageTiming, save_drop_trace, save_search_audit
from app.services.session_signals import record_signal
from app.services.summarizer import summarize_results
from app.core.logging import trace_id_var, client_ip_var, query_id_var, query_text_var

logger = logging.getLogger(__name__)

# Circuit breaker: failures per engine within settings.circuit_ttl_s window.
# TTL 可配（默认 9s）。走代理的引擎熔断时会异步请 proxy-hub 抽新 IP（见 _record_failure）。
_CIRCUIT_KEY = "circuit:{engine}"
_CIRCUIT_THRESHOLD = 3


def _resolve_gov_cfg(req: SearchRequest, query_id: str) -> PathConfig:
    gov_path = select_path(
        intent=req.intent,
        sites=req.Filter.Sites,
        default=settings.governance_default_path,
    )
    cfg = get_config(gov_path)
    logger.info("governance.path query_id=%s path=%s", query_id, gov_path.value)
    return cfg


def _single_source_monopoly(results: list[RawResult]) -> str | None:
    """召回被单一低信任域名垄断时返回该域名，否则 None。

    场景：baidu/bing/quark/sogou 分片被风控归零、只剩 sogou wechat 一片出结果
    （生产痼疾：10 条全是 weixin.sogou.com 陈货）。垄断按域名占比判定；垄断域
    在 T1/T2 可信域时视为合法结果（如站点限定查询），不值得付重试延迟。
    """
    if not results:
        return None
    counts = Counter(extract_domain(r.url) for r in results)
    domain, n = counts.most_common(1)[0]
    if n / len(results) < settings.recall_monopoly_share:
        return None
    if is_trusted_domain(domain):
        return None
    return domain


def _drop_stale_low_trust(results: list[RawResult]) -> tuple[list[RawResult], int]:
    """低信任源时效硬过滤：发布时间早于 recall_stale_max_age_days 的非 T1/T2 结果丢弃。

    只在调用方未显式传 TimeRange 时使用：推荐/对比类查询不召旧文，避免多年前
    公众号陈货霸榜。无发布时间/解析失败的结果保留（不确定不误杀）；滤空则
    fail-open 返回原列表（少而陈好过没有）。
    """
    if not results:
        return results, 0
    cutoff = datetime.now(UTC) - timedelta(days=settings.recall_stale_max_age_days)
    kept: list[RawResult] = []
    dropped = 0
    for r in results:
        stale = False
        if r.published_at:
            try:
                dt = datetime.fromisoformat(r.published_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                stale = dt < cutoff
            except ValueError:
                stale = False
        if stale and not is_trusted_domain(extract_domain(r.url)):
            dropped += 1
        else:
            kept.append(r)
    if not kept:  # fail-open：全陈也胜过空
        return results, 0
    return kept, dropped


class Orchestrator:
    def __init__(self) -> None:
        # SearXNG 拓扑由 build_engines() 统一装配（命名/拓扑唯一来源，admin/巡检共用）：
        # 分片模式（SEARXNG_SHARDS 非空）每片是独立逻辑引擎 searxng/<引擎名>——独立
        # 实例/熔断/ProxyQualityError，慢源或实例卡死只损失该源，其余片照常返回；
        # 非分片为单一 "searxng" 引擎，多实例间 page N 路由。
        self._searxng_engines: dict[str, SearXNGEngine] = build_engines()
        self._ddgs = DDGSEngine()
        self._serper = SerperEngine()
        self._dataforseo = DataForSEOEngine()
        self._volcengine = VolcEngineEngine()
        self._tavily = TavilyEngine()
        self._chinaso = ChinaSoEngine()
        # 走代理的引擎（出口 IP 失效时熔断它们 = 信号 proxy-hub 抽新 IP）：
        # 由引擎自身 proxy_dependent 声明扫描派生，新增走代理引擎零维护。
        self._proxy_dependent_names = [
            name
            for name, eng in [("chinaso", self._chinaso), *self._searxng_engines.items()]
            if eng.proxy_dependent
        ]
        # 本进程内"0 结果是代理质量失败（验证码/IP 被风控）"的 query 标记：
        # searxng 抛 ProxyQualityError 时记录，重试路径据此延长预算 + 先等代理刷新
        self._proxy_quality_failed: set[str] = set()

    async def _record_engine_latency(
        self, engine_name: str, query_id: str, latency_ms: int, status: str
    ) -> None:
        """Record per-engine latency to Redis for dashboard aggregation."""
        import json
        from app.core.redis import get_redis

        r = get_redis()
        record = json.dumps(
            {
                "ts": time.time(),
                "query_id": query_id,
                "latency_ms": latency_ms,
                "status": status,
            },
            ensure_ascii=False,
        )
        await r.lpush(f"dashboard:engine_latency:{engine_name}", record)
        await r.ltrim(f"dashboard:engine_latency:{engine_name}", 0, 499)

    async def _push_engine_stats(
        self, query_id: str, latency_ms: int, engine_stats: dict[str, dict]
    ) -> None:
        """Push per-engine stats to Redis for dashboard aggregation."""
        import json
        from app.core.redis import get_redis

        r = get_redis()
        record = json.dumps(
            {
                "ts": time.time(),
                "query_id": query_id,
                "latency_ms": latency_ms,
                "engine_stats": engine_stats,
            },
            ensure_ascii=False,
        )
        await r.lpush("dashboard:engine_stats", record)
        await r.ltrim("dashboard:engine_stats", 0, 999)  # keep last 1000

    async def search(
        self,
        req: SearchRequest,
        tenant_id: str,
        session_id: str,
        api_key: str = "",
    ) -> SearchResponse:
        start = time.monotonic()
        ts = time.time()
        query_id = str(uuid.uuid4())
        query_id_var.set(query_id)  # 全链路埋点自动携带（_TraceFilter 注入）
        query_text_var.set(req.query)  # proxy_client 信号随 body 上报 proxy-hub
        audit = SearchAuditRecord(
            query_id=query_id,
            tenant_id=tenant_id,
            session_id=session_id,
            query=req.query,
            started_at=ts,
            client_ip=client_ip_var.get(""),
            api_key=api_key,
        )
        # 按需刷新：登记活跃，避免后台在空闲期盲刷代理 IP
        proxy_client.notify_activity()

        # Auto-tune temporal queries
        # 捕获调用方显式 intent（时效策略会把 auto 提升为 news，提升的不算显式），
        # 热路凭此区分"显式 news 新闻查询"与"被时效误设为 news 的实时数值查询"
        _intent_explicit = req.intent != "auto"
        req = await _apply_temporal_policy(req)

        # Optional rule-based query rewrite (QueryControl.QueryRewrite=true)
        if req.QueryControl.QueryRewrite:
            new_q = rewrite_query(req.query)
            if new_q != req.query:
                logger.info(
                    "query.rewrite %r -> %r",
                    req.query,
                    new_q,
                    extra={"event": {"category": "search", "type": "query_rewrite"}},
                )
                req = req.model_copy(update={"Query": new_q})

        logger.info(
            "search.start query=%r tenant=%s query_id=%s recency_days=%s sort_by=%s intent=%s",
            req.query,
            tenant_id,
            query_id,
            req.recency_days,
            req.sort_by,
            req.intent,
            extra={
                "event": {"category": "search", "type": "start"},
                "query_id": query_id,
                "metrics": {
                    "recency_days": req.recency_days,
                    "top_n": req.top_n,
                },
            },
        )

        # 1. Cache check — key includes every shape-affecting field
        cache_fields = _cache_fields(req)
        cached = await cache_svc.get_cached(
            tenant_id, req.query, req.top_n, req.recency_days, **cache_fields
        )
        stale_cached = None
        if cached:
            latency = int((time.monotonic() - start) * 1000)
            logger.info(
                "search.cache_hit query_id=%s latency_ms=%d",
                query_id,
                latency,
                extra={
                    "event": {"category": "search", "type": "cache_hit"},
                    "query_id": query_id,
                    "metrics": {"latency_ms": latency},
                },
            )

            results = [SearchResult(**r) for r in cached["results"]]
            results = _apply_response_field_filters(results, req)

            audit.cached = True
            audit.elapsed_ms = latency
            audit.result_count = len(results)
            audit.results = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "source_engine": r.get("source_engine", "")}
                for r in cached["results"]
            ]
            audit.stages.append(StageTiming(stage="cache_hit", started_at=start, elapsed_ms=latency))
            asyncio.create_task(save_search_audit(audit))
            return SearchResponse(
                query_id=query_id,
                query=req.query,
                results=results,
                summary=cached.get("summary", ""),
                cached=True,
                latency_ms=latency,
            )

        t_cache = time.monotonic()
        logger.info(
            "search.cache_miss query_id=%s",
            query_id,
            extra={"event": {"category": "search", "type": "cache_miss"}, "query_id": query_id},
        )

        stale_cached = await cache_svc.get_stale_cached(
            tenant_id, req.query, req.top_n, req.recency_days, **cache_fields
        )

        # 2a. Governance path selection (before any engine dispatch)
        gov_cfg = _resolve_gov_cfg(req, query_id)

        # 2. Hot index check (Elasticsearch) — skip SearXNG if enough hits
        if settings.hot_index_enabled:
            from app.services import hot_index

            _is_temporal = detect_temporal_query(req.query)
            hot_hits = await hot_index.search(
                req.query,
                top_n=req.top_n,
                time_range=req.TimeRange,
                is_temporal=_is_temporal,
                intent=req.intent,
                intent_explicit=_intent_explicit,
            )
            # L1 覆盖率闸门对热路同样生效，且门槛更高（≥0.5）：热点新闻 snippet
            # 常含"最新/政策"等泛词，零覆盖闸门拦不住零相关新闻。
            # 闸门按话题词（剥掉"今日/相关/新闻"等噪声词）判定——否则"今日X相关新闻"
            # 模板的噪声 token 会把覆盖率稀释到 0.5 以下，热路全灭（财经标签根因）。
            # 例外：纯分类话题（"股市"/"财经"）召回靠 source terms 子句，快讯标题不会
            # 字面带分类词，闸门会误杀精确召回，故跳过。
            # 过滤后不足 min_hits 则降级走 SearXNG 全链路
            _gate_q = hot_index.topic_query(req.query)
            if _gate_q and not hot_index.is_category_topic(_gate_q):
                hot_hits, _, _ = apply_coverage_gate(
                    _gate_q, hot_hits, min_coverage=0.5, kill_empty=True
                )
            if len(hot_hits) >= settings.hot_index_min_hits:
                logger.info(
                    "search.hot_index_hit query_id=%s hits=%d latency_ms=%d",
                    query_id,
                    len(hot_hits),
                    int((time.monotonic() - start) * 1000),
                    extra={
                        "event": {"category": "search", "type": "hot_index_hit"},
                        "query_id": query_id,
                        "metrics": {
                            "hits": len(hot_hits),
                            "latency_ms": int((time.monotonic() - start) * 1000),
                        },
                    },
                )
                # 热点命中：直接进入排序/摘要流程，跳过 SearXNG
                results = hot_hits[: req.top_n]
                summary_text = ""
                if req.NeedSummary:
                    summary_text = await summarize_results(req.query, results)
                latency = int((time.monotonic() - start) * 1000)
                asyncio.create_task(
                    cache_svc.set_cached(
                        tenant_id,
                        req.query,
                        req.top_n,
                        req.recency_days,
                        [r.model_dump() for r in results],
                        summary=summary_text,
                        **cache_fields,
                    )
                )

                audit.elapsed_ms = latency
                audit.result_count = len(results)
                audit.engines_used = ["hot_index"]
                audit.results = [
                    {"title": getattr(r, "title", ""), "url": getattr(r, "url", ""), "source_engine": getattr(r, "source_engine", "")}
                    for r in results
                ]
                audit.stages.append(StageTiming(stage="hot_index", started_at=start, elapsed_ms=latency))
                asyncio.create_task(save_search_audit(audit))
                return SearchResponse(
                    query_id=query_id,
                    query=req.query,
                    results=results,
                    summary=summary_text,
                    cached=False,
                    latency_ms=latency,
                )

        # 3. Tier 1 engines in parallel (cold path)
        # 空闲后首查池可能已过期（proxy-hub 按需停刷）：先唤热，池冷触发刷新并短等，
        # 避免首查全员 proxy error（2026-08-25 生产实测"每轮首查必 0"）
        await proxy_client.ensure_warm()

        open_engines = await self._open_engines()
        ddgs_ok = settings.ddgs_enabled and "ddgs" not in open_engines
        chinaso_ok = settings.chinaso_enabled and "chinaso" not in open_engines
        tier1_tasks: dict[str, asyncio.Task] = {}
        for name, engine in self._tier1_searxng_shards(open_engines, ddgs_ok or chinaso_ok).items():
            tier1_tasks[name] = asyncio.create_task(
                self._named_fetch(name, engine, req, query_id)
            )
        if ddgs_ok:
            tier1_tasks["ddgs"] = asyncio.create_task(
                self._named_fetch("ddgs", self._ddgs, req, query_id)
            )
        if chinaso_ok:
            tier1_tasks["chinaso"] = asyncio.create_task(
                self._named_fetch("chinaso", self._chinaso, req, query_id)
            )

        raw_results: list[RawResult] = []
        engines_used: list[str] = []

        for fut in asyncio.as_completed(tier1_tasks.values()):
            name, batch = await fut
            if batch:
                raw_results.extend(batch)
                engines_used.append(name)

        # high_quality=true → also pull Tavily in parallel with Tier-1 results
        # (Tavily is paid; only fire when the caller explicitly opts in)
        if req.high_quality and self._tavily.is_configured() and "tavily" not in open_engines:
            tav = await self._safe_fetch(self._tavily, req, query_id)
            if tav:
                raw_results.extend(tav)
                engines_used.append("tavily")

        t_engine_end = time.monotonic()
        sub_engines = sorted({r.engine for r in raw_results if r.engine})
        logger.info(
            "search.engines_done query_id=%s engines=%s sub_engines=%s raw_count=%d",
            query_id,
            engines_used,
            sub_engines,
            len(raw_results),
            extra={
                "event": {"category": "search", "type": "engines_done"},
                "query_id": query_id,
                "metrics": {
                    "raw_count": len(raw_results),
                    "engines_used": engines_used,
                    "sub_engines": sub_engines,
                },
            },
        )

        # 代理质量失败（验证码/IP 被风控）的 query 标记只在本阶段消费一次
        proxy_quality = query_id in self._proxy_quality_failed
        self._proxy_quality_failed.discard(query_id)
        # 召回被单一低信任域名垄断也按代理质量问题处理：raw>0 不等于召回健康
        # （2026-09-16 生产实测："北京烤鸭推荐" 10/10 全是 2016-2022 的
        # weixin.sogou.com——四大分片被风控归零、只剩 sogou wechat 独苗）。
        monopoly_domain = (
            _single_source_monopoly(raw_results)
            if settings.recall_monopoly_retry and raw_results
            else None
        )
        if monopoly_domain:
            logger.warning(
                "search.recall_monopoly query_id=%s domain=%s raw_count=%d",
                query_id,
                monopoly_domain,
                len(raw_results),
                extra={
                    "event": {"category": "search", "type": "recall_monopoly"},
                    "query_id": query_id,
                    "metrics": {"domain": monopoly_domain, "raw_count": len(raw_results)},
                },
            )
        if not raw_results or monopoly_domain:
            if (proxy_quality or monopoly_domain) and proxy_client.configured():
                # 先等 proxy-hub 补一批新 IP 再重试（两阶段刷新不清池，旧 IP 继续
                # 可用；等待 ≈ 补位时间 3-5s）——晚几秒响应好过返回 0 结果
                refreshed = await proxy_client.request_force_refresh(timeout_s=8.0)
                if refreshed:
                    # 新 IP 即将落地，清掉走代理引擎的熔断计数，避免重试时被同一窗口
                    # 内的历史失败直接跳过。
                    await _reset_proxy_engine_circuits(self._proxy_dependent_names)
            # 代理质量失败/召回垄断给额外重试预算（可配），其余情况沿用响应预算余量
            extra_s = (
                settings.proxy_retry_extra_budget_s if (proxy_quality or monopoly_domain) else 0.0
            )
            remaining_retry_s = settings.response_time_budget_s + extra_s - (time.monotonic() - start) - 0.2
            if remaining_retry_s >= 1.0:
                retry_timeout = min(settings.engine_timeout_s, remaining_retry_s)
                retry_tasks: dict[str, asyncio.Task] = {}
                retry_open = await self._open_engines()
                chinaso_retry = settings.chinaso_enabled and "chinaso" not in retry_open
                # refresh 失败（如 proxy-hub ReadTimeout）时熔断未清，retry_open 仍是
                # 全员——重试是最后机会，同样走 fail-open，不许空转（生产实测
                # proxy_retry_done raw_count=0 即由此而来）
                for name, engine in self._tier1_searxng_shards(retry_open, chinaso_retry).items():
                    retry_tasks[name] = asyncio.create_task(
                        self._named_fetch(name, engine, req, query_id, timeout_s=retry_timeout)
                    )
                if chinaso_retry:
                    retry_tasks["chinaso"] = asyncio.create_task(
                        self._named_fetch("chinaso", self._chinaso, req, query_id, timeout_s=retry_timeout)
                    )
                for fut in asyncio.as_completed(retry_tasks.values()):
                    name, batch = await fut
                    if batch:
                        raw_results.extend(batch)
                        engines_used.append(name)
                if raw_results:
                    sub_engines = sorted({r.engine for r in raw_results if r.engine})
                logger.info(
                    "search.proxy_retry_done query_id=%s raw_count=%d timeout_s=%.1f",
                    query_id,
                    len(raw_results),
                    retry_timeout,
                    extra={
                        "event": {"category": "search", "type": "proxy_retry_done"},
                        "query_id": query_id,
                        "metrics": {"raw_count": len(raw_results), "timeout_s": retry_timeout},
                    },
                )
                self._proxy_quality_failed.discard(query_id)  # 重试可能再次标记，清掉
        if not raw_results and stale_cached:
            latency = int((time.monotonic() - start) * 1000)
            logger.warning(
                "search.stale_cache_fallback query_id=%s latency_ms=%d",
                query_id,
                latency,
                extra={
                    "event": {"category": "search", "type": "stale_cache_fallback"},
                    "query_id": query_id,
                    "metrics": {"latency_ms": latency},
                },
            )
            results = [SearchResult(**r) for r in stale_cached["results"]]
            results = _apply_response_field_filters(results, req)
            return SearchResponse(
                query_id=query_id,
                query=req.query,
                results=results,
                summary=stale_cached.get("summary", ""),
                cached=True,
                latency_ms=latency,
            )
        # 逐结果丢弃追踪：召回后 seed 全量，各层把丢弃 URL 连原因记进来，末尾 emit。
        trace = DropTrace()
        trace.seed(raw_results)

        # 3. Dedup (SimHash 由治理路径控制)
        t_dedup = time.monotonic()
        dedup_before = len(raw_results)
        raw_results = deduplicate(raw_results, simhash=gov_cfg.simhash_dedup)
        trace.drop_vanished([r.url for r in raw_results], "dedup", "duplicate/simhash")
        dedup_latency = int((time.monotonic() - t_dedup) * 1000)
        logger.info(
            "search.dedup query_id=%s count_after=%d simhash=%s latency_ms=%d",
            query_id,
            len(raw_results),
            gov_cfg.simhash_dedup,
            dedup_latency,
            extra={
                "event": {"category": "pipeline", "type": "dedup"},
                "query_id": query_id,
                "metrics": {
                    "count_after": len(raw_results),
                    "latency_ms": dedup_latency,
                },
            },
        )

        # 4. Denylist + per-request BlockHosts
        t_denylist = time.monotonic()
        denylist_before = len(raw_results)
        raw_results = await self._apply_denylist(raw_results, req.Filter.BlockHosts)
        trace.drop_vanished([r.url for r in raw_results], "denylist", "blocked domain")
        denylist_latency = int((time.monotonic() - t_denylist) * 1000)
        logger.info(
            "search.denylist query_id=%s count_after=%d latency_ms=%d",
            query_id,
            len(raw_results),
            denylist_latency,
            extra={
                "event": {"category": "pipeline", "type": "denylist"},
                "query_id": query_id,
                "metrics": {
                    "count_after": len(raw_results),
                    "latency_ms": denylist_latency,
                },
            },
        )

        # 4a. Spam gate: 内容农场/违规站硬丢，弱信号软标记（rank 软降权）
        raw_results, spam_dropped = apply_spam_gate(req.query, raw_results, trace=trace)
        if spam_dropped:
            logger.info(
                "search.spam_gate query_id=%s count_after=%d dropped=%d",
                query_id, len(raw_results), spam_dropped,
                extra={
                    "event": {"category": "pipeline", "type": "spam_gate"},
                    "query_id": query_id,
                    "metrics": {"count_after": len(raw_results), "dropped": spam_dropped},
                },
            )

        # 4a-ter. 低信任源时效硬过滤：未显式传 TimeRange 时丢弃 N 天前的非 T1/T2 结果
        # （推荐/对比类查询被多年前公众号陈货霸榜的治理，2026-09-16 生产实测）。
        # 放在 Tier2 判定之前：滤薄了池子要能让兜底触发。
        if not req.TimeRange and raw_results:
            raw_results, stale_dropped = _drop_stale_low_trust(raw_results)
            if stale_dropped:
                trace.drop_vanished(
                    [r.url for r in raw_results], "stale_filter", "low-trust stale"
                )
                logger.info(
                    "search.stale_filter query_id=%s count_after=%d dropped=%d",
                    query_id, len(raw_results), stale_dropped,
                    extra={
                        "event": {"category": "pipeline", "type": "stale_filter"},
                        "query_id": query_id,
                        "metrics": {"count_after": len(raw_results), "dropped": stale_dropped},
                    },
                )

        # 4a-bis. 过滤后池子薄了才触发 Tier2 兜底（旧逻辑在过滤前判，垃圾占位导致不触发）。
        # 注意 TIER2_DEFAULT=none 时此处为空操作；扩召回主力是 over-fetch + 引擎广度。
        if len(raw_results) < 5:
            new, engine = await self._tier2_backfill(req, raw_results, gov_cfg)
            if new:
                raw_results.extend(new)
                trace.seed(new)
                engines_used.append(engine)

        # 4b. Sites whitelist (keep only allowed domains)
        sites_filter_count: int | None = None
        if req.Filter.Sites:
            sites_filter_before = len(raw_results)
            raw_results = _apply_sites_filter(raw_results, req.Filter.Sites)
            trace.drop_vanished([r.url for r in raw_results], "sites", "not in Sites")
            sites_filter_count = len(raw_results)
            logger.debug(
                "search.sites_filter query_id=%s count_after=%d sites=%r",
                query_id,
                sites_filter_count,
                req.Filter.Sites,
            )

        content_prefetch_tasks: dict[str, asyncio.Task] = {}
        if req.require_detail and raw_results:
            # Start page fetches before semantic ranking so content fetch and CPU ranking overlap.
            # collect_page_fetches later waits only for final top URLs; non-top tasks continue
            # in the background and fill the existing content cache.
            prefetch_urls = list(dict.fromkeys(r.url for r in raw_results if r.url))
            content_prefetch_tasks = start_page_fetches(prefetch_urls)
            logger.info(
                "fetch.prefetch_started query_id=%s count=%d",
                query_id,
                len(content_prefetch_tasks),
            )

        # 5. Rank (async 7-factor) ? weights vary by intent. Keep it inside
        # the end-to-end response budget; embedding/reranker can be slower than
        # engines on CPU-only hosts, so fall back to a cheap lexical ranker.
        t_rank = time.monotonic()
        rank_timeout = max(0.3, settings.response_time_budget_s - (t_rank - start) - 0.5)
        rank_timeout = min(rank_timeout, settings.ranking_budget_s)
        rank_fallback = False
        try:
            ranked = await asyncio.wait_for(
                rank_results(
                    req.query, raw_results, sort_by=req.sort_by, session_id=session_id,
                    intent=req.intent, top_n=req.top_n, trace=trace,
                ),
                timeout=rank_timeout,
            )
        except asyncio.TimeoutError:
            rank_fallback = True
            ranked = _fast_rank_fallback(req.query, raw_results, top_n=req.top_n, trace=trace)
            logger.warning(
                "search.rank_timeout query_id=%s timeout_s=%.2f fallback_count=%d",
                query_id,
                rank_timeout,
                len(ranked),
            )
        rank_latency = int((time.monotonic() - t_rank) * 1000)
        logger.info(
            "search.rank query_id=%s latency_ms=%d fallback=%s timeout_s=%.2f",
            query_id,
            rank_latency,
            rank_fallback,
            rank_timeout,
            extra={
                "event": {"category": "pipeline", "type": "rank"},
                "query_id": query_id,
                "metrics": {"latency_ms": rank_latency, "fallback": rank_fallback, "timeout_s": rank_timeout},
            },
        )

        # 6. Build response (商业评分由治理路径控制)
        t_build = time.monotonic()
        top = ranked[: req.top_n]
        results = [_to_search_result(r, i, gov_cfg) for i, r in enumerate(top)]
        build_latency = int((time.monotonic() - t_build) * 1000)
        logger.info(
            "search.build query_id=%s count=%d latency_ms=%d",
            query_id,
            len(results),
            build_latency,
            extra={
                "event": {"category": "pipeline", "type": "build"},
                "query_id": query_id,
                "metrics": {
                    "count": len(results),
                    "latency_ms": build_latency,
                },
            },
        )

        # 6a. Authority level filter (post-build, uses trust_score + commercial_score)
        if req.Filter.AuthInfoLevel > 0:
            before_auth = {r.url for r in results}
            results = _apply_auth_filter(results, req.Filter.AuthInfoLevel)
            for url in before_auth - {r.url for r in results}:
                trace.drop(url, "auth", f"AuthInfoLevel<{req.Filter.AuthInfoLevel}")
            logger.debug(
                "search.auth_filter query_id=%s level=%d count_after=%d",
                query_id,
                req.Filter.AuthInfoLevel,
                len(results),
            )

        # 6a-bis. 逐结果裁决追踪：一处输出整条流水线对每条结果的去留+原因+分数
        trace.emit(logger, query_id, req.query)
        # 逐条裁决落 ES（prism_drop_trace，每 URL 一文档，Kibana 可查），fire-and-forget
        asyncio.create_task(save_drop_trace(query_id, req.query, trace.records()))

        # 6b. Deep 路径：AIGC 规则检测
        if gov_cfg.aigc_check and results:
            t_aigc = time.monotonic()
            results = apply_aigc_check(results)
            logger.info("governance.aigc_check query_id=%s latency_ms=%d",
                        query_id, int((time.monotonic() - t_aigc) * 1000))

        # 6c. Post-rank parallel work: page content / reachability / summary
        #     all kick off together so total latency ≈ max(enabled task), not sum.
        #     Per-task floors ensure content fetch and LLM summary actually have
        #     time to finish even when the overall budget would starve them.
        summary_text = ""
        if results:
            elapsed = time.monotonic() - start
            remaining = max(0.0, settings.response_time_budget_s - elapsed - 0.2)

            # When the caller asks for expensive work, give that work a real
            # budget floor (remaining is ~1.5s after engines, not enough for
            # a real page fetch or a DeepSeek round-trip).
            content_timeout = _content_budget(remaining, req.require_detail)
            summary_timeout = min(remaining, settings.summary_budget_s) if req.NeedSummary else 0

            content_task: asyncio.Task | None = None
            reach_task: asyncio.Task | None = None
            summary_task: asyncio.Task | None = None

            top_urls = [r.url for r in results]

            if req.require_detail:
                fetch_tasks = content_prefetch_tasks or start_page_fetches(top_urls)
                content_task = asyncio.create_task(
                    collect_page_fetches(fetch_tasks, top_urls, timeout=content_timeout)
                )

            # NeedUrl controls whether Url is included in response; it is not a reachability probe.

            if summary_timeout:
                summary_task = asyncio.create_task(summarize_results(req.query, results))

            if content_task is not None:
                prefetched = await content_task
                results = _attach_page_content(results, prefetched)
                asyncio.create_task(
                    record_signal(
                        session_id, "drill_down", domain=results[0].domain, query=req.query
                    )
                )
            if reach_task is not None:
                await reach_task
            if summary_task is not None:
                try:
                    summary_text = await asyncio.wait_for(summary_task, timeout=summary_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "search.summary_wait_timeout query_id=%s after=%.1fs",
                        query_id,
                        summary_timeout,
                        extra={
                            "event": {"category": "search", "type": "summary_timeout"},
                            "query_id": query_id,
                            "metrics": {"timeout_s": summary_timeout},
                        },
                    )
                    summary_task.cancel()
                    summary_text = ""

            # Apply ContentFormats only after content is attached
            _apply_content_format(results, req.ContentFormats)
            results = _apply_response_field_filters(results, req)

        # 6c. Engine survival stats (accuracy proxy)
        from collections import Counter

        raw_engine_counts = Counter(r.engine for r in raw_results)
        engine_stats: dict[str, dict] = {}
        for r in results:
            eng = r.source_engine or "unknown"
            engine_stats.setdefault(eng, {"survival_count": 0, "ranks": []})
            engine_stats[eng]["survival_count"] += 1
        for idx, r in enumerate(results):
            eng = r.source_engine or "unknown"
            engine_stats[eng]["ranks"].append(idx + 1)
        for eng, stat in engine_stats.items():
            stat["avg_rank"] = sum(stat["ranks"]) / len(stat["ranks"]) if stat["ranks"] else 0
            stat["raw_count"] = raw_engine_counts.get(eng, 0)
            stat["survival_rate"] = (
                stat["survival_count"] / stat["raw_count"] if stat["raw_count"] > 0 else 0.0
            )
            del stat["ranks"]

        # 7. Cache store
        asyncio.create_task(
            cache_svc.set_cached(
                tenant_id,
                req.query,
                req.top_n,
                req.recency_days,
                [r.model_dump() for r in results],
                summary=summary_text,
                **cache_fields,
            )
        )

        # Push to Redis for dashboard consumption (fire-and-forget)
        asyncio.create_task(
            self._push_engine_stats(
                query_id,
                latency_ms=int((time.monotonic() - start) * 1000),
                engine_stats=engine_stats,
            )
        )

        latency = int((time.monotonic() - start) * 1000)
        logger.info(
            "search.done query_id=%s result_count=%d latency_ms=%d",
            query_id,
            len(results),
            latency,
            extra={
                "event": {"category": "search", "type": "done"},
                "query_id": query_id,
                "metrics": {
                    "result_count": len(results),
                    "latency_ms": latency,
                    "engine_stats": engine_stats,
                },
            },
        )


        audit.elapsed_ms = latency
        audit.result_count = len(results)
        audit.engines_used = engines_used
        audit.sub_engines = sub_engines
        audit.engine_stats = engine_stats
        audit.stages = [
            StageTiming(stage="cache", started_at=start, elapsed_ms=int((t_cache - start) * 1000)),
            StageTiming(stage="engines", started_at=t_cache, elapsed_ms=int((t_engine_end - t_cache) * 1000)),
            StageTiming(stage="dedup", started_at=t_dedup, elapsed_ms=dedup_latency),
            StageTiming(stage="denylist", started_at=t_denylist, elapsed_ms=denylist_latency),
            StageTiming(stage="rank", started_at=t_rank, elapsed_ms=rank_latency),
            StageTiming(stage="build", started_at=t_build, elapsed_ms=build_latency),
            StageTiming(stage="total", started_at=start, elapsed_ms=latency),
        ]
        audit.results = [
            {"title": getattr(r, "title", ""), "url": getattr(r, "url", ""), "source_engine": getattr(r, "source_engine", "")}
            for r in results
        ]
        asyncio.create_task(save_search_audit(audit))
        return SearchResponse(
            query_id=query_id,
            query=req.query,
            engines_used=engines_used,
            results=results,
            summary=summary_text,
            latency_ms=latency,
        )

    async def search_stream(
        self,
        req: SearchRequest,
        tenant_id: str,
        session_id: str,
        api_key: str = "",
    ) -> AsyncGenerator[dict, None]:
        """Yield SSE events: plan → engine_done(每引擎) → results(SERP+摘要) →
        content(每抓到一个 URL 一条) → done。NeedContent=true 时正文逐条流式推送，
        SERP 先返回不被抓正文阻塞。缓存命中走 results(cached)+done 短路。"""
        query_id = str(uuid.uuid4())
        query_id_var.set(query_id)  # 全链路埋点自动携带（_TraceFilter 注入）
        query_text_var.set(req.query)  # proxy_client 信号随 body 上报 proxy-hub
        start = time.monotonic()

        proxy_client.notify_activity()

        req = await _apply_temporal_policy(req)

        if req.QueryControl.QueryRewrite:
            new_q = rewrite_query(req.query)
            if new_q != req.query:
                req = req.model_copy(update={"Query": new_q})

        logger.info(
            "search.start query=%r tenant=%s query_id=%s (stream)",
            req.query,
            tenant_id,
            query_id,
            extra={
                "event": {"category": "search", "type": "start"},
                "query_id": query_id,
                "metrics": {"top_n": req.top_n, "stream": True},
            },
        )
        yield {"event": "plan", "data": {"query_id": query_id, "query": req.query}}

        cache_fields = _cache_fields(req)
        cached = await cache_svc.get_cached(
            tenant_id, req.query, req.top_n, req.recency_days, **cache_fields
        )
        if cached:
            # 缓存命中：结果已含正文，一次性推 results（cached=True）+ done
            yield {
                "event": "results",
                "data": {
                    "query_id": query_id,
                    "cached": True,
                    "results": cached["results"],
                    "summary": cached.get("summary", ""),
                    "latency_ms": int((time.monotonic() - start) * 1000),
                },
            }
            yield {
                "event": "done",
                "data": {
                    "query_id": query_id,
                    "cached": True,
                    "latency_ms": int((time.monotonic() - start) * 1000),
                },
            }
            return

        gov_cfg = _resolve_gov_cfg(req, query_id)

        all_raw: list[RawResult] = []
        engines_used: list[str] = []

        # 同 search()：空闲后首查先唤热代理池
        await proxy_client.ensure_warm()

        open_engines = await self._open_engines()
        ddgs_ok = settings.ddgs_enabled and "ddgs" not in open_engines
        chinaso_ok = settings.chinaso_enabled and "chinaso" not in open_engines
        tier1_tasks: dict[str, asyncio.Task] = {}
        for name, engine in self._tier1_searxng_shards(open_engines, ddgs_ok or chinaso_ok).items():
            tier1_tasks[name] = asyncio.create_task(
                self._named_fetch(name, engine, req, query_id)
            )
        if ddgs_ok:
            tier1_tasks["ddgs"] = asyncio.create_task(
                self._named_fetch("ddgs", self._ddgs, req, query_id)
            )
        if chinaso_ok:
            tier1_tasks["chinaso"] = asyncio.create_task(
                self._named_fetch("chinaso", self._chinaso, req, query_id)
            )

        for fut in asyncio.as_completed(tier1_tasks.values()):
            name, results = await fut
            if results:
                all_raw.extend(results)
                engines_used.append(name)
                yield {"event": "engine_done", "data": {"engine": name, "count": len(results)}}

        if req.high_quality and self._tavily.is_configured() and "tavily" not in open_engines:
            tav = await self._safe_fetch(self._tavily, req, query_id)
            if tav:
                all_raw.extend(tav)
                engines_used.append("tavily")
                yield {"event": "engine_done", "data": {"engine": "tavily", "count": len(tav)}}

        logger.info(
            "search.engines_done query_id=%s engines=%s raw_count=%d (stream)",
            query_id,
            engines_used,
            len(all_raw),
            extra={
                "event": {"category": "search", "type": "engines_done"},
                "query_id": query_id,
                "metrics": {
                    "raw_count": len(all_raw),
                    "engines_used": engines_used,
                    "stream": True,
                },
            },
        )

        trace = DropTrace()
        trace.seed(all_raw)
        all_raw = deduplicate(all_raw, simhash=gov_cfg.simhash_dedup)
        trace.drop_vanished([r.url for r in all_raw], "dedup", "duplicate/simhash")
        all_raw = await self._apply_denylist(all_raw, req.Filter.BlockHosts)
        trace.drop_vanished([r.url for r in all_raw], "denylist", "blocked domain")
        all_raw, _spam_dropped = apply_spam_gate(req.query, all_raw, trace=trace)

        # 过滤后池子薄了才触发 Tier2 兜底（与非流式路径一致）
        if len(all_raw) < 5:
            new, tier2_engine = await self._tier2_backfill(req, all_raw, gov_cfg)
            if new:
                all_raw.extend(new)
                trace.seed(new)
                engines_used.append(tier2_engine)
                yield {
                    "event": "engine_done",
                    "data": {"engine": tier2_engine, "count": len(new)},
                }

        if req.Filter.Sites:
            all_raw = _apply_sites_filter(all_raw, req.Filter.Sites)
            trace.drop_vanished([r.url for r in all_raw], "sites", "not in Sites")
        ranked = await rank_results(
            req.query, all_raw, sort_by=req.sort_by, session_id=session_id,
            intent=req.intent, top_n=req.top_n, trace=trace,
        )
        top = ranked[: req.top_n]
        results = [_to_search_result(r, i, gov_cfg) for i, r in enumerate(top)]
        if req.Filter.AuthInfoLevel > 0:
            before_auth = {r.url for r in results}
            results = _apply_auth_filter(results, req.Filter.AuthInfoLevel)
            for url in before_auth - {r.url for r in results}:
                trace.drop(url, "auth", f"AuthInfoLevel<{req.Filter.AuthInfoLevel}")
        trace.emit(logger, query_id, req.query)
        asyncio.create_task(save_drop_trace(query_id, req.query, trace.records()))
        if gov_cfg.aigc_check and results:
            results = apply_aigc_check(results)

        # 摘要（BM25 抽取式，<100ms，不依赖正文）先算好，随 SERP 一起推送
        summary_text = ""
        if results and req.NeedSummary:
            try:
                summary_text = await asyncio.wait_for(
                    summarize_results(req.query, results),
                    timeout=max(0.8, settings.summary_budget_s),
                )
            except asyncio.TimeoutError:
                summary_text = ""

        # 1) SERP 立即推送（不含正文/可达性）——首字节只取决于 ranking + summary(<100ms)，
        #    不让 2.5s 可达性探测或抓正文阻塞它（流式的核心就是 SERP 要快）
        yield {
            "event": "results",
            "data": {
                "query_id": query_id,
                "query": req.query,
                "engines_used": engines_used,
                "results": [r.model_dump() for r in results],
                "summary": summary_text,
                "latency_ms": int((time.monotonic() - start) * 1000),
            },
        }

        # 可达性探测起并发 task，不阻塞 SERP 首字节，在正文流式期间并行跑
        reach_task = None

        # 2) 正文逐条流式：每抓完一个 URL 推一个 content 事件，连接全程有数据，
        #    既绕过客户端/网络中间层的"静默超时"，又让正文渐进填充
        if results and req.require_detail:
            content_timeout = max(0.8, settings.content_fetch_budget_s)
            prefetched: dict[str, tuple[str | None, bool]] = {}
            async for url, content, robots_allowed in stream_page_fetches(
                [r.url for r in results], content_timeout
            ):
                prefetched[url] = (content, robots_allowed)
                if content:
                    yield {
                        "event": "content",
                        "data": {
                            "url": url,
                            "content": content,
                            "robots_allowed": robots_allowed,
                        },
                    }
            results = _attach_page_content(results, prefetched)
            _apply_content_format(results, req.ContentFormats)
            asyncio.create_task(
                record_signal(
                    session_id, "drill_down", domain=results[0].domain, query=req.query
                )
            )

        if reach_task is not None:
            await reach_task

        # 缓存完整结果（含已抓到的正文 + 可达性）
        asyncio.create_task(
            cache_svc.set_cached(
                tenant_id,
                req.query,
                req.top_n,
                req.recency_days,
                [r.model_dump() for r in results],
                summary=summary_text,
                **cache_fields,
            )
        )

        # 3) 结束事件
        latency = int((time.monotonic() - start) * 1000)
        logger.info(
            "search.done query_id=%s result_count=%d latency_ms=%d (stream)",
            query_id,
            len(results),
            latency,
            extra={
                "event": {"category": "search", "type": "done"},
                "query_id": query_id,
                "metrics": {
                    "result_count": len(results),
                    "latency_ms": latency,
                    "stream": True,
                },
            },
        )
        yield {
            "event": "done",
            "data": {
                "query_id": query_id,
                "results": [r.model_dump() for r in results],
                "summary": summary_text,
                "fetched": sum(1 for r in results if r.page_content),
                "latency_ms": int((time.monotonic() - start) * 1000),
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch(
        self, req: SearchRequest, query_id: str = ""
    ) -> tuple[list[RawResult], list[str]]:
        """Dispatch Tier 1 in parallel (pipelined circuit check); fall back to Tier 2 if needed."""
        engines_used: list[str] = []
        all_raw: list[RawResult] = []

        # Pipeline all circuit-breaker checks in one Redis round trip
        open_engines = await self._open_engines()

        coros = {}
        for name, engine in self._open_searxng(open_engines).items():
            coros[name] = self._safe_fetch(engine, req, query_id)
        if settings.ddgs_enabled and "ddgs" not in open_engines:
            coros["ddgs"] = self._safe_fetch(self._ddgs, req, query_id)

        if coros:
            gathered = await asyncio.gather(*coros.values(), return_exceptions=True)
            for name, result in zip(coros.keys(), gathered):
                if isinstance(result, list) and result:
                    all_raw.extend(result)
                    engines_used.append(name)

        # Tier 2 if results are scarce
        if len(all_raw) < 5:
            tier2, engine = await self._fetch_tier2(req)
            if tier2:
                all_raw.extend(tier2)
                engines_used.append(engine)

        return all_raw, engines_used

    async def _tier2_backfill(
        self, req: SearchRequest, pool: list[RawResult], gov_cfg: PathConfig
    ) -> tuple[list[RawResult], str]:
        """池子过滤后偏薄时取 Tier2 兜底：取回 → 去重/denylist/去垃圾 → 剔除已在池中的。

        Tier2 是在主池 dedup/denylist/spam 之后取的，必须同样清洗一遍，避免把刚滤掉的
        垃圾从兜底重新放进来。返回 (净新增结果, 引擎名)；无 Tier2/全重复时返回 ([], "")。
        """
        tier2, engine = await self._fetch_tier2(req)
        if not tier2:
            return [], ""
        tier2 = deduplicate(tier2, simhash=gov_cfg.simhash_dedup)
        tier2 = await self._apply_denylist(tier2, req.Filter.BlockHosts)
        tier2, _ = apply_spam_gate(req.query, tier2)
        seen = {r.url for r in pool}
        return [r for r in tier2 if r.url not in seen], engine

    async def _fetch_tier2(
        self, req: SearchRequest, query_id: str = ""
    ) -> tuple[list[RawResult], str]:
        tier2 = settings.tier2_default
        if tier2 == "dataforseo" and self._dataforseo.is_configured():
            r = await self._safe_fetch(self._dataforseo, req, query_id)
            return r, "dataforseo"
        if tier2 == "serper" and self._serper.is_configured():
            r = await self._safe_fetch(self._serper, req, query_id)
            return r, "serper"
        if tier2 == "volcengine" and self._volcengine.is_configured():
            r = await self._safe_fetch(self._volcengine, req, query_id)
            return r, "volcengine"
        return [], ""

    async def _safe_fetch(self, engine, req: SearchRequest, query_id: str = "", timeout_s: float | None = None) -> list[RawResult]:
        # 时效由 time_range + intent=news + recency 衰减保证，不再把今天日期拼进
        # query：拼日期会把它当 BM25 查询词，主动捞来"盖了今天日期戳"的农场页/百科年份页。
        # 同理剥离独立时效副词（最新/刚刚/今日…），它们也只会招日期戳垃圾。
        # over-fetch：抽 top_n×5 候选，给去重/去垃圾留出余量（饿汉式并行扩召回）。
        # 框架词剥离同理：多约束长尾 query 的"适合/推荐/的"等框架词会把它坍缩成单
        # 高频词招来字典/百科页，剥掉后召回更宽（原串仍由排序侧保留做相关性）。
        from app.services.rewriter import normalize_for_engine
        query = normalize_for_engine(req.query, framing=settings.enable_framing_strip)
        t0 = time.monotonic()
        try:
            results = await asyncio.wait_for(
                engine.search(
                    query,
                    num=req.top_n * 5,
                    # 只在调用方显式传 TimeRange 时下发硬时间过滤；默认("")不传，避免给常青
                    # 查询套 month 窗口腰斩召回。时效新鲜度由 intent=news 的 recency 排序因子保证。
                    recency_days=(req.recency_days if req.TimeRange else None),
                ),
                timeout=timeout_s or settings.engine_timeout_s,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            for r in results:
                # Preserve sub-engine info (e.g. searxng/baidu) if already present
                if not r.engine or "/" not in r.engine:
                    r.engine = engine.name

            logger.info(
                "engine.done engine=%s count=%d latency_ms=%d",
                engine.name,
                len(results),
                latency_ms,
                extra={
                    "event": {"category": "engine", "type": "done"},
                    "engine": {"name": engine.name},
                    "metrics": {"result_count": len(results), "latency_ms": latency_ms},
                },
            )

            from app.services.metrics import search_latency_seconds

            search_latency_seconds.labels(engine=engine.name, tier="tier1").observe(
                latency_ms / 1000.0
            )

            # Sub-engine latency split for meta-engines (SearXNG：整引擎或其分片)
            if engine.meta_engine:
                from collections import Counter

                sub_counts = Counter(
                    r.engine.split("/", 1)[1]
                    for r in results
                    if r.engine and r.engine.startswith("searxng/")
                )
                if sub_counts:
                    logger.info(
                        "engine.sub_engines engine=%s distribution=%s",
                        engine.name,
                        dict(sub_counts),
                        extra={
                            "event": {"category": "engine", "type": "sub_engines"},
                            "engine": {"name": engine.name, "sub_distribution": dict(sub_counts)},
                            "metrics": {"sub_engine_counts": dict(sub_counts)},
                        },
                    )
                    from app.services.metrics import search_subengine_latency_seconds

                    for sub_name, count in sub_counts.items():
                        search_subengine_latency_seconds.labels(
                            parent_engine=engine.name, sub_engine=sub_name
                        ).observe(latency_ms / 1000.0)
                        await self._record_engine_latency(
                            f"{engine.name}/{sub_name}", query_id, latency_ms, "done"
                        )
            else:
                await self._record_engine_latency(engine.name, query_id, latency_ms, "done")

            return results
        except asyncio.TimeoutError:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "engine.timeout engine=%s latency_ms=%d",
                engine.name,
                latency_ms,
                extra={
                    "event": {"category": "engine", "type": "timeout"},
                    "engine": {"name": engine.name},
                    "metrics": {"latency_ms": latency_ms},
                },
            )
            from app.services.metrics import engine_failures_total

            engine_failures_total.labels(engine=engine.name).inc()
            await self._record_engine_latency(engine.name, query_id, latency_ms, "timeout")
            await self._record_failure(engine.name)
            return []
        except ProxyQualityError as exc:
            # 出口 IP 被目标站风控/验证码：引擎 200 但实质无数据。记熔断（第 1 次
            # 失败即触发 proxy-hub 异步刷新），并标记本 query——重试路径据此
            # 延长预算 + 先等新 IP 补位再重试（晚几秒响应好过返回 0 结果）
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "engine.proxy_quality engine=%s latency_ms=%d detail=%s",
                engine.name, latency_ms, exc,
                extra={
                    "event": {"category": "engine", "type": "proxy_quality"},
                    "engine": {"name": engine.name},
                    "metrics": {"latency_ms": latency_ms},
                },
            )
            from app.services.metrics import engine_failures_total

            engine_failures_total.labels(engine=engine.name).inc()
            await self._record_engine_latency(engine.name, query_id, latency_ms, "proxy_quality")
            await self._record_failure(engine.name)
            self._proxy_quality_failed.add(query_id)
            # 反哺 proxy-hub：上报被风控的目标站（分片名 searxng/baidu → baidu），
            # 让它即时剔除当时用的上游 IP，别等巡检 N 轮确认
            proxy_client.report_block(
                engine.name.split("/", 1)[1] if "/" in engine.name else engine.name
            )
            return []
        except Exception as exc:
            logger.warning(
                "engine.error engine=%s error=%s",
                engine.name,
                exc,
                extra={
                    "event": {"category": "engine", "type": "error"},
                    "engine": {"name": engine.name},
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
            )
            from app.services.metrics import engine_failures_total

            engine_failures_total.labels(engine=engine.name).inc()
            await self._record_engine_latency(engine.name, query_id, int((timeout_s or settings.engine_timeout_s) * 1000), "error")
            await self._record_failure(engine.name)
            return []

    def _open_searxng(self, open_engines: set[str]) -> dict[str, SearXNGEngine]:
        """未熔断的 SearXNG 引擎：分片为各片，非分片为单引擎。"""
        return {n: e for n, e in self._searxng_engines.items() if n not in open_engines}

    def _tier1_searxng_shards(
        self, open_engines: set[str], fallback_available: bool
    ) -> dict[str, SearXNGEngine]:
        """本轮要调度的 SearXNG 分片。

        全员熔断且无其他 tier1 来源时 fail-open：忽略熔断状态返回全部分片。
        全部跳过 = 确定性 0 结果（生产实测 122ms 秒回空，客户端随即重搜放大
        故障）；熔断 TTL 仅 circuit_ttl_s，fail-open 代价最多多等一个
        engine_timeout_s，好过确定性空结果。fallback_available=True（ddgs/
        chinaso 可用）时尊重熔断，不 bypass。
        """
        shards = self._open_searxng(open_engines)
        if shards or fallback_available or not self._searxng_engines:
            return shards
        logger.warning(
            "engine.circuit_bypass engines=%s reason=all_circuit_open",
            sorted(self._searxng_engines),
            extra={
                "event": {"category": "engine", "type": "circuit_bypass"},
                "engine": {"open": sorted(self._searxng_engines)},
            },
        )
        return dict(self._searxng_engines)

    async def _open_engines(self) -> set[str]:
        """Return set of engine names with open circuit breakers — one pipelined Redis call."""
        names = [*self._searxng_engines, "ddgs", "serper", "dataforseo", "volcengine"]
        r = get_redis()
        pipe = r.pipeline()
        for name in names:
            pipe.get(_CIRCUIT_KEY.format(engine=name))
        values = await pipe.execute()
        open_set = {
            name
            for name, val in zip(names, values)
            if val is not None and int(val) >= _CIRCUIT_THRESHOLD
        }
        if open_set:
            # 被跳过的引擎不会产生任何 engine.* 日志，没有这条 warning 时
            # engines=[] raw_count=0 的请求完全无法归因
            open_sorted = sorted(open_set)
            logger.warning(
                "engine.circuit_open engines=%s — skipped this request",
                open_sorted,
                extra={
                    "event": {"category": "engine", "type": "circuit_open"},
                    "engine": {"open": open_sorted},
                },
            )
        return open_set

    async def _named_fetch(
        self, name: str, engine, req: SearchRequest, query_id: str = "", timeout_s: float | None = None
    ) -> tuple[str, list[RawResult]]:
        """Wrap _safe_fetch to return (engine_name, results) — used with asyncio.as_completed."""
        results = await self._safe_fetch(engine, req, query_id, timeout_s=timeout_s)
        return name, results

    async def _record_failure(self, engine_name: str) -> None:
        r = get_redis()
        key = _CIRCUIT_KEY.format(engine=engine_name)
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.circuit_ttl_s)
        results = await pipe.execute()
        # 恰好跨过阈值那一次（INCR 原子，并发里只有一个请求拿到 ==threshold）→ searxng 熔断
        failures = int(results[0])
        if engine_name in self._proxy_dependent_names and failures in (1, _CIRCUIT_THRESHOLD):
            # 大概率是出口 IP 死了，异步请 proxy-hub 立即抽新 IP（它内部有锁 + 巡检自愈兜底）。
            # 刷新成功后清掉对应熔断计数，让新 IP 有机会被使用。信号自动携带
            # 请求上下文的 query_id（proxy_client._signal_body），proxyhub_logs
            # 按 query_id 检索即得"哪个查询的失败触发了换批"。
            async def _refresh_and_clear():
                ok = await proxy_client.request_force_refresh(timeout_s=3.0)
                if ok:
                    await _reset_proxy_engine_circuits(self._proxy_dependent_names)

            asyncio.create_task(_refresh_and_clear())

    async def _apply_denylist(
        self,
        results: list[RawResult],
        block_hosts: str = "",
    ) -> list[RawResult]:
        """Filter results whose domain is on the Redis denylist or per-request block list."""
        r = get_redis()
        domains = {extract_domain(res.url) for res in results}
        if not domains:
            return results

        pipe = r.pipeline()
        for d in domains:
            pipe.sismember("denylist:domains", d)
        flags = await pipe.execute()

        denied = {d for d, flag in zip(domains, flags) if flag}

        # Per-request block_hosts (comma-separated)
        if block_hosts:
            extra = {h.strip().lower() for h in block_hosts.split(",") if h.strip()}
            denied |= extra

        if denied:
            logger.debug("Denylist filtered domains: %s", denied)
        return [res for res in results if extract_domain(res.url) not in denied]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content_budget(remaining_s: float, require_detail: bool) -> float:
    """正文抓取预算：remaining 被引擎/rank 挤到零点几秒时给 min floor 兜底。

    无正文的结果对 deep research 类调用方等于不可用（生产实测 remaining=0.3s →
    fetch.collect 全员超时 → 响应只剩 title+snippet）。代价：极端情况总响应超出
    response_time_budget_s 最多约 content_fetch_min_budget_s。
    """
    if not require_detail:
        return 0
    return max(
        settings.content_fetch_min_budget_s,
        min(remaining_s, settings.content_fetch_budget_s),
    )


def _fast_rank_fallback(
    query: str,
    results: list[RawResult],
    top_n: int,
    trace=None,
) -> list[RawResult]:
    """Cheap timeout fallback when semantic ranking cannot fit the response budget.

    分词必须走 tokenize_query（jieba）：旧实现按空格切词，无空格中文 query
    （"长沙市雨花区强大酒业商行"）会整串当一个 term，没有任何页面的 title/snippet
    能完整命中，相关性得分全灭、排序退化成"有没有日期"（2026-09-16 生产实测：
    RankScore 0.2/0.0 两极、长沙旅游类跑题结果混榜）。
    """
    import unicodedata

    terms = tokenize_query(query)

    # 整词短语加成：切词之外保留原 query 整体作为一个"短语 term"参与打分。
    # 实体类查询（公司全名"长沙市雨花区强大酒业商行"）在企查查/爱企查标题里是
    # 完整连续出现的，短语命中是强信号，加分要盖过散命中 token 的堆砌。
    # 空白归一后匹配，吸收"湖南长沙 高桥" vs "湖南长沙高桥"的写法差异。
    phrase = "".join(unicodedata.normalize("NFKC", query.lower()).split())

    # rank 超时发生在 rank_results 内部时，L1 覆盖率闸门的过滤成果随超时任务
    # 一起被丢弃（2026-09-16 测试服实测：11 条零覆盖垃圾被闸门丢了又回到响应）。
    # 兜底路径自己补一道零覆盖过滤；杀空则 fail-open 保留原列表。
    if terms:
        survivors = [
            r for r in results
            if any(
                t in unicodedata.normalize("NFKC", f"{r.title} {r.snippet}".lower())
                for t in terms
            )
        ]
        if survivors:
            results = survivors

    def score(item: RawResult) -> float:
        title = unicodedata.normalize("NFKC", (item.title or "").lower())
        snippet = unicodedata.normalize("NFKC", (item.snippet or "").lower())
        haystack = f"{title} {snippet}"
        s = 0.0
        for term in terms:
            if term in title:
                s += 2.0
            if term in snippet:
                s += 1.0
            s += min(haystack.count(term), 3) * 0.1
        # 整词短语命中：title 连续出现完整 query（去空白）是实体精确匹配强信号
        if phrase and len(phrase) >= 2:
            if phrase in "".join(title.split()):
                s += 3.0
            elif phrase in "".join(snippet.split()):
                s += 1.5
        if item.published_at:
            s += 0.2
        if item.engine.startswith("chinaso"):
            s += 0.1
        item.extra["rank_score"] = round(s, 4)
        return s

    ranked = sorted(results, key=score, reverse=True)
    if trace is not None:
        for r in ranked[:top_n]:
            trace.mark_survivor_score(r.url, float(r.extra.get("rank_score", 0.0)))
    return ranked


def _attach_page_content(
    results: list[SearchResult],
    content_map: dict[str, tuple[str | None, bool]],
) -> list[SearchResult]:
    """Attach prefetched Markdown content to each SearchResult.

    If robots.txt disallows a URL, the content is still attached but the
    URL and domain are masked so the source is not exposed to the caller.
    """
    masked = 0
    fetched = 0
    for r in results:
        content, robots_allowed = content_map.get(r.url, (None, True))
        r.page_content = content
        if content:
            fetched += 1
        if content and not robots_allowed:
            r.url = ""
            r.domain = "来自网络搜索"
            masked += 1
    logger.info(
        "fetch.detail_done fetched=%d/%d masked=%d",
        fetched,
        len(results),
        masked,
        extra={
            "event": {"category": "fetch", "type": "detail_done"},
            "metrics": {"fetched": fetched, "total": len(results), "masked": masked},
        },
    )
    return results


def _summary_source(r: SearchResult) -> str:
    return (r.page_content or r.snippet or "").strip()


def _content_source(r: SearchResult) -> str:
    # Response Content falls back to Summary when fetched page content is empty.
    return (r.page_content or _summary_source(r)).strip()


def _apply_response_field_filters(
    results: list[SearchResult], req: SearchRequest
) -> list[SearchResult]:
    """Filter results only when Volc Need* flags are true."""
    filtered = results
    if req.Filter.NeedUrl:
        filtered = [r for r in filtered if (r.url or "").strip()]
    if req.NeedSummary:
        filtered = [r for r in filtered if _summary_source(r)]
    if req.Filter.NeedContent:
        filtered = [r for r in filtered if _content_source(r)]
    return filtered

def _cache_fields(req: SearchRequest) -> dict:
    """Extract every shape-affecting field into a dict for cache-key inclusion."""
    return {
        "need_content": req.Filter.NeedContent,
        "need_summary": req.NeedSummary,
        "need_url": req.Filter.NeedUrl,
        "content_formats": req.ContentFormats,
        "sites": req.Filter.Sites,
        "block_hosts": req.Filter.BlockHosts,
        "auth_level": req.Filter.AuthInfoLevel,
        "sort_by": req.sort_by,
        "intent": req.intent,
    }


async def _apply_temporal_policy(req: SearchRequest) -> SearchRequest:
    """时效查询：只把排序偏向新鲜度（intent=news → recency 权重 0.45），不注入硬时间过滤。

    旧实现把检测到的时效查询强制设 TimeRange=OneDay → recency_days=1 → SearXNG
    time_range=day，会把常青/季节/医学/参考类查询（"春季流感最新…指南"）的召回腰斩，
    且 baidu/sogou 对 time_range 松散执行，残骸里只剩 sogou 陈货。新鲜度改由排序的
    recency 因子压（intent=news）；硬时间窗只在调用方显式传 TimeRange 时生效（见 _safe_fetch）。
    用 intent=news 而非 sort_by=time：后者纯按时间倒序会让当天任何无关内容碾压相关结果。
    仅在用户未显式指定 intent（auto）时提升，尊重显式 intent。
    """
    if req.intent != "auto":
        return req  # 尊重显式 intent
    updates = {"intent": "news"}
    if detect_temporal_query(req.query):
        logger.info("temporal.detected query=%r — intent=news（recency 排序加成，不加硬时间过滤）", req.query)
        return req.model_copy(update=updates)
    # Semantic fallback: handles creative/dialectal temporal expressions regex misses
    from app.services import embedder as _embedder_mod
    if await _embedder_mod.is_temporal_semantic(req.query):
        logger.info(
            "temporal.detected_semantic query=%r — intent=news",
            req.query,
            extra={"event": {"category": "search", "type": "temporal_override"}},
        )
        return req.model_copy(update=updates)
    return req


def _apply_content_format(results: list[SearchResult], fmt: str) -> None:
    """Convert each result's page_content/snippet to the requested format in place."""
    for r in results:
        if r.page_content:
            r.page_content = format_content(r.page_content, fmt, source="markdown")


def _apply_sites_filter(results: list[RawResult], sites: str) -> list[RawResult]:
    """Keep only results whose domain is in the comma-separated Sites whitelist."""
    allowed = {s.strip().lower() for s in sites.split(",") if s.strip()}
    if not allowed:
        return results
    return [r for r in results if extract_domain(r.url) in allowed]


def _apply_auth_filter(results: list[SearchResult], level: int) -> list[SearchResult]:
    """Filter results below the requested authority level.

    Level 1 (非常权威): T1 trusted domains only — trust_score ≥ 0.85
    Level 2 (正常权威): non-commercial domains — commercial_score < 0.5
    Level 3 (一般权威): low-commercial — commercial_score < 0.8
    Level 4 / 0: no filtering
    Falls back to unfiltered list if every result would be removed.
    """
    if level == 1:
        filtered = [r for r in results if r.trust_score >= 0.85]
    elif level == 2:
        filtered = [r for r in results if r.commercial_score < 0.5]
    elif level == 3:
        filtered = [r for r in results if r.commercial_score < 0.8]
    else:
        return results
    return filtered if filtered else results  # never return empty


def _to_search_result(raw: RawResult, idx: int, gov_cfg: PathConfig) -> SearchResult:
    domain = extract_domain(raw.url)
    published_at = extract_published_at(raw)
    trust = _source_trust(domain)
    if gov_cfg.compute_commercial:
        commercial = compute_commercial_score(raw.url, raw.title, raw.snippet)
        tags = commercial_tags(commercial)
    else:
        commercial, tags = 0.0, []
    return SearchResult(
        id=f"r{idx + 1}",
        title=raw.title,
        url=raw.url,
        snippet=raw.snippet,
        published_at=published_at,
        domain=domain,
        commercial_score=commercial,
        trust_score=trust,
        rank_score=raw.extra.get("rank_score", 0.5),
        tags=tags,
        source_engine=raw.engine or "unknown",
    )


# 走代理引擎的熔断计数由 Orchestrator._proxy_dependent_names 维护
# （分片模式 = 各 searxng/<引擎> 片 + chinaso，非分片 = searxng + chinaso）


async def _reset_proxy_engine_circuits(names: list[str]) -> None:
    """清掉走代理引擎的熔断计数。

    proxy-hub 两阶段刷新成功后调用：新 IP 即将可用，旧的失败计数不应再让引擎
    被跳过。Redis Cluster 兼容：每个 key 单独删除。
    """
    r = get_redis()
    await asyncio.gather(
        *[r.delete(_CIRCUIT_KEY.format(engine=e)) for e in names],
        return_exceptions=True,
    )
