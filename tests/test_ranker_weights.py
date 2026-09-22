"""动态方差重分配 + embedding 归一化的纯函数单测（不依赖 redis/jieba/模型）。"""
from __future__ import annotations

from app.services.ranker import _redistribute_weights, _score_floor_keep, _stddev

# auto 7 因子原始权重
_AUTO = {"rec": 0.25, "trust": 0.15, "bm25": 0.15,
         "rep": 0.15, "sess": 0.10, "emb": 0.15, "comm": 0.05}


def _const(v: float, n: int = 4) -> list[float]:
    return [v] * n


def _varied(n: int = 4) -> list[float]:
    return [i / (n - 1) for i in range(n)]  # 0..1，有方差


def test_stddev_constant_is_zero():
    assert _stddev([0.5, 0.5, 0.5]) == 0.0
    assert _stddev([1.0]) == 0.0          # 单样本
    assert _stddev([]) == 0.0             # 空
    assert _stddev([0.0, 1.0]) > 0.0


def test_weights_unchanged_when_all_factors_vary():
    """所有相关性因子都有区分度时，权重不动。"""
    score_v = {k: _varied() for k in ("rec", "trust", "bm25", "rep", "sess", "comm", "emb")}
    out = _redistribute_weights(_AUTO, score_v)
    assert out == _AUTO


def test_dead_factors_donate_to_alive_proportionally():
    """当天时效 + 冷启动：rec/rep/sess/comm 常数，权重摊给 trust/bm25/emb。"""
    score_v = {
        "rec": _const(1.0),    # 当天，全 1.0
        "rep": _const(0.5),    # 冷启动
        "sess": _const(0.5),   # 冷启动
        "trust": _varied(),
        "bm25": _varied(),
        "emb": _varied(),
        "comm": _const(1.0),   # 无广告，commercial 常数
    }
    out = _redistribute_weights(_AUTO, score_v)

    # 死因子权重归零
    assert out["rec"] == 0.0
    assert out["rep"] == 0.0
    assert out["sess"] == 0.0
    assert out["comm"] == 0.0
    # 死权重 = 0.25+0.15+0.10+0.05 = 0.55；存活原权重和 = 0.45；三者等权各得 1/3
    for k in ("trust", "bm25", "emb"):
        assert abs(out[k] - (0.15 + 0.55 / 3)) < 1e-9
    # 权重守恒
    assert abs(sum(out.values()) - 1.0) < 1e-9
    # 相关性（bm25+emb）从 0.30 抬到约 0.667
    assert out["bm25"] + out["emb"] > 0.66


def test_commercial_contributes_but_never_receives():
    """commercial 有方差（混入广告）时保留自身权重，但不接收死权重。"""
    score_v = {
        "rec": _const(1.0),       # 死
        "rep": _const(0.5),       # 死
        "sess": _const(0.5),      # 死
        "trust": _varied(),
        "bm25": _varied(),
        "emb": _varied(),
        "comm": _varied(),        # commercial 有方差（混入广告）
    }
    out = _redistribute_weights(_AUTO, score_v)

    assert out["comm"] == 0.05  # 保留原值，未被抬高
    # 死权重 = rec+rep+sess = 0.50，摊给 trust/bm25/emb
    for k in ("trust", "bm25", "emb"):
        assert abs(out[k] - (0.15 + 0.50 / 3)) < 1e-9
    assert abs(sum(out.values()) - 1.0) < 1e-9


def test_no_alive_receivers_leaves_weights_untouched():
    """极端：所有接收因子都是常数 → 不重分配（避免除零），原样返回。"""
    score_v = {k: _const(0.5) for k in ("rec", "trust", "bm25", "rep", "sess", "comm", "emb")}
    out = _redistribute_weights(_AUTO, score_v)
    assert out == _AUTO


# ── _score_floor_keep（精排相对断层闸门）──────────────────────────
# 数据取自线上 trace 72793a31dc03（云南7日游 query）：好结果 0.72、跑题塌在 0.54。
_YUNNAN_RR = [0.726, 0.721, 0.719, 0.556, 0.545, 0.541, 0.541, 0.540, 0.540, 0.539]


def test_margin_cuts_offtopic_cluster():
    """相对断层切开 0.72 好结果与 0.54 跑题簇——只留 3 条真相关。"""
    assert _score_floor_keep(_YUNNAN_RR, floor=0.25, margin=0.15) == 3


def test_absolute_floor_alone_keeps_everything():
    """旧行为复现：固定 0.25 绝对地板咬不住压缩在 0.54 的跑题页。"""
    assert _score_floor_keep(_YUNNAN_RR, floor=0.25, margin=0.0) == len(_YUNNAN_RR)


def test_keep_at_least_three_guard():
    """整批偏低且彼此接近：相对断层不误杀清零，保底 3 条。"""
    low_flat = [0.30, 0.29, 0.28, 0.10, 0.09]  # 顶部 0.30，断层只在尾部
    assert _score_floor_keep(low_flat, floor=0.25, margin=0.15) == 3


def test_all_good_and_close_kept():
    """整批都好且接近：margin 内全部保留，不误伤。"""
    all_good = [0.80, 0.78, 0.76, 0.74, 0.72]
    assert _score_floor_keep(all_good, floor=0.25, margin=0.15) == 5


def test_both_gates_off_keeps_all():
    assert _score_floor_keep(_YUNNAN_RR, floor=0.0, margin=0.0) == len(_YUNNAN_RR)


def test_empty_returns_zero():
    assert _score_floor_keep([], floor=0.25, margin=0.15) == 0


def test_floor_dominates_when_higher_than_margin_cut():
    """floor 比相对断层更高时由 floor 主导（真垃圾地板）。"""
    rr = [0.9, 0.85, 0.2, 0.15, 0.1]  # top-margin=0.75；floor=0.8 更狠，只留 ≥0.8 的 2 条
    assert _score_floor_keep(rr, floor=0.8, margin=0.15) == 2


# ── 塌缩闸门（reranker 整批偏低 = 召回失败，别交付一池跑题页）────────────────
# 数据取自线上 trace ca741a6bc256（"一年标准回归年精确天数"）：整批 sigmoid 塌在 0.5-0.61，
# floor/margin 咬不住 → 修复前 dropped=0（10 条跑题页全交付）。
_COLLAPSE_RR = [0.611, 0.597, 0.509, 0.503, 0.502, 0.501, 0.5, 0.5, 0.5, 0.5]


def test_collapse_gate_keeps_only_top_few():
    """整批最高 sigmoid < ceiling（召回失败）：只留 collapse_keep 条，不交付 10 条跑题页。"""
    assert _score_floor_keep(
        _COLLAPSE_RR, floor=0.25, margin=0.15,
        collapse_ceiling=0.65, collapse_keep=2,
    ) == 2


def test_collapse_gate_not_triggered_for_good_batch():
    """正常相关批（头部 >ceiling）不触发塌缩，照常走 margin 相对断层。"""
    assert _score_floor_keep(
        _YUNNAN_RR, floor=0.25, margin=0.15,
        collapse_ceiling=0.65, collapse_keep=2,
    ) == 3


def test_collapse_gate_off_by_default():
    """collapse_ceiling=0（默认关闭）：复现修复前线上行为——10 条跑题页全留（dropped=0）。"""
    assert _score_floor_keep(_COLLAPSE_RR, floor=0.25, margin=0.15) == len(_COLLAPSE_RR)
