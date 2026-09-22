"""searxng/render-settings.sh 回归测试：脚本与 settings.template.yml 的兼容性。

背景：2026-09-17 生产事故——模板头部注释提到占位符名，子串匹配的 sed 把
proxies 块插进文件头注释中间，渲染出非法 YAML，searxng 全实例 crash loop。
本测试直接运行真实脚本 + 真实模板，校验渲染产物（YAML 合法、占位符无残留、
注释完好），任何脚本/模板改动都必须过这一关。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "searxng" / "render-settings.sh"
TEMPLATE = ROOT / "searxng" / "settings.template.yml"

SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(SH is None, reason="需要 sh/bash（Git Bash 或 POSIX 环境）")


def _render(tmp_path, service_url, token, template: Path = TEMPLATE):
    out = tmp_path / "settings.yml"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PROXY_SERVICE_URL": service_url,
        "PROXY_PROJECT_TOKEN": token,
    }
    proc = subprocess.run(
        [SH, str(SCRIPT), str(template), str(out)],
        env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30,
    )
    return proc, out


def _load(out: Path):
    return yaml.safe_load(out.read_text(encoding="utf-8"))


class TestRenderWithReplicas:
    def test_two_replicas_render_valid_yaml(self, tmp_path):
        proc, out = _render(
            tmp_path,
            "http://10.0.0.1:8100,http://10.0.0.2:8100",
            "tok123",
        )
        assert proc.returncode == 0, proc.stderr
        data = _load(out)
        proxies = data["outgoing"]["proxies"]
        expected = [
            "http://prism:tok123@10.0.0.1:8101",
            "http://prism:tok123@10.0.0.2:8101",
        ]
        assert proxies["http://"] == expected
        assert proxies["https://"] == expected

    def test_no_placeholder_residue_and_header_intact(self, tmp_path):
        """占位符必须只被替换一次；文件头注释完好（2026-09-17 事故回归点）。"""
        _, out = _render(tmp_path, "http://h1:8100", "t")
        text = out.read_text(encoding="utf-8")
        assert "__PROXY_TUNNEL_PROXIES__" not in text
        assert text.startswith("# SearXNG settings for Prism")
        # proxies 块只能出现在 outgoing 段下这一处（不能插进文件头）
        assert text.count("  proxies:") == 1
        assert text.index("  proxies:") > text.index("outgoing:")

    def test_any_control_port_becomes_8101(self, tmp_path):
        _, out = _render(tmp_path, "http://hub.example:9999", "t")
        proxies = _load(out)["outgoing"]["proxies"]
        assert proxies["http://"] == ["http://prism:t@hub.example:8101"]


class TestRenderEdgeCases:
    def test_empty_service_url_renders_without_proxies(self, tmp_path):
        proc, out = _render(tmp_path, "", "")
        assert proc.returncode == 0, proc.stderr
        assert "WARNING" in proc.stderr
        data = _load(out)
        assert "proxies" not in data["outgoing"]

    def test_service_url_without_token_refuses_to_start(self, tmp_path):
        proc, _ = _render(tmp_path, "http://h1:8100", "")
        assert proc.returncode == 1
        assert "PROXY_PROJECT_TOKEN" in proc.stderr

    def test_placeholder_name_in_comment_is_not_a_match(self, tmp_path):
        """注释里出现占位符字面量不得被误替换、也不得误报残留（锚定整行匹配）。"""
        template = tmp_path / "t.yml"
        template.write_text(
            "# 提到 __PROXY_TUNNEL_PROXIES__ 的注释\n"
            "outgoing:\n"
            "  __PROXY_TUNNEL_PROXIES__\n"
            "  request_timeout: 5.0\n",
            encoding="utf-8",
        )
        proc, out = _render(tmp_path, "http://h1:8100", "t", template=template)
        assert proc.returncode == 0, proc.stderr
        text = out.read_text(encoding="utf-8")
        assert text.startswith("# 提到 __PROXY_TUNNEL_PROXIES__ 的注释\n")
        data = yaml.safe_load(text)
        assert data["outgoing"]["proxies"]["http://"] == [
            "http://prism:t@h1:8101"
        ]
