"""检索内核：SQL 硬过滤 + 六维确定性打分。

设计铁律（来自方案定稿）：**大模型只做两头，中间不打分。**
- 一头：parser.py 把自然语言解析成 SearchSpec
- 另一头：explain.py 把结构化结果润色成人话
- 中间：本模块的分数全部是确定性算术 —— 同样的 spec + 同样的库，结果永远一样，
  可复现、可回归、可解释。绝不让模型对候选打分（不可复现，也无法调试）。

六维权重（用户已拍板）：

    匹配 0.28 | 性价比 0.20 | 贴合度 0.20 | 时效 0.16 | 车况 0.12 | 热度 0.04

「贴合度」是后加的**软偏好**，只在用户说了「50 万左右 / 2015 年左右」时才参与。
没有锚点的查询该维缺席，加权平均**数学上等价于**旧口径
（匹配 .35 | 性价比 .25 | 时效 .20 | 车况 .15 | 热度 .05）—— 见 WEIGHTS 处的推导。

**缺维权重归一**：热度只有 44.8% 的车有（列表页字段），车况 28.0%。若缺维记 0 分，
等于给一半以上的车无差别扣分，排序会被"数据齐全度"而不是"车的优劣"主导。所以
缺的维度**从分母里去掉**，分数是"在有数据的维度上的加权平均"。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


class ScanTooWide(RuntimeError):
    """候选集过大，拒绝全量扫描。

    这不是"内部错误" —— 是**调用方的查询条件太宽**（例如只给了座位数）。
    上层应把它翻成 4xx 并提示收窄条件，不要当成 500。
    """
from typing import Any

from carinfo.search.normalize import clean_text
from carinfo.search.spec import SORT_NEWEST, SORT_PRICE_ASC, SORT_PRICE_DESC, SearchSpec

#: 六维权重。改这里就等于改产品口径，务必同步改 explain.py 的话术。
#:
#: ⚠️ 前五项的老权重被**统一乘以 0.8**，腾出 0.20 给 near。这不是随手调的：
#: 缺维从分母去掉之后，没有「左右」的查询 near 缺席，加权平均 =
#:   Σ(0.8·wᵢ·sᵢ) / Σ(0.8·wᵢ) = Σ(wᵢ·sᵢ)
#: 与旧口径**数学上完全等价**。于是新维度对既有查询零影响 —— 只有带「左右」的查询
#: 排序才变，评测里的任何差异都能 100% 归因到新功能。
#: test_search.py 有断言锁住这个 0.8 比例，改权重前先看那条。
WEIGHTS: dict[str, float] = {
    "match": 0.28,
    "value": 0.20,
    "near": 0.20,
    "fresh": 0.16,
    "condition": 0.12,
    "heat": 0.04,
}

DIM_LABELS = {
    "match": "车型匹配",
    "value": "性价比",
    "near": "贴合度",
    "fresh": "挂牌时效",
    "condition": "车况",
    "heat": "关注热度",
}

#: ── 「50 万左右 / 2015 年左右」的贴合带 ──
#: 带内视为"就是它"，给满分；超出后线性衰减，到 band×(1+DECAY) 归零。
#:
#: 价格按**比例**：15% 这一档是**实测常数** —— Monroe(1971) 用 240 名消费者做"两个
#: 价格是否相同"的判断，参考价 $10 的阈限 $1.5、$100 的 $15、$1,000 的 $150，
#: 恒定 15%，即 Weber–Fechner 定律的 ΔI/I=const。所以「10 万左右」的容差必须是
#: 1.5 万而不是 15 万 —— 这也是不能把「左右」做成一个固定金额区间的原因。
#: ⚠️ 上下不对称（贵了更敏感，接受带在参考价上方更窄，出自 latitude of price
#: acceptance / 前景理论）是文献的**定性**结论；10% 与 20% 这两个具体数字是本项目的
#: 工程选择，不是实测常数，可调。
NEAR_BAND_UP = 0.10        # 比锚点**贵**：贴合带 +10%
NEAR_BAND_DOWN = 0.20      # 比锚点**便宜**：贴合带 −20%
NEAR_DECAY = 2.5           # 超出贴合带后，再衰减这么多个带宽即归零（→ 贵 35% / 便宜 70%）
#: 年份按**绝对年数**（等距标度，不按比例）：±2 年约等于同一代车型 / facelift 的跨度。
#: 与价格不同，2015 和 1995 的"左右"都该是 ±2 年，不是按数值比例放大。同样是工程取值。
NEAR_YEAR_BAND = 2.0
NEAR_YEAR_DECAY = 2.0      # → ±6 年归零

#: 性价比：价格比 → 分。锚点之间线性插值，中位价(1.00)得 0.70 分。
#: 为什么中位价不给 0.5 分：中位价是"正常价"，不该被判为及格线以下；
#: 二手车市场里"和同款一个价"是正常交易，不是差评。
VALUE_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.60, 1.00),   # 便宜 40%+
    (0.85, 0.88),   # 便宜 15%
    (1.00, 0.70),   # 与同款同年段中位价持平
    (1.15, 0.48),   # 贵 15%
    (1.40, 0.20),   # 贵 40%
    (2.00, 0.00),   # 贵 1 倍，基本是标错或另有隐情
)

#: 时效：挂牌天数 → 分。单调递减。
FRESH_ANCHORS: tuple[tuple[float, float], ...] = (
    (3, 1.00), (7, 0.90), (30, 0.70), (90, 0.45), (180, 0.25), (365, 0.10), (730, 0.00),
)

#: 候选集上限告警阈值：超过就说明过滤条件太宽，该提醒调用方收窄
WARN_SCAN_ROWS = 20000

#: 硬上限：候选超过这个数就抛 ScanTooWide，由 API 翻成 400（提示收窄条件）。
#:
#: 为什么需要它：`search()` 要让全部候选参与打分才能保证 TopN 正确
#: （sort=score 时不能简单加 SQL LIMIT），所以只能全量拉取。没有上限的话，
#: 一个宽条件（如只给 seats=7）就会把全库 2 万台 × 22 列一次拉进内存，
#: 而 API 是几十并发的内部服务 —— 内存与 CPU 都随库规模线性涨。
#:
#: ⚠️ **这个检查发生在 `fetchall()` 之后，拦不住内存峰值** —— 那一刻数据已经
#:    在内存里了，它真正防住的是"下游打分/序列化阶段被拖垮"，以及给调用方一个
#:    明确的「条件太宽」信号（400 而不是超时/504）。想真正在拉取前拦截，
#:    需要在查询里先 COUNT 一趟；实测 `count(*) FROM (原查询)` 要 162ms、
#:    占一次请求总耗时的 38%（窗口函数强制扫完所有匹配行，加 LIMIT 也救不了），
#:    为本库 2.1 万行的规模付这个代价不划算。**等库涨到接近上限时再改**：
#:    届时正确做法是把打分改成「SQL 排序 + 分批取」，而不是继续全量拉。
#:
#: 当前阈值 30 万约为在售量的 14 倍，留了增长余量，也能在条件写错时立刻叫停。
MAX_SCAN_ROWS = 300_000


def _interp(anchors: tuple[tuple[float, float], ...], x: float, decreasing: bool) -> float:
    """分段线性插值。anchors 需按 x 升序给，decreasing 只为文档语义。"""
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return anchors[-1][1]


@dataclass
class ScoredVehicle:
    """一条候选 + 打分明细。字段直接对应前端要展示的东西。"""

    vehicle_id: str
    car_model: str
    car_brand: str | None
    year: int | None
    price: float | None
    car_url: str | None
    seats: str | None
    engine_volume: str | None

    base_model: str | None = None
    brand_norm: str | None = None
    price_ratio: float | None = None
    market_median: float | None = None
    market_p25: float | None = None
    market_p75: float | None = None
    market_level: str | None = None
    market_ref_n: int | None = None

    age_days: int | None = None
    hand_count: int | None = None
    mileage_km: int | None = None
    import_type: str | None = None
    view_count: int | None = None
    is_anomaly: bool = False

    scores: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    #: 实际参与的维度（缺维不在其中），让上游能如实说"这条没车况数据"
    used_dims: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "car_model": self.car_model,
            "car_brand": self.car_brand,
            "year": self.year,
            "price": self.price,
            "car_url": self.car_url,
            "seats": self.seats,
            "engine_volume": self.engine_volume,
            "base_model": self.base_model,
            "brand_norm": self.brand_norm,
            "price_ratio": self.price_ratio,
            "market_median": self.market_median,
            "market_p25": self.market_p25,
            "market_p75": self.market_p75,
            "market_level": self.market_level,
            "market_ref_n": self.market_ref_n,
            "age_days": self.age_days,
            "hand_count": self.hand_count,
            "mileage_km": self.mileage_km,
            "import_type": self.import_type,
            "view_count": self.view_count,
            "is_anomaly": self.is_anomaly,
            "score": round(self.score, 4),
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
        }


@dataclass
class SearchResult:
    spec: SearchSpec
    items: list[ScoredVehicle]
    total_matched: int          # 硬过滤后、打分前的候选总数
    scanned: int                # 实际拉进内存打分的行数
    elapsed_ms: int
    notes: list[str] = field(default_factory=list)   # 给 explain 层用的诊断信息


# ---------------------------------------------------------------------------
# SQL 拼装
# ---------------------------------------------------------------------------

_SELECT = """
SELECT v.vehicle_id, v.car_model, v.car_brand, v.year, v.current_price, v.car_url,
       v.seats, v.engine_volume, v.extra_fields,
       f.base_model, f.brand_norm, f.price_ratio, f.market_median, f.market_p25,
       f.market_p75, f.market_level, f.market_ref_n, f.age_days, f.condition_score,
       f.has_condition, f.heat_score, f.is_anomaly,
       COUNT(*) OVER () AS _total_matched
FROM vehicles v
JOIN vehicle_features f ON f.vehicle_id = v.vehicle_id
WHERE v.vehicle_status = 1 AND v.current_price IS NOT NULL AND v.current_price > 0
"""


def _norm_import(val: str | None) -> str | None:
    """行/水货写法的两种字形（行貨/行货）统一到繁体，跟库里存的一致。"""
    m = {"行货": "行貨", "水货": "水貨"}
    return m.get(val, val) if val else None


def _like_escape(text: str) -> str:
    """转义 ILIKE 元字符（`%` `_` `\\`）。

    关键词来自自由文本，其中的 `%` / `_` 会被当成通配符：`CX_5` 实际匹配
    `CX-5`、`CX 5` 等（实测 `CX` 94 条 vs 转义后 `CX_` 0 条；不转义时单个 `%`
    直接匹配全库）。反斜杠必须最先转，否则会把转义符自己转掉。
    配合 SQL 里的 `ESCAPE '\\'` 使用。
    """
    return (text.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))


def build_query(spec: SearchSpec) -> tuple[str, list[Any]]:
    """把 spec 翻成参数化 SQL。**所有值必须走占位符**，不做字符串拼接。"""
    sql = _SELECT
    params: list[Any] = []

    if spec.base_model:
        sql += " AND f.base_model = %s"
        params.append(spec.base_model)
    if spec.brand:
        sql += " AND f.brand_norm = %s"
        params.append(spec.brand)
    if spec.model_keyword:
        # 归一失败时的模糊兜底：关键词出现在 car_model 任意位置即可。
        # 必须转义 LIKE 元字符（见 _like_escape）。
        sql += " AND v.car_model ILIKE %s ESCAPE '\\'"
        params.append(f"%{_like_escape(spec.model_keyword)}%")

    if spec.year_min is not None:
        # year 是 varchar，但库内全是 4 位标准年，字典序等值于数值序，能用上索引
        sql += " AND char_length(v.year) = 4 AND v.year >= %s"
        params.append(str(spec.year_min))
    if spec.year_max is not None:
        sql += " AND char_length(v.year) = 4 AND v.year <= %s"
        params.append(str(spec.year_max))

    if spec.price_min is not None:
        sql += " AND v.current_price >= %s"
        params.append(spec.price_min)
    if spec.price_max is not None:
        sql += " AND v.current_price <= %s"
        params.append(spec.price_max)

    if spec.seats is not None:
        # 座位数实测有 '7' / '7 座位' / '7座' 多种写法，取前导数字比
        sql += " AND v.seats ~ '^[0-9]+' AND substring(v.seats from '^[0-9]+')::int = %s"
        params.append(spec.seats)
    if spec.vehicle_type is not None:
        sql += " AND v.vehicle_type = %s"
        params.append(spec.vehicle_type)
    if spec.transmission:
        # 语义上是枚举（库里只有 2 种写法），通配符顶多让匹配变宽、不会注入；
        # 但口径与 model_keyword 统一，避免"有的字段转义有的不转"这种不一致。
        sql += " AND v.transmission ILIKE %s ESCAPE '\\'"
        params.append(f"%{_like_escape(spec.transmission)}%")
    if spec.fuel_type:
        sql += " AND v.fuel_type ILIKE %s ESCAPE '\\'"
        params.append(f"%{_like_escape(spec.fuel_type)}%")

    if spec.import_type:
        sql += " AND v.extra_fields->>'import_type' = %s"
        params.append(_norm_import(spec.import_type))
    if spec.hand_max is not None:
        sql += (" AND v.extra_fields->>'hand_count' ~ '^[0-9]+$'"
                " AND (v.extra_fields->>'hand_count')::int <= %s")
        params.append(spec.hand_max)
    if spec.mileage_max is not None:
        # 口径必须跟展示层 `_mileage_of()` **完全一致**：优先 mileage_km，没有才退回
        # 解析 mileage 文本。两处优先级不同过就会出这种怪事：SQL 放行了一台
        # mileage_km=140000 / mileage='180km' 的车（OR 分支被文本那侧满足），
        # 展示出来却是 14 万公里 —— 违反"里程≤5万"的硬条件。
        # 两个键都没有的车**排除**：买家无法核实里程，就不该出现在"里程以内"的结果里。
        sql += (
            " AND COALESCE("
            "   CASE WHEN v.extra_fields->>'mileage_km' ~ '^[0-9]+$'"
            "        THEN (v.extra_fields->>'mileage_km')::int END,"
            "   CASE WHEN v.extra_fields->>'mileage' ~ '^[0-9]+'"
            "        THEN substring(v.extra_fields->>'mileage' from '^[0-9]+')::int END"
            " ) <= %s"
        )
        params.append(spec.mileage_max)

    if spec.max_price_ratio is not None:
        sql += " AND f.price_ratio IS NOT NULL AND f.price_ratio <= %s"
        params.append(spec.max_price_ratio)
    if spec.exclude_anomaly:
        sql += " AND f.is_anomaly = FALSE"

    return sql, params


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


def score_match(
    spec: SearchSpec,
    car_model: str,
    base_model: str | None,
    brand_norm: str | None = None,
) -> float | None:
    """车型匹配分。**没有匹配目标时返回 None**（该维不参与，不是给 0 分也不给满分）。

    为什么不给满分：纯条件筛选（"50 万以内的 7 座 MPV"）时所有候选都"匹配"，
    给满分等于给每条都加 0.35 的常数，只会把其他维度的区分度压扁。
    """
    if not spec.has_model_target:
        return None

    m = 0.30  # 理论上到不了这里（SQL 已过滤），留个兜底
    if spec.base_model:
        if base_model == spec.base_model:
            m = 1.00
        elif base_model and base_model.startswith(spec.base_model):
            m = 0.85
    elif spec.brand:
        # 只指定品牌时，品牌确实命中才给 0.80（别因为 SQL 过滤了就想当然）
        m = 0.80 if brand_norm == spec.brand else 0.30
    elif spec.model_keyword:
        cm = clean_text(car_model)
        kw = clean_text(spec.model_keyword)
        if cm.startswith(kw):
            m = 0.90
        elif kw in cm:
            m = 0.70

    # 排量偏好只做加分不做硬过滤：搜"阿尔法 3.5"时 3.5 的排前面，2.5 的仍在
    # （用户明确要求"匹配用宽"，但专项偏好要能在排序上体现出来）
    if spec.displacement:
        d = clean_text(spec.displacement)
        if d and d in clean_text(car_model):
            m = min(1.0, m + 0.10)
    return m


def score_value(price_ratio: float | None) -> float | None:
    if price_ratio is None:
        return None
    return _interp(VALUE_ANCHORS, float(price_ratio), decreasing=True)


def score_fresh(age_days: int | None) -> float | None:
    if age_days is None:
        return None
    return _interp(FRESH_ANCHORS, float(age_days), decreasing=True)


def _band_fit(delta: float, band: float, decay: float) -> float:
    """偏离量 → 贴合分。带内满分，带外线性衰减，到 band×(1+decay) 归零。

    delta 必须是非负的偏离量（价格是相对比例，年份是绝对年数）。
    """
    if band <= 0:
        return 1.0 if delta <= 0 else 0.0
    if delta <= band:
        return 1.0
    span = band * decay
    if span <= 0:
        return 0.0
    return max(0.0, 1.0 - (delta - band) / span)


def score_near(spec: SearchSpec, price: float | None, year: int | None) -> float | None:
    """模糊量贴合度：「50 万左右 / 2015 年左右」这类**软偏好**。

    只影响排序，绝不产生硬过滤 —— 偏离带的车照样返回，只是排在后面。
    没设锚点、或该车缺对应字段 → 返回 None（该维不参与，走缺维权重归一）。

    价格按**比例**算偏差，年份按**绝对年数** —— 这是两种标度的本质区别，别统一成一种。
    """
    parts: list[float] = []
    if spec.price_near and price:
        anchor = float(spec.price_near)
        rel = (float(price) - anchor) / anchor
        # 贵了更敏感：上界带宽窄，下界带宽宽
        band = NEAR_BAND_UP if rel >= 0 else NEAR_BAND_DOWN
        parts.append(_band_fit(abs(rel), band, NEAR_DECAY))
    if spec.year_near is not None and year is not None:
        parts.append(
            _band_fit(abs(int(year) - int(spec.year_near)), NEAR_YEAR_BAND, NEAR_YEAR_DECAY)
        )
    if not parts:
        return None
    return sum(parts) / len(parts)


def combine(scores: dict[str, float | None]) -> tuple[float, list[str]]:
    """加权平均，缺维从分母去掉。返回 (总分, 实际参与的维度名)。"""
    acc = 0.0
    total_w = 0.0
    used: list[str] = []
    for dim, w in WEIGHTS.items():
        s = scores.get(dim)
        if s is None:
            continue
        acc += w * float(s)
        total_w += w
        used.append(dim)
    if total_w <= 0:
        return 0.0, used
    return acc / total_w, used


def _int_or_none(v) -> int | None:
    try:
        return int(str(v).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None


def _mileage_of(ef: dict) -> int | None:
    km = _int_or_none(ef.get("mileage_km"))
    if km is None and ef.get("mileage"):
        m = re.search(r"(\d[\d,，]*)", str(ef["mileage"]))
        km = _int_or_none(m.group(1)) if m else None
    return km if km and 0 < km <= 1_000_000 else None


def search(conn, spec: SearchSpec) -> SearchResult:
    """主入口。conn 由调用方给（API 层用连接池，MCP 用单连接）。"""
    t0 = time.perf_counter()
    sql, params = build_query(spec)
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    scanned = len(rows)

    #: 真实候选总数，由窗口函数给出（不是 len(rows) 的同义反复）。
    #: 两者当前恒等（全量拉取），但语义不同 —— 一旦将来引入分批/截断就不再相等，
    #: 分开维护才不会让消费方静默拿到错值。
    total_matched = int(rows[0][-1]) if rows else 0

    if scanned > MAX_SCAN_ROWS:
        raise ScanTooWide(
            f"候选 {scanned} 条超过上限 {MAX_SCAN_ROWS}，请加车型/预算/年份等约束"
        )

    notes: list[str] = []
    if scanned >= WARN_SCAN_ROWS:
        notes.append(f"候选 {scanned} 条，过滤条件偏宽，建议加车型/预算约束以提升排序质量")

    items: list[ScoredVehicle] = []
    for row in rows:
        (
            vid, car_model, car_brand, year, price, car_url, seats, engine_volume, extra,
            base_model, brand_norm, ratio, med, p25, p75, level, ref_n, age_days, cond,
            has_cond, heat, is_anomaly, _total,
        ) = row
        ef = extra if isinstance(extra, dict) else {}
        # year/price 提前解析：near 维度要按原始数值算偏差，不能再从字符串现取
        row_year = int(year) if year and str(year).isdigit() else None
        row_price = float(price) if price is not None else None
        dims: dict[str, float | None] = {
            "match": score_match(spec, car_model or "", base_model, brand_norm),
            "value": score_value(ratio),
            "near": score_near(spec, row_price, row_year),
            "fresh": score_fresh(age_days),
            "condition": float(cond) if has_cond and cond is not None else None,
            "heat": float(heat) if heat is not None else None,
        }
        total, used = combine(dims)
        items.append(
            ScoredVehicle(
                vehicle_id=vid,
                car_model=car_model or "",
                car_brand=car_brand,
                brand_norm=brand_norm,
                year=row_year,
                price=row_price,
                car_url=car_url,
                seats=seats,
                engine_volume=engine_volume,
                base_model=base_model,
                price_ratio=float(ratio) if ratio is not None else None,
                market_median=float(med) if med is not None else None,
                market_p25=float(p25) if p25 is not None else None,
                market_p75=float(p75) if p75 is not None else None,
                market_level=level,
                market_ref_n=ref_n,
                age_days=age_days,
                hand_count=_int_or_none(ef.get("hand_count")),
                mileage_km=_mileage_of(ef),
                import_type=ef.get("import_type"),
                view_count=_int_or_none(ef.get("view_count")),
                is_anomaly=bool(is_anomaly),
                scores={k: float(v) for k, v in dims.items() if v is not None},
                score=total,
                used_dims=used,
            )
        )

    _sort_items(items, spec)
    items = items[: spec.limit]
    elapsed = int((time.perf_counter() - t0) * 1000)
    return SearchResult(
        spec=spec, items=items, total_matched=total_matched, scanned=scanned,
        elapsed_ms=elapsed, notes=notes,
    )


def _sort_items(items: list[ScoredVehicle], spec: SearchSpec) -> None:
    if spec.sort == SORT_PRICE_ASC:
        items.sort(key=lambda x: (x.price is None, x.price or 0, -x.score))
    elif spec.sort == SORT_PRICE_DESC:
        items.sort(key=lambda x: (x.price is None, -(x.price or 0), -x.score))
    elif spec.sort == SORT_NEWEST:
        items.sort(key=lambda x: (x.age_days is None, x.age_days or 0, -x.score))
    else:
        # 综合分降序；同分时便宜的在前（捡漏优先）
        items.sort(key=lambda x: (-x.score, x.price or 0))
