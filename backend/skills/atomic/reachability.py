"""原子 Skill：可达性分析 / 等时圈（架构 §2.2 L3「可达性分析」、§11「可达性分析：低成本」）。

等时圈的产出方式（架构 §7 归属表 + 高德无等时圈 Web 服务这一事实）：
  · 在线：路由层用距离测量做 12 方位角 × 5 档半径 = 60 点采样，取「耗时 ≤ T」的最远点
    连成多边形（近似，输出如实带 saturated / under_sampled / sampled_points）；
  · 无 key：降级为「半径 = T × 平均速度」的正圆近似并打 degraded=True。
多边形再交给 SeekDB 做批量筛选（§7「高德出多边形 → OB ST_Contains 批量过滤」，
SpatialConstraint.polygon_wkt 已支持）。

铁律 2：本 Skill 不算坐标、不拼 SQL，只调 router；render_spec 只做契约装配。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from backend.api.contracts.map_spec import LayerSource, LayerSpec
from backend.services.spatial_router import (
    IsochroneResult,
    IsoMode,
    PlaceRef,
    RouteUnavailable,
    isochrone_geojson,
)
from backend.skills.atomic.geocode import PlaceOut
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill, elapsed_ms_since
from backend.skills.registry import register_skill

_SAMPLED_EVIDENCE = {"type": "amap", "ref": "distance_matrix",
                     "note": "高德在线：12 方位×5 档半径 60 点采样近似等时圈"}
_CIRCLE_EVIDENCE = {"type": "data", "ref": "iso_circle_approx",
                    "note": "无 key 降级：T×平均速度正圆近似（非路网等时圈，degraded）"}
_FAILED_EVIDENCE = {"type": "amap", "ref": "distance_matrix",
                    "note": "高德在线采样失败，不产出等时圈"}

# 图层样式取自架构 §9.3 的 isochrone 示例条目；dash/width 也在此声明，
# 前端渲染器只消费不决定（§9.6 前端零业务判断）。
# 颜色用 6 位 hex + 独立 opacity 键：AMap 2.0 对 rgba() 串与 3 位 hex 解析不可靠
# （实测 rgba 填充整个多边形不可见），前端渲染器按这两个键拆成 fillColor+fillOpacity。
_ISO_STYLE = {"fill": "#338AFF", "fillOpacity": 0.18, "stroke": "#1677FF",
              "dash": True, "width": 2.5}


class ReachabilityInput(BaseModel):
    center: PlaceRef = Field(description="起点：地名或 WGS84 坐标")
    minutes: int = Field(default=15, ge=1, le=120, description="时间预算（分钟）")
    mode: IsoMode = Field(default="driving", description="driving | walking（高德距离测量无骑行档）")


class IsoVertexOut(BaseModel):
    """一个方位角顶点（坐标 WGS84，与库内一致）。"""

    bearing_deg: float
    radius_m: float
    duration_s: float
    lon: float
    lat: float


class ReachabilityOutput(BaseModel):
    ok: bool
    mode: str = "driving"
    minutes: int = 15
    center: PlaceOut | None = None
    vertices: list[IsoVertexOut] = Field(default_factory=list)
    polygon_wkt: str = Field(default="", description="WGS84 闭合 POLYGON，供 ST_Contains 下推")
    polygon_wkt_gcj02: str = Field(default="", description="GCJ-02 版（§9.3 图层坐标系）")
    geojson: dict[str, Any] = Field(default_factory=dict, description="内联等时圈面（GCJ-02）")
    render_spec: dict[str, Any] = Field(default_factory=dict, description="Map Spec 图层声明（§9.3）")
    degraded: bool = True
    saturated: bool = Field(default=False, description="有方位角命中最大采样半径，边界可能被截断")
    under_sampled: int = Field(default=0, description="无合格采样点的方位角个数（退化到最近档一半）")
    sampled_points: int = 0
    reason: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float = 0.0


@register_skill
class ReachabilitySkill(SpatialSkill[ReachabilityInput, ReachabilityOutput]):
    """可达性分析：T 分钟内可达范围的多边形（在线采样近似 / 无 key 正圆降级）。"""

    metadata: SkillMetadata
    input_schema = ReachabilityInput
    output_schema = ReachabilityOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="reachability",
            version="1.0.0",
            scene="atomic",
            description="可达性分析：等时圈多边形（12 顶点 WGS84/GCJ-02 双版 WKT + 内联 GeoJSON + isochrone 图层声明）",
            intent_examples=["开车 15 分钟能到多大范围", "可达性怎么样", "步行 10 分钟覆盖哪些网格"],
        )

    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    def run(self, inputs: ReachabilityInput, ctx: SkillContext) -> ReachabilityOutput:
        router = ctx.services.router
        t0 = time.perf_counter()
        try:
            res = router.isochrone(inputs.center, minutes=float(inputs.minutes), mode=inputs.mode)
        except RouteUnavailable as exc:
            return ReachabilityOutput(
                ok=False, mode=inputs.mode, minutes=inputs.minutes, degraded=True,
                reason=str(exc), evidence=[_FAILED_EVIDENCE], elapsed_ms=elapsed_ms_since(t0),
            )
        return self._to_output(res, ctx, inputs, elapsed_ms=elapsed_ms_since(t0))

    # ------------------------------------------------------------------
    def _to_output(self, res: IsochroneResult, ctx: SkillContext,
                   inputs: ReachabilityInput, *, elapsed_ms: float) -> ReachabilityOutput:
        c = res.center
        return ReachabilityOutput(
            ok=True, mode=res.mode, minutes=inputs.minutes,
            center=PlaceOut(name=c.name, lon=c.lon, lat=c.lat, source=c.source),
            vertices=[IsoVertexOut(bearing_deg=v.bearing_deg, radius_m=v.radius_m,
                                   duration_s=v.duration_s, lon=v.lon, lat=v.lat)
                      for v in res.vertices],
            polygon_wkt=res.polygon_wkt_wgs84,
            polygon_wkt_gcj02=res.polygon_wkt_gcj02,
            geojson=isochrone_geojson(res),
            render_spec=_layer_spec(ctx.session_id).model_dump(mode="json"),
            degraded=res.degraded, saturated=res.saturated, under_sampled=res.under_sampled,
            sampled_points=res.sampled_points,
            evidence=[_CIRCLE_EVIDENCE if res.degraded else _SAMPLED_EVIDENCE],
            elapsed_ms=elapsed_ms,
        )


def _layer_spec(session_id: str) -> LayerSpec:
    """isochrone 图层声明（架构 §9.3 kind 注册表；source.type=geojson）。

    href 指向 §9.3 的图层数据端点——该端点服务 l_iso 与前端渲染器同为任务卡 2 范围，
    本轮先把声明与内联几何（输出 geojson）交付到位，不丢数据。
    """
    return LayerSpec(
        id="l_iso", kind="isochrone",
        source=LayerSource(href=f"/api/sessions/{session_id}/layers/l_iso", type="geojson"),
        data_ref="isochrone", style=dict(_ISO_STYLE), interactions=[], status="ready",
        derived_from=["reachability"],
    )


__all__ = ["ReachabilityInput", "ReachabilityOutput", "ReachabilitySkill"]
