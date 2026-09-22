"""Rule-based query rewriter for QueryControl.QueryRewrite=true.

Cheap deterministic transforms that broadly improve SearXNG / DDG hit-rate
without an LLM round-trip:
  - Strip Chinese filler phrases ("帮我搜一下", "请问", "我想知道"...)
  - Normalize whitespace
  - Trim trailing punctuation that hurts BM25 ("?" "？" "。")

Returns the original query if no rule fires, so callers can compare and skip.
"""
from __future__ import annotations

import re

# Filler phrases stripped from the START of the query — most common in Chinese
# voice-input or chat-derived queries.
_LEADING_FILLERS = [
    r"帮我搜(?:一下|索)?",
    r"帮我查(?:一下|找|查)?",
    r"请问(?:一下)?",
    r"我想(?:知道|查询|了解|搜)",
    r"告诉我(?:一下)?",
    r"搜(?:一下|索|寻)",
    r"查(?:一下|找|询)",
    r"please\s+search\s+(?:for\s+)?",
    r"can\s+you\s+(?:search|find|tell\s+me)",
    r"search\s+for\s+",
    r"find\s+(?:me\s+)?",
    r"tell\s+me\s+(?:about\s+)?",
]
_LEADING_RE = re.compile(
    r"^\s*(?:" + "|".join(_LEADING_FILLERS) + r")\s*[,，:：]?\s*",
    re.IGNORECASE,
)

# Trailing modal particles + redundant punctuation
_TRAILING_RE = re.compile(r"[?？。.!！\s]+$")

_MULTISPACE_RE = re.compile(r"\s+")

# 时效副词：时效已由 time_range + intent=news + recency 衰减编码，这些词作为 BM25
# 查询词只会招来"盖了今天日期戳"的农场页/百科年份页（"刚刚/最新"那批零结果根因之一）。
# detect_temporal_query 命中的也正是这些词，所以剥离它们 = 把语义交给结构化时效。
_TEMPORAL_MARKERS = frozenset({
    "最新", "刚刚", "今日", "今天", "近期", "最近", "目前", "现在", "当前",
    "实时", "突发", "近日", "本周", "本月", "昨天", "昨日", "前天",
    "这两天", "这几天", "今早", "今晨", "最新消息",
})


def strip_temporal_markers(query: str) -> str:
    """移除空白分隔的独立时效副词（最新/刚刚/今日…）。

    只删完整的空白分隔 token，不碰粘连词——"最新款手机"里的"最新"不会被误删，
    "刚刚 央行降准降息政策" → "央行降准降息政策"。全被删空时回退原串。
    """
    parts = query.split()
    if len(parts) < 2:
        return query  # 单 token（含纯"最新"）不动，避免把整个 query 删空
    kept = [p for p in parts if p not in _TEMPORAL_MARKERS]
    return " ".join(kept) if kept else query


# 框架词：低信息量的"提问框架"词。多约束长尾 query（"适合油性敏感肌的平价洗面奶推荐"）
# 整串无空格，发给引擎时主导词「适合」会把它坍缩成单高频词，召回一池子字典/百科页。
# jieba 切词后剥掉这些框架词（"适合/推荐/的"）→ query 变宽（"油性敏感肌平价洗面奶"），
# 推荐/横评内容照样命中。只收高精度纯框架词；测评/横评/天梯图等"内容形态"词不在表内，
# 精确整词匹配天然保护它们（jieba 把"推荐信"切成整词≠"推荐"，也不会误删）。
_FRAMING_MARKERS = frozenset({
    # 句首/句中提问框架
    "适合", "求", "想找", "想要", "有没有",
    # 句尾征询框架
    "推荐", "有哪些", "哪个好", "哪种好", "怎么样", "怎么选",
    # 精度修饰词：低信息量，却恰好是 SEO doorway / AI 知识库垃圾页（腾讯ima、伊秀美库）
    # 的堆词重灾区。"光速标准精确数值"剥成"光速标准"、"地球赤道准确周长数值"→"地球赤道周长"。
    # 不含「官方」（导航意图关键词，is_navigational 依赖）、「标准」「数据」（常为核心实体，
    # 如"国家标准GB""财务数据"），避免误删主词；原串始终保留给排序，只拓宽召回。
    "准确", "精确", "正确", "完整", "详细", "数值",
    # 结构助词
    "的",
})


def strip_framing_markers(query: str) -> str:
    """jieba 切词后剥掉命中框架词表的整词 token，拼回。

    精确整词匹配——只删词表内的完整 token，不碰粘连词（"推荐信"/"测评"/"天梯图"）。
    唯一护栏：剥离后有效字符 <2 则回退原串（纯框架词查询如单搜"推荐"/"适合"不被删空）。
    只动发给引擎的 query；调用方保留原串给 coverage/BM25/rerank 做相关性判定。
    jieba 不可用时原样返回（降级不剥，安全）。
    """
    try:
        import jieba
        tokens = list(jieba.cut(query))
    except Exception:
        return query
    kept = "".join(t for t in tokens if t not in _FRAMING_MARKERS).strip()
    kept = _MULTISPACE_RE.sub(" ", kept)
    # 护栏：剥到几乎为空（有效字符 <2）说明整串本就是框架词，回退原串
    if len(re.sub(r"\s", "", kept)) < 2:
        return query
    return kept


def normalize_for_engine(query: str, framing: bool = True) -> str:
    """发给搜索引擎前的 query 归一化：剥时效副词 + （可选）剥框架词。

    与 strip_temporal_markers 同类——只拓宽召回，原串始终由调用方保留给排序。
    framing=False 时只做时效剥离（供 ENABLE_FRAMING_STRIP 一键回滚）。
    """
    q = strip_temporal_markers(query)
    if framing:
        q = strip_framing_markers(q)
    return q


def rewrite(query: str) -> str:
    """Apply all rewrite rules. Returns the cleaned query (always non-empty;
    falls back to the original if rewriting would produce an empty string)."""
    cleaned = _LEADING_RE.sub("", query).strip()
    cleaned = _TRAILING_RE.sub("", cleaned).strip()
    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    return cleaned or query
