"""ChinaSo（中国搜索）适配器 — 国家队权威源（聚合人民网/新华/央视等）。

接口（2026-06-17 逆向自 newssearch SPA 的 app.js，非官方文档）：
  GET https://www.chinaso.com/v5/general/v1/search?q=<kw>&pn=<page>&ps=<size>
  响应 JSON：{"status":int,"msg":str,"data":{"data":[item...],"total":int,...}}
  item 字段：title / url / content(摘要) / source|sourceName|siteName(来源) / stime|time(时间)

风控是**会话 cookie**（不是纯 IP）：直连/无 cookie 命中 `{"status":2,"msg":"ip control"}`；
先 GET allResults 页面拿到 `uid` cookie，再带同一会话调接口即可拿到 status=0。出网走
proxy-hub 隧道代理（与 SearXNG 同一出口路径），地址从 PROXY_SERVICE_URL 推导
（host 相同、端口换 8101，proxy_client.get_tunnel_proxy_url）。
uid cookie 模块级缓存复用，稳态每查询 1 个请求；命中风控才重新预热（IP 轮换后失效会自愈）。
默认禁用 CHINASO_ENABLED=false。
"""
from __future__ import annotations

import html
import logging
import re

import httpx

from app.core.config import BROWSER_USER_AGENT
from app.engines.base import BaseEngine, RawResult
from app.services import proxy_client

logger = logging.getLogger(__name__)

_API = "https://www.chinaso.com/v5/general/v1/search"
_PAGE = "https://www.chinaso.com/newssearch/all/allResults"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)

_uid_cookie: str | None = None       # 跨请求复用的 uid cookie（命中风控才刷新）


_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """去掉接口返回的 <em> 高亮标签 + 反转义 HTML 实体。"""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _first(item: dict, *keys: str) -> str:
    for k in keys:
        v = item.get(k)
        if v:
            return _clean(str(v))
    return ""


def _ok(payload) -> bool:
    return isinstance(payload, dict) and payload.get("status") in (0, 1)


class ChinaSoEngine(BaseEngine):
    name = "chinaso"
    proxy_dependent = True  # 按 IP 风控，必须经代理池出网

    async def search(self, query: str, num: int = 20, **kwargs) -> list[RawResult]:
        global _uid_cookie
        params = {"q": query, "pn": 1, "ps": max(1, num)}
        headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Referer": _PAGE,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        proxy = proxy_client.get_tunnel_proxy_url()  # 复用 SearXNG 同款出网路径
        cookies = {"uid": _uid_cookie} if _uid_cookie else {}
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, proxy=proxy, follow_redirects=True, cookies=cookies
            ) as client:
                payload = (await client.get(_API, params=params, headers=headers)).json()
                if not _ok(payload):
                    # 风控/无 cookie：访问页面种 uid，缓存后重试一次
                    await client.get(_PAGE, params={"q": query},
                                     headers={"User-Agent": BROWSER_USER_AGENT})
                    if got := client.cookies.get("uid"):
                        _uid_cookie = got
                    payload = (await client.get(_API, params=params, headers=headers)).json()
        except Exception as exc:
            logger.warning("chinaso error proxy=%s: %s", bool(proxy), exc)
            return []

        if not _ok(payload):
            logger.warning("chinaso non-ok status=%s msg=%s",
                           payload.get("status"), payload.get("msg"))
            return []

        items = (payload.get("data") or {}).get("data") or []
        results: list[RawResult] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            url = _first(it, "url", "cacheUrl")
            title = _first(it, "title")
            if not url or not title:
                continue
            results.append(
                RawResult(
                    title=title,
                    url=url,
                    snippet=_first(it, "content", "summary", "description"),
                    published_at=_first(it, "stime", "time", "pubTime") or None,
                    engine="chinaso",
                )
            )
        return results[:num]
