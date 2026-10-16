"""坐标系转换与 GeoJSON 构造（架构 §9.6：空间计算归后端，前端零计算）。

库内为 WGS84（DDL 铁律：SRID 0 存值），高德底图为 GCJ-02，
本模块在 API 出口处完成转换，前端拿到的 GeoJSON 直接可渲染。

算法为公开的中国测绘偏移标准变换（克里托夫斯基椭球 + 非线性纬度/经度
修正），纯函数、无外部依赖、可对拍：
- wgs84_to_gcj02：GPS 原始坐标 → 高德/腾讯国内底图坐标
- gcj02_to_wgs84：逆变换（迭代法，用于前端手势回传后翻译回 WGS84）

注意：逆变换是近似解（公开算法性质），往返误差在米级；对"框选回传 →
重新检索"场景足够（后端另有 MBR 容差，见 query_builder）。
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

# 克拉托夫斯基椭球长半轴与第一偏心率平方（公开常量）
_K_A = 6378245.0
_K_EE = 0.00669342162296594323
_PI = math.pi


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320.0 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def _out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """WGS84 → GCJ-02。境外坐标原样返回（高德底图同理不偏移）。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _K_EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_K_A * (1 - _K_EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_K_A / sqrtmagic * math.cos(radlat) * _PI)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """GCJ-02 → WGS84（迭代逆变换，20 次迭代收敛）。"""
    if _out_of_china(lng, lat):
        return lng, lat
    wlng, wlat = lng, lat
    for _ in range(20):
        glng, glat = wgs84_to_gcj02(wlng, wlat)
        wlng += lng - glng
        wlat += lat - glat
    return wlng, wlat


# ---------------------------------------------------------------------------
# GeoJSON 构造（坐标已转 GCJ-02）
# ---------------------------------------------------------------------------
def _feature(lon: float, lat: float, props: dict[str, Any]) -> dict[str, Any]:
    glng, glat = wgs84_to_gcj02(lon, lat)
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(glng, 6), round(glat, 6)]},
            "properties": props}


def poi_feature_collection(pois: list[dict[str, Any]]) -> dict[str, Any]:
    """候选 POI 点层（kind=poi）。pois: [{poi_id, name, rating, dist_m, sem_dist, lon, lat}]。"""
    feats = []
    for p in pois:
        if p.get("lon") is None or p.get("lat") is None:
            continue  # 无坐标的点跳过（不编造位置）
        feats.append(_feature(float(p["lon"]), float(p["lat"]), {
            "poi_id": p.get("poi_id"), "name": p.get("name", ""),
            "rating": float(p["rating"]) if p.get("rating") is not None else None,
            "dist_m": p.get("dist_m"), "sem_dist": p.get("sem_dist"),
        }))
    return {"type": "FeatureCollection", "features": feats}


def grid_feature_collection(grids: list[dict[str, Any]], value_key: str = "flow") -> dict[str, Any]:
    """网格中心点层（kind=heatmap / grid）。

    grids: [{grid_id, center_lon, center_lat, flow, poi_cnt, avg_rating, score, ...}]。
    value_key 指定热力权重字段（缺省 flow）。
    """
    feats = []
    for g in grids:
        if g.get("center_lon") is None or g.get("center_lat") is None:
            continue
        props: dict[str, Any] = {"grid_id": g.get("grid_id")}
        for k in (value_key, "poi_cnt", "avg_rating", "score"):
            v = g.get(k)
            if v is not None:
                props[k] = float(v) if k in (value_key, "avg_rating", "score") else int(v)
        feats.append(_feature(float(g["center_lon"]), float(g["center_lat"]), props))
    return {"type": "FeatureCollection", "features": feats}


def viewport_from_center(center: tuple[float, float], radius_m: float) -> dict[str, Any]:
    """由查询中心与半径推算镜头（GCJ-02），保证首屏即可见全部候选。"""
    glng, glat = wgs84_to_gcj02(center[0], center[1])
    # 经验映射：1.5 km → z14，3 km → z13，6 km → z12（高德 zoom 语义）
    zoom = 16 if radius_m <= 800 else 15 if radius_m <= 1600 else 14 if radius_m <= 3200 else 13
    return {"center": [round(glng, 6), round(glat, 6)], "zoom": zoom, "pitch": 0.0}


# 降级画布的投影口径（web/src/layers/fallback.ts 的 spanDeg）：可见宽度
# span(z) = 0.02° / 2^(z-13)。高德侧 z0 全宽 360°，同一量级且随 z 单调收缩，
# 所以按这个模型算出的 z 在两档里都是「够宽」的方向（对高德还偏保守）。
_SPAN_AT_13_DEG = 0.02


def viewport_covering(center: tuple[float, float], radius_m: float,
                      margin: float = 1.2) -> dict[str, Any]:
    """镜头必须**装下**以 center 为圆心、radius_m 为半径的圆（等时圈这类几何用）。

    与 `viewport_from_center` 的分工要说清：那个是给候选簇用的经验档位（半径 1.5 km
    也只用 z15，视野约 1.1 km——簇点靠中心，看不全外圈也不影响判读），本函数则是几何
    装箱：`span(z) >= 2 * radius * margin`。两者并存，POI 镜头的既有钉死行为不变。
    """
    if radius_m <= 0:
        raise ValueError(f"radius_m 必须为正，收到 {radius_m}")
    need_deg = 2.0 * (radius_m / 111_320.0) * margin
    zoom = 13 + math.floor(math.log2(_SPAN_AT_13_DEG / need_deg))
    zoom = max(3, min(20, zoom))
    glng, glat = wgs84_to_gcj02(center[0], center[1])
    return {"center": [round(glng, 6), round(glat, 6)], "zoom": zoom, "pitch": 0.0}


def line_feature_collection(points_wgs84: Sequence[tuple[float, float]],
                            props: dict[str, Any]) -> dict[str, Any]:
    """线层（kind=route，架构 §9.3）：WGS84 折点 → 出口 GCJ-02 的 LineString Feature。

    少于 2 个点返回空集合——两点才成线，不编造几何（与 poi/grid 渲染器同口径）。
    """
    coords: list[list[float]] = []
    for pt in points_wgs84:
        glng, glat = wgs84_to_gcj02(float(pt[0]), float(pt[1]))
        coords.append([round(glng, 6), round(glat, 6)])
    if len(coords) < 2:
        return {"type": "FeatureCollection", "features": []}
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }]}
