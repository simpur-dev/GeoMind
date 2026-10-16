"""L4 空间计算服务：计算路由（架构 §2.2 L4「空间计算服务」/ §7 对接边界）。

职责严格两件事，其余一律不做：
  1. **能力路由**——能下推 SeekDB 的（距离/包含/连接/聚合）由 QueryBuilder 负责（本模块不碰）；
     OB 内核没有路网、没有实时路况、没有地址库，因此路径规划 / 地理编码 / 等时圈走高德网关。
  2. **结果归一**——高德各异构响应 + 库内兜底结果统一成同一组 dataclass，
     并且**出口坐标统一 WGS84**（与库内一致，架构 §6.3 DDL 铁律）。

坐标系（架构 §9.6 职责边界表：转换属后端，前端零计算）：
  高德=GCJ-02，库内=WGS84。转换只在本模块发生（复用 backend/api/geo.py 纯函数）。
  同时保留 GCJ-02 副产物，因为 §9.3 Map Spec 契约层的坐标定义就是 GCJ-02。

几何计算的边界（铁律 2 的同一口径，见 query_builder._wkt_bbox 注释）：
  方位角采样点推算、正圆近似、WKT 顶点串拼接都是**边界构造**，不是**空间裁决**；
  包含 / 距离 / 连接判定一律下推 OB ST_*，本模块不做。

降级链（无 key 或网关异常时，逐能力定义，架构 §7 归属表）：
  geocode      → 意图地名词表 PLACE_COORDS → dim_poi name LIKE（QueryBuilder）→ RouteUnavailable
  path_planning→ 无兜底（OB 无路网，直线近似是空间谎言）→ RouteUnavailable
  isochrone    → 无 key 时 T×平均速度正圆近似（degraded=True）；有 key 但调用失败时
                 不降级（不把网络故障粉饰成几何近似）→ RouteUnavailable
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from backend.api.geo import gcj02_to_wgs84, wgs84_to_gcj02
from backend.brain.intent import PLACE_COORDS
from backend.config import Settings, get_settings
from backend.query_builder import (
    CompileError,
    DryRunError,
    PlanAssertionError,
    PoiNameSpec,
    QueryBuilder,
    SQLValidationError,
)
from backend.services.amap_gateway import AmapApiError, AmapGateway, AmapUnavailable

RouteMode = Literal["driving", "walking", "bicycling"]
# 等时圈只支持驾车与步行：高德 /v3/distance 没有骑行档（2026-10-16 真实 key 实测）
IsoMode = Literal["driving", "walking"]

# 米/度（与 config.grid_ids_in_radius、query_builder 的 MBR 换算同一口径）
_M_PER_DEG = 111_320.0

# 等时圈采样网格（架构 §7：高德无等时圈 Web 服务，用距离测量方位采样近似）
# 12 方位 × 5 档 = 60 个 origin，在高德单次上限 100 内（amap_gateway.distance_matrix 已拦）
_BEARINGS: tuple[float, ...] = tuple(float(b) for b in range(0, 360, 30))
_RADII_M: tuple[float, ...] = (500.0, 1000.0, 1500.0, 2000.0, 3000.0)
_NO_SAMPLE_FALLBACK_M = _RADII_M[0] / 2.0  # 该方位角无任何合格采样点时的退化半径

# 高德 /v3/distance 的 type 语义（2026-10-16 真实 key 实测钉死，起终点为正北直线 3000 m）：
#   type=0 → distance 2999 m、duration 0 s    = 直线距离（**不是步行**）
#   type=1 → distance 6089 m、duration 1284 s = 驾车（路网，均速 17.1 km/h）
#   type=3 → distance 3927 m、duration 3727 s = 步行（路网，均速 3.8 km/h）
# 网关原注释「1 驾车 0 步行 3 骑行」与实测不符，已按实测定正（只动注释，签名未改）。
_DISTANCE_TYPE: dict[str, int] = {"driving": 1, "walking": 3}

# 高德路径规划支持的方式（v3 driving/walking + v4 bicycling）
_ROUTE_MODES: tuple[str, ...] = ("driving", "walking", "bicycling")

# 无 key 降级的正圆速度：名义经验值，不是实测常量（在线口径实测参考：驾车一单 17.1 km/h、
# 步行 3.8 km/h；驾车取 20 作城区名义上界）。降级结果一律打 degraded=True。
_AVG_SPEED_KMH: dict[str, float] = {"driving": 20.0, "walking": 4.0}



class RouteUnavailable(RuntimeError):
    """空间计算能力不可用（无 key 且无兜底 / 高德无结果 / 网关异常）。

    路由层把 AmapUnavailable、AmapApiError 全部收敛成本类型，
    上层（Skill → L2 图）不会看到高德专有异常。
    """


class PlaceRef(BaseModel):
    """地点引用：地名 or WGS84 坐标，二者必给其一（供 Skill 入参复用，MCP 自动导出 Schema）。"""

    name: str | None = Field(default=None, max_length=60, description="地名/地址（走高德或库内兜底）")
    lonlat: tuple[float, float] | None = Field(default=None, description="(lon, lat) WGS84")

    @model_validator(mode="after")
    def _exactly_one(self) -> PlaceRef:
        if (self.name is None) == (self.lonlat is None):
            raise ValueError("PlaceRef 必须且只能给 name 或 lonlat 之一")
        if self.lonlat is not None:
            lon, lat = self.lonlat
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("lonlat 超出经纬度范围")
        return self


@dataclass(frozen=True)
class ResolvedPlace:
    """定位结果。lon/lat 为 WGS84（可直接进 SQL 与状态），gcj02 供图层渲染。"""

    lon: float
    lat: float
    name: str
    source: str                      # amap | place_dict | dim_poi | input
    gcj02: tuple[float, float]
    address: str = ""
    poi_id: int | None = None


@dataclass(frozen=True)
class RouteResult:
    """路径规划结果（polyline 双坐标系，WGS84 为主）。"""

    mode: str
    origin: ResolvedPlace
    destination: ResolvedPlace
    distance_m: float
    duration_s: float
    polyline_wgs84: tuple[tuple[float, float], ...]
    polyline_gcj02: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class IsoVertex:
    """等时圈的一个方位顶点：该方位角上「耗时 ≤ 预算」的最远采样点。"""

    bearing_deg: float
    radius_m: float
    duration_s: float
    lon: float                       # WGS84
    lat: float
    gcj02: tuple[float, float]


@dataclass(frozen=True)
class IsochroneResult:
    """等时圈近似结果（架构 §7：高德出多边形 → OB ST_Contains 批量筛选）。

    saturated    —— 有方位角命中最大采样半径，真实边界可能被采样上限截断；
    under_sampled—— 无任何采样点满足时间预算的方位角个数：该顶点半径退化到 _NO_SAMPLE_FALLBACK_M，
                    duration_s 回填最近档实测耗时（必然 > 预算，不会伪装成 0 s 可达）；
    degraded     —— 无 key 时的正圆近似。
    """

    mode: str
    minutes: float
    center: ResolvedPlace
    vertices: tuple[IsoVertex, ...]
    polygon_wkt_wgs84: str
    polygon_wkt_gcj02: str
    degraded: bool
    saturated: bool
    under_sampled: int
    sampled_points: int


class SpatialRouter:
    """高德在线能力 + 库内兜底的统一路由与归一出口（L4）。"""

    def __init__(self, amap: AmapGateway, settings: Settings | None = None,
                 query_builder: QueryBuilder | None = None,
                 place_dict: dict[str, tuple[float, float]] | None = None) -> None:
        self.amap = amap
        self.s = settings or get_settings()
        # 仅 geocode 的库内兜底需要（铁律 1：SQL 只能经 QueryBuilder；铁律 2：兜底属路由层）
        self.query_builder = query_builder
        self.place_dict = PLACE_COORDS if place_dict is None else place_dict

    @property
    def available(self) -> bool:
        """高德在线能力是否可用（无 key 时全部走兜底，不产生外部请求）。"""
        return self.amap.available

    # ------------------------------------------------------------------
    # 地理编码
    # ------------------------------------------------------------------
    def geocode(self, address: str, city: str = "") -> ResolvedPlace:
        text = (address or "").strip()
        if not text:
            raise RouteUnavailable("地址为空，无法地理编码")
        # 城市限定缺省走配置（默认「成都」，与本库数据范围一致）：无 city 时高德按全国
        # 匹配同名路，实测把「春熙路」定到了外省同名路，等时圈整体漂到凉山。
        city = (city or self.s.default_city).strip()

        note = ""
        if self.amap.available:
            try:
                loc = self.amap.geocode(text, city)
            except (AmapUnavailable, AmapApiError) as exc:
                loc = None
                note = f"高德调用失败: {exc}"
            if loc is not None:
                return self._from_gcj(loc, name=text, source="amap", address=text)
            note = note or "高德未返回该地址的坐标"

        hit = self._lookup_dict(text)
        if hit is not None:
            key, (lon, lat) = hit
            return ResolvedPlace(lon=lon, lat=lat, name=key, source="place_dict",
                                 gcj02=wgs84_to_gcj02(lon, lat), address=key)

        poi = self._lookup_poi(text, city)
        if poi is not None:
            return poi

        raise RouteUnavailable(
            f"无法定位「{text}」：{'高德不可用（未配置 GEOMIND_AMAP_KEY）' if not self.amap.available else note}；"
            f"意图词表与 dim_poi 均未命中")

    def resolve(self, ref: PlaceRef) -> ResolvedPlace:
        """PlaceRef → ResolvedPlace（坐标入参视为 WGS84 原样采用，不转换）。"""
        if ref.lonlat is not None:
            lon, lat = ref.lonlat
            return ResolvedPlace(lon=lon, lat=lat, name=f"{lon:.5f},{lat:.5f}",
                                 source="input", gcj02=wgs84_to_gcj02(lon, lat))
        assert ref.name is not None      # _exactly_one 已保证
        return self.geocode(ref.name)

    # ------------------------------------------------------------------
    # 路径规划（无兜底：OB 无路网，直线近似不做）
    # ------------------------------------------------------------------
    def plan_route(self, mode: RouteMode, origin: PlaceRef | tuple[float, float],
                   destination: PlaceRef | tuple[float, float]) -> RouteResult:
        if mode not in _ROUTE_MODES:
            raise ValueError(f"不支持的出行方式: {mode}（高德路径规划支持 {list(_ROUTE_MODES)}）")
        o = self._place(origin)
        d = self._place(destination)
        try:
            res = self.amap.route_plan(mode, o.gcj02, d.gcj02)
        except (AmapUnavailable, AmapApiError) as exc:
            raise RouteUnavailable(
                f"路径规划不可用：{exc}。SeekDB 无路网数据，直线近似会给出空间谎言，故不做兜底（架构 §7）"
            ) from exc
        if res is None:
            raise RouteUnavailable(f"{o.name} → {d.name} 高德未规划出可行路线")
        # 网关用 (0,0) 占位表示无法解析的分段（amap_gateway.route_plan），这里剔除
        gcj_pts = tuple(p for p in res["polyline"] if p != (0.0, 0.0))
        wgs_pts = tuple(gcj02_to_wgs84(p[0], p[1]) for p in gcj_pts)
        return RouteResult(mode=mode, origin=o, destination=d,
                           distance_m=float(res["distance_m"]), duration_s=float(res["duration_s"]),
                           polyline_wgs84=wgs_pts, polyline_gcj02=gcj_pts)

    # ------------------------------------------------------------------
    # 等时圈（可达性）
    # ------------------------------------------------------------------
    def isochrone(self, center: PlaceRef | tuple[float, float], minutes: float,
                  mode: IsoMode = "driving") -> IsochroneResult:
        if mode not in _DISTANCE_TYPE:
            raise ValueError(
                f"等时圈只支持 {sorted(_DISTANCE_TYPE)}：高德 /v3/distance 实测无骑行档"
                f"（type 0=直线、1=驾车、3=步行），骑行请走 path_planning")
        if not 0 < minutes <= 120:
            raise ValueError("minutes 须在 (0, 120] 分钟内")
        c = self._place(center)
        if not self.amap.available:
            return self._iso_circle(c, minutes, mode)
        try:
            return self._iso_sampled(c, minutes, mode)
        except (AmapUnavailable, AmapApiError) as exc:
            # 有 key 却调用失败：不降级成正圆，避免把网络故障粉饰成几何近似
            raise RouteUnavailable(f"等时圈采样失败：{exc}") from exc

    # ------------------------------------------------------------------
    # 内部：等时圈两种形态
    # ------------------------------------------------------------------
    def _iso_sampled(self, c: ResolvedPlace, minutes: float, mode: str) -> IsochroneResult:
        budget_s = minutes * 60.0
        origins = [_destination_point(c.gcj02, b, r) for b in _BEARINGS for r in _RADII_M]
        results = self.amap.distance_matrix(origins, c.gcj02, mode=_DISTANCE_TYPE[mode])
        dur_by_index: dict[int, float] = {}
        for row in results:
            idx = row.get("origin_index")
            if idx is not None:
                dur_by_index[int(idx)] = float(row.get("duration_s", 0.0))

        n_ring = len(_RADII_M)
        vertices: list[IsoVertex] = []
        under_sampled = 0
        saturated = False
        for bi, bearing in enumerate(_BEARINGS):
            chosen: tuple[float, float] | None = None
            for ri, radius in enumerate(_RADII_M):
                dur = dur_by_index.get(bi * n_ring + ri)
                if dur is not None and dur <= budget_s:
                    chosen = (radius, dur)      # 半径升序遍历，最后一次赋值即最远合格档
                    if radius == _RADII_M[-1]:
                        saturated = True
            if chosen is None:
                # 该方位角没有任何 ≤预算 的采样点：半径退化到最近档一半以保证多边形闭合，
                # 但 duration_s 如实回填「最近档的实测耗时」（必然 > 预算），
                # 否则顶点上会出现 0 s 这种被读成「0 秒可达」的假数。
                inner = dur_by_index.get(bi * n_ring)
                chosen = (_NO_SAMPLE_FALLBACK_M, inner if inner is not None else 0.0)
                under_sampled += 1
            pt_gcj = _destination_point(c.gcj02, bearing, chosen[0])
            wgs = gcj02_to_wgs84(*pt_gcj)
            vertices.append(IsoVertex(
                bearing_deg=bearing, radius_m=round(chosen[0], 1), duration_s=round(chosen[1], 1),
                lon=round(wgs[0], 6), lat=round(wgs[1], 6),
                gcj02=(round(pt_gcj[0], 6), round(pt_gcj[1], 6)),
            ))
        if under_sampled == len(_BEARINGS):
            raise RouteUnavailable(
                f"{minutes:g} 分钟内连最近采样档（{_RADII_M[0]:g} m）都无法到达，等时圈不成立")

        return self._assemble(c, minutes, mode, vertices, degraded=False,
                              saturated=saturated, under_sampled=under_sampled,
                              sampled_points=len(origins))

    def _iso_circle(self, c: ResolvedPlace, minutes: float, mode: str) -> IsochroneResult:
        """无 key 降级：半径 = T × 平均速度 的正圆近似（12 顶点，degraded=True）。"""
        radius_m = _AVG_SPEED_KMH[mode] * 1000.0 / 60.0 * minutes
        budget_s = minutes * 60.0
        vertices = []
        for bearing in _BEARINGS:
            pt_gcj = _destination_point(c.gcj02, bearing, radius_m)
            wgs = gcj02_to_wgs84(*pt_gcj)
            vertices.append(IsoVertex(
                bearing_deg=bearing, radius_m=round(radius_m, 1), duration_s=round(budget_s, 1),
                lon=round(wgs[0], 6), lat=round(wgs[1], 6),
                gcj02=(round(pt_gcj[0], 6), round(pt_gcj[1], 6)),
            ))
        return self._assemble(c, minutes, mode, vertices, degraded=True,
                              saturated=False, under_sampled=0, sampled_points=0)

    @staticmethod
    def _assemble(center: ResolvedPlace, minutes: float, mode: str,
                  vertices: Sequence[IsoVertex], *, degraded: bool, saturated: bool,
                  under_sampled: int, sampled_points: int) -> IsochroneResult:
        pts_wgs = [(v.lon, v.lat) for v in vertices]
        pts_gcj = [v.gcj02 for v in vertices]
        return IsochroneResult(
            mode=mode, minutes=minutes, center=center, vertices=tuple(vertices),
            polygon_wkt_wgs84=_polygon_wkt(pts_wgs), polygon_wkt_gcj02=_polygon_wkt(pts_gcj),
            degraded=degraded, saturated=saturated, under_sampled=under_sampled,
            sampled_points=sampled_points,
        )

    # ------------------------------------------------------------------
    # 内部：兜底链与坐标助手
    # ------------------------------------------------------------------
    def _place(self, ref: PlaceRef | tuple[float, float]) -> ResolvedPlace:
        if isinstance(ref, tuple):
            return self.resolve(PlaceRef(lonlat=ref))
        return self.resolve(ref)

    def _from_gcj(self, loc: tuple[float, float], *, name: str, source: str,
                  address: str) -> ResolvedPlace:
        lon, lat = gcj02_to_wgs84(loc[0], loc[1])
        return ResolvedPlace(lon=round(lon, 6), lat=round(lat, 6), name=name, source=source,
                             gcj02=(round(loc[0], 6), round(loc[1], 6)), address=address)

    def _lookup_dict(self, text: str) -> tuple[str, tuple[float, float]] | None:
        """意图地名词表：精确 → 小写 → 最长子串（"春熙路1号" 命中 "春熙路"）。"""
        low = text.lower()
        for cand in (text, low):
            pos = self.place_dict.get(cand)
            if pos is not None:
                return cand, pos
        best: tuple[str, tuple[float, float]] | None = None
        for key, pos in self.place_dict.items():
            if key and (key in text or key.lower() in low) and (
                    best is None or len(key) > len(best[0])):
                best = (key, pos)
        return best

    def _lookup_poi(self, text: str, city: str) -> ResolvedPlace | None:
        """dim_poi 名称兜底：走 QueryBuilder 模板 + 三层卡点（铁律 1）。失败不静默吞根因。"""
        if self.query_builder is None:
            return None
        reason: str | None = None
        rows: list[dict[str, Any]] = []
        try:
            cq = self.query_builder.compile_poi_by_name(
                PoiNameSpec(name=text, city=city or None))
            self.query_builder.validate(cq)
            self.query_builder.dry_run(cq)
            self.query_builder.assert_plan(cq)
            rows = self.query_builder.ob.execute(cq.sql, cq.params)
        except (CompileError, SQLValidationError, DryRunError, PlanAssertionError) as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - DB 驱动层异常类型不固定（OBError 等）
            reason = f"库内兜底查询失败: {exc}"
        if reason:
            rows = []
        if not rows:
            if reason:
                # 交给上层拼进 RouteUnavailable 提示，避免"查到了但没说为什么没查到"
                raise RouteUnavailable(f"dim_poi 兜底不可用：{reason}")
            return None
        r = rows[0]
        lon, lat = r.get("lon"), r.get("lat")
        if lon is None or lat is None:
            return None
        glng, glat = wgs84_to_gcj02(float(lon), float(lat))
        return ResolvedPlace(
            lon=round(float(lon), 6), lat=round(float(lat), 6),
            name=str(r.get("name", text)), source="dim_poi",
            gcj02=(round(glng, 6), round(glat, 6)),
            address=str(r.get("address") or ""),
            poi_id=int(r["poi_id"]) if r.get("poi_id") is not None else None,
        )


def _destination_point(lonlat: tuple[float, float], bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """极坐标推算：起点 + 方位角 + 距离 → 终点（GCJ-02 平面近似，与库内 111320 m/度同口径）。

    这是等时圈**边界构造**（与 query_builder._wkt_bbox 同类），不是空间裁决；
    包含/距离判定仍由 OB ST_Contains / ST_Distance_Sphere 执行（架构 §6.7）。
    """
    lon, lat = lonlat
    rad = math.radians(bearing_deg)
    d_lat = (distance_m * math.cos(rad)) / _M_PER_DEG
    cos_lat = max(abs(math.cos(math.radians(lat))), 1e-6)
    d_lon = (distance_m * math.sin(rad)) / (_M_PER_DEG * cos_lat)
    return (lon + d_lon, lat + d_lat)


def _polygon_wkt(points: Sequence[tuple[float, float]]) -> str:
    """顶点按序连成闭合 POLYGON WKT（SRID 0，与 §6.3 DDL 与 SpatialConstraint.polygon_wkt 一致）。"""
    if len(points) < 3:
        raise RouteUnavailable(f"等时圈顶点不足 3 个：{len(points)}")
    ring = list(points) + [points[0]]
    return "POLYGON((" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in ring) + "))"


def isochrone_geojson(result: IsochroneResult, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    """等时圈 GeoJSON Polygon（GCJ-02，供 §9.3 图层与内联 data_ref 使用）。

    放在路由层而非 Skill 层：坐标选择（GCJ-02 图层）属表达归一，不是业务判断。
    """
    ring = [[round(x, 6), round(y, 6)] for x, y in
            [v.gcj02 for v in result.vertices] + [result.vertices[0].gcj02]]
    props: dict[str, Any] = {
        "kind": "isochrone", "mode": result.mode, "minutes": result.minutes,
        "degraded": result.degraded, "saturated": result.saturated,
        "under_sampled": result.under_sampled, "sampled_points": result.sampled_points,
    }
    props.update(properties or {})
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": props}]}


__all__ = [
    "IsoVertex",
    "IsochroneResult",
    "PlaceRef",
    "ResolvedPlace",
    "RouteResult",
    "RouteUnavailable",
    "SpatialRouter",
    "isochrone_geojson",
]
