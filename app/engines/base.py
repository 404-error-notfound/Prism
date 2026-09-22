"""Base class for all search engine adapters."""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RawResult:
    """Minimal raw result returned by any engine before governance."""
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    engine: str = ""
    extra: dict = field(default_factory=dict)


class BaseEngine(abc.ABC):
    name: str = "base"
    #: 聚合多个子源的元引擎（如 SearXNG）：结果 engine 字段带子源名（name/sub），
    #: 编排层据此做子源分布统计；按能力声明而非名字前缀识别。
    meta_engine: bool = False
    #: 经代理池出网的引擎：熔断时信号 proxy-hub 抽新 IP（见 orchestrator._record_failure）。
    #: 引擎自身声明，编排层扫描派生清单，避免维护名字枚举。
    proxy_dependent: bool = False

    @abc.abstractmethod
    async def search(self, query: str, num: int = 20, **kwargs) -> list[RawResult]:
        """Return up to *num* raw results for *query*."""

    async def health_check(self) -> bool:
        """Return True if engine is reachable. Override if needed."""
        return True


class ProxyQualityError(Exception):
    """代理质量失败：引擎 HTTP 层成功（200）但实质无数据——出口 IP 被目标站
    风控/弹验证码，子引擎大面积 unresponsive（proxy error / captcha）。

    与超时/网络错误区分开：编排层捕获后记熔断（触发 proxy-hub 刷新）并延长
    重试预算换新 IP 重试，而不是当作"该 query 真的没有结果"。
    """
