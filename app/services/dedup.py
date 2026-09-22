"""URL-level + SimHash content-level deduplication."""
from __future__ import annotations

import hashlib
import unicodedata
from urllib.parse import parse_qs, urlsplit

import tldextract

from app.engines.base import RawResult


# 跳转/代理型端点：query string 携带真实目标，去重时不得剥离
_REDIRECTOR_HOSTS = frozenset({"weixin.sogou.com"})


# ── URL 去重 ──────────────────────────────────────────────────

def deduplicate(results: list[RawResult], simhash: bool = True) -> list[RawResult]:
    """URL 去重 + 可选 SimHash 内容去重（纯内存，当前结果集内）。

    simhash=False 时只做 URL 去重（Fast 治理路径使用）。
    """
    url_deduped = _url_dedup(results)
    return _simhash_dedup(url_deduped) if simhash else url_deduped


def _url_dedup(results: list[RawResult]) -> list[RawResult]:
    seen: set[str] = set()
    out: list[RawResult] = []
    for r in results:
        key = _normalise_url(r.url)
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── SimHash 内容去重 ───────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text.lower())
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if buf.strip():
                tokens.extend(buf.split())
                buf = ""
            tokens.append(ch)          # CJK 单字保留
        else:
            buf += ch
    if buf.strip():
        tokens.extend(buf.split())
    # CJK 单字有意义；ASCII token 过滤长度 1 的噪声
    return [t for t in tokens if len(t) >= 1 and (
        "\u4e00" <= t[0] <= "\u9fff" or len(t) > 1
    )]


def _simhash(text: str) -> int:
    tokens = _tokenize(text)
    if not tokens:
        return 0
    v = [0] * 64
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16) & 0xFFFFFFFFFFFFFFFF
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i in range(64):
        if v[i] > 0:
            result |= 1 << i
    return result


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _simhash_dedup(
    results: list[RawResult], threshold: int = 3
) -> list[RawResult]:
    """
    在当前结果集内做 SimHash 内容去重。
    用 title + snippet 计算指纹（SearXNG 返回时正文还未抓取）。
    海明距离 ≤ threshold 视为重复内容，保留先出现的那条。
    token 数 < 10 时跳过（短文本 SimHash 误判率高）。
    """
    seen_fps: list[int] = []
    out: list[RawResult] = []
    for r in results:
        text = r.title + " " + (r.snippet or "")
        if len(_tokenize(text)) < 10:
            out.append(r)
            continue
        fp = _simhash(text)
        if any(_hamming(fp, e) <= threshold for e in seen_fps):
            continue
        seen_fps.append(fp)
        out.append(r)
    return out


# ── 工具函数 ──────────────────────────────────────────────────

def extract_domain(url: str) -> str:
    try:
        ext = tldextract.extract(url)
        if ext.domain and ext.suffix:
            sub = ext.subdomain
            domain = f"{ext.domain}.{ext.suffix}"
            if sub and sub not in ("www", ""):
                return f"{sub}.{domain}"
            return domain
    except Exception:
        pass
    return url


def _normalise_url(url: str) -> str:
    u = url.rstrip("/").lower()
    # 跳转/代理型 URL 的 query 是目标身份本身，整个剥掉会把同端点的不同文章
    # 全判成重复（2026-09-08 生产实测：sogou 微信源 10 条全是
    # weixin.sogou.com/link?url=<token>，9 条被误杀，微信公众号这个中文优质源
    # 等于被废掉）。但 type=/query= 等会话参数因请求而异，只保留身份参数 url=。
    parts = urlsplit(u)
    if parts.netloc in _REDIRECTOR_HOSTS:
        ident = parse_qs(parts.query).get("url", [""])[0]
        return f"{parts.netloc}{parts.path}?url={ident}" if ident else u
    return u.split("?")[0]
