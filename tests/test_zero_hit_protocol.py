"""统一零命中协议（**离线**：patch 掉 search/探针，不连库）。

覆盖放宽阶梯的分支与**「身份永不顶包」不变量**。真库口径的黄金用例见
`test_search_golden_db.py`。
"""

from __future__ import annotations

import pytest

from carinfo.search import engine
from carinfo.search.engine import SearchResult
from carinfo.search.spec import SearchSpec


def _res(spec, total, relaxed=None):
    return SearchResult(spec=spec, items=[], total_matched=total, scanned=0,
                        elapsed_ms=0, relaxed=list(relaxed or []))


@pytest.fixture
def patch(monkeypatch):
    """替换 `search` / 两个探针 / 诚实 0 提示，脚本化控制每步命中数。

    `total_fn(spec, relaxed) -> int` 决定重查命中数；`seen` 记录每步
    `(relaxed, year_min, year_max)`，用于断言阶梯**走了哪几步**。
    """
    def _install(total_fn, exists=None, counts=None):
        seen: list[tuple] = []

        def fake_search(conn, spec, *, relaxed=None, relaxed_source=None):
            seen.append((tuple(relaxed or []), spec.year_min, spec.year_max))
            return _res(spec, total_fn(spec, relaxed), relaxed=relaxed)

        monkeypatch.setattr(engine, "search", fake_search)
        monkeypatch.setattr(engine, "_probe_exists", exists or (lambda conn, s: False))
        monkeypatch.setattr(engine, "_probe_count", counts or (lambda conn, s: None))
        monkeypatch.setattr(engine, "_target_hint_safe", lambda conn, s, kw: ([], None))
        return seen

    return _install


# ---------------------------------------------------------------------------
# Step 1：L3 累积松绑
# ---------------------------------------------------------------------------
def test_single_l3_relaxation(patch):
    seen = patch(lambda spec, relaxed: 0 if not relaxed else 7,
                 exists=lambda conn, s: True)
    spec = SearchSpec(raw_query="q", base_model="LM350", import_type="水貨")
    r, fb = engine.search_with_fallback(None, spec)
    assert r.total_matched == 7
    assert r.relaxed == ["import_type"]                    # 结构化字段透出
    assert seen[:2] == [((), None, None), (("import_type",), None, None)]
    assert len(fb) == 1
    assert "已放宽" in fb[0]
    assert "排在前面" not in fb[0]          # 单条件松绑：这句承诺不可能兑现，不许说


def test_multi_l3_relaxation_accumulates(patch):
    # 第 1 次探针（只丢 import_type）不改善 → 累积到第 2 次（再丢 mileage_max）
    seen = patch(lambda spec, relaxed: 0 if not relaxed else 12,
                 exists=lambda conn, s: s.mileage_max is None)
    spec = SearchSpec(raw_query="q", base_model="LM350",
                      import_type="水貨", mileage_max=50_000)
    r, fb = engine.search_with_fallback(None, spec)
    assert r.total_matched == 12
    assert r.relaxed == ["import_type", "mileage_max"]
    assert seen[1] == (("import_type", "mileage_max"), None, None)
    assert "满足其中更多条件的排在前面" in fb[0]


# ---------------------------------------------------------------------------
# 身份永不顶包
# ---------------------------------------------------------------------------
def test_identity_keyword_never_dropped(patch):
    """只要关键词还在就 0、丢掉关键词才有结果 —— 若协议丢了它，这里会变成 5（顶包）。"""
    patch(lambda spec, relaxed: 5 if spec.model_keyword is None else 0)
    spec = SearchSpec(raw_query="q", model_keyword="740",
                      keyword_is_identity=True, brand="BMW")
    r, fb = engine.search_with_fallback(None, spec)
    assert r.total_matched == 0                    # 宁可诚实 0
    assert any("没有「740」" in n for n in fb)


def test_noise_keyword_may_be_dropped(patch):
    """非身份关键词（中文/'30' 这类）允许丢弃后重查。"""
    patch(lambda spec, relaxed: 4 if spec.model_keyword is None else 0)
    spec = SearchSpec(raw_query="q", model_keyword="保姆車", brand="BMW")
    r, fb = engine.search_with_fallback(None, spec)
    assert r.total_matched == 4
    assert any("已忽略它" in n for n in fb)


def test_identity_year_relax_keeps_identity(patch):
    """身份码在场 + 精确年份零命中 → 只放宽年份 ±3，身份不动。"""
    patch(lambda spec, relaxed: 3 if (spec.year_min, spec.year_max) == (2014, 2020) else 0)
    spec = SearchSpec(raw_query="q", model_keyword="740", keyword_is_identity=True,
                      brand="BMW", year_min=2017, year_max=2017)
    r, fb = engine.search_with_fallback(None, spec)
    assert r.total_matched == 3
    assert any("±3" in n for n in fb)
