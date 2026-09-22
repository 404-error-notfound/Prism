"""Input validation and SSRF protection."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import unicodedata
from urllib.parse import urlparse

from fastapi import HTTPException, status

# Query constraints
MAX_QUERY_LEN = 512
MIN_QUERY_LEN = 1
MAX_TOP_N = 50

# Characters that should never appear in a search query
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 强时效词：明确指向"最近发生/实时"，无歧义 → 一定是时效查询
_TEMPORAL_STRONG_RE = re.compile(
    r"今[天日晚早]|今年|本[周月季]|这[周月]|这[几两\d]+天|近[几两\d]+[天周月年]|"
    r"昨[天晚早]|前天|大前天|上[周月年]|"
    r"刚刚|刚才|此刻|实时|实况|今日|当天|当下|最快|最早|即时|"
    r"\btoday\b|\btonight\b|\bnow\b|\bjust now\b|\bbreaking\b|\blive\b|"
    r"\bthis week\b|\bthis month\b",
    re.IGNORECASE,
)
# 弱时效词：可能是"最新版本"而非"最新新闻"（与参考名词同现时按版本义，不判时效）
_TEMPORAL_WEAK_RE = re.compile(
    r"最新|最近|现在|当前|目前|现行|\blatest\b|\bcurrent\b",
    re.IGNORECASE,
)
# 参考/文档/规格名词：与弱时效词同现 → "最新"指现行版本（要参考文档，非新闻），不判时效。
# 例："最新个人所得税税率表" / "现行公司法" / "最新异地就医政策" 要的是当前版本，
# 判成新闻会加 1 天时间过滤把无时效的参考页全滤掉。
_REFERENCE_RE = re.compile(
    r"税率表?|税率|法(?:律|规|典|条)?|条例|规定|规范|标准|国标|政策|办法|"
    r"全文|教程|攻略|名单|排行?榜?|价目?表|汇率|版本|参数|配置|说明书?|"
    r"目录|课程表|时刻表|公式|定义|原理|流程|步骤|清单"
)


def detect_temporal_query(query: str) -> bool:
    """Return True if the query wants real-time / recent information.

    强时效词直接命中；弱时效词（最新/现行/latest…）只有在不含参考文档名词时才算
    时效——"最新税率表/现行法规"是要现行版本，不是新闻，不该加时间过滤。
    """
    if _TEMPORAL_STRONG_RE.search(query):
        return True
    return bool(_TEMPORAL_WEAK_RE.search(query)) and not _REFERENCE_RE.search(query)


def validate_query(query: str) -> str:
    """Sanitize and validate a search query string."""
    if not isinstance(query, str):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="query must be a string")

    # Strip leading/trailing whitespace
    query = query.strip()

    # Length check
    if len(query) < MIN_QUERY_LEN:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="query is empty")
    if len(query) > MAX_QUERY_LEN:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"query exceeds {MAX_QUERY_LEN} characters")

    # Remove control characters
    query = _CONTROL_RE.sub("", query)

    # Normalize unicode (NFC) — avoids homoglyph injection
    query = unicodedata.normalize("NFC", query)

    return query


def validate_top_n(top_n: int) -> int:
    if not (1 <= top_n <= MAX_TOP_N):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"top_n must be 1–{MAX_TOP_N}")
    return top_n


def validate_recency_days(recency_days: int) -> int:
    if not (1 <= recency_days <= 3650):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="recency_days must be 1–3650")
    return recency_days


def validate_user_id(user_id: str) -> str:
    """Accept alphanumeric + hyphens/underscores, max 128 chars."""
    if not user_id:
        return ""
    if len(user_id) > 128:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="user_id too long")
    if not re.fullmatch(r"[A-Za-z0-9_\-\.@]+", user_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="user_id contains invalid characters")
    return user_id


# ---------------------------------------------------------------------------
# SSRF protection for any URL we might fetch
# ---------------------------------------------------------------------------

_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def validate_fetch_url(url: str) -> str:
    """Raise HTTPException if the URL points to an internal/reserved address."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https URLs are allowed")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="URL missing hostname")

    # Reject bare IPs immediately (except public ones verified below)
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr):
            raise HTTPException(status_code=400, detail="URL resolves to private address")
        return url
    except ValueError:
        pass  # hostname is a domain name, proceed to DNS

    # Resolve to IP and check
    try:
        resolved_ip = socket.gethostbyname(hostname)
        addr = ipaddress.ip_address(resolved_ip)
        if _is_private(addr):
            raise HTTPException(status_code=400, detail="URL resolves to private address")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="URL hostname could not be resolved")

    return url


async def validate_fetch_url_async(url: str) -> str:
    """validate_fetch_url 的 async 包装：其内部 socket.gethostbyname 是同步阻塞 DNS，
    在协程里直接调会冻结事件循环。所有 async 调用方应统一用此函数，避免各自记得包
    to_thread（漏包会导致并发抓取时整个 loop 被串行 DNS 卡死）。"""
    return await asyncio.to_thread(validate_fetch_url, url)


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _PRIVATE_RANGES)
