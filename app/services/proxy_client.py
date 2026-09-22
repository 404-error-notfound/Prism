"""proxy-hub 服务客户端 + 隧道出网地址。

代理池已拆分为独立 proxy-hub 服务（gateway 进程内不再维护代理池）：
  - 活跃信号：节流 POST /activity（proxy-hub 据此做按需补位 + QPS 自适应升档）
  - 熔断恢复加速：POST /refresh 让 proxy-hub 两阶段刷新（先补新 IP 再淘汰旧批，
    刷新期间池子不空；锁门防扎堆；
    未配置 proxy_service_url 时静默跳过，靠 circuit TTL + proxy-hub 巡检自愈）
  - 风控反哺：引擎层识别"连得上但被目标站验证码/秒拒"时 POST /block-signal
    （HTTPS 隧道加密，proxy-hub 数据面看不到风控页，只能由消费方识别后上报；
    proxy-hub 按 forwarder 近期建联映射关联出当时用的上游 IP 即时剔除）
  - 出网：SearXNG / ChinaSo / fetcher 都经 proxy-hub 自建 HTTP 转发代理
    （每连接按目标站防重轮换挑上游 IP）。隧道地址从 PROXY_SERVICE_URL 推导
    （host 相同、端口固定换 8101——proxy-hub 隧道端口契约），账密
    prism:<PROXY_PROJECT_TOKEN> 注入；拓扑唯一事实源是 PROXY_SERVICE_URL。
消费端点一律 Bearer 项目 token 鉴权（PROXY_PROJECT_TOKEN，proxy-hub 侧
POST /admin/projects 注册获得）；token 缺失/错误会被 403 拒绝并告警。
全部 fire-and-forget / 短超时：proxy-hub 不可达不阻塞、不拖慢请求路径。
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_last_activity_mono: float = 0.0
_ACTIVITY_MIN_INTERVAL_S = 10.0  # 节流：进程内最多 10s 上报一次


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(3.0))
    return _client


def _base_urls() -> list[str]:
    """PROXY_SERVICE_URL 支持逗号分隔多个 proxy-hub 副本地址（主备故障转移）。"""
    return [u.strip().rstrip("/") for u in settings.proxy_service_url.split(",") if u.strip()]


def _auth_headers() -> dict[str, str]:
    """消费端点 Bearer 项目 token（proxy-hub 多项目隔离）。"""
    token = settings.proxy_project_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def configured() -> bool:
    return bool(_base_urls())


def _warn_if_403(resp: httpx.Response, path: str) -> None:
    if resp.status_code == 403:
        logger.warning(
            "proxy_client: %s 403 — PROXY_PROJECT_TOKEN 未配置或与 proxy-hub 项目不符",
            path,
        )


async def _post(
    path: str,
    timeout: httpx.Timeout | None = None,
    json_body: dict | None = None,
) -> httpx.Response | None:
    """按顺序尝试各副本，全部失败返回 None。副本短暂不可达属预期（主备切换/
    发布窗口），不告警刷屏。

    json_body：可选 JSON 体（如 {"query_id": ...}），proxy-hub 侧落日志字段，
    与该查询在 prism 侧的日志精确关联。"""
    kwargs: dict = {"headers": _auth_headers(), "timeout": timeout or 3.0}
    if json_body is not None:
        kwargs["json"] = json_body
    for base in _base_urls():
        try:
            resp = await _get_client().post(f"{base}{path}", **kwargs)
            _warn_if_403(resp, path)
            return resp
        except Exception as e:
            logger.debug("proxy_client: POST %s%s failed: %s: %s",
                         base, path, type(e).__name__, e)
    return None


def notify_activity() -> None:
    """节流上报活跃信号（fire-and-forget）。须在运行中的事件循环里调用。

    query_id 从请求上下文（query_id_var ContextVar，orchestrator 入口注入）
    自动读取并随信号上报——10s 节流，一个信号锚定踩中节流边界的那个查询，
    仅作旁证；精确锚点靠 /refresh。"""
    global _last_activity_mono
    if not configured():
        return
    now = time.monotonic()
    if now - _last_activity_mono < _ACTIVITY_MIN_INTERVAL_S:
        return
    _last_activity_mono = now
    task = asyncio.create_task(_post("/activity", json_body=_signal_body()))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


_BLOCK_SIGNAL_MIN_INTERVAL_S = 5.0  # 节流：同一目标站最多 5s 上报一次
_last_block_signal: dict[str, float] = {}


def report_block(target: str) -> None:
    """上报目标站风控信号（fire-and-forget，按 target 进程内节流）。

    proxy-hub 侧按 forwarder 近期建联映射（project × target，近 30s）关联出
    当时用的上游 IP 即时剔除出池，避免同一批被风控 IP 在后续查询里反复被
    抽中（2026-09-08 生产：四站秒拒的 IP 在两轮重试中反复命中）。
    """
    if not configured():
        return
    now = time.monotonic()
    if now - _last_block_signal.get(target, 0.0) < _BLOCK_SIGNAL_MIN_INTERVAL_S:
        return
    _last_block_signal[target] = now
    body = _signal_body() or {}
    body["target"] = target
    task = asyncio.create_task(_post("/block-signal", json_body=body))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def _signal_body() -> dict | None:
    """当前请求上下文的 query_id/query → 信号 body（不在请求链路 → 无 body）。

    读 ContextVar 而非显式传参：与日志埋点同一机制（ensure_warm 读
    proxy_batch_var 的先例），调用方零改动，唤热等非 orchestrator 路径
    只要在请求上下文内也自动带上。query（查询词）是人可读锚点：
    proxyhub_logs 直接按查询词模糊检索，不必先翻 prism 日志复制 uuid。"""
    from app.core.logging import query_id_var, query_text_var

    qd = query_id_var.get("")
    text = query_text_var.get("").strip()
    if not qd:
        return None
    body: dict = {"query_id": qd}
    if text:
        body["query"] = text[:200]  # 截断防超大 body；200 字远够人检索定位
    return body


async def request_force_refresh(timeout_s: float = 3.0) -> bool:
    """请 proxy-hub 立即抽新 IP。未配置服务地址返回 False。

    timeout_s：等刷新落地的时长上限。proxy-hub 是两阶段刷新（先补新 IP 再淘汰
    旧批次），补位要 抽取+探测 ≈3-5s——想"等到新 IP 再用"的场景（如验证码
    失败后的重试）需给到 8s 左右；异步兜底触发用默认 3s 即可。
    wait_s（timeout_s - 1，随 body 下发）：维护锁被占/补位撞冷却时服务端的
    最长等待——持锁方正在两阶段刷新，等它落地胜过消费方自己再撞同一坏窗口
    （2026-09-04 NBA 零结果根因：refresh 被锁/冷却秒拒，100ms 后重试又全灭）。
    HTTP 超时仍由 timeout_s 卡总时长，服务端等待至多 timeout_s - 1。
    超时不变式：wait 预算用完后服务端还会跑 fetch+probe（≤5s+），HTTP 超时
    （ReadTimeout → 返回 False）可能落在探测中途——但流式入池意味着超时发生
    时新 IP 多半已落地，消费方立即重试引擎恰好命中，无需额外等待一轮。
    信号自动携带请求上下文的 query_id（见 _signal_body）——proxyhub_logs
    按 query_id 检索即得"哪个查询触发了换批"，跨链路排查的确定性锚点。
    """
    if not configured():
        return False
    body = _signal_body() or {}
    body["wait_s"] = max(0.0, timeout_s - 1.0)
    resp = await _post("/refresh", timeout=httpx.Timeout(timeout_s), json_body=body)
    if resp is None:
        return False
    try:
        return bool(resp.status_code == 200 and resp.json().get("ok"))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 请求路径唤热（替代旧 ensure_fresh）：空闲后首查不把死池打向引擎
# ---------------------------------------------------------------------------

_status_cache: tuple[float, bool] = (0.0, True)  # (cached_at_mono, available)
_STATUS_CACHE_TTL_S = 5.0


async def ensure_warm(max_wait_s: float = 8.0) -> bool:
    """proxy-hub 池不新鲜时触发刷新并短等。返回池是否可用（不可用也不阻塞查询）。

    空闲期 proxy-hub 按需停刷（省钱），池会自然过期； idle 后第一个查询若直接打
    引擎会全员 proxy error（2026-08-25 生产实测"每轮首查必 0"）。热路径成本：
    5s 缓存内至多一次 GET /status（内网毫秒级）。proxy-hub 不可达时放行
    （隧道后端可能还有活 IP，查询照常，由熔断收敛）。
    """
    global _status_cache
    bases = _base_urls()
    if not bases:
        return True

    cached_at, cached_ok = _status_cache
    now = time.monotonic()
    if now - cached_at < _STATUS_CACHE_TTL_S and cached_ok:
        return True
    available = None
    for base in bases:  # 主备故障转移：任一副本可用即可
        try:
            resp = await _get_client().get(f"{base}/status", headers=_auth_headers())
            _warn_if_403(resp, "/status")
            if resp.status_code == 200:
                available = bool(resp.json().get("available"))
                # 记下当前代理批次号，让搜索日志带上 batch_id，
                # prism_logs(query_id) ↔ proxyhub_logs(batch_id) 可互相跳转
                # （滚动补位后 proxy-hub /status 暂无 batch_id，读取优雅降级为 ""）
                from app.core.logging import proxy_batch_var
                proxy_batch_var.set(str(resp.json().get("batch_id") or ""))
                break
        except Exception as e:
            logger.debug("proxy_client: GET %s/status failed: %s: %s",
                         base, type(e).__name__, e)
    if available is None:
        available = True  # proxy-hub 全部不可达不阻塞查询
    _status_cache = (now, available)
    if available:
        return True

    # 池冷：请 proxy-hub 两阶段刷新（先补新 IP 再淘汰旧批，持锁方补完才返回），
    # 补位要 抽取+探测 ≈3-8s，给独立的宽超时
    logger.info("proxy_client: pool cold, requesting warm refresh")
    ok = False
    for base in bases:
        try:
            # 带 wait_s（对齐 request_force_refresh）：撞维护锁时服务端等持锁方
            # 落地再应答，而不是秒返——旧行为撞锁秒返且 ok 口径只是"池非空"，
            # 池里全是禁区将死 IP 时被误判为已热，放行查询全员撞风控
            body = _signal_body() or {}
            body["wait_s"] = max(0.0, max_wait_s - 1.0)
            resp = await _get_client().post(
                f"{base}/refresh", headers=_auth_headers(),
                timeout=httpx.Timeout(max_wait_s + 2.0),
                json=body,
            )
            _warn_if_403(resp, "/refresh")
            ok = bool(resp.status_code == 200 and resp.json().get("ok"))
            if ok:
                break
        except Exception as e:
            logger.warning("proxy_client: warm refresh via %s failed: %s: %s",
                           base, type(e).__name__, e)
    _status_cache = (time.monotonic(), ok)
    return ok


# ---------------------------------------------------------------------------
# proxy-hub 隧道出网地址
# ---------------------------------------------------------------------------

_drift_warned: bool = False  # 废弃/漂移告警只打一次（本函数在抓取热路径上）


def _tunnel_urls() -> list[str]:
    """从 PROXY_SERVICE_URL 推导隧道地址列表（拓扑唯一事实源）。

    proxy-hub 端口是固定契约：控制面任意端口（惯例 8100）、数据面隧道固定
    监听 8101（CLAUDE.md §部署架构）。推导 = 剥 scheme/账密/端口 → host 加
    :8101 → 注入账密 prism:<PROXY_PROJECT_TOKEN>。token 为空时账密为空，
    会被 proxy-hub 拒绝——由 SearXNG 侧 fail-fast 与本模块 403 告警兜住。"""
    token = settings.proxy_project_token.strip()
    urls = []
    for base in _base_urls():
        hostport = base.split("://", 1)[-1].split("@")[-1].split("/")[0]
        host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
        urls.append(f"http://prism:{token}@{host}:8101")
    return urls


def get_tunnel_proxy_url() -> str | None:
    """返回 proxy-hub 隧道代理 URL（HTTP 代理形式，带项目账密）。

    从 PROXY_SERVICE_URL 推导（第一条；gateway 侧暂不做多隧道故障转移，
    SearXNG 侧由 settings 渲染消费全列表）。未配置时返回 None（直连）。

    兼容回退：旧字段 PROXY_TUNNEL_URL 仅在推导为空时生效（deprecated，
    待 .env.prod 迁移后下线）；与推导结果漂移时告警并以推导为准。"""
    global _drift_warned
    tunnels = _tunnel_urls()
    legacy = settings.proxy_tunnel_url.strip()
    if tunnels:
        if legacy and legacy not in tunnels and not _drift_warned:
            _drift_warned = True
            logger.warning(
                "proxy_client: PROXY_TUNNEL_URL(%s) 与 PROXY_SERVICE_URL 推导结果漂移，"
                "已废弃、以推导为准——请从 .env 删除该变量", legacy,
            )
        return tunnels[0]
    if legacy:
        if not _drift_warned:
            _drift_warned = True
            logger.warning(
                "proxy_client: PROXY_TUNNEL_URL 已废弃（PROXY_SERVICE_URL 未配置，"
                "临时回退旧字段）——请改配 PROXY_SERVICE_URL"
            )
        return legacy
    return None


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
