"""真库黄金用例（`@pytest.mark.db`，**无库自动 skip**）。

这是两个 P0 的**端到端**锁：解析 → SQL → 真数据。断言以**不变量**为主
（如「命中集全部含 740」），精确计数另标「快照」。

⚠️ 快照计数会随爬虫入库而变化 —— 失效时先确认是不是数据真的变了，再更新数字；
**不要**为了让测试变绿而放松不变量。
"""

from __future__ import annotations

import pytest

from carinfo.search.engine import search, search_with_fallback
from carinfo.search.parser import parse_query, rule_based_parse
from carinfo.search.spec import SearchSpec

pytestmark = pytest.mark.db


def _run(db_fetch, spec):
    return db_fetch(lambda conn: search_with_fallback(conn, spec))


# ---------------------------------------------------------------------------
# P0 案发现场
# ---------------------------------------------------------------------------
def test_bmw740_2018_no_top_package(real_ctx, db_fetch):
    """案发现场①：曾返回 229 台全系列宝马（X3/X5 顶包）。"""
    s = rule_based_parse("宝马740 2018年", real_ctx)
    r, _fb = _run(db_fetch, s)
    assert r.total_matched > 0
    assert all("740" in (it.car_model or "") for it in r.items), "顶包：命中了不含 740 的车"


def test_bmw740_year_range_is_honest_zero(real_ctx, db_fetch):
    """案发现场②：'2016到2017年的宝马740' 区间不得被 ±1 放宽成顶包。"""
    s = rule_based_parse("2016到2017年的宝马740", real_ctx)
    r, fb = _run(db_fetch, s)
    assert r.total_matched == 0
    assert any("740" in n for n in fb), "诚实 0 必须给「库里 740 有什么」的数字提示"


def test_lm350_hit_set_is_not_polluted(real_ctx, db_fetch):
    """案发现场③：'LM350' 命中集只能是 LM350 / LM350H（混动变体），不得卷入 LM500。"""
    s = rule_based_parse("雷克萨斯LM350", real_ctx)
    r, _fb = _run(db_fetch, s)
    assert r.total_matched > 0
    for it in r.items:
        assert (it.car_model or "").upper().startswith("LM350"), it.car_model


def test_m760_rule_path_is_honest_zero(real_ctx, db_fetch):
    """库外型号：规则路径现在也诚实 0（此前退化成品牌级 1,959 台）。"""
    s = rule_based_parse("宝马M760", real_ctx)
    assert s.model_keyword == "M760"
    r, _fb = _run(db_fetch, s)
    assert r.total_matched == 0


def test_m760_llm_path_is_honest_zero(real_ctx, db_fetch, fake_llm):
    parsed = parse_query("宝马M760", real_ctx,
                         llm=fake_llm({"model_keyword": "M760", "brand": "BMW"}))
    r, _fb = _run(db_fetch, parsed.spec)
    assert r.total_matched == 0


# ---------------------------------------------------------------------------
# 排量写法（本轮自审修复）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["3000cc 宝马", "3000 cc 宝马"])
def test_displacement_query_not_locked_out(real_ctx, db_fetch, query):
    s = rule_based_parse(query, real_ctx)
    assert s.model_keyword is None
    r, _fb = _run(db_fetch, s)
    assert r.total_matched > 0, "排量被当成车型 → 静默 0 台（误导）"


# ---------------------------------------------------------------------------
# 快照计数（数据变化时更新；不要放松不变量）
# ---------------------------------------------------------------------------
def test_snapshot_base_model_a6(real_ctx, db_fetch):
    spec = SearchSpec(raw_query="奥迪A6", base_model="A6")
    assert db_fetch(lambda conn: search(conn, spec).total_matched) == 30


def test_snapshot_base_model_model3(real_ctx, db_fetch):
    spec = SearchSpec(raw_query="特斯拉Model 3", base_model="MODEL 3")
    assert db_fetch(lambda conn: search(conn, spec).total_matched) == 276


def test_snapshot_bmw740_2018_count(real_ctx, db_fetch):
    s = rule_based_parse("宝马740 2018年", real_ctx)
    r, _fb = _run(db_fetch, s)
    assert r.total_matched == 6
