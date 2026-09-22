"""逐结果丢弃追踪：记录每条结果在哪一层被丢、为什么、分数多少。

orchestrator 在召回后 seed 全量；各过滤层（dedup/denylist/spam/coverage/rerank/sites/
auth）把丢弃的 URL 连同原因记进来；末尾 emit 一条 `search.trace`（INFO，结构化）——
一处看清整条流水线对每条结果的裁决，省掉"哪层把这条丢了/为什么对不上"的人肉排查。

设计：按 URL 主键（over-fetch 同 URL 只留一条）。两种归因路径，按"原因能否事后还原"分：
  - spam/coverage/rerank：丢弃原因（词典C/cov 数值/塌缩 vs 地板）set-diff 取不回，
    必须在决策处调 drop() 现场记——这是 trace 参数穿进这三层的唯一理由。
  - dedup/denylist/sites/auth：层名即原因，**不传 trace**，由 orchestrator 用
    drop_vanished() 做集合差归因。勿"顺手"把 trace 也穿进这几层。
各 filter 的 trace 参数均可选（默认 None 为空操作），不影响现有行为与单测。
"""
from __future__ import annotations

import logging
from collections import Counter

from app.engines.base import RawResult


class DropTrace:
    """一次查询的逐结果裁决账本。"""

    __slots__ = ("_rec",)

    def __init__(self) -> None:
        # url -> {url, title, snippet, engine, stage, reason, score}
        self._rec: dict[str, dict] = {}

    def seed(self, results: list[RawResult]) -> None:
        """登记新进入流水线的结果（idempotent：已登记的 URL 跳过）。

        url/title/snippet 全量存（不截断）：这些记录直落 ES、给 Kibana 查全/看全，
        截断在那里没有意义。snippet 是召回阶段的摘要（正文要 NeedContent 抓完才有）。
        """
        for r in results:
            if r.url and r.url not in self._rec:
                self._rec[r.url] = {
                    "url": r.url,
                    "title": r.title or "",
                    "snippet": r.snippet or "",
                    "engine": r.engine or "",
                    "stage": "kept",
                    "reason": "",
                    "score": None,
                }

    def drop(self, url: str, stage: str, reason: str = "", score: float | None = None) -> None:
        """精确记一条被某层丢弃，附原因/分数（spam/coverage/rerank 用）。"""
        rec = self._rec.get(url)
        if rec is None:
            return
        rec["stage"] = stage
        rec["reason"] = reason
        if score is not None:
            rec["score"] = round(float(score), 4)

    def drop_vanished(self, surviving_urls, stage: str, reason: str = "") -> None:
        """上一步还在、这一步消失的 URL 归到本层（dedup/denylist/sites/auth 用）。

        只动当前仍标 kept 的记录——已被更早的层丢弃的不覆盖。
        """
        alive = set(surviving_urls)
        for url, rec in self._rec.items():
            if rec["stage"] == "kept" and url not in alive:
                rec["stage"] = stage
                rec["reason"] = reason

    def mark_survivor_score(self, url: str, score: float | None) -> None:
        """给最终存活的结果补上排序综合分。"""
        rec = self._rec.get(url)
        if rec is not None and rec["stage"] == "kept" and score is not None:
            rec["score"] = round(float(score), 4)

    def records(self) -> list[dict]:
        """逐条裁决记录：存活在前（按分降序），丢弃在后（按层分组）。

        供 ES 落库（save_drop_trace）与 emit 复用——一次查询对每条结果的最终裁决。
        """
        return sorted(
            self._rec.values(),
            key=lambda r: (r["stage"] != "kept", r["stage"], -(r["score"] or 0.0)),
        )

    def emit(self, logger: logging.Logger, query_id: str, query: str) -> None:
        """输出一条 search.trace 聚合日志（message + metrics.drops 计数概览）。

        只给 stdout/趋势看"这次每层丢了几条"；逐条明细（每 URL 在哪层被丢）以 ES
        索引 prism_drop_trace 为准（save_drop_trace 落库，Kibana 可查）。逐条明细不
        塞进这条日志的 extra——formatter 白名单不含该键，进不了 ES，纯属死负载。
        """
        counts = Counter(r["stage"] for r in self._rec.values())
        drops = {k: v for k, v in counts.items() if k != "kept"}
        logger.info(
            "search.trace query_id=%s query=%r kept=%d drops=%s",
            query_id, query[:50], counts.get("kept", 0), drops,
            extra={
                "event": {"category": "pipeline", "type": "trace"},
                "query_id": query_id,
                "metrics": {"kept": counts.get("kept", 0), "drops": dict(drops)},
            },
        )
