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


# ---------------------------------------------------------------------------
# 规则路径排量（P1-1：无 LLM 时排量偏好也要抽出来）
# ---------------------------------------------------------------------------
def test_rule_displacement_extracted(synth_ctx):
    assert rule_based_parse("18年埃尔法3.5L排量", synth_ctx).displacement == "3.5"
    assert rule_based_parse("宝马 3500cc", synth_ctx).displacement == "3500"
    assert rule_based_parse("排量3000的宝马", synth_ctx).displacement == "3000"


def test_rule_displacement_not_confused_with_price(synth_ctx):
    # 「3.5萬」是预算不是排量
    assert rule_based_parse("3.5萬的阿尔法", synth_ctx).displacement is None
    # 预算与排量同时出现，各归各
    s = rule_based_parse("50萬以內的阿尔法 3.5L", synth_ctx)
    assert s.displacement == "3.5" and s.price_max == 500000


# ---------------------------------------------------------------------------
# 车身类型类目词（规则兜底路径）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("query", "code"), [
    ("越野车", "SUV"), ("越野車", "SUV"), ("吉普", "SUV"), ("SUV", "SUV"),
    ("suv", "SUV"),                                   # ASCII 不区分大小写
    ("七人车", "MPV"), ("七人車", "MPV"), ("MPV", "MPV"),
    ("7人车", "MPV"), ("7人車", "MPV"),               # 阿拉伯数字变体（二审 P1-1）
    ("保姆车", "MPV"), ("商务车", "MPV"),
    ("房车", "SEDAN"), ("房車", "SEDAN"), ("轿车", "SEDAN"), ("轎車", "SEDAN"),
    ("掀背", "HATCHBACK"), ("揭背", "HATCHBACK"), ("两厢", "HATCHBACK"),
    ("旅行车", "WAGON"), ("旅行版", "WAGON"),
    ("开篷", "CONVERTIBLE"), ("開篷", "CONVERTIBLE"), ("敞篷", "CONVERTIBLE"),
    ("跑车", "COUPE"), ("跑車", "COUPE"), ("轿跑", "COUPE"), ("轎跑", "COUPE"),
])
def test_rule_body_type_scan(synth_ctx, query, code):
    assert rule_based_parse(query, synth_ctx).body_type == code


def test_seven_seater_is_body_type_not_seats(synth_ctx):
    """「七人車」是车型类别(MPV)，**不是**座位数 —— 拆分已拍板。"""
    s = rule_based_parse("找台七人车", synth_ctx)
    assert s.body_type == "MPV"
    assert s.seats is None              # 旧行为会 seats=7，把 7 座 SUV 一起卷进来


def test_seat_count_still_works(synth_ctx):
    """「七座」是座位数，与车身类型无关。"""
    s = rule_based_parse("五十万以内的七座车", synth_ctx)
    assert s.seats == 7
    assert s.body_type is None


def test_seven_seat_suv_keeps_both(synth_ctx):
    """「七座的SUV」两个都要：座位数 + 车型，不能互相吞掉。"""
    s = rule_based_parse("七座的SUV", synth_ctx)
    assert s.seats == 7 and s.body_type == "SUV"


def test_offroad_brand_alias_not_hijacked_as_body():
    """「越野路華」(LAND ROVER) 不能被车身词扫成 SUV。

    车身词用的是**带车字后缀**的「越野車/越野车」（方案 §5.4 规则列举如此），
    所以「越野路華」这个品牌别名天然不命中 —— 品牌查询不该被强行加一个车身条件。
    （方案 §5.4 的冲突核对脚注假设的是裸「越野」，与规则列举不一致；此处按
    规则列举实现，避免把品牌名误判成车型条件。）
    """
    from carinfo.search.parser import _scan_body_type
    assert _scan_body_type("越野路華") is None
    assert _scan_body_type("越野路华") is None
    assert _scan_body_type("越野车") == "SUV"


# ---------------------------------------------------------------------------
# LLM 路径：车身类型
# ---------------------------------------------------------------------------
def test_llm_body_type_kept(synth_ctx, fake_llm):
    r = parse_query("15万左右的SUV", synth_ctx,
                    llm=fake_llm({"price_near": 150000, "body_type": "SUV"}))
    assert r.source == "llm"
    assert r.spec.body_type == "SUV"
    assert r.spec.price_near == 150000


def test_llm_body_type_alias_normalised(synth_ctx, fake_llm):
    """模型爱把车型类别写成 body / car_type —— 别名要归一到 body_type，不许静默丢。"""
    r = parse_query("找台七人车", synth_ctx, llm=fake_llm({"body": "MPV"}))
    assert r.spec.body_type == "MPV"


def test_llm_seven_seater_drops_spurious_seats(synth_ctx, fake_llm):
    """prompt 是软约束，代码兜底必须兜住：模型同时给 MPV + seats=7 时丢 seats。"""
    r = parse_query("找台七人车", synth_ctx,
                    llm=fake_llm({"body_type": "MPV", "seats": 7}))
    assert r.spec.body_type == "MPV"
    assert r.spec.seats is None                       # 兜底生效
    assert any("七人車" in n for n in r.notes)        # 且留下说明


def test_llm_seven_seater_seats_kept_when_body_not_mpv(synth_ctx, fake_llm):
    """兜底只针对「七人車→MPV」；原文含「七座」时 seats 必须保留。"""
    r = parse_query("七座的SUV", synth_ctx,
                    llm=fake_llm({"body_type": "SUV", "seats": 7}))
    assert r.spec.body_type == "SUV" and r.spec.seats == 7


def test_llm_no_body_type_when_not_asked(synth_ctx, fake_llm):
    """凭空多条件断言：「找台 14 年威尔法」没提车身，body_type 必须为 None。"""
    r = parse_query("找台14年威尔法", synth_ctx,
                    llm=fake_llm({"base_model": "ALPHARD", "year_min": 2014,
                                  "year_max": 2014}))
    assert r.spec.body_type is None


def test_rule_arabic_7seater(synth_ctx):
    """二审 P1-1 回归：「7人车」（阿拉伯数字）条件曾被整体丢失。"""
    s = rule_based_parse("找台7人车", synth_ctx)
    assert s.body_type == "MPV"
    assert s.seats is None
    s2 = rule_based_parse("7人車", synth_ctx)
    assert s2.body_type == "MPV" and s2.seats is None


def test_llm_seven_seater_forces_mpv_when_model_omits_body(synth_ctx, fake_llm):
    """二审 P1-2：模型漏给 body_type 时兜底必须独立生效——
    旧守卫条件 body_type=='MPV' 不成立 → 静默退化为 seats=7，7 座 SUV 卷进来。"""
    r = parse_query("找台七人车", synth_ctx, llm=fake_llm({"seats": 7}))
    assert r.spec.body_type == "MPV"                  # 原文兜底，不依赖模型
    assert r.spec.seats is None                       # seats 一并丢掉
    assert any("兜底" in n for n in r.notes)


def test_llm_seven_seater_corrects_wrong_body(synth_ctx, fake_llm):
    """原文「七人車」是类别词，模型给错（SEDAN）也要纠正为 MPV。"""
    r = parse_query("找台七人车", synth_ctx, llm=fake_llm({"body_type": "SEDAN"}))
    assert r.spec.body_type == "MPV"
    assert any("兜底" in n for n in r.notes)


# ---------------------------------------------------------------------------
# 同向多目标合并（多目标方案 §4.2）
# ---------------------------------------------------------------------------
def test_llm_same_direction_brand_merge(synth_ctx, fake_llm):
    """「奔驰SUV或者宝马都可以」→ 同向合并单池（brands 数组）。"""
    r = parse_query("15万左右的SUV，奔驰或者宝马的都可以", synth_ctx, llm=fake_llm({
        "queries": [
            {"brand": "MERCEDES-BENZ", "body_type": "SUV", "price_near": 150000},
            {"brand": "BMW", "body_type": "SUV", "price_near": 150000},
        ]}))
    assert len(r.specs) == 1
    assert r.spec.brands == ["BMW", "MERCEDES-BENZ"]
    assert r.spec.body_type == "SUV" and r.spec.price_near == 150000
    assert r.spec.limit == 10                       # 两组额度求和，总量不缩水
    assert any("合并" in n for n in r.notes)


def test_llm_same_direction_series_merge(synth_ctx, fake_llm):
    """「阿尔法、威尔法都可以」→ base_models 单池（引擎既有 ANY 能力）。"""
    r = parse_query("18万左右的宝马M3或者X5都可以", synth_ctx, llm=fake_llm({
        "queries": [
            {"base_model": "M3", "price_near": 180000},
            {"base_model": "X5", "price_near": 180000},
        ]}))
    assert len(r.specs) == 1
    assert r.spec.base_models == ["M3", "X5"]
    assert r.spec.base_model == "M3"


def test_llm_different_direction_stays_multi(synth_ctx, fake_llm):
    """年份不同 = 异向，绝不合并（§4.1 判定标准）。"""
    r = parse_query("14年威尔法，再找台20年埃尔法", synth_ctx, llm=fake_llm({
        "queries": [
            {"base_model": "VELLFIRE", "year_min": 2014, "year_max": 2014},
            {"base_model": "ALPHARD", "year_min": 2020, "year_max": 2020},
        ]}))
    assert len(r.specs) == 2


# ---------------------------------------------------------------------------
# 家族前缀兜底（多目标方案 §4.5：「奔驰 GLC 200」）
# ---------------------------------------------------------------------------
def test_llm_lm300_family_fallback(synth_ctx, fake_llm):
    """复现形态：bm='LM'(非键) + kw='300'(纯数字键) → 必须落到家族 LM，
    不许撞 '300' 垃圾键（HINO 300）。"""
    r = parse_query("雷克萨斯 LM 300", synth_ctx, llm=fake_llm({
        "brand": "LEXUS", "base_model": "LM", "model_keyword": "300"}))
    assert r.spec.base_model is None and r.spec.model_keyword is None
    assert r.spec.family == "LM"
    assert r.spec.brand == "LEXUS"
    assert any("家族" in n for n in r.notes)


def test_rule_lm300_family_fallback(synth_ctx):
    """规则路径同样兜底：'300' 被键扫描命中后由家族守卫接管。"""
    s = rule_based_parse("雷克萨斯 LM 300", synth_ctx)
    assert s.family == "LM"
    assert s.base_model is None


def test_llm_lm350_keyword_path_unaffected(synth_ctx, fake_llm):
    """keyword 正路（'LM350' 是真键）绝不插手 —— 既有 Fix 不许回退。"""
    r = parse_query("雷克萨斯 LM 350", synth_ctx, llm=fake_llm({
        "brand": "LEXUS", "base_model": "LM", "model_keyword": "LM350"}))
    assert r.spec.base_model == "LM350"
    assert r.spec.family is None
