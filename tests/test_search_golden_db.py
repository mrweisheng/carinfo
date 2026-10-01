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
# 快照计数（**下界**不变量）
# ---------------------------------------------------------------------------
# ⚠️ 本地库即线上服务库，正被爬虫**实时写入**，精确计数会随入库漂移
# （实测 MODEL 3 在几分钟内 276→278→279）。用下界锁定「变体合并没退化」这个
# 不变量即可：掉到历史值以下 = 过滤/归一坏了；高于它 = 正常新增车源。
# ⚠️ 这几条锁的是「车型归一/变体合并」不变量，与「默认只出个人车源」（dealer=False）
# 无关 —— 故显式 `dealer=None` 看全量池，否则计数会被车行过滤整体拉低（2026-09-29）。
def test_snapshot_base_model_a6(real_ctx, db_fetch):
    spec = SearchSpec(raw_query="奥迪A6", base_model="A6", dealer=None)
    assert db_fetch(lambda conn: search(conn, spec).total_matched) >= 30


def test_snapshot_base_model_model3(real_ctx, db_fetch):
    spec = SearchSpec(raw_query="特斯拉Model 3", base_model="MODEL 3", dealer=None)
    assert db_fetch(lambda conn: search(conn, spec).total_matched) >= 276


def test_snapshot_bmw740_2018_count(real_ctx, db_fetch):
    s = rule_based_parse("宝马740 2018年", real_ctx)
    s.dealer = None            # 同上：锁车型解析，不看车行过滤
    r, _fb = _run(db_fetch, s)
    assert r.total_matched >= 6


# ---------------------------------------------------------------------------
# 车身类型（2026-09-30 新增）—— 真库口径
# ---------------------------------------------------------------------------
def test_body_type_classified_rate_ge_70pct(db_fetch):
    """可判率 ≥70%（目标 75%）。掉下来 = R3 字典 / R1 规则退化，或重算没跑。

    ⚠️ 这是**数据质量**断言，不是性能断言。表被爬虫实时写入，只锁下界。
    """
    def _q(conn):
        cur = conn.cursor()
        cur.execute("SELECT count(*) FILTER (WHERE body_type IS NOT NULL), "
                    "count(*) FROM vehicle_features")
        return cur.fetchone()

    classified, total = db_fetch(_q)
    assert total > 0, "vehicle_features 是空的 —— features 重算没跑？"
    rate = classified / total
    assert rate >= 0.70, f"可判率仅 {classified}/{total} = {rate:.1%}"


def test_index_vf_body_type_exists_after_rebuild(db_fetch):
    """**防影子表机制回归**：`idx_vf_body_type` 在第 2 次重算后仍必须存在。

    `_shadow_ddl()` 的 `index_map` 是**硬编码白名单**（features.py:580-587）：
    新索引没登记进去的话，影子表建索引时 `IF NOT EXISTS` 撞名 → 索引不建 →
    换名时随旧表 DROP，业务索引带着静默消失（查询不报错、只是全表扫）。
    这条断言专抓那类回归 —— **不是性能断言**（见方案 §四）。
    """
    def _q(conn):
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('idx_vf_body_type')")
        return cur.fetchone()[0]

    assert db_fetch(_q) is not None, "idx_vf_body_type 消失了（影子表 index_map 漏登记？）"


def test_body_type_filter_hits_and_reports_unclassified(db_fetch):
    """端到端：body_type 生效 → 命中集全是该类型，且带未分类数（静默漏检提示）。"""
    spec = SearchSpec(raw_query="SUV", body_type="SUV", limit=10)
    r = db_fetch(lambda conn: search(conn, spec))
    assert r.total_matched > 0
    assert all(it.body_type == "SUV" for it in r.items)
    # 静默漏检断言：body_type 生效时未分类数必须出现（None = 没算，用户看不到漏掉了什么）
    assert r.body_type_unclassified_count is not None
    assert r.body_type_unclassified_count >= 0


@pytest.mark.parametrize(("query", "code"), [
    ("15万左右的SUV", "SUV"),
    ("20万左右的房车", "SEDAN"),
    ("找台七人车", "MPV"),
    ("七座的SUV", "SUV"),
])
def test_body_type_full_chain_rule_path(real_ctx, db_fetch, query, code):
    """4 条全链路（规则路径）：解析 → SQL → 真数据。"""
    s = rule_based_parse(query, real_ctx)
    assert s.body_type == code, f"{query!r} → {s.body_type!r}（应 {code}）"
    r, _fb = _run(db_fetch, s)
    assert r.total_matched > 0, f"{query!r} 命中 0 台（类型条件把候选砍空了？）"
    assert all(it.body_type == code for it in r.items)


def test_seven_seater_chain_drops_seats(real_ctx, db_fetch):
    """七人車兜底断言（全链路）：body_type=MPV 且 seats 必须为 None。"""
    s = rule_based_parse("找台七人车", real_ctx)
    assert s.body_type == "MPV"
    assert s.seats is None
