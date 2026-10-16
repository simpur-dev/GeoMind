"""高德适配网关单测：MockTransport 注入，不依赖真实 key / 不产生外部请求。

覆盖：无 key 降级、字段归一、TTL 缓存命中、数字签名、业务错误转译、
距离矩阵 origin_id → 下标归一。
"""
from __future__ import annotations

import httpx
import pytest

from backend.config import Settings
from backend.services.amap_gateway import (
    AmapApiError,
    AmapGateway,
    AmapUnavailable,
    amap_signature,
)


def make_gateway(handler, key: str = "test-key", **kw) -> tuple[AmapGateway, list[httpx.Request]]:
    """构造带请求记录的网关（返回网关 + 已发生的请求列表，供断言）。"""
    seen: list[httpx.Request] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    s = Settings(amap_key=key, amap_qps=50, **kw)
    return AmapGateway(s, transport=httpx.MockTransport(counting_handler)), seen


def test_no_key_raises_without_http():
    """无 key：直接抛 AmapUnavailable，不产生任何外部请求。"""
    gw, seen = make_gateway(lambda r: pytest.fail("不应发起 HTTP"), key="")
    assert gw.available is False
    with pytest.raises(AmapUnavailable):
        gw.geocode("春熙路")
    assert seen == []


def test_geocode_normalizes_location():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/geocode/geo"
        assert request.url.params["address"] == "天府广场"
        return httpx.Response(200, json={"status": "1", "geocodes": [
            {"location": "104.065735,30.659462"}]})

    gw, _ = make_gateway(handler)
    assert gw.geocode("天府广场") == (104.065735, 30.659462)


def test_cache_hits_within_ttl():
    """同参数二次调用命中缓存，不发第二次 HTTP（§7 缓存策略）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"status": "1", "geocodes": [
            {"location": "104.06,30.65"}]})

    gw, _ = make_gateway(handler)
    gw.geocode("春熙路")
    gw.geocode("春熙路")
    assert calls["n"] == 1


def test_signature_appended_and_correct():
    """配私钥时请求带 sig 参数，值与官方算法一致（参数升序→URL编码→拼接私钥→md5）。"""
    gw, seen = make_gateway(lambda r: httpx.Response(200, json={"status": "1", "geocodes": []}),
                            amap_signature_secret="test-secret")
    gw.geocode("春熙路", city="成都")
    got = dict(seen[0].url.params)
    expect = amap_signature(
        {"address": "春熙路", "city": "成都", "key": "test-key", "output": "json"},
        "test-secret")
    assert got["sig"] == expect


def test_business_error_translates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "0", "infocode": "10001",
                                         "info": "INVALID_USER_KEY"})

    gw, _ = make_gateway(handler)
    with pytest.raises(AmapApiError, match="10001"):
        gw.geocode("春熙路")


def test_place_around_normalization():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["location"] == "104.0817,30.657"
        return httpx.Response(200, json={"status": "1", "pois": [
            {"name": "瑞幸咖啡", "location": "104.08,30.66", "address": "某路1号",
             "typecode": "050500", "tel": "028-123"}]})

    gw, _ = make_gateway(handler)
    pois = gw.place_around((104.0817, 30.657), radius_m=1000, limit=5)
    assert pois == [{"name": "瑞幸咖啡", "location": (104.08, 30.66),
                     "address": "某路1号", "typecode": "050500", "tel": "028-123"}]


def test_route_plan_driving_polyline():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/direction/driving"
        return httpx.Response(200, json={"status": "1", "route": {"paths": [
            {"distance": "4200", "duration": "780",
             "steps": [{"polyline": "104.08,30.65;104.09,30.66"},
                       {"polyline": "104.09,30.66;104.10,30.67"}]}]}})

    gw, _ = make_gateway(handler)
    r = gw.route_plan("driving", (104.08, 30.65), (104.10, 30.67))
    assert r is not None
    assert r["distance_m"] == 4200 and r["duration_s"] == 780
    assert r["polyline"] == [(104.08, 30.65), (104.09, 30.66), (104.09, 30.66), (104.10, 30.67)]


def test_distance_matrix_origin_index():
    """高德 origin_id 是从 1 起的序号 → 归一化为 origins 下标。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "1", "results": [
            {"origin_id": "1", "distance": "1200", "duration": "300"},
            {"origin_id": "2", "distance": "3500", "duration": "600"}]})

    gw, _ = make_gateway(handler)
    out = gw.distance_matrix([(104.08, 30.65), (104.09, 30.66)], (104.10, 30.67))
    assert [o["origin_index"] for o in out] == [0, 1]
    assert [o["distance_m"] for o in out] == [1200.0, 3500.0]
