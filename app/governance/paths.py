"""治理路径路由 — Fast / Standard / Deep

Fast:     URL 去重 + 黑名单 + 基础排序（跳过 SimHash 去重和商业评分）
          适用：Sites 白名单全为 T1 可信域名、技术类查询
          增量延迟目标：<100ms

Standard: Fast + 完整商业评分 + SimHash 内容去重
          适用：通用查询（默认）
          增量延迟目标：<500ms

Deep:     Standard + AIGC 规则检测
          适用：commercial / news intent（内容质量风险更高）
          增量延迟目标：<2s
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.ranker import _T1_DOMAINS


class GovernancePath(str, Enum):
    FAST     = "fast"
    STANDARD = "standard"
    DEEP     = "deep"


@dataclass(frozen=True)
class PathConfig:
    simhash_dedup:      bool   # 是否执行 SimHash 内容去重
    compute_commercial: bool   # 是否计算 commercial_score / tags
    aigc_check:         bool   # 是否执行 AIGC 规则检测


_CONFIGS: dict[GovernancePath, PathConfig] = {
    GovernancePath.FAST:     PathConfig(simhash_dedup=False, compute_commercial=False, aigc_check=False),
    GovernancePath.STANDARD: PathConfig(simhash_dedup=True,  compute_commercial=True,  aigc_check=False),
    GovernancePath.DEEP:     PathConfig(simhash_dedup=True,  compute_commercial=True,  aigc_check=True),
}



def select_path(
    intent: str,
    sites: str = "",
    default: str = "standard",
) -> GovernancePath:
    """根据 intent 与 Sites 白名单选择治理路径。

    优先级：
    1. Sites 全为 T1 域名              → Fast
    2. intent in (commercial, news)   → Deep
    3. 其余                           → 使用 governance_default_path 配置（默认 standard）
    """
    if sites:
        site_list = {s.strip().lower() for s in sites.split(",") if s.strip()}
        if site_list and site_list.issubset(_T1_DOMAINS):
            return GovernancePath.FAST

    if intent in ("commercial", "news"):
        return GovernancePath.DEEP

    try:
        return GovernancePath(default)
    except ValueError:
        return GovernancePath.STANDARD


def get_config(path: GovernancePath) -> PathConfig:
    return _CONFIGS[path]
