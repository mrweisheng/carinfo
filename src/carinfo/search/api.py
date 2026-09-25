"""HTTP API 层。

职责很薄：接请求 → 组装 SearchSpec（原生 spec 或自然语言）→ 调 engine → 交 explain。
所有业务逻辑都在 search/ 下面的模块，这里只做参数校验、鉴权与连接管理。

启动（推荐，会先检查配置再起服务）：

    uv run python -m carinfo.search.api --host 127.0.0.1 --port 8088

也可以直接交给 uvicorn（省掉启动前的配置检查，鉴权由中间件照常兜着）：

    uv run uvicorn carinfo.search.api:app --host 127.0.0.1 --port 8088

与爬虫服务（run_service.py）**互不干扰**：这里只读数据库，不启动任何抓取。

**鉴权**：所有路由（含 `/docs`、`/openapi.json`）都要带 `X-API-Key` 请求头。
密钥在 `.env` 的 `SEARCH_API_KEY`，三态行为见 `auth.py` 的文档。

**连接**：每请求从进程内连接池借一条（`db.py`），不再共用全局单连接。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

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
from carinfo.search.db import (
    DbUnavailable,
    PoolBusy,
    close_pool,
    fetch,
    pool_stats,
    warm_pool,
)
from carinfo.search.engine import ScanTooWide, search
from carinfo.search.explain import explain, fmt_money
from carinfo.search.llm import LLMClient
from carinfo.search.parser import parse_query
from carinfo.search.spec import MAX_LIMIT, SearchSpec

log = logging.getLogger("carinfo.search.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    warm_pool()
    if not is_configured() and allow_no_auth():
        log.warning(
            "未配置 %s 且设了 %s=1 —— 任何能访问本端口的人都能查询车源库。仅限本机调试。",
            ENV_KEY, ENV_ALLOW_NO_AUTH,
        )
    yield
    close_pool()


app = FastAPI(
    title="carinfo 智能搜索",
    version="0.2.0",
    description="28car 车源的自然语言检索与比价。只读接口，不触发抓取。需带 X-API-Key。",
    lifespan=lifespan,
)
#: 用中间件而不是 Depends：Depends 漏掉 /docs、/openapi.json 这类自动路由
app.add_middleware(ApiKeyMiddleware)

_llm: LLMClient | None = None


@app.exception_handler(DbUnavailable)
@app.exception_handler(PoolBusy)
async def _db_unavailable(_request: Request, exc: Exception) -> JSONResponse:
    log.warning("数据库不可用：%s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(ScanTooWide)
async def _scan_too_wide(_request: Request, exc: Exception) -> JSONResponse:
    """候选集过大**不是服务故障**，是查询条件太宽 —— 必须给 4xx 而不是 500，
    否则调用方会以为服务坏了、去查日志，而正确动作是加个车型/预算条件。"""
    log.info("候选集过宽被拒：%s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=400)


def get_llm(use_llm: bool) -> LLMClient | None:
    global _llm
    if not use_llm:
        return None
    if _llm is None:
        _llm = LLMClient(load_llm_config())
    return _llm


# ---------------------------------------------------------------------------
# 各端点的"拿连接干活"部分单独成函数：`fetch()` 需要能整体重试
# ---------------------------------------------------------------------------
def _do_health(conn) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM vehicle_features")
    n = cur.fetchone()[0]
    cur.close()
    return {"ok": True, "detail": f"特征表 {n} 行"}


def _load_ctx(conn) -> SearchContext:
    """短借连接读一次上下文（词表 + 库内真实键），随即归还。"""
    return SearchContext.load(conn)


def _do_search_nl(q: str, limit: int | None, can_use: bool) -> dict[str, Any]:
    """自然语言检索：**分三段各自短借连接**，LLM 调用不占连接。

    为什么不能像别的端点那样整体包在 `fetch()` 里：`parse_query` 会调 MiniMax
    （最长 30s、重试 3 次），`explain` 可能再调一次润色。若整段都在 `with db()`
    块内，一次请求就有一条池连接被网络 IO 占着 —— 池只有 10 条、排队 5 秒，
    十个并发慢查询就能把池占满，后续请求全部 PoolBusy→503，
    而真正需要 SQL 的部分（检索 + 解释取数）加起来不到 1 秒。
    原则：**连接只包住 SQL，不包住网络调用。**
    """
    cfg = load_search_config()

    # ① 取上下文（短借）
    ctx = fetch(_load_ctx)

    # ② LLM 解析（无连接）
    parsed = parse_query(q, ctx, llm=get_llm(can_use))
    if limit is not None:
        parsed.spec.limit = limit

    # ③ 检索（短借；fetch 内已含连接级错误重试）
    result = fetch(lambda conn: search(conn, parsed.spec))

    # ④ 解释（模板确定性 + 可选 LLM 润色；无连接）
    out = explain(result, llm=get_llm(can_use), use_llm=cfg["polish_summary"])
    payload = out.__dict__.copy()
    payload["parse_source"] = parsed.source
    payload["notes"] = list(parsed.notes) + list(out.notes)
    return payload


_VEHICLE_SQL = """
SELECT v.vehicle_id, v.car_brand, v.car_model, v.year, v.current_price,
       v.original_price, v.seats, v.engine_volume, v.transmission,
       v.fuel_type, v.car_url, v.car_category, v.extra_fields,
       v.description,
       f.base_model, f.brand_norm, f.price_ratio, f.market_median,
       f.market_p25, f.market_p75, f.market_bucket, f.market_level,
       f.market_ref_n, f.condition_score, f.has_condition, f.age_days,
       f.heat_score, f.is_anomaly
FROM vehicles v
LEFT JOIN vehicle_features f ON f.vehicle_id = v.vehicle_id
WHERE v.vehicle_id = %s AND v.vehicle_status = 1
"""

#: 详情图片:全量按页面原始顺序(image_order)。列表的首图由 engine._attach_covers 负责
_IMAGES_SQL = """
SELECT image_url FROM vehicle_images WHERE vehicle_id = %s ORDER BY image_order
"""

_MODELS_SQL = """
SELECT base_model, count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY v.current_price) AS median
FROM vehicle_features f JOIN vehicles v USING (vehicle_id)
WHERE f.base_model IS NOT NULL
GROUP BY base_model HAVING count(*) >= 5
ORDER BY n DESC LIMIT %s
"""


def _do_vehicle(conn, vehicle_id: str) -> dict[str, Any] | None:
    cur = conn.cursor()
    cur.execute(_VEHICLE_SQL, (vehicle_id,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    if row is None:
        cur.close()
        return None
    cur.execute(_IMAGES_SQL, (vehicle_id,))
    images = [r[0] for r in cur.fetchall()]
    cur.close()

    data = dict(zip(cols, row))
    data["images"] = images
    for k in ("current_price", "original_price", "market_median", "market_p25", "market_p75",
              "price_ratio", "condition_score", "heat_score"):
        if data.get(k) is not None:
            data[k] = float(data[k])

    median = data.get("market_median")
    data["price_text"] = fmt_money(data.get("current_price"))
    data["market_median_text"] = fmt_money(median)
    if data.get("price_ratio") is not None:
        r = data["price_ratio"]
        data["price_verdict"] = (
            f"比同款行情低 {(1 - r) * 100:.0f}%" if r < 1 else f"比同款行情高 {(r - 1) * 100:.0f}%"
        )
    return data


def _do_models(conn, limit: int) -> list[dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(_MODELS_SQL, (limit,))
    rows = cur.fetchall()
    cur.close()
    return [
        {"base_model": b, "count": n, "median_price": float(m) if m else None}
        for b, n, m in rows
    ]


# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> JSONResponse:
    """存活 + 就绪探针。**也要鉴权** —— 一条规则没有例外，省得记住哪个口子是开的。

    DB 探测失败时返回 **503**（而不是 200 + ok:false）：探针存在的唯一意义就是
    让编排系统/LB 知道该不该继续导流。DB 挂了还回 200，等于这个接口白写 ——
    真正干活的 /search 会 503，探针却说"健康"，矛盾且危险。
    """
    cfg = load_search_config()
    llm = get_llm(cfg["use_llm"])
    db_ok = True
    try:
        result = fetch(_do_health)
    except Exception as e:  # noqa: BLE001
        db_ok = False
        result = {"ok": False, "detail": f"数据库不可用：{e}"}
    payload = {
        **result,
        "llm_configured": bool(llm and llm.configured),
        "llm_model": llm.cfg.model if llm else None,
        "llm_base_url": llm.cfg.base_url if llm else None,
        "auth": {"key_configured": is_configured(), "allow_no_auth": allow_no_auth()},
        "db_pool": pool_stats(),
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)


@app.get("/search")
def search_nl(
    q: str = Query(..., min_length=1, description="自然语言查询，如「五十萬以內的阿尔法」"),
    limit: int | None = Query(None, ge=1, le=MAX_LIMIT),
    use_llm: bool | None = Query(None, description="留空则用 config.json 的 search.use_llm"),
) -> JSONResponse:
    """自然语言检索。无 API key 时自动降级为规则解析（不报错）。"""
    cfg = load_search_config()
    can_use = cfg["use_llm"] if use_llm is None else use_llm
    # 不再整体包 fetch()：函数内部自己短借连接，LLM 调用不占池（见其 docstring）
    return JSONResponse(_do_search_nl(q, limit, can_use))


@app.post("/search/spec")
def search_spec(body: dict = Body(...)) -> JSONResponse:
    """结构化检索：直接给 SearchSpec 的字段，不经过模型。给程序调用。"""
    try:
        spec = SearchSpec.from_dict(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"参数不合法：{e}") from e

    def _run(conn):
        result = search(conn, spec)
        return explain(result).__dict__

    return JSONResponse(fetch(_run))


@app.get("/vehicle/{vehicle_id}")
def vehicle_detail(vehicle_id: str) -> JSONResponse:
    """单车详情：原文 + 全部特征 + 比价依据。"""
    data = fetch(lambda conn: _do_vehicle(conn, vehicle_id))
    if data is None:
        raise HTTPException(status_code=404, detail="车源不存在或已下架")
    return JSONResponse(data)


@app.get("/models")
def list_models(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """库内车系榜（按在售台数）。给前端做下拉/热门入口。"""
    return {"models": fetch(lambda conn: _do_models(conn, limit))}


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    """`uv run python -m carinfo.search.api` 的入口。

    作用只有一个：**在起服务之前把配置说清楚**。不配密钥时直接拒绝启动，
    而不是起来之后每个请求都 503（那样更难排查）。
    """
    import argparse
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="carinfo 智能搜索 HTTP API")
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    ap.add_argument("--port", type=int, default=8088)
    args = ap.parse_args(argv)

    if not is_configured():
        if allow_no_auth():
            print(
                f"[warn] 未配置 {ENV_KEY}，但设了 {ENV_ALLOW_NO_AUTH}=1 —— "
                f"任何能访问 {args.host}:{args.port} 的人都能查询车源库。仅限本机调试。",
                file=sys.stderr,
            )
        else:
            raise SystemExit(f"[fatal] {NO_KEY_HINT}")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
