"""Rule-based commercial_score computation (0.0 – 1.0).

No ML required. Scores are used by the ranker to penalise ads/spam
and populate SearchResult.tags with human-readable labels.

配置从 Redis 加载，存于 SET 类型 key（每条 member 是 JSON）：
  commercial:rules:domain        → SET，每条 {id, weight, data_type:"tag",     patterns:"a,b,c"}
  commercial:rules:path          → SET，每条 {id, weight, data_type:"regex",   patterns:"<regex>"}
  commercial:rules:title         → SET，每条 {id, weight, data_type:"regex",   patterns:"<regex>"}
  commercial:rules:snippet       → SET，每条 {id, weight, data_type:"regex",   patterns:"<regex>"}
  commercial:rules:seo_farm      → SET，每条 {id, weight, data_type:"regex",   patterns:"<regex>"}
  commercial:rules:tag_threshold → SET，每条 {id, weight, data_type:"literal", patterns:"<text>"}

data_type 决定消费端怎么解析 patterns：
  tag     — 逗号 split → 多个字面量并集，in 判断（domain 还支持 endswith 子域名）
  regex   — 整条当作合法正则 re.search
  literal — 整条当作单字面量 patterns in text

评分逻辑：每种信号类型的 max weight 叠加，最终分数封顶 1.0。
启动时调用 load_commercial_config_from_redis() 加载配置。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import tldextract as _tldextract

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default values (used when Redis data is missing)
# ---------------------------------------------------------------------------

_DEFAULT_DOMAIN_RULES: list[dict[str, Any]] = [
    {
        "weight": 0.25,
        "data_type": "tag",
        "patterns": (
            "jd.com,tmall.com,taobao.com,pinduoduo.com,suning.com,"
            "amazon.com,amazon.cn,ebay.com,aliexpress.com,shopify.com,"
            "shopee.com,lazada.com"
        ),
    },
]

_DEFAULT_PATH_RULES: list[dict[str, Any]] = [
    {
        "weight": 0.25,
        "data_type": "regex",
        "patterns": r"/(?:product|shop|buy|cart|checkout|order|price|goods|item|store|purchase|deal|offer|coupon|discount|promo|sale)(?:/|$|\?)",
    },
]

_DEFAULT_TITLE_RULES: list[dict[str, Any]] = [
    {
        "weight": 0.25,
        "data_type": "regex",
        "patterns": (
            r"购买|立即下单|优惠|折扣|特价|促销|限时|免费试用|抢购|秒杀|"
            r"加盟|代理|招商|报价|多少钱|哪家好|哪里买|最便宜|"
            r"\b(?:buy now|order now|free trial|limited offer|discount|coupon"
            r"|deal|sale|shop now|get it|click here)\b"
        ),
    },
]

_DEFAULT_SNIPPET_RULES: list[dict[str, Any]] = [
    {
        "weight": 0.15,
        "data_type": "regex",
        "patterns": (
            r"立即|马上|点击|扫码|加微信|联系我们|在线咨询|获取报价|免费领取|"
            r"\b(?:click|tap|get started|sign up free|try for free|contact us"
            r"|learn more →|buy now|add to cart)\b"
        ),
    },
]

_DEFAULT_SEO_FARM_RULES: list[dict[str, Any]] = [
    {
        "weight": 0.10,
        "data_type": "regex",
        "patterns": r"排名|十大|最好|最佳|推荐|评测|对比|测评|盘点|\btop \d+\b|\bbest \d+\b|\d+ best\b",
    },
]

_DEFAULT_TAG_THRESHOLDS: list[dict[str, Any]] = [
    {"data_type": "literal", "patterns": "含推广信息", "weight": 0.25},
    {"data_type": "literal", "patterns": "商业内容", "weight": 0.5},
    {"data_type": "literal", "patterns": "推广", "weight": 0.8},
]


# ---------------------------------------------------------------------------
# Compiled rule (data_type-aware matcher)
# ---------------------------------------------------------------------------

class _CompiledRule:
    """预编译的单条规则，data_type 决定 match / domain_match 的语义。"""

    __slots__ = ("weight", "data_type", "_regex", "_tag_items", "_literal_text")

    def __init__(
        self,
        weight: float,
        data_type: str,
        *,
        regex: re.Pattern | None = None,
        tag_items: list[str] | None = None,
        literal_text: str | None = None,
    ) -> None:
        self.weight = weight
        self.data_type = data_type
        self._regex = regex
        self._tag_items = tag_items
        self._literal_text = literal_text

    def match(self, text: str) -> bool:
        if self.data_type == "regex":
            return self._regex.search(text) is not None
        if self.data_type == "tag":
            return any(item in text for item in self._tag_items)
        if self.data_type == "literal":
            return self._literal_text in text
        return False

    def domain_match(self, domain: str) -> bool:
        """对 domain 匹配；tag/literal 额外支持 endswith 子域名（item.jd.com 命中 jd.com）。"""
        if self.data_type == "regex":
            return self._regex.search(domain) is not None
        if self.data_type == "tag":
            for item in self._tag_items:
                if domain == item or domain.endswith("." + item):
                    return True
            return False
        if self.data_type == "literal":
            return self._literal_text == domain or domain.endswith("." + self._literal_text)
        return False


def _compile_one_rule(rule: dict[str, Any]) -> _CompiledRule | None:
    """编译单条规则为 _CompiledRule。非法记录返回 None 并 log warning（容错：跳过该条）。"""
    try:
        weight = float(rule.get("weight", 0.0))
        data_type = (rule.get("data_type") or "").lower()
        patterns = rule.get("patterns", "")
    except Exception as exc:
        logger.warning("commercial: failed to read rule fields: %s | %r", exc, rule)
        return None

    if not isinstance(patterns, str) or not patterns:
        logger.warning("commercial: rule missing/non-str patterns, skipped: %r", rule)
        return None

    if data_type == "regex":
        try:
            compiled = re.compile(patterns, re.IGNORECASE)
        except re.error as exc:
            logger.warning("commercial: bad regex '%s': %s", patterns[:30], exc)
            return None
        return _CompiledRule(weight, "regex", regex=compiled)
    if data_type == "tag":
        items = [p.strip() for p in patterns.split(",") if p.strip()]
        if not items:
            logger.warning("commercial: empty tag patterns, skipped: %r", rule)
            return None
        return _CompiledRule(weight, "tag", tag_items=items)
    if data_type == "literal":
        return _CompiledRule(weight, "literal", literal_text=patterns)

    logger.warning("commercial: unknown data_type '%s', skipped: %r", data_type, rule)
    return None


# ---------------------------------------------------------------------------
# Runtime-loaded config (from Redis)
# ---------------------------------------------------------------------------

_COMPILED_DOMAIN: list[_CompiledRule] = []
_COMPILED_PATH: list[_CompiledRule] = []
_COMPILED_TITLE: list[_CompiledRule] = []
_COMPILED_SNIPPET: list[_CompiledRule] = []
_COMPILED_SEO_FARM: list[_CompiledRule] = []
_COMPILED_TAG_THRESHOLDS: list[_CompiledRule] = []

# 导出给 aigc.py 使用的合并正则：所有 snippet 中 data_type=regex 的 patterns 拼成大正则
_SNIPPET_CTA_RE: re.Pattern | None = None


def _init_defaults() -> None:
    """初始化默认值（同步版本，模块导入时调用一次）。"""
    global _COMPILED_DOMAIN, _COMPILED_PATH, _COMPILED_TITLE, _COMPILED_SNIPPET, _COMPILED_SEO_FARM
    global _COMPILED_TAG_THRESHOLDS
    global _SNIPPET_CTA_RE

    _COMPILED_DOMAIN = [r for r in (_compile_one_rule(x) for x in _DEFAULT_DOMAIN_RULES) if r]
    _COMPILED_PATH = [r for r in (_compile_one_rule(x) for x in _DEFAULT_PATH_RULES) if r]
    _COMPILED_TITLE = [r for r in (_compile_one_rule(x) for x in _DEFAULT_TITLE_RULES) if r]
    _COMPILED_SNIPPET = [r for r in (_compile_one_rule(x) for x in _DEFAULT_SNIPPET_RULES) if r]
    _COMPILED_SEO_FARM = [r for r in (_compile_one_rule(x) for x in _DEFAULT_SEO_FARM_RULES) if r]
    _COMPILED_TAG_THRESHOLDS = [r for r in (_compile_one_rule(x) for x in _DEFAULT_TAG_THRESHOLDS) if r]

    _SNIPPET_CTA_RE = _compile_combined_re(_COMPILED_SNIPPET)


def _compile_combined_re(rules: list[_CompiledRule]) -> re.Pattern | None:
    """合并所有 data_type=regex 的 rules 为单一正则（供 aigc.py finditer 使用）。"""
    patterns: list[str] = []
    for rule in rules:
        if rule.data_type == "regex" and rule._regex is not None:
            patterns.append(rule._regex.pattern)
    if not patterns:
        return None
    combined = "|".join(patterns)
    try:
        return re.compile(combined, re.IGNORECASE)
    except re.error as exc:
        logger.warning("commercial: failed to compile combined pattern: %s", exc)
        return None


# 初始化默认值（启动时立即可用；Redis 加载完成后会被覆盖）
_init_defaults()


async def load_commercial_config_from_redis() -> None:
    """从 Redis 加载商业评分配置（SET 模式，每条 record 是新格式 JSON）。"""
    global _COMPILED_DOMAIN, _COMPILED_PATH, _COMPILED_TITLE, _COMPILED_SNIPPET, _COMPILED_SEO_FARM
    global _COMPILED_TAG_THRESHOLDS
    global _SNIPPET_CTA_RE

    import asyncio

    from app.core.redis import get_redis

    r = get_redis()
    PREFIX = "commercial:rules:"

    (
        domain_members,
        path_members,
        title_members,
        snippet_members,
        seo_farm_members,
        threshold_members,
    ) = await asyncio.gather(
        r.smembers(f"{PREFIX}domain"),
        r.smembers(f"{PREFIX}path"),
        r.smembers(f"{PREFIX}title"),
        r.smembers(f"{PREFIX}snippet"),
        r.smembers(f"{PREFIX}seo_farm"),
        r.smembers(f"{PREFIX}tag_threshold"),
    )

    _COMPILED_DOMAIN = _compile_records(domain_members, _DEFAULT_DOMAIN_RULES)
    _COMPILED_PATH = _compile_records(path_members, _DEFAULT_PATH_RULES)
    _COMPILED_TITLE = _compile_records(title_members, _DEFAULT_TITLE_RULES)
    _COMPILED_SNIPPET = _compile_records(snippet_members, _DEFAULT_SNIPPET_RULES)
    _COMPILED_SEO_FARM = _compile_records(seo_farm_members, _DEFAULT_SEO_FARM_RULES)
    _COMPILED_TAG_THRESHOLDS = _compile_records(threshold_members, _DEFAULT_TAG_THRESHOLDS)

    _SNIPPET_CTA_RE = _compile_combined_re(_COMPILED_SNIPPET)

    logger.info(
        "commercial: loaded domain=%d, path=%d, title=%d, "
        "snippet=%d, seo_farm=%d, tag_thresholds=%d",
        len(_COMPILED_DOMAIN),
        len(_COMPILED_PATH),
        len(_COMPILED_TITLE),
        len(_COMPILED_SNIPPET),
        len(_COMPILED_SEO_FARM),
        len(_COMPILED_TAG_THRESHOLDS),
    )


def _compile_records(
    members: set[bytes | str],
    defaults: list[dict[str, Any]],
) -> list[_CompiledRule]:
    """解析 SET 中的每条 JSON record 并编译。空集或全部非法时回退到 defaults。"""
    compiled: list[_CompiledRule] = []
    for member in members:
        try:
            data = json.loads(member.decode() if isinstance(member, bytes) else member)
        except Exception as exc:
            logger.warning("commercial: failed to JSON-decode member: %s", exc)
            continue
        if not isinstance(data, dict):
            logger.warning("commercial: member is not a JSON object, skipped: %r", data)
            continue
        rule = _compile_one_rule(data)
        if rule is not None:
            compiled.append(rule)
    if not compiled:
        return [r for r in (_compile_one_rule(x) for x in defaults) if r]
    return compiled


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_commercial_score(url: str, title: str, snippet: str) -> float:
    """Return a float in [0, 1] estimating how 'commercial' this result is.

    评分逻辑：每种信号类型的 max weight 叠加，最终分数封顶 1.0。
    """
    score = 0.0

    domain = _extract_domain(url)
    path = urlparse(url).path

    # Signal 1: known e-commerce domain
    score += _match_max_weight(domain, _COMPILED_DOMAIN, use_domain_match=True)

    # Signal 2: commercial URL path keywords
    score += _match_max_weight(path, _COMPILED_PATH)

    # Signal 3: title contains sales/push keywords
    score += _match_max_weight(title, _COMPILED_TITLE)

    # Signal 4: snippet contains CTA phrases
    score += _match_max_weight(snippet, _COMPILED_SNIPPET)

    # Signal 5: SEO-farm title patterns
    score += _match_max_weight(title, _COMPILED_SEO_FARM)

    return min(score, 1.0)


def _match_max_weight(
    text: str,
    rules: list[_CompiledRule],
    *,
    use_domain_match: bool = False,
) -> float:
    """返回所有命中 rule 中 weight 的最大值。"""
    max_weight = 0.0
    for rule in rules:
        hit = rule.domain_match(text) if use_domain_match else rule.match(text)
        if hit and rule.weight > max_weight:
            max_weight = rule.weight
    return max_weight


def commercial_tags(score: float) -> list[str]:
    """Return human-readable tags for a given commercial_score.

    tag_threshold 按 weight 降序遍历，取首个 score >= weight 的 record，
    返回其 patterns 字段（单标签，与原 min_score 区间语义一致）。
    """
    rules = _COMPILED_TAG_THRESHOLDS
    if not rules:
        rules = [r for r in (_compile_one_rule(x) for x in _DEFAULT_TAG_THRESHOLDS) if r]
    for rule in sorted(rules, key=lambda r: r.weight, reverse=True):
        if score >= rule.weight:
            return [rule._literal_text or ""]
    return []


def _extract_domain(url: str) -> str:
    try:
        ext = _tldextract.extract(url)
        return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain
    except Exception:
        return urlparse(url).hostname or ""
