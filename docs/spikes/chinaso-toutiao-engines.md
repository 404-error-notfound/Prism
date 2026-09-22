# Spike：接入 ChinaSo（中国搜索）+ 头条搜索 作为免费召回源

状态：**待执行**（2026-06-17 立项）
目标：给冷路扩召回补充权威/时效免费源，缓解 baidu/sogou 那批通用引擎返回百科/字典/农场页的问题。

## 背景

- 现网 SearXNG 用**官方镜像** `searxng/searxng:latest`，只挂载 `settings.yml`（无自定义 Dockerfile、无 engines 目录挂载）。
- ChinaSo、头条搜索**都不是 SearXNG 内置引擎**（内置中文源仅 baidu/sogou/360search/quark/bilibili/sogou wechat）。
- `sogou wechat` 已在本轮直接启用（内置、keyless），无需 spike。

## 关键未决问题（spike 要回答）

### Q1. 集成路径：SearXNG 自定义引擎 vs 原生 Prism 适配器

| | SearXNG 自定义引擎（用户倾向） | 原生 Prism 适配器 `app/engines/chinaso.py` |
|---|---|---|
| 改动 | 必须改用**自定义镜像**（`FROM searxng/searxng` + `COPY chinaso.py searx/engines/` + 注册），三实例 compose 同步 | 仿 `serper.py`/`dataforseo.py` 加一个适配器，零镜像改动 |
| 代理出网 | 复用 SearXNG 的 forwarder 代理池（统一） | 需自己走 `proxy_pool` / httpx，单独接代理 |
| 测试 | 需起容器验证 | 可用 `tests/` + httpx mock 直测（符合现有引擎测试模式）|
| 多实例分页 | 自动复用 | 不涉及（直连） |

**建议**：先各写一个**原生 Prism 适配器**做可行性验证（最快、可单测、不动镜像）；确认源稳定后，若要统一走代理池再移植成 SearXNG 自定义引擎。把"用户倾向 SearXNG 侧"作为长期形态，不阻塞先验证。

### Q2. 接口可解析性（逐源验证）

**ChinaSo — 已逆向，适配器已落地（2026-06-17）**
- SPA 路由 `https://www.chinaso.com/newssearch/all/allResults?q=` 背后的真实数据接口（从 `js/app.<hash>.js` 逆出）：
  `GET https://www.chinaso.com/v5/general/v1/search?q=<kw>&pn=<page>&ps=<size>`
- 响应 **JSON**：`{"status":int,"msg":str,"data":{"data":[item...],"total":int,...}}`；item 字段 `title/url/content(摘要)/source|sourceName|siteName/stime|time`。
- **已实现** `app/engines/chinaso.py` + `tests/test_chinaso.py`（fixture 按此 schema），编排已接 Tier1（`CHINASO_ENABLED` 默认 false）。
- **唯一剩余 = 机房代理 IP 下的活体验证**：直连命中 `{"status":2,"msg":"ip control"}`（按 IP 风控，证实 Q3）。适配器已优先走 `proxy_pool.get_proxy_url()`。需在有代理池的环境跑一次确认拿到 `status=0` 的真实结果、字段名与逆向一致，再把 `CHINASO_ENABLED=true`。
- ChinaSo 聚合人民网/新华/央视等权威源 → 命中即高 trust，对权威新闻类 case 增益最大。

**头条搜索**
- 入口：`https://so.toutiao.com/search?keyword=<kw>&pd=information`（`pd=information` 为资讯垂类）。
- ⚠️ 风险高：ByteDance 反爬严，通常需要 `s_v_web_id` / `_signature` 等 cookie/签名，机房 IP + 无签名极可能 403/验证码。**可行性存疑，spike 重点验证**。

### Q3. 机房/代理 IP 是否被封（与现有零结果同源风险）

- 这是**决定性**检查：用**现网同款短效代理 IP** 发一次真实查询，看是否 200 + 有结果，而非用本机直连测（本机能通不代表机房能通）。
- 验证脚本：复用 `proxy_pool` 抽一个在用 IP，curl/httpx 带该代理打候选 endpoint，观察 status / body。

## 验证步骤（按序）

1. **手测 endpoint**：浏览器开 DevTools，对 ChinaSo / 头条搜索各搜一个词，记录真正返回结果的 URL、参数、响应格式（JSON 路径或 HTML 选择器）。
2. **机房 IP 复测**：用现网代理 IP 重放步骤 1 的请求，确认不被风控（Q3）。
3. **写最小适配器**（原生 Prism，见 Q1 建议）：实现 `search(query, num, recency_days)` → `list[RawResult]`，填入步骤 1 拿到的解析逻辑。
4. **单测**：`tests/test_chinaso.py` 用真实响应样本（截一段存 fixture）断言解析正确，不打真实网络。
5. **接入编排**：作为 Tier1 之一加入 `orchestrator` 第一轮并行批次（饿汉式扩召回，延迟中性，受 8s/引擎天花板约束）。
6. **跑评测**：`scripts/replay_testset.py` 对比接入前后 `_FARM_SPAM` 命中、零结果率、`MUST_HIT_TOKENS` 通过率。

## 验收标准

- 机房代理 IP 下稳定 200、单查询 ≥3 条结果、p50 延迟 < 3s。
- 解析准确率（title/url 抽取）抽检 ≥ 95%。
- 评测集：接入后零结果率下降，且未引入新的农场/字典域（FORBIDDEN 断言不回归）。

## 失败/降级

- 任一源机房 IP 被封或无可解析接口 → **该源跳过**，不硬刚（与 CLAUDE.md"别硬刚反爬"一致）。
- 头条搜索若签名墙过不去 → 放弃头条，保留 ChinaSo + sogou wechat。

## 关联

- 引擎扩充背景见本轮搜索质量优化（spam_gate + 扩召回）。
- 代理三态健康检查见 `docs/architecture.md` / `proxy_pool`。
