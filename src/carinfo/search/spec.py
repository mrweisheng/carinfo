"""检索规格：把「用户想找什么」表述成一份确定性的数据结构。

这份 spec 是**唯一**的查询入口 —— LLM 解析层（parser.py）、API、MCP 都只负责
把各自格式转成 SearchSpec，然后交给 engine 执行。好处：
- 打分逻辑与输入形态解耦，加一个新入口不用碰引擎
- 离线评测可以直接手搓 spec，不依赖模型
- spec 可序列化，便于日志回放与复现

「只答所问」原则在这里落地：spec 字段全部是**可选**的，没提到的维度一律不加
过滤条件，引擎不会自作主张替你收窄（例如你不说年份，就不会按年份筛）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

#: 返回条数：默认 5，上限 50（导航形态下给太多反而选不动）
DEFAULT_LIMIT = 5
MAX_LIMIT = 50

#: 排序口径
SORT_SCORE = "score"          # 综合分（默认）
SORT_PRICE_ASC = "price_asc"  # 最便宜优先
SORT_PRICE_DESC = "price_desc"
SORT_NEWEST = "newest"        # 最新挂牌优先
VALID_SORTS = (SORT_SCORE, SORT_PRICE_ASC, SORT_PRICE_DESC, SORT_NEWEST)

#: 车型范围（跟 config.json 的 vehicle_types 对齐）
VALID_VEHICLE_TYPES = (1, 2, 3, 4, 5)


def _coerce_int(v: Any) -> int | None:
    """把可能是字符串/浮点/None 的值收敛成 int。转不了返回 None（= 不限）。

    **为什么必须有这个函数**：模型输出的**值**和键一样不可信。白名单只挡住了
    幻觉出来的键，挡不住 `{'seats': '七座'}`、`{'year_min': '2015年'}` 这类
    「键对、值脏」的输入 —— 这些值会原样绑进 SQL 参数，PostgreSQL 抛
    `InvalidTextRepresentation`，用户看到的是 500。防线要盖到值上。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v == int(v) else None
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("，", "")
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _coerce_float(v: Any) -> float | None:
    """同上，收敛成 float。转不了返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("，", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _coerce_opt_bool(v: Any) -> bool | None:
    """三态布尔收敛:None 直通;真布尔直通;字符串只认 true/false/1/0(不分大小写)。

    不能用 bool(v):模型给字符串 "false" 时 bool("false") 是 True —— 方向反了。
    认不出的返回 None(= 不筛)。
    """
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return None


@dataclass
class SearchSpec:
    """一条检索请求。所有字段可选，None 表示「不限」。"""

    # ---- 匹配（宽：只到车系，不带年份/排量）----
    base_model: str | None = None      # 归一后的车系键，如 'ALPHARD'
    brand: str | None = None           # 归一后的品牌，如 'TOYOTA'
    model_keyword: str | None = None   # 归一失败的原始关键词，走 car_model 模糊匹配
    displacement: str | None = None    # 排量偏好（'3.5'）—— 只做加分，不做硬过滤

    # ---- 硬过滤（车况/属性）----
    year_min: int | None = None
    year_max: int | None = None
    price_min: float | None = None
    price_max: float | None = None
    seats: int | None = None
    #: 车型：1=私家车 2=客货车 3=货车 4=电单车 5=经典车（跟 config.json 对齐）。
    #: 默认只搜**私家车** —— 这不是"擅自收窄"，而是本产品的语料本身就是私家车：
    #: 爬虫配置里只有 type 1 有 pages=800，2-5 都是 pages=0（库内那 1814 条是历史
    #: 遗留）。不收窄的话，搜"最便宜的车"会返回电单车（实测 1500 港币的 ADV 100）。
    #: 要放开全车型，显式传 vehicle_type=None。
    vehicle_type: int | None = 1
    transmission: str | None = None
    fuel_type: str | None = None
    import_type: str | None = None     # '行貨' / '水貨'
    hand_max: int | None = None        # 手数上限（找「一手车」就是 hand_max=1）
    mileage_max: int | None = None     # 里程上限（公里）
    #: 中港牌(兩地牌)。None=不筛;True=只要中港牌;False=排除。
    #: 数据来自描述提取(覆盖约 2%),筛 True 的结果天然偏少,属正常。
    china_plate: bool | None = None
    #: 换车帖。None=不筛(默认,4% 的量不值得默认排除);True=只要换车帖
    #: (车商收购线索:卖家想换车=好谈价);False=排除换车帖。
    #: **刻意不进 NL 解析**——「换车」在自然语言里歧义太大(「我想换辆车」
    #: ≠「找换车帖」),只给 spec/API/MCP 的程序化调用。
    swap: bool | None = None

    # ---- 行情过滤 ----
    max_price_ratio: float | None = None   # 只要比同款便宜的：0.9 = 便宜 10% 以上
    exclude_anomaly: bool = True           # 默认剔除问题车（ratio < 0.5）

    # ---- 模糊量（**软偏好，绝不产生硬过滤**）----
    #: 「50 万左右」的锚点。**只参与排序打分**，不写进 price_min/price_max。
    #: 为什么不做硬过滤：硬切 ±15% 会把 39 万、62 万的好车直接从结果里删掉，
    #: 而排序本来就能把它们排在"刚好 50 万"后面 —— 删掉是不可逆的损失。
    #: 依据：Monroe(1971) 实测价格差别的可辨阈限恒为 15%（$10→$1.5 / $100→$15 /
    #: $1,000→$150），与 Weber–Fechner 定律 ΔI/I=const 一致，说明价格容差**按比例**；
    #: Google Cloud 自然语言查询理解的官方做法同样把这类条件归入 soft filter（boost）
    #: 而非 hard filter，理由是"硬过滤在排序之前就砍掉候选池，排序无法挽回"。
    price_near: float | None = None
    #: 「2015 年左右」的锚点。年份是**等距标度**，容差是绝对年数，不按比例 ——
    #: 2015 和 1995 的"左右"都是 ±2 年（同一代车型/facelift 的跨度），不是 ±300 年。
    year_near: int | None = None

    # ---- 呈现 ----
    sort: str = SORT_SCORE
    limit: int = DEFAULT_LIMIT

    #: 原始自然语言（仅用于日志/解释，不参与过滤）
    raw_query: str | None = None

    def __post_init__(self) -> None:
        # ---- 数量与排序 ----
        self.limit = max(1, min(_coerce_int(self.limit) or DEFAULT_LIMIT, MAX_LIMIT))
        if self.sort not in VALID_SORTS:
            self.sort = SORT_SCORE

        # ---- 数值字段统一收敛 ----
        # 模型/外部调用方给的脏值（'七座'、'2015年'、[1]、''）会原样绑进 SQL 参数，
        # PostgreSQL 对 int 列拿到字符串直接抛 InvalidTextRepresentation → 500。
        # 白名单只挡键，这里挡值：**转不了就置 None（= 该维度不限）**。
        # 置 None 而不是保留脏值，与下面 price_near/year_near 的口径一致：
        # 丢一个条件顶多让结果变宽，带着垃圾值查询是直接不可用。
        self.year_min = _coerce_int(self.year_min)
        self.year_max = _coerce_int(self.year_max)
        self.seats = _coerce_int(self.seats)
        self.hand_max = _coerce_int(self.hand_max)
        self.mileage_max = _coerce_int(self.mileage_max)
        self.price_min = _coerce_float(self.price_min)
        self.price_max = _coerce_float(self.price_max)
        self.max_price_ratio = _coerce_float(self.max_price_ratio)

        # vehicle_type：默认 1（私家车）。显式传的脏值先收敛，收敛不了再退回默认 1
        # —— 不能置 None，那等于"放开全车型"，会把电单车当"最便宜的车"返回。
        vt = _coerce_int(self.vehicle_type)
        self.vehicle_type = vt if vt in VALID_VEHICLE_TYPES else (None if self.vehicle_type is None else 1)

        # 字符串字段：非字符串的（数字/列表/dict）一律置 None，别让它们进 ILIKE 参数
        for fname in ("base_model", "brand", "model_keyword", "displacement",
                      "transmission", "fuel_type", "import_type"):
            val = getattr(self, fname)
            if val is not None and not isinstance(val, str):
                setattr(self, fname, None)

        # exclude_anomaly 只接受真布尔/可判真假的标量
        self.exclude_anomaly = bool(self.exclude_anomaly)

        # 三态布尔字段:字符串 "false" 必须落到 False,认不出落 None(不筛)
        self.china_plate = _coerce_opt_bool(self.china_plate)
        self.swap = _coerce_opt_bool(self.swap)

        # ---- 区间写反自动纠正 ----
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            self.price_min, self.price_max = self.price_max, self.price_min
        if (
            self.year_min is not None
            and self.year_max is not None
            and self.year_min > self.year_max
        ):
            self.year_min, self.year_max = self.year_max, self.year_min

        # ---- 模糊量：类型与量级都要收敛 ----
        # 模型塞个 "五十萬" 或 5000000 进来时**丢弃锚点**，退回"无偏好"
        # （软偏好丢了不影响正确性，比带着垃圾锚点排序安全）。
        self.price_near = _coerce_float(self.price_near)
        if self.price_near is not None and not 10_000 <= self.price_near <= 100_000_000:
            self.price_near = None
        self.year_near = _coerce_int(self.year_near)
        if self.year_near is not None and not 1950 <= self.year_near <= 2030:
            self.year_near = None

    # ------------------------------------------------------------------
    @property
    def has_model_target(self) -> bool:
        """是否给了可定位的车型/品牌目标。全空 = 纯条件筛选（如「50万以内的7座MPV」）。"""
        return any((self.base_model, self.brand, self.model_keyword))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchSpec":
        """从 LLM 输出的 JSON 构造。**只认白名单字段**，多余键直接丢，
        防止模型幻觉出 'order_by' 之类穿透到 SQL。"""
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})
