"""车身类型判定（**离线**）。

锁三件事：
1. **规则确定性** —— 同输入两次判定必须一致（整表重算幂等的前提）。
2. **金标案例** —— 全部用**库内真实 base_model 键**（`GLC300` 不是 `GLC`）。
   手搓 `base_model='GLC'` 会让集合实现的 bug 测试假绿、线上照错。
3. **字典自洽** —— 值只能是 7 码；排除表引用的车系必须在字典里有条目
   （否则 R1 让行后落进留白，`GLE53 COUPE` 变「未分类」，比标错更糟）。
"""

from __future__ import annotations

import pytest

from carinfo.search import body_types as bt

# ---------------------------------------------------------------------------
# 1. 确定性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("car_model,base_model", [
    ("GLE53 COUPE AMG", "GLE53"),
    ("COROLLA TOURING HYBRID WXB", "COROLLA"),
    ("A4 40TFSI SLINE AVANT", "A4"),
    ("E200 AVANTGARDE", "E200"),
    ("", None),
])
def test_deterministic(car_model, base_model):
    """同输入两次判定完全一致 —— 整表重算「同输入同输出」的硬前提。"""
    first = bt.classify(car_model, base_model, "MERCEDES-BENZ")
    second = bt.classify(car_model, base_model, "MERCEDES-BENZ")
    assert first == second


def test_unknown_is_blank():
    """判不了就留白，绝不猜。"""
    assert bt.classify("XYZ UNKNOWN THING", "XYZ", None) == (None, None)
    assert bt.classify(None, None, None) == (None, None)


# ---------------------------------------------------------------------------
# 2. 溜背 SUV 金标（v2 最大误判源，116 台）—— 必须用库内真实键
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("car_model", "base_model"), [
    ("GLC300 COUPE", "GLC300"),
    ("GLC250 COUPE AMG", "GLC250"),
    ("GLC43 COUPE", "GLC43"),
    ("GLE53 COUPE AMG", "GLE53"),
    ("GLE450 COUPE", "GLE450"),
    ("CAYENNE COUPE", "CAYENNE"),
    ("X6 COUPE", "X6"),
])
def test_coupe_lets_sliding_suv_pass(car_model, base_model):
    """COUPE 泛用词必须对溜背 SUV 让行，并落到 R3 → SUV。

    ⚠️ 这是 set 实现 vs 前缀实现的分水岭：库里没有裸 `GLC`/`GLE` 键，
    按集合写 `{CAYENNE, GLC, …}` 时这些全都会**漏排除**、被判成 COUPE。
    """
    assert bt.classify(car_model, base_model, "MERCEDES-BENZ") == (bt.SUV, "series")
    assert bt.classify("CAYENNE COUPE", "CAYENNE", "PORSCHE") == (bt.SUV, "series")


@pytest.mark.parametrize(("car_model", "base_model"), [
    ("Q5 SPORTBACK", "Q5"),
    ("Q3 SPORTBACK 35 TFSI", "Q3"),
    ("Q8 SPORTBACK", "Q8"),
])
def test_sportback_lets_suv_pass(car_model, base_model):
    assert bt.classify(car_model, base_model, "AUDI") == (bt.SUV, "series")


def test_dirty_base_model_prefix_does_not_leak():
    """手搓一个裸 `GLC` 键也不该把车判成 COUPE —— 前缀匹配天然覆盖它。"""
    assert bt.classify("GLC COUPE", "GLC", None)[0] == bt.SUV or \
        bt.classify("GLC COUPE", "GLC", None)[0] is None


# ---------------------------------------------------------------------------
# 3. 词边界与排除表配套项
# ---------------------------------------------------------------------------


def test_avant_needs_word_boundary():
    """真 AVANT 是旅行車；奔驰配置等级 AVANTGARDE 不是（87 台假阳性）。"""
    assert bt.classify("A4 40TFSI SLINE AVANT", "A4", "AUDI") == (bt.WAGON, "rule")
    assert bt.classify("RS6 AVANT QUATTRO", "RS6", "AUDI") == (bt.WAGON, "rule")
    assert bt.classify("E200 AVANTGARDE", "E200", "MERCEDES-BENZ")[0] != bt.WAGON
    assert bt.classify("V260 AVANTGARDE LONG", "V260", "MERCEDES-BENZ")[0] != bt.WAGON


def test_touring_lets_porsche_pass():
    """`992 GT3 TOURING` 是去尾翼版 911（跑車），不是旅行車。

    ⚠️ 让行之后必须落到 R3 有条目 —— 否则就成「未分类」，比标错更糟。
    """
    assert bt.classify("992 GT3 TOURING", "992", "PORSCHE") == (bt.COUPE, "series")
    assert bt.classify("COROLLA TOURING HYBRID WXB", "COROLLA", "TOYOTA") == (bt.WAGON, "rule")


def test_cross_lets_non_suv_pass():
    """CROSS 让行名单：JAZZ CROSSTAR 是掀背加高、CROWN CROSSOVER 是轿车。"""
    assert bt.classify("JAZZ CROSSTAR", "JAZZ", "HONDA") == (bt.HATCHBACK, "series")
    assert bt.classify("FREED HYBRID CROSSTAR GB7", "FREED", "HONDA") == (bt.MPV, "series")
    assert bt.classify("CROWN CROSSOVER", "CROWN", "TOYOTA") == (bt.SEDAN, "series")
    assert bt.classify("TAYCAN 4 CROSS TURISMO", "TAYCAN", "PORSCHE") == (bt.WAGON, "rule")
    assert bt.classify("CROSSFIRE", "CROSSFIRE", "CHRYSLER") == (bt.COUPE, "series")
    # 这几个判 SUV 是**正确**的，不该被让行名单误伤
    assert bt.classify("CROSSTREK 2.0IS", "CROSSTREK", "SUBARU") == (bt.SUV, "rule")
    assert bt.classify("YARIS CROSS HYBRID Z", "YARIS", "TOYOTA") == (bt.SUV, "rule")
    assert bt.classify("ECLIPSE CROSS", "ECLIPSE", "MITSUBISHI") == (bt.SUV, "rule")
    assert bt.classify("COROLLA CROSS HYBRID", "COROLLA", "TOYOTA") == (bt.SUV, "rule")


# ---------------------------------------------------------------------------
# 4. TYPE R 口径（拍板：COUPE 大口袋）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("car_model", "base_model"), [
    ("CIVIC TYPE R FL5", "CIVIC"),
    ("CIVIC TYPE R FL5 6MT", "FL5"),
    ("CIVIC FK8 TYPE R", "FK8"),
    ("CIVIC EK9 TYPE R", "CIVIC"),
])
def test_type_r_is_coupe(car_model, base_model):
    assert bt.classify(car_model, base_model, "HONDA") == (bt.COUPE, "rule")


def test_civic_plain_is_blank_and_fk8_hatch():
    """CIVIC 裸名留白（歧义不标）；FK7/FK8 无 TYPE R 时是掀背。"""
    assert bt.classify("CIVIC FK7", "CIVIC", "HONDA") == (bt.HATCHBACK, "rule")
    assert bt.classify("CIVIC", "CIVIC", "HONDA") == (None, None)


# ---------------------------------------------------------------------------
# 5. R2 车系限定词
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("car_model", "base_model", "expected"), [
    ("218I ACTIVE TOURER", "218I", bt.MPV),
    ("220IA GRAN TOURER", "220IA", bt.MPV),
    ("218I GRAN COUPE M SPORT", "218I", bt.COUPE),
    ("GR SUPRA RZ", "GR", bt.COUPE),
    ("GR YARIS RZ HIGH PERFORMANCE", "GR", bt.HATCHBACK),
    ("PRIUS V", "PRIUS", bt.WAGON),
    ("PRIUS 1.8 HYBRID", "PRIUS", bt.HATCHBACK),
    ("IONIQ 5 AWD", "IONIQ", bt.SUV),
    ("IONIQ 6 RANGE PLUS AWD", "IONIQ", bt.SEDAN),
    ("CONTINENTAL FLYING SPUR W12", "CONTINENTAL", bt.SEDAN),
    ("CONTINENTAL GTC W12", "CONTINENTAL", bt.CONVERTIBLE),
    ("CONTINENTAL GT W12", "CONTINENTAL", bt.COUPE),
    ("COUNTRYMAN S", "COUNTRYMAN", bt.SUV),
    ("MINI COOPER CLUBMAN", "MINI", bt.WAGON),
    ("MINI COOPER S CONVERTIBLE", "MINI", bt.CONVERTIBLE),
    ("GOLF SPORTSVAN 230 TSI LIFE", "GOLF", bt.MPV),
    ("HIACE", "HIACE", bt.MPV),
])
def test_golden_rows(car_model, base_model, expected):
    assert bt.classify(car_model, base_model, None)[0] == expected


def test_single_char_series_not_confused():
    """`RANGE ROVER SPORT` 是 SUV —— `SPORT` 刻意不做泛用词。"""
    assert bt.classify("RANGE ROVER SPORT P400", "RANGE", "LAND ROVER") == (bt.SUV, "series")


def test_cab_needs_word_boundary():
    """⚠️ `CAB` 是香港的敞篷写法，但 `WELCAB`（轮椅车）里嵌着 CAB。

    实测 ALPHARD/VELLYFIRE/NOAH/VOXY/FREED 的福祉车版本共 50+ 台，子串匹配
    会把整批 MPV 判成開篷車 —— 必须词边界。
    """
    assert bt.classify("428I CAB M SPORT", "428I", "BMW") == (bt.CONVERTIBLE, "rule")
    assert bt.classify("E250 CAB", "E250", "MERCEDES-BENZ") == (bt.CONVERTIBLE, "rule")
    assert bt.classify("CARRERA S CAB", "911", "PORSCHE") == (bt.CONVERTIBLE, "rule")
    for car_model, base in [("ALPHARD 3.5 GF WELCAB", "ALPHARD"),
                            ("NOAH WELCAB", "NOAH"),
                            ("FREED WELCAB", "FREED"),
                            ("VELLFIRE 3.5 WELCAB", "VELLFIRE")]:
        assert bt.classify(car_model, base, "TOYOTA") == (bt.MPV, "series"), car_model


def test_spider_and_shooting_brake():
    assert bt.classify("458 SPIDER", "458", "FERRARI") == (bt.CONVERTIBLE, "rule")
    assert bt.classify("570S SPIDER", "570S", "MCLAREN") == (bt.CONVERTIBLE, "rule")
    assert bt.classify("CLA250 SHOOTING BRAKE", "CLA250", "MERCEDES-BENZ") == (bt.WAGON, "rule")


def test_van_word_not_collected():
    """实测 `car_model ~ 'VAN'` 的命中全是 AVANTGARDE/LEVANTE/SPORTSVAN 假阳性
    —— 收 VAN 会污染一大片，方案不收是对的。"""
    words = {w for w, *_ in bt._R1_RULE_DEFS}
    assert "VAN" not in words


# ---------------------------------------------------------------------------
# 6. 字典自洽（结构断言，不连库）
# ---------------------------------------------------------------------------


def test_dict_values_are_valid_codes():
    bad = {k: v for k, v in bt.SERIES_DICT.items() if v not in bt.VALID_BODY_TYPES}
    assert not bad, f"字典里有非法码：{bad}"


def test_codes_are_seven():
    """体系是 7 码（VAN 已撤销）。"""
    assert len(bt.VALID_BODY_TYPES) == 7
    assert "VAN" not in bt.VALID_BODY_TYPES


def test_label_tables_cover_all_codes():
    assert set(bt.LABELS_HK) == set(bt.VALID_BODY_TYPES)
    assert set(bt.LABELS_CN) == set(bt.VALID_BODY_TYPES)
    assert bt.LABELS_HK[bt.COUPE] == "跑車"          # 拍板：不是「轎跑」
    assert "掲" not in bt.LABELS_HK[bt.HATCHBACK]     # 正字「揭背」，不用异体


def test_exclusion_companions_all_in_dict():
    """排除表引用的车系必须在 R3 字典里有条目。

    否则 R1 让行之后落到 R4 留白 —— 用户搜「SUV」看不到 `GLE53 COUPE`，
    这比标错更难排查。
    """
    missing = bt._EXCLUSION_COMPANIONS - set(bt.SERIES_DICT)
    assert not missing, f"排除表引用了但字典里没有：{sorted(missing)}"


def test_ambiguous_series_not_in_dict():
    """歧义车系刻意不标 —— 标了会误伤裸名。"""
    overlap = bt.AMBIGUOUS_SERIES & set(bt.SERIES_DICT)
    assert not overlap, f"歧义车系不该进字典：{sorted(overlap)}"


def test_dict_values_unique_by_construction():
    """每个 base_model 只映射到一个码（字典键唯一）。"""
    assert len(bt.SERIES_DICT) == len(set(bt.SERIES_DICT))


def test_van_words_not_in_r1():
    """「客貨車」不收 —— 它在 MODEL_ALIASES 里已是 HIACE 车系别名，收了两头打架。"""
    words = {w for w, *_ in bt._R1_RULE_DEFS}
    assert "客貨車" not in words and "客货车" not in words and "VAN" not in words


def test_dead_words_removed():
    """CABRIOLET / VARIANT 全库 0 台，已删；SPORTSVAN 双拼写都收。"""
    words = {w for w, *_ in bt._R1_RULE_DEFS}
    assert "CABRIOLET" not in words
    assert "VARIANT" not in words
    assert {"SPORTSVAN", "SPORTVAN"} <= words
