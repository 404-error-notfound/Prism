"""规则层 AIGC 检测（无 ML，目标 <5ms/条）。

检测三类信号：
  1. 句长方差过低（AI 行文句长高度均匀）
  2. AIGC 高频过渡模板词集中出现
  3. CTA 软广短语密度高

任意 ≥ 2 类信号触发 → 标记 [AI生成]，aigc_score 按命中数归一化。
"""
from __future__ import annotations

import re
import statistics

from app.schemas.search import SearchResult
from app.services.commercial import _SNIPPET_CTA_RE

# ── AIGC 高频过渡模板词 ─────────────────────────────────────────────
_AIGC_TEMPLATES: list[str] = [
    "首先，", "其次，", "再次，", "最后，",
    "总的来说", "综上所述", "综上", "总体而言",
    "值得注意的是", "需要指出的是", "不可否认", "毋庸置疑",
    "在此基础上", "与此同时", "值得一提的是",
    "总而言之", "由此可见", "不难发现", "不言而喻",
    "in conclusion", "it is worth noting", "in summary",
    "to sum up", "furthermore,", "moreover,",
]

_TEMPLATE_RE = re.compile(
    "|".join(re.escape(t) for t in _AIGC_TEMPLATES),
    re.IGNORECASE,
)

_SENT_SPLIT = re.compile(r"[。！？!?\n]+")


def _sentence_lengths(text: str) -> list[int]:
    lengths = []
    for s in _SENT_SPLIT.split(text):
        n = len(s.strip())
        if n > 5:
            lengths.append(n)
    return lengths


def _uniform_sentences(text: str) -> bool:
    """句长标准差 < 8 且句子数 ≥ 4 视为高度均匀（AI 特征）。"""
    lengths = _sentence_lengths(text)
    if len(lengths) < 4:
        return False
    try:
        return statistics.stdev(lengths) < 8
    except statistics.StatisticsError:
        return False


def _template_dense(text: str) -> bool:
    """AIGC 模板词命中 ≥ 2 处。"""
    it = _TEMPLATE_RE.finditer(text)
    return next(it, None) is not None and next(it, None) is not None


def _cta_dense(text: str) -> bool:
    """CTA 短语命中 ≥ 2 处。"""
    it = _SNIPPET_CTA_RE.finditer(text)
    return next(it, None) is not None and next(it, None) is not None


def check_aigc(snippet: str, content: str = "") -> tuple[float, bool]:
    """返回 (aigc_score, is_flagged)。

    aigc_score ∈ [0, 1]，is_flagged = True 表示 ≥ 2 类信号触发。
    """
    text = snippet + " " + content[:1000]
    s1 = _uniform_sentences(text)
    s2 = _template_dense(text)
    if s1 and s2:
        return round(2 / 3, 2), True
    s3 = _cta_dense(text)
    count = int(s1) + int(s2) + int(s3)
    return round(count / 3, 2), count >= 2


def apply_aigc_check(results: list[SearchResult]) -> list[SearchResult]:
    """对 SearchResult 列表原地执行 AIGC 检测，更新 aigc_score / tags / commercial_score。"""
    for r in results:
        score, flagged = check_aigc(r.snippet or "", r.page_content or "")
        r.aigc_score = score
        if flagged:
            if "[AI生成]" not in r.tags:
                r.tags.append("[AI生成]")
            # 软广 / 洗稿内容商业分小幅上调
            r.commercial_score = min(1.0, r.commercial_score + score * 0.3)
    return results
