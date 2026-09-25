"""请求头鉴权：一个 `X-API-Key`，够用就好。

**定位**：这是给**内网/本机**用的检索服务，不做用户体系、不做会话、不做权限分级。
需要的只是"别让互联网上随便谁都能查这个车源库"—— 一个共享密钥的请求头正好覆盖这个需求，
成本是零（没有第二套凭证要管）。

**三态，默认 fail-closed**：

| 情况 | 行为 |
|---|---|
| 配了 `SEARCH_API_KEY` | 每个请求必须带 `X-API-Key`，不匹配 → **401** |
| 没配，但设了 `SEARCH_ALLOW_NO_AUTH=1` | 全部放行（**只给本机调试用**，启动会打警告） |
| 没配，也没设开关 | 全部 **503**（拒绝提供服务，并说清怎么配） |

为什么默认拒绝而不是默认放行：库里是真实车源和报价，服务一旦绑到公网就等于裸奔。
"忘记配置"应该表现为**服务不可用**（立刻能被发现），而不是**服务可用但没人守门**（悄无声息）。

**MCP 那边**：默认的 `stdio` 传输没有 HTTP 请求头可言 —— 进程边界本身就是鉴权（谁能启这个
进程谁就能调）。只有切到 `sse` / `streamable-http` 时才需要，`mcp_server.main()` 会强制要求
配置密钥，并复用本模块的中间件。
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any, Callable

from carinfo.search.config import load_dotenv_once

ENV_KEY = "SEARCH_API_KEY"
ENV_ALLOW_NO_AUTH = "SEARCH_ALLOW_NO_AUTH"
HEADER_NAME = "X-API-Key"
HEADER_NAME_BYTES = b"x-api-key"        # ASGI scope 里的 header 名一律小写

NO_KEY_HINT = (
    f"检索服务未配置 {ENV_KEY} —— 请在项目根目录的 .env 里设置："
    f"python -c \"import secrets;print(secrets.token_urlsafe(32))\" 生成一个再填进去；"
    f"本机调试想彻底关掉鉴权，就设 {ENV_ALLOW_NO_AUTH}=1。"
)

load_dotenv_once()


def load_service_key() -> str:
    """共享密钥。唯一来源 = 环境变量 / `.env`，config.json 不参与。"""
    return (os.environ.get(ENV_KEY) or "").strip()


def allow_no_auth() -> bool:
    """显式关闭鉴权的开关（本机调试用）。"""
    return (os.environ.get(ENV_ALLOW_NO_AUTH) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_configured() -> bool:
    return bool(load_service_key())


def decide(provided: str | None) -> tuple[bool, int, str]:
    """鉴权判定，返回 `(是否放行, 拒绝时的 HTTP 状态码, 拒绝原因)`。

    单独抽出来是为了**能直接单测**：三态判定不需要起服务、不需要造请求。
    """
    expected = load_service_key()
    if not expected:
        if allow_no_auth():
            return True, 200, ""
        return False, 503, NO_KEY_HINT
    if not provided or not secrets.compare_digest(provided.strip(), expected):
        # 用 compare_digest 而不是 == ：避免比较耗时随前缀长度变化（时序侧信道）
        return False, 401, f"缺少或错误的 {HEADER_NAME} 请求头"
    return True, 200, ""


def header_value(headers: list[tuple[bytes, bytes]]) -> str | None:
    """从 ASGI headers 里取 `X-API-Key`。"""
    for k, v in headers or ():
        if k == HEADER_NAME_BYTES:
            return v.decode("latin-1").strip()
    return None


class ApiKeyMiddleware:
    """纯 ASGI 中间件。**API 与 MCP-over-HTTP 共用同一套**，避免两份实现走偏。

    用中间件而不是 FastAPI 的 `Depends`：`Depends` 只盖住 path operation，
    `/docs`、`/openapi.json` 这类自动生成的路由会漏在外面；中间件一个不漏。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":          # lifespan / websocket 原样放行
            await self.app(scope, receive, send)
            return
        ok, status, detail = decide(header_value(scope.get("headers") or []))
        if ok:
            await self.app(scope, receive, send)
            return
        await _send_json(send, status, {"detail": detail})


async def _send_json(send: Callable, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "ENV_ALLOW_NO_AUTH",
    "ENV_KEY",
    "HEADER_NAME",
    "NO_KEY_HINT",
    "ApiKeyMiddleware",
    "allow_no_auth",
    "decide",
    "header_value",
    "is_configured",
    "load_service_key",
]
