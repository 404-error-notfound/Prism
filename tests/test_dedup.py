"""Tests for URL deduplication."""
from app.engines.base import RawResult
from app.services.dedup import deduplicate, extract_domain


def _r(url: str, title: str = "T", snippet: str = "S") -> RawResult:
    return RawResult(title=title, url=url, snippet=snippet)


def test_dedup_removes_exact_duplicate():
    results = [_r("https://example.com/a"), _r("https://example.com/a")]
    assert len(deduplicate(results)) == 1


def test_dedup_trailing_slash():
    results = [_r("https://example.com/a/"), _r("https://example.com/a")]
    assert len(deduplicate(results)) == 1


def test_dedup_keeps_distinct():
    # 内容充分不同（token 数 >= 10）才会进入 SimHash 比较；此处标题/摘要各异
    r1 = _r(
        "https://example.com/a",
        title="Python asyncio 协程入门教程",
        snippet="本文介绍 asyncio 事件循环、Task 和 Future 的基本用法，适合初学者阅读",
    )
    r2 = _r(
        "https://example.com/b",
        title="Rust 所有权与借用机制详解",
        snippet="深入讲解 Rust 语言的所有权规则、借用检查器以及生命周期标注的实际应用",
    )
    assert len(deduplicate([r1, r2])) == 2


def test_extract_domain_simple():
    assert extract_domain("https://github.com/user/repo") == "github.com"


def test_extract_domain_subdomain_preserved():
    # mp.weixin.qq.com should stay as-is (not collapsed to qq.com)
    domain = extract_domain("https://mp.weixin.qq.com/s/abc")
    assert domain == "mp.weixin.qq.com"


def test_extract_domain_www_stripped():
    assert extract_domain("https://www.example.com/page") == "example.com"


def test_redirector_urls_keep_query():
    """跳转型 URL（weixin.sogou.com/link?url=<token>）的 query 是目标身份：
    不同 token 是不同文章，不得因剥 query 被误判重复（2026-09-08 实测
    微信源 10 条被误杀 9 条）。"""
    results = [
        _r(f"https://weixin.sogou.com/link?url=TOKEN{i}&type=2",
           title=f"文章{i}", snippet=f"内容{i}")
        for i in range(10)
    ]
    assert len(deduplicate(results, simhash=False)) == 10


def test_redirector_same_token_deduped():
    """同一跳转 token（同文多引擎重复召回）仍要去重。"""
    results = [
        _r("https://weixin.sogou.com/link?url=ABC&type=2", title="T", snippet="S"),
        _r("https://weixin.sogou.com/link?url=ABC&type=2&query=x", title="T", snippet="S"),
    ]
    assert len(deduplicate(results, simhash=False)) == 1
