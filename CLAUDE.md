# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 语言偏好

**始终用中文回复用户。** 所有解释、说明、问题和总结一律使用中文。代码、命令、文件路径保持原样不翻译。

## 提交规范

**每次 git commit 之前，必须先运行 `/simplify` 对本次改动做代码审查。** 审查通过、问题修复后再提交。

## 参考文档

| 文档 | 内容 |
|---|---|
| [`docs/api-reference.md`](docs/api-reference.md) | 请求/响应 schema、Filter 细节、SSE 端点 |
| [`docs/architecture.md`](docs/architecture.md) | Orchestrator 流程、引擎适配器、SearXNG/代理池、MCP、Auth、配置全表、Redis key |
| [`docs/elasticsearch.md`](docs/elasticsearch.md) | ES 索引架构、热点查询策略、日志索引 |
| [`docs/deploy.md`](docs/deploy.md) | 部署运维：.env 维护、hf_cache 卷、重启 vs 重建、代理调优、踩坑速查 |

## 项目概览

Prism is a self-hosted LLM web search MCP server — a low-cost alternative to commercial search APIs. It aggregates results from multiple search engines (SearXNG, DDGS, Serper, DataForSEO), deduplicates and ranks them, and exposes them via a FastAPI REST API and an MCP server.

## 技术栈

Python 3.11 · FastAPI · Redis · Elasticsearch 9.3 + IK 分词 · SearXNG（一源一实例分片 ×5）· MCP SDK  
rank-bm25 · Crawl4AI · trafilatura · APScheduler · Prometheus  
可选：`BAAI/bge-small-zh-v1.5`（排序第 7 因子 + 时效语义检测）· `BAAI/bge-reranker-base`（L3 精排，4c8g 推荐）

## Commands

### Install dependencies
```bash
pip install -e ".[dev]"                          # core + dev tools
pip install -e ".[dev,governance,browser]"       # full install with ML reranker + Crawl4AI
```

After installing `browser` extras:
```bash
crawl4ai-setup   # or: playwright install chromium
```

### Run locally
```bash
python -m app.main
uvicorn app.main:app --reload
```

### Docker (recommended)
```bash
docker compose up -d          # starts gateway + searxng-baidu/bing/quark/sogou/wechat（一源一实例分片；Redis 为外部依赖，不在 compose 里；代理隧道在 proxy-hub 侧）
docker compose logs -f gateway
```

> gateway 代码通过 bind mount 同步，但 uvicorn 未启用 `--reload`；修改后需 `docker compose restart gateway`（模型预热约 40s，healthcheck start_period 240s）。proxy-hub 侧已启用 `--reload`。

> **本地 ES**：`C:\elasticsearch\elasticsearch-9.3.3\bin\elasticsearch.bat`
> 容器通过 `http://host.docker.internal:9200` 访问；本地开发用 `localhost:9200`。IK 分词插件已安装。

### Lint / test
```bash
ruff check . && ruff format .
pytest                          # all tests
pytest -x -v                    # fail-fast
```

### Read logs
```bash
tail -f logs/prism.log | python -m json.tool        # 全量
tail -f logs/prism.err.log | python -m json.tool    # 只看 ERROR（排障先看这个）
tail -f logs/timing.log | python -m json.tool       # 链路耗时
docker compose logs -f gateway
```

**日志字段规范**（文件 JSON 与 ES `prism_logs` 同名字段，三项目 prism/proxy-hub/crawler 统一）：
`@timestamp / level / logger / msg / service` + 追踪键（`trace_id`=HTTP 链路、`query_id`=一次搜索全链路）+ 结构化块 `event/metrics/engine/error`。
一条日志 = 一个事件：msg 是人读短句，数据全走字段。全链路排查：

```bash
grep '"query_id":"abc123"' logs/prism.log | python -m json.tool   # 本地文件
# Kibana（prism_logs 数据视图）：query_id : "abc123" → search.start → engines_done → rank → done
# 建议建保存视图，列只留：@timestamp / level / logger / msg / query_id / metrics.latency_ms
```

crawler 一轮爬取用 `run_id` 检索（crawler_log 索引）；proxy-hub 侧一次补位用 `batch_id` 检索（proxyhub_logs 索引；滚动补位模型下与查询解耦，prism 日志侧无对应字段）。**跨链路排查关联机制**（2026-09-04 起）：① **query_id 直达**——prism 调 `/refresh`、`/activity` 自动携带 `{"query_id"}`，proxyhub_logs 直接按 `query_id` 检索，刷新/活跃信号与同一秒的池事件精确对齐（`/activity` 有 10s 节流，仅作旁证，确定性锚点靠 `/refresh`）；② **query 人可读锚点**——信号同时自动携带查询词（落 proxyhub_logs `metrics.query`），直接在 Kibana 搜关键词（如 "asyncio"）即可定位那批池事件，不必翻 prism 日志复制 uuid；③ **forwarder 连接日志**——每条隧道连接一行 `forwarder: conn <project> <client_ip> → <target_host> via <upstream_ip> <ms>ms`（metrics 含 target/upstream/client/elapsed_ms），client_ip 对应 SearXNG 实例（经宿主机 NAT 时为网关 IP，多实例共享，只能按秒级窗口拼接）。回退手段仍是裸时间窗（`prism_logs` 无 `instance_id` 字段）。

### Manage API keys
```bash
python scripts/create_api_key.py --tenant my-app --quota 5000 --qps 10
python scripts/seed_denylist.py
```

## 项目结构

```
app/
├── main.py              # FastAPI 入口 + MCP SSE 挂载 + 全局异常处理
├── api/                 # REST 路由层
│   ├── search.py        # /api/search、/api/search/stream
│   ├── admin.py         # /admin/* 管理接口
│   ├── feedback.py      # /api/feedback
│   ├── fetch.py         # /api/fetch 单 URL 抓取
│   ├── health.py        # /health、/ready
│   └── compat.py        # Volcengine 响应格式转换
├── core/                # 基础设施
│   ├── config.py        # pydantic-settings 统一配置（读取 .env）
│   ├── auth.py          # API Key 鉴权、租户配额、滑动窗口限流
│   ├── logging.py       # trace_id 注入的日志配置
│   ├── redis.py         # 异步 Redis 连接池
│   ├── session.py       # session_id 解析
│   └── validator.py     # 输入校验（query、top_n、user_id 等）
├── engines/             # 搜索引擎适配器
│   ├── base.py          # BaseEngine 抽象 + RawResult 定义
│   ├── searxng.py       # Tier 1（SearXNG 分片：一源一实例 ×5，SEARXNG_SHARDS 映射；空则回退多实例轮询）
│   ├── ddgs.py          # Tier 1b（默认禁用）
│   ├── serper.py        # Tier 2
│   ├── dataforseo.py    # Tier 2
│   └── volcengine.py    # Tier 2
├── mcp/                 # MCP 服务端
│   └── server.py        # web_search / search_feedback 两个 tool
├── schemas/             # Pydantic 模型
│   └── search.py        # SearchRequest、SearchResult、VolcSearchResponse 等
└── services/            # 核心业务逻辑
    ├── orchestrator.py  # 搜索编排器（缓存 → 引擎调用 → 去重 → 排序 → 返回）
    ├── proxy_client.py  # proxy-hub 客户端：节流 POST /activity、POST /refresh
    │                    #   （一律 Bearer 项目 token 鉴权，PROXY_PROJECT_TOKEN）；
    │                    #   另有 proxy-hub 隧道出网地址（fetcher/ChinaSo 用）
    ├── ranker.py        # 7 因子异步混合排序 + L3 精排分数闸门
    ├── relevance_gate.py # L1 覆盖率闸门 + 协调因子 + 导航意图规则
    ├── fetcher.py       # 页面正文并行抓取（trafilatura → Crawl4AI 双级降级）
    └── ...

tests/                   # pytest 测试
scripts/                 # 运维脚本
searxng/                 # settings.template.yml（唯一 settings 模板）+ entrypoint.sh / render-settings.sh（启动渲染隧道列表）+ limiter.toml
                         # + overrides/baidu.py（baidu 引擎 HTML 解析版，仅挂进 searxng-baidu 片）
gost/                    # 历史留档：早期本机 forwarder 的 Dockerfile/gost.yml（隧道化后 compose 不再引用）
```

## 部署架构（代理出网链路）

```
SearXNG instance-N ──出网经──> proxy-hub 隧道地址（HTTP 代理带项目账密，如 http://prism:<token>@<proxy-hub>:8101，从 PROXY_SERVICE_URL 推导：host 相同、端口换 8101）──> 短效上游 IP ──> 搜索引擎
                                        ↑ proxy-hub 自建 HTTP 转发代理（无 gost），每连接按目标站防重轮换挑上游 IP
```

**代理池已拆分为独立 proxy-hub 服务**（独立仓库，通用设计）。gateway 内不维护代理池，只通过 `app/services/proxy_client.py` 与其交互。项目隔离、滚动补位、主备锁、冷热分档、热配置等都在 proxy-hub 侧，详见其 README。

gateway 与 proxy-hub 的交互（`app/services/proxy_client.py`，一律 Bearer 项目 token 鉴权）：
- 每个搜索请求节流 `POST /activity`（驱动 proxy-hub 按需补位与 QPS 自适应）；
- 走代理的引擎（searxng/chinaso）熔断跨阈值时 `POST /refresh` 请它换批新 IP
  （proxy-hub 侧两阶段替换：先补新再淘汰旧，刷新期间池子不空）。请求带
  `wait_s=timeout_s-1`：撞上持锁方正在刷新时服务端等它落地再应答（等锁成功且
  池已可下发直接复用不重复抽），补位撞供应商冷却且旧池不可下发时等短冷却后
  再试一次——消费方"等到新 IP 再用"由服务端实现，被秒拒后 100ms 重试撞同一
  坏窗口的快速认输不会再发生（2026-09-04 NBA 零结果根因修复）；
- 验证码/IP 被风控：searxng 返回 200 但 0 结果且过半子引擎 unresponsive 时抛
  `ProxyQualityError`（`app/engines/base.py`），编排层记熔断 + 用额外预算
  （`PROXY_RETRY_EXTRA_BUDGET_S`，默认 5s）等 proxy-hub 补新 IP 后重试——
  晚几秒响应好过返回 0 结果；同时 `POST /block-signal` 反哺 proxy-hub
  （`proxy_client.report_block`，按目标站 5s 节流）：HTTPS 隧道加密，
  proxy-hub 数据面看不到风控页，由引擎层上报目标站、proxy-hub 按 forwarder
  近期建联映射关联出当时用的上游 IP，**按目标站拉黑**（该 IP 不再派给此站，
  其他站照用；≥3 站风控才真剔除）——2026-09-09 禁区波复盘新增；
- SearXNG / fetcher / ChinaSo 统一经 proxy-hub 隧道出网。隧道地址不写死在任何配置里：
  从 `PROXY_SERVICE_URL`（proxy-hub 拓扑唯一事实源）推导——host 相同、端口固定换 8101，
  账密 `prism:<PROXY_PROJECT_TOKEN>` 注入。SearXNG 侧由容器 entrypoint 调
  `searxng/render-settings.sh` 在启动时渲染 `settings.template.yml` 的隧道占位符行
  （渲染产物过一道 YAML 硬校验，非法拒启动；改动渲染逻辑必须跑
  `tests/test_searxng_render.py`）；gateway 侧由 `proxy_client._tunnel_urls()` 推导。
  加/换 proxy-hub 机器只改 `PROXY_SERVICE_URL` 一行。

proxy-hub 自建转发代理监听固定端口（8101）：消费方按上述推导得到隧道地址（账密 =
项目名:项目token），prism 机器上不再有本机 forwarder，也没有 gost sidecar。转发代理
每连接挑 IP 时做**目标站防重轮换**（同一 IP 不连续打同一搜索引擎，都给过一遍才开第二轮），
换 IP 零窗口（历史上靠重启 SearXNG 换 IP，5-15s 预热期查询全超时，即"重启风暴"零结果
根因，已消除）。
模型权重存 `hf_cache` 命名卷（旧镜像未烘焙模型时模型只在卷里，删卷挂载会触发 HF
重下、国际节点超时）。

### SearXNG 拓扑：一源一实例分片

`SEARXNG_SHARDS`（.env，格式 `引擎名=实例URL` 逗号分隔）非空时启用分片：baidu/bing/
quark/sogou/sogou wechat 各占一个 SearXNG 实例，gateway 注册为 5 个独立逻辑引擎
`searxng/<引擎名>`。设计要点：

- **容器零差异化**：5 实例同镜像同 `settings.template.yml`，"跑哪个源"完全由请求参数
  `engines=` 切片决定；compose 里 5 份 service 定义仅服务名不同。
  唯一例外是 searxng-baidu / searxng-quark 各多挂一个引擎 override（见下两条）。
- **baidu 引擎 HTML 解析版 override**（`searxng/overrides/baidu.py`，volume 挂进
  searxng-baidu 覆盖镜像内同名引擎）：上游 `tn=json` 接口 100% 触发 302 → wappass
  验证码（2026-09 A/B 实测，独享干净 IP + 全浏览器头也一样）；去掉 `tn=json` 后
  HTML 页 200 稳定。另一个实测分流点：SearXNG 默认出站头不设 Accept（实际发出
  `Accept: */*`）→ 百度一律 302 验证码，override 的 request() 给 headers 补上浏览器
  Accept（不覆盖模板配的 Referer 等已有头）。override 版 request 去掉该参数
  （rn/pn/gpc 保留），response 改走
  lxml HTML 解析（容器 `div.result.c-container`，标题/链接 `h3//a`，摘要按
  c-abstract → summary-text → content-right → c-span-last 候选兜底；h3 class 含
  `ec_title` 或带"广告"角标的容器过滤；JS 壳页自然 0 结果）；wappass captcha 检测
  保留。⚠️ 镜像内置 `__pycache__` 是 unchecked-hash pyc（不校验源文件），挂载新源码
  会被旧字节码静默盖掉——`entrypoint.sh` 检测到 override 标记（`parse_general_html`）
  时逐出对应 pyc，改 override 后 `docker compose up -d --force-recreate searxng-baidu`
  即可生效。回归测试 `tests/test_searxng_baidu_html.py`（固件在 outputs/）。
- **quark 引擎挂起时长 override**（`searxng/overrides/quark.py`，volume 挂进
  searxng-quark 覆盖镜像内同名引擎）：上游 quark.py:89 硬编码
  `raise SearxEngineCaptchaException(suspended_time=900, ...)`——为直连固定 IP 设计
  （Alibaba CAPTCHA 封禁约 15 分钟，挂 900s 等它自然解除），覆盖 settings 的
  `SearxEngineCaptcha: 10`，导致一次 CAPTCHA 就把 quark 片挂起 15 分钟、召回长期缺席
  （2026-09-22 实测 20 query quark 出勤 0/20，全是 34ms 挂起 fast-fail）。override 去掉
  `suspended_time=900`，回落 settings 10s（隧道每连接换 IP，下一请求即新 IP，无需等
  封禁解除），与 baidu override 同节奏；其余逻辑与上游完全一致。pyc 逐出同款：
  entrypoint.sh 检测 override 标记 `PRISM_OVERRIDE` 逐出镜像内置 quark.cpython-*.pyc，
  改 override 后 `docker compose up -d --force-recreate searxng-quark` 生效。
- **慢源/卡死隔离**：每片独立超时、独立熔断（`circuit:searxng/<引擎>`）、独立
  ProxyQualityError（该片唯一引擎 unresponsive 即判代理质量失败，触发 proxy-hub
  refresh）——一个源缺席不再拖垮整查（旧单引擎模式下 SearXNG 聚合打包：任一慢源把
  整包拖过 prism 5s 剪线，已到结果一起陪葬，整查 0 结果）。
- **聚合打包 vs 剪线预算**：SearXNG 无流式模式，"等全部引擎结束才一次性打包 JSON"。
  模板 per-engine timeout 3.5s（全部源）让慢源到点即弃、聚合 ~3.7s 完成 < prism
  `ENGINE_TIMEOUT_S=5`——曾出现 5s 剪线 < SearXNG `max_request_timeout` 6s 的预算倒挂，
  最好情况也是零结果。
- **分页**：页留在各片内部（page N → 该片实例带 `pageno=N`），不再跨实例路由。
  公式分母 = **总分片数**（= 旧模式子引擎数），分片前后总 raw 量严格等价；翻页 IP 不重复
  靠三层既有机制——forwarder 目标站防重轮换（每连接优先挑没服务过该站的 IP）+ 池深
  ≥ 页数（页数封顶 3 ≤ offpeak 池 3）+ 出站 `Connection: close` 每页必新连接。
  "page N → 实例 N"的旧路由本意是同 IP 翻页 CAPTCHA，已被隧道每连接轮换 IP 消解。
- 留空 `SEARXNG_SHARDS` 回退旧全引擎多实例模式（`SEARXNG_URLS`，page N 路由实例 N），
  单实例开发环境不受影响。

proxy-hub 侧沿用已验证的代理策略：三态健康探测（经 IP 发一次 bing 搜索：能搜索/能出网/死，
有"能搜索"优先、绝不空池）、连续 2 轮失败才剔除（防单次抖动误杀）、qg.net 用 `server`
中转网关连接（非 `proxy_ip`）、deadline 北京时间显式挂 `Asia/Shanghai` 比对。
残留零结果（引擎超时/503/CAPTCHA）已随住宅 IP 大幅下降；仍要近乎零可开 `TIER2_DEFAULT` 付费兜底。

### 代理池节奏参数速查

| 参数 | 默认值 | 含义 |
|---|---|---|
| 套餐生命周期 | 2min | qg.net 短效住宅 IP，以供应商返回的 `deadline` 为准 |
| `alloc_min_ttl_s` | 25s | IP 剩余寿命低于此值不再发放（下发禁区）；全体进禁区时回退发剩余寿命最长的（将死 IP 好过隧道 502 秒拒） |
| `health_dead_after` | 2 | 连续探测失败轮数才剔除 |
| peak 巡检间隔 | 15s | peak/normal 档 `health_recheck_interval_s` |
| offpeak 巡检间隔 | 30s | offpeak 档 `health_recheck_interval_s`（120s 会让 2min 套餐终生不复检）|
| peak 池大小 | 6 | `fetch_num` |
| normal 池大小 | 4 | `fetch_num` |
| offpeak 池大小 | 3 | `fetch_num` |
| 空闲阈值 | 300s/600s | peak/normal 与 offpeak 的 `idle_threshold_s`；超过则池归零 |
| 维护锁 TTL | 60s | 维护锁 TTL；主宕机后 ≤60s 内备机接管 |
| 热配置生效 | ≤15s | 热配置生效延迟；≤15s（Redis + 10s 进程缓存） |
| 分批补位防抖 | 缺口≥3 分批 | `topup_stagger_min_n/chunk_size=2/delay 2-5s`；防同批 IP 同 deadline 齐到期成"过期波"，池空/紧急整批快补 |
| 提前补位 | 维护轮投影 | 按"再过 `alloc_min_ttl_s` 仍可下发"投影水位提前补位，旧 IP 用满寿命不提前杀 |
| 低水位快补 | ≤目标一半 | 冷却缩到 `low_pool_cooldown_s`（默认 3s，热配置）+ refresh_tick 1s 一轮；空池持续 30s 打 ERROR（proxyhub_logs 可配告警） |
| 流式入池 | 探完即发 | 体检 SEARCHABLE 探完即刻入池（不等整批），唤热首个可用 IP ~2s |

> 所有时段表/档位参数/判死轮数/提前抛弃量都走 proxy-hub 热配置，不要改 prism 代码。

## 常见错误与排查

| 症状 | 原因 | 解法                                                                                                                                                                                                                                                                 |
|---|---|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `raw_count=0` + `engine.timeout`/`ProxyError 503`/`CAPTCHA` | 上游 IP 慢/死/被风控 | 查 proxy-hub `/status` 与日志，调热配置；仍残留则开 `TIER2_DEFAULT`                                                                                                                                                                                                               |
| `search.engines_done` 长期只剩 1-2 个 `sub_engines` / sogou 陈货霸榜首条 | 引擎吃 CAPTCHA 被 SearXNG 按**引擎名**挂起 `suspended_times.SearxEngineCaptcha`；**换 IP 不清这个挂起**（二者解耦，挂起期内换到干净 IP 也被跳过），默认已 900→30s 对齐换 IP 节奏 | 改模板 `searxng/settings.template.yml` 的 suspended_times + `docker compose up -d --force-recreate`（5 个分片实例全量重建；restart 不重新渲染 settings）+ 多抽干净 IP；仍抖动说明 IP 脏，开 `TIER2_DEFAULT` 兜底                                                                                       |
| proxy-hub 日志 `all fetched proxy IPs failed health check` | 探测全灭(别用百度做探测 URL，会全误杀) | 确认 proxy-hub 的 `HEALTH_CHECK_URL` 是 bing(`cn.bing.com/search?q=新闻`)，不是百度                                                                                                                                                                                           |
| 启动卡在 `embedder ready` 之后无 `reranker ready`/`Prism ready` | CentOS7 老 libgomp + tokenizers 在线程池里加载 reranker 死锁 | gateway 环境设 `OMP_NUM_THREADS=1` + `TOKENIZERS_PARALLELISM=false`（已写入 compose）                                                                                                                                                                                      |
| 启动一直下载模型(`cas-bridge.xethub.hf.co` 超时) | `hf_cache` 卷挂载被删 → 镜像未烘焙模型 → 去 HF 重下 | 确认 docker-compose 保留 `hf_cache:/root/.cache/huggingface` 卷挂载（模型在卷里）                                                                                                                                                                                                |
| 改了代码/配置后 `docker exec` 报 `getcwd() failed`/`No module named app` | 宿主机 repo 目录被删重建，容器 bind mount 指向旧 inode 失效 | `docker-compose up -d --force-recreate`（重建容器重新挂载）；`restart` 不够                                                                                                                                                                                                     |
| 生产 `deploy-prod.sh config/build` 报 `Read timed out (read timeout=300)` 或 `OCI runtime create failed ... pid from pipe caused "EOF"`；重试 5 轮后仍 `exited`，日志里容器名带 `<hash>_` 前缀 | 两条叠加：① `gateway` 的 `depends_on` 让 `up --force-recreate gateway` 连带重建 5 个 searxng，串行策略塌成 6 容器并发；② 宿主机 Storage Driver 是 `vfs`（无层共享），每个容器创建=整份镜像拷贝，并发之下 daemon 内存/磁盘/inode 吃紧，runc 子进程写回 pid 前就死了 | 已修（`deploy/deploy.sh`）：**恒定逐服务串行**（`compose_up` 不再按驱动分流）+ `--no-deps`、跨轮次跳过已成功重建的服务、每轮清 `created`/`dead` 残留、`COMPOSE_HTTP_TIMEOUT` 默认 600。现场卡死先 `bash deploy/recover-docker.sh`（清理后仍 EOF 才加 `--restart-daemon`）。根治见 [`docs/deploy.md`](docs/deploy.md) 「存储驱动是 vfs」 |
| 脚本报 `全部服务正常运行`、`/health` 也通，但之后 `docker ps` 里没有某容器（如 gateway） | 校验只在收工那一刻采样一次，容器可能在其后才崩 | 已加收工复核 `verify_stable`：硬判失败只认「不在运行态 / `running(restarting)` 崩溃循环 / 窗口内重启次数增长」；`RestartCount>0` 但稳定 running 属启动期自愈，**只提示不判失败**（曾收紧成硬判据，把一次成功部署误判为失败，见 [`docs/deploy.md`](docs/deploy.md)「收工复核」）；死因排查跑 `bash deploy/diagnose.sh`                                     |
| 本机服务明明正常，但请求都由对端机器响应（本机 access 日志无流量）；或本机一直不接流量 | 故障转移的维护标记文件残留——「切对端」配置下残留**不报错**（流量静默走对端，HTTP 探测发现不了），多为部署失败时按 `NGINX_KEEP_FLAG_ON_FAILURE=1`（默认）故意保留，或脚本被 `kill -9` 未触发 trap | 重跑一次部署即自愈（重放标记→成功→自动清除）；或手工 `rm -f /opt/nginx/maintenance/prism.flag`（无需 reload）；临时关闭该机制：`NGINX_MODE=off bash deploy/deploy-prod.sh config`。见 [`docs/deploy.md`](docs/deploy.md)「故障转移」                                                                             |
| `docker ps` 里 gateway 显示 `Up N minutes`，但 `CREATED` 比它早好几分钟，且 `重启次数` ≥1 | gateway 冷启动 200s+（vfs 创建=整份镜像拷贝），首次启动偶发退出后被 `restart: unless-stopped` 拉起，第二次启动成功 → 属自愈，部署成功 | 不影响服务；想查第一次为什么退出：`docker logs --tail 100 prism_gateway_1` + `docker inspect prism_gateway_1`（看 `ExitCode`/`OOMKilled`/`State.StartedAt`）                                                                                                                           |
| 本地（Windows）容器里 searxng 全部 `restarting` 重启次数飞涨，日志尾部是 `yaml.scanner.ScannerError: mapping values are not allowed here in "/etc/searxng/settings.yml", line NNN` + `FATAL: settings.yml 渲染产物 YAML 非法` | 工作区文件被 `core.autocrlf=true` 检出成 CRLF：Linux 上 `^  __PROXY_TUNNEL_PROXIES__$` 匹配不到带 `\r` 的行 → 占位符既不替换、也不报残留（静默通过）→ 产物留着占位符，下一行 `  request_timeout:` 的冒号被当成非法 mapping（2026-09-17 本地 5 实例全灭；MSYS 本地复现不出来，因为 Git Bash 的 sed 把 CRLF 当行尾） | 已修：`searxng/render-settings.sh` 对模板与两个 env 值做 `tr -d '\r'`、残留检查容忍行尾 CR；`.gitattributes` 扩为 `* text=auto eol=lf`。自查 `git ls-files --eol \| grep -v 'w/lf'`，历史检出 `git add --renormalize .`。回归测试 `tests/test_searxng_render.py::TestLineEndingRobustness`             |
| `Redis connection refused` | Redis 未启动 / 端口错误 | 检查 `REDIS_URL`（远程集群），`REDIS_CLUSTER_MODE=true`                                                                                                                                                                                                                     |
| `search.done result_count=0` 但 `raw_count>0` | 结果被相关性闸门过滤光（非代理问题） | 检查 L1 覆盖率闸门/L3 分数闸门，降 `RERANKER_SCORE_FLOOR`                                                                                                                                                                                                                       |
| 响应 >10s | 冷门 query 走 SearXNG 全链路 | 开启 `HOT_INDEX_ENABLED=true`，热点 query 走 ES，≤500ms                                                                                                                                                                                                                   |
| `NeedContent=true` 抓正文慢/`fetched=0/N` | 出网慢，单页 TLS 握手 2-3s，抓取赶不上墙钟闸 | 调大 `CONTENT_FETCH_BUDGET_S`（默认 5s，慢机房用 10s）                                                                                                                                                                                                                        |
| `fetch.failed` 整批全灭（尤其 toutiao/sina/zhihu/baijiahao 等国内内容站） | 看出网链路/单站风控：日志带 `reason=download:<egress>:<异常>` | `CONTENT_FETCH_EGRESS` 默认 `proxy_first`（代理池运营商 IP 优先、直连兜底）；宽带出口环境可改 `direct_first`；`weixin.sogou.com/link` 跳转壳必进验证码墙，已直接跳过                                                                                                                                         |
| 结果全是某低信任域名的陈年货（如 10/10 weixin.sogou.com 2016-2022 旧文） | 分片大面积被风控只剩独苗源；日志应有 `search.recall_monopoly` | 自动刷新 IP 重试（`RECALL_MONOPOLY_*`）；N 天前低信任结果由时效硬过滤丢弃（`RECALL_STALE_MAX_AGE_DAYS`，默认 730）；反复出现说明代理池质量差，查 proxy-hub                                                                                                                                                     |
| prism 日志 `proxy_client: /activity 403` / `refresh 403` | `PROXY_PROJECT_TOKEN` 缺失/错误 | 在 proxy-hub 注册 "prism" 项目，将 token 填入 `.env`                                                                                                                                                                                                                        |
| proxy-hub 日志 `prism refresh skipped, maintenance lock held` | 主备锁竞争，另一副本正在维护 | 正常现象；持续超锁 TTL 则检查是否有僵死副本                                                                                                                                                                                                                                           |
| proxy-hub `GET /status` 返回 `"available":false` | 池真空（全部自然过期；禁区回退也无 IP 可发） | 立即 `POST /refresh`；检查是否空闲过久                                                                                                                                                                                                                                        |
| 首查/空闲后首查零结果，随后正常 | 池归零后唤热不够快 | 检查 `ensure_warm()` 是否触发；`PROXY_SERVICE_URL` 是否可达                                                                                                                                                                                                                   |

> 零结果排查铁律：先看 `search.engines_done.raw_count`：`raw_count=0` 是引擎层问题（查 SearXNG 日志、proxy-hub `/status` 和 `proxyhub_logs`）；`raw_count>0` 而 `result_count=0` 是排序/闸门问题。跨链路排查无直接关联键，按时间窗关联 `proxyhub_logs`（方法见上文「日志字段规范」段）。

## 命令别名

```bash
# 快速查看某次请求的完整链路
grep 'abc123' logs/timing.log | python -m json.tool

# 压测（需 ab）
ab -n 100 -c 10 -H 'Authorization: Bearer <key>' 'http://localhost:8000/api/search?q=test'
```

## 测试规范

- 测试文件在 `tests/`，命名 `test_*.py`
- Redis：用 `fakeredis` 替代真实 Redis，**不用 `unittest.mock` 打桩**——fake 可覆盖真实 key/TTL 语义，mock 打桩会掩盖行为差异
- ES：本地不可用时用 `pytest.mark.skipif` 跳过，不用 mock 替代

## 安全与合规

- **API Key 鉴权**：请求头 `Authorization: Bearer <key>`；key 在 Redis 中以 `sha256` 存储
- **PII 保护**：`user_id` 使用 HMAC-SHA256 加盐存储为 `uid_hash`，盐值由 `HMAC_SALT` 提供
- **SSRF 防护**：`fetcher.py` 对抓取 URL 有校验，防止内网穿透
- **robots.txt**：页面抓取前检查并缓存 24h；若禁止则内容仍返回但 URL 脱敏为"来自网络搜索"
- **域名黑名单**：Redis 集合 `denylist:domains` 实时过滤
- **个保法合规**：`/admin/preferences/{user_id}`（删除用户偏好数据）**尚未实现**——代码中无此端点，勿按旧文档调用

## Architecture Overview

核心流水线：`app/services/orchestrator.py`

```
请求 → 时效检测(正则+向量) → 查询改写 → 缓存检查
     → ES热点索引(可选,L1闸门) → Tier1并发搜索(over-fetch top_n×5)
     → 去重(URL 归一化剥 query；跳转型端点如 weixin.sogou.com/link 例外，只保留 url= 身份参数) → denylist → 去垃圾农场(spam_gate) → Tier2兜底(过滤后池薄才触发)
     → 过滤 → L1覆盖率闸门 → 排序(7因子+协调因子+spam软降权) → L3精排+分数闸门 → 后处理 → 缓存写入
```

时效检测两阶段：**正则 fast-path**（今天/昨天/这两天/近N天/上周/最新等）→ **向量 fallback**（`bge-small-zh-v1.5` centroid cos≥0.65，覆盖"前阵子"/"这段时间"等方言表达）。检出后只设 `intent=news`（recency 排序加成），**不再注入硬时间过滤**——旧版强制 `TimeRange=OneDay`（→`time_range=day`）会把常青/季节/医学类查询（"春季流感最新…指南"）召回腰斩，且 baidu/sogou 对 time_range 松散执行残骸只剩陈货；硬时间窗仅在调用方**显式传 `TimeRange`** 时才下发引擎（`_safe_fetch`，默认 `""` 不下发 time_range），新鲜度交给排序 recency 因子。**发给引擎的 query 会剥离独立时效副词**（最新/刚刚/今日…，`rewriter.strip_temporal_markers`），避免它们当 BM25 词招来盖日期戳的农场/百科页（粘连词如"最新款"不动）。

引擎侧 query 归一化（`rewriter.normalize_for_engine`，`_safe_fetch` 内常开）：在时效副词剥离之外，再做**框架词软剥离**（`strip_framing_markers`，`ENABLE_FRAMING_STRIP=true` 默认开）。多约束长尾查询（"适合油性敏感肌的平价洗面奶推荐"）整串无空格，主导框架词「适合」会把它坍缩成单高频词，召回一池子 `适合_百度百科`/词典页。jieba 切词后剥掉高精度纯框架词（适合/求/想要/推荐/有哪些/怎么样/的…）→ query 变宽（"油性敏感肌平价洗面奶"），推荐/横评内容照样命中。**只删词表内的整词**（"推荐信"被切成整词≠"推荐"，"测评/天梯图"等内容形态词不在表内，天然不误删）；唯一护栏：剥到有效字符 <2 回退原串。**原串始终保留给 coverage/BM25/rerank 做相关性**，剥离只拓宽召回。字典/百科页若随召回改善仍残留，再按需补"非释义 query 降权纯释义页"（暂未做）。

ES 热路：`HOT_INDEX_ENABLED=true` 时查 `hot_news` 索引，先经 `min_coverage=0.5` L1 覆盖率闸门过滤，通过后命中 ≥ `HOT_INDEX_MIN_HITS` 条直接返回，跳过 SearXNG。**硬实时数值查询（天气/气温/股价/汇率等）跳过 ES**（ES 无此类数据，否则"最新天气预报"被时效编码成 intent=news 后会召回含"北京时间/2026年"泛词的无关热榜——天气→世界杯）；唯一例外是**调用方显式传 `intent=news`**（`intent_explicit`，区别于时效策略把 auto 提升成的 news）——财经快讯源（eastmoney_flash/ths_flash/cls_telegraph/nfra）已入 `hot_news`，"今日股市相关新闻"类新闻查询由热路服务。油价调整、赛事比分等真新闻事件不在此列，仍走热路。覆盖率闸门按**话题词**判定（`topic_query` 剥掉"今日/相关/新闻"等噪声词后的核心词，空话题则跳过闸门）——否则"今日X相关新闻"模板的噪声 token 会把覆盖率稀释到 0.5 以下致热路全灭；**纯分类话题**（话题词恰好是"股市/财经/数码"等分类词，`is_category_topic`）同样跳过闸门——其召回靠 source terms 子句，快讯标题不会字面带分类词，闸门会误杀精确召回；含分类词的复合话题（"茅台股市"）仍走正常闸门。覆盖率计算已剔除年份/月日时间数字泛词。

相关性 + 反垃圾防线（`spam_gate.py` + `relevance_gate.py` + `ranker.py`）：
- **L0 反垃圾闸门**（`spam_gate.py`，dedup/denylist 之后、rank 之前）：reranker 拦不住关键词堆砌的 SEO 内容农场（snippet 真含查询词，cross-encoder 给高分）。三信号——A 热搜拼盘（snippet 多碎片、含查询词碎片占比≤0.3）、B 域名抛弃型（wap./3g. 深层 mobile 前缀 或辅音乱码二级域）、C 色情/赌博/标题党词典。判定：`C` 无条件硬丢（合规）；`A∧B` 硬丢；单独 A/B 软标记 `spam_soft`（ranker ×0.5 降权）。合法热榜站（tophub 等）+ T1/T2 可信域豁免 A/B。
- **L1 覆盖率闸门**：query 分词去停用词后，title+snippet 零命中的结果排序前丢弃（拦引擎填充噪音）。**杀空保护**：冷路（`kill_empty=False`）下若过滤会清空非空结果集则不清零——全零覆盖多为 jieba 长复合词与措辞不匹配（词"个人所得税"匹配不上 snippet 的"个税/所得税"），保留交给 L3 语义判定（有返回就不该被一道词面闸门清零）。热路（`kill_empty=True`）仍杀空以降级 SearXNG。
- **L2 协调因子**：得分 × (0.5 + 0.5 × 覆盖率)，弱覆盖结果沉底；单 token 查询不惩罚
- **L3 精排 + 三道闸门**：`bge-reranker-base` 对 top-20 重排。① 绝对地板 `RERANKER_SCORE_FLOOR`（sigmoid 低于此丢弃假相关 SEO 页）；② 相对断层 `RERANKER_SCORE_MARGIN`（丢比当批最高 sigmoid 低超此值的，阈=`max(floor, top-margin)`）；③ **塌缩闸门** `RERANKER_COLLAPSE_CEILING`（整批最高 sigmoid 都低于此=召回失败/全跑题，只留 `RERANKER_COLLAPSE_KEEP` 条，治字典页/农场页中毒导致整批 dropped=0 全交付）。护栏：`min(3)` 回填不拉回低于绝对地板的真垃圾

导航意图（"官网"类查询）：根路径 URL 加 boost +0.15，UGC/博客平台（csdn/zhihu 等）惩罚 -0.20。
非导航查询：纯首页（根路径）结果惩罚 -0.15（`relevance_gate.homepage_penalty`）——信息类查询要具体文章不要门户首页（新华网/央视网首页那批答非所问）。

7因子排序权重（`intent=auto`）：recency(0.30) + source_trust(0.20) + BM25(0.20) + domain_reputation(0.15) + session_boost(0.10) + commercial_penalty(0.05)。`EMBED_MODEL` 启用时加第 7 因子语义相似度。

详细流程见 [`docs/architecture.md`](docs/architecture.md)。

## Key Configuration

```bash
# 必须设置
HMAC_SALT=                    # openssl rand -hex 32
ADMIN_BOOTSTRAP_KEY=          # 首次启动创建 admin key

# Redis
REDIS_URL=redis://localhost:6379/0   # 集群模式填任意节点，自动发现其余节点
REDIS_CLUSTER_MODE=false             # true = Redis Cluster；false = 单节点/哨兵
REDIS_KEY_PREFIX=                    # 命名空间前缀，如 prism:（多服务共用 Redis 时隔离用）

# 引擎（360search 已移除：对机房/代理 IP 反爬激进，常返回空 body Document is empty）
# sogou wechat：公众号文章，时效/正文优于通用网页；bilibili 已移除（视频对网页查询是噪音）
# 唯一来源是 .env（compose 不再覆盖，改引擎只改这一处）
# 分片拓扑：一源一实例（详见「部署架构 → SearXNG 拓扑」）；留空回退 SEARXNG_URLS 多实例模式
SEARXNG_SHARDS=baidu=http://searxng-baidu:8080,bing=http://searxng-bing:8080,quark=http://searxng-quark:8080,sogou=http://searxng-sogou:8080,sogou wechat=http://searxng-wechat:8080
SEARXNG_ENGINES=baidu,bing,sogou,quark,sogou wechat
TIER2_DEFAULT=none            # none | dataforseo | serper | volcengine（廉价 IP 想近乎零零结果靠这个付费兜底）

# 代理出网（代理池由独立 proxy-hub 服务托管；供应商/分档/巡检配置都在 proxy-hub 侧热配置）
# ⚠️ PROXY_SERVICE_URL 是 proxy-hub 拓扑唯一事实源：gateway 控制面（Bearer）、gateway/searxng
# 隧道地址（host 相同、端口换 8101 推导）都由它驱动；token 唯一事实源是 PROXY_PROJECT_TOKEN。
# 改 env 必须 docker compose up -d（--force-recreate），restart 不重读 .env。
PROXY_SERVICE_URL=                   # proxy-hub 地址（逗号分隔多副本，如 http://<proxy-hub-1>:8100,http://<proxy-hub-2>:8100）；空=整条 proxy-hub 链路停用（直连出网）
PROXY_PROJECT_TOKEN=                 # proxy-hub 项目 token（proxy-hub 侧注册 "prism" 项目获得）；空=控制面调用被 403、隧道认证失败

# 模型加载（CentOS7 老内核必设，否则 reranker 在线程池里死锁，startup 卡住）
OMP_NUM_THREADS=1
TOKENIZERS_PARALLELISM=false

# ES 热路（可选）
HOT_INDEX_ENABLED=false
ES_URL=http://host.docker.internal:9200

# 性能预算（秒）
SEARCH_TOTAL_BUDGET_S=3.5     # fast-path（不抓正文/不摘要）总耗时目标
CONTENT_FETCH_BUDGET_S=4.0    # NeedContent=true 抓所有页面的墙钟上限；慢出网机房调大到 10
CONTENT_FETCH_MIN_BUDGET_S=1.5 # 正文抓取的预算下限：引擎/rank 挤占响应预算时保底，正文必空会让 deep research 类客户端判低质重搜；代价是极端情况总响应最多超出 RESPONSE_TIME_BUDGET_S 约此值
SUMMARY_BUDGET_S=1.0          # NeedSummary=true 等摘要上限

# 语义模型（可选）
EMBED_MODEL=                  # BAAI/bge-small-zh-v1.5
RERANKER_MODEL=none           # none | BAAI/bge-reranker-base（4c8g 推荐）| BAAI/bge-reranker-v2-m3
RERANKER_TOP_K=20             # 精排候选数
RERANKER_SCORE_FLOOR=0.25     # 精排绝对地板：sigmoid 低于此值丢弃；0 = 只重排不过滤
RERANKER_SCORE_MARGIN=0.15    # 相对断层：丢比当批最高 sigmoid 低超此值的；阈=max(floor, top-margin)
RERANKER_COLLAPSE_CEILING=0.65 # 塌缩闸门：整批最高 sigmoid<此值=召回失败只留 KEEP 条；0=关；换 v2-m3 须重标定
RERANKER_COLLAPSE_KEEP=2      # 塌缩成立时保留的头部条数
CIRCUIT_TTL_S=9              # 引擎熔断窗口（秒）；3 次失败 within TTL 跳过；全员熔断且无其他 tier1 来源时 fail-open 回退全部分片（2026-09-08 修复：全员跳过会 122ms 秒回 0 结果触发客户端重搜风暴）
ENGINE_TIMEOUT_S=5           # 单次引擎调用超时（秒）
PROXY_RETRY_EXTRA_BUDGET_S=5.0 # 代理质量失败（验证码/风控 0 结果）的额外重试预算：总耗时可到 RESPONSE_TIME_BUDGET_S+本值
```

完整配置表见 [`docs/architecture.md#configuration-reference`](docs/architecture.md#configuration-reference)。

## 待实现功能

- **MinHash + LSH** 跨结果抄袭检测 — 未实现（SimHash 同批次去重 ✅）
- **AIGC 检测 Qwen ONNX** — 规则层 ✅，Qwen ONNX 变体未实现
- **Source gate**（ICP / 域名年龄）— 未实现

## Agent skills

### Issue tracker

Issues tracked via GitHub Issues (gh CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Uses canonical label names: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` at repo root + `docs/adr/` for architectural decisions. See `docs/agents/domain.md`.
