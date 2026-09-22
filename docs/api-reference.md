# API Reference

`POST /api/search` — Volcengine-compatible format. Existing Volcengine clients can switch to Prism by changing only the base URL.

## Request body (`SearchRequest`)

| Field | Type | Default | Description |
|---|---|---|---|
| `Query` | str | required | Search query (1–512 chars) |
| `SearchType` | str | `"web"` | `web` \| `web_summary` \| `image` (image not yet implemented) |
| `Count` | int | 10 | Number of results (1–50)；服务端下限 10，传 <10 一律按 10 算 |
| `TimeRange` | str | `""` | `OneDay` \| `OneWeek` \| `OneMonth` \| `OneYear` \| `""` = 30 days |
| `NeedSummary` | bool | false | When true, only keep results whose per-result `Summary` is non-empty. The `Summary` field is returned when available regardless of this flag. |
| `Filter.NeedContent` | bool | false | When true, fetch page content and only keep results whose final `Content` is non-empty. If fetched content is unavailable, Content falls back to Summary and is still considered non-empty. |
| `Filter.NeedUrl` | bool | false | When true, only keep results whose `Url` is non-empty. The `Url` field is returned when available regardless of this flag. |
| `Filter.Sites` | str | `""` | Comma-separated domain whitelist (e.g. `"github.com,arxiv.org"`) |
| `Filter.BlockHosts` | str | `""` | Comma-separated per-request domain blocklist |
| `Filter.AuthInfoLevel` | int | 0 | `0`=off `1`=非常权威(T1) `2`=正常权威(non-commercial) `3`=一般权威(low-commercial) |
| `QueryControl.QueryRewrite` | bool | false | Rule-based rewriter: strips fillers ("帮我搜一下", "tell me about" etc.) and trailing punctuation. No LLM. |
| `ContentFormats` | str | `"text"` | `text` / `markdown` / `html`. Pipeline always produces Markdown; conversion post-fetch. |
| `intent` | str | `"auto"` | `auto`\|`knowledge`\|`commercial`\|`news`\|`academic`. Switches ranker weight preset. |
| `user_id` | str | `""` | User identifier for session personalization |
| `sort_by` | str | `"hybrid"` | `hybrid`\|`time` |
| `high_quality` | bool | false | Also pulls Tavily (paid) in parallel. Requires `TAVILY_API_KEY`. |

## Filter implementation

- **`Filter.Sites`** — applied post-dedup; falls back to all results if whitelist produces nothing.
- **`Filter.BlockHosts`** — merged with `denylist:domains` Redis set; no extra Redis round-trip.
- **`Filter.AuthInfoLevel`** — post-ranking filter on `trust_score` + `commercial_score`. Level 1: `trust_score ≥ 0.85` (GitHub, Wikipedia, gov.cn, edu.cn). Level 2: removes `commercial_score ≥ 0.5`. Level 3: removes `commercial_score ≥ 0.8`. Never returns empty — falls back to unfiltered.
- **`Filter.NeedContent`** — triggers `fetch_pages_parallel()`; 8s batch timeout. Starts as soon as first engine returns, overlapping with second engine + dedup + ranking. HTTP: connect=2s, read=5s.

## Response body (`VolcSearchResponse`)

```json
{
  "ResponseMetadata": {
    "RequestId": "<uuid>", "Action": "WebSearch",
    "Version": "2025-01-01", "Service": "prism", "Region": "cn-self-hosted"
  },
  "Result": {
    "ResultCount": 10,
    "WebResults": [
      {
        "Id": "r1", "SortId": 1, "Title": "...", "SiteName": "github.com",
        "Url": "https://...", "Snippet": "...", "Content": "",
        "PublishTime": "2025-01-01", "RankScore": 0.82,
        "AuthInfoDes": "非常权威", "AuthInfoLevel": 1
      }
    ],
    "SearchContext": {"OriginQuery": "...", "SearchType": "web"},
    "SummarizedAnswer": "",
    "TimeCost": 423, "LogId": "<uuid>"
  }
}
```

`AuthInfoLevel` in response: 1=非常权威 2=正常权威 3=一般权威 4=一般不权威.
`Reachable`: compatibility field; `Filter.NeedUrl` filters empty Url values and does not perform reachability checks.

## SSE streaming endpoint

`GET /api/search/stream` — Query params: `q`, `top_n` (default 10), `time_range`, `intent`, `user_id`, `sort_by`. Yields: `plan → engine_done (per engine) → results`.
