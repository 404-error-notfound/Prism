"""searxng/overrides/baidu.py 测试：HTML 解析版 baidu 引擎。

背景：上游 baidu 引擎的 tn=json 接口 100% 触发 302 → wappass 验证码（2026-09
A/B 实测），override 版去掉 tn=json 改解析 HTML 结果页。本测试用 outputs/ 下的
实测固件（真实 baidu SERP 抓取）做 ground truth，并用 sys.modules 打桩 searx.*
依赖后按文件路径加载 override 模块（searxng/ 不是 Python 包，且模块顶层 import
searx.exceptions / searx.utils）。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE = ROOT / "searxng" / "overrides" / "baidu.py"
FIXTURES = ROOT / "outputs"


def _load_baidu_module():
    """打桩 searx.* 后按路径加载 override 模块（容器内它与 searx 包同进程）。"""
    exc = types.ModuleType("searx.exceptions")

    class SearxEngineAPIException(Exception):
        pass

    class SearxEngineCaptchaException(Exception):
        pass

    exc.SearxEngineAPIException = SearxEngineAPIException
    exc.SearxEngineCaptchaException = SearxEngineCaptchaException
    utils = types.ModuleType("searx.utils")
    utils.html_to_text = lambda x: x
    sys.modules.setdefault("searx", types.ModuleType("searx"))
    sys.modules["searx.exceptions"] = exc
    sys.modules["searx.utils"] = utils

    spec = importlib.util.spec_from_file_location("baidu_override", OVERRIDE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baidu = _load_baidu_module()


def _parse(fixture: str):
    text = (FIXTURES / fixture).read_text(encoding="utf-8", errors="replace")
    return baidu.parse_general_html(text)


class TestParseGeneralHtml:
    """固件实测：长尾 query 两个版本、热词版、JS 壳页（仅广告/卡片）。"""

    def test_sample2_longtail(self):
        results = _parse("baidu_html_sample2.html")
        assert len(results) >= 8
        assert any("强大酒业" in r["title"] for r in results)
        # 标题/链接/摘要三要素齐全；链接是 baidu redirect link（下游 prism 已兼容）
        for r in results:
            assert r["title"] and r["url"].startswith("http")
        assert any("强大酒业" in r["content"] for r in results)

    def test_v3_longtail(self):
        results = _parse("_v3.html")
        assert len(results) >= 8
        assert any("强大酒业" in r["title"] for r in results)

    def test_v2_hotword(self):
        # 固件实际可提取 8 条（6 条 www_index 有机结果 + 百度图片/精选笔记 2 张卡片），
        # 其余容器是 4 条 ec_title 广告 + AI 回答卡 + "大家还在搜"（均为非结果，应剔除）
        results = _parse("_v2.html")
        assert len(results) >= 8
        assert any("智能手表" in r["title"] for r in results)

    def test_sample3_ads_filtered(self):
        """JS 壳页：3 条 ec_title 广告必须全部过滤，只剩非广告卡片；
        广告标题（京东/淘宝类）不得出现在结果里。"""
        results = _parse("baidu_html_sample3.html")
        titles = [r["title"] for r in results]
        assert not any("京东" in t or "淘宝" in t or "剪映" in t for t in titles)
        # 广告落地链接（baidu.php?url= 跳转）不得漏进结果
        assert not any("baidu.php" in r["url"] for r in results)

    def test_js_shell_page_returns_empty_gracefully(self):
        """纯 JS 壳/空页：自然返回空列表，不算错误。"""
        assert baidu.parse_general_html("<html><body></body></html>") == []


class TestRequest:
    def test_no_tn_json_and_params_kept(self):
        """根因修复点：general 类目不得再带 tn=json；rn/pn/gpc 保留。"""
        params = baidu.request("测试", {"pageno": 2, "time_range": "week"})
        url = params["url"]
        assert url.startswith("https://www.baidu.com/s?")
        assert "tn=json" not in url
        assert "rn=10" in url and "pn=10" in url
        assert "gpc=" in url  # time_range → gpc 时间窗
        assert params["allow_redirects"] is False

    def test_no_time_range_no_gpc(self):
        params = baidu.request("测试", {"pageno": 1})
        assert "gpc=" not in params["url"]

    def test_accept_header_is_browser_like(self):
        """百度按 Accept 分流：SearXNG 默认出站 Accept 是 */* → 一律 302 验证码；
        浏览器 Accept → 200 放行（2026-09 同一独享 IP A/B 实测）。request() 必须补上。"""
        params = baidu.request("测试", {"pageno": 1})
        assert params["headers"]["Accept"] == (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        )

    def test_accept_header_does_not_override_existing(self):
        """引擎已有头（如模板配的 Referer）不被覆盖；已设 Accept 也不覆盖。"""
        params = baidu.request(
            "测试",
            {"pageno": 1, "headers": {"Referer": "https://www.baidu.com/"}},
        )
        assert params["headers"]["Referer"] == "https://www.baidu.com/"
        assert "image/avif" in params["headers"]["Accept"]
        params = baidu.request(
            "测试", {"pageno": 1, "headers": {"Accept": "text/custom"}}
        )
        assert params["headers"]["Accept"] == "text/custom"


class TestCaptchaDetection:
    """wappass 302 验证码：看 Location 头即抛 Captcha 异常（不读 body）。"""

    class _Resp:
        def __init__(self, headers, text=""):
            self.headers = headers
            self.text = text

    def test_wappass_redirect_raises_captcha(self):
        resp = self._Resp(
            {"Location": "https://wappass.baidu.com/static/captcha/xxx"}
        )
        with pytest.raises(baidu.SearxEngineCaptchaException):
            baidu.response(resp)

    def test_normal_200_parses(self, tmp_path):
        text = (FIXTURES / "_v3.html").read_text(encoding="utf-8", errors="replace")
        results = baidu.response(self._Resp({}, text))
        assert len(results) >= 8
