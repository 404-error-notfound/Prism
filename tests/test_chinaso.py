"""ChinaSo 适配器解析单测（不打真实网络）。

fixture 按 2026-06-17 逆向自 newssearch app.js 的响应 schema 构造：
  {"status":0,"data":{"data":[{title,url,content,source,stime}],"total":N}}
真实联调（机房代理 IP 下拿 status=0）见 docs/spikes/chinaso-toutiao-engines.md。
"""
from __future__ import annotations

import httpx
import pytest

from app.engines.chinaso import ChinaSoEngine

_OK_PAYLOAD = {
    "status": 0,
    "msg": "ok",
    "data": {
        "total": 2,
        "data": [
            {
                "title": "央行宣布降准0.5个百分点",
                "url": "https://www.gov.cn/lianbo/bumen/202606/x.htm",
                "content": "中国人民银行决定下调存款准备金率0.5个百分点，释放长期资金……",
                "source": "中国政府网",
                "sourceName": "中国政府网",
                "stime": "2026-06-15 10:08:38",
            },
            {
                "title": "解读：降准降息对市场的影响",
                "url": "https://www.xinhuanet.com/fortune/x.htm",
                "summary": "分析人士认为……",  # 退化字段：content 缺失时取 summary
                "siteName": "新华网",
                "time": "2026-06-15",
            },
        ],
    },
}


def _client_returning(payload, status_code=200):
    """构造一个 httpx mock transport，让 ChinaSoEngine 内部 client 命中它。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_parse_ok(monkeypatch):
    engine = ChinaSoEngine()
    # 替换 httpx.AsyncClient 使其使用 MockTransport，并屏蔽代理
    transport = _client_returning(_OK_PAYLOAD)
    orig = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr("app.engines.chinaso.proxy_client.get_tunnel_proxy_url", lambda: None)

    results = await engine.search("央行降息", num=10)
    assert len(results) == 2
    assert results[0].title == "央行宣布降准0.5个百分点"
    assert results[0].url.startswith("https://www.gov.cn/")
    assert "存款准备金率" in results[0].snippet
    assert results[0].published_at == "2026-06-15 10:08:38"
    assert results[0].engine == "chinaso"
    # 第二条：content 缺失退化到 summary，time 退化到 published_at
    assert results[1].snippet == "分析人士认为……"
    assert results[1].published_at == "2026-06-15"


@pytest.mark.asyncio
async def test_ip_control_returns_empty(monkeypatch):
    """机房 IP 风控 status=2 → 返回空，不抛。"""
    engine = ChinaSoEngine()
    transport = _client_returning({"status": 2, "msg": "ip control", "data": {}})
    orig = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr("app.engines.chinaso.proxy_client.get_tunnel_proxy_url", lambda: None)

    results = await engine.search("央行降息", num=10)
    assert results == []
