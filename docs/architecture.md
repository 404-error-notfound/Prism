# Architecture

## Request flow (orchestrator)

`app/services/orchestrator.py`:

1. **Temporal policy** — async `_apply_temporal_policy()`, two stages:
   - **Regex fast-path**: `detect_temporal_query()` matches 今天/昨天/前天/这两天/近N天/上周/最新/today/latest etc.
   - **Semantic fallback**: if regex misses, `embedder.is_temporal_semantic()` does cosine similarity against a temporal anchor centroid (15 examples, `bge-small-zh-v1.5`, threshold 0.65). Catches "前阵子"/"这段时间"/"上礼拜" etc.
   - Either stage triggers `TimeRange=OneDay`, and raises `intent=news`（仅当 intent=auto）so the hybrid ranker weights recency heavily **without** discarding relevance. (旧行为是设 `sort_by=time` 纯按发布时间排序，会让当天发布的无关内容碾压相关结果，已废弃。`sort_by=time` 仍是用户可显式指定的逃生舱。)
   - 发给引擎的 query 还会经 `rewriter.strip_temporal_markers()` 剥离独立时效副词（最新/刚刚/今日，只删空白分隔的整 token，粘连词如"最新款"不动），避免它们当 BM25 词招来盖日期戳的农场/百科页；旧 `_safe_fetch` 拼今日日期的逻辑同因已删。
2. **Query rewrite** — if `QueryControl.QueryRewrite=true`, strip fillers + trailing punctuation (`app/services/rewriter.py`).
3. **Cache check** — Redis lookup keyed by `(tenant_id, query, top_n, recency_days)`.
4. **Hot index check** — if `HOT_INDEX_ENABLED=true`, queries ES `hot_news`; hits pass `apply_coverage_gate(min_coverage=0.5)` first (ES BM25 snippets contain generic words like "最新/政策" that fool a zero-coverage gate); ≥ `HOT_INDEX_MIN_HITS` survivors → skips SearXNG. See [Elasticsearch guide](elasticsearch.md).
5. **Tier 1 parallel fetch** — SearXNG (+ ChinaSo if `CHINASO_ENABLED`) + DDGS via `asyncio.create_task` + `as_completed`; circuit-breaker pipelined. Over-fetch `top_n×5` candidates to leave headroom for dedup/spam drops (饿汉式并行扩召回，受每引擎 8s 天花板约束，延迟中性).
6. **Tavily** (optional) — if `high_quality=true` and `TAVILY_API_KEY` set, runs alongside Tier 1.
7. **Dedup** — URL normalization + domain-level (`app/services/dedup.py`).
8. **Denylist + BlockHosts** — drops domains in `denylist:domains` Redis set or `Filter.BlockHosts`; one pipelined call.
9. **Spam gate (L0)** — `app/services/spam_gate.py`, runs after dedup/denylist, before rank. A 热搜拼盘 + B 乱码/深层 mobile 子域 + C 色情赌博词典；C 或 A∧B 硬丢，单信号软标记 `spam_soft`（ranker ×0.5）；合法热榜站 + `ranker.is_trusted_domain` 的 T1/T2 域豁免 A/B。reranker 拦不住关键词堆砌的农场页（snippet 真含查询词），故单设结构性闸门。
10. **Tier 2 fallback (post-filter)** — 触发点在去重/denylist/spam **之后**判（旧逻辑在过滤前判，垃圾占位导致不触发）：过滤后池 < 5 时经 `_tier2_backfill()` 取 Serper / DataForSEO / Volcengine (`TIER2_DEFAULT`)，并用同一套 dedup/denylist/spam 清洗后按 URL 合并，避免把刚滤掉的垃圾放回来。`TIER2_DEFAULT=none` 时为空操作。
11. **Sites whitelist** — if `Filter.Sites` set, keeps only matching domains.
12. **`published_at` enrichment** — 6-level fallback: engine → extra dict → JSON-LD → URL regex → relative text ("3天前") → None.
13. **commercial_score** — rule-based signals (domain, URL path, title, CTA phrases); adds tags like `["推广"]`.
14. **Three-layer relevance defense + 7-factor ranking** — `app/services/relevance_gate.py` + `ranker.rank_results()`:
    - **L1 coverage gate**: query tokens (jieba, CJK single-char filtered, stopwords stripped) — results with zero substring hits in `title+snippet` dropped before scoring. Hot path uses `min_coverage=0.5`.
    - **L2 coord factor**: `score × (0.5 + 0.5 × coverage)`; single-token queries exempt. Weak matches sink without hard cuts.
    - **7-factor weights** (`intent=auto`): recency(0.30) + source_trust(0.20) + BM25(0.20) + domain_reputation(0.15) + session_boost(0.10) + commercial_penalty(0.05). `news` boosts recency(0.50). `commercial` flips penalty to reward. `spam_soft`(单信号弱垃圾) 结果再 ×0.5。
    - **Navigational intent** ("官网/official site"): root-path URL (depth ≤1) +0.15; UGC/blog platforms (csdn/zhihu/baijiahao…) -0.20. 非导航查询则对纯首页(根路径) -0.15（`homepage_penalty`，要具体文章不要门户首页）。
    - **L3 reranker + 三道闸**: `bge-reranker-base` cross-encoder reranks top-`RERANKER_TOP_K`。绝对地板 `RERANKER_SCORE_FLOOR`(0.25) + 相对断层 `RERANKER_SCORE_MARGIN`(0.15, 阈=`max(floor, top-margin)`) + **塌缩闸门** `RERANKER_COLLAPSE_CEILING`(0.65)：整批最高 sigmoid 都低于 ceiling = 召回失败/全跑题，只留 `RERANKER_COLLAPSE_KEEP`(2) 条（治字典页/农场页中毒导致整批 dropped=0 全交付）。护栏 `min(3)` 回填不拉回低于绝对地板的真垃圾。
15. **Authority filter** — if `Filter.AuthInfoLevel > 0`, post-ranking trust/commercial filter.
16. **Post-rank parallel block** — Content fetch / reachability / summary share remaining time budget (floor 0.8s).
17. **ContentFormats conversion** — Markdown → text/html in place after fetch.
18. **Cache write** (async background task).

## Page content fetcher (`app/services/fetcher.py`)

Fetch order (fast static path first, browser last):
1. **Redis content cache** — `content:{sha1(url)}`, TTL 1h.
2. **trafilatura** — pure HTTP, 0.5-2s；与 robots.txt 检查并行（robots Redis 缓存 24h，T1 域名跳过）。多数静态页在此命中。
3. **readability-lxml fallback** — 复用 trafilatura 已下载的 HTML，无二次请求。
4. **Crawl4AI** — Playwright 单例，`fit_markdown`，处理 JS 渲染/反爬页。仅前两步抽空后兜底。robots 禁止时：内容仍返回但 `url=""`、`domain="来自网络搜索"`。

`_clean_markdown`: strips JS/CSS noise, bare URLs, markdown images, high-symbol-density lines. Content truncated to 20,000 chars; min 100 chars to be valid.

> **抓取成功率的天花板由源站可抓性决定，不是吞吐。** 实测：对聚合页偏多的 query，把 `CRAWL4AI_TIMEOUT_S`、`CRAWL4AI_CONCURRENCY`、`CONTENT_FETCH_BUDGET_S` 三者全部调大（如 8s/6/25s），`fetched/N` 纹丝不动（2/10→2/10），只让延迟从 ~15s 飙到 ~36s，弱机器还会被多浏览器标签压垮。原因：`wenku.baidu`/`baike.sogou`/`xueshu.baidu` 等是登录墙/反爬站，Headless 浏览器给再多时间也过不了；能抓的（如 `zhidao.baidu` 静态页）trafilatura 早已秒抓。**结论：默认保守（5s/3），别靠调吞吐救成功率；要提升只能从源侧入手（跳过死站省延迟、排序把可抓源提前），或对慢响应改用 `/api/search/stream` 流式返回。**

Shared httpx client (`_get_http_client`): singleton, 出网走 proxy-hub 隧道地址（`PROXY_TUNNEL_URL`，IP 轮换发生在 proxy-hub 侧，客户端无需感知）。`max_connections=50`, `keepalive=20`. Timeouts: connect=2s, read=3s.

## Engine adapters

All engines implement `BaseEngine` (`app/engines/base.py`): `async search(query, num, **kwargs) -> list[RawResult]`.

| Engine | File | Tier | Notes |
|---|---|---|---|
| SearXNG | `app/engines/searxng.py` | 1 | 3 instances; page N → instance N (IP isolation); auto-concurrent up to 3 pages |
| ChinaSo | `app/engines/chinaso.py` | 1 | 中国搜索权威源; JSON API + uid-cookie 预热 + proxy-hub 隧道代理; off by default (`CHINASO_ENABLED=false`) |
| DDGS | `app/engines/ddgs.py` | 1 | DuckDuckGo; disabled by default (`DDGS_ENABLED=false`) |
| Serper | `app/engines/serper.py` | 2 | Paid; requires `SERPER_API_KEY` |
| DataForSEO | `app/engines/dataforseo.py` | 2 | Paid; requires `DATAFORSEO_LOGIN` + `DATAFORSEO_PASSWORD` |
| Volcengine | `app/engines/volcengine.py` | 2 | Raises `QuotaExhaustedError` (code 10406) on quota; prefers `Summary` over `Snippet` |

## SearXNG 分片拓扑 + 代理池

### 一源一实例分片（现行）

`SEARXNG_SHARDS`（.env，格式 `引擎名=实例URL` 逗号分隔）非空时启用分片：baidu/bing/
quark/sogou/sogou wechat 各占一个 SearXNG 实例，gateway 注册为 5 个独立逻辑引擎
`searxng/<引擎名>`（`build_engines()` 工厂是拓扑/命名唯一来源）。

```
            ┌─ searxng-baidu  (engines=baidu)        ─┐
            ├─ searxng-bing   (engines=bing)         ─┤
prism Tier1 ├─ searxng-quark  (engines=quark)        ─┼─并发─> proxy-hub 隧道 8101 ─> 短效住宅 IP ─> 搜索引擎
            ├─ searxng-sogou  (engines=sogou)        ─┤
            └─ searxng-wechat (engines=sogou wechat) ─┘
```

- **容器零差异化**：5 实例同镜像同 `settings.template.yml`，"跑哪个源"完全由请求参数
  `engines=` 切片；compose 5 份 service 仅服务名不同（共享 `x-searxng-common` 锚点）。
- **隔离性**：每片独立实例/熔断（`circuit:searxng/<引擎>`）/ProxyQualityError（该片唯一
  引擎 unresponsive 即判，触发 proxy-hub refresh）；慢源或实例卡死只损失该源。
- **分页**：页留在片内（page N → 该片实例带 `pageno=N`），公式分母 = 总分片数，
  分片前后总 raw 量等价；翻页 IP 不重复 = forwarder 目标站防重轮换 + 池深 ≥ 页数(3)
  + `Connection: close`。
- **per-engine timeout 3.5s**（模板全部源）：SearXNG 聚合打包无流式，慢源到点即弃，
  聚合 ~3.7s 完成 < prism `ENGINE_TIMEOUT_S=5`（曾出现 5s 剪线 < 6s max_request_timeout
  的预算倒挂，最好情况也零结果）。
- settings 由 `searxng/settings.template.yml` 经 entrypoint 渲染（token 占位符来自 .env，
  为空拒绝启动）；limiter.toml 全实例共享。
- 留空 `SEARXNG_SHARDS` 回退旧全引擎多实例模式：`SEARXNG_URLS` 逗号分隔实例列表，
  page N → 实例 N（`SEARXNG_URL` 向后兼容，URLS 为空时用）。

### 代理 IP 池（独立 proxy-hub 服务）

代理池已从 gateway 拆分为独立的 **proxy-hub** 服务（独立仓库，通用设计可服务任意消费方）。
proxy-hub 按**项目隔离池**（admin 注册项目发 token，prism 用 "prism" 项目）。
gateway 进程内不维护代理池、不跑代理后台任务，只经 `app/services/proxy_client.py` 交互
（一律 Bearer 项目 token 鉴权，`PROXY_PROJECT_TOKEN`）：

- 每个搜索请求节流 `POST /activity`（≥10s/次）——proxy-hub 的按需补位与 QPS 自适应升档
  以此判断全局活跃度；
- searxng/chinaso 熔断跨阈值时 `POST /refresh` 换批新 IP。proxy-hub 先补新 IP 再淘汰旧批次，刷新期间池不空；内部锁防扎堆，巡检兜底。
- SearXNG / fetcher / ChinaSo 统一经 proxy-hub 隧道出网。隧道地址从 `PROXY_SERVICE_URL`
  推导（拓扑唯一事实源）：host 相同、端口固定换 8101，账密 `http://prism:<token>@host:8101`
  由 `PROXY_PROJECT_TOKEN` 注入；SearXNG 侧由 compose entrypoint 渲染 settings，
  gateway 侧由 `proxy_client._tunnel_urls()` 推导。

额外交互：

- **`ensure_warm()`**：空闲后首查前 GET `/status`；cold 则 POST `/refresh` 唤热，避免首查撞死池。
- **`ProxyQualityError` 重试**：SearXNG 200 但 0 结果且子引擎大面积无响应时（分片模式：该片唯一引擎无响应即判；非分片：过半子引擎），记熔断、异步 `/refresh`，并用 `PROXY_RETRY_EXTRA_BUDGET_S`（默认 5s）延长预算重试；最坏 ≈ 15s。

proxy-hub 侧（详见 proxy-hub 仓库 README）：
- 多副本主备：Redis 分布式锁（`SET NX PX`，按项目粒度）保证全局只有持锁副本调供应商 API，
  主宕机锁过期后备机自动补位；池状态共享在 Redis ZSET（`pool:{project}`，score=各 IP 的
  deadline），副本重启先续管不重复抽；
- 滚动补位：巡检每轮清自然过期 → 连续 2 轮探测失败才剔除 → 缺几个补几个，池保持固定长度
  （目标大小 = 档位 fetch_num 或项目 pool_size 覆盖），IP 用满 2min 生命周期，不提前热切；
- `alloc_min_ttl_s`：IP 剩余寿命 <30s 不再发放，避免"给到就死"。
- 冷热时段分档（peak/normal/offpeak 各自的巡检间隔/目标池大小/空闲阈值）、
  低谷 QPS 突增自动升档、按项目成本埋点（`dashboard:cost:{project}:*`）；
- 热配置 `GET/PUT /admin/config`（≤15s 生效，审计快照 5 版回滚）；
- 自建 HTTP 转发代理提供隧道：固定监听端口 8101，每连接挑上游 IP 时做**目标站防重轮换**
  （同一 IP 不连续打同一搜索引擎），上游失败自动换 IP 重试。

**为什么零窗口**：消费方（SearXNG 等）代理静态指向 proxy-hub 隧道地址（如 :8101），
换 IP 只发生在转发代理的上游选择里，隧道地址与进程都不动，prism 机器上不再有本机
forwarder，也没有 gost sidecar。
历史演进：重启 SearXNG（5-15s 预热）→ 本机 gost forwarder（重启 ~1.5s）→ gost API
热更新（0s）→ proxy-hub gost sidecar → proxy-hub 自建转发代理（目标站防重轮换）。

**为什么用 bing 不用百度探测**：百度对机房/代理 IP 的搜索接口风控极激进，几乎全判死、会误杀
可用 IP；bing（`cn.bing.com`，SearXNG 的 bing 引擎也走这里）宽容得多。**三态择优 + 巡检补 IP**
共同压低"IP 中途死→503→零结果"。残留（引擎超时/CAPTCHA）是廉价短效 IP 物理上限。

qg.net get 响应：`{"code":"SUCCESS","data":[{"proxy_ip":"112.x","server":"60.x:19239","deadline":"2026-06-18 14:38:23"}],"request_id":"..."}`
代理连接用 `server`（中转网关 ip:port），`proxy_ip` 仅展示用。`deadline` 是每 IP 绝对过期时刻（北京时间，
显式挂 `Asia/Shanghai` 比对，避免 UTC 容器算出 +8h 假有效期），proxy-hub 按各 IP deadline 到期剔除补位。
鉴权：proxy-hub 配账密则代理 URL 为 `http://key:pwd@server`、转发代理连上游时带该账密；
留空则裸连，须在 qg.net 后台把出网 IP 加白名单。

### SearXNG 引擎配置

当前：`SEARXNG_ENGINES=baidu,bing,sogou,quark,sogou wechat`（唯一来源是 .env；分片模式下
`SEARXNG_SHARDS` 决定实际切片，`SEARXNG_ENGINES` 为文档性全集）

- sogou wechat：公众号文章，时效/正文质量优于通用网页（扩召回主力之一，keyless 内置引擎，
  需在 SearXNG `settings.yml` 启用 `disabled: false`）
- bilibili 已移除：返回视频结果，对网页/新闻查询是纯噪音；Prism 只查 `SEARXNG_ENGINES` 列出的
  引擎，移除即不召（引擎在 SearXNG 仍 enabled 无妨）
- 360search 已移除：对机房/代理 IP 反爬激进，常返回空 body（`lxml ParserError: Document is empty`），
  且在其为唯一幸存引擎时返回的垃圾结果会被 L1 覆盖率闸门全杀 → 零结果
- DuckDuckGo 禁用：代理封锁，返回 449
- Bing 用 `cn.bing.com` 绕过代理限制

### ChinaSo（中国搜索）原生引擎

`CHINASO_ENABLED=true` 时作为 Tier1 之一并行调用（`app/engines/chinaso.py`）。聚合人民网/新华/
央视等权威源。接口（逆向自 newssearch SPA，非官方）：`GET /v5/general/v1/search?q=&pn=&ps=`，
返回 JSON。**按会话 cookie 风控**：无 `uid` cookie 命中 `{"status":2,"msg":"ip control"}` → 先 GET
allResults 页面种 cookie 再调接口；`uid` 模块级缓存复用，命中风控才刷新。出网经 proxy-hub 隧道
代理（`proxy_client.get_tunnel_proxy_url()`，与 SearXNG 同一出口路径）。默认关，需代理池有活 IP
验证拿到 `status:0` 后再开。详见 `docs/spikes/chinaso-toutiao-engines.md`。

CAPTCHA 配置（每个实例，settings.template.yml）：
- `SearxEngineCaptcha` 暂停 10s（原 900s——换 IP 不清按引擎名的挂起，挂起期换到干净 IP 也被跳过，
  已对齐 proxy-hub 换 IP 落地时间 ~2-10s）
- quark 引擎 override（`searxng/overrides/quark.py`，挂载进 searxng-quark）：上游 quark.py:89
  硬编码 `suspended_time=900` 覆盖 settings 的 10s，一次 CAPTCHA 挂起 15 分钟（2026-09-22 实测
  20 query 出勤 0/20，全是挂起 fast-fail）；override 去掉硬编码回落 10s，与 baidu override 同节奏
- `SearxEngineTooManyRequests` / `SearxEngineAccessDenied` 暂停 60s
- HTTP/2 禁用，Connection: close（每页必新连接，配合 forwarder 目标站防重轮换保证翻页 IP 不重复），
  不固定 UA（SearXNG 内置 useragents 池按请求随机）
- 每源 per-engine timeout 3.5s（慢源到点即弃，不拖垮整包）

可选 Cookie 加强：`engines[baidu/quark].headers.Cookie`（浏览器登录后复制，有效期约 30 天）。

## MCP server

`app/mcp/server.py` — tools: `web_search`, `search_feedback`.
- `stdio` (`MCP_TRANSPORT=stdio`) — Claude Desktop / Claude Code 直连
- `http` (`MCP_TRANSPORT=http`) — 挂载在 `/mcp/sse` + `/mcp/messages`

## Authentication & tenancy

`app/core/auth.py` — API keys as `sha256(key)` in Redis (`apikey:{hash} → tenant_id`). Per-tenant config: `tenant:{id}:config` hash. Quota: daily counter (`tenant:{id}:quota:{date}`, TTL 25h). Rate limit: sliding-window ZSET. `ADMIN_BOOTSTRAP_KEY` auto-registers admin tenant on first startup.

## Configuration reference

All settings in `app/core/config.py` via `pydantic-settings`. See `.env.example`.

### Core
- `CHINASO_ENABLED` — `false`（默认）。ChinaSo 原生引擎开关；按 IP 风控、经代理池出网，代理池有活 IP 验证可用后再开
- `TIER2_DEFAULT` — `none | dataforseo | serper | volcengine`
- `MCP_TRANSPORT` — `http` | `stdio`
- `HMAC_SALT` — production required (`openssl rand -hex 32`)
- `SEARCH_TOTAL_BUDGET_S` — fast-path 总耗时目标 (default `3.0`s)
- `RESPONSE_TIME_BUDGET_S` — 端到端响应预算 (default `10.0`s)；排序/正文/摘要余量从这里扣
- `ENGINE_TIMEOUT_S` — 单次引擎调用超时 (default `5.0`s)
- `PROXY_RETRY_EXTRA_BUDGET_S` — 代理质量失败额外预算 (default `5.0`s)；总耗时最坏 ≈ 15s
- `CONTENT_FETCH_BUDGET_S` — NeedContent 墙钟上限 (default `4.0`s；慢出网机房调大到 10)
- `SUMMARY_BUDGET_S` — NeedSummary 等待上限 (default `0.6`s；当前抽取式 <100ms，纯安全网)
- `CRAWL4AI_TIMEOUT_S` — Crawl4AI 单页渲染+抽取超时 (default `5.0`s；使用点现读，改 env 下次请求生效)
- `CRAWL4AI_CONCURRENCY` — 并发浏览器标签数 (default `3`；进程级单例，调整需重启)。**注**：这两个旋钮与预算调大都不涨抓取成功率（受源站可抓性限制），只增延迟，见上文 fetcher 章节

### Embedding & reranking
- `EMBED_MODEL` — bi-encoder for ranking factor 7 + temporal centroid. Empty = disabled. Recommended: `BAAI/bge-small-zh-v1.5`. On load, `warm_up()` pre-encodes 15 temporal anchors → normalized ndarray `_temporal_centroid`.
- `EMBED_NUM_THREADS` — `torch.set_num_threads()` (default `2`; × `EMBED_MAX_CONCURRENT` = total cores)
- `EMBED_MAX_CONCURRENT` — `asyncio.Semaphore` size (default `2`)
- `RERANKER_MODEL` — `none` | `BAAI/bge-reranker-base` (~1.1GB, 0.5-1.5s CPU; recommended for 4c8g) | `BAAI/bge-reranker-v2-m3` (~2.3GB, 2-4s CPU). Cold-path top-K only; fill absolute path when downloaded locally (e.g. `/app/models/bge-reranker-base`).
- `RERANKER_TOP_K` — default `20`
- `RERANKER_SCORE_FLOOR` — 精排绝对地板，sigmoid below dropped (default `0.25`; `0` = reorder only)
- `RERANKER_SCORE_MARGIN` — 相对断层，丢比当批最高 sigmoid 低超此值的结果 (default `0.15`; 阈=`max(floor, top-margin)`)
- `RERANKER_COLLAPSE_CEILING` — 塌缩闸门：整批最高 sigmoid 都低于此=召回失败/全跑题，只留 `RERANKER_COLLAPSE_KEEP` 条 (default `0.65`; `0`=off; 换 v2-m3 须重标定)
- `RERANKER_COLLAPSE_KEEP` — 塌缩成立时保留的头部条数 (default `2`)
- `CIRCUIT_TTL_S` — 引擎熔断窗口（秒），默认 9；3 次失败 within TTL 跳过。searxng/chinaso 失败时异步触发 `proxy_client.request_force_refresh`。

### SearXNG 拓扑配置
- `SEARXNG_SHARDS` — 一源一实例分片拓扑（推荐）：`引擎名=实例URL` 逗号分隔（引擎名可含空格，
  如 `sogou wechat=http://searxng-wechat:8080`）；非空时每片注册为独立逻辑引擎 `searxng/<引擎>`
- `SEARXNG_URLS` — 非分片模式的实例列表（向后兼容）：comma-separated instance URLs; page N → instance N
- `SEARXNG_URL` — 向后兼容，`SEARXNG_URLS` 为空时用

### 代理出网（proxy-hub 服务）
- `PROXY_SERVICE_URL` — proxy-hub 服务地址（**拓扑唯一事实源**：控制面调用 + 隧道地址推导——host 相同、端口固定换 8101；空 = 整条 proxy-hub 链路停用，直连出网）
- `PROXY_PROJECT_TOKEN` — proxy-hub 项目 token（注册 "prism" 项目获得），/activity /refresh /status 一律 Bearer 鉴权，隧道账密（`prism:<token>@host:8101`）也从它注入；空 = 被 403 拒绝
- 供应商/冷热分档/巡检等代理配置全在 proxy-hub 侧（其 env + `PUT /admin/config` 热配置）

### Elasticsearch
- `ES_URL` — default `http://localhost:9200`; Docker: `http://host.docker.internal:9200`
- `ES_USER` / `ES_PASSWORD` — 可选认证
- `ES_INDEX` — 热点索引（default `hot_news`，与 crawler 保持一致）
- `HOT_INDEX_ENABLED` — default `false`
- `HOT_INDEX_MIN_HITS` — default `3`
- `LOG_ES_ENABLED` / `LOG_ES_INDEX` / `LOG_ES_MIN_LEVEL` / `LOG_ES_BATCH_SIZE` / `LOG_ES_FLUSH_INTERVAL_S`

## Redis key conventions

| Pattern | Purpose |
|---|---|
| `apikey:{sha256}` | key → tenant_id |
| `tenant:{id}:config` | tenant settings hash |
| `tenant:{id}:quota:{date}` | daily request counter (TTL 25h) |
| `tenant:{id}:ratelimit` | sliding-window QPS ZSET |
| `circuit:{engine}` | circuit breaker failure counter |
| `denylist:domains` | domain blocklist set |
| `reputation:domain:{domain}` | trust float (ranker reads, feedback writes) |
| `session:signals:{session_id}` | last 5 behavior signals (JSON, TTL 1h) |
| `prefs:{uid_hash}` | user preference hash (HMAC-hashed user_id) |
| `cache:search:{hash}` | search result cache |
| `content:{sha1(url)}` | page content cache (TTL 1h) |
| `robots:{netloc}` | robots.txt cache (TTL 24h) |
