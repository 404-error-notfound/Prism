"""rewriter 单测：填充词剥离 + 时效副词剥离 + 框架词剥离（纯函数）。"""
from __future__ import annotations

import pytest

from app.services.rewriter import (
    normalize_for_engine,
    rewrite,
    strip_framing_markers,
    strip_temporal_markers,
)

# ── strip_temporal_markers ─────────────────────────────────────

def test_strip_leading_temporal_marker():
    assert strip_temporal_markers("刚刚 央行降准降息政策") == "央行降准降息政策"
    assert strip_temporal_markers("最新 诺贝尔文学奖 得主") == "诺贝尔文学奖 得主"
    assert strip_temporal_markers("突发 某地 5.0级地震 震源深度") == "某地 5.0级地震 震源深度"


def test_glued_marker_not_stripped():
    """粘连词不动："最新款手机"里的"最新"、"最新金牌榜"里的"最新"。"""
    assert strip_temporal_markers("最新款手机") == "最新款手机"
    assert strip_temporal_markers("巴黎奥运会 最新金牌榜") == "巴黎奥运会 最新金牌榜"


def test_single_token_query_untouched():
    """单 token（含纯时效词）不删空。"""
    assert strip_temporal_markers("最新") == "最新"
    assert strip_temporal_markers("今日") == "今日"


def test_all_markers_fallback_to_original():
    """全是时效词被删空时回退原串。"""
    assert strip_temporal_markers("最新 今日") == "最新 今日"


def test_no_marker_noop():
    assert strip_temporal_markers("特斯拉 股价 走势") == "特斯拉 股价 走势"


# ── rewrite（既有填充词剥离不回归）─────────────────────────────

def test_rewrite_strips_leading_filler():
    assert rewrite("帮我搜一下 央行降息") == "央行降息"


def test_rewrite_trims_trailing_punct():
    assert rewrite("央行会降息吗？") == "央行会降息吗"


# ── strip_framing_markers（需要 jieba；本地无则跳过，CI/容器内跑）──────────
# jieba 切词依赖，与 ES 同样按"本地不可用即 skip"处理，不用 mock 替代。
jieba = pytest.importorskip("jieba")


def test_framing_strip_compositional_longtail():
    """核心 case：多约束长尾不再坍缩成单高频词。剥掉 适合/的/推荐 后召回变宽。"""
    assert (
        strip_framing_markers("适合油性敏感肌的平价洗面奶推荐")
        == "油性敏感肌平价洗面奶"
    )


def test_framing_strip_trailing_evaluative():
    assert strip_framing_markers("洗面奶怎么样") == "洗面奶"
    assert strip_framing_markers("油皮洗面奶推荐") == "油皮洗面奶"


def test_compound_word_not_split_stripped():
    """精确整词匹配：'推荐信'被 jieba 切成整词≠'推荐'，不误删。"""
    assert strip_framing_markers("推荐信怎么写") == "推荐信怎么写"


def test_content_type_word_preserved():
    """'测评/天梯图'等内容形态词不在框架词表 → 天然保留，不需保护名单。"""
    assert strip_framing_markers("显卡天梯图") == "显卡天梯图"
    assert "测评" in strip_framing_markers("油皮洗面奶测评")


def test_pure_framing_query_guard_fallback():
    """纯框架词查询剥到 <2 有效字符 → 回退原串，不删空。"""
    assert strip_framing_markers("推荐") == "推荐"
    assert strip_framing_markers("适合") == "适合"


def test_no_framing_marker_noop():
    assert strip_framing_markers("特斯拉股价走势") == "特斯拉股价走势"


# ── normalize_for_engine（时效 + 框架词组合 + 开关）────────────────────────

def test_normalize_composes_temporal_and_framing():
    """时效副词 + 框架词一起剥。"""
    assert normalize_for_engine("最新 油皮洗面奶推荐") == "油皮洗面奶"


def test_normalize_framing_off_keeps_framing_words():
    """framing=False（环境开关回滚）只剥时效，不剥框架词。"""
    assert normalize_for_engine("油皮洗面奶推荐", framing=False) == "油皮洗面奶推荐"
