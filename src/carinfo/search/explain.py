"""解释层：把结构化结果变成人话。

**设计：默认走确定性模板，模型只做可选润色。**
理由：解释层的每一句话都必须能被追溯到具体数字（"便宜 48%" 对应 price_ratio）。
让模型从零写，它会编出"车况极佳"这种库里没有依据的结论 —— 对一个比价工具来说
这是致命的（用户会据此下单）。所以模板负责**事实**，模型只在有 key 时负责**语气**。

金额按香港习惯用「萬」：HK$129,000 → "12.9 萬"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from carinfo.search.engine import DIM_LABELS, WEIGHTS, ScoredVehicle, SearchResult
from carinfo.search.llm import LLMClient, LLMError
from carinfo.search.spec import SearchSpec

#: 比价达到这个幅度才值得单独打标签（低于此值属于正常市场波动）
NOTABLE_RATIO = 0.85


def fmt_money(amount: float | None) -> str:
    """香港习惯的金额写法：万位以上用「萬」。"""
    if amount is None:
        return "—"
    if amount >= 10_000:
        return f"HK${amount / 10_000:.1f} 萬"
    return f"HK${amount:,.0f}"


def fmt_km(km: int | None) -> str:
    if km is None:
        return "—"
    return f"{km / 10_000:.1f} 萬公里" if km >= 10_000 else f"{km:,} 公里"


def market_basis_text(item: ScoredVehicle) -> str:
    """说清楚「比价基准是什么」—— 这是本产品最需要透明的地方。

    三档可信度必须如实说，不能让用户以为每个价格比都同样可靠：
    - bucket: 同车系同 5 年段，样本充足
    - near:   本年份段样本不足，退用邻近 5 年段
    - model:  只能用全年代中位，可信度最低（跨代比价会失真）
    """
    if item.market_median is None:
        return "库内缺同款行情，无法比价"
    if item.market_level == "bucket":
        # 年份缺失时不能靠 `year and ...` 隐式短路 —— 那会拼出「None-4 年段」这种
        # 直接给用户看的脏文案。理论上 bucket 档必有年份，但这条契约靠调用方维持，
        # 这里显式判一次，缺了就走不带年份的兜底句式。
        if item.year:
            lo = item.year // 5 * 5
            return (f"同款 {lo}-{lo + 4} 年段中位价 "
                    f"{fmt_money(item.market_median)}（{item.market_ref_n} 台）")
        return (f"同款行情中位价 {fmt_money(item.market_median)}"
                f"（{item.market_ref_n} 台，年份缺失）")
    if item.market_level == "near":
        return (f"本年份段样本不足，参考邻近 {item.market_bucket}-{item.market_bucket + 4} 年段 "
                f"中位价 {fmt_money(item.market_median)}（{item.market_ref_n} 台）")
    return f"仅参考全年代中位价 {fmt_money(item.market_median)}（{item.market_ref_n} 台，可信度低）"


def item_labels(item: ScoredVehicle) -> list[str]:
    """给前端展示的短标签。**只打有数据支撑的标签**。"""
    tags: list[str] = []

    if item.price_ratio is not None:
        delta = (1 - item.price_ratio) * 100
        if item.price_ratio <= 0.50:
            tags.append(f"⚡ 低于行情 {delta:.0f}%（需核实车况）")
        elif item.price_ratio <= NOTABLE_RATIO:
            tags.append(f"划算 {delta:.0f}%")
        elif item.price_ratio >= 1.15:
            tags.append(f"高于行情 {(item.price_ratio - 1) * 100:.0f}%")

    if item.is_anomaly:
        tags.append("⚠ 价格异常，可能是问题车/标错价")

    if item.age_days is not None:
        if item.age_days <= 3:
            tags.append("刚挂牌")
        elif item.age_days <= 14:
            tags.append(f"{item.age_days} 天前更新")
        elif item.age_days >= 180:
            tags.append(f"挂牌已 {item.age_days} 天")

    if item.hand_count is not None:
        tags.append("一手车" if item.hand_count == 0 else f"{item.hand_count} 手")
    if item.mileage_km is not None:
        tags.append(fmt_km(item.mileage_km))
    if item.import_type:
        tags.append(item.import_type)
    # 牌費到期:香港买家实打实关心(剩余牌費可退,值几万)。
    # 值里有数字才当时间展示(「26年12月」);「有牌費/長牌費」这类非时间值
    # (提取层偶尔摘到)只打布尔化标签,别拼出「牌費至有牌費」这种怪话
    if item.license_until:
        if re.search(r"[0-9]", item.license_until):
            tags.append(f"牌費至{item.license_until}")
        else:
            tags.append("有牌費")
    if item.china_plate:
        tags.append("中港牌")
    # 换车帖只标注不隐藏:对买家是背景信息,对车商是收购线索
    if item.is_swap:
        tags.append("可换车")
    # 车行标注:同一联系方式在售挂 ≥4 台(实测车行贡献 72% 盘源,
    # 买家看到「车行」标签能调整谈价预期;不加价转卖的车行也是正常渠道)
    if item.is_dealer:
        tags.append(f"车行(挂{item.dealer_listings or '?'}台)")
    # 仅邮箱联系:联系慢、优先级被排后,如实标注让调用方知道为什么靠后
    if not item.has_phone:
        tags.append("仅邮箱联系")
    if item.view_count is not None and item.view_count >= 200:
        tags.append(f"浏览 {item.view_count} 次")

    return tags


def near_anchor_text(spec: SearchSpec) -> str | None:
    """把「50 万左右 / 2015 年左右」说成人话。没设锚点返回 None。"""
    anchors: list[str] = []
    if spec.price_near:
        anchors.append(f"{fmt_money(spec.price_near)} 左右")
    if spec.year_near is not None:
        anchors.append(f"{spec.year_near} 年左右")
    return "、".join(anchors) if anchors else None


def item_explain(item: ScoredVehicle, spec: SearchSpec | None = None) -> str:
    """一句话说清「为什么排在这里」—— 必须有数字依据。"""
    parts: list[str] = []

    if item.price_ratio is not None:
        if item.price_ratio < 1:
            parts.append(f"比同款行情低 {(1 - item.price_ratio) * 100:.0f}%")
        else:
            parts.append(f"比同款行情高 {(item.price_ratio - 1) * 100:.0f}%")
    else:
        parts.append("库内同款样本不足，未计入性价比分")

    if item.market_level == "model":
        parts.append("行情基准跨年代，比价只作参考")
    elif item.market_level == "near":
        parts.append("参考邻近年份段")

    # 模糊量：说了"左右"就要告诉用户这条贴不贴 —— 同时点明它**只是排序偏好**，
    # 否则用户会以为范围外的车被藏起来了（其实全都还在，只是排在后面）。
    near = item.scores.get("near")
    if near is not None and spec is not None:
        what = near_anchor_text(spec)
        if what and near >= 1.0:
            parts.append(f"贴近你说的 {what}")
        elif what and near <= 0.0:
            parts.append(f"离你说的 {what} 较远（只是排后面，没被筛掉）")
        elif what:
            parts.append(f"稍偏离你说的 {what}")

    if item.age_days is not None and item.age_days <= 7:
        parts.append("挂牌很新")
    if item.hand_count is not None and item.hand_count == 0:
        parts.append("零手（新车级）")
    if item.scores.get("fresh") is not None and item.scores["fresh"] < 0.3:
        parts.append(f"挂牌已 {item.age_days} 天，热度可能虚高")

    # 缺维如实说：不要让用户以为"车况分低"是真的车况差，可能只是没数据
    missing = [DIM_LABELS[d] for d in WEIGHTS if d not in item.scores]
    if missing:
        parts.append("缺" + "/".join(missing) + "数据，该维度未计分")

    return "；".join(parts) + "。"


@dataclass
class ExplainedResult:
    """对外输出。前端/MCP 直接用这个结构。"""

    summary: str
    items: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    total_matched: int = 0
    spec: dict = field(default_factory=dict)
    source_query: str | None = None


def build_result_dict(item: ScoredVehicle, spec: SearchSpec | None = None) -> dict:
    return {
        **item.to_dict(),
        "price_text": fmt_money(item.price),
        "market_median_text": fmt_money(item.market_median),
        "labels": item_labels(item),
        "explain": item_explain(item, spec),
        "market_basis": market_basis_text(item),
        "score_breakdown": [
            {
                "dim": d,
                "label": DIM_LABELS[d],
                "weight": WEIGHTS[d],
                "score": round(item.scores[d], 4),
            }
            for d in WEIGHTS
            if d in item.scores
        ],
    }


def summarize(result: SearchResult) -> str:
    """确定性摘要。不调模型 —— 摘要里的每个数字都必须可核对。"""
    spec = result.spec
    target = spec.base_model or spec.brand or spec.model_keyword or "全部车型"
    n = len(result.items)
    if not result.items:
        return f"库里没有符合「{target}」条件的车。建议放宽预算或年份。"

    cheapest = min(
        (x for x in result.items if x.price is not None), key=lambda x: x.price, default=None
    )
    best_value = min(
        (x for x in result.items if x.price_ratio is not None),
        key=lambda x: x.price_ratio,
        default=None,
    )
    lines = [
        f"在 {result.total_matched} 台「{target}」里筛出 {n} 台，按综合分排序。"
    ]
    # 说了"左右"就明说**那是排序偏好不是筛选** —— 否则用户会以为范围外的车被藏了
    anchor_text = near_anchor_text(spec)
    if anchor_text:
        lines.append(f"已按你说的 {anchor_text} 优先排序"
                     f"（是偏好不是筛选，范围外的车照样会返回，只是排在后面）。")
    if cheapest is not None and cheapest.price is not None:
        lines.append(f"其中报价最低 {fmt_money(cheapest.price)}"
                     f"（{cheapest.year or '?'} 年 {cheapest.car_model}）。")
    if best_value is not None and best_value.price_ratio is not None:
        lines.append(
            f"最划算的是 {best_value.year or '?'} 年 {best_value.car_model}，"
            f"{fmt_money(best_value.price)}，比同款行情低 "
            f"{(1 - best_value.price_ratio) * 100:.0f}%。"
        )
    return "".join(lines)


# ---------------------------------------------------------------------------
# 可选润色（有 key 才走）
# ---------------------------------------------------------------------------

POLISH_SYSTEM = """你是香港二手车平台的资深经纪，把结构化的选车结果讲成人话。

【铁律】
1. 只能使用给你的数字和事实。**绝对不许新增**任何你没看到的车况、车龄、事故、
   里程、配置信息，也不许评价"车况靓"这类没有依据的话。
2. 数字原样照抄，不要换算、不要估算。
3. 讲香港粤语书面语，简短（3-5 句），结论先行。
4. 如果某台车缺车况数据，要如实说出来，不要替它遮掩。
"""


def polish(result: SearchResult, llm: LLMClient) -> str:
    """让模型把摘要润色成人话。失败就返回确定性摘要（**必须能降级**）。"""
    base = summarize(result)
    if llm is None or not llm.configured or not result.items:
        return base

    facts = [
        {
            "车型": it.car_model, "年份": it.year, "报价": it.price,
            "同款中位价": it.market_median, "价格比": it.price_ratio,
            "行情基准": it.market_level, "样本数": it.market_ref_n,
            "挂牌天数": it.age_days, "手数": it.hand_count, "里程km": it.mileage_km,
            "行水货": it.import_type, "浏览量": it.view_count,
        }
        for it in result.items[:8]
    ]
    try:
        text = llm.chat(POLISH_SYSTEM, f"检索条件：{result.spec.to_dict()}\n"
                                       f"候选：{facts}\n事实摘要：{base}")
        return text.strip() or base
    except LLMError:
        return base


def explain(result: SearchResult, llm: LLMClient | None = None, use_llm: bool = False) -> ExplainedResult:
    """组装对外结果。use_llm=True 且有 key 时，摘要换成模型润色版。"""
    summary = polish(result, llm) if (use_llm and llm is not None) else summarize(result)
    return ExplainedResult(
        summary=summary,
        items=[build_result_dict(x, result.spec) for x in result.items],
        notes=result.notes,
        total_matched=result.total_matched,
        spec=result.spec.to_dict(),
        source_query=result.spec.raw_query,
    )
