"""图片代理：把 28car CDN 图片按需取回并本地缓存，让调用方只接触我们的域名。

为什么要这层：`vehicle_images` 里存的是 28car 原始 CDN 链接，调用方直接下载会
把**它的**出口 IP / TLS 指纹 / 请求节奏打在 28car 上。本模块把这一步搬到服务端，
调用方只请求 `searchcar.eazycar.top`，28car 看到的仍是它早就认识的「carinfo 抓取」。

策略（2026-09-29 定稿，按量级/风控评估选定的最省-shěng 方案）：
- **直连**：图片打的是 CDN 域（非主站），无 `msg_busy` 动态反爬，实测直连 200；
  不走代理池（省一跳、省额度）。宿主 IP 暴露与爬虫池空直连（`proxy.py`）同源，非新增。
- **磁盘缓存**：图片不可变（文件名带 hash），命中即本地返回、不再打 28car。
- **限流**：进程内令牌桶，出站 ~2 req/s，与爬虫节奏一致，不突发。
- **降级**：超时/非 200/占位小图/超大图 → 返回 None（端点翻 502），**不重试轰炸**。

⛔ **SSRF 红线**：`fetch_image` 只接受「已由 DB 查出的 URL」，端点只收
`vehicle_id + index`，绝不接受调用方传 URL。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("carinfo.search.image_proxy")

#: 单张抓取超时（秒）。实测有 CDN 响应慢到 25s，必须卡住。
FETCH_TIMEOUT = 10
#: 单张大小上限（防异常大文件）
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: 小于此字节数视为占位图/异常（实测有 632B 占位响应），按失败处理
_MIN_VALID_BYTES = 1024
#: 磁盘缓存文件数上限（超出按最久未访问淘汰）
_CACHE_MAX_FILES = int(os.environ.get("IMAGE_CACHE_MAX_FILES", "5000"))
#: 出站速率（张/秒）；<=0 关闭限流（仅测试用）
_RATE_PER_SEC = float(os.environ.get("IMAGE_RATE_PER_SEC", "2"))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
#: 浏览器缓存 30 天（图片不可变，调用方/CDN 中间层也能缓存）
IMG_CACHE_CONTROL = "public, max-age=2592000"


def _cache_dir() -> Path:
    return Path(os.environ.get("IMAGE_CACHE_DIR", "data/image_cache"))


def _public_base() -> str:
    return os.environ.get("CARINFO_PUBLIC_BASE", "").rstrip("/")


def proxied_image_url(vehicle_id: str, index: int) -> str:
    return f"{_public_base()}/vehicle/{vehicle_id}/image/{index}"


def proxied_cover_url(vehicle_id: str) -> str:
    return f"{_public_base()}/vehicle/{vehicle_id}/cover"


class _RateLimiter:
    """最小全局限流：保证相邻两次出站间隔 >= 1/rate。线程安全。"""

    def __init__(self, rate: float):
        self._interval = (1.0 / rate) if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self._interval


_limiter = _RateLimiter(_RATE_PER_SEC)


def _paths(url: str) -> tuple[Path, Path]:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    d = _cache_dir()
    return d / h, d / (h + ".type")


def _read_cache(url: str) -> tuple[bytes, str] | None:
    p, t = _paths(url)
    try:
        if p.exists() and t.exists():
            body = p.read_bytes()
            ctype = t.read_text(encoding="utf-8").strip() or "image/jpeg"
            os.utime(p, None)   # LRU 触达
            return body, ctype
    except OSError:
        return None
    return None


def _evict_if_needed() -> None:
    d = _cache_dir()
    try:
        files = [f for f in d.iterdir() if f.is_file() and not f.name.endswith(".type")]
    except OSError:
        return
    if len(files) <= _CACHE_MAX_FILES:
        return
    files.sort(key=lambda f: f.stat().st_mtime)
    for f in files[: len(files) - _CACHE_MAX_FILES]:
        try:
            f.unlink()
            f.with_name(f.name + ".type").unlink(missing_ok=True)
        except OSError:
            pass


def _write_cache(url: str, body: bytes, ctype: str) -> None:
    p, t = _paths(url)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, p)
        t.write_text(ctype, encoding="utf-8")
        _evict_if_needed()
    except OSError as e:
        log.warning("图片缓存写入失败（不影响本次返回）%s: %s", url, e)


def _http_get(url: str):
    """默认抓取实现：curl_cffi + 浏览器 TLS 指纹/UA，直连（不走代理池）。"""
    from curl_cffi import requests as curl_requests

    return curl_requests.get(
        url,
        headers={"User-Agent": _UA, "Referer": "https://www.28car.com/"},
        timeout=FETCH_TIMEOUT,
        allow_redirects=True,
        impersonate="chrome",
    )


def fetch_image(url: str | None, *, get=None) -> tuple[bytes, str] | None:
    """取图。命中缓存不发请求；失败返回 None（调用方降级为无图）。

    `get`：抓取函数注入点（离线测试用），默认 `_http_get`。
    """
    if not url:
        return None

    cached = _read_cache(url)
    if cached is not None:
        return cached

    get = get or _http_get
    _limiter.wait()
    try:
        resp = get(url)
    except Exception as e:  # noqa: BLE001 —— 网络任何异常都按「取不到」处理
        log.warning("图片抓取失败 %s: %s", url, e)
        return None

    status = getattr(resp, "status_code", None)
    body = getattr(resp, "content", None)
    headers = getattr(resp, "headers", None) or {}
    ctype = ""
    try:
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip()
    except Exception:  # noqa: BLE001
        ctype = ""

    if status != 200 or not body:
        log.info("图片上游非 200/空：%s -> %s", url, status)
        return None
    if len(body) < _MIN_VALID_BYTES or len(body) > MAX_IMAGE_BYTES:
        log.info("图片大小异常（占位/超大）：%s -> %d bytes", url, len(body))
        return None
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"

    _write_cache(url, body, ctype)
    return body, ctype
