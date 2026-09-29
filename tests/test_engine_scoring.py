"""打分层：权重表、第七维门控、身份码判据（**离线**）。

第七维「松绑补偿」有三条不可动摇的性质，全部在这里锁住：
1. `WEIGHTS` 含第七键 `relaxed`，六维之和仍为 1.00（总和 1.08）；
2. `CORE_DIMS` **不含** `relaxed`（否则缺维叙述会给每条结果加噪声）；
3. 单条件松绑时第七维恒缺席（可证明：见 `_score_relaxed` docstring）。
"""

from __future__ import annotations

import pytest

from carinfo.search.engine import (
    CORE_DIMS,
    DISPLACEMENT_MISMATCH_CAP,
    DISPLACEMENT_UNKNOWN_SCORE,
    L3_RELAX_ORDER,
    WEIGHTS,
    _identity_keyword,
    _score_relaxed,
    is_identity_code,
    score_match,
)
from carinfo.search.spec import SearchSpec


def test_weight_table_shape():
    assert WEIGHTS["relaxed"] == 0.08
    # 六维之和仍是 1.00；加上第七维是 1.08（不是 1.0）
    assert abs(sum(WEIGHTS.values()) - 1.08) < 1e-9
    assert abs(sum(WEIGHTS[k] for k in CORE_DIMS) - 1.0) < 1e-9
    # boost 类（near/fresh/relaxed）必须显著低于 match/value
    for k in ("near", "fresh", "relaxed"):
        assert WEIGHTS[k] <= 0.10


def test_core_dims_excludes_relaxed():
    assert "relaxed" not in CORE_DIMS
    assert len(CORE_DIMS) == 6


def test_l3_relax_order_is_coverage_ascending():
    # import_type(32.4%) → mileage_max(44.1%) → hand_max(68.9%)
    assert L3_RELAX_ORDER == ("import_type", "mileage_max", "hand_max")


# ---------------------------------------------------------------------------
# 身份码判据（两边取或）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kw", ["M760", "Z8", "X1", "LM350", "740LI"])
def test_is_identity_code_true(kw):
    assert is_identity_code(kw) is True


@pytest.mark.parametrize("kw", ["30", "740", "2015", "保姆車", None, ""])
def test_is_identity_code_false(kw):
    """**纯数字码一律不是身份码** —— '30' 模糊匹配实测 767 台，放行等于放弃身份约束。
    '740' 要靠解析层用 ctx 盖章（`is_model_code`），不是靠这里。"""
    assert is_identity_code(kw) is False


@pytest.mark.parametrize(("kw", "flag", "expected"), [
    ("740", True, "740"),        # 解析层盖章（库里没有 '740' 这个整键）
    ("M760", None, "M760"),      # engine 兜底：含字母的 ASCII 码
    ("保姆車", None, None),       # 非 ASCII → 噪声，可丢
    ("30", None, None),          # 纯数字 → 噪声，可丢
    (None, None, None),
])
def test_identity_keyword_or_logic(kw, flag, expected):
    spec = SearchSpec(raw_query="q", model_keyword=kw, keyword_is_identity=flag)
    assert _identity_keyword(spec) == expected


# ---------------------------------------------------------------------------
# 第七维门控
# ---------------------------------------------------------------------------
def _relax_spec():
    return SearchSpec(raw_query="q", base_model="LM350",
                      import_type="水貨", mileage_max=50_000, hand_max=1)


@pytest.mark.parametrize("names", [[], ["import_type"], ["mileage_max"]])
def test_score_relaxed_absent_below_two(names):
    """0 或 1 个条件松绑 → 该维缺席（返回 None，不进分母）。"""
    assert _score_relaxed(names, _relax_spec(), {}) is None


def test_score_relaxed_partial_ratio():
    # 2 个条件：import 满足、mileage 不满足 → 0.5
    ef = {"import_type": "水貨", "mileage_km": 90_000}
    assert _score_relaxed(["import_type", "mileage_max"], _relax_spec(), ef) == 0.5


def test_score_relaxed_full_and_zero():
    spec = _relax_spec()
    assert _score_relaxed(["import_type", "mileage_max"], spec,
                          {"import_type": "水貨", "mileage_km": 10_000}) == 1.0
    assert _score_relaxed(["import_type", "mileage_max"], spec,
                          {"import_type": "行貨", "mileage_km": 90_000}) == 0.0


# ---------------------------------------------------------------------------
# 排量偏好（2026-09-29 修复：旧实现 `min(1.0, m+0.10)` 在 base_model 命中时是空操作）
# ---------------------------------------------------------------------------
def _alphard_spec(disp):
    return SearchSpec(raw_query="q", base_model="ALPHARD", displacement=disp)


def test_displacement_preference_orders_matching_first():
    s = _alphard_spec("3.5")
    # 命中（含容差内 3456cc）保持满分
    assert score_match(s, "ALPHARD SA VELLFIRE", "ALPHARD", None, "3500cc") == 1.0
    assert score_match(s, "ALPHARD EXECUTIVE", "ALPHARD", None, "3456cc") == 1.0
    # 不符降 match（仍在结果里，只是排后面）
    assert score_match(s, "ALPHARD 2.5S", "ALPHARD", None, "2500cc") < 1.0


def test_displacement_falls_back_to_car_model_text():
    s = _alphard_spec("3.5")
    assert score_match(s, "ALPHARD 3.5 VELLFIRE", "ALPHARD", None, None) == 1.0
    assert score_match(s, "ALPHARD 2.5", "ALPHARD", None, None) < 1.0


def test_displacement_unknown_not_penalized():
    """缺排量信息不罚 —— 与「缺维不扣分」一致。"""
    s = _alphard_spec("3.5")
    assert score_match(s, "ALPHARD SA VELLFIRE", "ALPHARD", None, None) == 1.0
    assert score_match(s, "ALPHARD 8", "ALPHARD", None, None) == 1.0  # 8 是代次不是排量


def test_no_displacement_spec_never_penalized():
    s = SearchSpec(raw_query="q", base_model="ALPHARD")
    assert score_match(s, "ALPHARD 2.5", "ALPHARD", None, "2500cc") == 1.0

def test_displacement_registration_noise_tolerated():
    """登记排量有噪声：3.5L 常登 3456/3490/3498/3499cc，2.5L 常登 2490 几 —— 都算命中。"""
    s35 = _alphard_spec("3.5")
    for cc in ("3456cc", "3490cc", "3498cc", "3499cc", "3500cc", "3510cc"):
        assert score_match(s35, "ALPHARD", "ALPHARD", None, cc) == 1.0, cc
    s25 = _alphard_spec("2.5")
    for cc in ("2490cc", "2493cc", "2494cc", "2498cc", "2500cc"):
        assert score_match(s25, "ALPHARD", "ALPHARD", None, cc) == 1.0, cc
    # 档位不同仍必须分开（容差远小于档距）
    assert score_match(s35, "ALPHARD", "ALPHARD", None, "2494cc") < 1.0
    assert score_match(s25, "ALPHARD", "ALPHARD", None, "3498cc") < 1.0

def test_displacement_car_model_fallback_requires_decimal():
    """车名里的裸数字是代次/型号（MODEL 3→3、A6→6、740→0.74），不能当排量。"""
    s_model3 = SearchSpec(raw_query="q", base_model="MODEL 3", displacement="3.5")
    assert score_match(s_model3, "MODEL 3", "MODEL 3", None, None) == 1.0
    s_a6 = SearchSpec(raw_query="q", base_model="A6", displacement="2.0")
    assert score_match(s_a6, "A6", "A6", None, None) == 1.0
    # 车名里带小数点(3.5/2.5) 才算排量
    s = _alphard_spec("3.5")
    assert score_match(s, "ALPHARD 3.5", "ALPHARD", None, None) == 1.0
    assert score_match(s, "ALPHARD 2.5", "ALPHARD", None, None) < 1.0

def test_only_displacement_ranks_by_displacement():
    """没有车型目标、只有排量偏好时，也要按排量排序（否则该维恒 None，偏好失效）。"""
    s = SearchSpec(raw_query="q", displacement="3.5")   # 无 base_model/brand/keyword
    assert score_match(s, "SOME CAR", None, None, "3500cc") == 1.0
    assert score_match(s, "SOME CAR", None, None, "2500cc") == DISPLACEMENT_MISMATCH_CAP
    assert score_match(s, "SOME CAR", None, None, None) == DISPLACEMENT_UNKNOWN_SCORE
