"""fetcher 内容过滤纯函数单测（不依赖网络/浏览器）。

样本取自真实返回：sogou 公众号 weixin.sogou.com/link 抓取后正文变成反爬验证页。
"""
from __future__ import annotations

from app.services.fetcher import _looks_blocked


def test_blocked_sogou_captcha_detected():
    """sogou 反爬验证页正文（曾作为唯一结果交付）→ 判为拦截页。"""
    captcha = (
        "IP：223.67.220.14访问时间：2026.06.23 15:12:38VerifyCode：39783a2970af"
        "From：weixin.sogou.com\n\n此验证码用于确认这些请求是您的正常行为"
        "而不是自动程序发出的，需要您协助验证。\n\n验证码：\n\n提交"
    )
    assert _looks_blocked(captcha) is True


def test_real_content_not_blocked():
    """正常公众号正文（含真实内容）→ 不误判。"""
    real = (
        "2026款特斯拉Model Y Juniper版最值得聊的，不是它又换了多醒目的前脸，"
        "而是它终于更像一台家用车了。续航、空间、智能座舱全面升级……"
    )
    assert _looks_blocked(real) is False


# ---------------------------------------------------------------------------
# 抓取出网链路与已知不可抓域名
# ---------------------------------------------------------------------------

from app.services import fetcher  # noqa: E402
from app.services.fetcher import (  # noqa: E402
    _fetch_clients,
    _get_direct_client,
    _is_skip_fetch_host,
    fetch_page_content,
)


def test_skip_fetch_host_sogou_weixin():
    """weixin.sogou.com 跳转壳（必进验证码墙）判为不可抓；普通域名不误判。"""
    assert _is_skip_fetch_host("https://weixin.sogou.com/link?url=x&token=y") is True
    assert _is_skip_fetch_host("https://www.toutiao.com/w/1858445490685952/") is False
    assert _is_skip_fetch_host("https://mbd.baidu.com/newspage/data/dtlandingsuper?nid=x") is False


async def test_fetch_page_content_skips_known_unfetchable():
    """不可抓域名直接返回失败，不触网、不占 content budget。"""
    content, robots_allowed = await fetch_page_content(
        "https://weixin.sogou.com/link?url=x&token=y"
    )
    assert content is None
    assert robots_allowed is True


async def test_fetch_clients_proxy_first_by_default(monkeypatch):
    """默认代理隧道优先、直连兜底（代理池是运营商 IP，对内容站风控更友好）。"""
    monkeypatch.setattr(fetcher.settings, "content_fetch_egress", "proxy_first")
    monkeypatch.setattr(fetcher.settings, "proxy_service_url", "")
    monkeypatch.setattr(
        fetcher.settings, "proxy_tunnel_url", "http://prism:t@127.0.0.1:8101"
    )
    clients = _fetch_clients()
    assert [tag for tag, _ in clients] == ["proxy", "direct"]


async def test_fetch_clients_direct_only_when_no_tunnel(monkeypatch):
    """未配隧道（PROXY_SERVICE_URL 与旧字段都空，推导不出隧道）时退化为仅直连。"""
    monkeypatch.setattr(fetcher.settings, "proxy_service_url", "")
    monkeypatch.setattr(fetcher.settings, "proxy_tunnel_url", "")
    for egress in ("proxy_first", "direct_first"):
        monkeypatch.setattr(fetcher.settings, "content_fetch_egress", egress)
        clients = _fetch_clients()
        assert [tag for tag, _ in clients] == ["direct"]
        assert clients[0][1] is _get_direct_client()


async def test_fetch_clients_direct_first(monkeypatch):
    """direct_first：直连优先、隧道兜底（家庭宽带等运营商出口环境）。"""
    monkeypatch.setattr(fetcher.settings, "content_fetch_egress", "direct_first")
    monkeypatch.setattr(fetcher.settings, "proxy_service_url", "")
    monkeypatch.setattr(
        fetcher.settings, "proxy_tunnel_url", "http://prism:t@127.0.0.1:8101"
    )
    clients = _fetch_clients()
    assert [tag for tag, _ in clients] == ["direct", "proxy"]
    assert clients[0][1] is _get_direct_client()


# ---------------------------------------------------------------------------
# mbd.baidu.com dtlandingsuper 内嵌 JSON 正文抽取
# ---------------------------------------------------------------------------

import json  # noqa: E402

from app.services.fetcher import _extract_baidu_dtlandingsuper  # noqa: E402


def _dt_page(content: str) -> str:
    """构造 dtlandingsuper 风格的 HTML：正文在首个 "content" JSON 字符串里。"""
    article = json.dumps(content, ensure_ascii=True)
    related = json.dumps(" unrelated ", ensure_ascii=True)
    return f'<html><script>{{"content":{article},"x":1}},{{"content":{related}}}</script></html>'


def test_baidu_dtlandingsuper_extracts_first_content():
    body = "外地游客眼中的北京烤鸭店体验。<br />四季民福排队最凶，便宜坊焖炉偏嫩。" * 5
    text = _extract_baidu_dtlandingsuper(_dt_page(body))
    assert text is not None
    assert "四季民福" in text
    assert "<br" not in text
    assert "unrelated" not in text  # 只取首个 content，不混入相关推荐


def test_baidu_dtlandingsuper_short_content_rejected():
    assert _extract_baidu_dtlandingsuper(_dt_page("太短")) is None
    assert _extract_baidu_dtlandingsuper("<html>no json here</html>") is None
