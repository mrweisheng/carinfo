"""车身类型划分（7 码）：确定性规则，零 LLM。

方案见 `docs/车身类型划分实施方案.md`（v3）。这里是唯一实现，特征层
（`features.build_features`）每轮重算时逐行调用 `classify()`。

## 为什么不用 LLM

与 `model_aliases` 同一教训：LLM 会把 STEPWGN 编成「大霸王」。字典全部人工确认，
规则与字典都在代码库里走 git review —— 比 DB 表多一层审计，且同输入同输出。

## 判定顺序（先到先得，上层命中不往下走）

    R1 泛用车款词  扫 car_model 全文，任意车系生效，长词优先（带排除前缀）
    R2 车系限定词  必须 base_model 匹配才生效（防泛用词误伤）
    R3 车系字典    (base_model) → 码，人工逐条确认（top 150 + 排除表配套项）
    R4 留白        都没命中 → NULL

**原则：宁可漏判，不可误判。** 判不了就留白（前端显示「其他」），不做
`seats>=7 → MPV` 之类的兜底 —— 实测 seats 以 7 开头的 7,085 台里有约 490 台
是 7 座 SUV（DISCOVERY 122/X5 97/LAND CRUISER 93…），噪声大于信号。

## 三个不能忘的坑（都是实测换来的）

1. **排除表必须按前缀匹配，不能按集合成员。** 库里**没有**裸 `GLC`/`GLE`/`GLS`/
   `EQC` 键 —— GLC 是 `GLC250/GLC300/GLC43/GLC63S` 分键，GLE 是
   `GLE320/400/43/450/53/63` 分键。按集合写 `{CAYENNE, GLC, GLE, …}` 时 10 项里
   只有 6 个能命中，GLC 51 + GLE 35 共 86 台溜背 SUV 照样被标成跑車。
2. **`AVANT` 必须用词边界。** 子串匹配会命中奔驰的配置等级 `AVANTGARDE`
   （87 台，E200 AVANTGARDE 等），把轿车判成旅行車。`(?<![A-Z0-9])AVANT(?![A-Z0-9])`。
3. **排除表引用的车系必须在 R3 字典里有条目**，否则被 R1 让行之后落到 R4 留白，
   `GLE53 COUPE` 会变成「未分类」—— 比标错更糟（用户搜 SUV 看不到它）。
   R3 里 `_EXCLUSION_COMPANIONS` 就是为此存在的。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 7 码与双语标签
# ---------------------------------------------------------------------------

SEDAN = "SEDAN"
HATCHBACK = "HATCHBACK"
WAGON = "WAGON"
SUV = "SUV"
MPV = "MPV"
CONVERTIBLE = "CONVERTIBLE"
COUPE = "COUPE"

#: 全部合法码（spec 收敛、DB 写入、测试都用它做单点）
VALID_BODY_TYPES: tuple[str, ...] = (
    SEDAN, HATCHBACK, WAGON, SUV, MPV, CONVERTIBLE, COUPE,
)

#: 香港标签（展示层主用，`ScoredVehicle.body_type_label` 取这个）
LABELS_HK: dict[str, str] = {
    SEDAN: "房車",
    HATCHBACK: "掀背（揭背）",
    WAGON: "旅行車",
    SUV: "SUV",
    MPV: "七人車",
    CONVERTIBLE: "開篷車",
    COUPE: "跑車",
}

#: 大陆标签（前端按用户地区再选词；解析层两边叫法都收）
LABELS_CN: dict[str, str] = {
    SEDAN: "轿车",
    HATCHBACK: "两厢",
    WAGON: "旅行车",
    SUV: "SUV",
    MPV: "MPV",
    CONVERTIBLE: "敞篷车",
    COUPE: "轿跑",
}

#: 未分类的展示词（前端「其他」）
UNCLASSIFIED_LABEL = "未分类"


def label_of(code: str | None, lang: str = "hk") -> str | None:
    """码 → 展示标签；None/未知码 → None（调用方决定显示「其他」还是留空）。"""
    if not code:
        return None
    return (LABELS_HK if lang == "hk" else LABELS_CN).get(code)


# ---------------------------------------------------------------------------
# R1 泛用车款词
# ---------------------------------------------------------------------------
#: 一条 R1 规则：(命中词, 码, 排除前缀, 是否要求词边界)
#: 排除前缀 = 该 base_model（前缀匹配）出现时本词让行，继续走 R2/R3。
_R1_RULE_DEFS: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    # ── 敞篷：CABRIOLET 全库 0 台，已删；CABRIO 25 台（含 GRANCABRIO） ──
    ("CONVERTIBLE", CONVERTIBLE, (), False),
    ("CABRIO", CONVERTIBLE, (), False),
    ("ROADSTER", CONVERTIBLE, (), False),
    ("SPYDER", CONVERTIBLE, (), False),
    # SPIDER 是另一种拼写（458 SPIDER/650S SPIDER/570S SPIDER…38 台），无假阳性风险
    ("SPIDER", CONVERTIBLE, (), False),
    # ⚠️ `CAB` 香港写法（428i CAB / E250 CAB / CARRERA S CAB，≈50 台）**必须词边界**：
    #    子串会吃下 **WELCAB（轮椅车）** —— ALPHARD/VELLYFIRE/NOAH/VOXY/FREED 的
    #    福祉车版本共 50+ 台 MPV，全部会被判成開篷車。
    ("CAB", CONVERTIBLE, (), True),
    # ── 猎装版（CLA Shooting Brake 等，4 台）是旅行車，不是跑車 ──
    ("SHOOTING BRAKE", WAGON, (), False),
    # ── 性能取向：TYPE R 273 台（CIVIC/FL5/FK8/FD2/FN2/INTEGRA/EP3 独立键全覆盖） ──
    ("TYPE R", COUPE, (), False),
    # ── BMW 2 系 Tourer 是 MPV；必须全词，与 GRAN COUPE 互为反例 ──
    ("GRAN TOURER", MPV, (), False),
    ("ACTIVE TOURER", MPV, (), False),
    ("GRAN COUPE", COUPE, (), False),
    # ── 旅行車：992 GT3 TOURING 是去尾翼版 911（键是 992 不是 911） ──
    ("TOURING", WAGON, ("911", "992"), False),
    ("WAGON", WAGON, (), False),
    ("ESTATE", WAGON, (), False),
    # ── AVANT 必须词边界，否则吃下奔驰配置等级 AVANTGARDE（87 台） ──
    ("AVANT", WAGON, (), True),
    ("HATCHBACK", HATCHBACK, (), False),
    # ── SPORTBACK：Q3/Q5/Q8 的 Sportback 是溜背 SUV ──
    ("SPORTBACK", HATCHBACK, ("Q3", "Q5", "Q8"), False),
    ("SEDAN", SEDAN, (), False),
    ("SALOON", SEDAN, (), False),
    # ── COUPE：溜背 SUV 车系让行（GLC51/GLE35/CAYENNE26/Q5 3/X6 1 ≈116 台） ──
    ("COUPE", COUPE,
     ("CAYENNE", "GLC", "GLE", "GLS", "X2", "X4", "X6", "Q5", "Q8", "EQC"), False),
    # ── CROSS：让行名单见 __doc__ 与 §四；CROSSTREK/YARIS CROSS 判 SUV 是对的 ──
    ("CROSS", SUV, ("TAYCAN", "FREED", "FIT", "JAZZ", "CROWN", "CROSSFIRE"), False),
    ("COUNTRYMAN", SUV, (), False),
    ("CLUBMAN", WAGON, (), False),
    ("SPORTSVAN", MPV, (), False),
    ("SPORTVAN", MPV, (), False),
    # ⚠️ `SPORT` 刻意**不做**泛用词：RANGE ROVER SPORT 是 SUV，
    #    只有 COROLLA SPORT 才是掀背 —— 那条走 R2 车系限定。
)

#: 长词优先：一次排好，避免每次调用再排
_R1_RULES: tuple[tuple[str, str, tuple[str, ...], bool, re.Pattern | None], ...] = tuple(
    sorted(
        ((w, code, exc, wb,
          re.compile(rf"(?<![A-Z0-9]){re.escape(w)}(?![A-Z0-9])") if wb else None)
         for w, code, exc, wb in _R1_RULE_DEFS),
        key=lambda r: -len(r[0]),
    )
)


# ---------------------------------------------------------------------------
# R2 车系限定词：必须 base_model 匹配才生效
# ---------------------------------------------------------------------------
#: (base_model, car_model 必须含的子串, 码, 说明)
_R2_RULES: tuple[tuple[str, str, str, str], ...] = (
    ("COROLLA", "SPORT", HATCHBACK, "COROLLA SPORT 是掀背（TOURING/CROSS 已被 R1 拦走）"),
    ("CIVIC", "FK7", HATCHBACK, "FK7 掀背"),
    ("CIVIC", "FK8", HATCHBACK, "FK8 掀背（TYPE R 版已被 R1 拦走 COUPE）"),
    ("CIVIC", "EK9", HATCHBACK, "EK9 掀背"),
    ("GR", "SUPRA", COUPE, "GR SUPRA 是跑车（GR YARIS/COROLLA 才是掀背）"),
    ("TAYCAN", "CROSS TURISMO", WAGON, "跨界旅行版，非 SUV"),
    ("PRIUS", "PRIUS V", WAGON, "Prius V 是紧凑旅行/MPV，不并进 PRIUS→HATCHBACK"),
    ("IONIQ", "IONIQ 5", SUV, "IONIQ 5 是 SUV"),
    ("IONIQ", "IONIQ 6", SEDAN, "IONIQ 6 是轿车"),
    ("CONTINENTAL", "FLYING SPUR", SEDAN, "大陆飞驰是四门轿车，不是跑车"),
    ("CONTINENTAL", "GTC", CONVERTIBLE, "Continental GTC 是敞篷"),
    ("MINI", "PACEMAN", SUV, "PACEMAN 是小型 SUV"),
)


# ---------------------------------------------------------------------------
# R3 车系字典
# ---------------------------------------------------------------------------
#: base_model → 码。人工逐条确认（方案 §七 第 0 步辅助脚本的产出：
#: `_migration/body_dict_helper.py`）。只收**主流形态唯一**的车系；
#: 歧义车系（MINI/COROLLA/CIVIC/GOLF/A3/BMW 3 系 5 系/奔驰 C-E-S 级/MAZDA/
#: 218I/220IA/AMG/IONIQ 基键）**刻意不标** —— 变体交给 R1/R2 拦，裸名留白。
#:
#: ⚠️ 加条目前过一遍验收清单：`base_model LIKE 'X%'` 查全库，确认没有跨车系撞前缀。
SERIES_DICT: dict[str, str] = {
    # ── MPV（香港市场主力，占车源约三成） ──
    "ALPHARD": MPV, "VELLFIRE": MPV, "FREED": MPV, "VOXY": MPV, "STEPWGN": MPV,
    "NOAH": MPV, "SIENTA": MPV, "SERENA": MPV, "ODYSSEY": MPV, "ESTIMA": MPV,
    "PREVIA": MPV, "ESQUIRE": MPV, "ELGRAND": MPV, "MIFA": MPV, "H1": MPV,
    "WISH": MPV, "VITO": MPV, "LM350": MPV, "LM500H": MPV,
    "HIACE": MPV,   # 仅 16 台私家车（191 是全口径）；VAN 码已撤销，见 §三
    # ── SUV ──
    "MACAN": SUV, "CAYENNE": SUV, "X1": SUV, "X3": SUV, "X4": SUV, "X5": SUV,
    "X6": SUV, "X7": SUV, "MODEL X": SUV, "MODEL Y": SUV, "RAV4": SUV,
    "LAND CRUISER": SUV, "SORENTO": SUV, "DISCOVERY": SUV, "DEFENDER": SUV,
    "EVOQUE": SUV, "RANGE": SUV, "QASHQAI": SUV, "JIMNY": SUV, "G63": SUV,
    "Q3": SUV, "Q5": SUV, "Q7": SUV, "Q8": SUV, "GLA200": SUV, "GLA250": SUV,
    "GLC250": SUV, "GLC300": SUV, "GLC43": SUV, "GLC63S": SUV, "GLE320": SUV,
    "GLE400": SUV, "GLE43": SUV, "GLE450": SUV, "GLE53": SUV, "GLE63": SUV,
    "GLS400": SUV, "GLS450": SUV, "GLS500": SUV, "EQC400": SUV, "EQA250": SUV,
    "EQB250": SUV, "XC40": SUV, "XC60": SUV, "XC90": SUV,
    "VEZEL": SUV, "XV": SUV, "CHR": SUV, "CX5": SUV, "TIGUAN": SUV, "CRV": SUV,
    "FORESTER": SUV, "URUS": SUV, "LEVANTE": SUV, "BENTAYGA": SUV,
    "SANTA": SUV, "LX570": SUV, "NX200T": SUV, "IX3": SUV, "IX": SUV,
    "X2": SUV,
    # ── SEDAN ──
    "MODEL 3": SEDAN, "MODEL S": SEDAN, "C200": SEDAN, "E200": SEDAN,
    "E250": SEDAN, "C220D": SEDAN, "C250": SEDAN, "S320": SEDAN, "S500": SEDAN,
    "S560": SEDAN, "CAMRY": SEDAN, "CROWN": SEDAN, "SEAL": SEDAN,
    "C43": SEDAN, "M3": SEDAN, "GHIBLI": SEDAN, "FLYING": SEDAN,
    "GHOST": SEDAN, "A4": SEDAN, "A6": SEDAN, "318IA": SEDAN, "320I": SEDAN,
    "320D": SEDAN, "320": SEDAN, "520I": SEDAN, "520D": SEDAN, "LANCER": SEDAN,
    "IS300H": SEDAN, "LS500": SEDAN,
    # ── HATCHBACK ──
    "SPADE": HATCHBACK, "SOLIO": HATCHBACK, "JAZZ": HATCHBACK, "FIT": HATCHBACK,
    "PRIUS": HATCHBACK, "RACTIS": HATCHBACK, "PORTE": HATCHBACK, "A200": HATCHBACK,
    "A250": HATCHBACK, "CT200H": HATCHBACK, "YARIS": HATCHBACK, "LEAF": HATCHBACK,
    "MORNING": HATCHBACK, "HUSTLER": HATCHBACK, "SWIFT": HATCHBACK,
    "AQUA": HATCHBACK, "NOTE": HATCHBACK, "B200": HATCHBACK,
    # ── WAGON ──
    "FIELDER": WAGON,
    # ── COUPE（跑車/轿跑大口袋，见 §三） ──
    "911": COUPE, "992": COUPE, "PANAMERA": COUPE, "TAYCAN": COUPE,
    "CAYMAN": COUPE, "718": COUPE, "GRANTURISMO": COUPE, "HURACAN": COUPE,
    "GR86": COUPE, "BRZ": COUPE, "M2": COUPE, "420IA": COUPE, "428I": COUPE,
    "I4": COUPE, "WRX": COUPE, "CLA250": COUPE, "CLA35": COUPE,
    "CONTINENTAL": COUPE, "FL5": COUPE, "FK8": COUPE, "SUPRA": COUPE,
    "CROSSFIRE": COUPE,   # 克莱斯勒跑车（R1 的 CROSS 让行后落在这里）
    # 992 与 CROSSFIRE 都是为了「排除表让行后不落进留白」而存在的配套条目：
    #   992 GT3 TOURING → R1 TOURING 让行（前缀 992）→ R3 → COUPE
    #   CROSSFIRE      → R1 CROSS 让行（前缀 CROSSFIRE）→ R3 → COUPE
    # ── CONVERTIBLE ──
    "MX5": CONVERTIBLE,
    "BOXSTER": CONVERTIBLE,     # 全系敞篷（Cayman 才是硬顶，键分开）
    "CALIFORNIA": CONVERTIBLE,  # 法拉利 California 硬顶敞篷
    # ── HATCHBACK：GR 家族（SUPRA 由 R2 排除） ──
    "GR": HATCHBACK,
    # ── 2026-09-30 上线压测补录批次 ─────────────────────────────────────
    # 背景：这批车系都有**常用中文别名**（model_aliases），但车系本身未分类 ——
    # 用户一旦说「雅阁轿车」「野马跑车」这类「别名+类目词」组合，硬过滤 AND
    # 未分类 = 0 命中。按「库内形态唯一」原则逐条核实后补录（每条抽过
    # car_model 变体；500/FOCUS/INTEGRA/328I/MEGANE 等脏键或混形态键刻意不收）。
    "SPORTAGE": SUV, "PAJERO": SUV, "VELAR": SUV, "NIRO": SUV,
    "WRANGLER": SUV, "XTRAIL": SUV, "TANK": SUV, "STELVIO": SUV,
    "T ROC": SUV, "CULLINAN": SUV,
    "E2008": SUV,   # 标致 e-2008；词表已补 E2008% 前缀防被奔驰 E200% 吞（评测抓到）
    "MARKX": SEDAN, "ACCORD": SEDAN, "PHANTOM": SEDAN, "CENTURY": SEDAN,
    "MAYBACH": SEDAN, "MULSANNE": SEDAN,
    "TOURAN": MPV, "CARNIVAL": MPV, "BIANTE": MPV, "STREAM": MPV,
    "SHARAN": MPV, "JADE": MPV,
    "CUBE": HATCHBACK, "POLO": HATCHBACK, "BEETLE": HATCHBACK,
    "SHUTTLE": WAGON,
    "MUSTANG": COUPE, "GTR": COUPE, "FAIRLADY": COUPE, "SCIROCCO": COUPE,
    "458": COUPE, "AVENTADOR": COUPE, "WRAITH": COUPE, "86": COUPE,
    # ── 同线兄弟键补录（二审 P2，2026-09-30）────────────────────────────
    # 「一个收了、兄弟没收」：NX200T 在而 NX300/NX300H 不在、S500 在而 S400/S450
    # 不在、GR86/86 在而 GT86 不在、Q3-Q8 在而 Q2 不在……逐键抽过 car_model
    # 变体核实形态唯一（M4/C300/E300 混形态键**刻意不收**，靠 R1 词+留白）。
    # 合计 ~390 台，全部是「搜 SUV/轿车会静默漏掉」的主流车。
    "NX300": SUV, "NX300H": SUV, "RX200T": SUV, "RX300": SUV, "RX350": SUV,
    "RX450H": SUV, "UX200": SUV, "GLB250": SUV, "ML400": SUV, "ML350": SUV,
    "Q2": SUV,
    "ES250": SEDAN, "ES300H": SEDAN, "S350": SEDAN, "S400": SEDAN,
    "S450": SEDAN, "IS250": SEDAN, "IS300": SEDAN,
    "Z4": CONVERTIBLE, "IS250C": CONVERTIBLE,   # IS250C 是独立键，敞篷
    "GT86": COUPE,
    "LEVORG": WAGON,
}

#: 排除表引用的车系必须同时在 SERIES_DICT 里有条目 —— 否则 R1 让行之后落到 R4
#: 留白（`GLE53 COUPE` 会变成「未分类」，比标错更糟）。此集合用于测试断言。
_EXCLUSION_COMPANIONS: frozenset[str] = frozenset({
    "CAYENNE", "GLC250", "GLC300", "GLC43", "GLC63S", "GLE320", "GLE400",
    "GLE43", "GLE450", "GLE53", "GLE63", "GLS400", "GLS450", "GLS500",
    "X2", "X4", "X6", "Q3", "Q5", "Q8", "EQC400",
    "TAYCAN", "FREED", "FIT", "JAZZ", "CROWN", "CROSSFIRE", "992", "911",
})

#: 刻意不标的歧义车系（注释用，也在测试里断言它们不在字典中）
AMBIGUOUS_SERIES: frozenset[str] = frozenset({
    "MINI", "COROLLA", "CIVIC", "GOLF", "A3", "A5", "MAZDA", "AMG", "IONIQ",
    "218I", "220IA", "3", "5", "RANGER",
    # 2026-09-30 压测补录时复核为「脏键/混形态」的键 —— 刻意留白：
    "FOCUS",       # 福克斯： sedan/hatch 混卖
    "500",         # 菲亚特 500 与奔驰 500E/500SEL 混在同一键（垃圾键）
    "INTEGRA",     # DC2 coupe / DC5 hatch / 新型格 sedan-hatch 跨代混形态
    "328I",        # 宝马 3 系轿车/敞篷同排量键难分
    "MEGANE",      # hatch/CC/estate 混形态
})


def _prefix_hit(base_model: str, prefixes: tuple[str, ...]) -> bool:
    """前缀匹配（**不是**集合成员）—— 见模块 docstring 坑 1。"""
    return any(base_model.startswith(p) for p in prefixes)


def classify(
    car_model: str | None,
    base_model: str | None,
    brand_norm: str | None = None,
) -> tuple[str | None, str | None]:
    """判定单车车身类型。

    Args:
        car_model: 完整车型串（原始，未归一），如 'GLE53 COUPE AMG'
        base_model: 归一后的车系键，如 'GLE53'
        brand_norm: 归一后的品牌（当前仅作日志/未来跨品牌撞键防线用）

    Returns:
        (body_type, source)：source ∈ {'rule', 'series', None}
        rule   = R1/R2 词命中
        series = R3 车系字典命中
        None   = R4 留白（未分类）
    """
    cm = (car_model or "").upper()
    bm = (base_model or "").upper().strip()

    # ── R1 泛用车款词（长词优先，带排除前缀） ──
    if cm:
        for word, code, except_prefixes, word_boundary, rx in _R1_RULES:
            if word_boundary:
                if rx.search(cm) is None:
                    continue
            elif word not in cm:
                continue
            if except_prefixes and bm and _prefix_hit(bm, except_prefixes):
                continue        # 让行，继续走后面的层
            return code, "rule"

    # ── R2 车系限定词 ──
    if bm:
        for base, sub, code, _note in _R2_RULES:
            if bm == base and sub in cm:
                return code, "rule"

    # ── R3 车系字典 ──
    if bm:
        code = SERIES_DICT.get(bm)
        if code:
            return code, "series"

    # ── R4 留白 ──
    return None, None


def classify_row(row) -> tuple[str | None, str | None]:
    """`features.RawRow` 适配层（避免 features 依赖具体字段名拼装）。"""
    return classify(
        getattr(row, "car_model", None),
        row.base_model,
        row.brand_norm,
    )
