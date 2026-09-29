"""两个 P0 的 **SQL 形状锁**（离线）。

顶包 = 丢掉了用户明说的车型约束。在 SQL 层，正确行为是：`car_model ILIKE '%740%'`
这条过滤**必须存在**。不连库也能据此证明「没有顶包」。
"""

from __future__ import annotations

from carinfo.search.engine import _where_clause
from carinfo.search.parser import rule_based_parse


def test_bmw740_identity_filter_present(synth_ctx):
    s = rule_based_parse("宝马740 2018年", synth_ctx)
    where, params = _where_clause(s)
    assert "v.car_model ILIKE %s" in where        # 身份约束没被丢
    assert "%740%" in params
    assert "2018" in params                        # 年份也锁住


def test_bmw740_year_range_has_both_bounds(synth_ctx):
    s = rule_based_parse("2016到2017年的宝马740", synth_ctx)
    _where, params = _where_clause(s)
    assert "%740%" in params
    assert "2016" in params and "2017" in params   # 区间两端都在，没塌成单年


def test_displacement_is_not_a_model_filter(synth_ctx):
    """排量写法不该产出车型过滤；品牌过滤仍在。"""
    s = rule_based_parse("3000cc 宝马", synth_ctx)
    where, params = _where_clause(s)
    assert "v.car_model ILIKE" not in where
    assert "BMW" in params
    assert not any(str(p).startswith("%3000") for p in params)


def test_out_of_library_model_has_identity_filter(synth_ctx):
    """'M760' 是库外型号，但形状补救后仍须产出身份过滤（→ 诚实 0，不顶包）。"""
    s = rule_based_parse("宝马M760", synth_ctx)
    where, params = _where_clause(s)
    assert "v.car_model ILIKE %s" in where
    assert "%M760%" in params


def test_lock_has_teeth(synth_ctx):
    """**反面用例**：把身份关键词抹掉后，SQL 里就没有 '%740%' 过滤了 —— 证明
    上面几条断言确有区分度（顶包发生时它们会红）。"""
    s = rule_based_parse("宝马740 2018年", synth_ctx)
    assert "%740%" in _where_clause(s)[1]        # 前提：正常路径有身份过滤
    s.model_keyword = None
    s.keyword_is_identity = None
    where, params = _where_clause(s)
    assert "v.car_model ILIKE" not in where
    assert "%740%" not in params                  # 顶包形态：身份过滤消失
