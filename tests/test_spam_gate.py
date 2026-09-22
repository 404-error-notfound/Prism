"""spam_gate 单测（纯函数，不依赖 redis/模型）。

用例取自真实返回的垃圾样本（央行降准降息那批内容农场页）。
"""
from __future__ import annotations

from app.engines.base import RawResult
from app.services.spam_gate import (
    _looks_gibberish,
    _signal_a,
    _signal_b,
    apply_spam_gate,
)


def _r(title: str, snippet: str, url: str) -> RawResult:
    return RawResult(title=title, url=url, snippet=snippet, engine="test")


# ── 信号 A：热搜拼盘 ────────────────────────────────────────────

def test_signal_a_headline_soup():
    """空格分隔的多条热搜碎片、查询词只埋在 1-2 条 → 命中。"""
    snippet = (
        "蓉超启幕 全球经济预警 团结协作 应对挑战 "
        "精准把握时机适时降准降息政策即将出台 俄央行警示 沙特王室豪掷千金"
    )
    tokens = ["央行", "降准", "降息", "政策"]
    assert _signal_a(tokens, snippet) is True


def test_signal_a_coherent_prose_not_flagged():
    """中文连贯正文几乎无空格 → 碎片数不足，不命中。"""
    snippet = "深夜传来的第一则重磅，指向央行两项创新型货币政策工具的进一步扩容。"
    tokens = ["央行", "降准", "降息", "政策"]
    assert _signal_a(tokens, snippet) is False


def test_signal_a_dense_match_not_flagged():
    """多碎片但查询词密度高（真·相关聚合）→ 不命中。"""
    snippet = "央行降准 央行降息 央行政策 货币政策 降准降息 央行新闻 政策解读"
    tokens = ["央行", "降准", "降息", "政策"]
    assert _signal_a(tokens, snippet) is False


# ── 信号 B：域名形状 ────────────────────────────────────────────

def test_gibberish_consonant_soup():
    assert _looks_gibberish("szzxcf") is True
    assert _looks_gibberish("bjxxhz") is True
    assert _looks_gibberish("hfteng") is True


def test_pinyin_domain_not_gibberish():
    assert _looks_gibberish("fuxiangtai") is False
    assert _looks_gibberish("jingmen") is False
    assert _looks_gibberish("baidu") is False


def test_signal_b_mobile_prefix():
    assert _signal_b("http://wap.jingmen.bjxxhz.cn/tnews/x.htm") is True
    assert _signal_b("http://3g.m.szzxcf.com/fnews/x.htm") is True


def test_signal_b_gibberish_sld():
    assert _signal_b("http://sansha.hfteng.com/lshow/x.htm") is True


def test_signal_b_clean_domain():
    assert _signal_b("https://www.gov.cn/zhengce/x.htm") is False
    assert _signal_b("https://finance.sina.com.cn/tech/x.shtml") is False


# ── 组合判定 ───────────────────────────────────────────────────

def test_lexicon_hard_drop():
    """C 高危词典命中即丢，不受白名单豁免。"""
    results = [
        _r("男生舔男生的光屁股的视频网站大全", "令人作呕的网站列表", "http://3g.m.szzxcf.com/x"),
        _r("图书官黄色视频令人瞩目的", "网络热议神秘视频", "http://3g.m.hqkj66.com/x"),
    ]
    kept, dropped = apply_spam_gate("央行降准降息政策", results)
    assert dropped == 2
    assert kept == []


def test_lexicon_porn_app_brand_hard_drop():
    """色情 App/片商品牌词（标题党 doorway 页塞热点关键词）命中即硬丢。"""
    results = [
        _r("麻豆传媒国产app免费追剧在线观看",
           "2026年6月最新LPR贷款市场报价利率公布,1年期3.00%", "https://food.yxlady.com/x.html"),
        _r("91短视频破解版", "央行降准降息政策最新解读", "https://abc.example.com/y"),
    ]
    kept, dropped = apply_spam_gate("2026年6月最新LPR", results)
    assert dropped == 2
    assert kept == []


def test_lexicon_gambling_streaming_brand_hard_drop():
    """赌博/色情导流品牌词（云开体育/老湿/黄游，真实漏过样本）命中即硬丢。"""
    results = [
        _r("云开体育app下载app官方正版", "安卓IOS双端同步更新 免费高清直播",
           "https://www.shuozhiwu.com/article/read/22102.html"),
        _r("老湿免费体验独家福利畅享沉浸式互动", "经典多样的职业",
           "https://bbs.21ic.com/wanews/article/x.htm"),
        _r("黄游下载免费版官方2026最新版", "iphone版安卓通用", "http://www.afsst-v.com/x"),
    ]
    kept, dropped = apply_spam_gate("世界杯赛程", results)
    assert dropped == 3
    assert kept == []


def test_content_farm_a_and_b_dropped():
    """A∧B：热搜拼盘 snippet + 乱码 mobile 域 → 硬丢。"""
    farm = _r(
        "9.1短视全免费超值体验",
        "蓉超启幕 全球经济预警 团结协作 应对挑战 "
        "精准把握时机适时降准降息政策即将出台 俄央行警示 沙特王室豪掷千金",
        "http://wap.jingmen.bjxxhz.cn/tnews/x.htm",
    )
    kept, dropped = apply_spam_gate("刚刚 央行降准降息政策", [farm])
    assert dropped == 1
    assert kept == []


def test_single_signal_b_soft_marked_not_dropped():
    """单独 B（乱码域但 snippet 连贯）→ 不丢，标记 spam_soft。"""
    r = _r(
        "央行货币政策工具扩容解读",
        "深夜传来的第一则重磅，指向央行两项创新型货币政策工具的进一步扩容。",
        "http://news.hfteng.com/article/123",
    )
    kept, dropped = apply_spam_gate("央行降准降息政策", [r])
    assert dropped == 0
    assert len(kept) == 1
    assert kept[0].extra.get("spam_soft") is True


def test_legit_result_kept_clean():
    """合法新闻页（白名单域 + 连贯正文）→ 保留且无软标记。"""
    r = _r(
        "深夜炸场!两大重磅消息突袭",
        "深夜传来的第一则重磅，指向央行两项创新型货币政策工具的进一步扩容。",
        "https://baijiahao.baidu.com/s?id=123",
    )
    kept, dropped = apply_spam_gate("央行降准降息政策", [r])
    assert dropped == 0
    assert len(kept) == 1
    assert kept[0].extra.get("spam_soft") is None


def test_legit_aggregator_whitelisted_against_a():
    """合法热榜站（tophub）snippet 是标题拼盘，A 会误判 → 白名单豁免，保留。"""
    r = _r(
        "今日热榜",
        "伊朗队被勒令离开美国 佛得角感谢中国 高考查分时间 山河郑州 "
        "醉驾撞死环卫工 周柯宇回复 过程全错结果全对 85花未入围",
        "https://tophub.today/c/news",
    )
    kept, dropped = apply_spam_gate("央行降准降息政策", [r])
    assert dropped == 0
    assert len(kept) == 1
    assert kept[0].extra.get("spam_soft") is None
