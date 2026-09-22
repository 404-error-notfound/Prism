"""
Elasticsearch 热点索引查询封装

ES hot_news 索引的数据性质：
  数据来源：微博热搜 / 百度热榜 / 知乎 / 头条 / B 站 / RSS 新闻
  适配查询：今日发生的事件、人物动态、社会话题、热门讨论
  不适配  ：天气/股价/比分等实时数据；知识/教程/常识等长尾内容

命中规则（按优先级，命中即返回结果或返回 []）：
  1. intent=news                       → 强制查 ES
  2. intent=knowledge/academic/commercial → 强制跳过
  3. 知识查询（"什么是X"/"如何Y"）       → 跳过
  4. 实时数据（天气/股价/赛事/汇率）      → 跳过（例外：调用方显式 intent=news
                                          时放行——crawler 已供财经快讯，
                                          "今日股市相关新闻"类新闻查询可由热路服务）
  5. 非时效查询（无"今日/最新"等标记）    → 跳过
  6. 时效 + 无话题词（"今日热搜"）        → 返回今日全量，按 heat_score+时间排
  7. 时效 + 有话题词（"今日娱乐新闻"）    → 多路 should：title(3x)/snippet(1.5x)/
                                          content(0.5x)/source 分类(2x)/kNN
                                          向量(2.5x，EMBED_MODEL 启用时)，
                                          至少命中一路即匹配，按总分排序

orchestrator 调用：
    hits = await hot_index.search(query, top_n, time_range, is_temporal, intent)
    if len(hits) >= settings.hot_index_min_hits:
        # 命中，跳过 SearXNG
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.schemas.search import SearchResult
from app.services import embedder
from app.services.dedup import extract_domain

logger = logging.getLogger(__name__)

# 知识/教程查询：ES 热点索引没这类内容
_KNOWLEDGE_RE = re.compile(
    r"什么是|是什么|啥是|"
    r"如何|怎么[办做样]?|怎样|"
    r"为什么|为啥|"
    r"教程|入门|方法|原理|步骤|做法|攻略|"
    r"区别|对比|差异|"
    r"^what\s+is|^how\s+to|^why",
    re.IGNORECASE,
)

# 实时数据词表（单一真相源，拆成"硬实时数值"+"新闻事件实时"两组，避免两份正则漂移）：
# 硬实时数值（天气/股价/汇率）—— ES 热点索引必然无此类"数值"字段，auto/时效提升的
# intent 一律跳过。例外：调用方显式 intent=news 时放行——crawler 已接入财经快讯源
# （eastmoney_flash/ths_flash/cls_telegraph/nfra），"今日股市相关新闻"是新闻查询而非
# 报价查询，热路可服务；真实时数值（"贵州茅台股价"）由调用方自己承担显式声明的责任。
_HARD_REALTIME = (
    r"天气|气温|温度|降[水雨]|湿度|空气质量|pm2\.5|"
    r"股[价票市]|大盘|行情|涨[幅跌]|跌[幅涨]|汇率|外汇|weather|stock"
)
# 新闻事件类实时（赛事/比分）—— ES 热榜里其实有，仅 auto intent 拦、news 放行走 hot，
# 故不进硬实时（"油价调整""世界杯赛果"同理是真热点，hot 命中是对的，均不在硬实时内）。
_SOFT_REALTIME = r"比分|赛果|赛事|赛程|score"

_HARD_REALTIME_RE = re.compile(_HARD_REALTIME, re.IGNORECASE)
_REALTIME_DATA_RE = re.compile(_HARD_REALTIME + "|" + _SOFT_REALTIME, re.IGNORECASE)

# 时效词 + 泛指词 + 填充词，剥完为空表示无明确话题
_NOISE_RE = re.compile(
    r"今[天日晚早]|今年|本[周月季]|这[周月]|这[几两\d]+天|近[几两\d]+[天周月年]|"
    r"昨[天晚早]|前天|大前天|上[周月年]|"
    r"最新|最近|刚刚|刚才|现在|当前|"
    r"实时|实况|今日|当天|当下|最快|最早|即时|latest|today|"
    r"热[点搜门榜]|头条|热议|trending|"
    r"相关|新闻|资讯|消息|报道|内容|信息|动态|进展|情况|状况|事件|事情|话题|"
    r"帮我?|搜一下|告诉我|查一下|请问|给我|看看|了解",
    re.IGNORECASE,
)

# 话题分类 → 对应 source（弥补"title 不含分类词"的硬伤）
# 用户说"今日数码新闻"时，标题不会写"数码"二字，但 ifanr/36kr/ithome 来源就是数码
_CATEGORY_TO_SOURCES: dict[str, list[str]] = {
    "数码":  ["ifanr", "ithome", "sspai", "36kr"],
    "科技":  ["ifanr", "ithome", "sspai", "36kr", "juejin"],
    "硬件":  ["ifanr", "ithome"],
    "财经":  ["36kr", "eastmoney_flash", "ths_flash", "cls_telegraph", "nfra"],
    "金融":  ["eastmoney_flash", "ths_flash", "cls_telegraph", "nfra"],
    "股市":  ["eastmoney_flash", "ths_flash", "cls_telegraph"],
    "商业":  ["36kr"],
    "创业":  ["36kr"],
    "游戏":  ["gcores", "bilibili_hot"],
    "技术":  ["juejin"],
    "编程":  ["juejin"],
    "开发":  ["juejin"],
    "代码":  ["juejin"],
    "娱乐":  ["bilibili_hot", "bilibili"],
    "视频":  ["bilibili_hot", "bilibili"],
    "知识":  ["zhihu_daily"],
}

_TIME_RANGE_MAP: dict[str, timedelta] = {
    "OneDay":   timedelta(days=1),
    "OneWeek":  timedelta(weeks=1),
    "OneMonth": timedelta(days=30),
    "OneYear":  timedelta(days=365),
}
_DEFAULT_RANGE = timedelta(days=30)

_ATTRS = ["id", "title", "url", "snippet", "content", "source", "heat_score",
          "published_at", "crawled_at"]

_client = None


def _get_client():
    global _client
    if _client is None:
        from elasticsearch import AsyncElasticsearch
        kwargs: dict = {"hosts": [settings.es_url]}
        if settings.es_user and settings.es_password:
            kwargs["basic_auth"] = (settings.es_user, settings.es_password)
        _client = AsyncElasticsearch(**kwargs)
    return _client


def _reset_client() -> None:
    """测试或配置热重载时重置 client。"""
    global _client
    _client = None


def _cutoff_ts(time_range: str) -> int:
    delta = _TIME_RANGE_MAP.get(time_range, _DEFAULT_RANGE)
    return int((datetime.now(timezone.utc) - delta).timestamp())


def _to_search_result(src: dict, rank: int) -> SearchResult:
    return SearchResult(
        id=f"hot_{rank}",
        title=src.get("title", ""),
        url=src.get("url", ""),
        snippet=src.get("snippet") or src.get("content", "")[:300],
        domain=extract_domain(src.get("url", "")),
        page_content=src.get("content", ""),
        published_at=src.get("published_at"),
    )


def _should_skip(query: str, is_temporal: bool, intent: str, intent_explicit: bool = False) -> bool:
    """决定是否跳过 ES 查询，由 orchestrator 走 SearXNG。

    intent_explicit：调用方显式声明的 intent（非时效策略 auto→news 自动提升）。
    显式 intent=news 时绕过硬实时拦截——财经快讯已入 hot_news，"今日股市相关新闻"
    类新闻查询可由热路服务；自动提升的 news 仍拦截（天气→世界杯根因）。
    """
    # 最先判：硬实时数值数据 ES 必无，优先于 intent——时效会把这类误设为 news，
    # 放进 ES 只会召回含"北京时间/2026年"泛词的无关热榜（天气→世界杯）。
    if _HARD_REALTIME_RE.search(query) and not (intent_explicit and intent == "news"):
        return True
    if intent in ("knowledge", "academic", "commercial"):
        return True
    if intent == "news":
        return False
    if _KNOWLEDGE_RE.search(query):
        return True
    if _REALTIME_DATA_RE.search(query):
        return True
    if not is_temporal:
        return True
    return False


def _topic_query(query: str) -> str:
    """剥掉时效词/泛指词/填充词，返回话题核心；为空表示无明确话题。"""
    return _NOISE_RE.sub("", query).strip()


def topic_query(query: str) -> str:
    """_topic_query 的公开包装：orchestrator 热路覆盖率闸门按话题词判定，
    避免"今日/相关/新闻"等噪声词稀释覆盖率（今日X相关新闻模板根因）。"""
    return _topic_query(query)


def is_category_topic(topic: str) -> bool:
    """话题词是否恰好是一个分类词（"股市"/"财经"/"数码"…）。

    纯分类查询（"今日股市相关新闻"→"股市"）的召回靠 source terms 子句——快讯标题
    写的是"股票/指数/A股"，几乎不会字面带分类词，覆盖率闸门会把这批精确召回全杀掉
    （股[价票市] 正则只命中"股票"，而 title 里没有"股市"）。orchestrator 据此跳过闸门。
    话题词包含分类词但不止分类词（"茅台股市"）时不算，仍走正常闸门。
    """
    return topic in _CATEGORY_TO_SOURCES


def _matched_sources(topic: str) -> list[str]:
    """从话题词中提取出对应的 source 列表，如"数码"→[ifanr,ithome,...]。"""
    matched: set[str] = set()
    for category, sources in _CATEGORY_TO_SOURCES.items():
        if category in topic:
            matched.update(sources)
    return list(matched)


async def search(
    query: str,
    top_n: int = 10,
    time_range: str = "",
    is_temporal: bool = False,
    intent: str = "auto",
    intent_explicit: bool = False,
) -> list[SearchResult]:
    """查询热点索引。不命中条件返回 [] 由 orchestrator 走 SearXNG 兜底。"""
    if _should_skip(query, is_temporal, intent, intent_explicit):
        return []

    _tail = {"sort": ["_score", {"crawled_at_ts": "desc"}], "size": top_n, "_source": _ATTRS}

    try:
        es = _get_client()
        cutoff = _cutoff_ts(time_range or "OneDay")
        topic = _topic_query(query)

        if topic:
            should_clauses: list[dict] = [
                {"match": {"title":   {"query": topic, "operator": "and", "boost": 3.0}}},
                {"match": {"snippet": {"query": topic, "boost": 1.5}}},
                {"match": {"content": {"query": topic, "boost": 0.5}}},
            ]
            sources = _matched_sources(topic)
            if sources:
                should_clauses.append({"terms": {"source": sources, "boost": 2.0}})

            if embedder.is_embed_available():
                q_vec = await embedder.encode_query(topic)
                if q_vec is not None:
                    should_clauses.append({"knn": {
                        "field": "vectors",
                        "query_vector": q_vec,
                        "num_candidates": 100,
                        "boost": 2.5,
                    }})

            body = {
                "query": {
                    "bool": {
                        "filter": [{"range": {"crawled_at_ts": {"gt": cutoff}}}],
                        "should": should_clauses,
                        "minimum_should_match": 1,
                    }
                },
                **_tail,
            }
        else:
            # heat_score: 1=最热，0=RSS 新闻无排名
            body = {
                "query": {
                    "function_score": {
                        "query": {"range": {"crawled_at_ts": {"gt": cutoff}}},
                        "functions": [
                            {"filter": {"range": {"heat_score": {"gte": 1,  "lte": 5}}},  "weight": 3.0},
                            {"filter": {"range": {"heat_score": {"gte": 6,  "lte": 20}}}, "weight": 2.0},
                            {"filter": {"range": {"heat_score": {"gte": 21}}},            "weight": 1.5},
                        ],
                        "score_mode": "max",
                        "boost_mode": "replace",
                    }
                },
                **_tail,
            }

        resp = await es.search(index=settings.es_index, body=body)
        hits = resp["hits"]["hits"]
        return [_to_search_result(h["_source"], i + 1) for i, h in enumerate(hits)]

    except Exception as e:
        logger.warning("hot_index search failed: %s", e)
        return []


async def is_available() -> bool:
    try:
        resp = await _get_client().cluster.health()
        return resp.get("status") in ("green", "yellow")
    except Exception:
        return False
