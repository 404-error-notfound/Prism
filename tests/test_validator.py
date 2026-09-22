"""Tests for input validation."""
import pytest
from fastapi import HTTPException

from app.core.validator import (
    detect_temporal_query,
    validate_query,
    validate_recency_days,
    validate_top_n,
    validate_user_id,
)


# ── detect_temporal_query ──────────────────────────────────────
@pytest.mark.parametrize("q", [
    "今天的股市行情",
    "比亚迪销量最新消息",        # 最新 + 非参考名词 → 时效
    "latest AI news",
    "刚刚发生的地震",
    "近几天的新闻",
])
def test_temporal_true(q):
    assert detect_temporal_query(q) is True


@pytest.mark.parametrize("q", [
    "2026年最新个人所得税税率表及速算扣除数",   # 最新+税率表 → 版本义，非时效
    "现行公司法全文",                          # 现行+法/全文
    "国家医保局最新异地就医直接结算政策",        # 最新+政策
    "最新国标 GB 8624",                        # 最新+标准/国标
    "Python asyncio 教程",                     # 无时效词
])
def test_temporal_false_reference(q):
    assert detect_temporal_query(q) is False


def test_validate_query_normal():
    assert validate_query("  Python asyncio  ") == "Python asyncio"


def test_validate_query_empty():
    with pytest.raises(HTTPException) as exc:
        validate_query("   ")
    assert exc.value.status_code == 422


def test_validate_query_too_long():
    with pytest.raises(HTTPException):
        validate_query("x" * 513)


def test_validate_query_strips_controls():
    result = validate_query("hello\x00world")
    assert "\x00" not in result


def test_validate_top_n_valid():
    assert validate_top_n(10) == 10
    assert validate_top_n(1) == 1
    assert validate_top_n(50) == 50


def test_validate_top_n_invalid():
    with pytest.raises(HTTPException):
        validate_top_n(0)
    with pytest.raises(HTTPException):
        validate_top_n(51)


def test_validate_recency_days():
    assert validate_recency_days(7) == 7
    with pytest.raises(HTTPException):
        validate_recency_days(0)


def test_validate_user_id_valid():
    assert validate_user_id("user_123") == "user_123"
    assert validate_user_id("") == ""


def test_validate_user_id_invalid():
    with pytest.raises(HTTPException):
        validate_user_id("user<script>")
