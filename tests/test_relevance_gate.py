"""L1 覆盖率闸门 + 协调因子 + 导航规则单测（不依赖 redis/模型）。"""
from __future__ import annotations

from app.engines.base import RawResult
from app.services.relevance_gate import (
    apply_coverage_gate,
    coord_factor,
    coverage,
    homepage_penalty,
    is_navigational,
    nav_adjustment,
    tokenize_query,
)


# ── homepage_penalty ───────────────────────────────────────────

def test_homepage_penalty_root_path():
    """门户首页（根路径）非导航查询下降权。"""
    assert homepage_penalty("https://www.news.cn/") < 0
    assert homepage_penalty("https://news.cctv.com/") < 0
    assert homepage_penalty("https://www.toutiao.com") < 0


def test_homepage_penalty_article_no_penalty():
    """有深路径的具体文章/词条不降权。"""
    assert homepage_penalty("https://news.cctv.com/china/") == 0.0
    assert homepage_penalty("https://baike.baidu.com/item/2026年/123") == 0.0
    assert homepage_penalty("https://www.gov.cn/zhengce/content_7047091.htm") == 0.0


def _r(title: str, snippet: str = "", url: str = "https://example.com/a") -> RawResult:
    return RawResult(title=title, url=url, snippet=snippet, engine="test")


# ── tokenize_query ─────────────────────────────────────────────

def test_cjk_single_char_filtered():
    """"戚风蛋糕"不能产出单字"戚"作为有效查询词。"""
    tokens = tokenize_query("戚风蛋糕 零失败家常烘焙教程")
    assert all(not ("一" <= t[0] <= "鿿" and len(t) < 2) for t in tokens)
    assert any("蛋糕" in t or "戚风" in t for t in tokens)


def test_single_char_query_fallback():
    """单字查询全部被过滤时退化为原始 token，不返回空。"""
    assert tokenize_query("戚") != []


def test_stopwords_removed():
    tokens = tokenize_query("电脑蓝屏怎么办 解决方法")
    assert "怎么" not in tokens
    assert "的" not in tokens


def test_symbols_stripped():
    """特殊符号混合查询提取出实义词。"""
    tokens = tokenize_query("手机#价格%2026*最新款")
    assert any("手机" in t for t in tokens)


def test_date_number_tokens_filtered():
    """年份/月日时间数字剔除：'2026''6月' 不计入有效查询词（农场页与真答案页都标）。"""
    assert "2026" not in tokenize_query("2026 油价 调整")
    t2 = tokenize_query("6月 高考 分数线")
    assert "6月" not in t2
    assert any("高考" in t for t in t2)


# ── coverage ───────────────────────────────────────────────────

def test_coverage_zero_for_unrelated():
    tokens = tokenize_query("南京 同城搬家 口碑推荐 报价透明")
    assert coverage(tokens, "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili 关于我们 联系我们") == 0.0


def test_coverage_full_for_exact():
    tokens = tokenize_query("宁波 居住证")
    assert coverage(tokens, "宁波市流动人口居住证申请指南") == 1.0


def test_dict_page_zero_coverage_after_single_char_filter():
    """字典站的"戚"字释义页对"戚风蛋糕"查询覆盖率必须为 0。"""
    tokens = tokenize_query("戚风蛋糕 零失败家常烘焙教程")
    assert coverage(tokens, "戚-汉字的艺术与中华姓氏文化荀卿庠整理 - 知乎 戚怎么读_戚的拼音") == 0.0


# ── apply_coverage_gate ────────────────────────────────────────

def test_gate_drops_zero_coverage():
    results = [
        _r("宁波流动人口居住证申请条件"),
        _r("哔哩哔哩下载中心"),
        _r("宁波居住证办理材料清单"),
    ]
    survivors, covs, n = apply_coverage_gate("宁波 流动人口居住证申请条件及材料", results)
    assert len(survivors) == 2
    assert all("居住证" in r.title for r in survivors)
    assert len(covs) == 2
    assert n >= 2


def test_gate_cold_path_keeps_all_when_would_empty():
    """冷路杀空保护：全零覆盖时不清零（多为分词/措辞不匹配），全部保留交给 L3 精排。"""
    results = [_r("广东二手车出口金融产品"), _r("居民境外收入补税130亿")]
    survivors, covs, _ = apply_coverage_gate("新冠疫苗最新加强针政策", results)
    assert len(survivors) == 2          # 不清零
    assert covs == [0.0, 0.0]           # 覆盖率 0，靠 L2 协调因子沉底 + L3 语义判定


def test_gate_hot_path_still_kills_all():
    """热路（kill_empty=True）仍杀空：那是为降级到 SearXNG，不是返回给用户。"""
    results = [_r("广东二手车出口金融产品"), _r("居民境外收入补税130亿")]
    survivors, covs, _ = apply_coverage_gate(
        "新冠疫苗最新加强针政策", results, min_coverage=0.5, kill_empty=True
    )
    assert survivors == []
    assert covs == []


def test_gate_disabled_for_pure_symbol_query():
    """纯符号/乱码 query 提不出有效 token 时闸门放行（token 兜底逻辑保留原文）。"""
    results = [_r("任意结果")]
    survivors, covs, _ = apply_coverage_gate("kjhfsd897&*#$vhjs", results)
    # 兜底 token 可能命不中，也可能闸门关闭——只要不抛异常即可
    assert isinstance(survivors, list)
    assert len(covs) == len(survivors)


def test_gate_min_coverage_blocks_generic_token_hits():
    """热路场景：snippet 含"最新/政策"泛词的零相关新闻必须被 min_coverage=0.5 拦截。"""
    results = [
        _r("前5个月居民境外收入补税约130亿元", "财政部最新政策显示……"),
        _r("新冠疫苗加强针接种最新政策通知", "国家卫健委发布新冠疫苗加强针政策"),
    ]
    survivors, _, _ = apply_coverage_gate("新冠疫苗最新加强针政策", results, min_coverage=0.5)
    assert len(survivors) == 1
    assert "疫苗" in survivors[0].title


# ── coord_factor ───────────────────────────────────────────────

def test_coord_full_coverage_no_penalty():
    assert coord_factor(1.0, 4) == 1.0


def test_coord_partial_coverage_discounts():
    assert abs(coord_factor(0.25, 4) - 0.625) < 1e-9


def test_coord_single_token_exempt():
    assert coord_factor(0.0, 1) == 1.0
    assert coord_factor(0.5, 0) == 1.0


# ── navigational ───────────────────────────────────────────────

def test_navigational_detection():
    assert is_navigational("字节跳动官网")
    assert is_navigational("北京市公安局出入境预约官网")
    assert is_navigational("Apple Official Website")
    assert is_navigational("八达岭长城 官方入口")
    assert not is_navigational("宁波 流动人口居住证申请条件")


def test_nav_root_url_boosted():
    assert nav_adjustment("https://www.bytedance.com/", "bytedance.com") > 0
    assert nav_adjustment("https://www.bytedance.com/zh", "bytedance.com") > 0


def test_nav_deep_path_not_boosted():
    assert nav_adjustment("https://www.bytedance.com/zh/news/2026/01/post", "bytedance.com") == 0.0


def test_nav_ugc_penalized():
    """讨论官网的 CSDN 博客在导航查询下必须被惩罚，即使是根路径。"""
    assert nav_adjustment("https://blog.csdn.net/abc/article/details/1", "blog.csdn.net") < 0
    assert nav_adjustment("https://zhuanlan.zhihu.com/p/123", "zhihu.com") < 0
    assert nav_adjustment("https://bj.bendibao.com/", "bj.bendibao.com") < 0
