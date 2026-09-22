from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# 出站抓取统一使用的真实 Chrome UA：非浏览器 UA（如 PrismBot）易被站点风控 403。
# 与 SearXNG 实例 settings.yml 的出站 UA 保持一致（那 3 份是容器配置，无法引用此常量）。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 基础
    app_env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    service_name: str = "prism"

    # 传输
    mcp_transport: Literal["stdio", "http"] = "http"
    host: str = "0.0.0.0"
    port: int = 8000
    sse_heartbeat_sec: int = 25

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""
    redis_max_conn: int = 200
    redis_cluster_mode: bool = False
    redis_key_prefix: str = ""

    # 引擎
    # searxng_shards：一源一实例分片拓扑（推荐），格式 "引擎名=实例URL" 逗号分隔，如
    # "baidu=http://searxng-baidu:8080,bing=http://searxng-bing:8080,sogou wechat=http://searxng-wechat:8080"。
    # 非空时每片注册为独立逻辑引擎（独立实例/熔断/ProxyQualityError），慢源卡死只损失该源；
    # 容器无需差异化配置——切片靠请求参数 engines=，各实例同镜像同 settings 模板。
    # 为空时回退 searxng_urls 全引擎多实例模式（page N 路由到第 N 个实例）。
    searxng_shards: str = ""
    searxng_url: str = "http://searxng-1:8080"          # 向后兼容，searxng_urls 为空时使用
    searxng_urls: str = "http://searxng-1:8080,http://searxng-2:8080,http://searxng-3:8080"
    searxng_engines: str = "baidu,bing"
    # 发给 SearXNG 的 X-Forwarded-For/X-Real-IP 值。
    # 必须是非 loopback 地址，否则 SearXNG botdetection 视为"未设置"并返回 403。
    searxng_forwarded_for: str = "10.0.0.1"
    ddgs_enabled: bool = False  # DDG inaccessible in mainland China
    # ChinaSo（中国搜索，权威源）。接口按 IP 风控，必须经代理池出网（见 app/engines/chinaso.py）。
    # 默认禁用：需先验证机房代理 IP 下能拿到 status=0 的真实结果再开。
    chinaso_enabled: bool = False
    # 框架词软剥离：发引擎前剥掉"适合/推荐/的"等低信息量框架词，避免多约束长尾
    # 查询坍缩成单高频词招来字典/百科页（见 rewriter.strip_framing_markers）。
    # 常开；出问题置 false 一键回滚（原串始终保留给排序，关掉只影响召回宽度）。
    enable_framing_strip: bool = True

    serper_api_key: str = ""
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    brave_api_key: str = ""
    tavily_api_key: str = ""
    volcengine_api_key: str = ""  # 火山引擎联网搜索 APIKey

    # 热点索引（Elasticsearch）
    es_url: str = "http://elasticsearch:9200"
    es_user: str = ""
    es_password: str = ""
    es_index: str = "hot_news"
    hot_index_enabled: bool = False   # 需 crawler 先写入数据后再开启
    hot_index_min_hits: int = 3       # 命中数低于此值时降级至 SearXNG

    # Tier 开关
    tier2_default: Literal["none", "dataforseo", "serper", "brave", "volcengine"] = "none"
    tier3_playwright_enabled: bool = False
    tier3_proxy_pool: Literal["selfhosted", "residential", "datacenter"] = "selfhosted"

    # 代理池由独立 proxy-hub 服务托管（gateway 进程内不维护代理池、不跑代理后台任务）。
    # proxy-hub 服务地址（如 http://proxy-hub:8100，逗号分隔多副本主备）：
    # **proxy-hub 拓扑唯一事实源**——gateway 向它节流上报活跃信号（POST /activity）、
    # 引擎熔断时 POST /refresh 加速抽新；数据面隧道地址也由此推导（host 相同、
    # 端口固定换 8101，见 proxy_client._tunnel_urls），SearXNG settings 同样由
    # entrypoint 从本变量渲染。加/换 proxy-hub 机器只改这一个变量。
    # 留空 = 整条 proxy-hub 链路停用（上报/唤热/强制刷新静默跳过 + 无隧道直连出网）
    proxy_service_url: str = ""
    # proxy-hub 项目 token（proxy-hub 侧 POST /admin/projects 注册 "prism" 项目获得）：
    # /activity /refresh /status 消费端点一律 Bearer 鉴权；隧道账密也由它注入
    # （prism:<token>@host:8101），token 唯一事实源即本字段。空 = 调用被 proxy-hub 403 拒绝
    proxy_project_token: str = ""
    # DEPRECATED（兼容回退，待 .env.prod 迁移后下线）：旧版隧道地址（带账密）。
    # 仅在 proxy_service_url 为空时生效；隧道拓扑请以 proxy_service_url 为准。
    proxy_tunnel_url: str = ""
    # 引擎熔断 TTL（秒）：单引擎 circuit_ttl_s 内失败 ≥3 次 → 后续请求跳过该引擎。
    # 走代理的引擎（searxng/chinaso）跨阈值那一刻异步请 proxy-hub 立即抽新 IP
    # （见 orchestrator._record_failure + proxy_client.request_force_refresh），
    # proxy-hub 侧有锁 + 巡检自愈兜底，此值无需设大
    circuit_ttl_s: int = 9
    # Per-engine outer timeout (seconds). Keep this below the end-to-end target
    # so ranking/content/summary still have time to finish.
    engine_timeout_s: float = 5.0
    response_time_budget_s: float = 10.0
    # 代理质量失败（验证码/IP 被风控，0 结果）时的额外重试预算：等 proxy-hub
    # 补新 IP + 换新 IP 重试都从这里出。晚几秒响应好过返回 0 结果
    proxy_retry_extra_budget_s: float = 5.0
    # 召回垄断重试：raw>0 但被单一低信任（非 T1/T2）域名垄断 ≥recall_monopoly_share
    # 时，视为分片大面积被风控、只剩独苗源（生产痼疾：10/10 全是 weixin.sogou.com
    # 陈货霸榜）——按代理质量问题处理：刷新 IP + 延长预算重试，已有结果保留并入。
    recall_monopoly_retry: bool = True
    recall_monopoly_share: float = 0.7
    # 低信任源时效硬过滤：未显式传 TimeRange 时，发布时间早于此天数的非 T1/T2 结果
    # 直接丢弃（推荐/对比类查询被多年前公众号陈货霸榜的治理）；滤空则 fail-open 放过。
    recall_stale_max_age_days: int = 730
    # Max time spent in semantic ranking before cheap lexical fallback.
    ranking_budget_s: float = 2.5

    # 代理预算（旧配置保留兼容）
    proxy_daily_budget_usd: float = 10.0

    # 治理
    governance_default_path: Literal["fast", "standard", "deep"] = "standard"
    governance_aigc_min_model: str = "rules"
    reranker_model: str = "none"
    reranker_top_k: int = 10
    # 精排分数闸门：sigmoid 分数低于此值的结果丢弃（0 = 关闭绝对地板）。
    # 存活 <3 条时自动放弃闸门，避免阈值失准误杀。
    reranker_score_floor: float = 0.25
    # 精排相对断层闸门：丢掉比当批最高 sigmoid 低超过此值的结果（0 = 关闭相对闸门）。
    # bge-reranker-base 的 sigmoid 输出被压缩在 ~0.5-0.73 窄区间，跑题页常 >0.5，固定
    # 绝对地板（0.25）咬不住；相对断层随每条 query 分布自适应：实测好结果 0.72、跑题
    # 塌在 0.54，0.15 的 margin 正好切开。实际阈 = max(floor, top_rr - margin)。
    reranker_score_margin: float = 0.15
    # 精排塌缩闸门：整批最高 sigmoid 低于此值 → 判定召回失败/全跑题，只留最高
    # reranker_collapse_keep 条，不把一池跑题页全交付。bge-reranker-base 实测：好结果
    # 头部 >0.7，全跑题塌在 ~0.5-0.61（floor/margin 在此区间咬不住，dropped=0）。0 = 关闭。
    reranker_collapse_ceiling: float = 0.65
    reranker_collapse_keep: int = 2

    # 语义向量模型（bi-encoder）
    # 空字符串 = 禁用语义因子，回退到 6 因子排序
    # 推荐：BAAI/bge-small-zh-v1.5（~90MB，CPU 推理 ~65-110ms/批）
    embed_model: str = ""
    # torch.set_num_threads()：每次推理使用的 PyTorch BLAS 线程数
    # asyncio.Semaphore：同时运行的推理任务数
    # 建议：两者之积 = 物理核数（4核推荐 2×2）
    embed_num_threads: int = 2
    embed_max_concurrent: int = 2

    # 鉴权
    admin_bootstrap_key: str = ""
    default_quota_per_day: int = 1000
    default_rate_limit_qps: int = 5

    # 安全
    hmac_salt: str = ""

    # 合规
    preference_retention_days: int = 90
    enforce_domestic_queries: bool = False

    # 性能预算
    # 整个 /api/search 请求的目标总耗时（秒）；fast-path（无 NeedContent/NeedSummary）
    # 保持在此预算内。需要抓页面或 LLM 摘要时会用下面两个独立预算作为下限。
    search_total_budget_s: float = 3.5
    # NeedContent=true 时抓取所有页面的最长时间（秒）。真实网页常需 2-5s/页，
    # 所以这里独立配置，不受 search_total_budget_s 约束。
    content_fetch_budget_s: float = 4.0
    # 正文抓取的预算下限（秒）：引擎/rank 耗掉大部分响应预算后，remaining 可能只剩
    # 零点几秒，正文必空（无正文的结果对 deep research 类调用方等于不可用）。floor
    # 保证 require_detail 的请求至少有这么长时间抓正文；代价是极端情况总响应会超出
    # response_time_budget_s 最多约这个值。
    content_fetch_min_budget_s: float = 1.5
    # NeedContent URL fetch concurrency. 0 = one task per result; >0 = upper cap.
    # Main request still returns at content_fetch_budget_s; pending tasks continue to fill cache.
    content_fetch_concurrency: int = 0
    # 正文抓取出网顺序："proxy_first"（默认）=代理隧道优先、直连兜底；
    # "direct_first"=直连优先、隧道兜底。代理池是运营商 IP，内容站风控对它比
    # 服务器机房 IP 友好得多；家庭宽带等本身即运营商出口的环境可改 direct_first。
    # 未配 PROXY_SERVICE_URL（推导不出隧道地址）时两种模式都退化为仅直连。
    content_fetch_egress: str = "proxy_first"
    # trafilatura/readability parse concurrency. 0 = max(2, CPU core count).
    content_parse_concurrency: int = 0
    # NeedSummary=true 时等待摘要完成的最长时间（秒）。当前实现是 BM25 抽取式，
    # 典型 <100ms；留这个上限纯粹作为安全网，不影响实际耗时。
    summary_budget_s: float = 1.0

    # Crawl4AI（JS 渲染页抓取）吞吐旋钮
    # 实测：抓取成功率受源站可抓性限制（大量 baidu/sogou 聚合页登录墙/反爬），
    # 不受这两个旋钮影响——调大只增延迟、压垮弱机器，不涨命中。保持保守默认。
    # 单页渲染+抽取超时（秒）。
    crawl4ai_timeout_s: float = 5.0
    # 并发浏览器标签数。越大越吃内存（每标签约 50-100MB），弱机器别调高。
    crawl4ai_concurrency: int = 0

    # 日志文件
    log_file_enabled: bool = True       # 是否写文件日志（关掉则只输出到 stdout）
    log_dir: str = "logs"               # 日志目录，相对于工作目录
    log_max_bytes: int = 20 * 1024 * 1024   # 单文件最大 20 MB
    log_backup_count: int = 10          # 保留最近 10 个滚动文件

    # 日志写入 ES（独立于 hot_news 热点索引）
    log_es_enabled: bool = False            # 开启后日志异步写入 ES
    log_es_index: str = "prism_logs"        # 日志专用索引，与 es_index(hot_news) 隔离
    log_es_min_level: str = "INFO"          # 写入 ES 的最低日志级别
    log_es_batch_size: int = 100            # 每批最多写入条数
    log_es_flush_interval_s: float = 5.0   # 最长刷新间隔（秒）

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def searxng_engines_list(self) -> list[str]:
        return [e.strip() for e in self.searxng_engines.split(",") if e.strip()]

    @property
    def searxng_shard_map(self) -> dict[str, str]:
        """分片拓扑 {引擎名: 实例URL}；空 dict = 非分片模式。引擎名可含空格（如 sogou wechat），按第一个 = 切分。"""
        out: dict[str, str] = {}
        for pair in self.searxng_shards.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            engine, _, url = pair.partition("=")
            engine, url = engine.strip(), url.strip().rstrip("/")
            if engine and url:
                out[engine] = url
        return out


settings = Settings()
