# Prism 技术架构与选型说明

> **项目定位**：面向 LLM 场景的自托管聚合搜索基础设施。通过 SearXNG 多实例元搜索、自建代理 IP 池、Elasticsearch 热点索引与七因子混合排序，为 AI 应用提供低成本、可观测、结构化的搜索结果。
>
> **配套项目**：`hot-crawler`（`C:\code\AI\crawler`）独立部署的 Scrapy 爬虫，将微博/百度/头条/RSS 等热点内容写入共享 Elasticsearch，供 Prism 命中热路。两个项目仅通过 ES 索引交互，无代码依赖。
>
> **阅读提示**：本文侧重架构与选型思路的全景说明，成文较早。个别实现细节已迭代（如 SearXNG 现为「一源一实例分片 ×5」而非三实例路由、时效检测不再注入硬时间过滤），最新事实以 [`docs/architecture.md`](docs/architecture.md) 与 [`CLAUDE.md`](CLAUDE.md) 为准。

---

## 目录

- [全链路请求流程图](#全链路请求流程图)
- 一、搜索层：多引擎分级调度
- 二、热点爬虫：hot-crawler（独立项目）
- 三、并发架构：异步调度与预取重叠
- 四、数据治理：六层防御体系
- 五、混合排序：七因子加权模型
- 六、域名信誉与 Session 反馈
- 七、语义因子：bge-small-zh 向量
- 八、对外服务：MCP + REST 双通道
- 九、可观测性：trace_id + ES 日志投递
- 十、多租户隔离与资源管控
- 十一、出站网络与代理
- 十二、部署规格与成本
- 十三、Redis 键约定
- 十四、落地状态总览
- 十五、未实现功能与规划路线图
- 十六、摘要层：BM25 抽取式

---

## 全链路请求流程图

> 包含 Elasticsearch 热点快路径（热路）与 SearXNG 冷查询路径（冷路），SearXNG 通过多实例 + 自建代理 IP 池实现翻页 IP 隔离。

```text
┌─────────────────────────────────────────────────────────────────────┐
│  调用方（LLM Agent / REST Client / Claude Desktop）                  │
│  POST /api/search   |   GET /api/search/stream   |   MCP web_search │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  trace_id（X-Trace-Id 自动生成或回传）
                    ┌──────────▼──────────┐
                    │  鉴权 & 限流         │
                    │  API Key → tenant   │
                    │  日配额 / QPS 滑动窗口│
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  时效检测 & 改写      │
                    │  今天/最新 → TimeRange│
                    │  QueryRewrite 去前缀 │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Redis 缓存查询     │─── 命中 ───────────────────────┐
                    │   key 含 9 个塑形字段│                                │
                    └──────────┬──────────┘                                │
                               │ 未命中                                    │
                    ┌──────────▼──────────┐                                │
                    │  热点索引查询        │  HOT_INDEX_ENABLED=true         │
                    │  Elasticsearch      │  IK BM25 / 时效两路             │
                    │  hot_news 索引      │                                │
                    │  <10ms              │                                │
                    └──────────┬──────────┘                                │
              命中 ≥ 3 条 /     \ 未命中或禁用                              │
          ┌────────────────────┘                                          │
          │ 热路（直接进结果合并）                                          │
          │                                       冷路 ────►              │
          │                       ┌───────────────────────────────┐       │
          │                       │                               │       │
          │              ┌────────▼──────────────┐  ┌────────────▼─────┐  │
          │              │  SearXNG 三实例并发    │  │  Tavily（可选）   │  │
          │              │  page 1 → instance-1  │  │  high_quality=   │  │
          │              │  page 2 → instance-2  │  │  true 时启用     │  │
          │              │  page 3 → instance-3  │  └────────┬────────┘   │
          │              │  各实例绑定独立代理 IP │           │            │
          │              │  baidu+bing+...       │           │            │
          │              └────────┬──────────────┘           │            │
          │                       └─────────────┬────────────┘            │
          │                                     │ Tier 1 合并             │
          │                            ┌────────▼────────┐                │
          │                            │  结果 < 5 条？   │ ─是─► Tier 2  │
          │                            └────────┬────────┘  DataForSEO/  │
          │                                     │ 否        Serper/火山  │
          │                            ┌────────▼────────┐                │
          │                            │  URL + SimHash  │                │
          │                            │  内容去重        │                │
          │                            └────────┬────────┘                │
          │                                     │                         │
          │                            ┌────────▼────────┐                │
          │                            │  黑名单过滤      │                │
          │                            │  denylist:domains│               │
          │                            │  + BlockHosts    │               │
          │                            └────────┬────────┘                │
          │                                     │                         │
          │                            ┌────────▼────────┐                │
          │                            │  Sites 白名单    │                │
          │                            └────────┬────────┘                │
          │                                     │                         │
          │                            ┌────────▼────────┐                │
          │                            │  数据丰富        │                │
          │                            │  published_at   │                │
          │                            │  6 级回退        │                │
          │                            │  commercial_score│               │
          │                            └────────┬────────┘                │
          │                                     │                         │
          │                ┌────────────────────▼─────────────────┐       │
          │                │  七因子混合排序（async）              │       │
          │                │  recency 25% + trust 15% + BM25 15% │       │
          │                │  + reputation 15% + session 10%     │       │
          │                │  + commercial -5% + embedding 15%   │       │
          │                │  intent 预设切换权重                 │       │
          │                │  （embed 未加载时降级 6 因子）       │       │
          │                └────────────────────┬─────────────────┘       │
          │                                     │                         │
          │                            ┌────────▼────────┐                │
          │                            │  AuthInfoLevel  │                │
          │                            │  权威度后置过滤  │                │
          │                            └────────┬────────┘                │
          └────────────────────────────────────►│◄────────────────────────┘
                                                │  热路 / 冷路 / 缓存命中 汇合
                          ┌─────────────────────┼─────────────────────┐
                          │                     │                     │
               ┌──────────▼──────┐ ┌────────────▼─────┐ ┌────────────▼──┐
               │  NeedContent    │ │  NeedUrl         │ │  NeedSummary  │
               │  Crawl4AI →     │ │  HEAD 可达性     │ │  BM25 抽取式  │
               │  trafilatura →  │ │  并发 10        │ │  无 LLM       │
               │  readability    │ │  2.5s 超时      │ │  <100ms       │
               │  代理 IP 抓取    │ │                  │ │               │
               └──────────┬──────┘ └────────┬─────────┘ └────────┬──────┘
                          └─────────────────┴─────────────────────┘
                                            │ 共享剩余时间预算
                                            │（floor: content 5s / summary 1s）
                                   ┌────────▼────────┐
                                   │  ContentFormats │
                                   │  text/markdown  │
                                   │  /html 转换     │
                                   └────────┬────────┘
                                            │
                                   ┌────────▼────────┐
                                   │  异步写缓存      │
                                   │  Redis TTL      │
                                   └────────┬────────┘
                                            │
                          ┌─────────────────▼──────────────────────────┐
                          │  VolcSearchResponse（火山兼容格式）          │
                          │  WebResults[] + SummarizedAnswer + Meta    │
                          │  X-Trace-Id 响应头回传                      │
                          └────────────────────────────────────────────┘

  ─── 异步旁路 ─── （不阻塞请求路径）
  ESLogHandler 队列 ──► log_shipper 后台任务 ──► Elasticsearch prism_logs 索引
  proxy_client 节流 ──► proxy-hub POST /activity（按需补位驱动）；引擎熔断 ──► POST /refresh 清池重抽
```

### 热路 vs 冷路 对比

| 维度 | 热路（Elasticsearch hot_news） | 冷路（SearXNG 多实例） |
|------|-----------------------------|---------------------|
| 触发条件 | `HOT_INDEX_ENABLED=true` 且命中 ≥ 3 条 | 热路未命中或禁用 |
| 延迟 | <10ms（本地 ES 查询） | 500ms–2s（外部搜索 + 代理） |
| 数据来源 | hot-crawler 定时抓取的热点内容 | 实时元搜索 + 代理 IP 池 |
| 适用场景 | 热点话题、最新新闻 | 长尾查询、实时冷门内容 |
| 缓存写入 | 命中后异步写 Redis | 同冷路走完后写 Redis |

---

## 一、搜索层：多引擎分级调度

Prism 不维护自有爬虫索引（热点除外），而是通过**SearXNG 多实例元搜索 + 商业 API 降级 + 动态浏览器渲染**的三级体系获取结果。

### 1.1 Tier 1：SearXNG 三实例聚合（主力）

| 组件 | 技术本质 | 工程定位 |
|------|---------|---------|
| **SearXNG × 3 实例** | 开源元搜索引擎，自身不爬取，并发查询百度/Bing/搜狗/360/夸克/Bilibili 等底层引擎，对结果去广告、去格式差异后返回标准化输出。Prism 部署 3 个独立容器（`prism-searxng-1/2/3`），按页码路由实现 IP 隔离。 | 主力引擎，承担约 80% 通用流量 |
| **DDGS** | DuckDuckGo Python 接口 | **当前禁用**（`DDGS_ENABLED=false`）：自建代理出口被 `html.duckduckgo.com` 封 449 |

**关键澄清**：SearXNG 并非「自建搜索引擎」，而是「搜索引擎的调度编排层」。其价值在于**结果可控、无单点绑定、聚合消除引擎偏见**。

### 1.2 SearXNG 多实例 + 自建代理 IP 池

#### 多实例分页架构

```text
page 1 → prism-searxng-1（绑定 IP1）→ baidu p1 + bing p1 + ...
page 2 → prism-searxng-2（绑定 IP2）→ baidu p2 + bing p2 + ...   ← 并发，不增加耗时
page 3 → prism-searxng-3（绑定 IP3）→ baidu p3 + bing p3 + ...
```

**解决的问题**：单 IP 翻页时，对同一底层引擎短时间内会发出 6 次以上请求，触发百度/夸克 X5SEC 等的 CAPTCHA 或限速（实测阈值约 9 次）。多实例方案将每个 IP 的同引擎请求降到约 6 次，保留在阈值以下，并发不变、耗时不增加。

实例配置已收敛为单模板 `searxng/settings.template.yml`（分片实例共用，不含任何代理地址与 token），容器启动时由 `searxng/entrypoint.sh` 调 `searxng/render-settings.sh` 从 `.env` 的 `PROXY_SERVICE_URL` 推导隧道列表渲染生成 settings.yml（host 相同、端口换 8101，账密用 `PROXY_PROJECT_TOKEN` 注入；产物过 YAML 硬校验，非法拒启）；`limiter.toml` 全实例共享一份。渲染产物写在容器层，SearXNG 运行期重写的 `settings.yml.new` 已加入 `.gitignore`。

**降级行为**：IP 分散由 proxy-hub 侧滚动补位保证（池保持固定长度）；proxy-hub 不可达时 SearXNG 沿用隧道内现有 IP，查询照常由熔断收敛。

#### 代理 IP 池（独立 proxy-hub 服务托管）

代理池已拆分为独立 proxy-hub 服务（独立仓库），gateway 进程内不再维护代理池
（原 `app/services/proxy_pool.py` 已删除）：

```text
proxy-hub 后台（主备副本，Redis 锁协调）
  → 按项目滚动补位：巡检清过期/判死剔除 → 缺几个补几个（ZSET 池保持固定长度，IP 用满 1min 生命周期）
  → prism 消费：SearXNG/fetcher/ChinaSo 统一经 proxy-hub 自建转发代理出网
     （隧道地址从 PROXY_SERVICE_URL 推导：host 相同、端口固定换 8101，账密
      http://prism:<项目token>@<proxy-hub>:8101 由 PROXY_PROJECT_TOKEN 注入；
      每连接按目标站防重轮换挑上游 IP，换 IP 零窗口、永不重启 SearXNG）
```

消费端点一律 Bearer 项目 token 鉴权（`PROXY_PROJECT_TOKEN`）。qg.net get 接口响应（默认 json）：
```json
{
  "code": "SUCCESS",
  "data": [
    {"proxy_ip": "112.247.54.216", "server": "60.188.69.215:19239",
     "isp": "联通", "deadline": "2026-06-18 14:38:23"}
  ],
  "request_id": "d628226a-..."
}
```

代理连接用 `server`（中转网关 ip:port），`proxy_ip` 仅是住宅出口 IP。`deadline` 是每 IP 绝对过期时刻（北京时间，proxy-hub 侧显式挂 `Asia/Shanghai` 比对），过期 IP 由 proxy-hub 巡检剔除补位。鉴权账密（proxy-hub 侧 `PROVIDER_AUTH_KEY`/`PWD`）时代理 URL 为 `http://key:pwd@server`，否则裸连走 IP 白名单。

**fetcher 代理共享**：`app/services/fetcher.py` 的 httpx 客户端经 `proxy_client.get_tunnel_proxy_url()` 接入隧道地址。`Filter.NeedContent=true` 抓取的网页内容也走代理，避免源站封禁。

**故障隔离**：proxy-hub 不可达只记录 WARNING/DEBUG，不影响搜索主流程（隧道可能仍有活 IP，由熔断收敛）。

#### 当前 SearXNG 引擎清单

一源一实例分片（`SEARXNG_SHARDS`，.env 唯一事实源）：`baidu / bing / quark / sogou / sogou wechat`，各占一个实例、独立熔断；`sogou wechat`=公众号文章源。留空 `SEARXNG_SHARDS` 回退非分片模式（`SEARXNG_URLS` 多实例 + `SEARXNG_ENGINES` 引擎清单）。

- **DuckDuckGo** 已禁用：自建代理被 `html.duckduckgo.com` 封 449
- **Bing** 使用 `cn.bing.com` 绕过代理域名限制
- **chinaso** 已作为独立 Tier1 引擎回归（`app/engines/chinaso.py`，默认关，须走代理隧道）
- **bilibili** 返回视频结果，`content` 字段空，需 `NeedContent=true` 拉简介
- 各引擎每页约 10 条，适配器自动并发拉取多页（最多 3 页）凑足所需数量

#### CAPTCHA 应对策略（每实例 settings.yml 均已配置）

| 配置 | 作用 |
|---|---|
| `search.suspended_times.SearxEngineCaptcha: 10` | CAPTCHA 后暂停 10s（与 proxy-hub 换 IP 落地时间 ~2-10s 对齐；原 900s 会让换到干净 IP 的引擎仍被挂起 15 分钟）|
| `search.suspended_times.SearxEngineTooManyRequests: 60` | 限速后暂停 60s |
| `outgoing.enable_http2: false` | 禁 HTTP/2，避免多路复用暴露指纹 |
| `outgoing.default_http_headers.Connection: close` | 每次响应后关闭 TCP 连接 |
| `outgoing.default_http_headers` | 伪装成 Chrome 124 |
| `engines[baidu].headers.Referer` | 百度站内跳转伪装 |
| `engines[quark].headers.Referer` | X5SEC 校验 Referer |
| `outgoing.request_timeout: 5.0` | 全局超时；quark 单独 4s，bilibili 单独 3s |

**baidu 引擎为 HTML 解析 override 版**（`searxng/overrides/baidu.py` 挂载进 searxng-baidu，2026-09）：上游 `tn=json` 接口 100% 触发验证码、SearXNG 默认出站 `Accept: */*` 也必被 302——override 去掉 `tn=json`、补浏览器 Accept 头、改 lxml 解析 `div.result.c-container` 并过滤广告。改 override 后需 `docker compose up -d --force-recreate searxng-baidu`（entrypoint 会逐出镜像内置的旧 pyc 字节码缓存）。

**quark 引擎为挂起时长 override 版**（`searxng/overrides/quark.py` 挂载进 searxng-quark，2026-09-22）：上游 quark.py:89 硬编码 `suspended_time=900`（为直连固定 IP 设计，Alibaba CAPTCHA 封禁约 15 分钟），覆盖 settings 的 10s，导致一次 CAPTCHA 把 quark 片挂起 15 分钟（实测 20 query 出勤 0/20，全是挂起 fast-fail）。override 去掉硬编码、回落 settings 10s（隧道每连接换 IP，无需等封禁解除），与 baidu override 同节奏；其余逻辑与上游一致。改 override 后需 `docker compose up -d --force-recreate searxng-quark`（entrypoint 检测 `PRISM_OVERRIDE` 标记逐出旧 pyc）。

**可选加强**：在每实例 settings.yml 配置浏览器登录 Cookie：
- Baidu：`engines[baidu].headers.Cookie: "BAIDUID=...; BDUSS=..."`（约 30 天有效）
- Quark：`engines[quark].headers.Cookie: "_xsrf=xxx; uc_session=xxx"`

### 1.3 Tier 2：商业 API 兜底

当 Tier 1（SearXNG + 可选 Tavily）合并结果 < 5 条时，自动降级至商业 API。

| 组件 | 能力 | 成本 | 选型理由 |
|------|------|------|---------|
| **DataForSEO** | Google/Bing/Yahoo SERP 原始数据 | ~¥0.15/千次 | 批量极便宜，首选兜底 |
| **Serper** | Google SERP，结构化好 | ~¥2–7/千次 | 国内可达性优于直接调 Google |
| **火山引擎** | 字节跳动联网搜索 API | ~¥1–3/千次 | 已做格式兼容；遇 `QuotaExhaustedError`（10406）自动跳过 |
| **Tavily** | 学术/新闻类付费搜索 | 按调用计费 | `high_quality=true` 时与 Tier 1 并行（非降级），需 `TAVILY_API_KEY` |

`TIER2_DEFAULT` 选择活跃的 Tier 2 引擎：`none | dataforseo | serper | brave | volcengine`。

> **架构决策**：Tier 1 免费引擎扛 80% 流量，长尾 20% 走付费 API，单次成本下降 1–2 个数量级。

### 1.4 Tier 3：动态页面渲染与正文提取

`Filter.NeedContent=true` 或源站 JavaScript 渲染时触发深度抓取。

| 组件 | 路径 | 适用 |
|------|------|------|
| **Crawl4AI** | Playwright 异步浏览器，原生 `fit_markdown` 输出，内置 robots.txt 检查 | 首选：JS 渲染、强反爬（微信公众号、知乎）|
| **trafilatura** | 启发式正文提取，纯 HTTP 0.5–2s | fallback：Crawl4AI 返回空时降级 |
| **readability-lxml** | 复用 trafilatura 已抓 HTML 二次提取 | 第三层兜底，无额外 HTTP |

**实际抓取顺序**（`app/services/fetcher.py`）：

1. **Redis 内容缓存** `content:{sha1(url)}`，TTL 1h
2. **Crawl4AI**：robots.txt Redis 缓存 24h；T1 域名跳过 robots 检查
   - robots 不允许时**仍返回内容**，但 `url` 置空、`domain` 改为 `"来自网络搜索"`，源站不暴露
3. **trafilatura**：纯 HTTP fallback
4. **readability-lxml**：复用上一步 HTML

只用 Crawl4AI 的 `fit_markdown`，`raw_markdown` 太噪。`_clean_markdown` 剔除 JS/CSS 噪声、合并空行。

**共享 httpx 客户端**：模块级单例，自动接入代理池，`max_connections=50, keepalive=20`，连接超时 2s / 读取 3s。Semaphore 按实际 URL 数动态调整，10 个 URL 即 10 路并行。内容上限 20,000 字符，<100 字符视为无效。

### 1.5 完整的降级流程

```text
用户提问
  ↓
查 Redis 缓存 → 命中直接返回
  ↓ 未命中
查热点索引（ES hot_news，HOT_INDEX_ENABLED=true）→ 命中 ≥ 3 条直接返回
  ↓ 未命中或禁用（冷路）
SearXNG 三实例并发查询（baidu/bing/...，每页一个 IP，代理 IP 池托管）
高质量模式：并行启动 Tavily（high_quality=true）
  ↓ 结果 ≥ 5
URL + SimHash 去重 → 治理 → 排序 → 返回
  ↓ 结果 < 5
降级 DataForSEO / Serper / 火山引擎（Tier 2）
  ↓
NeedContent=true？
  Crawl4AI（LLM-friendly Markdown）→ trafilatura → readability-lxml
```

---

## 二、热点爬虫：hot-crawler（独立项目）

热点索引由独立部署的 Scrapy 爬虫服务 `C:\code\AI\crawler` 写入，与 prism gateway 通过 Elasticsearch 解耦。两个项目无代码依赖，仅共享 ES 索引名 `hot_news`。

### 2.1 调度与 Spider 结构

```text
hot-crawler/
  main.py                  # Twisted asyncio reactor + LoopingCall 定时调度
  hot_crawler/
    spiders/
      news_rss.py          # 9 个 RSS 源聚合（微博/知乎/36氪/IT之家/澎湃/网易/腾讯...）
      baidu.py             # 百度热搜（HTML 解析）
      toutiao.py           # 今日头条（JSON API）
    pipelines.py           # 四级流水线
    fingerprint.py         # 64-bit SimHash（CJK 字级 + ASCII 词级，MD5 确定性哈希）
    items.py               # HotItem 定义
    settings.py            # Scrapy 全局配置
```

**调度频率**（`main.py`）：

| Spider | 间隔 | 来源数 |
|---|---|---|
| `news_rss` | 60 分钟 | 9（含微博/知乎/36氪/IT之家/抽屉 + 澎湃/网易/腾讯）|
| `baidu` | 30 分钟 | 百度热搜 |
| `toutiao` | 30 分钟 | 今日头条 JSON |
| `cleanup` | 24 小时 | 清理 `crawled_at_ts` 早于 400 天的文档 |

启动时 `callLater(0/2/4)` 错峰预热，避免 ES 写入竞争。

### 2.2 四级 Pipeline（按优先级数字递增执行）

| 优先级 | Pipeline | 职责 |
|---|---|---|
| 200 | `ContentFetchPipeline` | trafilatura 抓正文（截 `CONTENT_MAX_CHARS=2000` 字符）|
| 300 | `EmbedPipeline` | bge-small-zh-v1.5 生成 512 维向量；`EMBED_MODEL` 空时整 pipeline 跳过 |
| 400 | `DedupPipeline` | 两层去重：批次内存缓存 + ES kNN/BM25 召回 5 条候选 + SimHash 海明距离比对（阈值 3） |
| 500 | `ESPipeline` | 批量写 ES（50 条/批），自动建立 `hot_news` 索引（IK 分词 + dense_vector 映射） |

### 2.3 Elasticsearch 文档结构（hot_news 索引）

| 字段 | 类型 | 备注 |
|---|---|---|
| `id` | keyword | `sha1(url)[:16]` |
| `title` / `snippet` / `content` | text | IK：`ik_max_word` 索引 / `ik_smart` 检索 |
| `url` / `source` | keyword | source 如 `weibo`、`baidu`、`tencent` |
| `heat_score` | integer | 热榜排名 |
| `simhash` | long | 64 位指纹 |
| `crawled_at` / `crawled_at_ts` | date / long | UTC 抓取时间，long 字段做 range 查询性能优于字符串 |
| `published_at` / `published_at_ts` | date / long | 同上 |
| `vectors` | dense_vector(512, cosine) | 可选；`EMBED_MODEL` 为空时不写入 |

**TTL 策略**：保留 400 天（> OneYear=365 天，确保 TimeRange 全档位可查）；每 24 小时清理超期文档。

### 2.4 与 Prism 的契约

- **共享**：ES 实例与 `hot_news` 索引名（双方 `.env` 中 `ES_INDEX` 必须一致）
- **隔离**：crawler 不读 Redis、不依赖 Prism 任何 API；Prism 不直接访问 crawler 容器
- **降级**：crawler 停摆 → ES 数据陈旧 → Prism `hot_index_min_hits` 阈值过滤掉过老结果 → 自然回落到冷路 SearXNG

---

## 三、并发架构：异步调度与预取重叠

全链路基于 Python `asyncio`，核心思想是**非阻塞 I/O + 任务重叠**。

| 并发层级 | 实现机制 | 工程收益 |
|---|---|---|
| **引擎级并行** | `asyncio.as_completed`：SearXNG 与（可选）Tavily 同时发起 | 消除单引擎等待 |
| **SearXNG 多实例并发** | 按 `Count` 动态计算页数（≤ 3 页），每页通过 `asyncio.gather` 并行；page N 路由到 instance N | 突破单 IP 限速且不增加 wall-clock |
| **多页/多引擎扇出** | SearXNG 一次请求内部并发 N 引擎 × M 页 | top_n=20 时一次拿 60+ 原始结果 |
| **后置并行块** | 内容抓取 / 可达性 / 摘要在 ranking 完成后一起 launch；总耗时 = max(任务) | 三个 NeedXxx 同时开启不累加耗时 |
| **Redis Pipeline 批量读** | 排序前一次性读所有域名信誉 + Session 信号；circuit-breaker 检查也合并 | N 次 RTT 降为 1 次 |
| **多 URL 并发抓取** | `Semaphore(len(urls))` + `asyncio.gather` | 10 URL = 10 路并行 |

**预算管理**：`SEARCH_TOTAL_BUDGET_S=3.5` 是端到端目标，但 `NeedContent` 与 `NeedSummary` 各有独立预算（`CONTENT_FETCH_BUDGET_S=4.0`、`SUMMARY_BUDGET_S=1.0`），且正文抓取有下限 floor（`CONTENT_FETCH_MIN_BUDGET_S=1.5`）——引擎/排序把 `RESPONSE_TIME_BUDGET_S` 挤占后仍保底抓正文，避免响应只剩 title+snippet 被 deep research 类调用方判低质。

---

## 四、数据治理：六层防御体系

中文互联网搜索结果面临 SEO 农场、镜像采集、竞价广告、AI 洗稿等多重污染。Prism 设计了六层递进式治理：

```text
原始结果池
  ↓
① 源站准入控制（域名年龄 / ICP 备案 / 观察期）   — 规划中
② 内容清洗（DOM 降噪 / 导航过滤）                — ✅ 已落地（fetcher._clean_markdown）
③ 指纹去重（SimHash 内 / MinHash 跨）            — ✅ SimHash / 🔲 MinHash 规划中
④ AIGC 检测（Burstiness / PPL / 模板）           — 规划中
⑤ 广告与垃圾过滤（黑名单 / 商业评分 / 权威度）    — ✅ 已落地
⑥ 事实一致性校验（多源矛盾检测）                 — 规划中
  ↓
干净结果池 → Ranker
```

### 4.1 指纹去重：内容级镜像识别

URL 去重无法解决「同一文章发布于 100 个不同域名」的问题。指纹去重通过**内容哈希相似度**识别镜像与洗稿。

| 算法 | 原理 | 阈值 |
|---|---|---|
| **SimHash**（已落地） | 局部敏感哈希。文档映射为 64 位指纹，相似文档的指纹海明距离极小 | 海明距离 < 3 → 重复 |
| **MinHash + LSH**（规划中） | 文档拆为 shingle 集合，最小哈希签名估计 Jaccard 相似度，LSH 实现亚线性检索 | Jaccard > 0.85 → 洗稿 |

Prism 与 hot-crawler 都使用同一份 `fingerprint.py` 算法（CJK 字级 + ASCII 词级，MD5 确定性哈希，PYTHONHASHSEED 安全）。

### 4.2 AIGC 检测：多信号融合判别（规划中）

计划基于 `Qwen2.5-0.5B` ONNX 实现。

| 信号维度 | 判别依据 |
|---|---|
| **Burstiness** | 句长变异系数。AI 文本句长自相关性强、变异系数低 |
| **Perplexity** | 小模型计算条件概率负对数似然，AI 洗稿在特定区间异常平滑 |
| **模板句式** | AIGC 高频套话库匹配 |
| **事实密度** | 命名实体数 / 总词数，水文密度低 |

仅对 top-10 执行；Fast 档跳过；ONNX Int8 量化目标延迟 100–300ms/条。

### 4.3 商业内容评分：规则光谱模型

每条结果计算 `commercial_score ∈ [0, 1]`：

| 信号源 | 规则示例 |
|---|---|
| 域名黑名单 | 已知内容农场、营销聚合站 |
| URL 路径 | `buy` / `price` / `coupon` / `promo` |
| 标题关键词 | "哪家好"、"报价"、"加盟"、"限时" |
| 正文 CTA 密度 | "立即咨询"、"扫码领取"、"点击查看" 频次 |

**分级处置**：

| 分数 | 处置 |
|---|---|
| 0.8–1.0 | 硬广，过滤或标记 `[推广]` 沉底 |
| 0.5–0.8 | 商业内容，权重衰减 30%–50%，标记 `[商业内容]` |
| 0.2–0.5 | 含推广信息，轻微降权 |
| 0–0.2 | 正常通过 |

### 4.4 权威度过滤：源站分级

| 等级 | trust_score | 域名特征 |
|---|---|---|
| 高权威（T1）| 0.9 | `gov.cn` / `edu.cn` / `github.com` / `wikipedia.org` / `arxiv.org` / `stackoverflow.com` / `mdn` |
| 普通 | 0.5 | 其他 |

`Filter.AuthInfoLevel` 后置过滤：
- 1（非常权威）：`trust_score ≥ 0.85`
- 2（正常权威）：`commercial_score < 0.5`
- 3（一般权威）：`commercial_score < 0.8`

**空结果回退**：若过滤后无结果，自动放宽，保证可用性。

### 4.5 BM25：经典概率检索

```text
score(D, Q) = Σ IDF(q_i) · [ f(q_i, D) · (k_1 + 1) ] / [ f(q_i, D) + k_1 · (1 - b + b · |D| / avgdl) ]
```

`rank-bm25` 库，对 top-50 结果池构建 ad-hoc 索引，归一化后接入混合排序。

---

## 五、混合排序：七因子加权模型

`app/services/ranker.py` 全异步实现：

| 因子 | 默认权重（intent=auto, 含 embedding）| 来源 |
|---|---|---|
| Recency | 25% | 指数衰减，半衰期 ~30 天 |
| Source Trust | 15% | T1 域名白名单先验 |
| BM25 | 15% | 当次结果集 ad-hoc |
| Domain Reputation | 15% | Redis 行为反馈动态信誉 |
| Session Boost | 10% | 当前 Session 偏好 |
| Commercial Penalty | 5%（负向） | 1 - commercial_score |
| **Embedding Cosine** | 15% | bge-small-zh query-doc 余弦 |

**降级**：`EMBED_MODEL` 为空时切换至 6 因子（recency 30 / trust 20 / BM25 20 / reputation 15 / session 10 / commercial 5）。

**intent 预设权重切换**：`auto | knowledge | commercial | news | academic`：
- `news`：recency 提到 0.50
- `academic` / `knowledge`：trust + BM25 加权
- `commercial`：商业惩罚翻转为奖励

**Tie-breaking**：综合分差 < 0.02 时以 `published_at` 降序稳定排序。

**bge-reranker-v2-m3**（cross-encoder，可选）：`RERANKER_MODEL=bge-reranker-v2-m3`，仅冷路 top-K 执行，热路跳过，CPU ~300–800ms/top-20。

---

## 六、域名信誉与 Session 反馈

### 6.1 行为信号采集

隐式反馈推断，最近 5 个事件存 Redis（`session:signals:{session_id}` LIST，TTL 1h）：

| 行为模式 | 语义 | 信誉调整 |
|---|---|---|
| 30s 内重搜同 query | Rescue（不满意） | -0.25 |
| 1min 内改述再搜 | Rephrase（偏离意图） | -0.12 |
| 新 query 包含旧 query 且更具体 | Drill-down（深入） | +0.08 |
| 2min 内无后续 | Silent Accept | +0.10 |

### 6.2 域名信誉计算

Bayesian 平滑 + 置信度加权：

```text
ratio = (good + 10) / (good + bad + 20)          # 平滑好评率
confidence = min(1, impression / 100)            # 观测置信度
score = 0.5 * (1 - confidence) + ratio * confidence
```

- **月度衰减**：`good` / `bad` × 0.9，防止历史固化
- **跨 Session 沉淀**：同 API Key 信号汇总至 `prefs:{uid_hash}`

---

## 七、语义因子：bge-small-zh 向量

`app/services/embedder.py` 已落地。`EMBED_MODEL=BAAI/bge-small-zh-v1.5` 后启用：

1. Query 编码 `v_q = Encoder(query)`
2. 结果编码 `v_d = Encoder(title + snippet)`
3. 余弦相似度 `cosine_sim(v_q, v_d)` 接入第 7 因子

**资源约束**（4 核 CPU 推荐）：
- `EMBED_NUM_THREADS=2` × `EMBED_MAX_CONCURRENT=2` = 4 cores
- 单批延迟 65–110ms，内存 ~90MB
- 模型首次运行从 HuggingFace 下载，本地缓存

弥补 BM25 的 Vocabulary Mismatch 问题（"苹果手机" vs "iPhone 15 评测" 字面无共现，向量空间近）。

---

## 八、对外服务：MCP + REST 双通道

### 8.1 MCP（Model Context Protocol）

| 传输 | 适用 | Session 标识 |
|---|---|---|
| STDIO | Claude Desktop / Cursor 等本地客户端 | `stdio:{pid}:{start_ts}`（注：MCP/STDIO 不携带 trace_id）|
| HTTP+SSE | 远程服务、Web 应用，挂在 `/mcp/sse` 与 `/mcp/messages` | SSE transport 层 UUID |

**暴露工具**：
- `web_search`：主搜索
- `search_feedback`：显式反馈，LLM 可对特定 URL 标 useful/useless（×1.5 加权）

### 8.2 REST API

| 端点 | 能力 |
|---|---|
| `POST /api/search` | 标准搜索，返回结构化 JSON（火山兼容） |
| `GET /api/search/stream` | SSE：`plan → engine_done (per engine) → results` |
| `POST /api/fetch` | 单 URL 正文抓取 |
| `GET /metrics` | Prometheus 指标 |
| `GET /health` | 健康检查 |

**响应格式对齐火山引擎 `VolcSearchResponse`**：现有火山调用方只需改 base URL 即可降级为 Prism。

---

## 九、可观测性：trace_id + ES 日志投递

### 9.1 trace_id 全链路追踪

每个 HTTP 请求生成 12 位 hex `trace_id`（或读 `X-Trace-Id` 请求头），存入 `ContextVar`，自动注入每条日志。响应头回传 `X-Trace-Id`，调用方可拼接外部链路系统。

按 `trace_id` 还原一次完整请求：
```bash
grep '"trace_id": "abc123ef"' logs/timing.log | python -m json.tool
```

`logs/timing.log` 仅含 `search.* / engine.* / cache.*` 等性能事件，独立于人类可读的 `prism.log`。

### 9.2 ES 双索引架构

**同一 ES 实例承载两个完全独立的索引**，不冲突、不交叉：

| 索引 | 默认名 | 写入方 | 读取方 | 用途 |
|---|---|---|---|---|
| 热点内容索引 | `hot_news` | hot-crawler 项目 | `app/services/hot_index.py` | 热路命中，跳过 SearXNG |
| 运行日志索引 | `prism_logs` | `app/services/log_shipper.py` | Kibana / 运维查询 | Prism 自身日志持久化 |

**部署形态**：ES 不再随 Prism docker compose 启动，改为宿主机本地实例（`C:\elasticsearch\elasticsearch-9.3.3`）。容器通过 `host.docker.internal:9200` 访问。crawler 项目自带 ES 容器（`es-with-ik:9.3.3`，含 IK 9.3.3 插件）；本地 ES 同样需安装 IK 插件。

### 9.3 ES 日志投递（log_shipper）

`app/services/log_shipper.py` 实现非阻塞异步日志投递：

- **`ESLogHandler.emit()` 非阻塞**：`queue.put_nowait` 满时静默丢弃，绝不阻塞请求路径
- **后台 task 批量 flush**：每 5s 或 100 条 → ES `bulk` 写入
- **故障隔离**：连续 3 次 flush 失败后退避 60s；shipper task 崩溃自动重启
- **ES 连接超时 5s**：ES 挂掉不长时间阻塞事件循环
- **ECS 字段**：`@timestamp / level / logger / msg / service / trace_id / tenant_id / session_id / query_id`
- **IK 不可用降级**：自动 fallback 至 standard analyzer

**关键约束**：`LOG_ES_INDEX=prism_logs`（默认）必须与 `ES_INDEX=hot_news`（默认）不同，二者**完全隔离**。

### 9.4 Prometheus 指标

`/metrics` 暴露：缓存命中率、引擎错误率、各阶段延迟分位数等，供 Grafana 看板使用。

---

## 十、多租户隔离与资源管控

### 10.1 鉴权与配额

| 机制 | 实现 | 存储 |
|---|---|---|
| API Key 鉴权 | `Authorization: Bearer <key>` | `apikey:{sha256}` → tenant_id |
| 日配额 | 原子计数器，TTL 25h | `tenant:{id}:quota:{date}` |
| QPS 滑动窗口 | Redis ZSET 时间戳分布 | `tenant:{id}:ratelimit` |
| 引擎白名单 | 租户 hash 字段 `allowed_engines` | `tenant:{id}:config` |

**限流响应**：HTTP 429 + `Retry-After`。

**首次启动**：`ADMIN_BOOTSTRAP_KEY` 自动注册 admin 租户。

### 10.2 PII 合规与数据安全

| 措施 | 实现 |
|---|---|
| 用户标识哈希 | `uid_hash = HMAC-SHA256(user_id, HMAC_SALT)`，明文不入库 |
| 偏好留存 | 90 天，APScheduler 月度任务清理 |
| 主动删除 | `DELETE /admin/preferences/{user_id}` 按 hash 删除 |
| 日志生命周期 | 文件日志按 `LOG_BACKUP_COUNT=10` 滚动；ES 日志按索引生命周期策略（外部配置） |

`HMAC_SALT` 生产必须设置（`openssl rand -hex 32`）。

---

## 十一、出站网络与代理

> 本节相对旧版本完全重写：原本规划的"自建 VPS + 数据中心代理 + 住宅代理"三级体系未落地，实际方案是**自建代理 IP 池**单层。

### 11.1 当前代理方案

**独立 proxy-hub 服务托管的代理 IP 池 + SearXNG 多实例**：
- 供应商 API（qg.net 1min 短效住宅 IP）由 proxy-hub 按项目滚动补位抽取（ZSET 池保持固定长度）
- SearXNG 实例的 `settings.yml` 静态指向 proxy-hub 自建转发代理（`http://prism:<项目token>@<proxy-hub>:8101`），每连接按目标站防重轮换挑上游 IP，换 IP 不再重启实例
- 同一隧道地址供 fetcher（内容抓取）共享，所有出站经代理

**包月 vs 按量**：
- 仅搜索：少量 IP 即可，按量包月均可
- 搜索 + 热点爬取（启用 hot-crawler 后）：包月 ~¥340/月 立即值回票价（按量累计 ~¥900+/月）

### 11.2 故障与降级

- proxy-hub 不可达 → WARNING/DEBUG + SearXNG 沿用隧道内现有 IP（不影响搜索）
- 代理 IP 大面积失效 → 引擎熔断跨阈值时自动 `POST /refresh` 请 proxy-hub 清池重抽
- 代理无可用 IP → fetcher 自动直连（成功率下降但不报错）

`PROXY_DAILY_BUDGET_USD` 字段在配置里保留兼容，但当前未挂熔断逻辑（旧规划遗留）。

---

## 十二、部署规格与成本

| 档位 | 规模 | 主要组件 | 月成本估算 |
|---|---|---|---|
| **S** | ≤ 1.5 万/月 | Gateway × 1 + Redis + SearXNG × 3 + 本地 ES | ¥75–100 + 代理包月 |
| **M** | 3–9 万/月 | + crawler + Tier 2 兜底 | ¥440–780 |
| **L** | 100 万/月 | + Gateway × N + Playwright Worker | ¥2,600–5,000 |

**与纯付费 API 成本对比**（100 万调用/月）：

| 方案 | 月成本 | 说明 |
|---|---|---|
| 纯火山/Brave/Tavily | ¥10,000–50,000 | 全部走商业 API |
| Prism（L 档）| ¥2,600–5,000 | 80% 流量走免费引擎，长尾降级 |

---

## 十三、Redis 键约定

| Pattern | 用途 |
|---|---|
| `apikey:{sha256}` | key → tenant_id 映射 |
| `tenant:{id}:config` | 租户设置 hash |
| `tenant:{id}:quota:{date}` | 日配额计数器 |
| `tenant:{id}:ratelimit` | 滑动窗口 QPS ZSET |
| `circuit:{engine}` | 引擎熔断器失败计数 |
| `denylist:domains` | 全局域名黑名单 SET |
| `reputation:domain:{domain}` | good / bad 浮点 hash |
| `session:signals:{session_id}` | 最近 5 行为信号 LIST，TTL 1h |
| `prefs:{uid_hash}` | 用户偏好 hash（HMAC 后）|
| `cache:search:{hash}` | 搜索结果缓存（含 9 个塑形字段）|
| `content:{sha1(url)}` | 网页内容缓存，TTL 1h |
| `robots:{netloc}` | robots.txt 缓存，TTL 24h |

---

## 十四、落地状态总览

| 模块 | 状态 |
|---|---|
| MCP STDIO / HTTP+SSE 双传输 | ✅ |
| REST API + SSE 流式搜索 | ✅ |
| **SearXNG 三实例 + 翻页 IP 隔离** | ✅ |
| **代理 IP 池（独立 proxy-hub 服务托管，滚动补位 + 目标站防重轮换）** | ✅ |
| **fetcher 共享代理（NeedContent 走代理）** | ✅ |
| 多引擎自动降级（Tier 1 SearXNG + 可选 Tavily / Tier 2 Serper / DataForSEO / 火山）| ✅ |
| 引擎熔断器（3 次失败 → 60s 冷却，pipeline 单次 Redis RTT）| ✅ |
| 七因子混合排序（含 embedding，未配置时降级 6 因子）| ✅ |
| `intent` 预设权重切换（auto / news / academic / knowledge / commercial）| ✅ |
| 域名黑名单 + `commercial_score` 评分 | ✅ |
| `AuthInfoLevel` 权威度后置过滤 | ✅ |
| Session 行为反馈 + 信誉衰减（APScheduler 月度）| ✅ |
| Crawl4AI → trafilatura → readability 三层正文抓取 | ✅ |
| Redis 内容缓存 1h + robots.txt 缓存 24h | ✅ |
| 火山引擎响应格式兼容 `VolcSearchResponse` | ✅ |
| 多租户鉴权、配额、滑动窗口限流 | ✅ |
| PII 哈希 + 偏好删除接口 | ✅ |
| `published_at` 6 级回退（引擎 → JSON-LD → URL 正则 → 相对时间）| ✅ |
| `NeedSummary` BM25 抽取式摘要（无 LLM，<100ms）| ✅ |
| `NeedUrl` 可达性 HEAD 并发探测 | ✅ |
| `ContentFormats` text/markdown/html 转换 | ✅ |
| `QueryControl.QueryRewrite` 规则改写 | ✅ |
| 时效查询自动调参（"今天/最新" → `TimeRange=OneDay` + 日期注入）| ✅ |
| 热点索引快路径（ES `hot_news` + IK，命中 ≥ 3 跳过 SearXNG）| ✅ |
| **ES 日志独立索引（`prism_logs`，异步非阻塞投递）** | ✅ |
| Prometheus 指标 `/metrics` | ✅ |
| `trace_id` 全链路日志（`X-Trace-Id` 响应头回传） | ✅ |
| `high_quality=true` Tavily 并行补充 | ✅ |
| SimHash 内容指纹去重（治理层 3，结果集内）| ✅ |
| 语义排序因子 bge-small-zh-v1.5 第 7 因子 | ✅ `services/embedder.py` |
| bge-reranker-v2-m3 cross-encoder 重排 | ✅ `services/embedder.py` |
| **hot-crawler（独立项目，3 个 spider，4 级 pipeline）** | ✅ `C:\code\AI\crawler` |
| MinHash + LSH 跨结果集洗稿检测 | ❌ 规划中 |
| AIGC 小模型检测（Qwen2.5-0.5B ONNX）| ❌ 规划中 |
| 源站准入控制（域名年龄 / ICP 备案）| ❌ 规划中 |
| 事实一致性校验（多源矛盾检测）| ❌ 规划中 |
| 治理路径分级（Fast / Standard / Deep）| ✅ `app/governance/paths.py` + `aigc.py`，AIGC 规则层已接入 Deep 路径 |
| 意图-商业容忍度映射（`query_commercial_tolerance`）| ❌ 预留设计 |
| 软广信息榨取（CTA 段剔除）| ❌ 规划中 |
| 基于 rescue rate 的动态黑名单 | ❌ 规划中 |

**阶段结论**：核心搜索 pipeline（引擎调度 → 治理 → 排序 → 输出）已完成工程闭环，关键基础设施（多实例 + 代理池、双 ES 索引、trace_id、bge embedder + reranker）均已落地。下一优先级：MinHash 跨域洗稿检测 + 测试覆盖率扩展。

---

## 十五、未实现功能与规划路线图

### 15.1 数据治理补强

| 编号 | 功能 | 设计位置 | 优先级 | 预计工期 |
|---|---|---|---|---|
| T-01 | ~~SimHash 内容指纹去重~~ | ✅ 已落地 `services/dedup.py` | — | — |
| T-02 | **MinHash + LSH 洗稿检测**（跨结果集）| `services/dedup.py` 扩展 | 🟡 中 | 1 周 |
| T-03 | **AIGC 检测**（Qwen2.5-0.5B ONNX）| `governance/aigc.py` | 🟡 中 | 2 周 |
| T-04 | **源站准入控制**（ICP + 域名年龄）| `governance/source_gate.py` | 🟡 中 | 2 周 |
| T-05 | **软广信息榨取**（CTA 段剔除）| `governance/extractor.py` | 🟢 低 | 1 周 |
| T-06 | **事实一致性校验**（多源 NER 矛盾） | `governance/fact_check.py` | 🟢 低 | M/L 档可选 |

### 15.2 排序层增强

| 编号 | 功能 | 状态 | 优先级 |
|---|---|---|---|
| R-01 | ~~语义排序因子接入~~ | ✅ 已落地 | — |
| R-02 | ~~bge-reranker-v2-m3 重排~~ | ✅ 已落地 | — |
| R-03 | **意图-商业容忍度映射** | 草案，未接线 | 🟡 中 |
| R-04 | **rescue rate 动态黑名单** | 7 天 rescue > 80% 且 impression > 10 自动拉黑 | 🟢 低 |

### 15.3 治理路径分级（已落地）

`app/governance/paths.py` + `app/governance/aigc.py` 已实现，三档路径均已接入 orchestrator：

```text
Fast:     URL 去重（跳过 SimHash + 商业评分）  — T1 白名单域名、技术类 Sites
Standard: Fast + SimHash + 完整商业评分       — 默认
Deep:     Standard + AIGC 规则检测            — intent=commercial / news
```

AIGC 检测（rules-based，无 ONNX）：句长方差 + 模板词密度 + CTA 密度，≥ 2 类触发 → `[AI生成]` 标签，<5ms/条。Qwen ONNX 检测作为 Deep 路径的可选增强保留为未来扩展点。

### 15.4 工程基础

| 编号 | 功能 | 说明 |
|---|---|---|
| E-01 | **测试覆盖率扩展** | 仅 `test_dedup.py` + `test_validator.py`，缺 auth / ranker / orchestrator / mcp / sse / proxy_client / log_shipper |
| E-02 | **PostgreSQL 指纹存储**（M/L 档）| SimHash 持久化，Redis 做 Bloom 预筛 |
| E-03 | **Nginx 多机部署** | `nginx/` 配置、SSE 反代、健康检查路由 |
| E-04 | **K8s Helm Chart**（L 档）| Gateway × 3 + Playwright × 2 + 推理节点 |
| E-05 | **种子数据脚本** | `scripts/seed_t1.py` / `refresh_icp.py` / `seed_aigc_templates.py` 未创建 |
| E-06 | **精度评估集** | `tests/eval_set.jsonl` 100 条月度回归（技术 / 新闻 / 政策 / 商业 / 学术） |

### 15.5 建议实施顺序

```text
第一步（已完成）：SimHash ✅ + embedding factor ✅ + bge-reranker ✅
                + SearXNG 多实例 + 代理池 ✅ + ES 日志独立索引 ✅

第二步（当前优先级）：T-02 MinHash 洗稿 + T-03 AIGC 检测 + E-01 测试覆盖
                      → 治理防线完整化

第三步（按需）：T-04 源站准入 + T-06 事实校验 + E-02 PG 指纹存储
                → M/L 档生产必备，S 档可跳过
```

---

## 十六、摘要层：BM25 抽取式

当前摘要完全基于 `rank-bm25`，**无 LLM 调用，无网络依赖，无 Token 成本**。

| 用途 | 实现 | 延迟 |
|---|---|---|
| `Result.SummarizedAnswer` | 跨 top-5 抽取 4 句，前缀去重 | <100ms |
| `WebResults[i].Summary` | 单条 `page_content` 抽取 3 句（query-aware） | <10ms/条 |

**混合分词器**：CJK 字级 + ASCII 词级，支持中英混合。`NeedSummary=true` 不需要任何配置，始终可用。
