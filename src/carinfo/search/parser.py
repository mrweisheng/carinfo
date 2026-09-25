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
    "base_model", "brand", "model_keyword", "displacement",
    "year_min", "year_max", "year_near",
    "price_min", "price_max", "price_near",
    "seats", "vehicle_type", "transmission", "fuel_type",
    "import_type", "hand_max", "mileage_max", "china_plate",
    "max_price_ratio", "exclude_anomaly",
    "sort", "limit",
}
#: swap(换车帖)**刻意不给 NL**:「换车」歧义太大(「我想换辆车」≠「找换车帖」),
#: 只给 spec/API/MCP 的程序化调用。

SYSTEM_PROMPT = """你是香港二手车平台的查询解析器。把用户的话翻译成一个检索 JSON 对象。

【输出格式 —— 最容易出错，务必遵守】
只输出一个 JSON 对象。不要解释文字、不要 markdown 代码块。
**键名必须逐字用下面「字段清单」里的英文名**。自己换个名字（model / car_model /
brand_name 之类）等于这个条件没写 —— 系统会直接丢掉它。

【字段清单】未提到的条件一律不输出该键；不要给 null，也不要猜。
base_model      车型，英文大写正式名
brand           品牌，英文大写
model_keyword   车型名拿不准英文正式名时，填用户原文
displacement    排量字符串，如 "3.5"
year_min        年份下限，整数
year_max        年份上限，整数
year_near       年份**模糊锚点**（"2015年左右"）→ 2015。只影响排序，不是筛选
price_min       价格下限，港币整数
price_max       价格上限，港币整数
price_near      价格**模糊锚点**（"五十万左右"）→ 500000。只影响排序，不是筛选
seats           座位数，整数
hand_max        手数上限，整数
mileage_max     里程上限（公里），整数
import_type     "行貨" 或 "水貨"
china_plate     布尔。用户明确要「中港牌/兩地牌」的车 → true；没提就不输出
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
- "五万公里以內" → {"mileage_max": 50000}
- "性价比高" → {"max_price_ratio": 0.9}；"捡漏 / 超值 / 特别便宜" → {"max_price_ratio": 0.8}
- vehicle_type：用户明确说"客货车 / 货车 / 电单车"才填；说"车"或没说就不填
- limit：没说就不填（系统默认返回 5 条）

【完整示例】
"阿尔法"                 → {"base_model": "ALPHARD"}
"五十萬以內的阿尔法"       → {"base_model": "ALPHARD", "price_max": 500000}
"五十万左右的阿尔法"       → {"base_model": "ALPHARD", "price_near": 500000}
"2015年打後的一手威尔法"   → {"base_model": "VELLFIRE", "year_min": 2015, "hand_max": 1}
"2015年左右的威尔法"       → {"base_model": "VELLFIRE", "year_near": 2015}
"三十万以下的七座车"       → {"price_max": 300000, "seats": 7}
"最便宜的平治"            → {"brand": "MERCEDES-BENZ", "sort": "price_asc"}
"捡漏阿尔法"              → {"base_model": "ALPHARD", "max_price_ratio": 0.8}

【铁律】
用户没提的条件，绝对不要加。不要替他决定预算、年份、座位数。
说了"左右"就**只填 *_near** —— 那是排序偏好，不是筛选条件。
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


@dataclass
class ParseResult:
    spec: SearchSpec
    source: str                      # 'llm' | 'fallback' | 'mixed'
    notes: list[str] = field(default_factory=list)
    raw_llm: dict | None = None      # 保留模型原始输出，便于排查


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
    data: dict | None = None
    raw_llm: dict | None = None      # 模型**原生**输出，筛过白名单前的，只给排查用

    if llm is not None and llm.configured:
        try:
            raw_llm = llm.chat_json(SYSTEM_PROMPT, f"用户查询：{query}\n{SOFT_HINT}")
            # ① 键别名归一：model / car_model → base_model。
            #    不能只靠 prompt —— 实测 M3 约 4/5 的次数把车型写成 `model`，光靠
            #    prompt 拦不住，必须在这里硬兜底（否则「搜阿尔法」退化成全库扫描）。
            data = normalize_llm_keys(raw_llm)
            # ② 白名单过滤：模型多给的字段一律丢掉，防止幻觉字段穿透到 SQL
            data = {k: v for k, v in data.items() if k in ALLOWED_LLM_FIELDS and v is not None}
            if not data:
                stranger = sorted(set(raw_llm) - ALLOWED_LLM_FIELDS)
                notes.append(
                    "模型没给出可用字段"
                    + (f"（不认识的键 {stranger}）" if stranger else "（返回了空 JSON）")
                    + "，已改用规则解析"
                )
        except LLMError as e:
            notes.append(f"模型解析失败，已降级为规则解析：{e}")
            data = None
    else:
        notes.append("未配置模型，用规则解析（复杂语义会解不准）")

    if not data:
        return ParseResult(spec=rule_based_parse(query, ctx), source="fallback",
                           notes=notes, raw_llm=raw_llm)

    base_model, brand, keyword, displacement = resolve_model_target(
        data.get("base_model") or data.get("model_keyword"),
        data.get("brand"),
        ctx,
    )
    if data.get("model_keyword") and base_model:
        # 模型给了关键词但也解析出了车系 → 以车系为准，关键词丢弃
        keyword = None
    if data.get("base_model") and base_model is None and not keyword:
        notes.append(f"模型给的车型 {data['base_model']!r} 在库里找不到，已退化为模糊匹配")
        keyword = str(data["base_model"])

    # ③ 模糊量守卫：原文说"左右"时，不许让区间溜进来，也不许让偏好丢掉。
    #    以**原文**为准而不是模型 —— 模型把"五十万左右"写成 price_max=500000 的话，
    #    硬过滤会静默删掉 60 万的车，而用户只是想让他们排后面。
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
    for k in (
        "year_min", "year_max", "year_near", "price_min", "price_max", "price_near",
        "seats", "vehicle_type", "transmission", "fuel_type", "import_type",
        "hand_max", "mileage_max", "max_price_ratio", "china_plate",
    ):
        if data.get(k) is not None:
            payload[k] = data[k]
    if data.get("exclude_anomaly") is not None:
        payload["exclude_anomaly"] = bool(data["exclude_anomaly"])

    spec = SearchSpec.from_dict(payload)
    return ParseResult(spec=spec, source="llm", notes=notes, raw_llm=raw_llm)


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
    if not payload.get("base_model") and not payload.get("brand"):
        for model in sorted(ctx.models, key=len, reverse=True):
            if len(model) < 3 or not any(c.isalpha() for c in model):
                continue  # 跳过 '3.5' / '5.5' / '2015' 这类从脏数据兜出来的数字键
            if re.search(rf"(?<![A-Z0-9]){re.escape(model)}(?![A-Z0-9])", up):
                payload["base_model"] = model
                break

    # --- 纯数字车系（911 / 718 / 458）只认「整句就是它」，避免「300萬以內」误命中 ---
    if not payload.get("base_model") and not payload.get("brand"):
        stripped = up.strip()
        if stripped in ctx.models:
            payload["base_model"] = stripped

    # --- 品牌：扫品牌别名 ---
    if not payload.get("base_model") and not payload.get("brand"):
        for alias, en in sorted(BRAND_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
            if alias in query and en in ctx.brands:
                payload["brand"] = en
                break

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

    # --- 年份 ---
    # 同样不能用 \b：「2015年」里「年」是 word character，\b 在数字后不成立，
    # 会导致年份一个字都抽不出来。改用数字边界断言。
    for m in _YEAR_RE.finditer(query):
        y, pos = int(m.group(1)), m.start()
        around = _window(query, pos)
        if any(w in around for w in _MIN_WORDS):
            payload["year_min"] = max(payload.get("year_min", y), y)
        elif any(w in around for w in _MAX_WORDS):
            payload["year_max"] = min(payload.get("year_max", y), y)
        elif any(w in around for w in _AROUND_WORDS):
            payload.setdefault("year_near", y)   # "2015年左右" → 软锚点，不是 year_min

    # --- 座位数 ---
    seat = re.search(r"([0-9]+|[零一二两三四五六七八九十]+)\s*[座坐]", query)
    if seat:
        n = _cn_to_int(seat.group(1))
        if n and 2 <= n <= 30:
            payload["seats"] = n

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
        elif "一手" in hand_text or "一手車" in hand_text:
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

    return SearchSpec.from_dict(payload)
