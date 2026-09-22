"""BM25-based extractive summarization.

Fast, deterministic, hallucination-free summaries built by picking the
highest-scoring sentences from the source text against the query. Used for:

  1. Per-result `WebResults[i].Summary` — picks query-relevant sentences
     from that result's own page_content (or snippet when not fetched).
  2. Overall `Result.SummarizedAnswer` — picks top sentences across the
     top-K results and concatenates them into a 2-4 sentence answer.

Runs in <100ms for typical inputs (≤10 results, ≤200 sentences total).
"""
from __future__ import annotations

import logging
import re

from rank_bm25 import BM25Okapi

from app.schemas.search import SearchResult

logger = logging.getLogger(__name__)

# Split on Chinese 。！？ / English .!? followed by space/newline.
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？])(?=[^」』])|(?<=[\.\!\?])\s+|\n+")
_MIN_SENT_LEN = 10
_MAX_SENT_LEN = 300


def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    sents = _SENT_SPLIT_RE.split(text.strip())
    return [s.strip() for s in sents if _MIN_SENT_LEN <= len(s.strip()) <= _MAX_SENT_LEN]


def _tokenize(text: str) -> list[str]:
    """Char-level tokens work well for Chinese BM25; English falls back to
    word split. We use a hybrid: keep ASCII words, split CJK into chars."""
    text = text.lower()
    tokens: list[str] = []
    buf: list[str] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(ch)
        elif ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf))
                buf = []
    if buf:
        tokens.append("".join(buf))
    return tokens


def extract_top_sentences(
    query: str,
    text: str,
    max_sentences: int = 3,
    max_chars: int = 500,
) -> str:
    """Return up to `max_sentences` sentences from `text` most relevant to
    `query`, preserving their original order, capped at `max_chars`."""
    sents = split_sentences(text)
    if not sents:
        return text[:max_chars] if text else ""
    if len(sents) <= max_sentences:
        out = "".join(sents)
        return out[:max_chars]

    tokenized = [_tokenize(s) for s in sents]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))

    ranked = sorted(range(len(sents)), key=lambda i: scores[i], reverse=True)
    picked_idx = sorted(ranked[:max_sentences])
    out = "".join(sents[i] for i in picked_idx)
    return out[:max_chars]


def summarize_results(
    query: str,
    results: list[SearchResult],
    max_sentences: int = 4,
    max_chars: int = 400,
    top_k: int = 5,
) -> str:
    """Build an overall extractive answer across the top-K results."""
    if not results:
        return ""
    candidates: list[str] = []
    for r in results[:top_k]:
        body = (r.page_content or r.snippet or "").strip()
        candidates.extend(split_sentences(body))
    if not candidates:
        return ""
    if len(candidates) <= max_sentences:
        return "".join(candidates)[:max_chars]

    tokenized = [_tokenize(s) for s in candidates]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))

    ranked = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
    picked: list[str] = []
    seen_prefixes: set[str] = set()
    total_len = 0
    for i in ranked:
        if len(picked) >= max_sentences:
            break
        s = candidates[i]
        prefix = s[:20]
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        picked.append(s)
        total_len += len(s)
        if total_len >= max_chars:
            break
    return "".join(picked)[:max_chars]
