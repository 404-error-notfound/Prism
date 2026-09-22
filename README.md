# Prism

自托管 LLM 网络搜索 MCP 服务器 —— 商业搜索 API 的低成本替代方案。

聚合多个搜索引擎（SearXNG、DDGS、Serper、DataForSEO、**火山引擎**），去重、过滤、排序后，通过 **MCP 协议**和 **REST API** 双通道提供结构化搜索结果。`POST /api/search` 直接兼容**火山引擎联网搜索 API** 格式，现有火山项目只需更换 base URL 即可接入，也可直接对接 Claude Desktop / Claude Code。

## 功能特性

- **多引擎聚合**：并行调用 Tier 1（SearXNG 多引擎 baidu/bing/sogou/quark/公众号 + 可选 ChinaSo 国家队权威源），over-fetch 后去重去垃圾；过滤后结果不足才降级 Tier 2（Serper / DataForSEO / **火山引擎**）
- **SearXNG 一源一实例分片 ×5 + 零窗口换 IP**：baidu/bing/quark/sogou/公众号各占一个实例，每源独占进程与连接（IP 隔离是结构性的）、独立熔断与超时控制——慢源或实例卡死只损失该源，其余源照常返回；翻页 IP 不重复由 proxy-hub 转发代理的目标站防重轮换保证（每连接挑未服务过该站的 IP）；换 IP 由独立 **proxy-hub 服务**（独立仓库，主备多副本）托管——详见下文「代理池与 proxy-hub 集成」
- **SearXNG 并发翻页**：根据 `Count` 动态计算所需页数（最多 3 页），所有页面通过 `asyncio.gather` 一次性并发请求，再按 URL 去重合并，打破"每引擎每页 10 条"的上限
- **电路熔断 + 代理联动**：引擎连续 3 次失败即跳过（`CIRCUIT_TTL_S`，默认 9s）；searxng/chinaso 熔断时异步通知 proxy-hub 换 IP；全员熔断且无其他引擎可用时 fail-open 回退全部分片（杜绝"确定性 0 结果"）。`ensure_warm` 避免空闲后首查撞死池。
- **去重 + 治理**：URL 规范化去重 + Redis 域名黑名单过滤 + 规则层广告分（`commercial_score`）+ `AuthInfoLevel` 权威度过滤
- **L0 反垃圾闸门**：reranker 拦不住关键词堆砌的 SEO 内容农场（snippet 真含查询词），故在排序前单设结构性闸门——A 热搜拼盘（snippet 多碎片、含查询词碎片占比极低）+ B 乱码/深层 mobile 子域 + C 色情赌博词典；C 或 A∧B 硬丢，单信号软降权；合法热榜站与 T1/T2 可信域豁免
- **三层相关性防线**：L1 覆盖率硬闸门（零覆盖结果排序前丢弃，**杀空保护**——会清空非空结果集时不清零，交给 L3 判定，避免长复合词措辞不匹配误杀）→ L2 协调因子（得分 × (0.5+0.5×覆盖率)，弱匹配沉底）→ L3 `bge-reranker-base` cross-encoder 精排 + sigmoid 分数闸门（假相关 SEO 页丢弃，头部全低于闸门时只留 top3 不原样吐垃圾）；配合 CJK 单字 token 过滤消除 BM25 单字中毒
- **7 因子混合排序**：recency(30%) + 域名可信度(20%) + BM25(20%) + 域名信誉(15%) + Session 行为增益(10%) + 广告惩罚(5%)；`EMBED_MODEL` 启用时加第 7 因子语义余弦相似度；导航意图（"X 官网"）自动识别并对根路径 URL / UGC 平台做加减分，非导航查询则对门户首页降权（要具体文章不要首页）
- **ES 热点快路**：`HOT_INDEX_ENABLED=true` 时优先查 Elasticsearch `hot_news` 索引（crawler 项目写入），命中 ≥ 3 条直接返回，跳过 SearXNG，热点查询响应 <50ms
- **`published_at` 6 级推断**：引擎值 → extra dict → JSON-LD → URL 路径正则 → 文本相对时间（"3 天前"）→ None
- **Redis 缓存**：按租户 + 查询缓存结果；时效查询（含"今天/最新"等词）或稀疏结果（< 3 条）自动缩短至 5 分钟 TTL；新闻类 10 分钟，其余 1 小时
- **两阶段时效检测**：Regex 快路（0ms）检测"今天/最新/实时/today/latest"等；未命中时用 `bge-small-zh-v1.5` 向量余弦相似度对 15 个时效锚点求均值质心，捕获"前阵子/这段时间/上礼拜"等方言表达；命中后只切换 `intent=news`（混合排序 recency 主导，而非纯按时间倒序），**不再强制 `TimeRange=OneDay` 硬时间过滤**（曾把常青/季节/医学类查询召回腰斩）——硬时间窗仅在调用方显式传 `TimeRange` 时生效；并从发给引擎的 query 剥离独立时效副词（最新/刚刚/今日，避免它们当 BM25 词招来盖日期戳的农场/百科页）
- **全文抓取（AI 可读）**：`Filter.NeedContent=true` 时调用 Crawl4AI 抓取页面正文，输出 LLM 友好的 Markdown，附在每条结果的 `Content` 字段
- **SSE 流式响应**：逐步推送 `plan → engine_done → results` 事件
- **多租户鉴权**：API Key（sha256 哈希）+ 每日配额 + 滑动窗口限流
- **请求链路追踪**：每个请求生成 `trace_id`，自动注入所有日志行，响应头回传 `X-Trace-Id`
- **Prometheus 指标**：`/metrics` 端点暴露缓存命中率、请求数、引擎错误率等
- **双传输模式**：MCP over STDIO（本地客户端）或 HTTP+SSE（多客户端部署）

## 快速开始

### 方式一：Docker Compose（推荐，Ubuntu 22.04 全量依赖）

```bash
# 1. 复制配置文件
cp .env.example .env
# 编辑 .env，至少填写 ADMIN_BOOTSTRAP_KEY 和 HMAC_SALT
# ADMIN_BOOTSTRAP_KEY=$(openssl rand -hex 32)
# HMAC_SALT=$(openssl rand -hex 32)

# 2. 启动所有服务（Prism + SearXNG 分片 ×5；代理隧道在 proxy-hub 侧）
# Redis 不在 compose 里，需外部可达（共享集群或本地实例），在 .env 配 REDIS_URL
docker compose up -d

# 3. 验证
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

### 方式二：本地裸机开发

```bash
# 需要可访问的 Redis（共享集群或本地实例，默认 localhost:6379）
pip install -e ".[dev]"
cp .env.example .env   # 配置 REDIS_URL 等
python -m app.main
```

### 启用全文抓取（NeedContent）

全文抓取依赖 `browser` extras，安装后无需额外配置：

```bash
pip install -e ".[dev,browser]"

# 首次使用需安装 Playwright 浏览器内核
crawl4ai-setup
# 或手动：playwright install chromium
```

## 代理池与 proxy-hub 集成

Prism 不自己维护代理池，而是把代理池拆分为独立服务 [proxy-hub](https://github.com/404-error-notfound/Prism-proxy)（通用共享代理池）。数据面只配一个隧道地址，控制面通过 API 驱动 proxy-hub 按需补位与换 IP。

```
SearXNG 分片实例 ×5（baidu/bing/quark/sogou/sogou wechat 一源一实例）──静态指向──> proxy-hub 隧道 8101（项目账密）──> qg.net 短效住宅 IP ──> 搜索引擎
                                                                                                  └─ baidu/quark 由 proxy-hub 按目标站自动路由到独享池（exclusive），池空落回共享
fetcher / ChinaSo ──────────────────────────────────────────────> 同上
```

### 控制面交互

prism 在 `app/services/proxy_client.py` 中封装了与 proxy-hub 的三类交互：

| 调用 | 触发时机 | 作用 |
|---|---|---|
| `POST /activity` | 每个搜索请求节流 ≥10s | 上报活跃，驱动补位与 QPS 升档 |
| `GET /status` → `POST /refresh` | `ensure_warm()` 空闲后首查 | 检查池可用性；cold 时唤热补新 IP |
| `POST /refresh` | 熔断阈值 / `ProxyQualityError` | 强制换批：先补新 IP 再淘汰旧批次，无空窗期 |

三个调用自动携带当前请求的 `query_id`（`proxy_client` 读请求上下文 ContextVar，调用方零改动），proxy-hub 侧日志落同名字段——`proxyhub_logs` 按 `query_id` 检索即得"哪个查询触发了换批/唤热/活跃"，跨链路排查的确定性锚点。

### 数据面隧道

消费方代理统一指向：

```
http://prism:<PROXY_PROJECT_TOKEN>@<proxy-hub-host>:8101
```

- 每连接按目标站防重轮换挑上游 IP（同一 IP 不连续打同一搜索引擎）。
- SearXNG、ChinaSo、`Filter.NeedContent=true` 的 fetcher 全部走这个地址。
- 旧版的本机 gost forwarder 已退休，`gost/` 目录仅作历史留档。

### 风控重试预算

`ProxyQualityError` 会记熔断、异步 `/refresh`，并用 `PROXY_RETRY_EXTRA_BUDGET_S` 延长预算重试；最坏总耗时 ≈ 15s。

配置项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROXY_SERVICE_URL` | `""` | proxy-hub 拓扑唯一事实源（控制面 + 隧道推导：host 相同、端口换 8101），支持逗号分隔多副本 failover |
| `PROXY_PROJECT_TOKEN` | `""` | "prism" 项目的 Bearer token + 隧道账密来源；空则 activity/status/refresh 被拒 |
| `PROXY_RETRY_EXTRA_BUDGET_S` | `5.0` | 代理质量失败（验证码/风控 0 结果）的额外重试预算 |
| `CIRCUIT_TTL_S` | `9` | 引擎熔断窗口（秒）|
| `ENGINE_TIMEOUT_S` | `5` | 单次引擎调用超时 |

## 项目结构

```
prism/
├── app/
│   ├── core/           # config, auth, logging, validator
│   ├── engines/        # SearXNG, DDGS, Serper, DataForSEO, Volcengine adapters
│   ├── governance/     # AIGC detection, governance path routing
│   ├── mcp/            # MCP server (stdio / HTTP+SSE)
│   ├── services/       # orchestrator, ranker, fetcher, embedder, hot_index,
│   │                   # dedup, rewriter, proxy_client, log_shipper …
│   └── main.py
├── docs/               # 详细参考文档（见下方「参考文档」）
├── scripts/            # create_api_key.py, seed_denylist.py
├── searxng/            # settings.template.yml（settings 唯一模板）+ entrypoint.sh / render-settings.sh（启动渲染隧道列表）+ limiter.toml
├── gost/               # 历史留档：早期本机 forwarder 的 Dockerfile/gost.yml（隧道化后 compose 不再引用）
├── tests/
├── docker-compose.yml
├── .env.example
└── CLAUDE.md           # Claude Code 工作指南（引用 docs/ 各文件）
```

## 接入 Claude

### STDIO 模式（Claude Desktop / Claude Code）

在 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "prism": {
      "command": "python",
      "args": ["-m", "app.main"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "REDIS_URL": "redis://localhost:6379/0",
        "SEARXNG_URL": "http://localhost:8080"
      }
    }
  }
}
```

### HTTP+SSE 模式

服务启动后（`MCP_TRANSPORT=http`），MCP 端点为：

| 端点 | 说明 |
|---|---|
| `GET /mcp/sse` | SSE 长连接入口 |
| `POST /mcp/messages` | 消息发送 |

## API 端点

所有 `/api/*` 端点需要在请求头携带 API Key：
```
Authorization: Bearer <your-api-key>
```

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/search` | **火山引擎兼容**标准搜索，返回 `VolcSearchResponse` JSON |
| `GET` | `/api/search/stream` | SSE 流式搜索，参数通过 query string 传递 |
| `POST` | `/api/fetch` | 抓取单个 URL 正文，返回 Markdown |
| `POST` | `/api/feedback` | 提交搜索结果有用/无用反馈 |
| `GET` | `/health` | 存活探针 |
| `GET` | `/ready` | 就绪探针（检查 Redis + SearXNG） |
| `GET` | `/metrics` | Prometheus 指标 |
| `GET` | `/docs` | Swagger UI（仅 `APP_ENV=dev`） |

### 管理员端点（需要 admin 租户 Key）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/admin/keys` | 创建新租户 API Key |
| `POST` | `/admin/denylist` | 增删黑名单域名（body `{"domains": [...], "action": "add"\|"remove"}`）|
| `GET` | `/admin/denylist` | 列出黑名单域名 |
| `POST` | `/admin/circuit/reset/{engine}` | 手动重置引擎熔断器 |
| `GET` | `/admin/engines` | 引擎时延/存活率/熔断状态（近 1h）|

## 搜索参数

`POST /api/search` 请求体为**火山引擎联网搜索 API 兼容格式**：

```json
{
  "Query": "claude code mcp",
  "SearchType": "web",
  "Count": 10,
  "TimeRange": "OneWeek",
  "NeedSummary": false,
  "Filter": {
    "NeedContent": false,
    "NeedUrl": false,
    "Sites": "",
    "BlockHosts": "",
    "AuthInfoLevel": 0
  },
  "QueryControl": {
    "QueryRewrite": false
  },
  "ContentFormats": "text",
  "intent": "auto",
  "user_id": "u1",
  "sort_by": "hybrid"
}
```

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `Query` | string | 必填 | 搜索词，最长 512 字符 |
| `SearchType` | string | `web` | `web` \| `web_summary` \| `image`（image 未实现）|
| `Count` | int | 10 | 返回结果数，最大 50；服务端下限 10，传 <10 一律按 10 算 |
| `TimeRange` | string | `""` | `OneDay` \| `OneWeek` \| `OneMonth` \| `OneYear` \| `""`=30 天；含时效词时自动覆盖为 `OneDay` |
| `NeedSummary` | bool | false | 开启 BM25 抽取式摘要（无 LLM，无 API Key，始终可用）；从 top-5 结果中抽取关键句，结果返回在 `Result.SummarizedAnswer` 字段，延迟 <100ms |
| `Filter.NeedContent` | bool | false | 抓取各结果页面正文，返回 `Content` Markdown 字段；首次抓取约 1-3s（静态页）或 5-12s（JS 渲染页），重复请求命中 Redis 缓存近乎零延迟 |
| `Filter.NeedUrl` | bool | false | 校验结果 URL 是否可达 |
| `Filter.Sites` | string | `""` | 逗号分隔的域名白名单（如 `"github.com,arxiv.org"`）|
| `Filter.BlockHosts` | string | `""` | 逗号分隔的请求级域名黑名单 |
| `Filter.AuthInfoLevel` | int | 0 | 权威度过滤：`0`=关闭 `1`=非常权威(T1 domains) `2`=正常权威(非商业) `3`=一般权威(低商业) |
| `intent` | string | `auto` | Prism 扩展：`auto` \| `knowledge` \| `commercial` \| `news` \| `academic` |
| `user_id` | string | `""` | Prism 扩展：用户标识，用于 Session 个性化 |
| `sort_by` | string | `hybrid` | Prism 扩展：`hybrid`（混合评分）或 `time`（纯时间排序）|
| `high_quality` | bool | false | Prism 扩展：同时调用 Tavily（付费）并行补充高质量结果，需配置 `TAVILY_API_KEY` |

`Filter.NeedContent=true` 时每条结果新增 `Content` 字段（火山兼容名）：

```json
{
  "Content": "# 文章标题\n\n正文 Markdown..."
}
```

### 权威度过滤说明（`Filter.AuthInfoLevel`）

- **Level 1（非常权威）**：仅保留 `trust_score ≥ 0.85` 的 T1 域名（如 GitHub、Wikipedia、gov.cn、edu.cn）
- **Level 2（正常权威）**：过滤掉明显商业结果（`commercial_score ≥ 0.5`）
- **Level 3（一般权威）**：过滤掉高度商业结果（`commercial_score ≥ 0.8`）
- 若过滤后结果为空，自动回退到未过滤结果，避免空响应

## `/api/fetch` 深度抓取

单独抓取任意 URL 的正文：

```bash
curl -X POST http://localhost:8000/api/fetch \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article", "use_browser": false}'
```

`use_browser=true` 强制走 Crawl4AI（Playwright），需要租户开启 `allow_browser` 权限。

## 引擎层级

| 层级 | 引擎 | 条件 |
|---|---|---|
| Tier 1a | SearXNG | 默认启用，需自托管实例 |
| Tier 1b | DDGS | `DDGS_ENABLED=true`（默认 `false`）|
| Tier 2 | Serper / DataForSEO / **火山引擎** | `TIER2_DEFAULT` 配置对应引擎且 API Key 已填写 |
| Tier 3 | **Crawl4AI**（Playwright 内核）| 安装 `browser` extras，`Filter.NeedContent=true` 且 trafilatura 无法抓取时触发 |

Tier 1 并行调用；若总结果 < 5 条，自动触发 Tier 2。`Filter.NeedContent=true` 时抓取流程：
1. **Redis 内容缓存**（1h TTL）— 命中直接返回，零延迟
2. **robots.txt 检查 + trafilatura 并行**跑，所有 URL 同时发出请求（并发数 = Count）
   - robots.txt 不允许时：内容照常返回，但来源 URL 替换为 `"来自网络搜索"`
3. trafilatura 拿不到内容时降级到 **Crawl4AI**（浏览器单例，无重启开销）

## 管理 API Key

```bash
# 首次启动：在 .env 中设置 ADMIN_BOOTSTRAP_KEY，admin key 会自动注册

# 通过脚本创建新租户 Key（需要 Redis 可访问）
python scripts/create_api_key.py --tenant my-app --quota 5000 --qps 10

# 通过 Admin API 创建
curl -X POST http://localhost:8000/admin/keys \
  -H "Authorization: Bearer <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "my-app", "quota_per_day": 1000, "rate_limit_qps": 5}'
```

## 主要环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_ENV` | `dev` | `dev / staging / prod`，dev 模式开启 Swagger |
| `MCP_TRANSPORT` | `http` | `stdio` 或 `http` |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接地址；集群模式填任意一个节点即可，会自动发现其余节点 |
| `REDIS_CLUSTER_MODE` | `false` | `true` = Redis Cluster 模式（多节点集群）；`false` = 单节点/哨兵 |
| `REDIS_KEY_PREFIX` | `""` | 所有 Redis key 的命名空间前缀，如 `prism:`；多服务共用同一 Redis 时用于隔离 |
| `SEARXNG_SHARDS` | `baidu=http://searxng-baidu:8080,...` | **一源一实例分片拓扑**（推荐）：`引擎名=实例URL` 逗号分隔（引擎名可含空格）。每引擎一个 SearXNG 实例，独立熔断/超时/质量检测，慢源卡死只损失该源；容器同镜像同模板，跑哪个源由请求参数 `engines=` 切片。留空回退 `SEARXNG_URLS` 全引擎多实例模式（page N 路由实例 N） |
| `SEARXNG_URLS` | `http://searxng-1:8080,...` | 非分片模式的实例列表（向后兼容）；分片模式下忽略 |
| `SEARXNG_ENGINES` | `baidu,bing,sogou,quark,sogou wechat` | 启用的搜索引擎列表。`sogou wechat`=公众号文章（时效/正文优于通用网页）；bilibili 已移除（视频对网页查询是噪音）；360search 已移除（反爬返回空 body）；DDG 已禁用；Google 需境外代理。**唯一来源是本 .env（compose 不再覆盖，改引擎只改这一处）** |
| `CHINASO_ENABLED` | `false` | 是否启用 ChinaSo（中国搜索，聚合人民网/新华/央视等权威源）。JSON 接口已逆向，按 IP 风控、经代理池出网（uid cookie 预热）；代理池有活 IP 验证可用后再开 |
| `DDGS_ENABLED` | `false` | 是否启用 DuckDuckGo 直连 |
| `TIER2_DEFAULT` | `none` | `none / serper / dataforseo / brave / volcengine` |
| `SERPER_API_KEY` | | Serper API Key |
| `DATAFORSEO_LOGIN` | | DataForSEO 账号 |
| `DATAFORSEO_PASSWORD` | | DataForSEO 密码 |
| `VOLCENGINE_API_KEY` | | 火山引擎联网搜索 API Key |
| `ADMIN_BOOTSTRAP_KEY` | | 首次启动自动注册的 admin key |
| `HMAC_SALT` | | PII 哈希盐，生产必填（`openssl rand -hex 32`） |
| `DEFAULT_QUOTA_PER_DAY` | `1000` | 新租户默认每日配额 |
| `RERANKER_MODEL` | `none` | `none` \| `BAAI/bge-reranker-base`（4c8g 推荐，~1.1GB）\| `BAAI/bge-reranker-v2-m3`（~2.3GB，需 governance extras） |
| `RERANKER_SCORE_FLOOR` | `0.25` | L3 精排绝对地板：sigmoid 低于此值丢弃；`0` = 只重排不过滤 |
| `RERANKER_SCORE_MARGIN` | `0.15` | 相对断层：丢比当批最高 sigmoid 低超此值的结果；阈=`max(floor, top-margin)` |
| `RERANKER_COLLAPSE_CEILING` | `0.65` | 塌缩闸门：整批最高 sigmoid 都低于此=召回失败/全跑题，只留 `COLLAPSE_KEEP` 条；`0`=关；换 v2-m3 须重标定 |
| `RERANKER_COLLAPSE_KEEP` | `2` | 塌缩判定成立时保留的头部条数 |
| `CIRCUIT_TTL_S` | `9` | 引擎熔断窗口（秒）；3 次失败 within TTL 跳过；全员熔断且无其他 tier1 来源时 fail-open 回退全部分片 |
| `ENGINE_TIMEOUT_S` | `5` | 单次引擎调用超时（秒）|
| `RESPONSE_TIME_BUDGET_S` | `10.0` | 端到端响应预算（秒）；排序/正文/摘要余量从此扣除 |
| `PROXY_RETRY_EXTRA_BUDGET_S` | `5.0` | 代理质量失败的额外重试预算；最坏总耗时 ≈ 15s |
| `RANKING_BUDGET_S` | `2.5` | 语义排序最长等待（秒），超时降级廉价词面排序 |
| `EMBED_MODEL` | `""` | 双塔编码模型，空=禁用；推荐 `BAAI/bge-small-zh-v1.5`（~90MB），启用后加第 7 排序因子 + 时效语义检测 |
| `HOT_INDEX_ENABLED` | `false` | 启用 ES 热点快路径 |
| `ES_URL` | `http://elasticsearch:9200` | Elasticsearch 地址；本地开发用 `http://localhost:9200`，宿主机访问用 `http://host.docker.internal:9200` |
| `PROXY_SERVICE_URL` | `""` | proxy-hub 拓扑唯一事实源（如 `http://proxy-hub:8100`，逗号分隔多副本 failover）：控制面调用与数据面隧道（host 相同、端口固定换 8101 推导，账密 `prism:<token>` 注入）都由它驱动，SearXNG/fetcher/ChinaSo 统一出网；空 = 整条 proxy-hub 链路停用（直连）。代理池的供应商/冷热分档/巡检配置全在 proxy-hub 侧 |
| `PROXY_PROJECT_TOKEN` | `""` | proxy-hub "prism" 项目 token（`POST /admin/projects` 注册获得），/activity /refresh /status 一律 Bearer 鉴权、隧道账密也从它注入；空 = 这些调用被 403 拒绝，池子断供 |
| `OMP_NUM_THREADS` / `TOKENIZERS_PARALLELISM` | `1` / `false` | CentOS7 老内核必设，否则 reranker 在线程池里加载死锁、startup 卡住 |
| `CONTENT_FETCH_BUDGET_S` | `4.0` | `NeedContent=true` 抓所有页面的墙钟上限（秒）；出网慢的机房单页 TLS 握手 2-3s，10 并发需调大到 `10`，否则抓取赶不上闸出现 `fetched=0/N` |
| `CONTENT_FETCH_MIN_BUDGET_S` | `1.5` | 正文抓取的预算下限（秒）：引擎/排序挤占响应预算时保底，避免响应只剩 title+snippet；代价是极端情况总响应最多超出 `RESPONSE_TIME_BUDGET_S` 约此值 |
| `SEARCH_TOTAL_BUDGET_S` | `3.5` | fast-path（不抓正文/不摘要）总耗时目标（秒）|

完整配置项参见 `.env.example`。

## 可观测性与排查

三个 ES 索引分工：

| 索引 | 内容 | 检索键 |
|---|---|---|
| `prism_logs` | gateway 链路日志（事件流）| 按 `query_id` 查 |
| `prism_audit` | 请求审计账本：一次请求一文档，含 `stages` 分阶段耗时 + `results` 快照 | 按 `query_id` 查 |
| `proxyhub_logs` | proxy-hub 代理服务日志（补位/探测/剔除/信号）| 按 `query_id` 直达（信号自动携带）；裸时间窗兜底 |

查一条零结果请求的标准流程：

1. `prism_audit` 按 `query_id` 拿 `stages`，定位断点。
2. `prism_logs` 按 `query_id` 看事件流：`search.engines_done`（`raw_count` / `sub_engines`）→ `engine.proxy_quality` → `search.proxy_retry_done`。
3. `proxyhub_logs` 按同一 `query_id` 直达 prism 触发的 `/refresh`、`/activity` 信号，与同秒池事件（判死剔除/补位）对齐；数据面每条隧道连接一行 `forwarder: conn` 日志（client=SearXNG 实例 / target=搜索引擎 / upstream=出口 IP / elapsed_ms），按秒级窗口与该查询的 trace 日志拼接。无 query_id 锚点时（纯超时未触发换批）退回裸时间窗。

关键日志事件（`event.type`）：

- `search.start` → `search.engines_done`
- `engine.timeout` / `engine.proxy_quality`
- `search.proxy_retry_done` → `search.done`
- 缓存兜底：`search.stale_cache_fallback`

proxy-hub 侧自查：`GET /status`（Bearer 项目 token，池状态/档位/成本）、`GET /admin/config`（Bearer admin token，热配置快照）。

## 参考文档

| 文件 | 说明 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 请求流程（17 步）、引擎适配器、SearXNG 分片拓扑、代理池、MCP、鉴权、Redis Key 约定、完整配置参考 |
| [`docs/api-reference.md`](docs/api-reference.md) | `SearchRequest` / `VolcSearchResponse` 字段全表、过滤器实现细节、SSE 端点 |
| [`docs/elasticsearch.md`](docs/elasticsearch.md) | ES 双索引架构、热点索引查询策略（决策树 + 路径 A/B）、日志索引 ESLogHandler |
| [`docs/deploy.md`](docs/deploy.md) | 部署运维：.env 维护、hf_cache 卷、重启 vs 重建、代理调优旋钮、踩坑速查 |

## 开发

```bash
# 安装开发依赖（核心）
pip install -e ".[dev]"

# 安装全量依赖（含 Crawl4AI + 治理模型）
pip install -e ".[dev,browser,governance]"

# 代码检查 & 格式化
ruff check .
ruff format .

# 运行测试
pytest
pytest tests/test_validator.py  # 单个文件

# 管理黑名单
python scripts/seed_denylist.py
```

## 技术栈

- **Python 3.11+** · FastAPI · Uvicorn · Pydantic v2
- **Redis** — 缓存、鉴权、限流、熔断、域名黑名单、会话信号、内容缓存
- **Elasticsearch 9.3.3 + IK 分词** — 热点索引快路径（`HOT_INDEX_ENABLED=true`），<10ms 响应
- **SearXNG** — 自托管元搜索引擎（冷路径主力，Baidu + Bing + Sogou + Quark，经 proxy-hub 隧道代理走短效 IP + 并发多页翻页）
- **proxy-hub（独立服务）** — 代理池托管 + 自建 HTTP 转发代理隧道：固定隧道地址（带项目账密），每连接按目标站防重轮换挑活 IP，换 IP 不重启
- **MCP SDK** — Model Context Protocol，支持 stdio / HTTP+SSE
- **Crawl4AI** — LLM 友好正文抓取，原生输出 `fit_markdown`，JS 渲染 + 反爬
- **trafilatura** — 静态页面正文提取降级方案
- **rank-bm25** — BM25 相关性排序 + NeedSummary 抽取式摘要（无 LLM）
- **APScheduler** — 定时任务（信誉衰减、引擎健康探针）
- **Prometheus** — 指标采集（`/metrics`）
