"""ContentFormats conversion — text / markdown / html.

Internal pipeline always produces Markdown (from trafilatura/readability/Crawl4AI).
Caller asks for one of three output formats via SearchRequest.ContentFormats.
"""
from __future__ import annotations

import html
import re

_MD_LINK_RE  = re.compile(r"\[([^\]]+)\]\([^)]+\)")          # [text](url) → text
_MD_IMG_RE   = re.compile(r"!\[[^\]]*\]\([^)]+\)")           # ![alt](url) → ""
_MD_CODE_RE  = re.compile(r"`([^`]+)`")                      # `code` → code
_MD_BOLD_RE  = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")    # **x** / __x__ → x
_MD_ITAL_RE  = re.compile(r"\*([^*]+)\*|_([^_]+)_")          # *x* / _x_ → x
_MD_HEAD_RE  = re.compile(r"^#{1,6}\s*", re.MULTILINE)       # leading #s
_MD_QUOTE_RE = re.compile(r"^>\s*", re.MULTILINE)            # > quote
_MD_LIST_RE  = re.compile(r"^[*\-+]\s+", re.MULTILINE)       # - bullet
_MD_HR_RE    = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_BLANKS_RE   = re.compile(r"\n{3,}")


def markdown_to_text(md: str) -> str:
    """Strip Markdown markup → plain text. Preserves paragraph breaks."""
    s = _MD_IMG_RE.sub("", md)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _MD_CODE_RE.sub(r"\1", s)
    s = _MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2), s)
    s = _MD_ITAL_RE.sub(lambda m: m.group(1) or m.group(2), s)
    s = _MD_HEAD_RE.sub("", s)
    s = _MD_QUOTE_RE.sub("", s)
    s = _MD_LIST_RE.sub("", s)
    s = _MD_HR_RE.sub("", s)
    s = _BLANKS_RE.sub("\n\n", s)
    return s.strip()


def markdown_to_html(md: str) -> str:
    """Lightweight Markdown → HTML. Avoids a heavy dependency for the common case."""
    lines = md.split("\n")
    out: list[str] = []
    in_para: list[str] = []

    def flush_para() -> None:
        if in_para:
            text = " ".join(in_para)
            text = _MD_IMG_RE.sub("", text)
            text = _MD_LINK_RE.sub(r'<a href="\g<0>">\1</a>', text)  # naive but acceptable
            text = _MD_BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
            text = _MD_ITAL_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
            text = _MD_CODE_RE.sub(r"<code>\1</code>", text)
            out.append(f"<p>{text}</p>")
            in_para.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_para()
            continue
        if stripped.startswith("#"):
            flush_para()
            level = len(stripped) - len(stripped.lstrip("#"))
            level = min(level, 6)
            text = stripped.lstrip("#").strip()
            out.append(f"<h{level}>{html.escape(text)}</h{level}>")
        elif stripped.startswith(("- ", "* ", "+ ")):
            flush_para()
            out.append(f"<li>{html.escape(stripped[2:])}</li>")
        else:
            in_para.append(stripped)
    flush_para()
    return "\n".join(out)


def format_content(content: str, fmt: str, source: str = "markdown") -> str:
    """Convert `content` (currently always sourced from markdown) to `fmt`."""
    fmt = (fmt or "text").lower()
    if not content:
        return content
    if fmt == "markdown":
        return content
    if fmt == "html":
        return markdown_to_html(content)
    # "text" or unknown → strip
    return markdown_to_text(content)
