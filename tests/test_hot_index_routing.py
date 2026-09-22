"""hot_index._should_skip 路由判定（纯函数，不依赖 ES）。

回归 2026-06-18 "天气→世界杯" 污染：时效把'最新天气预报'编码成 intent=news，
旧逻辑 news 分支先 return False 绕过了实时数据拦截，放它进 ES 召回无关热榜。
修复后硬实时数值数据（天气/股价/汇率）优先于 intent 一律跳过。

2026-08-07 财经快讯入 hot_news 后：调用方**显式** intent=news（intent_explicit=True）
绕过硬实时拦截——"今日股市相关新闻"是新闻查询而非报价查询；时效自动提升的 news
（intent_explicit=False）仍拦截，保护真实时数值查询。
"""
from __future__ import annotations

from app.schemas.search import SearchResult
from app.services.hot_index import _should_skip, is_category_topic, topic_query
from app.services.relevance_gate import apply_coverage_gate


def test_weather_skipped_even_when_news_intent():
    """时效把'最新天气预报'设成 news 也必须跳过 ES（ES 无天气数据）→ 走 SearXNG。"""
    assert _should_skip("2026年6月最新天气预报北京", is_temporal=True, intent="news") is True


def test_stock_market_skipped_even_when_news_intent():
    assert _should_skip("今日股市收盘行情", is_temporal=True, intent="news") is True


def test_stock_market_allowed_when_explicit_news_intent():
    """显式 intent=news（如定时财经抓取）+ 财经快讯已入索引 → 放行热路。"""
    assert (
        _should_skip("今日股市相关新闻", is_temporal=True, intent="news", intent_explicit=True)
        is False
    )


def test_stock_market_skipped_when_auto_intent():
    """auto intent 下硬实时词照常拦截（报价类查询不进 ES）。"""
    assert _should_skip("贵州茅台股价", is_temporal=False, intent="auto") is True


def test_oil_price_news_not_skipped():
    """油价调整是真新闻事件（非实时数值），news 下应进 ES（hot 命中是对的）。"""
    assert _should_skip("2026年6月最新国内油价调整消息", is_temporal=True, intent="news") is False


def test_sports_result_news_still_allowed():
    """世界杯赛果是热点新闻，不在硬实时拦截内，news intent 下仍可走 hot。"""
    assert _should_skip("世界杯赛果", is_temporal=True, intent="news") is False


def test_knowledge_query_skipped():
    assert _should_skip("什么是回归年", is_temporal=False, intent="auto") is True


def test_topic_query_strips_noise():
    """"今日X相关新闻"模板剥掉时效词/泛指词后只剩话题核心。"""
    assert topic_query("今日货币政策相关新闻") == "货币政策"


def test_is_category_topic():
    """纯分类词才算分类话题（闸门跳过）；含分类词的复合话题仍走闸门。"""
    assert is_category_topic("股市") is True
    assert is_category_topic("财经") is True
    assert is_category_topic("茅台股市") is False
    assert is_category_topic("货币政策") is False


def test_coverage_gate_on_topic_survives():
    """覆盖率闸门按话题词判定：title 含话题词的财经快讯不被噪声词稀释误杀。

    回归：闸门用原始 query（[今日,货币政策,相关,新闻] 4 token）时覆盖率 1/4<0.5 全灭。
    """
    hits = [
        SearchResult(
            id="hot_1",
            title="央行开展5000亿元MLF操作 货币政策宽松预期升温",
            url="https://finance.eastmoney.com/a/1.html",
            domain="finance.eastmoney.com",
            snippet="中国人民银行今日开展中期借贷便利操作，货币政策边际宽松。",
        )
    ]
    survivors, _, _ = apply_coverage_gate(
        topic_query("今日货币政策相关新闻"), hits, min_coverage=0.5, kill_empty=True
    )
    assert len(survivors) == 1
