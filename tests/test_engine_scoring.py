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
    L3_RELAX_ORDER,
    WEIGHTS,
    _identity_keyword,
    _score_relaxed,
    is_identity_code,
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
