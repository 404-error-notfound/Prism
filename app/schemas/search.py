"""Request / Response schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# TimeRange helper
# ---------------------------------------------------------------------------

def time_range_to_days(time_range: str) -> int:
    """Convert Volcengine TimeRange string to recency_days integer.

    OneDay → 1, OneWeek → 7, OneMonth → 30, OneYear → 365, "" → 30
    大小写不敏感：'oneday' 与 'OneDay' 等价，避免客户端大小写不一致时
    被静默当成默认 30 天。
    """
    _MAP = {"oneday": 1, "oneweek": 7, "onemonth": 30, "oneyear": 365}
    return _MAP.get(time_range.lower(), 30)


# ---------------------------------------------------------------------------
# Volcengine-compatible request schemas
# ---------------------------------------------------------------------------

class VolcFilter(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    NeedContent: bool = Field(False, validation_alias=AliasChoices("NeedContent", "need_content"))   # fetch full page content (sets require_detail=True)
    NeedUrl: bool = Field(False, validation_alias=AliasChoices("NeedUrl", "need_url"))  # verify result URLs are reachable
    Sites: str = ""             # comma-separated domain whitelist (e.g. "github.com,arxiv.org")
    BlockHosts: str = ""        # comma-separated per-request domain blocklist
    AuthInfoLevel: int = 0      # min authority: 0=off 1=非常权威 2=正常权威 3=一般权威


class VolcQueryControl(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    QueryRewrite: bool = Field(False, validation_alias=AliasChoices("QueryRewrite", "query_rewrite"))


class SearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_compat_fields(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "filter" in data and "Filter" not in data:
            data["Filter"] = data.pop("filter")
        if "query_control" in data and "QueryControl" not in data:
            data["QueryControl"] = data.pop("query_control")
        filter_data = data.get("Filter")
        if not isinstance(filter_data, dict):
            filter_data = {}
        else:
            filter_data = dict(filter_data)
        for top_key, filter_key in ((
            ("NeedUrl", "NeedUrl"),
            ("need_url", "NeedUrl"),
            ("NeedContent", "NeedContent"),
            ("need_content", "NeedContent"),
        )):
            if top_key in data and filter_key not in filter_data:
                filter_data[filter_key] = data.pop(top_key)
        if filter_data:
            data["Filter"] = filter_data
        if data.get("SearchType") == "web_summary" and "NeedSummary" not in data and "need_summary" not in data:
            data["NeedSummary"] = True
        return data

    # --- Volcengine-compatible fields (PascalCase) ---
    Query: str = Field(..., min_length=1, max_length=512)
    SearchType: Literal["web", "web_summary", "image"] = "web"
    Count: int = Field(10, ge=1, le=50)

    @field_validator("Count", mode="after")
    @classmethod
    def _enforce_min_count(cls, v: int) -> int:
        # Count 下限 10：top_n 太小（如 5）时，去重/过滤稍有损失响应就跌破调用方
        # 预期，deep research 类客户端会判定低质并反复重搜（2026-09-08 生产实测
        # Count=5 → 68% 请求不足 5 条 → 重搜风暴）。低于 10 一律按 10 算。
        return max(10, v)

    Filter: VolcFilter = Field(default_factory=VolcFilter, validation_alias=AliasChoices("Filter", "filter"))
    NeedSummary: bool = Field(False, validation_alias=AliasChoices("NeedSummary", "need_summary"))       # reserved — Prism doesn't generate AI summaries
    TimeRange: str = ""             # OneDay | OneWeek | OneMonth | OneYear | ""=30d
    QueryControl: VolcQueryControl = Field(default_factory=VolcQueryControl, validation_alias=AliasChoices("QueryControl", "query_control"))
    ContentFormats: str = "text"
    # --- Prism-specific extensions (snake_case, not in Volcengine API) ---
    intent: Literal["auto", "knowledge", "commercial", "news", "academic"] = "auto"
    user_id: str = Field("", max_length=128)
    sort_by: Literal["hybrid", "time"] = "hybrid"
    high_quality: bool = False      # Tavily channel (only if tenant.allow_tavily)

    # --- Convenience properties used by the internal pipeline ---
    @property
    def query(self) -> str:
        return self.Query

    @property
    def top_n(self) -> int:
        return self.Count

    @property
    def recency_days(self) -> int:
        return time_range_to_days(self.TimeRange)

    @property
    def require_detail(self) -> bool:
        return self.Filter.NeedContent


class SearchResult(BaseModel):
    id: str
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    domain: str
    commercial_score: float = 0.0
    aigc_score: float = 0.0
    quality_score: float = 0.5
    trust_score: float = 0.5
    rank_score: float = 0.5         # ranker 混合排序分（7 因子 × 协调因子）
    tags: list[str] = Field(default_factory=list)
    reachable: bool | None = None   # None = not yet checked
    verified: bool = False
    page_content: str | None = None  # Markdown full-page content (require_detail=true)
    source_engine: str = ""          # Which engine produced this result


class SearchResponse(BaseModel):
    query_id: str
    query: str
    commercial_tolerance: float = 0.8
    search_notes: list[str] = Field(default_factory=list)
    engines_used: list[str] = Field(default_factory=list)
    results: list[SearchResult]
    summary: str = ""               # NeedSummary=true 时的 LLM 综合摘要
    cached: bool = False
    latency_ms: int = 0


class FeedbackRequest(BaseModel):
    url: str
    useful: bool
    user_id: str = ""
    query_id: str = ""


# ---------------------------------------------------------------------------
# 火山引擎联网搜索 API 兼容响应格式
# /api/search 直接返回此格式，现有火山引擎项目只需更换 base URL 即可切换
# ---------------------------------------------------------------------------

class VolcWebItem(BaseModel):
    Id: str
    SortId: int
    Title: str
    SiteName: str = ""
    Url: str = ""
    Snippet: str = ""
    Summary: str = ""              # ~500 字页面精要；page_content 可用时取其前缀，否则回退 Snippet
    Content: str = ""              # require_detail=true 时填充
    PublishTime: str = ""
    LogoUrl: str = ""
    RankScore: float = 0.5
    AuthInfoDes: str = "正常权威"
    AuthInfoLevel: int = 2         # 1=非常权威 2=正常权威 3=一般权威 4=一般不权威
    Reachable: bool | None = None  # NeedUrl=true 时填充：true/false/null=未检查
    RuyiInfo: None = None


class VolcSearchContext(BaseModel):
    OriginQuery: str
    SearchType: str = "web"


class VolcResult(BaseModel):
    ResultCount: int
    WebResults: list[VolcWebItem] = Field(default_factory=list)
    SearchContext: VolcSearchContext
    TimeCost: int
    LogId: str
    CardResults: None = None
    SummarizedAnswer: str = ""     # NeedSummary=true 时返回的 LLM 摘要


class VolcResponseMetadata(BaseModel):
    RequestId: str
    Action: str = "WebSearch"
    Version: str = "2025-01-01"
    Service: str = "prism"
    Region: str = "cn-self-hosted"


class VolcSearchResponse(BaseModel):
    ResponseMetadata: VolcResponseMetadata
    Result: VolcResult
