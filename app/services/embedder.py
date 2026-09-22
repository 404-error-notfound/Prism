"""
bge-small-zh-v1.5 向量编码 + bge-reranker-v2-m3 语义重排

启动时调用 warm_up() 预热，后续每次请求只跑推理：
  编码（bi-encoder）  : query + N doc 批量 encode，~65-110ms（4核/2并发）
  重排（cross-encoder）: top-K 对，~300-800ms（4核）；RERANKER_MODEL 配置时生效

CPU 调优原则（4核示例）：
  torch.set_num_threads(2)       — PyTorch BLAS 内部线程/推理
  asyncio.Semaphore(2)           — 并发推理任务数
  两者相乘 = 物理核数，充分利用所有核且不超卖
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

# 必须在 import torch / sentence_transformers / tokenizers 之前设置：
# 老内核（CentOS7 libgomp）下 torch OpenMP 并行区、HF tokenizers 的 Rust 并行
# 在子线程（run_in_executor）里首次进入会死锁，导致 reranker 在 startup 卡死。
# 放代码里而非仅 docker-compose，保证 python -m / k8s / 裸 Dockerfile 各启动方式都不卡。
# setdefault：compose 或 .env 显式设置时不覆盖，仍可调。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder, SentenceTransformer

logger = logging.getLogger(__name__)

_embedder: "SentenceTransformer | None" = None
_reranker: "CrossEncoder | None" = None
_embed_sem: asyncio.Semaphore | None = None
_rerank_sem: asyncio.Semaphore | None = None
_temporal_centroid: "object | None" = None  # numpy ndarray, set by warm_up

_TEMPORAL_ANCHOR_TEXTS = [
    "今天发生了什么", "今日最新新闻", "这两天热点",
    "昨天发生的大事", "前天新闻", "近期动态",
    "最近圈子里在讨论什么", "上周科技热点", "这段时间有什么进展",
    "前阵子热议话题", "这几天娱乐新闻", "近两天发生了什么",
    "latest news today", "what happened recently", "breaking news this week",
]
_TEMPORAL_SIM_THRESHOLD = 0.65

# embed_for_ranking 固定区间归一化端点：bge-small-zh query-doc 余弦经验工作区间。
# 绝对映射（非批内 min-max），避免全无关批次虚顶 top1。上线后可按实际分布微调。
_SIM_LO = 0.35
_SIM_HI = 0.75


def _set_torch_threads(n: int) -> None:
    try:
        import torch
        torch.set_num_threads(n)
        logger.info("embedder: torch.set_num_threads(%d)", n)
    except ImportError:
        pass


async def warm_up(
    embed_model: str = "",
    reranker_model: str = "none",
    num_threads: int = 2,
    max_concurrent: int = 2,
) -> None:
    """启动时调用一次：设置线程数、加载模型、触发 JIT 预热。"""
    global _embedder, _reranker, _embed_sem, _rerank_sem, _temporal_centroid

    _set_torch_threads(num_threads)
    _embed_sem = asyncio.Semaphore(max_concurrent)
    _rerank_sem = asyncio.Semaphore(max_concurrent)

    loop = asyncio.get_event_loop()

    if embed_model:
        try:
            from sentence_transformers import SentenceTransformer

            def _load() -> "SentenceTransformer":
                m = SentenceTransformer(embed_model)
                m.encode("预热", normalize_embeddings=True)  # 触发 JIT / 缓存
                return m

            _embedder = await loop.run_in_executor(None, _load)
            logger.info("embedder ready: %s", embed_model)

            def _build_centroid(m: "SentenceTransformer") -> "object | None":
                vecs = m.encode(_TEMPORAL_ANCHOR_TEXTS, normalize_embeddings=True)
                c = vecs.mean(axis=0)
                norm = float((c @ c) ** 0.5)
                return c / norm if norm > 1e-9 else None

            _temporal_centroid = await loop.run_in_executor(None, _build_centroid, _embedder)
            logger.info("temporal centroid ready (%d anchors)", len(_TEMPORAL_ANCHOR_TEXTS))
        except Exception as e:
            logger.warning("embedder load failed (%s): %s — 语义因子已禁用", embed_model, e)

    if reranker_model and reranker_model != "none":
        try:
            from sentence_transformers import CrossEncoder

            def _load_re() -> "CrossEncoder":
                m = CrossEncoder(reranker_model)
                m.predict([("预热", "test")])  # 触发 JIT
                return m

            _reranker = await loop.run_in_executor(None, _load_re)
            logger.info("reranker ready: %s", reranker_model)
        except Exception as e:
            logger.warning("reranker load failed (%s): %s — 语义重排已禁用", reranker_model, e)


def is_embed_available() -> bool:
    return _embedder is not None


def is_reranker_available() -> bool:
    return _reranker is not None


async def _run_encode(texts: "str | list[str]", **kwargs) -> "object | None":
    if _embedder is None or _embed_sem is None:
        return None
    loop = asyncio.get_event_loop()
    async with _embed_sem:
        try:
            return await loop.run_in_executor(
                None,
                lambda: _embedder.encode(  # type: ignore[union-attr]
                    texts, normalize_embeddings=True, **kwargs
                ),
            )
        except Exception as e:
            logger.warning("encode error: %s", e)
            return None


async def encode_query(text: str) -> list[float] | None:
    """单条文本编码为归一化向量，用于 ES kNN 检索。模型未加载返回 None。"""
    vec = await _run_encode(text)
    return vec.tolist() if vec is not None else None  # type: ignore[union-attr]


async def embed_for_ranking(query: str, docs: list[str]) -> list[float] | None:
    """
    批量编码 query + docs，返回 query 与每条 doc 的余弦相似度（归一化至 [0,1]）。
    模型未加载时返回 None，ranker 跳过该因子。

    因向量已 L2 归一化，点积 == 余弦相似度，无需额外除法。
    """
    texts = [query] + docs
    vecs = await _run_encode(texts, batch_size=len(texts))
    if vecs is None:
        return None

    q_vec = vecs[0]
    sims = [float(q_vec @ d) for d in vecs[1:]]

    # 固定区间 clip 归一化（不用批内 min-max）：
    # min-max 是相对排序，全无关批次也会把余弦最高的强行抬成 1.0（虚顶）；
    # 当该因子在 ranker 里权重被动态抬高后，虚顶会把绝对不相关的结果推上前排。
    # bge-small-zh query-doc 余弦经验工作区间约 [0.35, 0.75]：低于 0.35 视为不相关→0，
    # 高于 0.75 视为强相关→1。全无关批次余弦集体落在低端，归一化后集体低分（绝对）。
    return [max(0.0, min(1.0, (s - _SIM_LO) / (_SIM_HI - _SIM_LO))) for s in sims]


async def is_temporal_semantic(query: str) -> bool | None:
    """余弦相似度与时效锚点向量比较，判断 query 是否具有时效意图。
    embedder 未加载时返回 None（调用方保持原判断）。"""
    if _temporal_centroid is None:
        return None
    vec = await _run_encode(query)
    if vec is None:
        return None
    return float(vec @ _temporal_centroid) >= _TEMPORAL_SIM_THRESHOLD


async def rerank(
    query: str, docs: list[str], top_k: int = 20
) -> tuple[list[int], list[float]] | None:
    """
    对 docs[:top_k] 进行 cross-encoder 重排。
    返回 (原始索引的新顺序, sigmoid 分数列表[原始索引])。
    分数仅覆盖被精排的前 k 条，供调用方做分数闸门；tail 条目无分数。
    模型未加载时返回 None（保持原顺序）。
    top_k 之后的条目直接追加在尾部，索引不变。
    """
    if _reranker is None or _rerank_sem is None:
        return None

    k = min(top_k, len(docs))
    pairs = [(query, doc) for doc in docs[:k]]
    loop = asyncio.get_event_loop()
    async with _rerank_sem:
        try:
            scores = await loop.run_in_executor(
                None,
                lambda: _reranker.predict(pairs),  # type: ignore[union-attr]
            )
        except Exception as e:
            logger.warning("rerank error: %s", e)
            return None

    import math
    sigmoid = [1.0 / (1.0 + math.exp(-float(s))) for s in scores[:k]]
    head = sorted(range(k), key=lambda i: scores[i], reverse=True)
    tail = list(range(k, len(docs)))
    return head + tail, sigmoid
