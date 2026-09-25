"""特征层：把原始 vehicles 行算成「行情基准」+「单车特征」两张表。

设计要点（血泪换来的，别随手改）：

1. **两套键**（用户明确要求）
   - 匹配键（宽）：`base_model` —— 搜「阿尔法」要能搜出 ALPHARD / ALPHARD 3.5 /
     ALPHARD 3.5 M，所以匹配只用车系，不带年份/排量。
   - 行情键（细）：`(base_model, year_bucket)` —— 价格基准必须同类同代才可比。
     实测 ALPHARD 按年代差 10 倍（2023+ 中位 59.8 万 vs 2008-2014 中位 5.8 万），
     而排量只差 21%（2.5 中位 38.6 万 vs 3.5 中位 31.8 万，2.5 反而更贵，因为
     新款都是 2.5）—— **年份段是硬约束，排量不是**。这是实测打脸后的结论。

2. **降级链**：`(base_model, year_bucket)` 样本 < 5 时，退到 `(base_model, ALL_YEARS)`
   的车型级中位价。两档都存进表，不靠运行时兜。

3. **缺维权重重归一**（关键）：五维打分里，热度只有 44.7% 覆盖、车况 28.6%，
   若缺维记 0 分等于给一半的车无差别扣分。所以缺维的**权重从分母里去掉**。
   这里通过 `has_condition` / `heat_score IS NULL` / `price_ratio IS NULL` 暴露
   缺维事实，由 engine 决定怎么归一。

4. 行情基准只用**在售**（vehicle_status=1）。比价的对象是"现在市场上还有什么"，
   掺进几个月前的已售记录会把中位价往下拽。

用法：
    uv run python -m carinfo.search.features            # 建表 + 全量重算
    uv run python -m carinfo.search.features --dry-run  # 只算不写，打印分布
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

import psycopg2
from psycopg2.extras import execute_values

from carinfo.search.normalize import Vocabulary, clean_text, normalize_brand, normalize_model

# ---------------------------------------------------------------------------
# 常量：改动前先读上面 docstring 的第 1、3 条
# ---------------------------------------------------------------------------

#: 行情分组的年份桶宽度（年）
YEAR_BUCKET_WIDTH = 5
#: 车型级行情行的哨兵 year_bucket（真年份桶不会是 -1）
ALL_YEARS = -1
#: 一组至少多少台才算「行情可信」，低于此值走降级链
MIN_SAMPLES = 5
#: 价格比低于此值判为异常（问题车/事故车/标错价），不参与捡漏推荐
ANOMALY_RATIO = 0.5

#: 车况子项权重（只在子项有值时参与，缺的子项权重从分母去掉）。
#: import 0.20→0.10(2026-09-25,外部审核 D-4):行/水货是**来源属性**不是车况,
#: 实测 1,000 台只填「行貨」两个字就拿车况满分,压过有真实手数/里程的车。
#: 腾出的权重给手数/里程(0.50/0.40)——它们才是车况本体。
CONDITION_WEIGHTS = {"hand": 0.50, "mileage": 0.40, "import": 0.10}

#: 手数 → 分（0 手是新车级，逐级递减）
HAND_SCORE = {0: 1.0, 1: 0.80, 2: 0.60, 3: 0.40, 4: 0.25}
HAND_SCORE_TAIL = 0.10  # 5 手及以上

#: 年均里程 → 分（香港私家车常见 1-1.5 万公里/年）
MILEAGE_BANDS = ((10_000, 1.0), (15_000, 0.85), (20_000, 0.65), (30_000, 0.40))
MILEAGE_SCORE_TAIL = 0.20

#: 行/水货 → 分（行货是总代理正规进口，二手保值更好）
IMPORT_SCORE = {"行貨": 1.0, "水貨": 0.70, "行货": 1.0, "水货": 0.70}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS market_stats (
  base_model   varchar(100) NOT NULL,
  year_bucket  integer      NOT NULL,
  sample_n     integer      NOT NULL,
  median_price numeric(12,2),
  p25_price    numeric(12,2),
  p75_price    numeric(12,2),
  updated_at   timestamptz  DEFAULT now(),
  PRIMARY KEY (base_model, year_bucket)
);
CREATE INDEX IF NOT EXISTS idx_market_stats_bucket ON market_stats (year_bucket);

CREATE TABLE IF NOT EXISTS vehicle_features (
  vehicle_id      varchar(50) PRIMARY KEY REFERENCES vehicles(vehicle_id) ON DELETE CASCADE,
  base_model      varchar(100),
  brand_norm      varchar(100),
  year_bucket     integer,
  price_ratio     numeric(6,3),
  market_median   numeric(12,2),
  market_p25      numeric(12,2),
  market_p75      numeric(12,2),
  market_bucket   integer,
  market_ref_n    integer,
  market_level    varchar(10),
  condition_score numeric(4,3),
  has_condition   boolean NOT NULL DEFAULT FALSE,
  age_days        integer,
  heat_score      numeric(4,3),
  is_anomaly      boolean NOT NULL DEFAULT FALSE,
  updated_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vf_base_model ON vehicle_features (base_model);
CREATE INDEX IF NOT EXISTS idx_vf_base_bucket ON vehicle_features (base_model, year_bucket);
CREATE INDEX IF NOT EXISTS idx_vf_ratio ON vehicle_features (price_ratio);
"""


def _pct(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（对 21k 行来说比拉 numpy 划算，也不需要额外依赖）。"""
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


@dataclass
class RawRow:
    """从 vehicles 拉出来、已归一的一行原料。"""

    vehicle_id: str
    base_model: str | None
    brand_norm: str | None
    year: int | None
    price: float | None
    age_days: int | None
    view_count: int | None
    comment_count: int | None
    hand_count: int | None
    mileage_km: int | None
    import_type: str | None


def _to_int(val) -> int | None:
    """extra_fields 里的值是 jsonb 取出来的，可能是 str/int/带逗号文本。"""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    text = str(val).strip().replace(",", "").replace("，", "")
    return int(text) if text.isdigit() else None


_MILEAGE_TEXT_RE = re.compile(r"(\d[\d,，]*)")


def _parse_mileage_km(raw_km, raw_text) -> int | None:
    """里程归一：优先用已结构化的 mileage_km，退回解析 mileage 文本（'40000km'）。

    实测两键交集只有 1508，并集 3001 —— 必须都吃，缺一个丢 1500 台。
    """
    km = _to_int(raw_km)
    if km is None and raw_text:
        m = _MILEAGE_TEXT_RE.search(str(raw_text))
        if m:
            km = _to_int(m.group(1))
    # 明显不合理的值（0 或 >100 万公里）当缺失处理
    if km is None or km <= 0 or km > 1_000_000:
        return None
    return km


def load_rows(conn, vocab: Vocabulary) -> list[RawRow]:
    """拉全量在售车并归一。归一放 Python 做，因为词表在内存里（1110 条），
    塞进 SQL 反而要建临时表，21k 行不值得。

    age_days 在 SQL 里算，**不要在 Python 里对 update_date 做减法**：
    update_date 在 schema 里是 varchar(50)，psycopg2 交回来的是 str，
    `datetime - str` 会抛 TypeError。上一版就是被 except 吞掉，导致 age_days
    全表 NULL、"时效"维度静默失效（打分只剩匹配+性价比两维）。实测 update_date
    100% 是 ISO 格式（21116/21116），SQL 里加正则守卫后转换是安全的。
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT vehicle_id, car_model, car_brand, year, current_price,
               EXTRACT(EPOCH FROM (now() - COALESCE(
                   CASE WHEN update_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                        THEN update_date::timestamptz END,
                   created_at))) / 86400.0 AS age_days_raw,
               extra_fields
        FROM vehicles
        WHERE vehicle_status = 1
          AND current_price IS NOT NULL AND current_price > 0
    """)
    rows: list[RawRow] = []
    for vid, car_model, car_brand, year, price, age_raw, extra in cur.fetchall():
        base, brand_hint, _ = normalize_model(car_model, vocab)
        ef = extra if isinstance(extra, dict) else (json.loads(extra) if extra else {})
        rows.append(
            RawRow(
                vehicle_id=vid,
                base_model=base or None,
                brand_norm=normalize_brand(car_brand) or brand_hint,
                year=int(year) if year and year.isdigit() and 1950 <= int(year) <= 2030 else None,
                price=float(price),
                age_days=max(0, int(age_raw)) if age_raw is not None else None,
                view_count=_to_int(ef.get("view_count")),
                comment_count=_to_int(ef.get("comment_count")),
                hand_count=_to_int(ef.get("hand_count")),
                mileage_km=_parse_mileage_km(ef.get("mileage_km"), ef.get("mileage")),
                import_type=(clean_text(ef.get("import_type")) or None),
            )
        )
    cur.close()
    return rows


_NOW = None


def _now(conn):
    """取一次库时间就缓存，避免 21k 次 now() —— 也保证 age_days 用的是同一个基准。"""
    global _NOW
    if _NOW is None:
        cur = conn.cursor()
        cur.execute("SELECT now()")
        _NOW = cur.fetchone()[0]
        cur.close()
    return _NOW


def year_bucket_of(year: int | None) -> int | None:
    """5 年段：1998→1995, 2024→2020。0-4 年为一档。"""
    if year is None:
        return None
    return (year // YEAR_BUCKET_WIDTH) * YEAR_BUCKET_WIDTH


def _bucket_distance(bucket: int, year: int) -> int:
    """车年份到某个年份段区间 [bucket, bucket+4] 的距离，落在区间内为 0。"""
    upper = bucket + YEAR_BUCKET_WIDTH - 1
    if bucket <= year <= upper:
        return 0
    return bucket - year if year < bucket else year - upper


# ---------------------------------------------------------------------------
# 行情基准
# ---------------------------------------------------------------------------


def build_market_stats(rows: list[RawRow]) -> dict[tuple[str, int], tuple]:
    """算出 (base_model, year_bucket) 与 (base_model, ALL_YEARS) 两档行情。

    Returns: {(base_model, bucket): (n, median, p25, p75)}，其中 bucket 可能是 ALL_YEARS
    """
    per_bucket: dict[tuple[str, int], list[float]] = defaultdict(list)
    per_model: dict[str, list[float]] = defaultdict(list)

    for r in rows:
        if not r.base_model or r.price is None:
            continue
        per_model[r.base_model].append(r.price)
        bucket = year_bucket_of(r.year)
        if bucket is not None:
            per_bucket[(r.base_model, bucket)].append(r.price)

    out: dict[tuple[str, int], tuple] = {}
    for (base, bucket), prices in per_bucket.items():
        prices.sort()
        out[(base, bucket)] = (
            len(prices),
            _pct(prices, 0.5),
            _pct(prices, 0.25),
            _pct(prices, 0.75),
        )
    for base, prices in per_model.items():
        prices.sort()
        out[(base, ALL_YEARS)] = (
            len(prices),
            _pct(prices, 0.5),
            _pct(prices, 0.25),
            _pct(prices, 0.75),
        )
    return out


def pick_reference(
    stats: dict[tuple[str, int], tuple], base_model: str | None, year: int | None
) -> tuple[float, float, float, int, str, int | None] | None:
    """三级降级链，返回 (median, p25, p75, n, level, used_bucket)。

    1. `bucket` —— 本年份段，样本 ≥ MIN_SAMPLES
    2. `near`   —— 隔一档的年份段（±5 年），取**年份距离最近**的；距离相同时
                   取更老的那一档。为什么要保守：更新的年份段中位价更高，选它
                   会把车算得"异常便宜"，把问题车推成"超级捡漏"。宁可漏，不可错。
    3. `model`  —— 车系全年代中位（最后的兜底，会标注出来，可信度最低）

    第 2 级是必须的，不是锦上添花：只做 1→3 时，2004 年的 ALPHARD（本年份段只有
    1 台）会去比车系全年代中位 34.8 万 —— 而那个中位被 2020 年代的新车主导，
    算出 ratio=0.072 这种垃圾值。改成比 2005-2009 段（中位 3.98 万）才对。
    """
    if not base_model:
        return None

    bucket = year_bucket_of(year)
    if bucket is not None:
        hit = stats.get((base_model, bucket))
        if hit and hit[0] >= MIN_SAMPLES:
            return hit[1], hit[2], hit[3], hit[0], "bucket", bucket

        # 隔一档的邻近年份段
        cands: list[tuple[int, int, tuple, int]] = []
        for delta in (-YEAR_BUCKET_WIDTH, YEAR_BUCKET_WIDTH):
            nb = bucket + delta
            nb_hit = stats.get((base_model, nb))
            if nb_hit and nb_hit[0] >= MIN_SAMPLES:
                # (距离, 是否更新, 行情, 段起点)：距离相同时「是否更新」小的（更老）优先
                cands.append((_bucket_distance(nb, year), 1 if delta > 0 else 0, nb_hit, nb))
        if cands:
            cands.sort(key=lambda c: (c[0], c[1]))
            _, _, hit, nb = cands[0]
            return hit[1], hit[2], hit[3], hit[0], "near", nb

    hit = stats.get((base_model, ALL_YEARS))
    if hit and hit[0] >= MIN_SAMPLES:
        return hit[1], hit[2], hit[3], hit[0], "model", None
    return None


# ---------------------------------------------------------------------------
# 单车特征
# ---------------------------------------------------------------------------


def condition_score(r: RawRow) -> tuple[float | None, bool]:
    """车况子项加权平均，缺的子项权重从分母去掉（不让缺数据等于差车况）。

    实测覆盖（在售 21061 台）：手数 3085 / 里程 3001 / 行水货 3078，
    三项任一有值 6030 台（28.6%）。牌费到期（license_until）值形如 '11月'、
    '2027年'，语义残缺，**刻意不用**。
    """
    parts: list[tuple[str, float, float]] = []  # (子项名, 权重, 分)

    if r.hand_count is not None and 0 <= r.hand_count <= 12:
        parts.append(("hand", CONDITION_WEIGHTS["hand"],
                      HAND_SCORE.get(r.hand_count, HAND_SCORE_TAIL)))

    if r.mileage_km is not None:
        # 年均里程：车龄至少按 1 年算，避免新车 500 公里除出天文数字
        years = max(1.0, (r.age_days or 365) / 365.0)
        per_year = r.mileage_km / years
        score = MILEAGE_SCORE_TAIL
        for limit, s in MILEAGE_BANDS:
            if per_year <= limit:
                score = s
                break
        parts.append(("mileage", CONDITION_WEIGHTS["mileage"], score))

    if r.import_type in IMPORT_SCORE:
        parts.append(("import", CONDITION_WEIGHTS["import"], IMPORT_SCORE[r.import_type]))

    if not parts:
        return None, False
    # 只有来源属性(行/水货)、没有任何真实车况子项 → 封顶 0.60(外部审核 D-4):
    # 「行貨」两个字不构成车况证据,不该和「0手+低里程+行货」同拿满分。
    # 0.60 = 中性偏上:来源信息有一点价值,但远不等于车况被核实过。
    if all(k == "import" for k, _, _ in parts):
        return 0.600, True
    total_w = sum(w for _, w, _ in parts)
    return round(sum(w * s for _, w, s in parts) / total_w, 3), True


def build_heat(rows: list[RawRow]) -> tuple[dict[str, float], float, float]:
    """热度：浏览量为主（80%）+ 留言数（20%），log1p 压缩后按 P95 归一。

    实测只有 44.7% 的车有浏览量（列表页字段，详情页爬到的那批才有），
    所以缺失必须保持 NULL，交给 engine 做权重归一，不能填 0。
    Returns: ({vehicle_id: score}, views_p95, comments_p95)
    """
    views = sorted(r.view_count for r in rows if r.view_count is not None)
    comments = sorted(r.comment_count for r in rows if r.comment_count is not None)
    v95 = _pct(views, 0.95) if views else 1.0
    c95 = _pct(comments, 0.95) if comments else 1.0
    v_denom = math.log1p(max(v95, 1.0))
    c_denom = math.log1p(max(c95, 1.0))

    out: dict[str, float] = {}
    for r in rows:
        if r.view_count is None:
            continue
        v = math.log1p(max(r.view_count, 0)) / v_denom
        c = math.log1p(max(r.comment_count or 0, 0)) / c_denom
        out[r.vehicle_id] = round(min(1.0, 0.8 * v + 0.2 * c), 3)
    return out, round(v95, 1), round(c95, 1)


def build_features(
    rows: list[RawRow], stats: dict[tuple[str, int], tuple]
) -> list[tuple]:
    """拼出 vehicle_features 的行。"""
    heat, v95, c95 = build_heat(rows)
    print(f"    热度基准: 浏览量 P95={v95}, 留言数 P95={c95}, 有热度值的车 {len(heat)} 台")

    out: list[tuple] = []
    lvl_counter: dict[str, int] = defaultdict(int)
    for r in rows:
        ref = pick_reference(stats, r.base_model, r.year)
        ratio = level = None
        ref_n = used_bucket = None
        med = p25 = p75 = None
        if ref is not None:
            med, p25, p75, ref_n, level, used_bucket = ref
            if med and med > 0:
                ratio = round(r.price / med, 3)
        cond, has_cond = condition_score(r)
        is_anomaly = bool(ratio is not None and ratio < ANOMALY_RATIO)
        lvl_counter[level or "none"] += 1
        out.append(
            (
                r.vehicle_id,
                r.base_model,
                r.brand_norm,
                year_bucket_of(r.year),
                ratio,
                round(med, 2) if med is not None else None,
                round(p25, 2) if p25 is not None else None,
                round(p75, 2) if p75 is not None else None,
                used_bucket,
                ref_n,
                level,
                cond,
                has_cond,
                r.age_days,
                heat.get(r.vehicle_id),
                is_anomaly,
            )
        )
    print(f"    行情降级分布: {dict(lvl_counter)}")
    return out


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------


def ensure_tables(conn) -> None:
    """在**影子表**上建空表（正式表此时原样可读）。

    为什么不用「DROP 正式表 + CREATE」：那中间有个**空表窗口**，而 engine 是
    `JOIN vehicle_features` —— 窗口期内所有搜索都返回 0 条。实测抓到了：并发 4 路
    搜索跑一次重算，`total_matched` 取值集合是 `[0, 2030]`，**静默空结果**。
    这比报错更糟：前端不会异常、监控不报警，用户只看到"没搜到车"。

    影子表方案：数据灌进 `*_new`，最后一次事务内换名（见 swap_tables），
    搜索要么看到旧表全量、要么看到新表全量，不存在中间态。

    表名后缀固定 `_new`（不用时间戳）：重算脚本是串行调用的，且每次开头都
    `DROP ... IF EXISTS`，固定名让上一次崩溃留下的残骸能被自动清掉。
    """
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS vehicle_features_new")
    cur.execute("DROP TABLE IF EXISTS market_stats_new")
    cur.execute(_shadow_ddl())
    conn.commit()
    cur.close()


def _shadow_ddl() -> str:
    """把 CREATE_SQL 里的表名/索引名换成影子表版本。

    原始 DDL 是唯一真相，影子 DDL 由它机械派生 —— 免得改了正式 DDL 忘了改影子。

    ⚠️ 这里**不能用链式 str.replace**（踩过两次坑）：
      - 索引名 `idx_market_stats_bucket` 里嵌着表名 `market_stats`，表名替换会二次命中；
      - 想把索引名提前换成 `..._bucket_new` 规避，结果表名替换继续命中，
        得到 `idx_market_stats_new_bucket_new`，还原后多一段 `_new`；
      - 正确做法被这种"边替换边制造新匹配"的写法带偏，越改越错。

    改用**一次性正则**：把「表名」与「索引名」两类标识符分别映射，
    一次扫描全部替换，不存在"替换产物被后续规则再次命中"的问题。
    """
    table_map = {"market_stats": "market_stats_new", "vehicle_features": "vehicle_features_new"}
    index_map = {
        "idx_market_stats_bucket": "idx_market_stats_bucket_new",
        "idx_vf_base_model": "idx_vf_base_model_new",
        "idx_vf_base_bucket": "idx_vf_base_bucket_new",
        "idx_vf_ratio": "idx_vf_ratio_new",
    }
    mapping = {**index_map, **table_map}   # 索引名较长，优先命中（regex alternation 顺序）

    # 只替换紧跟在 TABLE / INDEX / REFERENCES / ON 之后的标识符，不碰列名与约束表达式。
    # ⚠️ `ON` 这一支是必需的：`CREATE INDEX ... ON vehicle_features (...)` 里的表名
    #    若不替换，索引就建到了**正式表**上（名字却带 _new），随后 DROP 正式表会把
    #    它一起删掉 —— 表现为"重算几次之后正式表的业务索引全没了"。实测踩过。
    pattern = re.compile(
        r"\b(TABLE(?:\s+IF\s+NOT\s+EXISTS)?"
        r"|INDEX(?:\s+IF\s+NOT\s+EXISTS)?"
        r"|REFERENCES"
        r"|ON)\s+([a-z_][a-z_0-9]*)",
        re.IGNORECASE,
    )

    def _sub(m: re.Match) -> str:
        kw, name = m.group(1), m.group(2)
        return f"{kw} {mapping.get(name, name)}"

    return pattern.sub(_sub, CREATE_SQL)


def write_all(conn, stats: dict, feats: list[tuple]) -> None:
    """灌进影子表（正式表仍对外服务）。"""
    cur = conn.cursor()
    execute_values(
        cur,
        """INSERT INTO market_stats_new
           (base_model, year_bucket, sample_n, median_price, p25_price, p75_price, updated_at)
           VALUES %s""",
        [(b, yb, n, round(m, 2), round(p25, 2), round(p75, 2), _now(conn))
         for (b, yb), (n, m, p25, p75) in stats.items()],
        page_size=1000,
    )
    execute_values(
        cur,
        """INSERT INTO vehicle_features_new
           (vehicle_id, base_model, brand_norm, year_bucket, price_ratio,
            market_median, market_p25, market_p75, market_bucket, market_ref_n,
            market_level, condition_score, has_condition, age_days, heat_score,
            is_anomaly, updated_at)
           VALUES %s""",
        [f + (_now(conn),) for f in feats],
        page_size=2000,
    )
    conn.commit()
    cur.close()


def swap_tables(conn) -> None:
    """在一个事务里把影子表换成正式表（原子切换）。

    这里必须用**单条 DROP+RENAME 同事务**：PostgreSQL 的 DDL 是事务性的，
    失败会整体回滚，正式表不受影响。搜索方最多在该事务提交瞬间被行锁阻塞
    （毫秒级），不会看到空表。

    **护栏（必须留）**：影子表为空时直接拒绝切换。否则「上游算出空结果」
    会变成「把正式表换成空表」—— 搜索全库返回 0 条，而这属于数据损坏，
    不是"重算失败"。派生表宁可保持旧值也不要被清空。

    外键说明：`vehicle_features.vehicle_id` 引用 `vehicles`。影子表建的时候
    一并带上了外键（DDL 机械替换保留了 REFERENCES），换名不影响约束。

    索引改名：RENAME TABLE **不会**自动改索引名，PK 索引和显式索引都会留着
    影子后缀（实测 `market_stats_new_pkey1`）。不改回来的话第二次重算时
    影子索引名会冲突。所以这里把所有影子索引名逐一还原。
    """
    cur = conn.cursor()

    # 护栏：影子表必须有数据（两者是同时重建的，任一为空都说明上游出了问题）
    cur.execute("SELECT count(*) FROM vehicle_features_new")
    n_feat = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM market_stats_new")
    n_stats = cur.fetchone()[0]
    if n_feat == 0 or n_stats == 0:
        cur.close()
        raise RuntimeError(
            f"影子表为空（features={n_feat}, stats={n_stats}），拒绝切换 —— "
            f"换成空表等于清空派生数据，搜索会全库返回 0 条"
        )

    try:
        cur.execute("BEGIN")
        # ⚠️ 必须设 lock_timeout。这里要 ACCESS EXCLUSIVE 锁，而搜索请求会在
        # vehicle_features 上加读锁；若恰有长事务（或有人把连接借走忘了还），
        # 这个 LOCK 会**无限期等下去** —— 后果是重算永远不结束，且它会一直占着
        # 自己的连接与影子表。宁可 5 秒后失败退让（下次重算再来），也不要挂死。
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("LOCK TABLE vehicle_features IN ACCESS EXCLUSIVE MODE")
        cur.execute("DROP TABLE IF EXISTS vehicle_features")
        cur.execute("DROP TABLE IF EXISTS market_stats")
        cur.execute("ALTER TABLE vehicle_features_new RENAME TO vehicle_features")
        cur.execute("ALTER TABLE market_stats_new RENAME TO market_stats")
        _rename_shadow_indexes(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _rename_shadow_indexes(cur) -> None:
    """把影子表带来的 `_new` 名字还原：索引名 + 外键约束名。

    规则只有一条：掉末尾/中段的 `_new`。
      idx_vf_base_model_new                     → idx_vf_base_model
      market_stats_new_pkey1                    → market_stats_pkey1
      vehicle_features_new_vehicle_id_fkey1     → vehicle_features_vehicle_id_fkey1

    为什么不写死名字：PK 索引与外键名都由 PostgreSQL 自动生成（带序号后缀），
    漏掉的话每轮重算都会攒下一批 `_new` 名字。
    """
    # ① 索引（含 PK 索引）
    cur.execute("""
        SELECT c.relname
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        JOIN pg_class t ON t.oid = i.indrelid
        WHERE t.relname IN ('vehicle_features', 'market_stats')
          AND c.relname LIKE %s
    """, ("%_new%",))
    for (name,) in cur.fetchall():
        new_name = name.replace("_new_pkey", "_pkey")
        if new_name.endswith("_new"):
            new_name = new_name[:-4]
        if new_name != name:
            cur.execute(f'ALTER INDEX "{name}" RENAME TO "{new_name}"')

    # ② 外键约束（影子表建表时一并带过来，名字里含影子表名）
    cur.execute("""
        SELECT con.conname, rel.relname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname IN ('vehicle_features', 'market_stats')
          AND con.conname LIKE %s
    """, ("%_new%",))
    for conname, tbl in cur.fetchall():
        base = tbl.replace("_new", "")          # 换名后表名不含 _new，此处即 tbl
        new_name = conname.replace(f"{tbl}_new_", f"{base}_").replace("_new", "")
        if new_name != conname:
            cur.execute(f'ALTER TABLE "{tbl}" RENAME CONSTRAINT "{conname}" TO "{new_name}"')


def main(argv: list[str] | None = None) -> int:
    """重算派生表。

    Args:
        argv: 命令行参数（默认取 sys.argv）；被爬虫调用时传 [] 走默认路径。
            调用方只关心成功/失败，**不要**在成功路径上抛异常 —— 特征表是派生
            数据，重算失败不应该让爬虫整轮判为失败。
    """
    ap = argparse.ArgumentParser(description="重算行情基准与单车特征")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印分布，不写库")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # 连库失败是环境问题，返回非 0 而不是抛栈；调用方只记日志
    try:
        conn = psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", 5432)),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            dbname=os.environ["DB_NAME"],
            connect_timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 连接数据库失败，重算中止: {e}")
        return 2

    t0 = time.time()
    try:
        cur = conn.cursor()
        cur.execute("SELECT base_model, brand, search_pattern FROM model_vocabulary WHERE status='active'")
        vocab = Vocabulary.from_rows(cur.fetchall())
        cur.close()
        print(f"[1] 词表 {len(vocab)} 条（剔脏 {len(vocab.dropped)} 条），耗时 {time.time()-t0:.2f}s")

        rows = load_rows(conn, vocab)
        print(f"[2] 载入在售车 {len(rows)} 台，耗时 {time.time()-t0:.2f}s")

        stats = build_market_stats(rows)
        n_bucket = sum(1 for (_, yb) in stats if yb != ALL_YEARS)
        n_model = len(stats) - n_bucket
        print(f"[3] 行情基准: {n_bucket} 个(车系,年份段) + {n_model} 个车型级全段，耗时 {time.time()-t0:.2f}s")

        feats = build_features(rows, stats)
        if not feats:
            print("[ERROR] 无在售车可算，跳过写入（避免把派生表清空）")
            return 3
        covered = sum(1 for f in feats if f[4] is not None)
        print(f"[4] 单车特征 {len(feats)} 行，能算出价格比 {covered} 台 "
              f"({100.0*covered/len(feats):.1f}%)，耗时 {time.time()-t0:.2f}s")

        # 分布体检：价格比不该是一团死水，也不该有大量离谱值
        ratios = sorted(f[4] for f in feats if f[4] is not None)
        if ratios:
            print(f"    价格比 P05={_pct(ratios,0.05):.3f} P25={_pct(ratios,0.25):.3f} "
                  f"P50={_pct(ratios,0.5):.3f} P75={_pct(ratios,0.75):.3f} P95={_pct(ratios,0.95):.3f}")
            print(f"    异常(ratio<{ANOMALY_RATIO}) {sum(f[15] for f in feats)} 台；"
                  f"便宜 15% 以上 {sum(1 for x in ratios if x <= 0.85)} 台")
        print(f"    车况有值 {sum(1 for f in feats if f[12])} 台；"
              f"热度有值 {sum(1 for f in feats if f[14] is not None)} 台")

        if args.dry_run:
            print("\n[DRY-RUN] 未写库")
        else:
            # 顺序：建影子表 → 灌影子表 → 原子换名。
            # 全程正式表保持可读，搜索不会看到空表（对比旧实现 DROP+CREATE 的
            # 空表窗口会让搜索结果静默变 0）。
            ensure_tables(conn)
            write_all(conn, stats, feats)
            swap_tables(conn)
            print(f"\n[OK] 已原子切换到新的 market_stats / vehicle_features，"
                  f"总耗时 {time.time()-t0:.2f}s")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
