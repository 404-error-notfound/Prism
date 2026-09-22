"""Page content fetcher for require_detail=true requests.

Strategy (in order):
  1. trafilatura  — fast, pure-HTTP; handles most static pages
  2. readability-lxml — lightweight DOM parse on the same already-fetched HTML
  3. Crawl4AI  — Playwright-backed fallback for JS-rendered / anti-bot pages;
                browser singleton kept alive to avoid per-URL startup cost

robots.txt is checked before fetching (cached in Redis 24h).
SSRF protection is enforced via app.core.validator.validate_fetch_url.
Fetch concurrency defaults to the full result count; parse/browser concurrency are CPU-aware.
All result URLs are fetched when NeedContent=true; batch timeout (default 5s) caps wall-clock time.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import re
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import httpx

from app.core.config import BROWSER_USER_AGENT, settings
from app.core.redis import get_redis
from app.core.validator import validate_fetch_url_async

logger = logging.getLogger(__name__)

CONTENT_CACHE_TTL = 3600  # Redis content cache TTL (1h)

# 统一 5s：并发抓 10 个 URL 时多路 TLS 握手同时挤在一起，旧值 connect=2s 会系统性
# 误超时（单发能过、并发全挂）。整体墙钟仍由 collect_page_fetches 的 content budget
# 兜底，放宽单连接超时不会放大冷路延迟。
_HTTP_TIMEOUT = httpx.Timeout(5.0)

# crawl4ai 外层 wait_for 在 page_timeout 之上的余量：兜的是导航完成后的
# 抽取/fit_markdown 阶段（不受 page_timeout 约束），让 Playwright 先在内部
# 干净地中止导航，避免 asyncio 级取消产生孤儿 Future
_CRAWL4AI_WAIT_SLACK_S = 2.0

# Shared httpx client — connection pool reused across all parallel fetches.
# 代理池刷新时自动重建（通过 _current_proxy 跟踪当前代理 URL）。
_http_client: httpx.AsyncClient | None = None
_current_proxy: str | None = None
# 直连客户端（内容抓取默认路径）：国内内容站（toutiao/sina/zhihu/baijiahao
# 等）会风控代理池的短效出口 IP，走隧道实测整批 fetch.failed；直连才抓得到。
_direct_http_client: httpx.AsyncClient | None = None


def _get_direct_client() -> httpx.AsyncClient:
    global _direct_http_client
    if _direct_http_client is None or _direct_http_client.is_closed:
        _direct_http_client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
    return _direct_http_client


def _get_http_client() -> httpx.AsyncClient:
    global _http_client, _current_proxy

    # 经 proxy-hub 隧道出网（隧道后端自动轮询当前活 IP 池）；
    # 隧道地址变更即代理变更，客户端随之重建
    try:
        from app.services.proxy_client import get_tunnel_proxy_url
        new_proxy = get_tunnel_proxy_url()
    except Exception:
        new_proxy = None

    if _http_client is None or _http_client.is_closed or new_proxy != _current_proxy:
        if _http_client and not _http_client.is_closed:
            # 旧客户端异步关闭放到后台，不阻塞当前请求
            asyncio.ensure_future(_http_client.aclose())
        _current_proxy = new_proxy
        _http_client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            proxy=new_proxy,  # httpx 0.28+ 移除了 proxies=，改用 proxy=（None 即直连）
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client, _current_proxy, _direct_http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
    _current_proxy = None
    if _direct_http_client and not _direct_http_client.is_closed:
        await _direct_http_client.aclose()
        _direct_http_client = None


def _fetch_clients() -> list[tuple[str, httpx.AsyncClient]]:
    """内容抓取客户端，按尝试顺序返回 (egress_tag, client)，第一个失败换下一个。

    content_fetch_egress 控制顺序：
      proxy_first  —— 默认。代理隧道（运营商 IP 池）优先、直连兜底。内容站风控
                     对运营商 IP 比服务器机房 IP 友好得多；
      direct_first —— 直连优先、隧道兜底（家庭宽带等本身即运营商出口的环境适用）。
    未配隧道（PROXY_SERVICE_URL 为空，推导不出隧道地址）时两种模式都退化为仅直连。
    """
    direct = _get_direct_client()
    tunnel: httpx.AsyncClient | None = None
    try:
        from app.services.proxy_client import get_tunnel_proxy_url
        if get_tunnel_proxy_url():
            tunnel = _get_http_client()
    except Exception:
        pass
    ordered = [("proxy", tunnel), ("direct", direct)]
    if settings.content_fetch_egress == "direct_first":
        ordered.reverse()
    return [(tag, c) for tag, c in ordered if c is not None]
MIN_CONTENT_LEN = 100      # discard result if shorter
MAX_CONTENT_LEN = 20_000   # truncate to save tokens (~5k tokens)
_CPU_COUNT = max(1, os.cpu_count() or 1)
FETCH_CONCURRENCY = settings.content_fetch_concurrency  # 0 means one task per URL
PARSE_CONCURRENCY = settings.content_parse_concurrency or max(2, _CPU_COUNT)
CRAWL4AI_CONCURRENCY = settings.crawl4ai_concurrency or max(1, _CPU_COUNT // 2)
_PARSE_SEM = asyncio.Semaphore(PARSE_CONCURRENCY)
_BACKGROUND_FETCH_TASKS: set[asyncio.Task] = set()


def _fetch_concurrency(url_count: int) -> int:
    """Return per-batch URL fetch concurrency. 0 means no artificial cap."""
    if url_count <= 0:
        return 1
    if FETCH_CONCURRENCY <= 0:
        return url_count
    return min(url_count, FETCH_CONCURRENCY)


async def _run_parse(func, *args):
    """Run synchronous HTML extraction under a CPU-sized concurrency gate."""
    async with _PARSE_SEM:
        start = time.monotonic()
        try:
            return await asyncio.to_thread(func, *args)
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            if latency_ms >= 200:
                logger.info(
                    "fetch.parse_slow extractor=%s latency_ms=%d",
                    getattr(func, "__name__", "unknown"),
                    latency_ms,
                )


def _keep_background_fetch(task: asyncio.Task) -> None:
    """Let timed-out content fetches finish in the background and populate cache."""
    _BACKGROUND_FETCH_TASKS.add(task)

    def _consume(done: asyncio.Task) -> None:
        _BACKGROUND_FETCH_TASKS.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("background content fetch failed: %s", exc)

    task.add_done_callback(_consume)

# Domains that skip robots.txt check — loaded from Redis at startup
_ROBOTS_SKIP_DOMAINS: set[str] = set()


async def load_robots_skip_domains_from_redis() -> None:
    """从 Redis 加载 robots 跳过域名配置。"""
    global _ROBOTS_SKIP_DOMAINS
    r = get_redis()
    domains = await r.smembers("domain:tier:robots_skip")
    _ROBOTS_SKIP_DOMAINS = {d.decode() if isinstance(d, bytes) else d for d in domains}
    logger.info("fetcher: loaded %d robots-skip domains from Redis", len(_ROBOTS_SKIP_DOMAINS))


# Shared Crawl4AI browser instance (started lazily, kept alive for reuse)
_crawler = None
_crawler_lock: asyncio.Lock | None = None
_CRAWL4AI_SEM = asyncio.Semaphore(CRAWL4AI_CONCURRENCY)  # 并发浏览器标签上限


def _get_crawler_lock() -> asyncio.Lock:
    global _crawler_lock
    if _crawler_lock is None:
        _crawler_lock = asyncio.Lock()
    return _crawler_lock


async def _get_crawler():
    """Return the shared AsyncWebCrawler, starting it on first use."""
    global _crawler
    async with _get_crawler_lock():
        if _crawler is None:
            try:
                from crawl4ai import AsyncWebCrawler, BrowserConfig
                cfg = BrowserConfig(headless=True, verbose=False)
                _crawler = AsyncWebCrawler(config=cfg)
                await _crawler.start()
                logger.info("crawl4ai browser started (singleton)")
            except Exception as exc:
                logger.warning("crawl4ai unavailable: %s", exc)
                _crawler = None
    return _crawler


async def close_crawler() -> None:
    """Gracefully close the shared browser at shutdown."""
    global _crawler
    if _crawler is not None:
        try:
            await _crawler.close()
        except Exception:
            pass
        _crawler = None
        logger.info("crawl4ai browser closed")


# ---------------------------------------------------------------------------
# Content cleaning
# ---------------------------------------------------------------------------

# Meaningful chars: letters (Latin + CJK) and digits
_WORD_CHAR_RE = re.compile(r"[a-zA-Z\u4e00-\u9fff0-9]")

# Patterns that identify JS/CSS/boilerplate lines regardless of char ratio
_CODE_LINE_RE = re.compile(
    r"^\s*(?:"
    r"var\s|let\s|const\s|function\s|function\(|=>|"       # JS declarations
    r"import\s|export\s|require\(|module\.exports|"         # JS modules
    r"class\s\w|@media|@keyframes|@import|"                 # CSS / JS class
    r"<script|</script|<style|</style|"                     # HTML tags leaking in
    r"window\.|document\.|console\.|addEventListener|"      # JS globals
    r"\.css|\.js|\.map\b"                                   # asset references
    r")"
)

_STANDALONE_URL_RE = re.compile(r"^https?://\S+$")
_IMAGE_MD_RE = re.compile(r"^!\[.*?\]\(.*?\)$")


def _is_noise_line(line: str) -> bool:
    """Return True if the line is likely JS/CSS/boilerplate and should be dropped."""
    s = line.strip()
    if not s:
        return False  # blank lines handled separately

    # Single-char or pure-punctuation lines
    if len(s) <= 2 and not s.startswith("#"):
        return True

    # Standalone bare URL (no surrounding text)
    if _STANDALONE_URL_RE.match(s):
        return True

    # Markdown image-only lines (no readable text)
    if _IMAGE_MD_RE.match(s):
        return True

    # JavaScript / CSS code patterns
    if _CODE_LINE_RE.match(s):
        return True

    # High noise-char density: if meaningful chars < 35% of line, discard
    # Only apply to lines long enough to be sure (avoids dropping short headings)
    if len(s) > 12:
        ratio = len(_WORD_CHAR_RE.findall(s)) / len(s)
        if ratio < 0.35:
            return True

    return False


# 搜索引擎反爬/验证页签名：抓到的"正文"其实是 CAPTCHA 拦截页（sogou 公众号
# weixin.sogou.com/link 高发），内容与查询无关。命中视为抓取失败，逐级降级，
# 绝不把验证页当正文交付（否则它常作为唯一结果活到最后，正文全是验证提示）。
_BLOCKED_CONTENT_RE = re.compile(
    r"验证码用于确认这些请求|需要您协助验证|请输入验证码以继续",
)


def _looks_blocked(text: str) -> bool:
    """正文是否为反爬/验证拦截页（而非真实内容）。"""
    return bool(_BLOCKED_CONTENT_RE.search(text))


# 已知抓不出正文的域名：weixin.sogou.com/link 是带 token 的跳转壳，直连与代理
# 实测都被 302 进 /antispider/ 验证码墙（需人机验证，Crawl4AI 也过不了）。抓它
# 只会白吃 content budget，直接判失败把预算留给其他 URL。
_SKIP_FETCH_HOSTS = frozenset({"weixin.sogou.com"})


def _bare_host(url: str) -> str:
    """取 hostname 并去掉 www. 前缀。注意不能用 lstrip("www.")——它按字符集剥离，
    "weixin.sogou.com" 会被剥成 "eixin.sogou.com"。"""
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _is_skip_fetch_host(url: str) -> bool:
    netloc = _bare_host(url)
    return any(netloc == h or netloc.endswith("." + h) for h in _SKIP_FETCH_HOSTS)


def _clean_markdown(text: str) -> str:
    """Strip JS/CSS/boilerplate lines and collapse excess blank lines."""
    lines = text.splitlines()
    kept = [ln for ln in lines if not _is_noise_line(ln)]
    # Collapse 3+ consecutive blank lines into 1
    cleaned: list[str] = []
    blanks = 0
    for ln in kept:
        if ln.strip() == "":
            blanks += 1
            if blanks == 1:
                cleaned.append("")
        else:
            blanks = 0
            cleaned.append(ln)
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# robots.txt check (Redis-cached 24h)
# ---------------------------------------------------------------------------

async def _is_allowed_by_robots(url: str) -> bool:
    """Return True if robots.txt permits crawling this URL. Fails open."""
    try:
        parsed = urlparse(url)
        netloc = _bare_host(url)

        # Skip robots.txt fetch for well-known crawlable domains
        if any(netloc == d or netloc.endswith("." + d) for d in _ROBOTS_SKIP_DOMAINS):
            return True

        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        cache_key = f"robots:{parsed.netloc}"

        r = get_redis()
        cached = await r.get(cache_key)
        if cached is None:
            client = _fetch_clients()[0][1]  # 与正文抓取同链路（默认代理隧道）
            resp = await client.get(robots_url)
            robots_text = resp.text if resp.status_code == 200 else ""
            await r.setex(cache_key, 86400, robots_text)
        else:
            robots_text = cached

        if not robots_text:
            return True

        try:
            from robotexclusionrulesparser import RobotExclusionRulesParser
            parser = RobotExclusionRulesParser()
            parser.parse(robots_text)
            return parser.is_allowed("*", url)
        except ImportError:
            # governance extra not installed — fail open
            return True
    except Exception as exc:
        logger.debug("robots.txt check failed for %s: %s", url, exc)
        return True  # fail open


# ---------------------------------------------------------------------------
# Crawl4AI fetcher (primary) — uses shared singleton browser
# ---------------------------------------------------------------------------

_run_cfg = None


def _get_run_cfg():
    """构建并缓存 CrawlerRunConfig（settings 是导入期单例，字段均为进程级常量）。"""
    global _run_cfg
    if _run_cfg is None:
        from crawl4ai import CacheMode, CrawlerRunConfig

        _run_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            word_count_threshold=10,
            remove_overlay_elements=True,
            excluded_tags=["nav", "footer", "header", "aside", "script", "style"],
            # 让 Playwright 在内部超时中止导航，走干净的错误路径。
            # 注意只在 wait_for 先于外部取消时生效：批量路径下 CONTENT_FETCH_BUDGET_S
            # 的 task.cancel() 通常先触发，撕裂出的孤儿 Future 靠 logging.py 的
            # _PlaywrightOrphanFilter 消音；这里完整覆盖的是 /api/fetch 单 URL 路径
            page_timeout=int(settings.crawl4ai_timeout_s * 1000),
        )
    return _run_cfg


async def _fetch_crawl4ai(url: str) -> str | None:
    try:
        crawler = await _get_crawler()
        if crawler is None:
            return None

        async with _CRAWL4AI_SEM:
            result = await asyncio.wait_for(
                crawler.arun(url=url, config=_get_run_cfg()),
                timeout=settings.crawl4ai_timeout_s + _CRAWL4AI_WAIT_SLACK_S,
            )

        if result.success and result.markdown:
            # Only use fit_markdown — raw_markdown contains full-page noise (JS/CSS/nav)
            md = result.markdown.fit_markdown
            if md:
                md = _clean_markdown(md)
                return md[:MAX_CONTENT_LEN] if len(md) >= MIN_CONTENT_LEN else None
        return None
    except ImportError:
        logger.debug("crawl4ai not installed, falling back to trafilatura")
        return None
    except Exception as exc:
        logger.debug("crawl4ai failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# trafilatura fallback (fast, static pages)
# ---------------------------------------------------------------------------

def _trafilatura_extract_sync(html: str) -> str | None:
    """Sync trafilatura call — runs in a worker thread to avoid blocking the loop."""
    import trafilatura
    return trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        output_format="markdown",
    )


async def _fetch_trafilatura(url: str) -> tuple[str | None, str | None, str]:
    """Fetch URL and extract via trafilatura.

    Returns (content, raw_html, fail_reason):
      content  — cleaned Markdown, or None if extraction failed / too short
      raw_html — the response body (even when extraction failed), so callers
                 can pass it to a second extractor without a second HTTP fetch
      fail_reason — 失败原因（供 fetch.failed 日志定位）：
                 download:<egress>:<异常类型>[|<egress2>:...] 每条出网链路的
                 失败都留痕；extract_empty 表示下载成功但正文抽取为空/过短

    下载按 _fetch_clients() 顺序尝试（默认代理优先、直连兜底），全部失败才降级。

    返回的 fail_reason 供 fetch.failed 日志定位：download:<egress>:<异常> 表示
    某条链路下载失败；html 非空而 content 为 None 表示下载成功但抽取为空。
    """
    try:
        html: str | None = None
        legs: list[str] = []  # 每条出网链路的失败原因（proxy/direct 都留痕）
        for tag, client in _fetch_clients():
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
                break
            except Exception as exc:
                legs.append(f"{tag}:{type(exc).__name__}")
                logger.debug("content download failed for %s via %s: %s", url, tag, exc)
        if html is None:
            return None, None, "download:" + "|".join(legs) if legs else "download:unknown"
        # httpx 选错编码会产出大量 U+FFFD（常见于声明 utf-8 实为 GBK 的中文站，
        # 如 51test.net；正文整段变 "���й�����"）。GBK/GB2312 是 gb18030 子集，
        # 用它重解码若替换符更少即采用——只在确诊乱码时触发，不动正常页。
        bad = html.count("�")
        if bad >= 5:
            alt = resp.content.decode("gb18030", errors="replace")
            if alt.count("�") < bad:
                html = alt

        # trafilatura.extract is sync CPU-bound (lxml parse) — offload to a
        # worker thread so it doesn't block the event loop. Otherwise parallel
        # fetches serialize on the loop and starve unrelated Redis pipelines.
        text = await _run_parse(_trafilatura_extract_sync, html)
        if text and len(text) >= MIN_CONTENT_LEN:
            text = _clean_markdown(text)
            if len(text) >= MIN_CONTENT_LEN:
                return text[:MAX_CONTENT_LEN], html, ""
        return None, html, "extract_empty"
    except ImportError:
        logger.debug("trafilatura not installed")
        return None, None, "trafilatura_missing"
    except Exception as exc:
        logger.debug("trafilatura failed for %s: %s", url, exc)
        return None, None, f"error:{type(exc).__name__}"


# ---------------------------------------------------------------------------
# readability-lxml fallback (lightweight DOM parse on already-fetched HTML)
# ---------------------------------------------------------------------------

def _extract_readability(html: str) -> str | None:
    """Extract Markdown from an already-fetched HTML string via readability-lxml.

    Called with the HTML body that trafilatura already downloaded, so no extra
    HTTP round-trip is needed. Sync — call via asyncio.to_thread from async code.
    """
    try:
        import lxml.html as lh
        from readability import Document

        doc = Document(html)
        summary = doc.summary()
        text = lh.document_fromstring(summary).text_content()
        if text and len(text) >= MIN_CONTENT_LEN:
            paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
            md = "\n\n".join(paragraphs)
            md = _clean_markdown(md)
            return md[:MAX_CONTENT_LEN] if len(md) >= MIN_CONTENT_LEN else None
    except ImportError:
        logger.debug("readability-lxml not installed")
    except Exception as exc:
        logger.debug("readability extraction failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# mbd.baidu.com dtlandingsuper 落地页：正文嵌在首个 "content":" JSON 字符串里
# （\uXXXX 转义 + <br> 标签），页面靠 JS 渲染，trafilatura/readability 都抽不到
# ---------------------------------------------------------------------------

_DT_CONTENT_RE = re.compile(r'"content":"')
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _extract_baidu_dtlandingsuper(html: str) -> str | None:
    """从 baidu dtlandingsuper 页的内嵌 JSON 抽正文（首个 content 字段是正文，

    后面的是相关推荐）。json.raw_decode 从开引号处解析，自动处理 \\uXXXX 转义。
    """
    import json

    m = _DT_CONTENT_RE.search(html)
    if not m:
        return None
    try:
        text, _ = json.JSONDecoder().raw_decode(html[m.end() - 1 :])
    except ValueError:
        return None
    # <br> 转换行、剥掉其余标签、反转义 HTML 实体
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    import html as _html_mod

    text = _html_mod.unescape(text).strip()
    if len(text) < MIN_CONTENT_LEN:
        return None
    text = _clean_markdown(text)
    return text[:MAX_CONTENT_LEN] if len(text) >= MIN_CONTENT_LEN else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_page_content(
    url: str, sem: asyncio.Semaphore | None = None
) -> tuple[str | None, bool]:
    """Fetch clean Markdown content for a URL.

    sem 为并发闸；单 URL 调用方（如 /api/fetch）可不传，内部兜底为容量 1。

    Returns (content, robots_allowed):
      - content       : Markdown string, or None if fetch failed
      - robots_allowed: False means robots.txt disallows this URL —
                        caller should mask the source URL in the response
                        but may still show the content to the user.

    Pipeline (fast-path first):
      0. Redis content cache (1h TTL)
      1. robots.txt + trafilatura run in PARALLEL — saves the robots wait time;
         trafilatura also returns the raw HTML for the next step
      2. readability-lxml — extracts from the same HTML body (no extra HTTP fetch)
      3. Crawl4AI — Playwright fallback for JS-rendered / anti-bot pages
    """
    # 已知抓不动的域名（如 weixin.sogou.com 跳转壳必进验证码墙）直接判失败，
    # 不浪费时间/预算——省下的 content budget 留给其他 URL。
    if _is_skip_fetch_host(url):
        logger.info("fetch.skip known-unfetchable url=%s", url)
        return None, True

    # SSRF guard（async 版内部把阻塞 DNS 卸到线程池，避免 N 个 URL 串行 DNS 冻结 loop、
    # 让 collect_page_fetches 的整体超时失效）
    try:
        await validate_fetch_url_async(url)
    except Exception:
        logger.warning("fetch blocked by SSRF guard: %s", url)
        return None, True

    # 0. Redis content cache
    cache_key = "content:" + hashlib.sha1(url.encode()).hexdigest()
    r = get_redis()
    cached = await r.get(cache_key)
    if cached:
        logger.debug("fetch.cache_hit url=%s", url)
        return cached, True  # cached content was already vetted

    if sem is None:
        sem = asyncio.Semaphore(1)
    async with sem:
        # 1. robots.txt check + trafilatura fetch in parallel.
        #    robots.txt is Redis-cached 24h (usually <1ms); T1 domains skip entirely.
        #    We fetch regardless and let the caller mask the URL if disallowed —
        #    content is still useful to the user.
        robots_task = asyncio.create_task(_is_allowed_by_robots(url))
        trafilatura_task = asyncio.create_task(_fetch_trafilatura(url))
        robots_allowed, (content, html, reason) = await asyncio.gather(
            robots_task, trafilatura_task
        )

        if content and len(content) >= MIN_CONTENT_LEN and not _looks_blocked(content):
            logger.debug("fetch.trafilatura ok url=%s len=%d robots=%s",
                         url, len(content), robots_allowed)
            await r.setex(cache_key, CONTENT_CACHE_TTL, content)
            return content, robots_allowed

        # 2. trafilatura extracted nothing → try readability on the same HTML body
        #    (no extra HTTP fetch — html was already downloaded above).
        #    readability-lxml is also sync CPU-bound, so offload to a thread.
        if html:
            content = await _run_parse(_extract_readability, html)
            if content and len(content) >= MIN_CONTENT_LEN and not _looks_blocked(content):
                logger.debug("fetch.readability ok url=%s len=%d robots=%s",
                             url, len(content), robots_allowed)
                await r.setex(cache_key, CONTENT_CACHE_TTL, content)
                return content, robots_allowed

        # 2.5 baidu dtlandingsuper 落地页：正文在内嵌 JSON 里，静态抽取器都够不着
        if html and _bare_host(url) == "mbd.baidu.com":
            content = await _run_parse(_extract_baidu_dtlandingsuper, html)
            if content and not _looks_blocked(content):
                logger.debug("fetch.baidu_dt ok url=%s len=%d robots=%s",
                             url, len(content), robots_allowed)
                await r.setex(cache_key, CONTENT_CACHE_TTL, content)
                return content, robots_allowed

        # 3. Static extraction failed → JS-rendered page, try Crawl4AI
        content = await _fetch_crawl4ai(url)
        if content and len(content) >= MIN_CONTENT_LEN and not _looks_blocked(content):
            logger.debug("fetch.crawl4ai ok url=%s len=%d robots=%s",
                         url, len(content), robots_allowed)
            await r.setex(cache_key, CONTENT_CACHE_TTL, content)
            return content, robots_allowed

        if not reason:
            reason = "extract_empty+crawl4ai_fail" if html else "download+crawl4ai_fail"
        logger.info("fetch.failed url=%s reason=%s", url, reason)
        return None, robots_allowed


async def fetch_pages_parallel(
    urls: list[str], timeout: float = 4.0
) -> dict[str, tuple[str | None, bool]]:
    """Fetch all URLs concurrently with an overall timeout.

    Returns {url: (content, robots_allowed)}.
    robots_allowed=False means the URL should be masked in the response,
    but content can still be shown to the user.
    """
    if not urls:
        return {}
    tasks = start_page_fetches(urls)
    return await collect_page_fetches(tasks, urls, timeout)


def start_page_fetches(urls: list[str]) -> dict[str, asyncio.Task]:
    """Kick off prefetch tasks without awaiting them. Returns {url: Task}.

    Use with collect_page_fetches when the caller only needs a subset of the
    URLs (e.g. orchestrator prefetches all candidates, then awaits only the
    ranked top-N).
    """
    if not urls:
        return {}
    sem = asyncio.Semaphore(_fetch_concurrency(len(urls)))
    return {url: asyncio.create_task(fetch_page_content(url, sem)) for url in urls}


async def collect_page_fetches(
    tasks: dict[str, asyncio.Task],
    keep_urls: list[str],
    timeout: float,
) -> dict[str, tuple[str | None, bool]]:
    """Await only `keep_urls` from `tasks`; cancel the rest immediately.

    URLs in keep_urls without a prefetched task get a fresh fetch task spun up,
    so callers can safely include URLs that weren't prefetched.
    """
    keep_set = set(keep_urls)
    for url, task in tasks.items():
        if url not in keep_set and not task.done():
            task.cancel()

    new_urls = [u for u in keep_urls if u not in tasks]
    if new_urls:
        sem = asyncio.Semaphore(_fetch_concurrency(len(new_urls)))
        for url in new_urls:
            tasks[url] = asyncio.create_task(fetch_page_content(url, sem))

    keep_tasks = {u: tasks[u] for u in keep_urls if u in tasks}
    if not keep_tasks:
        return {}

    done, pending = await asyncio.wait(keep_tasks.values(), timeout=timeout)
    for t in pending:
        _keep_background_fetch(t)

    out: dict[str, tuple[str | None, bool]] = {}
    for url, task in keep_tasks.items():
        if task in done:
            try:
                out[url] = task.result()
            except Exception:
                out[url] = (None, True)
        else:
            out[url] = (None, True)

    if pending:
        logger.warning(
            "fetch.collect timeout after %.1fs, %d/%d pending",
            timeout, len(pending), len(keep_urls),
        )
    return out


async def stream_page_fetches(
    urls: list[str], timeout: float
) -> AsyncIterator[tuple[str, str | None, bool]]:
    """并发抓取所有 URL，每抓完一个就 yield (url, content, robots_allowed)。

    整体墙钟受 timeout 约束，超时后未完成的任务被取消。供 SSE 流式使用：
    SERP 先返回、正文逐条推送，连接全程有数据流动，避免被客户端/网络中间层
    按"静默超时"掐断（这正是同步抓正文撞超时的根因）。
    """
    if not urls:
        return
    sem = asyncio.Semaphore(_fetch_concurrency(len(urls)))

    async def _one(u: str) -> tuple[str, str | None, bool]:
        content, robots_allowed = await fetch_page_content(u, sem)
        return u, content, robots_allowed

    tasks = [asyncio.create_task(_one(u)) for u in urls]
    done_count = 0
    try:
        for fut in asyncio.as_completed(tasks, timeout=timeout):
            url, content, robots_allowed = await fut
            done_count += 1
            yield url, content, robots_allowed
    except asyncio.TimeoutError:
        logger.warning(
            "fetch.stream timeout after %.1fs, %d/%d done",
            timeout, done_count, len(urls),
        )
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
