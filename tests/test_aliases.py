"""车名别名表（**离线**）：拼音/繁简归一、映射构建、解析、生成校验闸。

锁住三件事：
1. 同音错字与繁简差异靠无调拼音兜住（步威↔布威、阿爾法↔阿尔法）；
2. 别名只指向**库内真实存在**的车系键；拼音有歧义时整条剔除（宁可漏不可错）；
3. 生成校验闸挡掉脏值（非纯中文、过长过短、与车系键重名、黑名单、跨车系抢名）。
"""

from __future__ import annotations

from carinfo.search.aliases import (
    _valid_alias,
    normalize_alias,
    to_pinyin,
)
from carinfo.search.context import SearchContext, _build_alias_maps
from carinfo.search.normalize import Vocabulary


def _ctx(models: set[str]) -> SearchContext:
    alias_map, pinyin_map = _build_alias_maps([])
    return SearchContext(Vocabulary(), models, set(), {}, alias_map, pinyin_map)


# ---------------------------------------------------------------------------
# 拼音归一
# ---------------------------------------------------------------------------
def test_pinyin_bridges_homophones_and_traditional():
    assert to_pinyin("步威") == to_pinyin("布威") == "buwei"
    assert to_pinyin("阿爾法") == to_pinyin("阿尔法") == to_pinyin("阿尔发")
    assert to_pinyin("stepwgn") == ""      # 非中文 → 空（不参与拼音匹配）
    assert normalize_alias("  布 威 ") == "布威"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_seed_covers_stepwgn_typo():
    """人工种子必须含「步威→STEPWGN」，否则「布威」的拼音兜底无从谈起。"""
    ctx = _ctx({"STEPWGN"})
    assert ctx.resolve_model_alias("步威") == "STEPWGN"
    assert ctx.resolve_model_alias("布威") == "STEPWGN"      # 同音错字
    assert ctx.resolve_model_alias("斯帝普威") is None        # 未收录，不硬猜


def test_traditional_and_typo_alphard():
    ctx = _ctx({"ALPHARD"})
    assert ctx.resolve_model_alias("阿爾法") == "ALPHARD"
    assert ctx.resolve_model_alias("阿尔发") == "ALPHARD"
    assert ctx.resolve_model_alias("埃尔法") == "ALPHARD"


def test_alias_requires_model_in_library():
    """库内没有 STEPWGN 时，别名不得硬指向它（否则等于凭空造车）。"""
    ctx = _ctx({"ALPHARD"})
    assert ctx.resolve_model_alias("布威") is None


def test_find_model_alias_in_sentence():
    ctx = _ctx({"STEPWGN", "ALPHARD"})
    assert ctx.find_model_alias("我想搵布威七人車") == "STEPWGN"
    assert ctx.find_model_alias("有沒有阿尔发") == "ALPHARD"
    assert ctx.find_model_alias("普通一句話") is None


def test_ambiguous_pinyin_is_dropped():
    """两个车系共用同一拼音 → 该拼音整条剔除，避免指向错误车系。"""
    rows = [("步威", "buwei", "STEPWGN", "seed"), ("布威", "buwei", "SERENA", "llm")]
    alias_map, pinyin_map = _build_alias_maps(rows)
    assert "buwei" not in pinyin_map
    # 精确别名仍可用（各指各的），只是拼音兜底被禁用
    ctx = SearchContext(Vocabulary(), {"STEPWGN", "SERENA"}, set(), {}, alias_map, pinyin_map)
    assert ctx.resolve_model_alias("步威") == "STEPWGN"
    assert ctx.resolve_model_alias("布威") == "SERENA"


# ---------------------------------------------------------------------------
# 生成校验闸
# ---------------------------------------------------------------------------
def test_valid_alias_gates():
    known = {"STEPWGN", "SERENA"}
    assert _valid_alias("步威", "STEPWGN", known, {}) == "步威"
    assert _valid_alias(" 布 威 ", "STEPWGN", known, {}) == "布威"
    assert _valid_alias("A6", "STEPWGN", known, {}) is None        # 含字母
    assert _valid_alias("步威2", "STEPWGN", known, {}) is None      # 含数字
    assert _valid_alias("威", "STEPWGN", known, {}) is None         # 过短
    assert _valid_alias("一二三四五六七八九", "STEPWGN", known, {}) is None  # 过长
    assert _valid_alias("STEPWGN", "STEPWGN", known, {}) is None    # 与车系键重名
    assert _valid_alias("自由", "STEPWGN", known, {}) is None        # 黑名单幻觉词
    assert _valid_alias("步威", "STEPWGN", known, {"步威": "SERENA"}) is None  # 跨车系抢名


def test_seed_present_in_module():
    from carinfo.search.aliases import SEED_ALIASES
    assert SEED_ALIASES.get("步威") == "STEPWGN"


# ---------------------------------------------------------------------------
# LLM 路径：输入里的中文别名优先于模型猜测
# ---------------------------------------------------------------------------
class _FakeLLM:
    configured = True

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, system, user, **kwargs):
        return self.payload


def test_query_alias_overrides_llm_guess(monkeypatch):
    """「找一台本田布威」：M3 会猜成 Honda BR-V，但输入里的「布威」（同音步威）
    是确定性证据，必须归到 STEPWGN。"""
    from carinfo.search.parser import parse_query

    alias_map, pinyin_map = _build_alias_maps([])
    ctx = SearchContext(Vocabulary(), {"STEPWGN", "ALPHARD"}, {"HONDA"}, {}, alias_map, pinyin_map)
    llm = _FakeLLM({"queries": [{"brand": "HONDA", "model_keyword": "BR-V"}]})
    pr = parse_query("找一台本田布威", ctx, llm=llm)
    assert pr.spec.base_model == "STEPWGN"
    assert pr.spec.model_keyword is None


def test_query_alias_not_applied_to_multigroup(monkeypatch):
    """多车混输时不做整句别名覆盖（会把同一个别名错配到别的组）。"""
    from carinfo.search.parser import parse_query

    alias_map, pinyin_map = _build_alias_maps([])
    ctx = SearchContext(Vocabulary(), {"STEPWGN", "ALPHARD", "VELLFIRE"}, set(),
                        {}, alias_map, pinyin_map)
    llm = _FakeLLM({"queries": [{"base_model": "ALPHARD"}, {"base_model": "VELLFIRE"}]})
    pr = parse_query("阿尔法或者威尔法", ctx, llm=llm)
    assert [s.base_model for s in pr.specs] == ["ALPHARD", "VELLFIRE"]
