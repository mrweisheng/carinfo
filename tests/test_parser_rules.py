"""解析层回归（**离线**：合成 ctx + FakeLLM，不连库不联网）。

锁住两个 P0 的解析成因，以及 Fix-1/2/2b/4/6 与「库外型号形状补救」：

- `宝马740 2018年` / `2016到2017年的宝马740`：'740' 必须以**身份关键词**留在 spec
  （丢了就是全系列宝马顶包 —— 实测 229/321 台）；
- 规则路径的两位键（M3/X5/A6）不能被 `len<3` 一刀切掉；
- 排量写法（'3000cc'）**不得**被词表松匹配误提升为型号；
- 库外型号（'M760'）按「字母+数字」形状补成身份关键词，与 LLM 路径一致。
"""

from __future__ import annotations

import pytest

from carinfo.search.parser import parse_query, rule_based_parse


# ---------------------------------------------------------------------------
# 规则路径
# ---------------------------------------------------------------------------
def test_bmw740_keeps_identity_keyword(synth_ctx):
    s = rule_based_parse("宝马740 2018年", synth_ctx)
    assert s.model_keyword == "740"          # Fix-2b：规则层也得产出关键词
    assert s.keyword_is_identity is True
    assert (s.year_min, s.year_max) == (2018, 2018)


def test_bmw740_year_range_kept(synth_ctx):
    s = rule_based_parse("2016到2017年的宝马740", synth_ctx)
    assert s.model_keyword == "740"
    assert (s.year_min, s.year_max) == (2016, 2017)   # 区间必须表达出来，不能塌成单年


@pytest.mark.parametrize(("query", "expected"), [
    ("宝马M3", "M3"), ("宝马X5", "X5"), ("奥迪A6", "A6"),
])
def test_two_char_keys_recognised(synth_ctx, query, expected):
    # Fix-4：len<3 → len<2，两位真实键不再被跳过
    assert rule_based_parse(query, synth_ctx).base_model == expected


def test_brand_only_has_no_false_identity(synth_ctx):
    s = rule_based_parse("最便宜的平治", synth_ctx)
    assert s.base_model is None and s.model_keyword is None
    assert s.brand == "MERCEDES-BENZ"
    assert s.sort == "price_asc"


def test_seats_and_price_extracted(synth_ctx):
    s = rule_based_parse("五十万以内的七座车", synth_ctx)
    assert s.seats == 7
    assert s.price_max == 500000


@pytest.mark.parametrize("query", ["3000cc 宝马", "3000 cc 宝马"])
def test_displacement_not_promoted_to_model(synth_ctx, query):
    """排量写法不是型号：词表松匹配会把 '3000' 命中 HINO 的 '300' 模式，
    必须在 `is_model_code` 之前用 `_QUANTITY_RE` / 单位前瞻拦下。"""
    s = rule_based_parse(query, synth_ctx)
    assert s.model_keyword is None
    assert s.brand == "BMW"


def test_out_of_library_model_shape_promoted(synth_ctx):
    """库外型号（'M760'）按形状补成身份关键词 → 与 LLM 路径一致（诚实 0）。"""
    s = rule_based_parse("宝马M760", synth_ctx)
    assert s.model_keyword == "M760"
    assert s.keyword_is_identity is True


def test_code_shape_needs_short_letters(synth_ctx):
    """字母 >3 的 'SDRIVE18IA' 不匹配型号形状 → 不当作型号（避免误伤）。"""
    s = rule_based_parse("宝马 SDRIVE18IA", synth_ctx)
    assert s.model_keyword is None


def test_compact_key_rescue(synth_ctx):
    """'LM 350' 带空格 → 压平匹配救回 base_model='LM350'（原名键识别过不去）。"""
    assert rule_based_parse("雷克萨斯 LM 350", synth_ctx).base_model == "LM350"


# ---------------------------------------------------------------------------
# FakeLLM 路径
# ---------------------------------------------------------------------------
def test_llm_truncated_keyword_is_dropped(synth_ctx, fake_llm):
    """模型把 'LM350' 截成 '350' → 必须丢弃（否则 '%350%' 会卷进 IS350 等车）。"""
    r = parse_query("雷克萨斯 LM 350", synth_ctx,
                    llm=fake_llm({"model_keyword": "350", "brand": "LEXUS"}))
    assert r.source == "llm"
    assert r.spec.base_model == "LM350"
    assert r.spec.model_keyword is None


def test_llm_full_keyword_is_kept(synth_ctx, fake_llm):
    """'740' 是完整 token（只是库内键的前缀）→ 保留并盖章为身份。"""
    r = parse_query("宝马740 2018年", synth_ctx,
                    llm=fake_llm({"model_keyword": "740", "brand": "BMW"}))
    assert r.spec.model_keyword == "740"
    assert r.spec.keyword_is_identity is True
