# Elasticsearch

同一个 ES 实例，两个完全独立的索引：

| 索引 | 默认名 | 写入方 | 读取方 | 用途 |
|---|---|---|---|---|
| 热点内容 | `hot_news` | crawler 项目 | `app/services/hot_index.py` | 热路命中，跳过 SearXNG |
| 运行日志 | `prism_logs` | `app/services/log_shipper.py` | Kibana / 运维 | Prism 自身日志持久化 |

crawler 项目（`C:\code\AI\crawler`）每 15–60 分钟爬取微博/百度/知乎/头条/RSS，写入 `hot_news`。两个项目只通过 ES 索引交互，无代码依赖。

## 热点索引查询策略

`app/services/hot_index.py` — `_should_skip()` 决策树（按优先级，命中即返回）：

| 条件 | 动作 |
|---|---|
| `intent=news` | **强制查 ES** |
| `intent=knowledge/academic/commercial` | **强制跳过** |
| 知识查询（"什么是X"/"如何Y"/"教程"...） | 跳过 |
| 实时数据（"天气"/"股价"/"比分"/"汇率"...） | 跳过 |
| 无时效意图（正则+向量均未命中） | 跳过 |

通过决策树后，`_NOISE_RE` 剥离时效词/泛指词得到话题核心：
- `"帮我查今日最新数码相关新闻"` → `topic="数码"`
- `"今日热搜"` → `topic=""`（空，无具体话题）

### 路径 A：有话题词

5 路 `should` 并联，`minimum_should_match: 1`：

| 路 | 字段 | boost | 说明 |
|---|---|---|---|
| 1 | `title` | 3.0 | `operator: and` 精确命中 |
| 2 | `snippet` | 1.5 | 模糊命中 |
| 3 | `content` | 0.5 | 正文兜底 |
| 4 | `source` (terms) | 2.0 | 分类→来源映射 |
| 5 | `vectors` (kNN) | 2.5 | 语义检索（`EMBED_MODEL` 启用时） |

`_CATEGORY_TO_SOURCES` 映射：

| 话题词 | source 值 |
|---|---|
| 数码 / 科技 / 硬件 | ifanr, ithome, sspai, 36kr |
| 财经 / 商业 / 创业 | 36kr |
| 游戏 | gcores, bilibili_hot |
| 技术 / 编程 / 开发 / 代码 | juejin |
| 娱乐 / 视频 | bilibili_hot, bilibili |
| 知识 | zhihu_daily |

`vectors` 字段用 `BAAI/bge-small-zh-v1.5`（512 维），**必须与 crawler `EMBED_MODEL` 一致**，向量相容是 kNN 正确工作的前提。

### 路径 B：无话题词（如"今日热搜"）

`function_score` 按 `heat_score` 分段加权：

| heat_score | weight |
|---|---|
| 1–5 | 3.0（最热 top5） |
| 6–20 | 2.0 |
| 21+ | 1.5 |
| 0 | 1.0（RSS，无排名） |

`heat_score=1` = 排名第 1，`0` = RSS 来源。排序：`["_score", crawled_at_ts:desc]`。

## 日志索引 (`app/services/log_shipper.py`)

- `ESLogHandler`：有界内存队列（最多 10,000 条），满时静默丢弃，**不阻塞请求路径**
- 每 5s 或 100 条做一次 `bulk` 写入
- ES 宕机：flush 静默失败，连续 3 次后退避 60s 重建连接；shipper task 崩溃自动重启
- 字段：`@timestamp / level / logger / msg / service / trace_id / tenant_id`（ECS 规范）
- IK 分词插件不存在时自动降级为 standard analyzer
