"""DropTrace 逐结果裁决账本单测（纯函数，不依赖 redis/模型）。"""
from __future__ import annotations

import logging

from app.engines.base import RawResult
from app.services.drop_trace import DropTrace
from app.services.spam_gate import apply_spam_gate


def _r(url: str, title: str = "t", snippet: str = "s") -> RawResult:
    return RawResult(title=title, url=url, snippet=snippet, engine="searxng/baidu")


def test_seed_idempotent_and_kept_default():
    t = DropTrace()
    rs = [_r("http://a"), _r("http://b")]
    t.seed(rs)
    t.seed(rs)  # 二次 seed 不重复
    assert len(t._rec) == 2
    assert all(v["stage"] == "kept" for v in t._rec.values())


def test_drop_precise_reason_and_score():
    t = DropTrace()
    t.seed([_r("http://a")])
    t.drop("http://a", "rerank", "below_floor", score=0.42)
    rec = t._rec["http://a"]
    assert rec["stage"] == "rerank"
    assert rec["reason"] == "below_floor"
    assert rec["score"] == 0.42


def test_drop_vanished_only_touches_kept():
    t = DropTrace()
    t.seed([_r("http://a"), _r("http://b"), _r("http://c")])
    t.drop("http://a", "spam", "lexicon_C")          # a 先被 spam 丢
    t.drop_vanished(["http://b"], "dedup", "dup")     # 只剩 b 存活 → c 归 dedup，a 不被覆盖
    assert t._rec["http://a"]["stage"] == "spam"
    assert t._rec["http://b"]["stage"] == "kept"
    assert t._rec["http://c"]["stage"] == "dedup"


def test_drop_unknown_url_noop():
    t = DropTrace()
    t.seed([_r("http://a")])
    t.drop("http://nonexistent", "spam", "x")  # 不报错、不新增
    assert len(t._rec) == 1


def test_spam_gate_records_into_trace():
    """spam_gate 硬丢时把 URL+原因写进 trace。"""
    t = DropTrace()
    results = [
        _r("http://porn.example/x", title="成人视频在线", snippet="..."),
        _r("https://baijiahao.baidu.com/s?id=1", title="正经新闻", snippet="央行政策解读"),
    ]
    t.seed(results)
    kept, dropped = apply_spam_gate("央行政策", results, trace=t)
    assert dropped == 1
    assert t._rec["http://porn.example/x"]["stage"] == "spam"
    assert t._rec["http://porn.example/x"]["reason"] == "lexicon_C"
    assert t._rec["https://baijiahao.baidu.com/s?id=1"]["stage"] == "kept"


def test_emit_does_not_raise(caplog):
    t = DropTrace()
    t.seed([_r("http://a"), _r("http://b")])
    t.drop("http://b", "coverage", "cov=0.00<0", score=0.0)
    t.mark_survivor_score("http://a", 0.87)
    with caplog.at_level(logging.INFO):
        t.emit(logging.getLogger("test"), "qid123", "测试查询")
    assert any("search.trace" in r.message for r in caplog.records)
