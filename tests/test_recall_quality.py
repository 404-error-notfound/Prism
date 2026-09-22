"""召回质量治理单测：单一低信任域名垄断判定 + 低信任源时效硬过滤。

样本场景取自 2026-09-16 生产实测："北京烤鸭推荐" 10/10 全是
2016-2022 的 weixin.sogou.com（四大分片被风控归零、只剩 sogou wechat 独苗）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.engines.base import RawResult
from app.services import orchestrator, ranker
from app.services.orchestrator import _drop_stale_low_trust, _single_source_monopoly


def _mk(url: str, published_at: str | None = None) -> RawResult:
    return RawResult(title="t", url=url, snippet="s", published_at=published_at)


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------------------
# 垄断判定
# ---------------------------------------------------------------------------

def test_monopoly_detected_when_single_low_trust_domain_dominates():
    results = [_mk(f"https://weixin.sogou.com/link?url=x{i}") for i in range(10)]
    assert _single_source_monopoly(results) == "weixin.sogou.com"


def test_monopoly_not_triggered_when_diverse():
    results = [
        _mk("https://weixin.sogou.com/link?url=x1"),
        _mk("https://m.toutiao.com/w/1/"),
        _mk("https://mbd.baidu.com/newspage/data/dtlandingsuper?nid=1"),
        _mk("https://page.sm.cn/blm/x"),
    ]
    assert _single_source_monopoly(results) is None


def test_monopoly_skipped_for_trusted_domain(monkeypatch):
    """可信域（T1/T2）垄断视为合法结果（如站点限定查询），不触发重试。"""
    monkeypatch.setattr(ranker, "_T1_DOMAINS", {"gov.cn"})
    results = [_mk(f"https://www.gov.cn/a/{i}.htm") for i in range(10)]
    assert _single_source_monopoly(results) is None


def test_monopoly_share_threshold_configurable(monkeypatch):
    monkeypatch.setattr(orchestrator.settings, "recall_monopoly_share", 0.9)
    results = [_mk(f"https://weixin.sogou.com/link?url=x{i}") for i in range(7)]
    results += [_mk(f"https://m.toutiao.com/w/{i}/") for i in range(3)]
    assert _single_source_monopoly(results) is None  # 0.7 < 0.9 阈值


# ---------------------------------------------------------------------------
# 低信任源时效硬过滤
# ---------------------------------------------------------------------------

def test_stale_low_trust_dropped(monkeypatch):
    monkeypatch.setattr(orchestrator.settings, "recall_stale_max_age_days", 730)
    results = [
        _mk("https://weixin.sogou.com/link?url=old", _iso(2000)),   # 2016 陈货 → 丢
        _mk("https://weixin.sogou.com/link?url=new", _iso(30)),     # 新文 → 留
        _mk("https://m.toutiao.com/w/1/", _iso(4000)),              # 更老的低信任 → 丢
    ]
    kept, dropped = _drop_stale_low_trust(results)
    assert dropped == 2
    assert [r.url for r in kept] == ["https://weixin.sogou.com/link?url=new"]


def test_stale_trusted_domain_kept(monkeypatch):
    """可信域（T1/T2）的旧文保留——常青内容（历史/百科）不该被时效误杀。"""
    monkeypatch.setattr(ranker, "_T1_DOMAINS", {"baike.baidu.com"})
    monkeypatch.setattr(orchestrator.settings, "recall_stale_max_age_days", 730)
    results = [_mk("https://baike.baidu.com/item/北京烤鸭", _iso(3000))]
    kept, dropped = _drop_stale_low_trust(results)
    assert dropped == 0
    assert len(kept) == 1


def test_stale_unknown_date_kept():
    """无发布时间/解析失败的结果保留（不确定不误杀）。"""
    results = [_mk("https://weixin.sogou.com/link?url=x"), _mk("https://a.com/b", "not-a-date")]
    kept, dropped = _drop_stale_low_trust(results)
    assert dropped == 0
    assert len(kept) == 2


def test_stale_filter_failopen_when_all_stale(monkeypatch):
    """滤空则 fail-open：少而陈好过没有。"""
    monkeypatch.setattr(orchestrator.settings, "recall_stale_max_age_days", 730)
    results = [_mk(f"https://weixin.sogou.com/link?url=x{i}", _iso(2000)) for i in range(5)]
    kept, dropped = _drop_stale_low_trust(results)
    assert dropped == 0
    assert len(kept) == 5


# ---------------------------------------------------------------------------
# 词面兜底排序（语义排序超时降级）：无空格中文 query 必须按 jieba 分词命中
# ---------------------------------------------------------------------------

def test_fast_rank_fallback_chinese_query(monkeypatch):
    """生产回归（2026-09-16）："长沙市雨花区强大酒业商行" 整串当一个 term 时，
    所有结果相关性全灭，排序退化成"有没有日期"，跑题结果（长沙旅游）混榜。
    修复后真相关结果必须排在跑题结果前面，即使后者有日期加成。"""
    from app.services.orchestrator import _fast_rank_fallback

    good = _mk("https://www.qcc.com/firm/abc123.html")
    good.title = "长沙市雨花区强大酒业商行 - 企查查"
    good.snippet = "长沙市雨花区强大酒业商行成立于2018年06月25日，位于雨花区高桥大市场"
    bad = _mk("https://www.sohu.com/a/925575425_121893286")
    bad.title = "长沙旅游必去十大景点推荐"
    bad.snippet = "长沙作为新晋网红城市，雨花区也有不少好去处"  # 蹭 1 个 token 存活
    bad.published_at = _iso(10)  # 日期加成不许盖过相关性

    ranked = _fast_rank_fallback("长沙市雨花区强大酒业商行", [bad, good], top_n=2)
    assert ranked[0].url == good.url
    assert ranked[0].extra["rank_score"] > ranked[1].extra["rank_score"]


def test_fast_rank_fallback_drops_zero_coverage(monkeypatch):
    """兜底排序补 L1 零覆盖过滤：rank 超时会丢弃 rank_results 内部的闸门成果，
    兜底路径要自己把零覆盖垃圾挡掉（测试服实测 11 条垃圾回流）；杀空 fail-open。"""
    from app.services.orchestrator import _fast_rank_fallback

    on_topic = _mk("https://www.qcc.com/firm/abc.html")
    on_topic.title = "长沙市雨花区强大酒业商行 - 企查查"
    on_topic.snippet = "成立于2018年，位于长沙市雨花区高桥大市场"
    off_topic = _mk("https://map.baidu.com/")
    off_topic.title = "百度地图"
    off_topic.snippet = "浏览地图、搜索地点、查询公交驾车线路"

    ranked = _fast_rank_fallback("长沙市雨花区强大酒业商行", [off_topic, on_topic], top_n=10)
    assert [r.url for r in ranked] == [on_topic.url]  # 零覆盖的被丢弃

    # 杀空 fail-open：全部零覆盖时保留原列表（"搜不到"不该由兜底路径制造）
    only_junk = _fast_rank_fallback("长沙市雨花区强大酒业商行", [off_topic], top_n=10)
    assert len(only_junk) == 1


def test_fast_rank_fallback_phrase_bonus():
    """整词短语加成：title 连续出现完整 query > 同样的 token 但分散出现。

    实体类查询（公司全名）在企查查/爱企查标题里是连续完整的，这是精确匹配
    强信号，必须盖过"五个 token 都命中但语序分散"的页面。
    """
    from app.services.orchestrator import _fast_rank_fallback

    contiguous = _mk("https://www.qcc.com/firm/abc.html")
    contiguous.title = "长沙市雨花区强大酒业商行 - 企查查"
    scattered = _mk("https://www.example.com/x")
    scattered.title = "长沙市雨花区酒业商行（强大）"  # 5 个 token 全中但非连续

    ranked = _fast_rank_fallback("长沙市雨花区强大酒业商行", [scattered, contiguous], top_n=2)
    assert ranked[0].url == contiguous.url
    assert (
        contiguous.extra["rank_score"] - scattered.extra["rank_score"] >= 3.0
    )  # token 分打平，差距全部来自短语加成


def test_fast_rank_fallback_phrase_whitespace_insensitive():
    """短语匹配吸收空白差异：query 带空格、正文不带也能命中。"""
    from app.services.orchestrator import _fast_rank_fallback

    r = _mk("https://changsha.city8.com/shopping/x")
    r.title = "湖南强大酒业详细地址"
    r.snippet = "位于高桥新一区"
    ranked = _fast_rank_fallback("湖南 强大酒业", [r], top_n=1)
    assert ranked[0].extra["rank_score"] >= 3.0  # "湖南强大酒业" 连续命中 title


def test_search_request_schema_contract():
    """管线引用的 SearchRequest 字段钉住（2026-09-16 事故：时效过滤误写成
    req.Filter.TimeRange 而 TimeRange 在 SearchRequest 上，冷路全量 500）。"""
    from app.schemas.search import SearchRequest

    req = SearchRequest(Query="测试")
    assert req.TimeRange == ""                 # 时效过滤的判定字段在请求顶层
    assert not hasattr(req.Filter, "TimeRange")
    # 管线用到的 Filter 字段
    for f in ("NeedContent", "NeedUrl", "Sites", "BlockHosts", "AuthInfoLevel"):
        assert hasattr(req.Filter, f), f
