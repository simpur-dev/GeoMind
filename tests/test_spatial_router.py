"""L4 空间计算路由 SpatialRouter 单测。

铁律 6：永不调真实外部服务——高德一律 httpx.MockTransport（样板见 test_amap_gateway.py），
库内兜底用 OB 桩（不连 SeekDB）。真实 key 冒烟是 docs/开发进度.md 里的一次性脚本，不进 pytest。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

import httpx
import pytest

from backend.config import Settings
from backend.query_builder import QueryBuilder
from backend.services.amap_gateway import AmapGateway
from backend.services.spatial_router import (
    PlaceRef,
    RouteUnavailable,
    SpatialRouter,
    isochrone_geojson,
)

_M_PER_DEG = 111_320.0


def flat_dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """平面近似距离（与路由层 _destination_point 同口径，用于对拍采样点半径）。"""
    dlon = (a[0] - b[0]) * _M_PER_DEG * math.cos(math.radians(b[1]))
    dlat = (a[1] - b[1]) * _M_PER_DEG
    return math.hypot(dlon, dlat)


def bearing_of(origin: tuple[float, float], dest: tuple[float, float]) -> float:
    """origin→dest 的方位角（0=正北，顺时针），用于给不同方位角配不同耗时。"""
    dlon = (dest[0] - origin[0]) * math.cos(math.radians(origin[1]))
    dlat = dest[1] - origin[1]
    return math.degrees(math.atan2(dlon, dlat)) % 360.0


class ObStub:
    """最小 SeekDB 桩：只提供 QueryBuilder 三层卡点与取行所需的方法。"""

    def __init__(self, rows: list[dict[str, Any]], plan: str = "TABLE FULL SCAN") -> None:
        self.rows = rows
        self.plan = plan
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return self.rows

    def explain(self, sql: str, params: dict[str, Any] | None = None) -> str:
        return self.plan


def make_router(handler: Any = None, *, key: str = "test-key",
                rows: list[dict[str, Any]] | None = None,
                place_dict: dict[str, tuple[float, float]] | None = None) -> SpatialRouter:
    """构造路由：有 key 时注入 MockTransport，无 key 时不产生任何外部请求。"""
    s = Settings(amap_key=key, amap_qps=50)
    transport = httpx.MockTransport(handler) if handler is not None else None
    amap = AmapGateway(s, transport=transport)
    ob = ObStub(rows if rows is not None else [])
    return SpatialRouter(amap, s, QueryBuilder(ob, s), place_dict=place_dict)


# ---------------------------------------------------------------------------
# geocode：在线 + 三级兜底
# ---------------------------------------------------------------------------
def test_geocode_online_returns_wgs84_with_chengdu_offset():
    """验收：高德返回 GCJ-02，出口归一为 WGS84，两者差值在成都偏移量级（300-600 m）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/geocode/geo"
        assert request.url.params["address"] == "春熙路"
        # 缺省城市限定必须下发：真实 key 实测无 city 时「春熙路」会命中外省同名路
        assert request.url.params["city"] == "成都"
        return httpx.Response(200, json={"status": "1", "geocodes": [
            {"location": "104.0817,30.6570", "formatted_address": "四川省成都市锦江区春熙路"}]})

    p = make_router(handler).geocode("春熙路")
    assert p.source == "amap"
    assert (p.lon, p.lat) != p.gcj02
    assert flat_dist_m((p.lon, p.lat), p.gcj02) == pytest.approx(400, abs=120)
    assert p.gcj02 == (104.0817, 30.6570)     # 高德原值保留，供 §9.3 图层直用


def test_geocode_city_default_is_configurable():
    """GEOMIND_DEFAULT_CITY 留空 = 不加城市过滤（全国匹配由调用方负责）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("city", ""))
        return httpx.Response(200, json={"status": "1", "geocodes": [{"location": "121.49,31.24"}]})

    s = Settings(amap_key="test-key", amap_qps=50, default_city="")
    router = SpatialRouter(AmapGateway(s, transport=httpx.MockTransport(handler)), s)
    p = router.geocode("外滩", city="上海")
    assert p.source == "amap"
    assert seen == ["上海"]
    router2 = SpatialRouter(AmapGateway(s, transport=httpx.MockTransport(handler)), s)
    router2.geocode("南京路")
    assert seen[-1] == ""


def test_geocode_offline_uses_place_dict():
    r = make_router(key="", place_dict={"春熙路": (104.0817, 30.6570)})
    p = r.geocode("春熙路")
    assert p.source == "place_dict" and (p.lon, p.lat) == (104.0817, 30.6570)


def test_geocode_offline_dict_substring_and_case():
    """子串与大小写："春熙路1号" 命中词表 "春熙路"；"IFS IFS" 小写后命中 "ifs"。"""
    r = make_router(key="", place_dict={"春熙路": (104.0817, 30.657), "ifs": (104.08, 30.66)})
    assert r.geocode("春熙路1号").name == "春熙路"
    assert r.geocode("IFS IFS").name == "ifs"
    assert r.geocode("ifs").name == "ifs"


def test_geocode_offline_falls_back_to_dim_poi():
    """词表未命中 → dim_poi（走 QueryBuilder 模板 + 三层卡点，参数化 LIKE）。"""
    rows = [{"poi_id": 6469, "name": "环球中心24h健身", "address": "成都市武侯区光华大道372号",
             "city": "成都", "lon": 104.345518, "lat": 30.593414}]
    r = make_router(key="", rows=rows, place_dict={})
    p = r.geocode("环球中心", city="成都")
    assert p.source == "dim_poi" and p.poi_id == 6469
    assert (p.lon, p.lat) == (104.345518, 30.593414)      # 库内即 WGS84，不转换
    sql, params = r.query_builder.ob.calls[-1]
    assert "dim_poi" in sql and "%(name_like)s" in sql
    assert params["name_like"] == "%环球中心%"


def test_geocode_dim_poi_like_wildcards_escaped():
    """用户输入里的 LIKE 通配符被转义，不当成模式扩大匹配（仍全参数化）。"""
    r = make_router(key="", rows=[{"poi_id": 1, "name": "x", "lon": 1.0, "lat": 2.0}], place_dict={})
    r.geocode("50%_a")
    assert r.query_builder.ob.calls[-1][1]["name_like"] == "%50\\%\\_a%"


def test_geocode_all_miss_raises_route_unavailable_without_amap_error():
    """验收：三级兜底全 miss 抛带提示的 RouteUnavailable（高德专有异常不外泄，类型即证明）。"""
    r = make_router(key="", place_dict={})
    with pytest.raises(RouteUnavailable) as exc:
        r.geocode("火星基地")
    assert "GEOMIND_AMAP_KEY" in str(exc.value) and "dim_poi" in str(exc.value)


def test_geocode_api_error_falls_back_not_raises():
    """有 key 但高德业务失败（status=0）：继续走兜底链，不向上抛高德异常。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"})

    r = make_router(handler, place_dict={"春熙路": (104.0817, 30.657)})
    assert r.geocode("春熙路").source == "place_dict"


def test_geocode_empty_and_db_error_messages():
    r = make_router(key="", place_dict={})
    with pytest.raises(RouteUnavailable, match="地址为空"):
        r.geocode("  ")

    class BoomOb(ObStub):
        def execute(self, sql: Any, params: Any = None) -> list[dict[str, Any]]:
            raise RuntimeError("SeekDB 连接失败")

    s = Settings(amap_key="", amap_qps=50)
    broken = SpatialRouter(AmapGateway(s), s, QueryBuilder(BoomOb([]), s), place_dict={})
    with pytest.raises(RouteUnavailable, match="SeekDB 连接失败"):
        broken.geocode("环球中心")


# ---------------------------------------------------------------------------
# resolve / PlaceRef
# ---------------------------------------------------------------------------
def test_resolve_lonlat_is_input_wgs84():
    p = make_router(key="").resolve(PlaceRef(lonlat=(104.0817, 30.6570)))
    assert p.source == "input" and (p.lon, p.lat) == (104.0817, 30.6570)
    assert p.gcj02 != (104.0817, 30.6570)      # GCJ 副产物由路由层算好


def test_placeref_requires_exactly_one_of_name_or_lonlat():
    with pytest.raises(ValueError, match="name 或 lonlat"):
        PlaceRef()
    with pytest.raises(ValueError, match="name 或 lonlat"):
        PlaceRef(name="春熙路", lonlat=(104.0, 30.6))
    with pytest.raises(ValueError, match="经纬度范围"):
        PlaceRef(lonlat=(200.0, 30.6))


# ---------------------------------------------------------------------------
# path_planning：只有在线一条路
# ---------------------------------------------------------------------------
def _driving_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v3/direction/driving"
    return httpx.Response(200, json={"status": "1", "route": {"paths": [
        {"distance": "4200", "duration": "780",
         "steps": [{"polyline": "104.08,30.65;104.09,30.66"},
                   {"polyline": "104.09,30.66;104.10,30.67"}]}]}})


def test_path_planning_normalizes_polyline_to_wgs84():
    r = make_router(_driving_handler)
    res = r.plan_route("driving", (104.08, 30.65), (104.10, 30.67))
    assert res.distance_m == 4200.0 and res.duration_s == 780.0
    assert len(res.polyline_wgs84) == 4 == len(res.polyline_gcj02)
    assert res.polyline_wgs84[0] != (104.08, 30.65)
    assert res.origin.source == "input" and res.destination.source == "input"


def test_path_planning_resolves_place_names_online():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/geocode/geo":
            seen.append(request.url.params["address"])
            loc = "104.0817,30.6570" if "春熙" in request.url.params["address"] else "104.0665,30.6575"
            return httpx.Response(200, json={"status": "1", "geocodes": [{"location": loc}]})
        assert request.url.path == "/v3/direction/driving"
        return httpx.Response(200, json={"status": "1", "route": {"paths": [
            {"distance": "1800", "duration": "420",
             "steps": [{"polyline": "104.0817,30.6570;104.0665,30.6575"}]}]}})

    res = make_router(handler).plan_route(
        "driving", PlaceRef(name="春熙路"), PlaceRef(name="天府广场"))
    assert seen == ["春熙路", "天府广场"]
    assert res.origin.source == "amap" and res.destination.name == "天府广场"
    assert res.distance_m == 1800.0


def test_path_planning_no_fallback_and_no_amap_error_leak():
    r = make_router(key="")
    with pytest.raises(RouteUnavailable) as exc:
        r.plan_route("driving", (104.08, 30.65), (104.06, 30.66))
    assert "无路网" in str(exc.value) and "空间谎言" in str(exc.value)


def test_path_planning_rejects_unknown_mode():
    with pytest.raises(ValueError, match="出行方式"):
        make_router(_driving_handler).plan_route("transit", (104.08, 30.65), (104.06, 30.66))


def test_path_planning_no_route_is_route_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "1", "route": {"paths": []}})

    with pytest.raises(RouteUnavailable, match="未规划出可行路线"):
        make_router(handler).plan_route("driving", (104.08, 30.65), (104.06, 30.66))


# ---------------------------------------------------------------------------
# isochrone：方位采样近似 + 正圆降级
# ---------------------------------------------------------------------------
def _distance_handler(*, slow_bearings: tuple[float, ...] = (),
                      speed_mps: float = 10.0, slow_factor: float = 4.0):
    """造一个「按真实采样几何算耗时」的 distance API 假响应。

    origins 顺序 = 12 方位 × 5 档半径；origin_id 从 1 起（网关归一为下标）。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/distance"
        dest = tuple(float(x) for x in request.url.params["destination"].split(","))
        origins = [tuple(float(x) for x in o.split(","))
                   for o in request.url.params["origins"].split("|")]
        results = []
        for i, o in enumerate(origins):
            b = round(bearing_of(dest, o)) % 360
            dist = flat_dist_m(dest, o)
            factor = slow_factor if b in slow_bearings else 1.0
            results.append({"origin_id": str(i + 1), "distance": f"{dist:.1f}",
                            "duration": f"{dist / speed_mps * factor:.1f}"})
        return httpx.Response(200, json={"status": "1", "results": results})

    return handler


def test_isochrone_samples_60_points_and_saturates():
    """15 分钟 × 10 m/s（=9 km）超出 3 km 采样上限 → 全部命中最远档，saturated=True。"""
    r = make_router(_distance_handler())
    res = r.isochrone((104.0817, 30.6570), minutes=15)
    assert res.sampled_points == 60 and res.degraded is False
    assert res.saturated is True and res.under_sampled == 0
    assert [v.bearing_deg for v in res.vertices] == [float(b) for b in range(0, 360, 30)]
    assert all(v.radius_m == 3000.0 for v in res.vertices)
    assert all(v.duration_s <= 15 * 60 for v in res.vertices)


def test_isochrone_picks_farthest_qualifying_radius():
    """部分方位角慢：慢方位取到 500 m，快方位取到 3000 m，混合且不打 degraded。"""
    slow = (30.0, 60.0, 90.0, 120.0, 150.0)
    r = make_router(_distance_handler(slow_bearings=slow, slow_factor=4.0))
    res = r.isochrone((104.0817, 30.6570), minutes=6)      # 预算 360 s（刻意避开 300 s 边界）
    by_bearing = {v.bearing_deg: v.radius_m for v in res.vertices}
    assert all(by_bearing[b] == 500.0 for b in slow)
    assert all(v == 3000.0 for b, v in by_bearing.items() if b not in slow)
    assert res.saturated is True and res.under_sampled == 0 and res.degraded is False


def test_isochrone_vertex_radius_matches_sampled_geometry():
    """顶点半径与采样点几何自洽（对拍 _destination_point 构造，防漂移）。"""
    r = make_router(_distance_handler())
    res = r.isochrone((104.0817, 30.6570), minutes=1)       # 60 s → 600 m → 只有 500 档合格
    assert res.saturated is False
    for v in res.vertices:
        assert flat_dist_m(res.center.gcj02, v.gcj02) == pytest.approx(v.radius_m, abs=2.0)


def test_isochrone_polygon_wkt_has_12_vertices_and_closes():
    r = make_router(_distance_handler())
    res = r.isochrone((104.0817, 30.6570), minutes=10)
    pairs = re.findall(r"(-?\d+\.\d+) (-?\d+\.\d+)", res.polygon_wkt_wgs84)
    assert len(res.vertices) == 12 and len(pairs) == 13       # 闭合重复首点
    assert pairs[0] == pairs[-1]
    assert res.polygon_wkt_wgs84.startswith("POLYGON((") and res.polygon_wkt_wgs84.endswith("))")
    gcj_pairs = re.findall(r"-?\d+\.\d+ -?\d+\.\d+", res.polygon_wkt_gcj02)
    assert len(gcj_pairs) == 13 and res.polygon_wkt_wgs84 != res.polygon_wkt_gcj02


def test_isochrone_degrades_to_circle_without_key():
    """无 key：半径 = T × 名义平均速度 的正圆近似，degraded=True（验收项）。"""
    res = make_router(key="").isochrone((104.0817, 30.6570), minutes=15, mode="driving")
    assert res.degraded is True and res.sampled_points == 0 and len(res.vertices) == 12
    expected_r = 20.0 * 1000 / 60 * 15         # 驾车名义 20 km/h → 500 m/min → 15 min = 5 km
    assert all(v.radius_m == pytest.approx(expected_r, abs=1.0) for v in res.vertices)
    assert res.vertices[0].lat > 30.6570          # 方位角 0 = 正北
    walking = make_router(key="").isochrone((104.0817, 30.6570), minutes=15, mode="walking")
    assert walking.vertices[0].radius_m == pytest.approx(4.0 * 1000 / 60 * 15, abs=1.0)


def test_isochrone_rejects_bicycling_because_distance_api_has_no_cycling_type():
    """高德 /v3/distance 实测只有 0 直线 / 1 驾车 / 3 步行：骑行不硬套一个错误的 type。"""
    with pytest.raises(ValueError, match="无骑行档"):
        make_router(key="").isochrone((104.0817, 30.6570), minutes=15, mode="bicycling")


def test_isochrone_all_under_sampled_raises():
    """每个方位角连最近档都到不了：不硬造多边形（抛 RouteUnavailable）。"""
    all_slow = tuple(float(b) for b in range(0, 360, 30))
    r = make_router(_distance_handler(slow_bearings=all_slow, slow_factor=100.0))
    with pytest.raises(RouteUnavailable, match="等时圈不成立"):
        r.isochrone((104.0817, 30.6570), minutes=1)


def test_isochrone_partial_under_sampled_uses_inner_half():
    """单方位角到不了最近档 → 半径退化 250 m、under_sampled=1，且耗时回填最近档真值（不写 0 s）。"""
    r = make_router(_distance_handler(slow_bearings=(270.0,), slow_factor=100.0))
    res = r.isochrone((104.0817, 30.6570), minutes=6)          # 预算 360 s
    assert res.under_sampled == 1
    bad = next(v for v in res.vertices if v.bearing_deg == 270.0)
    assert bad.radius_m == 250.0
    assert bad.duration_s == pytest.approx(5000.0, abs=60)      # 500 m × factor 100 / 10 m·s⁻¹
    assert bad.duration_s > 6 * 60                             # 必然超预算，不会被读成「0 秒可达」
    assert all(v.radius_m == 3000.0 for v in res.vertices if v.bearing_deg != 270.0)


def test_isochrone_api_error_does_not_become_circle():
    """有 key 却调用失败：抛 RouteUnavailable，不把网络故障粉饰成几何近似。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "0", "infocode": "10004", "info": "DAILY_QUERY_OVER_LIMIT"})

    with pytest.raises(RouteUnavailable, match="等时圈采样失败"):
        make_router(handler).isochrone((104.0817, 30.6570), minutes=15)


def test_isochrone_rejects_out_of_range_minutes():
    r = make_router(key="")
    with pytest.raises(ValueError, match="minutes"):
        r.isochrone((104.0817, 30.6570), minutes=0)
    with pytest.raises(ValueError, match="minutes"):
        r.isochrone((104.0817, 30.6570), minutes=121)


def test_isochrone_center_is_geocoded_first():
    """中心给地名：先解析坐标（无 key 走词表），再算等时圈。"""
    r = make_router(key="", place_dict={"春熙路": (104.0817, 30.6570)})
    res = r.isochrone(PlaceRef(name="春熙路"), minutes=10)
    assert res.center.source == "place_dict" and (res.center.lon, res.center.lat) == (104.0817, 30.6570)


# ---------------------------------------------------------------------------
# GeoJSON 出口（§9.3 图层坐标系 = GCJ-02）
# ---------------------------------------------------------------------------
def test_isochrone_geojson_is_closed_gcj_polygon_with_honest_props():
    res = make_router(key="").isochrone((104.0817, 30.6570), minutes=10, mode="walking")
    fc = isochrone_geojson(res, {"session_id": "s1"})
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 1
    geom = fc["features"][0]["geometry"]
    ring = geom["coordinates"][0]
    assert geom["type"] == "Polygon" and len(ring) == 13 and ring[0] == ring[-1]
    assert ring[0] != [res.vertices[0].lon, res.vertices[0].lat]      # 图层用 GCJ，不是 WGS
    props = fc["features"][0]["properties"]
    assert props["kind"] == "isochrone" and props["degraded"] is True
    assert props["mode"] == "walking" and props["session_id"] == "s1"
    assert json.dumps(fc, ensure_ascii=False)                         # 进 SSE 必须 JSON 安全
