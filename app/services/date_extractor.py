"""published_at 6-level fallback chain.

L1  Engine-provided published_at (already set on RawResult)
L2  extra["og:article:published_time"] / extra["article:published_time"]
L3  extra["datePublished"] (JSON-LD, returned by Serper / DataForSEO)
L4  URL path regex  /2024/03/15/  or  /2024-03-15
L5  Snippet / title relative time  "3天前"  "2 days ago"
L6  None
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.engines.base import RawResult

# ---------------------------------------------------------------------------
# L4 — URL date patterns
# ---------------------------------------------------------------------------

_URL_DATE_RE = re.compile(
    r"/(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})(?:[/\-?#]|$)"   # /2024/03/15/
    r"|[?&](?:date|pubdate|pub_date|publishdate)=(\d{4})(\d{2})(\d{2})",  # ?date=20240315
)

# ---------------------------------------------------------------------------
# L5 — Relative time patterns
# ---------------------------------------------------------------------------

_REL_ZH_RE = re.compile(
    r"(\d+)\s*(?:秒|分钟?|小时|天|周|月|年)前"
)
_REL_EN_RE = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_ABS_ZH_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_ABS_EN_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ZH_UNIT_SECONDS = {
    "秒": 1, "分": 60, "分钟": 60,
    "小时": 3600, "天": 86400,
    "周": 7 * 86400, "月": 30 * 86400, "年": 365 * 86400,
}
_EN_UNIT_SECONDS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400,
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _try_url(url: str) -> str | None:
    m = _URL_DATE_RE.search(url)
    if not m:
        return None
    if m.group(1):  # path pattern
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:           # query string pattern
        y, mo, d = int(m.group(4)), int(m.group(5)), int(m.group(6))
    try:
        dt = datetime(y, mo, d, tzinfo=timezone.utc)
        # Sanity: ignore dates before 2000 or in the future
        now = datetime.now(timezone.utc)
        if dt.year < 2000 or dt > now:
            return None
        return _iso(dt)
    except ValueError:
        return None


def _try_text(text: str) -> str | None:
    now = datetime.now(timezone.utc)

    # Chinese relative
    m = _REL_ZH_RE.search(text)
    if m:
        n = int(m.group(1))
        unit_str = text[m.start(1) + len(m.group(1)):m.end()].strip().rstrip("前")
        for unit, secs in _ZH_UNIT_SECONDS.items():
            if unit_str.startswith(unit):
                try:
                    return _iso(now - timedelta(seconds=n * secs))
                except OverflowError:
                    return None

    # English relative
    m = _REL_EN_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = _EN_UNIT_SECONDS.get(unit, 0)
        if secs:
            try:
                return _iso(now - timedelta(seconds=n * secs))
            except OverflowError:
                return None

    # Chinese absolute
    m = _ABS_ZH_RE.search(text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          tzinfo=timezone.utc)
            if 2000 <= dt.year <= now.year + 1:
                return _iso(dt)
        except ValueError:
            pass

    # English absolute
    m = _ABS_EN_RE.search(text)
    if m:
        try:
            mo = _MONTH_MAP.get(m.group(1)[:3].lower(), 0)
            dt = datetime(int(m.group(3)), mo, int(m.group(2)),
                          tzinfo=timezone.utc)
            if 2000 <= dt.year <= now.year + 1:
                return _iso(dt)
        except ValueError:
            pass

    return None


def extract_published_at(raw: RawResult) -> str | None:
    """Return best-guess published_at ISO string, or None."""
    # L1: engine-provided
    if raw.published_at:
        return raw.published_at

    extra = raw.extra or {}

    # L2: OpenGraph / article meta
    for key in ("og:article:published_time", "article:published_time",
                "article:published", "pubdate"):
        if val := extra.get(key):
            return val

    # L3: JSON-LD datePublished (Serper / DataForSEO)
    if val := extra.get("datePublished"):
        return val

    # L4: URL path / query string
    if val := _try_url(raw.url):
        return val

    # L5: snippet + title text patterns
    text = f"{raw.title} {raw.snippet}"
    if val := _try_text(text):
        return val

    return None  # L6: give up
