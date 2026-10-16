"""原子 Skill：路径规划（架构 §2.2 L3「路径规划」/ §7「驾车/步行/骑行：高德 API，OB 无路网」）。

**无兜底**：SeekDB 没有路网数据，用直线距离近似是空间谎言（架构 §7 归属表把路径
规划划给高德）。路由层抛 RouteUnavailable，本 Skill 收敛成 ok=False 的诚实结果，
不让高德专有异常进入 L2 图执行（架构 §2.2 L2 图骨架不为此改动）。

polyline 只输出 WGS84（供入库/复算）：一条驾车路线可能几百个点，双坐标系会翻倍
MCP/LLM 可见负载；渲染所需的 GCJ-02 与 poi/heat 图层同口径，由后端 GeoJSON 端点
出口时转换（架构 §9.6 职责边界表、backend/api/geo.py）。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from backend.services.spatial_router import PlaceRef, RouteMode, RouteUnavailable
from backend.skills.atomic.geocode import PlaceOut
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill, elapsed_ms_since
from backend.skills.registry import register_skill

_ONLINE_EVIDENCE = {"type": "amap", "ref": "route_plan", "note": "高德在线路径规划（按起终点+策略缓存 1h）"}
_FAILED_EVIDENCE = {"type": "amap", "ref": "route_plan",
                    "note": "高德在线不可用且 OB 无路网，不做直线近似"}


class PathPlanningInput(BaseModel):
    mode: RouteMode = Field(default="driving", description="driving | walking | bicycling")
    origin: PlaceRef = Field(description="起点：地名（走地理编码）或 WGS84 坐标")
    destination: PlaceRef = Field(description="终点：地名（走地理编码）或 WGS84 坐标")


class PathPlanningOutput(BaseModel):
    ok: bool
    mode: str = "driving"
    origin: PlaceOut | None = None
    destination: PlaceOut | None = None
    distance_m: float | None = None
    duration_s: float | None = None
    polyline: list[tuple[float, float]] = Field(default_factory=list, description="WGS84 折线")
    point_count: int = 0
    degraded: bool = True
    reason: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float = 0.0


@register_skill
class PathPlanningSkill(SpatialSkill[PathPlanningInput, PathPlanningOutput]):
    """起终点 → 路网距离/耗时/折线（仅高德在线；不可用时如实失败，不造近似路线）。"""

    metadata: SkillMetadata
    input_schema = PathPlanningInput
    output_schema = PathPlanningOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="path_planning",
            version="1.0.0",
            scene="atomic",
            description="路径规划：驾车/步行/骑行的路网距离、耗时与折线（高德在线，无库内兜底）",
            intent_examples=["从春熙路到天府广场开车多久", "怎么走过去", "骑行路线规划"],
        )

    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    def run(self, inputs: PathPlanningInput, ctx: SkillContext) -> PathPlanningOutput:
        router = ctx.services.router
        t0 = time.perf_counter()
        try:
            r = router.plan_route(inputs.mode, inputs.origin, inputs.destination)
        except RouteUnavailable as exc:
            return PathPlanningOutput(
                ok=False, mode=inputs.mode, degraded=True, reason=str(exc),
                evidence=[_FAILED_EVIDENCE], elapsed_ms=elapsed_ms_since(t0),
            )
        return PathPlanningOutput(
            ok=True, mode=r.mode,
            origin=PlaceOut(name=r.origin.name, lon=r.origin.lon, lat=r.origin.lat, source=r.origin.source),
            destination=PlaceOut(name=r.destination.name, lon=r.destination.lon,
                                 lat=r.destination.lat, source=r.destination.source),
            distance_m=r.distance_m, duration_s=r.duration_s,
            polyline=list(r.polyline_wgs84), point_count=len(r.polyline_wgs84),
            degraded=False, evidence=[_ONLINE_EVIDENCE], elapsed_ms=elapsed_ms_since(t0),
        )


__all__ = ["PathPlanningInput", "PathPlanningOutput", "PathPlanningSkill"]
