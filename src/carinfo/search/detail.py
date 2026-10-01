"""单车详情：HTTP API 与 MCP 共用的取数与组装。

两端原先各写一份（SQL + 取图 + 联系人展示串 + 数值转换 + 价格文案），逐行重合。
此模块是唯一实现，两端只保留「不存在时如何回」的差异包装（API → 404，MCP → error）。
"""

from __future__ import annotations

from typing import Any

from carinfo.search.image_proxy import proxied_image_url
from carinfo.utils import contact_display_of, fmt_money

_VEHICLE_SQL = """
SELECT v.vehicle_id, v.car_brand, v.car_model, v.year, v.current_price,
       v.original_price, v.seats, v.engine_volume, v.transmission,
       v.fuel_type, v.car_url, v.car_category, v.extra_fields,
       v.description,
       v.contact_name, v.phone_number, v.contact_email, v.contact_info,
       f.base_model, f.brand_norm, f.price_ratio, f.market_median,
       f.market_p25, f.market_p75, f.market_bucket, f.market_level,
       f.market_ref_n, f.condition_score, f.has_condition, f.age_days,
       f.heat_score, f.is_anomaly, f.dealer_listings, f.is_dealer,
       f.is_unverified, f.verify_age_days
FROM vehicles v
LEFT JOIN vehicle_features f ON f.vehicle_id = v.vehicle_id
WHERE v.vehicle_id = %s AND v.vehicle_status = 1
"""

#: 详情图片：全量按页面原始顺序(image_order)。列表的图由 engine._attach_images 负责
_IMAGES_SQL = """
SELECT image_url FROM vehicle_images WHERE vehicle_id = %s ORDER BY image_order
"""

#: jsonb/numeric 取回来可能是 Decimal，统一转 float 供 JSON 序列化
_FLOAT_COLS = (
    "current_price", "original_price", "market_median", "market_p25", "market_p75",
    "price_ratio", "condition_score", "heat_score",
)


def fetch_vehicle_detail(conn, vehicle_id: str) -> dict[str, Any] | None:
    """取单车完整信息（原文 + 全部特征 + 比价依据 + 联系方式）。

    不存在或已下架返回 None —— 「如何回」由调用方决定（API 404 / MCP error）。
    """
    cur = conn.cursor()
    cur.execute(_VEHICLE_SQL, (vehicle_id,))
    row = cur.fetchone()
    if row is None:
        cur.close()
        return None
    cols = [d[0] for d in cur.description]
    cur.execute(_IMAGES_SQL, (vehicle_id,))
    images = [r[0] for r in cur.fetchall()]
    cur.close()

    data = dict(zip(cols, row))
    # 图片走我们自己的代理 URL，调用方不直接接触 28car（原链保留在 images_raw）。
    data["images"] = [proxied_image_url(vehicle_id, i) for i in range(len(images))]
    data["images_raw"] = images

    # 联系人展示串：找车的最终目的是联系车主，详情必带（电话优先，仅邮箱带「電郵」前缀）
    data["contact_display"] = contact_display_of(
        data.get("contact_name"), data.get("phone_number"), data.get("contact_email")
    )

    for k in _FLOAT_COLS:
        if data.get(k) is not None:
            data[k] = float(data[k])

    data["price_text"] = fmt_money(data.get("current_price"))
    data["market_median_text"] = fmt_money(data.get("market_median"))
    if data.get("price_ratio") is not None:
        r = data["price_ratio"]
        data["price_verdict"] = (
            f"比同款行情低 {(1 - r) * 100:.0f}%" if r < 1 else f"比同款行情高 {(r - 1) * 100:.0f}%"
        )
    return data
