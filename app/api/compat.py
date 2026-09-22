"""Volcengine-compatible response conversion utilities."""
from __future__ import annotations

from app.schemas.search import (
    SearchResponse,
    SearchResult,
    VolcResponseMetadata,
    VolcResult,
    VolcSearchContext,
    VolcSearchResponse,
    VolcWebItem,
)
from app.services.extractive import extract_top_sentences

_AUTH_LEVEL_DES = {
    1: "\u975e\u5e38\u6743\u5a01",
    2: "\u6b63\u5e38\u6743\u5a01",
    3: "\u4e00\u822c\u6743\u5a01",
    4: "\u4e00\u822c\u4e0d\u6743\u5a01",
}
_SUMMARY_MAX = 500


def to_volc_response(resp: SearchResponse, search_type: str = "web") -> VolcSearchResponse:
    """Convert internal SearchResponse to Volcengine-compatible format."""
    web_items = [_to_volc_item(r, idx, resp.query) for idx, r in enumerate(resp.results)]
    return VolcSearchResponse(
        ResponseMetadata=VolcResponseMetadata(RequestId=resp.query_id),
        Result=VolcResult(
            ResultCount=len(web_items),
            WebResults=web_items,
            SearchContext=VolcSearchContext(
                OriginQuery=resp.query,
                SearchType=search_type,
            ),
            TimeCost=resp.latency_ms,
            LogId=resp.query_id,
            SummarizedAnswer="",
        ),
    )


def build_item_summary(r: SearchResult, query: str) -> str:
    """Build per-result Summary from page_content, falling back to Snippet."""
    body = (r.page_content or "").strip()
    if not body:
        return r.snippet
    return extract_top_sentences(query, body, max_sentences=3, max_chars=_SUMMARY_MAX)


def _to_volc_item(r: SearchResult, idx: int, query: str) -> VolcWebItem:
    auth_level, auth_des = _trust_to_auth(r.trust_score, r.commercial_score)
    summary = build_item_summary(r, query)
    content = (r.page_content or summary).strip()
    return VolcWebItem(
        Id=r.id,
        SortId=idx + 1,
        Title=r.title,
        SiteName=r.domain,
        Url=r.url,
        Snippet=r.snippet,
        Summary=summary,
        Content=content,
        PublishTime=r.published_at or "",
        RankScore=r.rank_score,
        AuthInfoDes=auth_des,
        AuthInfoLevel=auth_level,
        Reachable=r.reachable,
    )


def _trust_to_auth(trust: float, commercial: float) -> tuple[int, str]:
    if commercial >= 0.5:
        level = 4
    elif trust >= 0.85:
        level = 1
    elif trust >= 0.65:
        level = 2
    else:
        level = 3
    return level, _AUTH_LEVEL_DES[level]
