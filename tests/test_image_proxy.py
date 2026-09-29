"""图片代理（**离线**）：缓存命中不发请求、异常降级、URL 构造、SSRF 边界。

代理把「调用方直连 28car」转成「服务端按 DB 查出的 URL 取图并缓存」，调用方只见
我们的域名。这里锁住降级与缓存行为，以及 URL 只由 vehicle_id 拼、不接受外部 URL。
"""

from __future__ import annotations

import pytest

from carinfo.search import image_proxy as ip


class FakeResp:
    def __init__(self, status_code=200, content=b"", content_type="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Type": content_type}


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_CACHE_DIR", str(tmp_path / "img"))
    monkeypatch.setattr(ip._limiter, "wait", lambda: None)   # 测试不等待限流
    yield


def test_cache_hit_skips_upstream(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return FakeResp(content=b"x" * 5000)

    first = ip.fetch_image("https://cdn/img1.jpg", get=get)
    second = ip.fetch_image("https://cdn/img1.jpg", get=get)
    assert first == second
    assert len(calls) == 1, "第二次应命中缓存、不再打上游"


def test_placeholder_small_body_rejected():
    got = ip.fetch_image("https://cdn/small.jpg", get=lambda u: FakeResp(content=b"x" * 632))
    assert got is None, "占位小图应按失败处理"


def test_non_200_rejected():
    assert ip.fetch_image("https://cdn/404.jpg", get=lambda u: FakeResp(status_code=404)) is None


def test_oversized_rejected():
    big = b"x" * (ip.MAX_IMAGE_BYTES + 1)
    assert ip.fetch_image("https://cdn/big.jpg", get=lambda u: FakeResp(content=big)) is None


def test_non_image_content_type_coerced():
    got = ip.fetch_image("https://cdn/a.jpg",
                         get=lambda u: FakeResp(content=b"x" * 5000, content_type="application/octet-stream"))
    assert got is not None and got[1] == "image/jpeg"


def test_get_exception_degrades_to_none():
    def boom(url):
        raise RuntimeError("network down")

    assert ip.fetch_image("https://cdn/boom.jpg", get=boom) is None


def test_none_url_returns_none():
    assert ip.fetch_image(None) is None


def test_proxied_urls_use_public_base(monkeypatch):
    monkeypatch.setenv("CARINFO_PUBLIC_BASE", "https://searchcar.eazycar.top/")
    assert ip.proxied_cover_url("v123") == "https://searchcar.eazycar.top/vehicle/v123/cover"
    assert ip.proxied_image_url("v123", 2) == "https://searchcar.eazycar.top/vehicle/v123/image/2"


def test_proxied_urls_relative_when_base_unset(monkeypatch):
    monkeypatch.delenv("CARINFO_PUBLIC_BASE", raising=False)
    assert ip.proxied_cover_url("v1") == "/vehicle/v1/cover"
    assert ip.proxied_image_url("v1", 0) == "/vehicle/v1/image/0"
