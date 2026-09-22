"""L1 覆盖率闸门 + 协调因子 + 导航意图规则。

三层防线中的词面层（L1）：
  覆盖率闸门  — query 分词去停用词后，title+snippet 一个词都不命中的结果直接丢弃。
               拦截引擎填充噪音（平台导航页、无关新闻、单字字典页）。
  协调因子    — 排序得分 × (0.5 + 0.5 × coverage)，弱相关结果沉底。
  导航规则    — "X官网"类查询：根路径 URL 加 boost，UGC/博客平台施加惩罚。
               这类查询要的是站点本身，不是讨论站点的文章，文本相关性无法区分二者。

杀空策略：L1 杀空 → 返回空不回填（零覆盖意味着全是垃圾，"搜不到"比"答非所问"有用）。
单 token 查询：闸门退化为"命中即可"，协调因子恒为 1（无覆盖率可言）。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urlparse

from app.engines.base import RawResult

logger = logging.getLogger(__name__)

# 覆盖率计算用停用词：query 中的虚词/疑问词不算有效查询词
_STOPWORDS = frozenset({
    "的", "了", "在", "是", "和", "与", "及", "或", "把", "被", "对", "从", "向",
    "吗", "呢", "吧", "啊", "嘛", "之", "其", "该", "等", "这", "那", "些",
    "怎么", "怎样", "如何", "什么", "哪些", "哪个", "哪里", "多少", "为什么",
    "请问", "一下", "关于", "推荐", "查询", "搜索",
    "a", "an", "the", "of", "to", "in", "on", "for", "at", "by", "and", "or",
    "is", "are", "was", "what", "how", "where", "which", "why",
})

# 时间数字泛词：年份(2026)/月日(6月/18日)。农场页与真答案页都标这些，计入覆盖率只会让
# 跑题页蒙混（"2026"几乎命中所有页）。时效语义已由 time_range/recency 编码，此处剔除。
# 只剔"纯时间数字"，不碰 iPhone16 / 95号 这类无单位数字（fullmatch 保证整词匹配）。
_DATE_NUM_RE = re.compile(r"(?:19|20)\d{2}|\d{1,4}[年月日号]")


def _is_cjk(token: str) -> bool:
    return bool(token) and "一" <= token[0] <= "鿿"


def tokenize_query(query: str) -> list[str]:
    """query → 有效查询词列表（去停用词、去 CJK 单字、NFKC 小写归一）。

    CJK 单字不算有效查询词："戚风蛋糕"切出的"戚"会让字典站的单字释义页
    拿到覆盖率，这正是 BM25 单字中毒的覆盖率版本。全部被过滤时返回原始
    token（单字查询如"戚"本身仍可用）。
    """
    normalized = unicodedata.normalize("NFKC", query.lower())
    try:
        import jieba
        raw_tokens = [t.strip() for t in jieba.cut(normalized) if t.strip()]
    except Exception:
        # 无 jieba 降级：按符号切段后，CJK 段转字符 bigram（整段当单 token 会让
        # 无空格中文 query 永远无法命中），ASCII 段保留原词
        raw_tokens = []
        for seg in re.split(r"[\s/|，。、；：！？#%*&@（）()【】\[\]]+", normalized):
            if not seg:
                continue
            if _is_cjk(seg):
                raw_tokens.extend(
                    [seg] if len(seg) < 2
                    else [seg[i:i + 2] for i in range(len(seg) - 1)]
                )
            else:
                raw_tokens.append(seg)

    tokens = [
        t for t in raw_tokens
        if t not in _STOPWORDS
        and not (_is_cjk(t) and len(t) < 2)   # CJK 单字剔除
        and not (t.isascii() and len(t) < 2)  # ASCII 单字符噪声剔除
        and not _DATE_NUM_RE.fullmatch(t)     # 年份/月日时间数字剔除
        and re.search(r"[\w一-鿿]", t)  # 纯符号剔除
    ]
    if tokens:
        return tokens
    # 全部被过滤：退化为原始 token（单字查询兜底），仍去纯符号
    return [t for t in raw_tokens if re.search(r"[\w一-鿿]", t)]


def coverage(query_tokens: list[str], text: str) -> float:
    """text 命中了多少比例的查询词（子串匹配，NFKC 小写归一）。"""
    if not query_tokens:
        return 1.0
    normalized = unicodedata.normalize("NFKC", text.lower())
    hits = sum(1 for t in query_tokens if t in normalized)
    return hits / len(query_tokens)


def apply_coverage_gate(
    query: str,
    results: list[RawResult],
    min_coverage: float = 0.0,
    kill_empty: bool = False,
    trace=None,
) -> tuple[list[RawResult], list[float], int]:
    """L1 硬闸门：覆盖率不足的结果丢弃。返回 (存活结果, 对应覆盖率, 有效查询词数)。

    min_coverage：命中阈值。冷路默认 0（只丢零覆盖，弱相关交给协调因子和精排）；
    热路传 0.5（ES BM25 召回的热点新闻 snippet 常含"最新/政策"等泛词，必须命中至少
    一半查询词才允许跳过 SearXNG 直接出门）。

    kill_empty：过滤会清空非空结果集时的策略（与 min_coverage 正交）。
      False（冷路默认，返回给用户）：不清零，全部保留。全零覆盖多为 jieba 长复合词与
        措辞不匹配（词"个人所得税"匹配不上 snippet 的"个税/所得税"），清零=保证零结果；
        保留交给 L2 协调因子 + L3 语义精排判定（真垃圾由 L3 分数闸门按语义丢，更准）。
      True（热路）：照常清空——那是为降级到 SearXNG，不是返回给用户。
    """
    tokens = tokenize_query(query)
    if not tokens:
        return results, [1.0] * len(results), 0

    n = len(tokens)
    covs_all = [
        sum(1 for t in tokens if t in unicodedata.normalize("NFKC", f"{r.title} {r.snippet}".lower())) / n
        for r in results
    ]
    survivors: list[RawResult] = []
    covs: list[float] = []
    cov_drops: list[tuple[str, float]] = []  # (url, cov)：仅当本轮真丢弃时才写进 trace
    for r, c in zip(results, covs_all):
        if c > 0 and c >= min_coverage:
            survivors.append(r)
            covs.append(c)
        else:
            cov_drops.append((r.url, c))

    if not survivors and results and not kill_empty:
        logger.info(
            "relevance_gate: all %d zero-coverage, kept for L3 (分词/措辞不匹配) query=%r",
            len(results), query[:50],
        )
        return results, covs_all, n  # 全保留：不记 coverage 丢弃

    if cov_drops:
        logger.info("relevance_gate: dropped %d/%d zero-coverage results query=%r",
                    len(cov_drops), len(results), query[:50])
        if trace is not None:
            for url, c in cov_drops:
                trace.drop(url, "coverage", f"cov={c:.2f}<{min_coverage:g}", score=c)
    return survivors, covs, n


def coord_factor(cov: float, n_tokens: int) -> float:
    """协调因子：多词查询按覆盖率打折，单 token 查询不惩罚。"""
    if n_tokens < 2:
        return 1.0
    return 0.5 + 0.5 * cov


# ---------------------------------------------------------------------------
# 导航意图（"X官网"类查询）
# ---------------------------------------------------------------------------

_NAV_RE = re.compile(
    r"官网|官方网站|官方入口|官方网址|official\s+(?:web)?site|official\s+homepage",
    re.IGNORECASE,
)

# 导航查询场景下不可能是官网本身的 UGC/博客/内容农场平台
_NAV_UGC_DOMAINS = frozenset({
    "csdn.net", "zhihu.com", "jianshu.com", "baijiahao.baidu.com",
    "tieba.baidu.com", "zhidao.baidu.com", "wenku.baidu.com",
    "bilibili.com", "toutiao.com", "sohu.com", "163.com", "qq.com",
    "weibo.com", "douyin.com", "xiaohongshu.com", "360doc.com",
    "docin.com", "renrendoc.com", "doc88.com", "bendibao.com",
})

_NAV_ROOT_BOOST = 0.15
_NAV_UGC_PENALTY = 0.20
_NAV_UGC_DOMAINS_DOT = frozenset("." + d for d in _NAV_UGC_DOMAINS)


def is_navigational(query: str) -> bool:
    return bool(_NAV_RE.search(query))


def _path_depth(url: str) -> int:
    try:
        path = urlparse(url).path
    except ValueError:
        return 99
    return sum(1 for seg in path.split("/") if seg)


def nav_adjustment(url: str, domain: str) -> float:
    """导航查询下的得分修正：根路径 boost，UGC 平台惩罚。"""
    if domain in _NAV_UGC_DOMAINS or any(
        domain.endswith(d) for d in _NAV_UGC_DOMAINS_DOT
    ):
        return -_NAV_UGC_PENALTY
    if _path_depth(url) <= 1:
        return _NAV_ROOT_BOOST
    return 0.0


# 非导航查询：纯首页（根路径）结果降权——信息类查询要的是具体文章，不是门户首页
# （新华网/央视网/今日头条首页那批答非所问的根因）。软降权不删，避免误伤。
_HOMEPAGE_PENALTY = 0.15


def homepage_penalty(url: str) -> float:
    """非导航查询下，根路径首页结果的得分惩罚（与 nav_adjustment 的根路径 boost 对称）。"""
    return -_HOMEPAGE_PENALTY if _path_depth(url) == 0 else 0.0
