"""Hybrid result ranking — 7-factor async scorer.

因子与权重（有语义向量时，合计 = 1.0）：
  0.25  recency_score      — 指数衰减，半衰期 ~30 天
  0.15  source_trust       — 域名白名单先验可信度
  0.15  bm25               — 当次结果集 ad-hoc BM25
  0.15  domain_reputation  — Redis 行为反馈动态信誉
  0.10  session_boost      — 当前 Session 偏好信号
  0.05  commercial_penalty — 1 - commercial_score（广告惩罚）
  0.15  embedding_cosine   — bge-small-zh query-doc 余弦相似度

语义向量未加载时自动降级为 6 因子：
  recency 0.30 / source_trust 0.20 / bm25 0.20 / reputation 0.15 / session 0.10 / commercial 0.05
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from statistics import pstdev

from app.core.redis import get_redis
from app.engines.base import RawResult
from app.services.dedup import extract_domain
from app.services.relevance_gate import (
    apply_coverage_gate,
    coord_factor,
    homepage_penalty,
    is_navigational,
    nav_adjustment,
)
from app.services.session_signals import build_session_boost

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain trust tiers — loaded from Redis at startup
# ---------------------------------------------------------------------------

_T1_DOMAINS: set[str] = set()
_T2_DOMAINS: set[str] = set()
_ACADEMIC_DOMAINS: set[str] = set()
_NEWS_DOMAINS: set[str] = set()


async def load_domain_tiers_from_redis() -> None:
    """从 Redis 加载域名分级配置。"""
    global _T1_DOMAINS, _T2_DOMAINS, _ACADEMIC_DOMAINS, _NEWS_DOMAINS
    import asyncio
    r = get_redis()

    t1, t2, academic, news = await asyncio.gather(
        r.smembers("domain:tier:T1"),
        r.smembers("domain:tier:T2"),
        r.smembers("domain:tier:academic"),
        r.smembers("domain:tier:news"),
    )
    _T1_DOMAINS = {d.decode() if isinstance(d, bytes) else d for d in t1}
    _T2_DOMAINS = {d.decode() if isinstance(d, bytes) else d for d in t2}
    _ACADEMIC_DOMAINS = {d.decode() if isinstance(d, bytes) else d for d in academic}
    _NEWS_DOMAINS = {d.decode() if isinstance(d, bytes) else d for d in news}
    logger.info("ranker: loaded T1=%d T2=%d academic=%d news=%d domains",
                len(_T1_DOMAINS), len(_T2_DOMAINS), len(_ACADEMIC_DOMAINS), len(_NEWS_DOMAINS))


def _source_trust(domain: str) -> float:
    if domain in _T1_DOMAINS or any(domain.endswith("." + t) for t in _T1_DOMAINS):
        return 0.9
    if domain in _T2_DOMAINS or any(domain.endswith("." + t) for t in _T2_DOMAINS):
        return 0.7
    return 0.5


def is_trusted_domain(domain: str) -> bool:
    """domain 是否在 T1/T2 可信域（含子域后缀）。供 spam_gate 白名单豁免复用。"""
    return any(
        domain in tier or any(domain.endswith("." + d) for d in tier)
        for tier in (_T1_DOMAINS, _T2_DOMAINS)
    )


# ---------------------------------------------------------------------------
# Recency score — exponential decay
# ---------------------------------------------------------------------------

def _recency_score(published_at: str | None) -> float:
    if not published_at:
        return 0.5

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S+00:00",
                "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(published_at[:19], fmt[:len(published_at[:19])])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).days
            return max(0.05, math.exp(-age_days / 43.0))  # 半衰期 ~30 天
        except ValueError:
            continue
    return 0.5


# ---------------------------------------------------------------------------
# BM25 (ad-hoc corpus over current result set)
# ---------------------------------------------------------------------------

try:
    from rank_bm25 import BM25Okapi as _BM25Okapi
except ImportError:
    _BM25Okapi = None  # type: ignore[assignment]

# 模块导入时预热 jieba 字典（~5MB，同步 I/O），避免第一次请求时在事件循环里卡住
try:
    import jieba as _jieba_preload
    _jieba_preload.initialize()
    logger.info("jieba initialized (dict ready)")
except Exception as _e:
    _jieba_preload = None  # type: ignore[assignment]
    logger.warning("jieba pre-init failed, will use bigram fallback: %s", _e)


def _tokenize(text: str) -> list[str]:
    """中文用 jieba 分词，英文按空格切分，无依赖时降级为字符 bigram。

    CJK 单字 token 剔除：jieba 把"戚风蛋糕"切出单字"戚"后，字典站的单字
    释义页会靠这一个字拿到 BM25 高分（单字中毒）。全部被剔除时保留原
    token，单字查询（如搜"戚"本身）不受影响。
    """
    if _jieba_preload is not None:
        try:
            tokens = list(_jieba_preload.cut(text.lower()))
            filtered = [t for t in tokens if not ("一" <= t[0] <= "鿿" and len(t) < 2)]
            return filtered or tokens
        except Exception:
            pass
    words = text.lower().split()
    result: list[str] = []
    for w in words:
        if w.isascii():
            result.append(w)
        else:
            result.extend(w[i:i + 2] for i in range(len(w) - 1))
    return result or [text.lower()]


def _stddev(xs: list[float]) -> float:
    """总体标准差；<2 个样本返回 0（无方差可言）。"""
    return pstdev(xs) if len(xs) >= 2 else 0.0


def _bm25_scores(query: str, corpus: list[str]) -> list[float]:
    try:
        if _BM25Okapi is None:
            raise ImportError
        tokenized = [_tokenize(doc) for doc in corpus]
        bm25 = _BM25Okapi(tokenized)
        scores = bm25.get_scores(_tokenize(query))
        max_s = float(max(scores)) if max(scores) > 0 else 1.0
        return [float(s / max_s) for s in scores]
    except Exception as exc:
        logger.warning("bm25_scores fallback uniform=0.5: %s", exc)
        return [0.5] * len(corpus)


# ---------------------------------------------------------------------------
# Batch Redis fetches
# ---------------------------------------------------------------------------

async def _fetch_reputations(domains: list[str]) -> dict[str, float]:
    if not domains:
        return {}
    r = get_redis()
    pipe = r.pipeline()
    for d in domains:
        pipe.hgetall(f"reputation:domain:{d}")
    results = await pipe.execute()

    scores: dict[str, float] = {}
    for domain, data in zip(domains, results):
        if not data:
            scores[domain] = 0.5
            continue
        good = float(data.get("good", 0))
        bad = float(data.get("bad", 0))
        imp = good + bad
        if imp == 0:
            scores[domain] = 0.5
        else:
            ratio = (good + 10) / (imp + 20)
            confidence = min(1.0, imp / 100)
            scores[domain] = 0.5 * (1 - confidence) + ratio * confidence
    return scores


# ---------------------------------------------------------------------------
# Intent weight presets
# ---------------------------------------------------------------------------

# 7 因子顺序：recency, source_trust, bm25, reputation, session, commercial, embedding
_WEIGHTS_7: dict[str, tuple[float, ...]] = {
    "auto":       (0.25, 0.15, 0.15, 0.15, 0.10, 0.05, 0.15),
    "knowledge":  (0.05, 0.25, 0.15, 0.15, 0.05, 0.05, 0.30),  # 语义相似度主导
    "news":       (0.45, 0.10, 0.10, 0.10, 0.05, 0.05, 0.15),  # 时效主导
    "academic":   (0.05, 0.35, 0.15, 0.15, 0.05, 0.05, 0.20),  # 可信度 + 语义
    "commercial": (0.10, 0.05, 0.25, 0.10, 0.10, 0.25, 0.15),
}

# 6 因子回退（语义向量未加载）：recency, source_trust, bm25, reputation, session, commercial
_WEIGHTS_6: dict[str, tuple[float, ...]] = {
    "auto":       (0.30, 0.20, 0.20, 0.15, 0.10, 0.05),
    "knowledge":  (0.10, 0.30, 0.30, 0.20, 0.05, 0.05),
    "news":       (0.50, 0.15, 0.15, 0.10, 0.05, 0.05),
    "academic":   (0.10, 0.40, 0.25, 0.15, 0.05, 0.05),
    "commercial": (0.15, 0.10, 0.30, 0.10, 0.10, 0.25),
}

def _intent_trust_bonus(intent: str, domain: str) -> float:
    if intent == "academic" and (
        domain in _ACADEMIC_DOMAINS
        or domain.endswith(".edu")
        or domain.endswith(".edu.cn")
    ):
        return 0.15
    if intent == "news" and domain in _NEWS_DOMAINS:
        return 0.10
    return 0.0


# 因子的唯一顺序源：必须与 _WEIGHTS_7 元组顺序一致（6 因子表后补 emb=0.0）。
# base_w / score_v / 打分点积都从这里派生，避免多处字符串键各自维护、改动不同步。
_FACTOR_NAMES = ("rec", "trust", "bm25", "rep", "sess", "comm", "emb")
# comm 是惩罚项，只贡献死权重、不接收（避免广告混入时商业惩罚被异常放大）
_RECEIVER_FACTORS = tuple(f for f in _FACTOR_NAMES if f != "comm")


def _redistribute_weights(
    base_w: dict[str, float],
    score_v: dict[str, list[float]],
) -> dict[str, float]:
    """把本批结果里无区分度（方差≈0）因子的权重，按比例让渡给存活因子。

    当天的 recency、冷启动的 reputation/session 在某批结果里是常数，占着权重却不影响
    排序。检测方差≈0 的因子并将其权重按原比例摊给有区分度的存活因子，让相关性因子
    （bm25/embedding/trust）在这些场景下自动主导。严格阈值 1e-6 保证因子一旦有微弱
    区分度就立刻复活（如反馈数据到来后的 reputation）。返回新字典，不改入参。
    """
    w = dict(base_w)
    alive = [k for k in _RECEIVER_FACTORS if _stddev(score_v[k]) > 1e-6]
    alive_base = sum(w[k] for k in alive)
    if not alive or alive_base <= 1e-9:
        return w  # 无存活接收者，原样返回（不清零任何权重，避免权重泄漏）
    dead_w = sum(w[k] for k in _RECEIVER_FACTORS if k not in alive)
    if _stddev(score_v["comm"]) <= 1e-6:
        dead_w += w["comm"]
        w["comm"] = 0.0
    if dead_w > 1e-9:
        for k in alive:
            w[k] += dead_w * (w[k] / alive_base)
        for k in _RECEIVER_FACTORS:
            if k not in alive:
                w[k] = 0.0
    return w


# ---------------------------------------------------------------------------
# Reranker score gate
# ---------------------------------------------------------------------------

def _score_floor_keep(
    rr_ranked: list[float],
    floor: float,
    margin: float,
    collapse_ceiling: float = 0.0,
    collapse_keep: int = 2,
) -> int:
    """精排序（sigmoid 降序）下应保留的头部条数。

    塌缩闸门（最先判）：整批最高 sigmoid 都 < collapse_ceiling → 召回大概率失败/全跑题
      （好结果头部本应 >0.7）。此时 floor/margin 的"留 ≥3 条"护栏反而把一池跑题页全留下
      （dropped=0 根因）；改为只留最高 collapse_keep 条——"就这么点且可能不准"远胜"答非所问 10 条"。
      collapse_ceiling=0 时关闭，回到原 floor/margin 行为。

    floor/margin（非塌缩批）：实际阈值 = max(floor, top - margin)。
      floor  绝对地板，杀真垃圾（sigmoid 接近 0）。
      margin 相对断层，适配 bge-reranker-base 被压缩在 ~0.5-0.73 的 sigmoid——跑题页
             常 >0.5，固定绝对阈咬不住；按当批最高分相对下切，随 query 分布自适应。
    护栏：保留 ≥ min(3, k) 条，整批偏低时不因阈值失准清零（退为留精排最高 3 条）。
      但护栏只回填到"绝对地板 floor 允许的条数"——floor 明确切掉的真垃圾（rr < floor）
      不被 min(3) 拉回来（否则 floor=0.8 切到 2 条仍被回填成 3 条垃圾）。floor=0（地板
      关闭）时 floor_eligible = k，护栏行为与原实现完全一致，向后兼容。
    rr_ranked 已降序，达标者必为前缀，返回数量即可。
    """
    k = len(rr_ranked)
    if k == 0:
        return k
    top = rr_ranked[0]  # 已降序，最高分即首元素（省两次 O(k) max 扫描）
    # 塌缩闸门优先：召回失败的批次，护栏的"留 ≥3"会适得其反，直接收到 collapse_keep
    if collapse_ceiling > 0 and top < collapse_ceiling:
        return min(max(1, collapse_keep), k)
    if floor <= 0 and margin <= 0:
        return k
    cut = max(floor, top - margin) if margin > 0 else floor
    kept = sum(1 for s in rr_ranked if s >= cut)
    guard = min(3, k)  # 整批偏低时的保底，不因阈值失准清零
    floor_eligible = sum(1 for s in rr_ranked if s >= floor) if floor > 0 else k
    return max(1, min(max(kept, guard), floor_eligible))


# ---------------------------------------------------------------------------
# Main ranking function
# ---------------------------------------------------------------------------

async def rank_results(
    query: str,
    results: list[RawResult],
    sort_by: str = "hybrid",
    session_id: str = "",
    intent: str = "auto",
    top_n: int | None = None,
    trace=None,
) -> list[RawResult]:
    if not results:
        return results

    # L1 覆盖率闸门：零覆盖结果（引擎填充噪音）在排序前丢弃，杀空不回填
    results, coverages, n_query_tokens = apply_coverage_gate(query, results, trace=trace)
    if not results:
        return results

    if sort_by == "time":
        return sorted(results, key=lambda r: r.published_at or "", reverse=True)

    t0 = time.monotonic()
    domains = [extract_domain(r.url) for r in results]
    unique_domains = list(set(domains))

    # 三路并发：Redis 信誉 + Session boost + 语义向量（均为 I/O 或线程池）
    import asyncio
    from app.services import embedder as _emb

    corpus = [f"{r.title} {r.snippet}" for r in results]

    reputations, session_boosts, sem_scores = await asyncio.gather(
        _fetch_reputations(unique_domains),
        build_session_boost(session_id, unique_domains),
        _emb.embed_for_ranking(query, corpus),
    )

    bm25 = _bm25_scores(query, corpus)

    # 根据语义向量是否可用选择权重表（6 因子表补 emb=0.0，对齐 _FACTOR_NAMES）
    if sem_scores is not None:
        w = _WEIGHTS_7.get(intent, _WEIGHTS_7["auto"])
        logger.debug("ranker: 7-factor (embedding active), intent=%s", intent)
    else:
        w = _WEIGHTS_6.get(intent, _WEIGHTS_6["auto"]) + (0.0,)
        logger.debug("ranker: 6-factor (no embedding), intent=%s", intent)
    base_w = dict(zip(_FACTOR_NAMES, w))

    commercial_term = (lambda c: c) if intent == "commercial" else (lambda c: 1.0 - c)

    # 预计算每个因子的逐结果分数向量（键与 _FACTOR_NAMES 一致，供重分配与打分点积复用）
    n = len(results)
    score_v: dict[str, list[float]] = {
        "rec": [_recency_score(r.published_at) for r in results],
        "trust": [_source_trust(domains[i]) + _intent_trust_bonus(intent, domains[i]) for i in range(n)],
        "bm25": bm25,
        "rep": [reputations.get(domains[i], 0.5) for i in range(n)],
        "sess": [session_boosts.get(domains[i], 0.5) for i in range(n)],
        "comm": [commercial_term(getattr(r, "commercial_score", 0.0)) for r in results],
        "emb": [sem_scores[i] if sem_scores is not None else 0.0 for i in range(n)],
    }

    # 动态方差重分配：当天的 recency、冷启动的 reputation/session 等常数因子的权重，
    # 按比例让渡给有区分度的存活因子，使相关性因子在这些场景下自动主导。
    base_w = _redistribute_weights(base_w, score_v)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("ranker: weights=%s", {k: round(v, 3) for k, v in base_w.items()})

    # 导航意图（"X官网"类）：根路径 boost / UGC 平台惩罚。
    # 这类查询要的是站点本身，文本相关性区分不了"官网"和"讨论官网的文章"。
    navigational = is_navigational(query)

    # 协调因子逐结果预计算（打分与 detail 日志共用），弱覆盖结果按比例打折（单 token 查询不惩罚）
    coords = [coord_factor(c, n_query_tokens) for c in coverages]

    # 三元组 (score, 原始下标, result)：下标供 detail 日志反查 score_v / coords
    scored: list[tuple[float, int, RawResult]] = []
    for i, r in enumerate(results):
        score = sum(base_w[k] * score_v[k][i] for k in _FACTOR_NAMES)
        score *= coords[i]
        # 弱垃圾信号（单独 A 或 B 命中 spam_gate）软降权——沉底但不删
        if r.extra.get("spam_soft"):
            score *= 0.5
        if navigational:
            score += nav_adjustment(r.url, domains[i])
        else:
            score += homepage_penalty(r.url)
        # 混合分写入 extra，供 API 响应回填真实 RankScore
        r.extra["rank_score"] = round(score, 4)
        scored.append((score, i, r))

    # 初步排序（分差 < 0.02 时以 published_at 倒序作 tiebreaker）
    scored.sort(key=lambda x: (x[0], x[2].published_at or ""), reverse=True)

    # Cross-encoder 语义重排（可选，仅冷路 top-K）+ 分数闸门
    rr_by_orig: dict[int, float] = {}  # 原始下标 → sigmoid 分数
    reranked = False
    dropped = 0
    if _emb.is_reranker_available():
        from app.core.config import settings
        rerank_corpus = [f"{r.title} {r.snippet}" for _, _, r in scored]
        requested = max(1, top_n or settings.reranker_top_k)
        rerank_top_k = min(len(scored), max(requested, min(settings.reranker_top_k, requested * 2)))
        rerank_out = await _emb.rerank(query, rerank_corpus, rerank_top_k)
        if rerank_out is not None:
            new_order, rr_scores = rerank_out
            rr_by_orig = {scored[pos][1]: sig for pos, sig in enumerate(rr_scores)}
            scored = [scored[i] for i in new_order]
            reranked = True
            # 分数闸门：精排头部 sigmoid 低于阈值的丢弃（假相关 SEO 页）。
            # scored 已按精排序（sigmoid 降序）重排，rr 取回每条对应分数即为降序序列。
            k = len(rr_scores)
            rr_ranked = [rr_scores[new_order[j]] for j in range(k)]
            keep = _score_floor_keep(
                rr_ranked,
                settings.reranker_score_floor,
                settings.reranker_score_margin,
                settings.reranker_collapse_ceiling,
                settings.reranker_collapse_keep,
            )
            dropped = k - keep
            if dropped and trace is not None:
                collapsed = (settings.reranker_collapse_ceiling > 0
                             and rr_ranked[0] < settings.reranker_collapse_ceiling)
                reason = "collapse" if collapsed else "below_floor"
                for j in range(keep, k):
                    trace.drop(scored[j][2].url, "rerank", reason, score=rr_ranked[j])
            scored = scored[:keep] + scored[k:]
            if dropped:
                logger.info("ranker: score floor dropped %d/%d query=%r",
                            dropped, k, query[:50])
            logger.debug("ranker: reranker applied top_k=%d requested=%d", rerank_top_k, requested)

    weights_r = {k: round(v, 3) for k, v in base_w.items()}
    logger.info(
        "ranker.detail query=%r intent=%s n=%d reranked=%s dropped=%d weights=%s",
        query[:50], intent, len(scored), reranked, dropped, weights_r,
        extra={
            "event": {"category": "pipeline", "type": "rank_detail"},
            "metrics": {
                "intent": intent,
                "n": len(scored),
                "reranked": reranked,
                "dropped": dropped,
                "weights": weights_r,
                "top": [
                    {
                        "rank": rank,
                        "url": r.url[:100],
                        "title": (r.title or "")[:50],
                        "score": round(s, 3),
                        **{k: round(score_v[k][i], 3) for k in _FACTOR_NAMES},
                        "coord": round(coords[i], 3),
                        **({"rr": round(rr_by_orig[i], 3)} if i in rr_by_orig else {}),
                    }
                    for rank, (s, i, r) in enumerate(scored[:10], 1)
                ],
            },
        }
    )

    logger.debug("ranker: done latency_ms=%d n=%d",
                 int((time.monotonic() - t0) * 1000), len(scored))
    if trace is not None:
        for s, _, r in scored:
            trace.mark_survivor_score(r.url, s)
    return [r for _, _, r in scored]
