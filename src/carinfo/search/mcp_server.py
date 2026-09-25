"""MCP 服务：把检索能力暴露成工具，给 Agent / 客户端调用。

四个工具（刻意只给这几个 —— 工具太多会稀释模型的选择准确率）：
- `search_cars`        自然语言检索（主力入口）
- `get_car_detail`     单车详情 + 比价依据
- `list_hot_models`    库内热门车系（让模型先知道库里有什么，避免瞎猜车型）
- `search_by_spec`     结构化检索（调用方自己知道条件时用）

mcp 2.x 里 FastMCP 已改名 `MCPServer`（from mcp.server.mcpserver import MCPServer）。
用 mcp<2 的老写法会 ModuleNotFoundError。

启动（stdio，供本地 Agent 直连）：
    uv run python -m carinfo.search.mcp_server

**鉴权的边界**：默认的 `stdio` 传输**没有 HTTP 请求头可言**，进程边界本身就是鉴权
（谁能启这个进程谁就能调），所以不加。但本服务也支持 `--transport sse|streamable-http`，
那两种会真的监听端口 —— 那时**必须**配 `SEARCH_API_KEY`，否则拒绝启动。

**连接**：每工具调用从进程内连接池借一条（`db.py`），不再共用全局单连接。
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from carinfo.search.auth import (
    ENV_ALLOW_NO_AUTH,
    ENV_KEY,
    NO_KEY_HINT,
    ApiKeyMiddleware,
    allow_no_auth,
    is_configured,
)
from carinfo.search.config import load_llm_config, load_search_config
from carinfo.search.context import SearchContext
from carinfo.search.db import fetch
from carinfo.search.engine import search
from carinfo.search.explain import explain
from carinfo.search.llm import LLMClient
from carinfo.search.parser import parse_query
from carinfo.search.spec import DEFAULT_LIMIT, MAX_LIMIT, SearchSpec

server = MCPServer(
    name="carinfo",
    instructions=(
        "香港 28car 二手车库检索。库内为港币报价的右舵车源，"
        "覆盖在售与已售。查价、找车、比价都用这里。\n"
        "典型用法：先用 list_hot_models 看看库里有哪些车系，再用 search_cars 检索，"
        "需要细节时用 get_car_detail。"
    ),
)

#: HTTP 传输监听时默认绑本机：想对外必须显式改 host，别默认敞开
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000


def build_transport_security() -> TransportSecuritySettings:
    """传输层 Host/Origin 校验：**关掉**。

    为什么需要这么一个函数（看起来多此一举）：mcp 2.x 有段自作主张的逻辑
    （`mcpserver/server.py` → `lowlevel/server.py` 的 "Auto-enable DNS rebinding
    protection"）—— 只要 `transport_security is None` 且 host 属于 localhost 家族，
    就替我们塞一份 `allowed_hosts=["127.0.0.1:*","localhost:*","[::1]:*"]`。
    本服务绑 127.0.0.1 由 Nginx 转进来，外部请求的 Host 是公网域名，于是被判
    **421 Misdirected Request**。这是线上 MCP 连不上的根因。

    **为什么不配置白名单放行域名**：本服务对外开放，凡是拿到 key 的第三方都该能用，
    来源不可枚举。按 Host 画线等于用「调用方在哪」代替「调用方是谁」，既拦得住陌生
    第三方（本该放行），也拦不住伪造 Host（本该靠 key 挡）。所以这里直接关掉，
    访问控制统一交给 `X-API-Key` 中间件 —— 那道门对所有路径生效，包括 /mcp。

    必须**显式传**这个对象（而不是留 None），否则库又会按 localhost 自动兜底。
    """
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


_llm: LLMClient | None = None


def _llm_client() -> LLMClient | None:
    global _llm
    cfg = load_search_config()
    if not cfg["use_llm"]:
        return None
    if _llm is None:
        _llm = LLMClient(load_llm_config())
    return _llm


def _load_ctx(conn) -> SearchContext:
    """短借连接读一次上下文，随即归还。"""
    return SearchContext.load(conn)


def _coerce_limit(v) -> int:
    """limit 收敛到合法值。**上限取自 spec.MAX_LIMIT，不另写数字** ——
    条数口径单点在 spec.py，别处各写一个数曾经就是 bug 的来源。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


# ---------------------------------------------------------------------------
# 各工具的"拿连接干活"部分单独成函数：`fetch()` 需要能整体重试
# ---------------------------------------------------------------------------
def _do_search_cars(query: str, limit: int) -> dict[str, Any]:
    """自然语言检索：**分三段各自短借连接**，LLM 调用不占连接。

    与 api.py 的 _do_search_nl 同因同理：parse_query 会调 MiniMax（最长 30s×3），
    若整段包在 `with db()` 里，一次请求就有一条池连接被网络 IO 占着；池满后
    所有请求 PoolBusy→503，而 SQL 部分本身不到 1 秒。
    **连接只包住 SQL，不包住网络调用。**
    """
    llm = _llm_client()

    ctx = fetch(_load_ctx)                                    # ① 短借：读上下文
    parsed = parse_query(query, ctx, llm=llm)                 # ② 无连接：LLM 解析
    parsed.spec.limit = max(1, min(_coerce_limit(limit), MAX_LIMIT))
    result = fetch(lambda conn: search(conn, parsed.spec))    # ③ 短借：检索
    out = explain(result, llm=llm, use_llm=False)             # ④ 无连接：解释
    return {
        "summary": out.summary,
        "parse_source": parsed.source,
        "spec": out.spec,
        "total_matched": out.total_matched,
        "notes": list(parsed.notes) + list(out.notes),
        "items": [
            {
                "vehicle_id": it["vehicle_id"],
                "car_model": it["car_model"],
                "brand": it["car_brand"],
                "year": it["year"],
                "price": it["price"],
                "price_text": it["price_text"],
                "price_ratio": it["price_ratio"],
                "market_basis": it["market_basis"],
                "labels": it["labels"],
                "why": it["explain"],
                "url": it["car_url"],
            }
            for it in out.items
        ],
    }


_DETAIL_SQL = """
SELECT v.vehicle_id, v.car_brand, v.car_model, v.year, v.current_price,
       v.original_price, v.seats, v.engine_volume, v.transmission, v.fuel_type,
       v.car_url, v.car_category, v.extra_fields,
       f.base_model, f.price_ratio, f.market_median, f.market_p25, f.market_p75,
       f.market_bucket, f.market_level, f.market_ref_n, f.condition_score,
       f.has_condition, f.age_days, f.heat_score, f.is_anomaly
FROM vehicles v
LEFT JOIN vehicle_features f ON f.vehicle_id = v.vehicle_id
WHERE v.vehicle_id = %s AND v.vehicle_status = 1
"""

_HOT_MODELS_SQL = """
SELECT f.base_model, count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY v.current_price) AS median
FROM vehicle_features f JOIN vehicles v USING (vehicle_id)
WHERE f.base_model IS NOT NULL
GROUP BY f.base_model HAVING count(*) >= 5
ORDER BY n DESC LIMIT %s
"""


def _do_car_detail(conn, vehicle_id: str) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute(_DETAIL_SQL, (vehicle_id,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    cur.close()
    if row is None:
        return {"error": "车源不存在或已下架", "vehicle_id": vehicle_id}

    data = dict(zip(cols, row))
    for k in ("current_price", "original_price", "market_median", "market_p25", "market_p75",
              "price_ratio", "condition_score", "heat_score"):
        if data.get(k) is not None:
            data[k] = float(data[k])
    return data


def _do_hot_models(conn, limit: int) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute(_HOT_MODELS_SQL, (max(1, min(int(limit), 200)),))
    rows = cur.fetchall()
    cur.close()
    return {
        "count": len(rows),
        "models": [
            {"base_model": b, "listings": n, "median_price_hkd": float(m) if m else None}
            for b, n, m in rows
        ],
    }


def _do_search_by_spec(conn, spec: SearchSpec) -> dict[str, Any]:
    result = search(conn, spec)
    out = explain(result)
    return {
        "summary": out.summary,
        "total_matched": out.total_matched,
        "items": [
            {
                "vehicle_id": it["vehicle_id"],
                "car_model": it["car_model"],
                "year": it["year"],
                "price": it["price"],
                "price_ratio": it["price_ratio"],
                "labels": it["labels"],
            }
            for it in out.items
        ],
    }


# ---------------------------------------------------------------------------
@server.tool(
    name="search_cars",
    description=(
        "按中文/粤语自然语言或英文车系名检索香港二手车源，返回按综合分排序的候选，"
        "含比价依据（与同款同年段中位价的比值）。"
        "例：'五十萬以內的阿尔法'、'2015年打後的一手威尔法'、'最便宜的七座MPV'。"
        "价格均为港币。"
    ),
)
def search_cars(query: str, limit: int = 5) -> dict[str, Any]:
    # 不再整体包 fetch()：函数内部自己短借连接，LLM 调用不占池
    return _do_search_cars(query, limit)


@server.tool(
    name="get_car_detail",
    description="按 vehicle_id 取单车完整信息与比价依据（含同款 p25/中位/p75 价、行情样本数、车况原始字段）。",
)
def get_car_detail(vehicle_id: str) -> dict[str, Any]:
    return fetch(lambda conn: _do_car_detail(conn, vehicle_id))


@server.tool(
    name="list_hot_models",
    description=(
        "列出库内在售台数最多的车系（含中位价）。"
        "在不确定用户说的车型库里有没有时，先用这个查，再用 search_cars。"
    ),
)
def list_hot_models(limit: int = 30) -> dict[str, Any]:
    return fetch(lambda conn: _do_hot_models(conn, limit))


@server.tool(
    name="search_by_spec",
    description=(
        "结构化检索，不经过自然语言解析。适合明确知道要什么条件的调用方。"
        "参数即下面函数签名里的那些。"
        "⚠️ 默认值不等于「不设限」，有两条隐式收窄："
        "① 只搜**私家车**（车型类别固定为私家车，不含客货车/货车/电单车/经典车）；"
        "② **自动排除疑似问题车**（比同款行情低 50% 以上的）。"
        "china_plate/swap 是三态：None=不筛；true=只要（中港牌车 / 换车帖——换车帖对"
        "收购场景是线索：卖家想换车=好谈价）；false=排除。"
        "签名里没列出的条件（变速箱、燃料、行水货、排量、关键词）则是真的不设限。"
    ),
)
def search_by_spec(
    base_model: str | None = None,
    brand: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    seats: int | None = None,
    hand_max: int | None = None,
    mileage_max: int | None = None,
    max_price_ratio: float | None = None,
    china_plate: bool | None = None,
    swap: bool | None = None,
    sort: str = "score",
    limit: int = 5,
) -> dict[str, Any]:
    spec = SearchSpec.from_dict(
        {
            "base_model": base_model,
            "brand": brand,
            "price_min": price_min,
            "price_max": price_max,
            "year_min": year_min,
            "year_max": year_max,
            "seats": seats,
            "hand_max": hand_max,
            "mileage_max": mileage_max,
            "max_price_ratio": max_price_ratio,
            "china_plate": china_plate,
            "swap": swap,
            "sort": sort,
            "limit": limit,
        }
    )
    return fetch(lambda conn: _do_search_by_spec(conn, spec))


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="carinfo 检索 MCP 服务")
    ap.add_argument(
        "--transport",
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        choices=("stdio", "sse", "streamable-http"),
        help="stdio（默认，供本地 Agent 直连）| sse | streamable-http",
    )
    ap.add_argument("--host", default=DEFAULT_HTTP_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    args = ap.parse_args(argv)

    if args.transport == "stdio":
        # stdio：进程边界就是鉴权，没有 HTTP 请求头可言，不需要 key
        server.run("stdio")
        return

    # HTTP 传输会真的监听端口 —— 不配 key 就是裸奔，直接拒绝启动
    if not is_configured():
        if allow_no_auth():
            print(
                f"[warn] MCP 走 {args.transport} 监听 {args.host}:{args.port}，"
                f"但未配置 {ENV_KEY} 且设了 {ENV_ALLOW_NO_AUTH}=1 —— 无鉴权。仅限本机调试。",
                file=sys.stderr,
            )
        else:
            raise SystemExit(
                f"[fatal] MCP 走 {args.transport} 会监听网络端口，必须先配鉴权。{NO_KEY_HINT}"
            )

    # 显式关掉 mcp 库自带的 Host 校验（留 None 会被按 localhost 自动兜底，
    # 反代进来就 421）。鉴权统一由下面这个 X-API-Key 中间件负责。
    ts = build_transport_security()
    http_app = (
        server.streamable_http_app(transport_security=ts, host=args.host)
        if args.transport == "streamable-http"
        else server.sse_app(transport_security=ts, host=args.host)
    )
    # 与 HTTP API 共用同一个中间件，避免两份鉴权实现走偏
    http_app.add_middleware(ApiKeyMiddleware)

    import uvicorn

    uvicorn.run(http_app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

