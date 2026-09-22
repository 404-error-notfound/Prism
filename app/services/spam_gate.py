"""SEO 内容农场 / 违规站点过滤 — L0 硬闸门（dedup/denylist 之后、rank 之前）。

reranker 拦不住关键词堆砌的内容农场：snippet 里真的反复出现查询词，cross-encoder
就给高分。覆盖率闸门同理。所以单设一道结构性 + 词典 + 域名形状的硬闸门。

三信号：
  A 热搜拼盘   — snippet 是空格分隔的多条热搜碎片，含查询词的碎片占比极低（关键词注入）。
  B 域名抛弃型 — 深层 mobile 前缀(wap./3g.) 或辅音乱码二级域（典型一次性农场域）。
  C 高危词典   — 色情/赌博/标题党词。命中即合规丢弃。

判定与动作：
  C            → 无条件硬丢（合规刚需）。
  A AND B      → 判为内容农场，硬丢。
  单独 A / 单独 B → 不丢，标记 r.extra["spam_soft"]=True，由 ranker 软降权（防误杀）。

白名单豁免（仅豁免 A/B，不豁免 C）：合法热榜/聚合站 + ranker 的 T1/T2 可信域。
返回 (kept, hard_dropped_count)。kept 内含被软标记但保留的结果。
"""
from __future__ import annotations

import logging
import re
import unicodedata

import tldextract

from app.engines.base import RawResult
from app.services.dedup import extract_domain
from app.services.relevance_gate import tokenize_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 信号 C：高危词典（保守、高精度——只列基本无歧义的词，避免误杀正经报道）
# ---------------------------------------------------------------------------
_LEXICON_RE = re.compile(
    r"黄色视频|色情|情色|裸聊|裸露|约炮|一夜情|成人(?:视频|电影|网站|片)|"
    r"av在线|三级片|光屁股|屁股的视频|无码|porn|xxx|"
    # 色情片商/导流 App 品牌词（高精度无歧义；常以"标题党 doorway 页"塞热点关键词）
    r"麻豆传媒|天美传媒|蜜桃传媒|果冻传媒|星空传媒|皇家华人|"
    r"国产自拍|嫩模私拍|自慰|抖阴|91短视频|91视频|swag台湾|黑料不打烊|老湿|黄游|"
    r"草榴|caoliu|香蕉(?:视频|社区|影视|tv)|"
    # 赌博：品牌/平台/运营术语。"棋牌"由 棋牌(?:游戏|平台) 放宽为裸词——doorway 标题
    # （"天元棋牌277.c.o.m…官网版"）把关键词堆在品牌与"官网"之间，proximity 匹配被绕开；
    # 正经报道几乎不含"棋牌"二字，FP（腾讯/社区棋牌室）极少且可接受，如误杀再收窄。
    # "彩集团"catch 3569彩集团；"bd(?:官方)?体育"catch bd官方体育（"官方"隔开 bd 与体育）。
    r"博彩|赌场|赌博|棋牌|彩集团|彩票集团|六合彩|私彩|时时彩|菠菜平台|百家乐|老虎机|"
    r"真人荷官|返水|洗码|bd(?:官方)?体育|云开体育|开云体育|沙巴体育|亚博体育|华体会|"
    r"乐鱼体育|爱游戏体育|kok体育",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 白名单：合法热榜/聚合站（snippet 天然是标题拼盘，A 会误判）
# ranker 的 T1/T2 域在运行时另行豁免（_is_whitelisted）
# ---------------------------------------------------------------------------
_LEGIT_AGGREGATORS = frozenset({
    "tophub.today", "zhihu.com", "weibo.com", "toutiao.com",
    "baidu.com", "news.baidu.com", "sina.com.cn", "163.com",
    "sohu.com", "qq.com", "ifeng.com", "thepaper.cn",
})

# ---------------------------------------------------------------------------
# 信号 A 阈值：热搜拼盘
# ---------------------------------------------------------------------------
# 中文正文 snippet 几乎无空格；热搜拼盘是空格分隔的标题碎片。碎片数 ≥ 此值
# 且含查询词的碎片占比 ≤ 阈值 → 判为关键词注入。
_A_MIN_FRAGMENTS = 6
_A_MAX_DENSITY = 0.30
_FRAG_SPLIT_RE = re.compile(r"[\s　]+")

# ---------------------------------------------------------------------------
# 信号 B：域名形状
# ---------------------------------------------------------------------------
# 一次性农场常用的深层 mobile 子域前缀
_THROWAWAY_SUB_LABELS = frozenset({"wap", "3g"})
_VOWELS = set("aeiou")


def _signal_a(query_tokens: list[str], snippet: str) -> bool:
    """热搜拼盘：snippet 切成多碎片、含查询词的碎片占比极低。"""
    if not snippet or not query_tokens:
        return False
    norm = unicodedata.normalize("NFKC", snippet.lower())
    fragments = [f for f in _FRAG_SPLIT_RE.split(norm) if f]
    if len(fragments) < _A_MIN_FRAGMENTS:
        return False
    hit = sum(1 for f in fragments if any(t in f for t in query_tokens))
    density = hit / len(fragments)
    return density <= _A_MAX_DENSITY


def _looks_gibberish(sld: str) -> bool:
    """二级域像辅音乱码：纯字母段含 ≥4 连续无元音串，或元音占比 < 0.2。

    中文拼音域名总有元音（fuxiangtai/jingmen）；szzxcf/bjxxhz/hfteng 这类
    无元音长辅音串是机器生成抛弃域的指纹。
    """
    letters = [c for c in sld if c.isalpha()]
    if len(letters) < 5:
        return False
    run = 0
    for c in letters:
        if c in _VOWELS:
            run = 0
        else:
            run += 1
            if run >= 4:
                return True
    vowel_ratio = sum(1 for c in letters if c in _VOWELS) / len(letters)
    return vowel_ratio < 0.2


def _signal_b(url: str) -> bool:
    """域名抛弃型：深层 mobile 前缀 或 辅音乱码二级域。"""
    try:
        ext = tldextract.extract(url)
    except Exception:
        return False
    sub = ext.subdomain or ""
    sub_labels = [s for s in sub.split(".") if s]
    # 深层 mobile 前缀：含 wap/3g 标签，或 ≥2 级子域（如 3g.m / wap.jingmen）
    if any(lbl in _THROWAWAY_SUB_LABELS for lbl in sub_labels) or len(sub_labels) >= 2:
        return True
    return _looks_gibberish(ext.domain or "")


def _is_whitelisted(domain: str) -> bool:
    """合法聚合站 + ranker T1/T2 可信域豁免 A/B（不豁免 C）。"""
    if domain in _LEGIT_AGGREGATORS or any(
        domain.endswith("." + d) for d in _LEGIT_AGGREGATORS
    ):
        return True
    from app.services import ranker  # 延迟导入避免循环依赖；tiers 在 startup 从 Redis 加载
    return ranker.is_trusted_domain(domain)


def apply_spam_gate(
    query: str, results: list[RawResult], trace=None
) -> tuple[list[RawResult], int]:
    """硬丢内容农场/违规站，软标记弱信号。返回 (kept, hard_dropped)。"""
    if not results:
        return results, 0
    query_tokens = tokenize_query(query)

    kept: list[RawResult] = []
    dropped = 0
    for r in results:
        text = f"{r.title} {r.snippet}"
        domain = extract_domain(r.url)

        # C：合规，无条件丢，不受白名单豁免
        if _LEXICON_RE.search(text):
            dropped += 1
            if trace is not None:
                trace.drop(r.url, "spam", "lexicon_C")
            continue

        if _is_whitelisted(domain):
            kept.append(r)
            continue

        a = _signal_a(query_tokens, r.snippet or "")
        b = _signal_b(r.url)
        if a and b:
            dropped += 1
            if trace is not None:
                trace.drop(r.url, "spam", "farm_A_and_B")
            continue
        if a or b:
            r.extra["spam_soft"] = True
        kept.append(r)

    if dropped:
        logger.info(
            "spam_gate: hard-dropped %d/%d query=%r",
            dropped, len(results), query[:50],
            extra={
                "event": {"category": "pipeline", "type": "spam_gate"},
                "metrics": {"dropped": dropped, "total": len(results)},
            },
        )
    return kept, dropped
