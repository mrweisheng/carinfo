"""SearchSpec 值收敛与白名单（**离线**）。

模型输出的**键**和**值**都不可信：白名单挡键，`_coerce_*` 挡值。这里锁住
`keyword_is_identity` 的三态收敛与「不暴露给模型」的边界。
"""

from __future__ import annotations

import pytest

from carinfo.search.parser import ALLOWED_LLM_FIELDS
from carinfo.search.spec import SearchSpec


@pytest.mark.parametrize(("raw", "expected"), [
    ("false", False), ("0", False), ("no", False),
    ("true", True), ("1", True), ("yes", True),
    (False, False), (True, True),
    (None, None), ("乱写", None),
])
def test_keyword_is_identity_three_state(raw, expected):
    assert SearchSpec.from_dict({"keyword_is_identity": raw}).keyword_is_identity is expected


def test_keyword_is_identity_absent_is_none():
    assert SearchSpec.from_dict({}).keyword_is_identity is None


def test_keyword_is_identity_not_exposed_to_llm():
    """程序注入字段，**不进** LLM 白名单 —— 模型不能自己声称「这是身份码」。"""
    assert "keyword_is_identity" not in ALLOWED_LLM_FIELDS


def test_from_dict_drops_unknown_keys():
    s = SearchSpec.from_dict({"brand": "BMW", "order_by": "price", "raw_sql": "DROP"})
    assert s.brand == "BMW"
    assert not hasattr(s, "order_by")
    assert not hasattr(s, "raw_sql")


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({"base_model": "X"}, True),
    ({"brand": "BMW"}, True),
    ({"model_keyword": "740"}, True),
    ({"seats": 7, "price_max": 5e5}, False),
])
def test_has_model_target(kwargs, expected):
    assert SearchSpec(raw_query="q", **kwargs).has_model_target is expected


def test_bad_values_coerced_to_none():
    """脏值不能穿透到 SQL 参数（否则 PostgreSQL 抛 InvalidTextRepresentation → 500）。"""
    s = SearchSpec.from_dict({"seats": "七座", "year_min": "2015年", "price_max": "五十万"})
    assert s.seats is None
    assert s.year_min is None
    assert s.price_max is None


def test_numeric_strings_coerced():
    s = SearchSpec.from_dict({"seats": "7", "year_min": "2015", "price_max": "500,000"})
    assert s.seats == 7
    assert s.year_min == 2015
    assert s.price_max == 500000
