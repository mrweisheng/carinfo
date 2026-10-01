"""解释层文案（**离线**）。

锁住自审发现并修复的零结果文案，以及「缺维叙述只认核心六维」这条
（否则每条结果都会多一句「缺松绑补偿数据」的噪声）。
"""

from __future__ import annotations

from carinfo.search.engine import CORE_DIMS, DIM_LABELS, WEIGHTS, ScoredVehicle, SearchResult
from carinfo.search.explain import build_result_dict, explain, item_explain, summarize
from carinfo.search.spec import SearchSpec


def _vehicle(**over) -> ScoredVehicle:
    base = {"vehicle_id": "v1", "car_model": "730LIA G12", "car_brand": "BMW", "year": 2018,
            "price": 200_000.0, "car_url": "http://x", "seats": "5", "engine_volume": "3000"}
    base.update(over)
    return ScoredVehicle(**base)


def _result(spec, items=(), total=0, relaxed=None) -> SearchResult:
    return SearchResult(spec=spec, items=list(items), total_matched=total, scanned=0,
                        elapsed_ms=0, relaxed=list(relaxed or []))


# ---------------------------------------------------------------------------
# 零结果文案（自审修复）
# ---------------------------------------------------------------------------
def test_zero_copy_with_model_keyword():
    s = _result(SearchSpec(raw_query="q", brand="BMW", model_keyword="740"))
    assert summarize(s) == "没有「740」能同时满足你这些条件。"


def test_zero_copy_brand_only():
    s = _result(SearchSpec(raw_query="q", brand="BMW"))
    assert summarize(s) == "没有「BMW」能同时满足你这些条件。"


def test_zero_copy_without_model_target():
    """纯条件筛选：没有名号可点名，也不该拼出「符合『全部车型』条件」的别扭话。"""
    s = _result(SearchSpec(raw_query="q", seats=7, price_max=500_000))
    assert summarize(s) == "库里没有符合条件的车。建议放宽预算或年份。"


def test_zero_copy_never_says_loosen_budget_when_target_known():
    s = _result(SearchSpec(raw_query="q", model_keyword="M760", brand="BMW"))
    assert "建议放宽预算或年份" not in summarize(s)


# ---------------------------------------------------------------------------
# 正常结果文案
# ---------------------------------------------------------------------------
def test_nonzero_copy_mentions_count_and_rank():
    it = _vehicle(price_ratio=0.9, market_median=222_000.0)
    s = _result(SearchSpec(raw_query="q", brand="BMW", model_keyword="740"),
                items=[it], total=6)
    text = summarize(s)
    assert "筛出" in text and "综合分" in text
    assert "HK$" in text


# ---------------------------------------------------------------------------
# target 名号拼装（2026-09-30：补 family + body_type）
# ---------------------------------------------------------------------------
def test_target_includes_family_with_brand():
    """「特斯拉 Model 3」走 family，摘要不能再显示成光秃秃的 TESLA。"""
    from carinfo.search.explain import target_name
    assert target_name(SearchSpec(raw_query="q", brand="TESLA",
                                  family="MODEL 3")) == "TESLA MODEL 3"


def test_target_body_type_when_no_model():
    """只有类型条件时，名号用香港标签（房車/七人車…），不再落到「全部车型」。"""
    from carinfo.search.explain import target_name
    assert target_name(SearchSpec(raw_query="q", body_type="SEDAN")) == "房車"
    assert target_name(SearchSpec(raw_query="q", body_type="MPV")) == "七人車"


def test_target_brand_plus_body_type():
    from carinfo.search.explain import target_name
    assert target_name(SearchSpec(raw_query="q", brand="MERCEDES-BENZ",
                                  body_type="SUV")) == "MERCEDES-BENZ SUV"


def test_target_specific_model_does_not_double_type():
    """已有精确车系/型号时，不再叠类型标签（同义冗余）。"""
    from carinfo.search.explain import target_name
    assert target_name(SearchSpec(raw_query="q", base_model="ALPHARD",
                                  body_type="MPV")) == "ALPHARD"


def test_nonzero_copy_shows_body_type_label():
    it = _vehicle(price_ratio=0.9, market_median=222_000.0)
    s = _result(SearchSpec(raw_query="q", body_type="SUV"), items=[it], total=6)
    assert "SUV" in summarize(s)


# ---------------------------------------------------------------------------
# 缺维叙述 / 明细（核心六维 vs 第七维）
# ---------------------------------------------------------------------------
def test_missing_dim_narrative_excludes_relaxed():
    it = _vehicle(scores={"match": 1.0, "value": 1.0, "fresh": 1.0, "heat": 1.0})
    text = item_explain(it, SearchSpec(raw_query="q"))
    assert "缺" in text and "未计分" in text
    assert "松绑补偿" not in text          # 第七维常态缺席，不许进「缺数据」叙述


def test_core_dims_are_labelled():
    for d in CORE_DIMS:
        assert d in DIM_LABELS


def test_score_breakdown_shows_relaxed_when_present():
    it = _vehicle(scores={"match": 1.0, "relaxed": 0.5}, score=1.2)
    d = build_result_dict(it, SearchSpec(raw_query="q"))
    dims = [b["dim"] for b in d["score_breakdown"]]
    assert "relaxed" in dims               # 「每一分可解释」：第七维也要看得见
    assert all("weight" in b and "label" in b for b in d["score_breakdown"])


def test_score_breakdown_skips_absent_dims():
    it = _vehicle(scores={"match": 1.0})
    d = build_result_dict(it, SearchSpec(raw_query="q"))
    dims = [b["dim"] for b in d["score_breakdown"]]
    assert dims == ["match"]


# ---------------------------------------------------------------------------
# relaxed 透出
# ---------------------------------------------------------------------------
def test_explain_passes_relaxed_through():
    s = _result(SearchSpec(raw_query="q"), relaxed=["import_type"])
    out = explain(s)
    assert out.relaxed == ["import_type"]
    assert isinstance(out.items, list)


def test_score_breakdown_reports_weight_sum():
    """前端按权重和做分母：含 relaxed 时和为 1.08，必须显式给出，别让它猜。"""
    it = _vehicle(scores={"match": 1.0, "relaxed": 0.5}, score=1.2)
    d = build_result_dict(it, SearchSpec(raw_query="q"))
    assert abs(d["weight_sum"] - (WEIGHTS["match"] + WEIGHTS["relaxed"])) < 1e-9
