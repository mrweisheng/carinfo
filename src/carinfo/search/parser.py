"""解析层：自然语言 → SearchSpec。

**大模型只做语义，不做映射。** 分工：
- 模型负责：把「阿尔法」认成 ALPHARD、「三十万以下」认成 300000、分清
  「一手车」是手数条件而不是车型 —— 这是世界知识，本地表做不全。
- 本地负责：把模型给的 ALPHARD 映射到**库内真实存在的键**。模型可能说
  "SIENNA" 而库里只有 1 台，也可能拼成 "ALPHARD 3.5 M"。本地用词表归一 +
  存在性校验，命中就用等值匹配，没命中就退关键词模糊匹配。

这样模型幻觉的代价是"退化成模糊搜索"，而不是"搜出空结果"或"报错"。

**降级路径**（无 API key / 调用失败）走 rule_based_parse：别名表 + 数字/单位
正则。覆盖不了复杂语义，但保证搜索功能不会因为模型不可用而整体瘫痪。

**「模糊量」单列一类**（「50 万左右」「2015 年左右」）：它们只产出 `price_near` /
`year_near` 两个**软锚点**，只影响排序，**绝不产生 `price_min/max` 硬过滤**。
两条路径都受这条约束，`_rule_near_anchor()` 是共同裁判 —— 即使模型把"左右"写成
死区间，也会被它拦下来。
"""

from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any

from carinfo.search.context import SearchContext
from carinfo.search.llm import LLMClient, LLMError
from carinfo.search.normalize import BRAND_ALIASES, MODEL_ALIASES, resolve_alias
from carinfo.search.spec import DEFAULT_LIMIT, SearchSpec

#: LLM 允许输出的字段白名单（跟 SearchSpec 对齐）。
#: **刻意不给它 sort / limit 之外的呈现字段**：返回多少条、怎么排是产品口径，
#: 不该由模型每次即兴决定。limit 只允许它区分"给我一个"和"给我一批"。
#:
#: `year_near` / `price_near` 是「左右」这类**模糊量**的锚点。它们跟 min/max 是
#: 两类东西：min/max 是筛选（不符合的直接不返回），near 是偏好（只影响排序）。
#: 之所以必须有它们：把"五十万左右"写成 price_max=500000 会把 39 万、62 万的好车
#: 直接从结果里删掉，而用户的本意只是"离 50 万近的排前面"。
ALLOWED_LLM_FIELDS = {
    "base_model", "brand", "model_keyword", "family", "displacement",
    "year_min", "year_max", "year_near",
    "price_min", "price_max", "price_near",
    "seats", "body_type", "vehicle_type", "transmission", "fuel_type",
    "import_type", "hand_max", "mileage_max", "china_plate",
    "dealer", "max_price_ratio", "exclude_anomaly",
    "sort", "limit", "label",
}
#: swap(换车帖)**刻意不给 NL**:「换车」歧义太大(「我想换辆车」≠「找换车帖」),
#: 只给 spec/API/MCP 的程序化调用。

SYSTEM_PROMPT = """你是香港二手车平台的查询解析器。把用户的话翻译成检索 JSON。

【输出格式 —— 最容易出错，务必遵守】
输出一个 JSON 对象：{"queries": [组1, 组2, ...]}。不要解释文字、不要 markdown 代码块。
**每组是下面「字段清单」里的一个对象**；只有一句话且只提到一台车 → queries 里只有一个组。
用户一次提到**多台车**（"找台14年威尔法，再找台14年埃尔法"、"阿尔法或者威尔法"）→
**每组一台车**，各写一个组对象，别合并、别丢掉任何一台。
每组里**键名必须逐字用字段清单的英文名**。自己换名字（model / car_model / brand_name
之类）等于这个条件没写 —— 系统会直接丢掉它。

【字段清单】未提到的条件一律不输出该键；不要给 null，也不要猜。
label           这组的人话短标签（≤12字，如"14年威尔法"），给结果分组展示用
base_model      车型，英文大写正式名（库里有的精确车系，如 ALPHARD / VELLFIRE）
family          **车系家族前缀**：口语车系名（"宝马7系"→"7"、"奔驰S级"→"S"、
                "Model 3"→"MODEL 3"、"A6"→"A6"）。用户说的不是某个精确车型而是一
                个系列时用这个，通常配 brand。用户说了具体型号（"730"）则不用 family，
                用 model_keyword
brand           品牌，英文大写
model_keyword   型号拿不准或用户说了具体子型号（如 "730"、"2.0T"）时，填该子型号原文
displacement    排量字符串，如 "3.5"
year_min        年份下限，整数
year_max        年份上限，整数
year_near       年份**模糊锚点**（"2015年左右"）→ 2015。只影响排序，不是筛选。
                ⚠️ 用户说**裸年份**（"17年的Model 3"/"2015年威尔法"，没有"左右"）
                = 精确要求 → year_min 与 year_max **都填该年**。"左右/大约"才用 year_near
price_min       价格下限，港币整数
price_max       价格上限，港币整数
price_near      价格**模糊锚点**（"五十万左右"）→ 500000。只影响排序，不是筛选
seats           座位数，整数（**只认「七座 / N座」**；「七人车」是车型，见 body_type）
body_type       车身类型，枚举（只准写这 7 个英文码，中文一律翻译成码）：
                SEDAN(房車/轿车)  HATCHBACK(掀背/揭背/两厢)  WAGON(旅行車/旅行版)
                SUV(越野车/吉普)  MPV(七人車/商务车/保姆车)  CONVERTIBLE(開篷/敞篷)
                COUPE(跑車/轿跑)
                ⚠️「七人車」是**车型类别** → body_type=MPV；「七座」才是座位数 → seats=7
hand_max        手数上限，整数
mileage_max     里程上限（公里），整数
import_type     "行貨" 或 "水貨"
china_plate     布尔。用户明确要「中港牌/兩地牌」的车 → true；没提就不输出
dealer          布尔。「不要车行/只要私人车主/个人卖家」→ false；「只要车行」→ true；没提不输出
transmission    "自動" / "手動"
fuel_type       "汽油" / "柴油" / "混能" / "電動"
vehicle_type    1=私家车 2=客货车 3=货车 4=电单车 5=经典车
max_price_ratio 只要比同款便宜的场合，小数（0.8 / 0.9）
sort            "score"（默认综合）| "price_asc" | "price_desc" | "newest"
limit           整数，返回条数

【车型与品牌】
- 车型放 **base_model**。❌ 不要写 model / car_model / model_name / vehicle_model
- 品牌放 **brand**。❌ 不要写 brand_name / car_brand / make
- 阿尔法 / 埃尔法 / 阿爾法 → ALPHARD，威尔法 → VELLFIRE，海狮 → HIACE，
  诺亚 → NOAH，塞纳 → SIENNA，陆地巡洋舰 → LAND CRUISER
- 平治 / 奔驰 → MERCEDES-BENZ，凌志 → LEXUS，宝马 → BMW，福士 → VOLKSWAGEN，
  万事得 → MAZDA，富豪 → VOLVO
- 只说了品牌没说车型时，只填 brand。
- 说了排量（如"3.5"）填 displacement，值写 "3.5"。
- 车型名实在拿不准英文正式名时，填 model_keyword（原文），不要硬编一个英文名。

【值的规则】
- "打後 / 之後 / 以後 / 以上" = 下限 → year_min；"以內 / 以下 / 樓下" = 上限 → year_max
- price 写港币整数。"三十万以下" → {"price_max": 300000}
- **"左右 / 大约 / 大概 / 前后"是模糊量：只填 *_near，❌ 绝不许写死区间。**
  "五十万左右" → {"price_near": 500000}，❌ 不要写 price_max / price_min
  "2015年左右" → {"year_near": 2015}，❌ 不要写 year_min / year_max
  理由：写死区间会把 39 万、62 万的车直接删掉；"左右"的本意是"离得近的排前面"，
  不是"超出的不要"。
- "七座" → {"seats": 7}；"一手车" → {"hand_max": 1}；"零手" → {"hand_max": 0}
- 车身类型：**只有用户明确说了车型类别词才填 body_type**（"SUV"/"房车"/"七人车"/
  "跑车"/"旅行车"/"敞篷"/"掀背"）。用户没提类型，**绝对不要**替他填 —— 例如
  「找台 14 年威尔法」只填 base_model+年份，不许补 body_type。
  "七人车" → {"body_type": "MPV"}（**不是 seats**）；"七座的 SUV" → 两个都要
- "五万公里以內" → {"mileage_max": 50000}
- "性价比高" → {"max_price_ratio": 0.9}；"捡漏 / 超值 / 特别便宜" → {"max_price_ratio": 0.8}
- vehicle_type：用户明确说"客货车 / 货车 / 电单车"才填；说"车"或没说就不填
- limit：没说就不填（系统默认返回 5 条）

【完整示例】
"阿尔法"                 → {"queries":[{"base_model": "ALPHARD"}]}
"五十萬以內的阿尔法"       → {"queries":[{"base_model": "ALPHARD", "price_max": 500000}]}
"五十万左右的阿尔法"       → {"queries":[{"base_model": "ALPHARD", "price_near": 500000}]}
"我找一台宝马7系"         → {"queries":[{"brand": "BMW", "family": "7"}]}
"宝马730"               → {"queries":[{"brand": "BMW", "model_keyword": "730"}]}
"特斯拉 Model 3"         → {"queries":[{"brand": "TESLA", "family": "MODEL 3"}]}
"奔驰S级或者E级"          → {"queries":[{"brand":"MERCEDES-BENZ","family":"S"},
                                        {"brand":"MERCEDES-BENZ","family":"E"}]}
"找台14年威尔法，再找台14年埃尔法"
                       → {"queries":[{"label":"14年威尔法","base_model":"VELLFIRE","year_min":2014,"year_max":2014},
                                        {"label":"14年埃尔法","base_model":"ALPHARD","year_min":2014,"year_max":2014}]}
"三十万以下的七座车"       → {"queries":[{"price_max": 300000, "seats": 7}]}
"找台七人车"             → {"queries":[{"body_type": "MPV"}]}
"15万左右的SUV"          → {"queries":[{"price_near": 150000, "body_type": "SUV"}]}
"七座的SUV"             → {"queries":[{"body_type": "SUV", "seats": 7}]}
"最便宜的平治"            → {"queries":[{"brand": "MERCEDES-BENZ", "sort": "price_asc"}]}
"捡漏阿尔法"              → {"queries":[{"base_model": "ALPHARD", "max_price_ratio": 0.8}]}

【铁律】
用户没提的条件，绝对不要加。不要替他决定预算、年份、座位数。
说了"左右"就**只填 *_near** —— 那是排序偏好，不是筛选条件。
口语车系名（X系/X级/Model N）用 family + brand，**不要**硬编成 base_model ——
库里的精确车系键不含这些家族名，写进去会查不到。
"""

#: 模型可能用的同义键名 → 本系统的规范键名。
#: **必须在白名单过滤之前过一遍** —— 只靠 prompt 约束模型是不可靠的（实测 M3 有
#: 约 4/5 的次数把车型写成 `model`，被白名单当幻觉字段丢掉，导致「搜阿尔法」变成
#: 全库扫描）。prompt 是软约束，这里是硬兜底。
LLM_KEY_ALIASES: dict[str, str] = {
    # 车型
    "model": "base_model",
    "car_model": "base_model",
    "model_name": "base_model",
    "vehicle_model": "base_model",
    "carmodel": "base_model",
    # 品牌
    "brand_name": "brand",
    "car_brand": "brand",
    "make": "brand",
    # 年份
    "year_from": "year_min",
    "year_to": "year_max",
    "min_year": "year_min",
    "max_year": "year_max",
    "year_start": "year_min",
    "year_end": "year_max",
    # 价格
    "price_from": "price_min",
    "price_to": "price_max",
    "min_price": "price_min",
    "max_price": "price_max",
    "price_range": "price_max",
    "budget": "price_max",
    "budget_max": "price_max",
    # 模糊锚点（"左右"语义）：模型会自造键名，同样要归一。
    # **不能漏** —— 漏了就被白名单当幻觉字段丢掉，"左右"偏好静默消失。
    "price_around": "price_near",
    "price_approx": "price_near",
    "price_about": "price_near",
    "near_price": "price_near",
    "approx_price": "price_near",
    "price_target": "price_near",
    "price_ideal": "price_near",
    "year_around": "year_near",
    "year_approx": "year_near",
    "year_about": "year_near",
    "near_year": "year_near",
    "approx_year": "year_near",
    "year_target": "year_near",
    # 其它
    "seat": "seats",
    "seat_count": "seats",
    # 车身类型：模型爱把车型类别写成 body/type/car_type，同样要归一 ——
    # 漏了就被白名单当幻觉字段丢掉，"SUV/房车"这类条件静默消失。
    "body": "body_type",
    "bodytype": "body_type",
    "body_style": "body_type",
    "car_type": "body_type",
    "vehicle_body": "body_type",
    "keyword": "model_keyword",
    "mileage": "mileage_max",
    "mileage_limit": "mileage_max",
    "hand": "hand_max",
    "owners_max": "hand_max",
    "trans": "transmission",
    "fuel": "fuel_type",
    "import": "import_type",
    "order_by": "sort",
    "sort_by": "sort",
}


def normalize_llm_keys(data: dict) -> dict:
    """把模型用的同义键名换成规范键名。

    两条规则（顺序不能反）：
      1. 别名键先落地（`model`/`car_model` → `base_model` 等），让同义写法有归宿；
      2. 规范键再覆盖 —— 但**只在它自己不是 None 时**覆盖。

    第 2 条的 `is not None` 是必需的：模型偶尔会同时吐出
    `{"base_model": null, "model": "ALPHARD"}`。若无条件覆盖，第 1 步刚填好的
    ALPHARD 会被 `null` 冲掉，而下游第 317 行又按 `v is not None` 过滤，结果这个
    字段彻底消失 —— 用户看到"模型没给出车型"。语义上「显式 null」= 模型没表态，
    不该有权否决别名键里的有效值。
    """
    out: dict = {}
    for k, v in (data or {}).items():
        if v is None:
            continue
        out[LLM_KEY_ALIASES.get(k, k)] = v
    for k, v in (data or {}).items():
        if k in ALLOWED_LLM_FIELDS and v is not None:
            out[k] = v   # 规范键优先（仅当有值）
    return out


SOFT_HINT = "（用户只是随口问问，不要替他加任何筛选条件。）"


# ---------------------------------------------------------------------------
# 车身类型类目词（规则兜底路径 + 「七人車」口径兜底共用）
# ---------------------------------------------------------------------------
#: (词, 码)。长词优先由下面的正则保证（按长度降序拼 alternation）。
#: **刻意不收** 「客貨車 / van仔」—— §三 已定：与既有车系别名（客貨車→HIACE）打架，
#: 且那本就不是私家车业务范畴，混进来只会制造语义倒退。
#: 「越野車/越野车」**必须带车字后缀**：不然会命中品牌别名「越野路華」(LAND ROVER)，
#: 把品牌查询误判成车型查询。
_BODY_WORD_RULES: tuple[tuple[str, str], ...] = (
    ("七人車", "MPV"), ("七人车", "MPV"),
    ("保姆車", "MPV"), ("保姆车", "MPV"),
    ("商務車", "MPV"), ("商务车", "MPV"),
    ("MPV", "MPV"),
    ("越野車", "SUV"), ("越野车", "SUV"),
    ("吉普", "SUV"),
    ("SUV", "SUV"),
    ("房車", "SEDAN"), ("房车", "SEDAN"),
    ("轎車", "SEDAN"), ("轿车", "SEDAN"),
    ("掀背", "HATCHBACK"), ("揭背", "HATCHBACK"),
    ("兩廂", "HATCHBACK"), ("两厢", "HATCHBACK"),
    ("旅行車", "WAGON"), ("旅行车", "WAGON"), ("旅行版", "WAGON"),
    ("開篷", "CONVERTIBLE"), ("开篷", "CONVERTIBLE"),
    ("開蓬", "CONVERTIBLE"), ("开蓬", "CONVERTIBLE"),
    ("敞篷", "CONVERTIBLE"),
    ("轎跑", "COUPE"), ("轿跑", "COUPE"),
    ("跑車", "COUPE"), ("跑车", "COUPE"),
)

#: 全词扫描正则：按词长降序拼 alternation（长词优先），ASCII 词不区分大小写。
_BODY_WORD_RE = re.compile(
    "|".join(re.escape(w) for w, _ in
             sorted(_BODY_WORD_RULES, key=lambda kv: -len(kv[0]))),
    re.IGNORECASE,
)
_BODY_WORD_MAP: dict[str, str] = {w.upper(): code for w, code in _BODY_WORD_RULES}

#: 「七人車」专用（粤语口语，含繁简）。用于口径兜底：它是车型类别，不是座位数。
_SEVEN_SEATER_RE = re.compile(r"七人\s*[車车]")


def _mentions_7seater_car(text: str) -> bool:
    """原文是否出现「七人車 / 七人车」（车型类别语义）。"""
    return bool(_SEVEN_SEATER_RE.search(text or ""))


def _scan_body_type(text: str) -> str | None:
    """从原文扫出车身类型码（长词优先，命中即返回）。

    同时只命中一个词是常态；万一原文含两个类型词（"SUV 还是 房车"），
    取**最靠前出现**的那个 —— 第一诉求优先，跟「左右」锚点取最靠前同一口径。
    """
    if not text:
        return None
    best: tuple[int, str] | None = None
    for m in _BODY_WORD_RE.finditer(text):
        code = _BODY_WORD_MAP.get(m.group(0).upper())
        if code is None:
            continue
        if best is None or m.start() < best[0]:
            best = (m.start(), code)
    return best[1] if best else None


@dataclass
class ParseResult:
    spec: SearchSpec
    source: str                      # 'llm' | 'fallback' | 'mixed'
    notes: list[str] = field(default_factory=list)
    raw_llm: dict | None = None      # 保留模型原始输出，便于排查
    #: 多组条件（2026-09-27 多车混输支持）：「威尔法+埃尔法」/「A或B」拆成多组，
    #: 上层（API/MCP）对每组各查一次、结果带组标签合并。单查询时长度为 1。
    specs: list[SearchSpec] = field(default_factory=list)
    #: 每组的人话标签（如「14年威尔法」），与 specs 一一对应
    labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.specs:
            self.specs = [self.spec]
        if len(self.labels) < len(self.specs):
            self.labels += [""] * (len(self.specs) - len(self.labels))


# ---------------------------------------------------------------------------
# 车型/品牌的本地落地
# ---------------------------------------------------------------------------

_DISP_RE = re.compile(r"(?<![\d.])(\d\.\d)(?!\d)")
"""排量提取：只认「独立的一位小数」，两侧都不能紧邻数字或小数点。

为什么不用 `\\b`：`\\b` 的边界定义是「word char 与非 word char 之间」，而 `.`
不是 word char —— 于是 `\\b(\\d\\.\\d)\\b` 里 dot 两侧根本不构成边界，整个 pattern
靠的是首尾 `\\d` 与相邻字符的关系，实测会把 `2013.5` 里的 `13.5` 也切出来。
改用否定环视后：`3.5` / `2.0T`(→2.0) 命中，`2013.5` / `3.55` / `.3.5` 不命中。
"""


def resolve_model_target(
    raw_model: str | None,
    raw_brand: str | None,
    ctx: SearchContext,
) -> tuple[str | None, str | None, str | None, str | None]:
    """把模型给的车型名落到库内真实键。

    Returns: (base_model, brand, model_keyword, displacement)

    判定顺序（能等值就不模糊）：
      1. 词表原文/压平匹配，且该键在库里真的存在 → base_model
      2. 别名表（阿尔法→ALPHARD）→ base_model，同样要存在性校验
      3. 库里存在同名键（模型自己给对了英文名）→ base_model
      4. 都不行 → 退 model_keyword 模糊匹配（**不返回空结果**）
    """
    displacement = None
    if raw_model:
        m = _DISP_RE.search(raw_model)
        if m:
            displacement = m.group(1)

    base_model = None
    if raw_model:
        text = str(raw_model).strip().upper()
        head = text.split(" ")[0]
        hit = ctx.vocab.match(text) or ctx.vocab.match_compact(text)
        if hit and hit[0] in ctx.models:
            base_model = hit[0]
        if base_model is None:
            alias_base, alias_brand = resolve_alias(raw_model)
            # 别名指向的车系库里没有时**不硬用**，宁可退模糊匹配也不要空结果
            if alias_base and alias_base in ctx.models:
                base_model = alias_base
            if alias_brand and not raw_brand:
                raw_brand = alias_brand
        # 别名表（种子 + 生成）含**同音错字/繁简**兜底：模型把「步威」写成「布威」
        # 时，静态表查不到，但这里能靠无调拼音命中。
        if base_model is None:
            ctx_base = ctx.resolve_model_alias(raw_model)
            if ctx_base:
                base_model = ctx_base
        if base_model is None and text in ctx.models:
            base_model = text
        # 排量后缀（'ALPHARD 3.5'）不影响车系，取首词元再试一次
        if base_model is None:
            hit = ctx.vocab.match(head) or ctx.vocab.match_compact(head)
            if hit and hit[0] in ctx.models:
                base_model = hit[0]
            elif head in ctx.models:
                base_model = head
        # 模型有时把**品牌名**填进车型槽位（'福士' → base_model='VOLKSWAGEN'）。
        # 这个值在车系里归一不了，会退化成 model_keyword 模糊匹配，而 car_model 里
        # 根本没有品牌名 → 静默返回 0 条。实测 VOLKSWAGEN / BMW / MERCEDES-BENZ /
        # TOYOTA 四个全部 0 条（只有 LEXUS 侥幸不为 0，因为车名里恰好含这个词）。
        # 值本身是库内已知品牌，就顺手当品牌用 —— 比返回空结果正确得多。
        if base_model is None and not raw_brand:
            for cand in (text, head):
                if cand in ctx.brands:
                    raw_brand = cand
                    break

    brand = None
    if raw_brand:
        b = str(raw_brand).strip().upper()
        if b in ctx.brands:
            brand = b
        else:
            _, alias_brand = resolve_alias(raw_brand)
            if alias_brand and alias_brand in ctx.brands:
                brand = alias_brand
            elif b in BRAND_ALIASES.values() and b in ctx.brands:
                brand = b

    keyword = None
    if base_model is None and raw_model:
        raw_text = str(raw_model).strip()
        # 模型给的车型名恰好**就是刚解析出的品牌**（'福士' → 'VOLKSWAGEN'）时不能再当
        # 关键词：car_model 里没有品牌名，模糊匹配只会返回 0 条。这是 LLM 模式偶发
        # 空结果的成因，`eval_search.py --mode llm` 抓到过（用例「福士」）。
        if not brand or raw_text.upper() != brand:
            keyword = raw_text

    return base_model, brand, keyword, displacement


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def parse_query(
    query: str,
    ctx: SearchContext,
    llm: LLMClient | None = None,
) -> ParseResult:
    notes: list[str] = []
    raw_llm: dict | None = None      # 模型**原生**输出，筛过白名单前的，只给排查用
    groups: list[dict] = []          # 每组的白名单后数据

    if llm is not None and llm.configured:
        try:
            # 解析用 parse_temperature（默认 0.0，config.llm 可调；实测 temp=0.2
            # 时 5/12 次把 LM350 截成 'LM'）。兼容注入式假模型（测试桩无 cfg
            # 属性、签名可能不带 temperature）—— 没有	cfg 就不传该参数。
            _cfg = getattr(llm, "cfg", None)
            _tkw = {"temperature": _cfg.parse_temperature} if _cfg is not None else {}
            raw_llm = llm.chat_json(SYSTEM_PROMPT, f"用户查询：{query}\n{SOFT_HINT}", **_tkw)
            # 批量协议：{"queries": [组1, 组2...]}；兼容旧单对象格式（裸 dict）。
            raw_groups = raw_llm.get("queries") if isinstance(raw_llm, dict) else None
            if not (isinstance(raw_groups, list) and raw_groups
                    and all(isinstance(g, dict) for g in raw_groups)):
                raw_groups = [raw_llm if isinstance(raw_llm, dict) else {}]
            # ① 键别名归一（每组都过；实测 M3 约 4/5 次把车型写成 model，必须硬兜底）
            # ② 白名单过滤：幻觉字段一律丢掉
            for g in raw_groups:
                norm = normalize_llm_keys(g)
                norm = {k: v for k, v in norm.items()
                        if k in ALLOWED_LLM_FIELDS and v is not None}
                groups.append(norm)
            if not any(groups):
                stranger = sorted(set(raw_llm) - ALLOWED_LLM_FIELDS) if isinstance(raw_llm, dict) else []
                notes.append(
                    "模型没给出可用字段"
                    + (f"（不认识的键 {stranger}）" if stranger else "（返回了空 JSON）")
                    + "，已改用规则解析"
                )
        except LLMError as e:
            notes.append(f"模型解析失败，已降级为规则解析：{e}")
            groups = []
    else:
        notes.append("未配置模型，用规则解析（复杂语义会解不准）")

    if not groups:
        return ParseResult(spec=rule_based_parse(query, ctx), source="fallback",
                           notes=notes, raw_llm=raw_llm)

    # 输入里的**已知中文别名**（如「布威」→STEPWGN）：确定性证据，优先于模型对生僻
    # 叫法的猜测（实测「找一台本田布威」被 M3 猜成 Honda BR-V）。只对单组查询生效 ——
    # 多组时同一个别名会被错配到别的组（「布威或阿尔法」里 布威 会覆盖 ALPHARD 那组）。
    query_alias = ctx.find_model_alias(query) if len(groups) == 1 else None

    specs: list[SearchSpec] = []
    labels: list[str] = []
    for data in groups:
        label = str(data.get("label") or "").strip()[:16]
        spec = _group_to_spec(data, query, ctx, notes, query_alias)
        if spec is not None:
            specs.append(spec)
            labels.append(label)

    if not specs:                     # 每组都空 → 与历史行为一致：降级规则
        return ParseResult(spec=rule_based_parse(query, ctx), source="fallback",
                           notes=notes, raw_llm=raw_llm)
    return ParseResult(spec=specs[0], specs=specs, labels=labels,
                       source="llm", notes=notes, raw_llm=raw_llm)


def _group_to_spec(data: dict, query: str, ctx: SearchContext,
                   notes: list[str], query_alias: str | None = None) -> SearchSpec | None:
    """把模型输出的一组条件（已过白名单）转成 SearchSpec。空组返回 None。

    `query_alias`：输入原文里命中的**已知中文别名**所对应的车系（由 parse_query 单组
    时预先解析）。它是确定性证据，会覆盖模型给的车型 —— 模型对生僻叫法会猜错。
    """
    if not data:
        return None

    # 车型定位：模型可能同时给 base_model（可能被截断，如 'LM'）和
    # model_keyword（可能是完整型号 'LM350'）。**两个都试**，base_model 归一
    # miss 时再拿 keyword 去归一 —— 只取第一个会把正确的那个丢掉
    # （2026-09-27 审核实测：脏 base_model='LM' + 对 keyword='LM350' 的组合）。
    bm_raw, kw_raw = data.get("base_model"), data.get("model_keyword")
    base_model, brand, keyword, displacement = resolve_model_target(
        bm_raw or kw_raw, data.get("brand"), ctx)
    if bm_raw and kw_raw and base_model is None:
        base_model, brand2, keyword, displacement = resolve_model_target(
            kw_raw, data.get("brand") or brand, ctx)
        if base_model:
            brand = brand or brand2
            kw_raw = None
            notes.append(f"模型给的车型 {bm_raw!r} 在库里找不到，已用关键词 "
                         f"{data['model_keyword']!r} 识别出车系 {base_model!r}")
    if data.get("model_keyword") and base_model:
        # 模型给了关键词但也解析出了车系 → 以车系为准，关键词丢弃
        keyword = None
    if data.get("base_model") and base_model is None and not keyword:
        notes.append(f"模型给的车型 {data['base_model']!r} 在库里找不到，已退化为模糊匹配")
        keyword = str(data["base_model"])

    # 输入里的已知中文别名优先（见 parse_query 的 query_alias 说明）。放在短词防呆之前：
    # 命中别名就把模型猜错的英文关键词一起丢掉，不留给下游。
    if query_alias and base_model != query_alias:
        guess = bm_raw or kw_raw
        notes.append(
            f"按输入中的中文别名识别出车系 {query_alias!r}"
            + (f"（模型给的是 {guess!r}）" if guess else "")
        )
        base_model = query_alias
        keyword = None

    # ── 短关键词/截断关键词防呆（P3）───────────────────────────────────
    # 模型偶发把 'LM350' 截成 'LM'（temp=0.2 实测 5/12 次；temp=0 后仍有
    # 1/20 次截成 '350'）。'%LM%' / '%350%' 模糊匹配会把 LM500 / IS350 等
    # 其它车型卷进来 —— 型号污染，比查不到更糟。两类残缺一律不信任：
    #   ① 长度 < 3；
    #   ② 截断检测：keyword 是原文里某个更长型号词的**不到 2/3** 的片段
    #      （'350' vs 'LM350'：3 < 5×2/3≈3.3 → 残缺）。完整出现的不拦
    #      （'730' 在原文就是 730）。
    # 丢弃后用规则层从原文重新抓车型补救（门控已放开，有品牌也能扫键）。
    if keyword is not None:
        kw = str(keyword).strip()
        truncated = False
        is_full_token = False
        if re.fullmatch(r"[A-Za-z0-9]+", kw):
            # token 集合 = 原文分词 + **空格相连的 ASCII 段组压平**（'LM 350'
            # 分词是 'LM'/'350' 两截，'350' 与第二截相等不算截断，压平成
            # 'LM350' 才暴露它是残缺片段）。只压平 ASCII 段组：整句压平会把
            # 中文带进来（'宝马730' 压平后 '730' 成了它的"片段"，把完整的
            # 730 误判成截断 —— 实测教训）。
            tokens = re.findall(r"[A-Z0-9]+", query.upper())
            for grp in re.findall(r"[A-Z0-9]+(?:\s+[A-Z0-9]+)*", query.upper()):
                flat = re.sub(r"\s+", "", grp)
                # 压平组**只有本身是已知车型**时才参与截断判定（Fix-1，2026-09-28）：
                # 原先无条件压平，'740 2018' → '7402018' 让**完整的** '740' 被当成
                # 它的截断片段（'740' 是前缀且 3 < 8×2/3）→ 真型号被丢弃 →
                # 全系列宝马顶包（实测 229 台，Top 是 X3/X5）。
                # 实测 '7402018' ∉ models、vocab.match_compact 也不命中，故 gating 后
                # '740' 不再被误判；'LM 350' → 'LM350' ∈ models，仍能拦下残缺的 '350'
                # ——**原有防线一字未减**。副作用：库外长尾车型的空格写法漏判截断
                # → 关键词保留 → 查宽，是安全方向。
                if flat not in tokens and (flat in ctx.models
                                           or ctx.vocab.match_compact(flat)):
                    tokens.append(flat)
            # 完整 token 判定必须是**精确相等**（list 的 in 是相等比较，不是子串）：
            # 写成子串会把「530」的片段「30」也当完整词放行（实测 '%30%' 命中 767 台）。
            is_full_token = kw.upper() in tokens
            for token in tokens:
                if kw.upper() in token and len(kw) < len(token) * 2 / 3:
                    truncated = True
                    break
        # 丢弃条件（2026-09-28 收紧）：长度 <3 的一律不信任，**除非**它既是原文的
        # 完整 token、又至少含一个字母。Z8/M8/X1 这类真实两字符型号据此放行；
        # 纯数字 token（「30萬」的 30）与截断片段（LM350 的 LM）照旧丢弃 ——
        # 前者模糊匹配实测命中 767 台、后者会卷进其它型号。
        too_short = len(kw) < 3 and not (is_full_token and any(c.isalpha() for c in kw))
        if too_short or truncated:
            notes.append(f"模型给出的关键词 {kw!r} 疑似残缺（过短或截断），已丢弃")
            keyword = None
            try:
                rescue = rule_based_parse(query, ctx)
                if rescue.base_model:
                    base_model = rescue.base_model
                    notes.append(f"已按规则从原文重新识别出车型 {rescue.base_model!r}")
            except Exception:   # noqa: BLE001 —— 补救失败不影响主流程（最多查宽）
                pass

    # 模糊量守卫：原文说"左右"时，不许让区间溜进来，也不许让偏好丢掉。
    # 以**原文**为准而不是模型 —— 模型把"五十万左右"写成 price_max=500000 的话，
    # 硬过滤会静默删掉 60 万的车，而用户只是想让他们排后面。
    for near_key, hard_keys, kind in (
        ("price_near", ("price_min", "price_max"), "price"),
        ("year_near", ("year_min", "year_max"), "year"),
    ):
        anchor, has_bound = _rule_near_anchor(query, kind)
        if anchor is None:
            continue    # 原文没说"左右" → 不插手模型给的任何东西
        data.setdefault(near_key, anchor)
        if has_bound:
            # 原文另有**明确**边界词（"五十万左右，不要超过八十万"）→ 那个区间是真的，
            # 不能丢。锚点照样补上，两者共存是对的。
            continue
        dropped = {k: data.pop(k) for k in hard_keys if k in data}
        if dropped:
            notes.append(
                f"「左右」是排序偏好不是筛选，已忽略模型多写的 {dropped}"
            )

    payload: dict[str, Any] = {
        "raw_query": query,
        "base_model": base_model,
        "brand": brand,
        "model_keyword": keyword,
        "displacement": displacement or data.get("displacement"),
        "sort": data.get("sort") or "score",
        # limit 不在这里 int() —— 模型给 "五条" 时 int() 会抛 ValueError，
        # 一路穿透到 API 变成 500（同样是脏值，走 /search/spec 却是 400）。
        # 统一交给 SearchSpec.__post_init__ 收敛：转不了退回 DEFAULT_LIMIT。
        "limit": data.get("limit") if data.get("limit") is not None else DEFAULT_LIMIT,
    }
    # 关键词身份盖章（Fix-2，2026-09-28）：'740' 这类**库内某键的前缀**是真型号，
    # 零命中放宽时不许当噪声丢弃（丢了就是顶包）。engine 手上没有 SearchContext，
    # 判不出前缀，故由解析层盖章；engine 侧再 OR 上自己的「含字母 ASCII 码」判定。
    if keyword:
        payload["keyword_is_identity"] = bool(ctx.is_model_code(keyword))
    # 同款变体展开（P1）：LM350 → [LM350, LM350H]。映射在 SearchContext 里
    # 预计算（构建规则见 context.build_variant_map 的 docstring）。
    if base_model:
        payload["base_models"] = [base_model] + ctx.variant_map.get(base_model, [])
    # family 透传（spec.__post_init__ 做白名单收敛）。base_model 命中时不用 family：
    # 精确车系与家族前缀同时 AND 会出现「ALPHARD 且以 7 开头」的空集
    if data.get("family") and not base_model:
        payload["family"] = data["family"]
    for k in (
        "year_min", "year_max", "year_near", "price_min", "price_max", "price_near",
        "seats", "body_type", "vehicle_type", "transmission", "fuel_type", "import_type",
        "hand_max", "mileage_max", "max_price_ratio", "china_plate", "dealer",
    ):
        if data.get(k) is not None:
            payload[k] = data[k]
    if data.get("exclude_anomaly") is not None:
        payload["exclude_anomaly"] = bool(data["exclude_anomaly"])

    # ── 「七人車」口径兜底（2026-09-30，09-24 P0 规矩：prompt 是软约束）──────
    # 香港语义里「七人車」是**车型类别**(MPV)，不是座位数。模型完全可能同时吐
    # {"body_type":"MPV","seats":7}（字面「七人」就是 7）—— 那样会叠一层 seats=7，
    # 把 7 座 SUV 一起卷进来，正是本次要修的错。故以**原文**为准：原文出现
    # 「七人車/七人车」且最终 body_type 是 MPV 时，丢掉 seats。
    # **只丢 seats，不丢其它条件**（"七人车 3.5 排量" 里的排量照留）。
    # 不碰「七座」：那是真座位数诉求（"七座的 SUV" 里 seats=7 必须保留）。
    if _mentions_7seater_car(query) and payload.get("body_type") == "MPV" \
            and payload.get("seats") is not None:
        dropped_seats = payload.pop("seats")
        notes.append(
            f"「七人車」是车型类别（MPV），已忽略模型多写的座位数 seats={dropped_seats}"
        )

    return SearchSpec.from_dict(payload)


# ---------------------------------------------------------------------------
# 规则解析（降级路径）
# ---------------------------------------------------------------------------

#: 中文数字 → 阿拉伯数字（只覆盖到十位，车牌/预算场景够用）
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

#: 单位的乘法因子
_UNIT_FACTOR = (
    ("億", 100_000_000), ("亿", 100_000_000),
    ("萬", 10_000), ("万", 10_000), ("萬", 10_000), ("w", 10_000), ("W", 10_000),
    ("k", 1_000), ("K", 1_000), ("千", 1_000),
)


def _cn_to_int(text: str) -> int | None:
    """把 '7' / '七' / '七座' / '二十五' 里的数字取出来。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    # 二十五 / 三十 / 十五
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return None


#: 紧跟在数字后面的这些单位说明**那不是钱**：里程(5万公里) / 排量(2000cc) /
#: 座位(7座) / 年份(2015年) / 手数(2手)。
_NOT_MONEY_AFTER = re.compile(r"\s*(?:公里|千米|里|km|KM|Km|kM|cc|CC|座|坐|年|手|匹)")


def _extract_amount(text: str) -> list[tuple[int, int]]:
    """抓出所有「数字+单位」金额，返回 [(金额, 位置)]。

    支持 '50萬' / '50万' / '30萬' / '二十五萬' / '500000' / '500k'。

    **带非金额单位的必须跳过。** 漏了这个判断，'里程5万公里以内的阿尔法' 会被当成
    "价格 ≤ 5 万"，用户压根没提预算却凭空多出一条价格过滤，候选从 118 台塌成 1 台
    —— 而且 P@5 这类指标看不出来（返回的车都满足剩下的条件，照样满分）。
    """
    out: list[tuple[int, int]] = []
    # 阿拉伯数字 + 单位
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([萬万億亿千kwKW])?", text):
        if _NOT_MONEY_AFTER.match(text[m.end():]):
            continue
        num = float(m.group(1))
        unit = m.group(2)
        factor = 1
        if unit:
            for u, f in _UNIT_FACTOR:
                if unit == u:
                    factor = f
                    break
        out.append((int(num * factor), m.start()))
    # 中文数字 + 萬
    for m in re.finditer(r"([零一二两三四五六七八九十]+)\s*([萬万])", text):
        if _NOT_MONEY_AFTER.match(text[m.end():]):
            continue
        n = _cn_to_int(m.group(1))
        if n:
            out.append((n * 10_000, m.start()))
    out.sort(key=lambda x: x[1])
    return out


def _window(text: str, pos: int) -> str:
    """取数字附近的文字，用来判断是上限还是下限（左多看 10 字，右多看 8 字）。"""
    return text[max(0, pos - 10) : pos + 8]


_MAX_WORDS = (
    "以內", "以内", "以下", "樓下", "楼下", "唔好過", "不超过", "之内", "之內",
    # 「超过」单独出现是**下限**（"超过三十万" = 三十万以上），所以不能把裸的
    # "超过" 放进这张表；但带上否定词就是上限。漏了这三条，"五十万左右，不要超过
    # 八十万" 会被判成"没有明确边界"，硬边界随模糊量一起被丢掉。
    "不要超过", "不要超過", "唔好超過", "不得超过", "不得超過",
)
#: 下限词。**繁简要成对写全**：「以后」和「以後」漏掉任一个，都会让"2018年以後"
#: 一个字都抽不出来，静默退化成"不限年份"（评测里表现为 29 条违例）。
#: ⚠️ 顺序要紧：`_MAX_WORDS` 必须先判 —— "不要超过三十万" 同时含「不要超过」和
#: 「超过」，先判上限才对。
_MIN_WORDS = (
    "以上", "超过", "超過", "起", "樓上", "楼上", "打後", "打后", "之後", "之后",
    "以後", "以后", "過後", "过后", "其後", "其后", "起錶",
)

#: 模糊量词：「50 万**左右**」「**大约** 30 万」「2015 年**前后**」。
#: 命中表示用户只是"说个大概"，于是**只产生软锚点 `*_near`，绝不产生 min/max 硬过滤**。
#: 为什么不能做成区间：区间会把 39 万、62 万的车直接从结果里删掉，而"左右"的本意
#: 只是"离得近的排前面"。价格容差按比例（Monroe 1971 实测可辨阈限恒为 15%，与
#: Weber–Fechner 定律 ΔI/I=const 一致）、年份按绝对年数 —— 见 engine.NEAR_BAND_*。
_AROUND_WORDS = ("左右", "上下", "前後", "前后", "大概", "差不多", "约", "約")

#: 年份：1950-2049。**不能用 `\b`** —— 「2015年」里「年」在 Python 眼里也是 word
#: character，`\b` 在数字后不成立，会导致年份一个字都抽不出来。
_YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")

#: 「数字 + 排量/功率单位」写法（'3000CC' / '2000KW'）—— 是排量/功率，不是型号。
#: **必须排在 `ctx.is_model_code` 之前**：词表的松匹配（`match_compact`）会把
#: '3000' 命中 HINO 的 '300' 前缀模式，使 `is_model_code` 误判为 True —— 实测
#: 「3000cc 宝马」会被锁成 `model_keyword='3000CC'` → 0 台 + 误导提示
#: （Fix-2b 排量守卫，2026-09-29；原以为「库里没有 3000 开头的键」能兜住，实测不成立）。
_QUANTITY_RE = re.compile(r"\d+(?:\.\d+)?(?:CC|KW|HP|PS|NM|KM)")

#: 型号**形状**：字母 ≤3 + 数字 ≤4（'M760' / 'C200' / 'RS6'）。规则层没有世界知识，
#: 但「库外型号」多为此形状 —— 用它把 '宝马M760' 补成身份关键词，与 LLM 路径行为
#: 一致（诚实 0）。长度守卫 ≥3 挡掉 'V6' / 'W12' 这类引擎排布短词；字母 >3 的
#: 'SDRIVE18IA' 天然不命中（2026-09-29，用户确认的可选项）。
_CODE_SHAPE_RE = re.compile(r"[A-Z]{1,3}[0-9]{1,4}")

#: 规则路径的排量提取（LLM 不可用时的降级）。**必须带排量专属线索**（L/升/T/cc
#: 或前置「排量」字样），不能用裸 `\d\.\d` —— 那会把「3.5萬」的预算当成排量。
#: 排量值是档位（3.5 / 2.0 / 3500），engine._displacement_liters 容差 ±0.25L 兜噪声。
_DISP_RULE_RES = (
    re.compile(r"排量[：:\s]*(\d{3,4})"),                                   # 排量3500 / 排量：3500
    re.compile(r"(?<![\d.])(\d\.\d)\s*(?:[Ll]|升|T)(?![A-Za-z0-9])"),       # 3.5L / 2.0T / 3.5升
    re.compile(r"(?<![\d.])(\d{3,4})\s*[cC][cC](?![A-Za-z0-9])"),           # 3500cc / 2000CC
)


def _rule_displacement(query: str) -> str | None:
    """无 LLM 时从原文抽排量（'3.5L排量' / '3500cc' / '2.0T'）。抽不到返回 None。"""
    for pat in _DISP_RULE_RES:
        m = pat.search(query)
        if m:
            return m.group(1)
    return None


def _rule_near_anchor(query: str, kind: str) -> tuple[float | None, bool]:
    """从原文里按规则抽出模糊锚点。返回 `(锚点, 是否还有明确边界词)`。

    与 `rule_based_parse` 用同一套词表和窗口，保证两条路径口径一致。

    之所以 LLM 路径也要跑这个：**模型对"左右"的写法不可信**。它可能把"五十万左右"
    写成 `price_max: 500000`（硬过滤，把 60 万的车删掉），也可能压根不给 `*_near`。
    原文是模糊量、模型却给了区间时，以**原文**为准。
    """
    if kind == "price":
        cands = [(float(amt), pos) for amt, pos in _extract_amount(query) if amt >= 10_000]
    else:
        cands = [(float(m.group(1)), m.start()) for m in _YEAR_RE.finditer(query)]

    anchor: float | None = None
    has_bound = False
    for value, pos in cands:
        around = _window(query, pos)
        if any(w in around for w in (*_MAX_WORDS, *_MIN_WORDS)):
            has_bound = True       # 用户真的划了硬边界，不当锚点
            continue
        if anchor is None and any(w in around for w in _AROUND_WORDS):
            anchor = value          # 多个模糊量时取最靠前的那个
    return anchor, has_bound


def rule_based_parse(query: str, ctx: SearchContext) -> SearchSpec:
    """无模型时的兜底：别名表找车系 + 正则抽条件。

    能做到：车系/品牌识别、预算上下限、年份下限、座位数、手数、里程、行水货、
    最常见排序词。做不到：「三十万左右的七座混能车」这类多条件模糊语义。
    """
    payload: dict[str, Any] = {"raw_query": query}
    up = query.upper()

    # --- 车型/品牌：扫**两张**别名表（只扫品牌表会漏掉「阿尔法」「海狮」这些
    #     车型名 —— 它们在 MODEL_ALIASES 里）。按长度降序，避免「阿爾法」被「阿法」
    #     抢先，也避免「丰田」先于「丰田世纪」命中。
    all_aliases = sorted({**MODEL_ALIASES, **BRAND_ALIASES}, key=len, reverse=True)
    for alias in all_aliases:
        if alias.upper() in up:
            base_model, brand, keyword, _ = resolve_model_target(alias, None, ctx)
            if base_model:
                payload["base_model"] = base_model
            elif brand:
                payload["brand"] = brand
            break

    # --- 车型：再扫库内真实车系键（用户直接打英文名，或库里长尾车系）---
    # 边界用 (?<![A-Z0-9])…(?![A-Z0-9]) 而不是 \b：\b 在「ALPHARD車」这种
    # 中英混排里失效（「車」在 Python 眼里也是 word character）。
    # 2026-09-27 放开门控（原来要求「无品牌」才扫）：「雷克萨斯LM350」会先命中
    # 品牌 LEXUS，导致 LM350 永远识别不出 —— P3 的规则补救依赖这一步能扫到车型
    # （键名字面匹配，品牌在场不会引入误命中：键不在原文就不会中）。
    if not payload.get("base_model"):
        # 双口径匹配：原文边界匹配 + **压平匹配**（去空格/连字符后比，与
        # vocab.match_compact 同口径）——「LM 350」这种带空格写法原文匹配
        # 必然落空，LLM 降级时车型条件会静默消失（2026-09-27 审核实锤）
        up_compact = re.sub(r"[\s\-]+", "", up)
        for model in sorted(ctx.models, key=len, reverse=True):
            if len(model) < 2 or not any(c.isalpha() for c in model):
                continue  # 跳过 '3.5' / '5.5' / '2015' 这类从脏数据兜出来的纯数字键
            # len<3 → len<2（Fix-4，2026-09-28）：两位真实键（M3/X5/Z4/A6…）原先被
            # 一刀切跳过，无 LLM 时「宝马M3」「奥迪A6」退化成全品牌 1,959 台。
            # 纯数字键仍由 `not any(c.isalpha())` 拦住（'30'/'2015' 不会误命中），
            # 单字符脏键仍被 len<2 拦住。风险面实测为 **97 个两位含字母键**
            # （含 EV/GT/RS/AC 等泛型短词），靠 `(?<![A-Z0-9])…(?![A-Z0-9])`
            # 边界断言挡住子串误命中；用户单独喊「EV」这类词会被当成车系——
            # 属可接受代价（宁可认出也不漏识），已在方案文档 §5 Fix-4 记录。
            if (re.search(rf"(?<![A-Z0-9]){re.escape(model)}(?![A-Z0-9])", up)
                    or re.search(rf"(?<![A-Z0-9]){re.escape(model.replace(' ', ''))}(?![A-Z0-9])",
                                 up_compact)):
                payload["base_model"] = model
                break

    # --- 纯数字车系（911 / 718 / 458）只认「整句就是它」，避免「300萬以內」误命中 ---
    if not payload.get("base_model"):
        stripped = up.strip()
        if stripped in ctx.models:
            payload["base_model"] = stripped

    # --- 别名表（种子 + LLM 生成）整句匹配 ---
    # 含同音错字/繁简兜底：「布威」→（拼音 buwei）→ 步威 → STEPWGN。静态 MODEL_ALIASES
    # 已在上面扫过；这里补上生成别名与拼音匹配。放在品牌之前，避免同一句里品牌抢先。
    if not payload.get("base_model"):
        bm = ctx.find_model_alias(query)
        if bm:
            payload["base_model"] = bm

    # --- 品牌：扫品牌别名 ---
    if not payload.get("base_model") and not payload.get("brand"):
        for alias, en in sorted(BRAND_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
            if alias in query and en in ctx.brands:
                payload["brand"] = en
                break

    # --- 排量（降级路径；LLM 路径由 resolve_model_target 的 _DISP_RE 产出）---
    # 没有这一步时，「3.5L排量」在无 key / LLM 失败时会静默丢掉排量条件。
    _disp = _rule_displacement(query)
    if _disp:
        payload["displacement"] = _disp

    # --- 金额：明确边界 → 硬过滤；「左右」→ 软锚点（顺序不能反：说了"以内"就是
    #     硬边界，比模糊量优先；两者都不沾才轮不到金额条件）
    for amt, pos in _extract_amount(query):
        if amt < 10_000:      # 座位数/年份/排量不是钱
            continue
        around = _window(query, pos)
        if any(w in around for w in _MAX_WORDS):
            payload["price_max"] = min(payload.get("price_max", amt), amt)
        elif any(w in around for w in _MIN_WORDS):
            payload["price_min"] = max(payload.get("price_min", amt), amt)
        elif any(w in around for w in _AROUND_WORDS):
            # "五十万左右" → **只给软锚点，绝不写 price_max**。写死区间会把 39 万、
            # 62 万的车直接删掉，而"左右"的本意是"离得近的排前面"。
            # 多个模糊量时取最靠前的（"50 万左右"后面再蹦一个数，语义已经不清了）。
            payload.setdefault("price_near", amt)

    # --- 年份区间（「2016到2017年」「14-17年」）---
    # 现状只认单个年份，且靠「数字后紧跟『年』」判定裸年份 —— '2016到2017年' 里
    # 2016 后面是「到」不是「年」，于是被忽略，只剩 2017 被锁成**精确年**，
    # 语义从「区间」悄悄缩成「某一年」。区间空窗正是 B-2 的典型场景，
    # 规则路径必须先能表达区间，修复才有处落地（2026-09-28 审核补充）。
    interval: tuple[int, int] | None = None
    _iv = re.search(r"(?<!\d)(\d{2,4})\s*(?:到|至|~|～|—|–|-)\s*(\d{2,4})(?=\s*年)", query)
    if _iv:
        a, b = int(_iv.group(1)), int(_iv.group(2))
        if len(_iv.group(1)) == 2:
            a += 2000
        if len(_iv.group(2)) == 2:
            b += 2000
        if a > b:
            a, b = b, a
        if 1950 <= a <= 2049 and 1950 <= b <= 2049:
            interval = (a, b)
            payload["year_min"] = a
            payload["year_max"] = b
    iv_span: tuple[int, int] | None = _iv.span() if interval else None

    # --- 年份 ---
    # 同样不能用 \b：「2015年」里「年」是 word character，\b 在数字后不成立，
    # 会导致年份一个字都抽不出来。改用数字边界断言。
    # 两位年份（"17年"）单独补一张正则（+2000 归一）：口语里说 17 年就是 2017 年，
    # _YEAR_RE 只认四位会把它整个漏掉（LLM 不可用的降级路径下裸年份全丢）。
    year_hits: list[tuple[int, int, int]] = [(int(m.group(1)), m.start(), len(m.group(1)))
                                             for m in _YEAR_RE.finditer(query)]
    for m in re.finditer(r"(?<!\d)(\d{2})(?=年)(?!\d)", query):
        y2 = int(m.group(1)) + 2000
        # 未来年不收（「車齡30年」→ 2030 不是年份诉求）；两位年份天然只到 99，
        # 这里再按当前年截一次，避免「30年」这类车龄/数量词被锁成未来年份
        if 2000 <= y2 <= datetime.now().year:
            # 记原文长度 2（不是归一后 2017 的 4）：裸年份判定要拿它索引原文
            year_hits.append((y2, m.start(), 2))
    for y, pos, raw_len in sorted(year_hits, key=lambda t: t[1]):
        if iv_span and iv_span[0] <= pos < iv_span[1]:
            continue          # 已由区间表达（'2016到2017年'），不再缩成单年
        around = _window(query, pos)
        if any(w in around for w in _MIN_WORDS):
            payload["year_min"] = max(payload.get("year_min", y), y)
        elif any(w in around for w in _MAX_WORDS):
            payload["year_max"] = min(payload.get("year_max", y), y)
        elif any(w in around for w in _AROUND_WORDS):
            payload.setdefault("year_near", y)   # "2015年左右" → 软锚点，不是 year_min
        elif query[pos + raw_len:pos + raw_len + 1] == "年":
            # 裸年份（"17年的X"/"2015年威尔法"，数字后紧跟「年」且无左右/边界词）
            # = 精确要求（2026-09-27 定稿：「找17年的Model 3」曾被静默当成
            # "2017左右"参与排序，返回 19/20/21 年的车且不说明）。
            # 只认带「年」字的：裸数字（"编号2015"）不锁。
            payload["year_min"] = y
            payload["year_max"] = y

    # --- 车身类型类目词（规则兜底；放在车系/品牌之后，独立写 body_type）---
    # 与座位数**分开**：「七人車」是车型类别(MPV)，不是座位数(见下条)。
    # 不写 payload 时不动；命中即写 7 码之一（spec.__post_init__ 再收敛一次）。
    _body = _scan_body_type(query)
    if _body:
        payload["body_type"] = _body

    # --- 座位数 ---（只认「七座/N座/N坐」；「七人车」已在上面的 body 段消费，
    #     这里**不再**匹配 N人車 —— 否则「找台七人车」会 seats=7 + body_type=MPV
    #     双写，把 7 座 SUV 一起卷进来，正是本次要修的错。）
    seat = re.search(r"([0-9]+|[零一二两三四五六七八九十]+)\s*[座坐]", query)
    if seat:
        n = _cn_to_int(seat.group(1))
        if n and 2 <= n <= 30:
            payload["seats"] = n

    # --- 车系家族（「宝马7系/奔驰S级/A6」的口语说法）---
    # 捕获家族前缀（单字符或字母数字串），与 brand 组合成前缀匹配。只认
    # ASCII 家族符（数字/字母）：「车系」的「车」是汉字天然不匹配，「一系列」同理。
    fam = re.search(r"(?<![0-9A-Za-z])([0-9]|[一二三四五六七八])\s*-?\s*(?:系|級|级|series|SERIES)", query) \
        or re.search(r"(?<![0-9A-Za-z])([A-Za-z]{1,3})\s*-?\s*(?:級|级)", query)
    if fam and not payload.get("base_model"):
        candidate = str(_cn_to_int(fam.group(1)) or fam.group(1)).upper() \
            if fam.group(1) in "一二三四五六七八" else fam.group(1).upper()
        if re.fullmatch(r"[A-Z0-9 -]+", candidate):
            payload["family"] = candidate

    # --- 型号关键词补救（Fix-2b，2026-09-28 审核补充）---
    # 规则层原先**压根不产出 model_keyword**（只写 base_model/brand），于是「品牌 +
    # 非完整键的型号」这类查询直接丢掉型号条件：实测 `宝马740 2018年` 解析成
    # 「BMW + 2018」→ 命中 229 台全系列宝马（Top 是 X3/X5）→ 顶包；
    # `宝马740 50万以内` → 1,838 台。同样的成因，与 Fix-1/Fix-2 修的 LLM 路径
    # **不是同一条** —— 那两处对这条路径一点作用都没有。
    # '740' 不是库内完整键（库里是 740I/740LI/740LIA），上面的键扫描扫不到它，
    # 只能靠「是不是某键的前缀」把它补成 model_keyword。
    # 放在 family 之后：family 命中的查询（「宝马7系」）不该再叠一个关键词，
    # 两者 AND 起来容易空集。
    # 主判据是 `ctx.is_model_code`（等值键 / 键前缀 / 词表命中）—— 库内证据。
    # 库外型号（如 'M760'：库里既无 M760 也无 M760* 前缀）is_model_code 判不出，
    # 再用**形状**兜底（`_CODE_SHAPE_RE`，见下），使规则路径与 LLM 路径**行为一致**
    # （诚实 0），不再退化成品牌级 1,959 台（2026-09-29 起）。
    if not payload.get("base_model") and not payload.get("family"):
        for _m in re.finditer(r"[A-Z0-9]+", up):
            tok = _m.group(0)
            if len(tok) < 3 or tok in ctx.models:
                continue          # 完整键上面的扫描已处理；两位前缀歧义太大，不收
            # 排量/功率写法（'3000CC'）不是型号。必须排在 is_model_code **之前**：
            # 词表松匹配把 '3000' 命中 HINO 的 '300' 前缀，is_model_code 会误判 True
            # —— 实测「3000cc 宝马」被锁成 model_keyword='3000CC' → 0 台 + 误导提示。
            if _QUANTITY_RE.fullmatch(tok):
                continue
            # 单位前瞻：看 token **后至多 3 个字符**（允许空格），不是只看后 1 个 ——
            # '3000 cc'（数字与单位间有空格）后 1 个字符是空格，原先漏判。
            after = up[_m.end():_m.end() + 3].lstrip()
            if after[:1] and after[:1] in "萬万億亿千kKwW公里座坐年手匹":
                continue          # '50萬' 的 50、'2018年' 的 2018、'5萬公里' 的 5 都不是型号
            if after[:2] in ("CC", "KW", "HP", "PS", "NM", "KM"):
                continue          # '3000 cc' 这种空格分隔的排量写法
            if _YEAR_RE.fullmatch(tok):
                continue
            if ctx.is_model_code(tok):
                payload["model_keyword"] = tok
                payload["keyword_is_identity"] = True   # 真型号，放宽阶梯里不许丢
                break
            # 库外型号（规则层无世界知识，「字母+数字」是通用型号形状）：'宝马M760'
            # 库里既无 M760 也无 M760* 前缀，is_model_code 判不出 → 在此按形状补成
            # 身份关键词，与 LLM 路径**行为一致**（诚实 0 + 数字提示），不再退化成
            # 品牌级 1,959 台（2026-09-29）。长度 ≥3 已过滤 'V6' 这类短词；字母 >3 的
            # 'SDRIVE18IA' 天然不命中。
            if _CODE_SHAPE_RE.fullmatch(tok):
                payload["model_keyword"] = tok
                payload["keyword_is_identity"] = True
                break

    # --- 手数 ---
    # ⚠️ 先把「二手」整体剔掉，再匹配手数。
    # 「二手」是 used car 的**泛称**，不是「过户 2 次的车」—— 中文里没人用「二手」
    # 表示手数。但中文数字类 [一二两三四五]+手 会把它匹配成 hand_max=2，
    # 而手数字段覆盖率只有约 15% —— 一句最常见的「二手阿尔法」会静默把候选
    # 从全量砍到 15%（SQL 还要求 hand_count 存在）。实测踩过。
    # 剔掉而不是加否定断言：这样「二手 一手车」「二手2手车」里的真手数条件仍能命中。
    hand_text = query.replace("二手车", "").replace("二手車", "").replace("二手", "")

    if re.search(r"[零0]\s*手", hand_text):
        payload["hand_max"] = 0
    else:
        # 「手數：1」「手数2」——香港车源挂牌的常见写法，**名词在前数字在后**，
        # 只认「N手」会让这类查询静默丢掉手数条件（表现为候选暴涨、混进多手车）。
        hand_named = re.search(r"手[数數]\D{0,3}([0-9]+|[一二两三四五]+)", hand_text)
        hand = hand_named or re.search(r"([0-9]+|[一二两三四五]+)\s*手", hand_text)
        if hand:
            n = _cn_to_int(hand.group(1))
            if n is not None:
                payload["hand_max"] = n
        elif (
            "一手" in hand_text or "一手車" in hand_text
            # Fix-6（2026-09-28）：补「一部手」「首任車主」两种说法。
            # 实测描述语料里 '一部手' 0 条、'首任' 1 条（'一手' 165 条），
            # 所以这是**用户输入侧**的口语覆盖，不是提取侧的能力提升；
            # 优先级最低，不与两个 P0 混批次（见方案文档 §7）。
            or "一部手" in hand_text
            or "首任車主" in hand_text or "首任车主" in hand_text
        ):
            payload["hand_max"] = 1

    # --- 里程 ---
    # 中文数字也要认：「五万公里」只写 [0-9]+ 的话一个字都抽不出来，里程条件静默丢失
    # （表现为候选数暴涨、返回一堆高里程车，评测里看不出来）。
    mileage = re.search(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十]+)\s*([萬万])?\s*"
        r"(?:公里|千米|km|KM|Km|kM)",
        query,
    )
    if mileage:
        head = mileage.group(1)
        val = float(head) if head[0].isdigit() else float(_cn_to_int(head) or 0)
        if mileage.group(2):
            val *= 10_000
        if 1_000 <= val <= 1_000_000:
            payload["mileage_max"] = int(val)

    # --- 行/水货 ---
    if "水貨" in query or "水货" in query:
        payload["import_type"] = "水貨"
    elif "行貨" in query or "行货" in query:
        payload["import_type"] = "行貨"

    # --- 中港牌 ---(否定式先判:「没有中港」是排除,不是筛选)
    if re.search(r"沒有中港|无中港|沒中港|没中港", query):
        payload["china_plate"] = False
    elif "中港" in query:
        payload["china_plate"] = True

    # --- 车行/个人卖家 ---（否定/个人诉求先判:「不要车行」是排除,主用法）
    if re.search(
        r"不要车行|唔要車行|唔要车行|不要車行|排除车行|排除車行"
        r"|私人车主|私人車主|私人卖家|私人賣家|个人卖家|個人賣家|个人车主|個人車主"
        r"|一手车主|一手車主",
        query,
    ):
        payload["dealer"] = False
    elif re.search(r"只要车行|只要車行|车行货源|車行貨源|车行的车|車行的車", query):
        payload["dealer"] = True

    # --- 排序 ---
    if "最便宜" in query or "最平" in query or "最抵" in query:
        payload["sort"] = "price_asc"
    elif "最新" in query or "剛放" in query or "刚放" in query:
        payload["sort"] = "newest"
    elif "最貴" in query or "最贵" in query:
        payload["sort"] = "price_desc"

    # --- 捡漏语义 ---
    if any(w in query for w in ("捡漏", "撿漏", "超值", "笋盤", "笋盘", "特别便宜", "特別便宜")):
        payload["max_price_ratio"] = 0.8
    elif "性价比" in query or "性價比" in query:
        payload["max_price_ratio"] = 0.9

    # --- 同款变体展开（与 LLM 路径同一份映射，P1）---
    if payload.get("base_model"):
        payload["base_models"] = [payload["base_model"]] + ctx.variant_map.get(
            payload["base_model"], [])

    return SearchSpec.from_dict(payload)
